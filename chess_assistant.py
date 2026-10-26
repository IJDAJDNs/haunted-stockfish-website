"""Haunted Stockfish v1.15 - a Stockfish move assistant for chess.com games.

Setup:  python -m pip install -U selenium python-chess customtkinter keyboard edge-tts
Needs:  stockfish.exe next to this script, and Google Chrome.

TIP: start it with  pythonw chess_assistant_v6.py  (or rename it to .pyw) so no
black console window exists. The panic key hides the dashboard completely
(no taskbar icon, not in Alt+Tab) and also hides the console if there is one.

Default keys (all rebindable on the Keys tab):
  Delete  repeat the move        F9      PANIC: hide / show everything
  F10     auto-move on/off       End     play the suggested move once
  F2      explain their last move out loud
  PageUp  spoken move callouts on/off
  F6 mute, F7 moves shown, F8 pause, PageDown voice persona, Q reveal arrows
  Insert  compact view (hides the tabs and log - just the banner + deck)

Optional free voice: edge-tts (pip install edge-tts) - see the Spooky tab >
Voice persona.
"""

import array
import asyncio
import difflib
import html
import json
import math
import os
import queue
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import wave

try:
    import chess
    import chess.engine
    import chess.pgn
    import customtkinter as ctk
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
except ImportError as _imp_err:
    _msg = (
        f"A required package is missing: {_imp_err}\n\n"
        "Open Command Prompt and run:\n"
        f'"{sys.executable}" -m pip install -U selenium python-chess customtkinter keyboard edge-tts\n\n'
        "Then start the app again."
    )
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "crash_log.txt"), "w", encoding="utf-8") as _f:
            _f.write(_msg)
    except Exception:
        pass
    print(_msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, _msg, "Haunted Stockfish", 0x10)
    except Exception:
        pass
    raise SystemExit(1)


# ---- license gate (shared with the dashboard) ----
import licensing as _lic


def _license_die(reason):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "License problem:\n\n" + str(reason) + "\n\nOpen the Haunted Dashboard to fix it.",
            "Haunted Stockfish", 0x10)
    except Exception:
        pass
    os._exit(1)


# ---------------------------------------------------------------------------
# Windows: don't let helper programs open their own console windows
# ---------------------------------------------------------------------------
# Stockfish and Selenium's chromedriver are console programs. A .pyw has no
# console of its own, so Windows gives each of them a new black console window,
# and closing that window kills the program (the assistant "stops working").
# Starting them with CREATE_NO_WINDOW keeps them invisible and un-closable by
# accident.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def no_window_kwargs():
    """Extra keyword arguments for launching a helper program silently."""
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def hide_child_consoles():
    """Make every program started by this app (including ones started inside
    python-chess and Selenium) run without a console window."""
    if os.name != "nt" or getattr(subprocess.Popen, "_haunted_no_window", False):
        return
    original_init = subprocess.Popen.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched_init
    subprocess.Popen._haunted_no_window = True


def make_chrome_service():
    """A chromedriver service that starts without a console window."""
    service = ChromeService()
    if os.name == "nt":
        for attr in ("creation_flags", "creationflags"):  # name differs by version
            try:
                setattr(service, attr, CREATE_NO_WINDOW)
            except Exception:
                pass
    return service


hide_child_consoles()

try:
    import keyboard  # python -m pip install keyboard
except Exception:
    keyboard = None

try:
    import winsound  # Windows only (used for the wind ambience)
except Exception:
    winsound = None

try:
    import edge_tts  # python -m pip install edge-tts  (neural voices, needs internet)
except Exception:
    edge_tts = None

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

DEBUG_PORT = 9222
START_URL = "https://www.chess.com/play/computer"

# The assistant only runs on chess.com bot ("computer") games. On any other
# page it pauses and does nothing (unless Test mode is ticked).
ALLOWED_URL_PART = "/computer"

APP_VERSION = "v1.15"

# Personal build: the Test mode switch (Play tab) is available, so the assistant
# can also be used on a chess.com game against your own phone / second account.
ALLOW_TEST_MODE = True  # Test mode: use it on your own games (no auto-move while it is on)

DEFAULT_HOTKEY = "delete"

# Extra shortcuts: action -> (label shown on the Keys tab, default key).
EXTRA_ACTIONS = {
    "auto": ("Auto-move on / off", "f10"),
    "playnow": ("Play the suggested move once", "end"),
    "explain": ("Explain their last move (spoken)", "f2"),
    "quality": ("Spoken move callouts on / off", "page up"),
    "compact": ("Compact view on / off", "insert"),
    "mute": ("Mute / unmute the voice", "f6"),
    "lines": ("Cycle moves shown (1-2-3)", "f7"),
    "pause": ("Pause / resume the assistant", "f8"),
    "persona": ("Next voice persona", "page down"),
    "fullscreen": ("Fullscreen cockpit on / off", "f11"),
    "copyfen": ("Copy the position (FEN)", "f4"),
}
DEFAULT_REVEAL_KEY = "q"

# One-click bundles of settings. Keys are the labels of real settings.
PRESETS = {
    "Learner": {
        "Show the recommended moves": True,
        "Moves to show": "3",
        "When do they appear?": "Step by step",
        "Grade the opponent's moves in the log": True,
        "Say out loud how good their last move was": True,
        "Only speak for brilliant moves, mistakes and blunders": False,
    },
    "Showcase": {
        "Animate the haunted scene": True,
        "Spookiness": 10,
        "Sparks follow your cursor over the scene": True,
        "Depth: scene layers slide with your mouse": True,
        "Drifting motes in the window background": True,
    },
    "Lightweight": {
        "Animate the haunted scene": False,
        "Drifting motes in the window background": False,
        "Haunted wind and creaking wood (Windows)": False,
        "Spookiness": 2,
    },
}

# Words that mean the same thing, so searching "sound" finds the voice settings.
SEARCH_SYNONYMS = [
    "background backdrop wallpaper image picture particles rain embers fireflies"
    " ash stars motes palette gradient dim darkness",
    "voice sound speak spoken audio talk say tts narrator mute volume",
    "arrow arrows overlay board highlight squares draw",
    "colour color theme skin look appearance",
    "panic hide stealth boss invisible hidden taskbar",
    "auto automatic autoplay click",
    "speed pace fast slow tempo",
    "engine stockfish strength elo skill depth think",
    "scene animation spooky bats ghosts halloween lightning",
    "key keys hotkey shortcut keyboard bind",
    "pgn export save game copy analysis",
    "neural edge voice",
    "window top opacity transparent",
    "fullscreen full screen maximize cockpit big large",
    "fen position clipboard copy board",
]

ORB_SQ = 30  # size of one square on the crystal-ball mini board

# (light square, dark square). "Haunted" is worked out from the colour theme.
BOARD_STYLES = {
    "Haunted": None,
    "Classic wood": ("#f0d9b5", "#b58863"),
    "Ice": ("#dce9f5", "#6f93b8"),
    "Slime": ("#d6f5c4", "#4c8a3a"),
    "Midnight": ("#4a4466", "#1e1a33"),
}

INSTANT_MODE = "Instant"
DELAY_MODE = "Delayed"
STEP_MODE = "Step by step"
REVEAL_MODES = [INSTANT_MODE, DELAY_MODE, STEP_MODE]
# Older saved settings used these names.
OLD_MODE_NAMES = {
    "Instantly": INSTANT_MODE,
    "After 1 second": DELAY_MODE,
    "After delay": DELAY_MODE,
    "Step by step (key)": STEP_MODE,
}

# Where the app lives. Works both as a script and as a PyInstaller EXE.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _pick_data_dir():
    """Settings, saved games and the Chrome profile go next to the app, or in
    %APPDATA% if that folder is read-only (for example Program Files)."""
    if os.access(APP_DIR, os.W_OK):
        return APP_DIR
    try:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "HauntedStockfish")
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return APP_DIR


DATA_DIR = _pick_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "assistant_settings.json")


def find_stockfish():
    """stockfish.exe (or any stockfish*.exe) next to the app or in a stockfish folder."""
    if not _lic.check_saved()[0]:      # engine is withheld without a valid license
        return None
    for folder in (APP_DIR, os.path.join(APP_DIR, "stockfish"), getattr(sys, "_MEIPASS", APP_DIR)):
        direct = os.path.join(folder, "stockfish.exe")
        if os.path.exists(direct):
            return direct
        try:
            for name in sorted(os.listdir(folder)):
                low = name.lower()
                if low.startswith("stockfish") and low.endswith(".exe"):
                    return os.path.join(folder, name)
        except OSError:
            pass
    return None

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

# ---------------------------------------------------------------------------
# Halloween palette
# ---------------------------------------------------------------------------
BG = "#0e0914"
SURFACE = "#190f25"
SURFACE2 = "#2a1a3d"
PUMPKIN = "#ff7518"
PUMPKIN_HOVER = "#e0620d"
PURPLE = "#7b2fbe"
PURPLE_DARK = "#4b1d78"
SLIME = "#7dff4f"
SLIME_DARK = "#3c9a1f"
BLOOD = "#c62828"
BLOOD_HOVER = "#a31f1f"
CANDLE = "#ffd24a"
TEXT = "#f3e9ff"
MUTED = "#9d86b8"

# Colour themes. Pick one on the Spooky tab; it applies the next time you start.
THEMES = {
    "Haunted": {},
    "Blood Moon": dict(
        BG="#120709", SURFACE="#1f0d10", SURFACE2="#34161b", PUMPKIN="#ff3b3b",
        PUMPKIN_HOVER="#d92b2b", PURPLE="#8e1b2d", PURPLE_DARK="#4f1019",
        SLIME="#ffb347", SLIME_DARK="#a8641a", CANDLE="#ffe08a", MUTED="#b58a90",
    ),
    "Toxic Lab": dict(
        BG="#060f0a", SURFACE="#0c1c12", SURFACE2="#143222", PUMPKIN="#7dff4f",
        PUMPKIN_HOVER="#5fd636", PURPLE="#1f8f4d", PURPLE_DARK="#0f4a29",
        SLIME="#c6ff3d", SLIME_DARK="#4c9a14", CANDLE="#f4ff7a", MUTED="#7fb08f",
    ),
    "Midnight Ice": dict(
        BG="#060b14", SURFACE="#0d1626", SURFACE2="#162540", PUMPKIN="#4dc3ff",
        PUMPKIN_HOVER="#2fa6e6", PURPLE="#2f5fbe", PURPLE_DARK="#1b3a78",
        SLIME="#6dffd1", SLIME_DARK="#1f9a76", CANDLE="#ffe9a3", MUTED="#8aa0bd",
    ),
    "Royal Gold": dict(
        BG="#0b0a10", SURFACE="#15131f", SURFACE2="#241f36", PUMPKIN="#ffc83d",
        PUMPKIN_HOVER="#e0ac22", PURPLE="#6b4bd1", PURPLE_DARK="#3a2a85",
        SLIME="#7dff4f", SLIME_DARK="#3c9a1f", CANDLE="#fff0a8", MUTED="#a39bbf",
    ),
}


def _saved_theme():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return str(json.load(f).get("theme", "Haunted"))
    except Exception:
        return "Haunted"


THEME_NAME = _saved_theme()
if THEME_NAME not in THEMES:
    THEME_NAME = "Haunted"
globals().update(THEMES[THEME_NAME])

GRADE_COLORS = {
    "Brilliant": "#4dd8ff",
    "Best": SLIME,
    "Excellent": SLIME,
    "Good": "#b7ff9a",
    "Inaccuracy": CANDLE,
    "Mistake": PUMPKIN,
    "Blunder": "#ff4d4d",
}

STATUS_COLORS = {
    "idle": MUTED,
    "run": SLIME,
    "think": PUMPKIN,
    "pause": CANDLE,
    "error": "#ff5a5a",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Could not save settings: {e}")


def num_setting(cfg, key, default, lo, hi, cast=float):
    """Read a number from the saved settings, clamped to a sane range."""
    try:
        value = cast(cfg.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def port_is_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_chrome():
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def find_computer_tab(driver, test_mode=False):
    """True if the controlled tab is a page we're allowed to work on:
    a chess.com bot game, or (in test mode) any chess.com page.
    If the current tab isn't one, tries to switch to another tab that is."""

    def page_ok(url):
        if "chess.com" not in url:
            return False
        return True if test_mode else ALLOWED_URL_PART in url

    try:
        if page_ok(driver.current_url):
            return True
    except Exception:
        pass
    try:
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            if page_ok(driver.current_url):
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Page scripts
# ---------------------------------------------------------------------------

# Reads the pieces straight off the chess.com board. Each piece has classes
# like "piece wn square-72" = white knight on file 7 (g), rank 2. Highlighted
# squares show the last move played. "flipped" is true when Black is at the
# bottom of the screen (i.e. you are playing Black).
READ_BOARD_JS = r"""
var root = document.querySelector('wc-chess-board, chess-board') || document;
var pieces = [];
root.querySelectorAll('.piece').forEach(function(p) {
    var cls = Array.from(p.classList);
    var pc = cls.find(function(c) { return /^[wb][pnbrqk]$/.test(c); });
    var sq = cls.find(function(c) { return /^square-\d\d$/.test(c); });
    if (pc && sq) { pieces.push(pc + ':' + sq.slice(7)); }
});
var highlights = [];
root.querySelectorAll('.highlight').forEach(function(h) {
    var sq = Array.from(h.classList).find(function(c) { return /^square-\d\d$/.test(c); });
    if (sq) { highlights.push(sq.slice(7)); }
});
var flipped = null;
var lo = null, hi = null;
root.querySelectorAll('.piece').forEach(function(p) {
    var sq = Array.from(p.classList).find(function(c) { return /^square-\d\d$/.test(c); });
    if (!sq) { return; }
    var rank = parseInt(sq.charAt(8), 10);
    var top = p.getBoundingClientRect().top;
    if (lo === null || rank < lo.rank) { lo = {rank: rank, top: top}; }
    if (hi === null || rank > hi.rank) { hi = {rank: rank, top: top}; }
});
if (lo && hi && lo.rank !== hi.rank) { flipped = lo.top < hi.top; }
else if (root.classList) { flipped = root.classList.contains('flipped'); }
var ae = document.activeElement;
var typing = false;
if (ae) {
    var tn = (ae.tagName || '').toLowerCase();
    typing = tn === 'input' || tn === 'textarea' || tn === 'select' || ae.isContentEditable === true;
}
return {pieces: pieces, highlights: highlights, flipped: flipped, typing: typing};
"""

# Draws (or removes) the recommended moves on top of the board, in chess.com's
# arrow colors (green = best, yellow = 2nd, red = 3rd).
SET_OVERLAY_JS = r"""
// arguments[0] = list of moves like ["g1f3", "e2e4"] (best first), or null to clear
// arguments[1] = flipped (true when Black is at the bottom)
// arguments[2] = "arrows" or "squares"
// arguments[3] = opacity (0.2 - 1)
// Safe to call repeatedly: it only redraws when something changed.
var moves = arguments[0];
var flipped = arguments[1] === true;
var style = arguments[2] === 'squares' ? 'squares' : 'arrows';
var opacity = Math.max(0.2, Math.min(1, Number(arguments[3]) || 0.8));
var root = document.querySelector('wc-chess-board, chess-board');
if (!root) { return false; }
var old = root.querySelector('#sf-overlay');
if (!moves || !moves.length) { if (old) { old.remove(); } return true; }
var sig = moves.join(',') + '|' + flipped + '|' + style + '|' + opacity;
if (old && old.getAttribute('data-sig') === sig) { return true; }
if (old) { old.remove(); }
if (window.getComputedStyle(root).position === 'static') {
    root.style.position = 'relative';
}

var COLORS = ['#81b64c', '#ffaa00', '#eb6150'];
var NS = 'http://www.w3.org/2000/svg';

function center(sq) {
    var f = sq.charCodeAt(0) - 97;
    var r = parseInt(sq.charAt(1), 10) - 1;
    var x = flipped ? (7 - f + 0.5) * 12.5 : (f + 0.5) * 12.5;
    var y = flipped ? (r + 0.5) * 12.5 : (7 - r + 0.5) * 12.5;
    return {x: x, y: y};
}

var svg = document.createElementNS(NS, 'svg');
svg.setAttribute('id', 'sf-overlay');
svg.setAttribute('data-sig', sig);
svg.setAttribute('viewBox', '0 0 100 100');
svg.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;' +
                    'pointer-events:none;z-index:200;';

function drawArrow(uci, color) {
    var a = center(uci.substring(0, 2));
    var b = center(uci.substring(2, 4));
    var dx = b.x - a.x, dy = b.y - a.y;
    var adx = Math.abs(dx), ady = Math.abs(dy);
    var knight = (Math.abs(adx - 25) < 0.5 && Math.abs(ady - 12.5) < 0.5) ||
                 (Math.abs(adx - 12.5) < 0.5 && Math.abs(ady - 25) < 0.5);
    var pts = [a];
    var last = a;
    if (knight) {
        // Knight moves are drawn as an L, like chess.com's knight arrows.
        var corner = adx > ady ? {x: b.x, y: a.y} : {x: a.x, y: b.y};
        pts.push(corner);
        last = corner;
    }
    var sx = b.x - last.x, sy = b.y - last.y;
    var len = Math.sqrt(sx * sx + sy * sy) || 1;
    var ux = sx / len, uy = sy / len;
    var px = -uy, py = ux;
    var headLen = 4.6, headHalf = 3.0;
    var tipX = b.x - ux * 1.0, tipY = b.y - uy * 1.0;
    var baseX = tipX - ux * headLen, baseY = tipY - uy * headLen;
    pts.push({x: baseX, y: baseY});

    var g = document.createElementNS(NS, 'g');
    g.setAttribute('opacity', String(opacity));

    var shaft = document.createElementNS(NS, 'polyline');
    var coords = [];
    for (var i = 0; i < pts.length; i++) { coords.push(pts[i].x + ',' + pts[i].y); }
    shaft.setAttribute('points', coords.join(' '));
    shaft.setAttribute('fill', 'none');
    shaft.setAttribute('stroke', color);
    shaft.setAttribute('stroke-width', 2.4);
    shaft.setAttribute('stroke-linejoin', 'miter');
    shaft.setAttribute('stroke-linecap', 'butt');
    g.appendChild(shaft);

    var head = document.createElementNS(NS, 'polygon');
    head.setAttribute('points',
        tipX + ',' + tipY + ' ' +
        (baseX + px * headHalf) + ',' + (baseY + py * headHalf) + ' ' +
        (baseX - px * headHalf) + ',' + (baseY - py * headHalf));
    head.setAttribute('fill', color);
    g.appendChild(head);
    svg.appendChild(g);
}

function drawSquares(uci, color) {
    var squares = [uci.substring(0, 2), uci.substring(2, 4)];
    for (var i = 0; i < squares.length; i++) {
        var c = center(squares[i]);
        var r = document.createElementNS(NS, 'rect');
        r.setAttribute('x', c.x - 6.25);
        r.setAttribute('y', c.y - 6.25);
        r.setAttribute('width', 12.5);
        r.setAttribute('height', 12.5);
        r.setAttribute('fill', color);
        r.setAttribute('opacity', String(opacity * 0.8));
        svg.appendChild(r);
    }
}

// Draw the worst first so the best move ends up on top.
for (var k = Math.min(moves.length, 3) - 1; k >= 0; k--) {
    if (style === 'squares') { drawSquares(moves[k], COLORS[k]); }
    else { drawArrow(moves[k], COLORS[k]); }
}
root.appendChild(svg);
return true;
"""


def board_from_dom(state):
    """Build a python-chess Board from the page. Returns None if unclear."""
    if not state or not state.get("pieces"):
        raise ValueError("No pieces found on the page. Is a game board open?")

    board = chess.Board(None)  # empty board
    for item in state["pieces"]:
        pc, sq = item.split(":")
        square = chess.square(int(sq[0]) - 1, int(sq[1]) - 1)
        color = chess.WHITE if pc[0] == "w" else chess.BLACK
        board.set_piece_at(square, chess.Piece(chess.PIECE_SYMBOLS.index(pc[1]), color))

    # Whose turn? The piece sitting on a highlighted square just moved.
    mover_colors = set()
    for h in state.get("highlights", []):
        piece = board.piece_at(chess.square(int(h[0]) - 1, int(h[1]) - 1))
        if piece:
            mover_colors.add(piece.color)

    if len(mover_colors) > 1:
        return None  # a piece is selected mid-move; wait for a clear state
    board.turn = chess.WHITE if not mover_colors else (not mover_colors.pop())

    # Castling rights (best guess: king and rook on their home squares).
    rights = chess.BB_EMPTY
    for color, king_sq, rooks in (
        (chess.WHITE, chess.E1, ((chess.H1, chess.BB_H1), (chess.A1, chess.BB_A1))),
        (chess.BLACK, chess.E8, ((chess.H8, chess.BB_H8), (chess.A8, chess.BB_A8))),
    ):
        if board.piece_at(king_sq) == chess.Piece(chess.KING, color):
            for rook_sq, bb in rooks:
                if board.piece_at(rook_sq) == chess.Piece(chess.ROOK, color):
                    rights |= bb
    board.castling_rights = rights

    if not board.is_valid():
        return None  # likely mid-animation
    return board


# ---------------------------------------------------------------------------
# Voice: personas, move narration, and a speech queue that never overlaps
# ---------------------------------------------------------------------------

PIECE_WORDS = {
    chess.PAWN: "Pawn",
    chess.KNIGHT: "Knight",
    chess.BISHOP: "Bishop",
    chess.ROOK: "Rook",
    chess.QUEEN: "Queen",
    chess.KING: "King",
}

# Each persona changes the voice (pitch / speed / gender where Windows has a
# matching voice installed) and adds a little commentary.
PERSONAS = {
    "Plain": {},
    "Cryptkeeper": {
        "gender": "Male", "pitch": "x-low", "rate": "slow",
        "open": ["Hehehe.", "Welcome, little ghoul.", "Oh, delicious."],
        "alarm": "Ooh, that one hurt.",
        "check": "The king trembles!",
        "mate": "Sweet dreams!",
        "capture": "Another soul for the crypt!",
        "castle": "Hide behind the walls!",
        "promo": "A new monster is born!",
    },
    "Dracula": {
        "gender": "Male", "pitch": "low", "rate": "slow",
        "open": ["Ah, my friend.", "Listen carefully.", "The night is young."],
        "alarm": "Blood drains from my face.",
        "check": "He is cornered!",
        "mate": "Finished. Dark and final.",
        "capture": "A taste of blood!",
        "castle": "Retreat to the castle.",
        "promo": "Rise, my child!",
    },
    "Witch": {
        "gender": "Female", "pitch": "high", "rate": "medium",
        "open": ["Oh, obviously.", "Try to keep up, dearie.", "Fine. Slowly, then."],
        "alarm": "Well. That was a disaster.",
        "check": "Check. Try not to cry.",
        "mate": "Checkmate. Shocking, I know.",
        "capture": "Snack time!",
        "castle": "Hiding already?",
        "promo": "Abracadabra!",
    },
}


def spoken_square(square):
    name = chess.square_name(square)
    return f"{name[0].upper()} {name[1]}"


def describe_move(board, move):
    """Turn a move into natural speech: 'Knight to F 3', 'E pawn takes D 5',
    'Castle kingside', ', check', ', checkmate'. Returns (text, flags)."""
    flags = set()
    piece = board.piece_at(move.from_square)
    if piece is None:
        return f"{spoken_square(move.from_square)} to {spoken_square(move.to_square)}", flags

    if board.is_castling(move):
        flags.add("castle")
        kingside = chess.square_file(move.to_square) > chess.square_file(move.from_square)
        text = "Castle kingside" if kingside else "Castle queenside"
    else:
        capture = board.is_capture(move)
        if capture:
            flags.add("capture")
        dest = spoken_square(move.to_square)
        if piece.piece_type == chess.PAWN:
            file_letter = chess.square_name(move.from_square)[0].upper()
            text = f"{file_letter} pawn takes {dest}" if capture else f"Pawn to {dest}"
        else:
            name = PIECE_WORDS[piece.piece_type]
            # Two identical pieces can reach the square: say where this one starts.
            clash = any(
                m != move
                and m.to_square == move.to_square
                and board.piece_type_at(m.from_square) == piece.piece_type
                for m in board.legal_moves
            )
            origin = f" from {spoken_square(move.from_square)}" if clash else ""
            text = f"{name}{origin} takes {dest}" if capture else f"{name}{origin} to {dest}"
        if move.promotion:
            flags.add("promo")
            text += f", promoting to {PIECE_WORDS[move.promotion].lower()}"

    after = board.copy(stack=False)
    after.push(move)
    if after.is_checkmate():
        flags.add("mate")
        text += ", checkmate"
    elif after.is_check():
        flags.add("check")
        text += ", check"
    return text, flags


def compose_speech(persona, text, flags, alarm=False):
    """Wrap the move in the chosen persona's flavour."""
    p = PERSONAS.get(persona) or {}
    if not p:
        return text
    parts = [p["alarm"] if alarm else random.choice(p["open"]), text + "."]
    for flag in ("mate", "check", "promo", "castle", "capture"):
        if flag in flags:
            parts.append(p[flag])
            break
    return " ".join(parts)


def eval_to_fear(score, color):
    """0.0 = you are comfortably winning, 1.0 = you are in deep trouble."""
    try:
        s = score.pov(color)
        if s.is_mate():
            return 0.0 if (s.mate() or 0) > 0 else 1.0
        cp = s.score()
        if cp is None:
            return None
        return max(0.0, min(1.0, (150 - cp) / 500.0))
    except Exception:
        return None


# Neural voices (edge-tts). Pitch is in Hz and rate in % relative to the voice.
EDGE_VOICES = {
    "Plain": {"voice": "en-US-AriaNeural", "pitch": 0, "rate": 0},
    "Cryptkeeper": {"voice": "en-GB-RyanNeural", "pitch": -22, "rate": -18},
    "Dracula": {"voice": "en-US-GuyNeural", "pitch": -28, "rate": -14},
    "Witch": {"voice": "en-US-JennyNeural", "pitch": 18, "rate": 0},
}


# A few good free neural voices to choose from (Spooky tab). "Persona default"
# keeps the voice each persona normally uses.
EDGE_VOICE_CHOICES = [
    "Persona default",
    "en-US-AndrewNeural", "en-US-BrianNeural", "en-US-ChristopherNeural",
    "en-US-EricNeural", "en-US-GuyNeural",
    "en-US-AvaNeural", "en-US-EmmaNeural", "en-US-JennyNeural",
    "en-US-AriaNeural", "en-US-MichelleNeural",
    "en-GB-RyanNeural", "en-GB-SoniaNeural", "en-GB-LibbyNeural",
    "en-AU-WilliamNeural", "en-AU-NatashaNeural",
]
EDGE_VOICE_OVERRIDE = None  # set from the Spooky tab

async def _edge_save(text, voice, rate, pitch, path):
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await asyncio.wait_for(comm.save(path), timeout=10)


def synth_edge_mp3(text, rate, persona, path):
    """Render `text` to an mp3 with a Microsoft neural voice."""
    cfg = EDGE_VOICES.get(persona) or EDGE_VOICES["Plain"]
    pct = max(-60, min(60, cfg["rate"] + int(rate) * 6))
    voice = EDGE_VOICE_OVERRIDE or cfg["voice"]
    asyncio.run(_edge_save(text, voice, f"{pct:+d}%", f"{cfg['pitch']:+d}Hz", path))


def build_mp3_play_script(path, volume):
    """PowerShell that plays an mp3 with Windows' built-in media player."""
    p = path.replace("'", "''")
    vol = max(0, min(100, int(volume))) / 100.0
    return (
        "Add-Type -AssemblyName presentationCore; "
        "$p = New-Object System.Windows.Media.MediaPlayer; "
        f"$p.Volume = {vol}; $p.Open([uri]'{p}'); $p.Play(); "
        "$t = 0; "
        "while (-not $p.NaturalDuration.HasTimeSpan -and $t -lt 100) "
        "{ Start-Sleep -Milliseconds 50; $t++ }; "
        "if ($p.NaturalDuration.HasTimeSpan) { Start-Sleep -Milliseconds "
        "([int]$p.NaturalDuration.TimeSpan.TotalMilliseconds + 250) }; "
        "$p.Close();"
    )


# --- engine telemetry --------------------------------------------------------

def format_count(n):
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.2f}G"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(int(n))


def telemetry_text(info):
    """One line of engine stats from a python-chess info dict."""
    if not info:
        return "Engine: no data"
    bits = []
    if info.get("depth") is not None:
        d = f"Depth {info['depth']}"
        if info.get("seldepth"):
            d += f"/{info['seldepth']}"
        bits.append(d)
    if info.get("nps") is not None:
        bits.append(f"NPS {format_count(info['nps'])}")
    if info.get("nodes") is not None:
        bits.append(f"Nodes {format_count(info['nodes'])}")
    if info.get("hashfull") is not None:
        bits.append(f"Hash {info['hashfull'] / 10:.0f}%")
    return "  |  ".join(bits) or "Engine: no data"


# --- move quality ------------------------------------------------------------

PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
QUALITY_ORDER = ["Brilliant", "Best", "Excellent", "Good", "Inaccuracy", "Mistake", "Blunder"]
CP_CAP = 1000


def score_cp(score, color):
    """Centipawns from `color`'s point of view, with mates capped."""
    value = score.pov(color).score(mate_score=CP_CAP)
    return max(-CP_CAP, min(CP_CAP, value))


def find_played_move(before, after):
    """The legal move that turns `before` into `after` (or None)."""
    target = after.board_fen()
    for m in before.legal_moves:
        b = before.copy(stack=False)
        b.push(m)
        if b.board_fen() == target:
            return m
    return None


def advance_tracker(track, board):
    """Push the one or two moves that lead from `track` to the page's `board`.
    True if the tracker now matches the page."""
    target = board.board_fen()
    if track.board_fen() == target:
        return True
    for m in list(track.legal_moves):
        track.push(m)
        if track.board_fen() == target:
            return True
        for m2 in list(track.legal_moves):
            track.push(m2)
            if track.board_fen() == target:
                return True
            track.pop()
        track.pop()
    return False


def san_list(board):
    # The moves played so far on a tracker board, as SAN strings.
    b = chess.Board()
    out = []
    for m in board.move_stack:
        try:
            out.append(b.san(m))
            b.push(m)
        except Exception:
            break
    return out


class _Pt:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def tracker_to_pgn(track, user_color):
    game = chess.pgn.Game.from_board(track)
    game.headers["Event"] = "chess.com bot game"
    game.headers["Site"] = "chess.com"
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["White"] = "Me" if user_color == chess.WHITE else "Bot"
    game.headers["Black"] = "Bot" if user_color == chess.WHITE else "Me"
    if track.is_game_over():
        game.headers["Result"] = track.result()
    return str(game)


OPENING_LINES = [
    ("e4", "King's Pawn Opening"),
    ("d4", "Queen's Pawn Opening"),
    ("c4", "English Opening"),
    ("Nf3", "Reti Opening"),
    ("f4", "Bird's Opening"),
    ("b4", "Sokolsky Opening"),
    ("e4 e5", "Open Game"),
    ("e4 e5 Nf3 Nc6", "King's Knight Opening"),
    ("e4 e5 Nf3 Nc6 Bb5", "Ruy Lopez"),
    ("e4 e5 Nf3 Nc6 Bc4", "Italian Game"),
    ("e4 e5 Nf3 Nc6 Bc4 Bc5", "Giuoco Piano"),
    ("e4 e5 Nf3 Nc6 Bc4 Nf6", "Two Knights Defense"),
    ("e4 e5 Nf3 Nc6 d4", "Scotch Game"),
    ("e4 e5 Nf3 Nf6", "Petrov's Defense"),
    ("e4 e5 Nf3 d6", "Philidor Defense"),
    ("e4 e5 f4", "King's Gambit"),
    ("e4 e5 Nc3", "Vienna Game"),
    ("e4 c5", "Sicilian Defense"),
    ("e4 c5 c3", "Sicilian: Alapin Variation"),
    ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6", "Sicilian: Najdorf Variation"),
    ("e4 e6", "French Defense"),
    ("e4 c6", "Caro-Kann Defense"),
    ("e4 d5", "Scandinavian Defense"),
    ("e4 d6 d4 Nf6 Nc3 g6", "Pirc Defense"),
    ("e4 g6", "Modern Defense"),
    ("e4 Nf6", "Alekhine's Defense"),
    ("d4 d5", "Closed Game"),
    ("d4 d5 c4", "Queen's Gambit"),
    ("d4 d5 c4 e6", "Queen's Gambit Declined"),
    ("d4 d5 c4 dxc4", "Queen's Gambit Accepted"),
    ("d4 d5 c4 c6", "Slav Defense"),
    ("d4 d5 Bf4", "London System"),
    ("d4 Nf6", "Indian Defense"),
    ("d4 Nf6 c4 g6 Nc3 Bg7", "King's Indian Defense"),
    ("d4 Nf6 c4 e6 Nc3 Bb4", "Nimzo-Indian Defense"),
    ("d4 Nf6 c4 e6 Nf3 b6", "Queen's Indian Defense"),
    ("d4 Nf6 c4 c5 d5", "Benoni Defense"),
    ("d4 f5", "Dutch Defense"),
    ("c4 e5", "English: Reversed Sicilian"),
]


def build_opening_book():
    """board-fen -> opening name, built from OPENING_LINES (bad lines are skipped)."""
    book = {}
    for line, name in OPENING_LINES:
        b = chess.Board()
        try:
            for san in line.split():
                b.push_san(san)
        except Exception:
            continue
        book[b.board_fen()] = name
    return book


def looks_like_sacrifice(before, move):
    """Rough test: a piece moved onto an attacked square for less than it is worth."""
    piece = before.piece_at(move.from_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    after = before.copy(stack=False)
    after.push(move)
    if not after.is_attacked_by(not piece.color, move.to_square):
        return False
    captured = before.piece_at(move.to_square)
    gain = PIECE_VALUES.get(captured.piece_type, 0) if captured else 0
    return PIECE_VALUES[piece.piece_type] - gain >= 2


def classify_move(loss, sacrifice, best_cp, after_cp):
    """Label a move by how many centipawns it gave up versus the engine's best."""
    if loss <= 10 and sacrifice and best_cp < 400 and after_cp >= -50:
        return "Brilliant"
    if loss <= 5:
        return "Best"
    if loss <= 25:
        return "Excellent"
    if loss <= 60:
        return "Good"
    if best_cp >= 600 and after_cp >= 600:
        return "Good"  # already completely winning; don't nitpick
    if loss <= 120:
        return "Inaccuracy"
    if loss <= 250:
        return "Mistake"
    return "Blunder"


def quality_tag(text):
    for word, tag in (("Brilliant", "brill"), ("Blunder", "error"),
                      ("Mistake", "warn"), ("Inaccuracy", "warn")):
        if word in text:
            return tag
    return "good"


LABEL_PHRASES = {
    "Brilliant": "a brilliant move",
    "Best": "the best move in the position",
    "Excellent": "an excellent move",
    "Good": "a good move",
    "Inaccuracy": "a small inaccuracy",
    "Mistake": "a mistake",
    "Blunder": "a blunder",
}
BAD_LABELS = ("Inaccuracy", "Mistake", "Blunder")
BIG_LABELS = ("Brilliant", "Mistake", "Blunder")
QUALITY_SPEECH = {
    "Brilliant": "Brilliant move from them.",
    "Best": "They played the best move.",
    "Excellent": "Excellent move from them.",
    "Good": "Their last move was good.",
    "Inaccuracy": "Their last move was a small inaccuracy.",
    "Mistake": "They made a mistake.",
    "Blunder": "They blundered!",
}


def quality_speech(label):
    """The short spoken verdict on the opponent's last move."""
    return QUALITY_SPEECH.get(label, f"Their last move was {label.lower()}.")


def _val(piece_type):
    """Piece value for attacker comparisons (a king counts as 'very big')."""
    return PIECE_VALUES.get(piece_type, 0) or 99


def _pname(piece):
    return PIECE_WORDS[piece.piece_type].lower()


def hanging_pieces(board, color):
    """Pieces of `color` that the other side could win for free or at a profit.
    A rough check (not full exchange maths). Biggest piece first."""
    found = []
    for sq, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not color, sq)
        if not attackers:
            continue
        cheapest = min(_val(board.piece_type_at(a)) for a in attackers)
        if not board.attackers(color, sq) or PIECE_VALUES[piece.piece_type] > cheapest:
            found.append((sq, piece))
    found.sort(key=lambda t: -PIECE_VALUES[t[1].piece_type])
    return found


def _standing(cp):
    """Plain-English verdict on a score from the opponent's point of view."""
    if cp >= 300:
        return "They are clearly winning now."
    if cp >= 120:
        return "That leaves them a bit better."
    if cp > -120:
        return "The position is roughly level."
    if cp > -300:
        return "They are still a bit worse."
    return "They are still in deep trouble."


def explain_move(before, move, after, label, loss, best_cp, after_cp, best_move, reply):
    """A few spoken-style sentences on WHY the opponent's move was good or bad.

    before/after: the positions around their move.  loss: centipawns given up.
    best_cp/after_cp: scores from their point of view.  best_move: the engine's
    choice instead.  reply: the engine's best answer for you afterwards."""
    mover = before.turn
    said = describe_move(before, move)[0]
    captured = before.piece_at(move.to_square)
    if before.is_en_passant(move):
        captured = chess.Piece(chess.PAWN, not mover)
    best_said = None
    if best_move is not None and best_move != move and best_move in before.legal_moves:
        best_said = describe_move(before, best_move)[0]
    reasons = []

    if label not in BAD_LABELS:
        if after.is_checkmate():
            reasons.append("It delivers checkmate.")
        else:
            if label == "Brilliant":
                reasons.append(
                    "It gives up material on purpose, but the engine sees it paying off."
                )
            elif best_move == move:
                reasons.append("It is the engine's top choice.")
            else:
                reasons.append("It is very close to the engine's top choice.")
            if captured is not None:
                reasons.append(f"It takes your {_pname(captured)}.")
            if after.is_check():
                reasons.append("It gives check.")
            if before.is_castling(move):
                reasons.append("It castles, tucking the king away and connecting the rooks.")
            elif move.promotion:
                reasons.append("It promotes a pawn.")
            else:
                moved = after.piece_at(move.to_square)
                threats = []
                for t in after.attacks(move.to_square):
                    p = after.piece_at(t)
                    if (
                        p is not None
                        and p.color != mover
                        and p.piece_type != chess.KING
                        and moved is not None
                        and (
                            not after.attackers(p.color, t)
                            or PIECE_VALUES[p.piece_type] > PIECE_VALUES[moved.piece_type]
                        )
                    ):
                        threats.append((t, p))
                threats.sort(key=lambda x: -PIECE_VALUES[x[1].piece_type])
                piece = before.piece_at(move.from_square)
                back_rank = 0 if mover == chess.WHITE else 7
                if threats:
                    t, p = threats[0]
                    reasons.append(f"It now attacks your {_pname(p)} on {spoken_square(t)}.")
                elif (
                    piece is not None
                    and piece.piece_type in (chess.KNIGHT, chess.BISHOP)
                    and chess.square_rank(move.from_square) == back_rank
                    and before.fullmove_number <= 12
                ):
                    reasons.append("It develops a piece.")
                elif (
                    piece is not None
                    and piece.piece_type == chess.PAWN
                    and chess.square_file(move.to_square) in (3, 4)
                    and chess.square_rank(move.to_square) in (3, 4)
                ):
                    reasons.append("It stakes a claim in the centre.")
        text = f"Their move, {said}, was {LABEL_PHRASES.get(label, label.lower())}. "
        text += " ".join(reasons[:3])
        text += " " + _standing(after_cp)
        return text.strip()

    # ----- a bad move: find the concrete reason -----
    if after_cp <= -(CP_CAP - 50):
        reasons.append("It walks into a forced checkmate.")
    hang_before = {sq for sq, _ in hanging_pieces(before, mover)}
    hang_after = hanging_pieces(after, mover)
    for sq, p in hang_after:
        if len(reasons) >= 2:
            break
        if sq == move.to_square:
            reasons.append(
                f"It leaves the {_pname(p)} on {spoken_square(sq)} where you can take it."
            )
        elif sq not in hang_before:
            reasons.append(
                f"It leaves their {_pname(p)} on {spoken_square(sq)} unprotected."
            )
        else:
            reasons.append(
                f"It ignores the threat to their {_pname(p)} on {spoken_square(sq)}."
            )
    if reply is not None and reply in after.legal_moves and len(reasons) < 2:
        rc = after.piece_at(reply.to_square)
        if after.is_en_passant(reply):
            rc = chess.Piece(chess.PAWN, mover)
        a2 = after.copy(stack=False)
        a2.push(reply)
        rdesc = describe_move(after, reply)[0]
        if a2.is_checkmate():
            reasons.append(f"You can now play {rdesc}, which is checkmate.")
        elif rc is not None:
            reasons.append(f"You can now play {rdesc}, winning their {_pname(rc)}.")
    if best_move is not None and best_move != move and best_move in before.legal_moves:
        b2 = before.copy(stack=False)
        b2.push(best_move)
        if b2.is_checkmate():
            reasons.append(f"They missed checkmate with {best_said}.")
        elif before.is_capture(best_move) and loss >= 100 and len(reasons) < 2:
            bc = before.piece_at(best_move.to_square)
            if before.is_en_passant(best_move):
                bc = chess.Piece(chess.PAWN, not mover)
            if bc is not None:
                reasons.append(
                    f"They missed a chance to take your {_pname(bc)} with {best_said}."
                )
    if not reasons:
        reasons.append("The engine found a clearly stronger plan.")

    text = f"Their move, {said}, was {LABEL_PHRASES.get(label, label.lower())}."
    if loss >= 30:
        text += f" It gave up about {loss / 100:.1f} pawns of advantage."
    text += " " + " ".join(reasons[:3])
    if best_said:
        text += f" The best move was {best_said}."
    return text.strip()


def build_speech_script(text, rate, volume, persona):
    """PowerShell that speaks `text` with Windows' built-in voices."""
    rate = max(-10, min(10, int(rate)))
    volume = max(0, min(100, int(volume)))
    p = PERSONAS.get(persona) or {}
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; $s.Volume = {volume}; "
    )
    if p.get("gender"):
        script += (
            "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::"
            + p["gender"]
            + ") } catch { }; "
        )
    if p.get("pitch"):
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis"'
            ' xml:lang="en-US">'
            f'<prosody pitch="{p["pitch"]}" rate="{p["rate"]}">'
            f"{html.escape(text, quote=False)}</prosody></speak>"
        )
        script += "$s.SpeakSsml('" + ssml.replace("'", "''") + "');"
    else:
        safe = text.replace("'", "''").replace('"', "")
        script += f"$s.Speak('{safe}');"
    return script


class SpeechQueue:
    """Speaks one thing at a time, in order, on its own thread.

    - A new move interrupts (cuts off) an older one, so fast play never
      leaves you hearing stale advice or overlapping voices.
    - Anything queued before an interruption is dropped.
    - Asking for the very same line that is already playing is ignored."""

    def __init__(self, on_error=None):
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._proc = None
        self._cancelled = False
        self._current = None
        self._seq = 0
        self._min_seq = 0
        self._on_error = on_error
        self._edge_warned = False
        threading.Thread(target=self._worker, daemon=True).start()

    def say(self, text, rate=0, volume=100, persona="Plain", interrupt=True, neural=False):
        with self._lock:
            self._seq += 1
            seq = self._seq
            if interrupt:
                self._min_seq = seq  # everything older is now stale
        if interrupt:
            self._drain()
            self.cancel_current()
        self._q.put((seq, text, rate, volume, persona, neural))

    def is_speaking(self, text=None):
        with self._lock:
            return self._current is not None and (text is None or self._current == text)

    def cancel_current(self):
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                self._cancelled = True
                try:
                    proc.terminate()
                except Exception:
                    pass

    def silence(self):
        """Stop talking now and forget everything still waiting to be said."""
        with self._lock:
            self._min_seq = self._seq + 1
        self._drain()
        self.cancel_current()

    def _drain(self):
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def _worker(self):
        while True:
            seq, text, rate, volume, persona, neural = self._q.get()
            with self._lock:
                if seq < self._min_seq:
                    continue  # superseded while waiting
            try:
                self._speak_one(seq, text, rate, volume, persona, neural)
            except Exception as e:
                self._report(str(e))

    def _speak_one(self, seq, text, rate, volume, persona, neural=False):
        script = None
        mp3 = None
        if neural and edge_tts is not None:
            try:
                mp3 = os.path.join(
                    tempfile.gettempdir(), f"haunted_sf_voice_{os.getpid()}.mp3"
                )
                synth_edge_mp3(text, rate, persona, mp3)
                script = build_mp3_play_script(mp3, volume)
            except Exception as e:
                mp3 = None
                if not self._edge_warned:
                    self._edge_warned = True
                    self._report(
                        f"Neural voice failed ({type(e).__name__}); "
                        "using the Windows voice instead."
                    )
        with self._lock:
            if seq < self._min_seq:
                return  # superseded while the neural voice was being generated
        if script is None:
            script = build_speech_script(text, rate, volume, persona)
        with self._lock:
            self._cancelled = False
            self._current = text
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            proc = self._proc
        try:
            _, err = proc.communicate()
        finally:
            with self._lock:
                cancelled = self._cancelled
                self._proc = None
                self._current = None
                self._cancelled = False
        if mp3:
            try:
                os.remove(mp3)
            except OSError:
                pass
        if proc.returncode != 0 and not cancelled:
            self._report((err or "unknown PowerShell error").strip()[:200])

    def _report(self, message):
        if self._on_error:
            try:
                self._on_error(message)
            except Exception:
                pass


class Ambience:
    """A quiet looping wind-and-creaks track, generated on the fly (no files to
    download). Windows only (uses winsound)."""

    RATE = 16000
    SECONDS = 8

    def __init__(self):
        self.path = os.path.join(tempfile.gettempdir(), "haunted_stockfish_wind.wav")
        self.playing = False

    @property
    def available(self):
        return winsound is not None

    def build_wav(self, path=None):
        path = path or self.path
        rate, n = self.RATE, self.RATE * self.SECONDS
        xfade = rate // 2
        rnd = random.Random(99)
        two_pi = 2 * math.pi
        lp1 = lp2 = 0.0
        ext = []
        for i in range(n + xfade):
            lp1 += 0.045 * (rnd.uniform(-1, 1) - lp1)
            lp2 += 0.020 * (lp1 - lp2)
            t = i / rate
            gust = 0.45 + 0.30 * math.sin(two_pi * t / 8.0 + 0.4) + 0.20 * math.sin(two_pi * t / 2.0)
            ext.append(lp2 * gust)
        # Cross-fade the tail into the head so the loop has no click.
        out = []
        for i in range(n):
            if i < xfade:
                w = i / xfade
                out.append(ext[i] * w + ext[n + i] * (1 - w))
            else:
                out.append(ext[i])
        # A couple of old-wood creaks.
        for start in (2.4, 5.9):
            length = int(0.7 * rate)
            s0 = int(start * rate)
            phase = 0.0
            for k in range(length):
                u = k / length
                freq = 150 - 55 * u + 12 * math.sin(two_pi * 9 * u)
                phase += two_pi * freq / rate
                env = math.sin(math.pi * u) ** 2
                out[s0 + k] += 0.085 * env * (math.sin(phase) + 0.4 * math.sin(2.02 * phase))
        peak = max(abs(v) for v in out) or 1.0
        scale = 0.30 * 32767 / peak
        samples = array.array("h", [int(v * scale) for v in out])
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(samples.tobytes())
        return path

    def start(self):
        if winsound is None:
            return False
        try:
            if not os.path.exists(self.path) or os.path.getsize(self.path) < 1000:
                self.build_wav()
            winsound.PlaySound(
                self.path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP,
            )
            self.playing = True
            return True
        except Exception:
            return False

    def stop(self):
        if winsound is None:
            return
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        self.playing = False


# ---------------------------------------------------------------------------
# The animated haunted scene
# ---------------------------------------------------------------------------

def _hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class HauntedScene:
    """A haunted graveyard on a Tk canvas: bats and ghosts drift past, skeletons
    dance, pumpkins and windows flicker, lightning strikes, a spider dangles,
    fog rolls, wisps float. Move the mouse over it for sparks; click it for a
    burst of bats. The `fear` value (0-1, from the engine's evaluation) makes
    it stormier and redder."""

    FPS = 30
    # Parallax layers: how far (x, y in pixels) each layer slides against the mouse.
    AMP = {"far": (2.5, 1.0), "far2": (5.0, 1.5), "mid": (9.0, 2.5),
           "near": (13.0, 3.5), "fg": (22.0, 6.0)}
    BAT_FILL = "#0b0613"
    BAT_EDGE = "#7a4cc0"
    BONE = "#e8e2cf"

    def __init__(self, root, canvas, width, height):
        self.root = root
        self.c = canvas
        self.W = width
        self.H = height
        self.ground = height - 26

        self.enabled = True
        self.level = 6          # 1-10: how crowded / stormy
        self.sparks = True
        self.fear = 0.0         # 0-1

        self._job = None
        self._t = 0.0
        self._last = time.perf_counter()
        self._err_logged = False
        self._fear_bucket = None
        self._flash_windows = []
        self._next_flash = time.perf_counter() + random.uniform(4, 9)
        self._title_flicker = []
        self._next_title_flicker = time.perf_counter() + random.uniform(3, 7)
        self._last_spark = 0.0
        self._last_burst = 0.0

        self.particles = []
        self.temp_bats = []
        self.bats = []
        self.ghosts = []
        self.wisps = []
        self.windows = []
        self.pumpkins = []
        self.skeletons = []
        self.fog = []
        self.rays = []
        self.parallax = True
        self.off = {name: (0.0, 0.0) for name in self.AMP}
        self._px = self._py = 0.0   # eased parallax position (-1..1)
        self._mx = self._my = 0.0   # mouse target (-1..1)
        self._hover = False
        self.thinking = False       # the engine is calculating
        self.rain = []
        self._rain_on = False
        self.toasts = []
        self._bolt = None

        self._build()
        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Leave>", self._on_leave)
        canvas.bind("<Button-1>", self._on_click)
        self.apply_level()

    # ------------------------------------------------------------------
    # Scene construction
    # ------------------------------------------------------------------

    def _build(self):
        c, W, H, G = self.c, self.W, self.H, self.ground
        rnd = random.Random(13)

        # Night sky: a stepped gradient.
        bands = 18
        for i in range(bands):
            col = lerp_color("#06030c", "#46215f", (i / (bands - 1)) ** 1.35)
            c.create_rectangle(0, i * H / bands, W, (i + 1) * H / bands + 1,
                               fill=col, outline="")
        # Red storm-glow (shown when the position gets scary) and lightning.
        self.fear_glow = c.create_rectangle(
            0, H * 0.30, W, H, fill="#c0102c", outline="", stipple="gray12", state="hidden")
        self.flash = c.create_rectangle(
            0, 0, W, H, fill="#d8c8ff", outline="", stipple="gray50", state="hidden")

        _far0 = self._begin()
        # Stars.
        self.stars = []
        for _ in range(36):
            x, y = rnd.uniform(4, W - 4), rnd.uniform(3, H * 0.5)
            r = rnd.choice((0.7, 1.0, 1.4))
            item = c.create_oval(x - r, y - r, x + r, y + r, fill="#8d7bb0", outline="")
            self.stars.append((item, rnd.uniform(0, 6.28), rnd.uniform(1.2, 3.0)))

        # Moon with a pulsing glow.
        mx, my, mr = W * 0.83, 52, 23
        self.moon = (mx, my, mr)
        self.moon_glow = []
        for extra, stip in ((20, "gray12"), (11, "gray25")):
            gid = c.create_oval(mx - mr - extra, my - mr - extra, mx + mr + extra,
                                my + mr + extra, fill="#ffe9a8", outline="", stipple=stip)
            self.moon_glow.append((gid, extra))
        c.create_oval(mx - mr, my - mr, mx + mr, my + mr, fill="#fff3cf", outline="#fff9e6")
        for dx, dy, r in ((-8, -6, 5), (7, 4, 6), (-2, 11, 3.5), (9, -10, 2.8)):
            c.create_oval(mx + dx - r, my + dy - r, mx + dx + r, my + dy + r,
                          fill="#e6d49c", outline="")

        self._end("L_far", _far0)   # stars + moon: the most distant layer

        # Distant mountains (lighter and bluer = further away).
        _far2 = self._begin()
        pts = [-60, H + 30]
        for x in range(-60, int(W) + 80, 18):
            pts += [x, G - 54 - 22 * abs(math.sin(x / 47.0 + 0.3)) - 9 * math.sin(x / 17.0)]
        pts += [W + 60, H + 30]
        c.create_polygon(pts, fill="#2a1745", outline="#3a2160")
        self._end("L_far2", _far2)

        # Moonlight shafts, swaying slowly (animated every frame).
        for dxr, wr, ph in ((-70, 20, 0.0), (-135, 15, 2.1), (-195, 11, 4.2)):
            item = c.create_polygon(0, 0, 1, 1, 2, 2, fill="#ffe9a8", outline="",
                                    stipple="gray12")
            self.rays.append({"id": item, "dx": dxr, "w": wr, "ph": ph})

        # Pale mist between the mountains and the hills.
        for _ in range(3):
            self._make_fog(rnd, G - 46 + rnd.uniform(-10, 10), color="#5a3d86",
                           stipple="gray25", slow=0.5)

        # Rolling hills.
        _mid0 = self._begin()
        pts = [-60, H + 30]
        for x in range(-60, int(W) + 84, 24):
            pts += [x, G - 30 - 13 * math.sin(x / 61.0 + 0.7) - 7 * math.sin(x / 23.0)]
        pts += [W + 60, H + 30]
        c.create_polygon(pts, fill="#180c27", outline="")

        # Haunted houses and bare trees.
        self._house(W * 0.13, G - 4, 0.55)
        self._house(W * 0.70, G + 2, 0.92)
        self._tree(W * 0.43, G, 0.9)
        self._tree(W * 0.97, G + 2, 1.0)
        self._tree(W * 0.02, G + 2, 0.85)
        self._end("L_mid", _mid0)

        # Creatures of the night (drawn behind the foreground).
        for i in range(3):
            self._make_ghost(i, rnd.uniform(0, W), rnd.uniform(40, 100), rnd.uniform(0.9, 1.25))
        for i in range(7):
            self.bats.append(self._make_bat(f"bat{i}", rnd.uniform(0, W),
                                            rnd.uniform(14, 100), rnd.uniform(0.8, 1.35)))
            b = self.bats[-1]
            b.update(vx=rnd.choice((-1, 1)) * rnd.uniform(26, 62), base=b["y"],
                     amp=rnd.uniform(6, 16), freq=rnd.uniform(1.4, 3.2),
                     phase=rnd.uniform(0, 6.28), flap=0.0)

        _near0 = self._begin()
        # Graveyard ground, grass and tombstones.
        c.create_rectangle(-40, G, W + 40, H + 40, fill="#090510", outline="")
        for _ in range(80):
            x = rnd.uniform(-30, W + 30)
            c.create_line(x, G + 1, x + rnd.uniform(-2, 2), G - rnd.uniform(3, 7),
                          fill="#140b20")
        self._tomb(W * 0.07, G + 12, 14, 20)
        self._tomb(W * 0.37, G + 14, 13, 16)
        self._tomb(W * 0.59, G + 11, 15, 22)
        self._tomb(W * 0.80, G + 13, 14, 17)
        self._cross(W * 0.50, G + 12, 1.0)
        self._cross(W * 0.93, G + 14, 0.9)

        self._end("L_near", _near0)

        # Back fog (drifts behind the skeletons).
        for _ in range(4):
            self._make_fog(rnd, G + rnd.uniform(-4, 6))

        # Dancing skeletons.
        for x, ph in ((W * 0.20, 0.0), (W * 0.31, 1.7)):
            self.skeletons.append(self._make_skeleton(x, 0.86, ph))

        # Jack-o'-lanterns (they sit on the near-ground layer).
        _pk0 = self._begin()
        for cx, r in ((W * 0.455, 12), (W * 0.535, 8), (W * 0.885, 15)):
            self.pumpkins.append(self._make_pumpkin(cx, G + 12, r))
        self._end("L_near", _pk0)

        # Front fog.
        for _ in range(3):
            self._make_fog(rnd, G + rnd.uniform(6, 16))

        # Will-o'-the-wisps.
        for i in range(5):
            cx, cy = rnd.uniform(30, W - 30), rnd.uniform(G - 30, G - 4)
            glow = c.create_oval(0, 0, 1, 1, fill="#7dff4f", outline="", stipple="gray25")
            core = c.create_oval(0, 0, 1, 1, fill="#d6ffbf", outline="")
            self.wisps.append({"glow": glow, "core": core, "cx": cx, "cy": cy,
                               "ax": rnd.uniform(14, 40), "ay": rnd.uniform(5, 12),
                               "ph": rnd.uniform(0, 6.28), "sp": rnd.uniform(0.5, 1.1)})

        # A spider on a thread.
        self.spider_x = W * 0.29
        self.spider_thread = c.create_line(self.spider_x, 0, self.spider_x, 30,
                                           fill="#8a7aa8")
        self.spider = [c.create_oval(0, 0, 1, 1, fill="#120a1c", outline="#6a4a9a")]
        for side in (-1, 1):
            for k in range(4):
                self.spider.append(c.create_line(0, 0, 1, 1, fill="#6a4a9a"))
        self.spider_y = 30

        # Foreground silhouettes: the nearest layer, so it slides the most.
        _fg0 = self._begin()
        pts = [-40, H + 10]
        x = -40.0
        while x < W + 40:
            blade = rnd.uniform(5, 16)
            pts += [x, H - 2, x + 3.2, H - blade, x + 6.4, H - 2]
            x += 6.4
        pts += [W + 40, H + 10]
        c.create_polygon(pts, fill="#050309", outline="")
        for sx in (W * 0.06, W * 0.52, W * 0.91):   # dead cattails
            lean = rnd.uniform(-5, 5)
            top = H - rnd.uniform(34, 46)
            c.create_line(sx, H, sx + lean, top, fill="#050309", width=2)
            c.create_oval(sx + lean - 2.4, top - 7, sx + lean + 2.4, top + 6,
                          fill="#050309", outline="")
        for (x1, y1), (x2, y2) in (((-20, 10), (36, 22)), ((36, 22), (74, 50)),
                                   ((74, 50), (92, 84)), ((36, 22), (66, 16)),
                                   ((66, 16), (96, 24)), ((50, 32), (48, 60))):
            c.create_line(x1, y1, x2, y2, fill="#050309",
                          width=4 if x1 < 40 else 2.5, capstyle="round")
        self._end("L_fg", _fg0)

        # Title.
        tx, ty = W / 2, 30
        font = ("Chiller", 36)
        self.title_shadow = c.create_text(tx + 2, ty + 2, text="Haunted Stockfish",
                                          font=font, fill="#2b0f3d")
        self.title = c.create_text(tx, ty, text="Haunted Stockfish", font=font,
                                   fill="#ff7518")
        self.skull_l = c.create_text(tx - 138, ty, text="\u2620", font=("Segoe UI Symbol", 20),
                                     fill="#ff7518")
        self.skull_r = c.create_text(tx + 138, ty, text="\u2620", font=("Segoe UI Symbol", 20),
                                     fill="#ff7518")

        self._build_overlay()

    def _begin(self):
        """Snapshot of the canvas items that exist right now."""
        return set(self.c.find_all())

    def _end(self, layer, start):
        """Tag every item created since `start` as belonging to `layer`."""
        for item in self.c.find_all():
            if item not in start:
                self.c.addtag_withtag(layer, item)

    def _build_overlay(self):
        """Things that stay fixed over the scene: cobweb and edge vignette."""
        c, W, H = self.c, self.W, self.H
        rnd = random.Random(21)
        for _ in range(40):
            x, y = rnd.uniform(0, W), rnd.uniform(0, H)
            item = c.create_line(x, y, x - 2, y + 8, fill="#7a82c8", state="hidden")
            self.rain.append({"id": item, "x": x, "y": y, "v": rnd.uniform(220, 340)})
        self._cobweb(W, 0, 54)
        for width, stip in ((34, "gray12"), (20, "gray25"), (9, "gray50")):
            top = width * 0.4
            for box in ((0, 0, width, H), (W - width, 0, W, H),
                        (0, 0, W, top), (0, H - width, W, H)):
                c.create_rectangle(*box, fill="#05020a", outline="", stipple=stip)

    def _cobweb(self, cx, cy, R):
        c = self.c
        angles = [math.pi / 2 + i * (math.pi / 2) / 5 for i in range(6)]
        col = "#8a7aa8"
        for a in angles:
            c.create_line(cx, cy, cx + R * math.cos(a), cy + R * math.sin(a), fill=col)
        for frac in (0.3, 0.55, 0.8, 1.0):
            r = R * frac
            for a1, a2 in zip(angles, angles[1:]):
                p1 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
                p2 = (cx + r * math.cos(a2), cy + r * math.sin(a2))
                am = (a1 + a2) / 2
                sag = (cx + r * 0.9 * math.cos(am), cy + r * 0.9 * math.sin(am))
                c.create_line(p1[0], p1[1], sag[0], sag[1], p2[0], p2[1],
                              smooth=True, fill=col)

    def _house(self, cx, base, s, body="#0b0612", edge="#2a1743"):
        c = self.c

        def P(*pts):
            return [(cx + v * s) if i % 2 == 0 else (base + v * s) for i, v in enumerate(pts)]

        c.create_rectangle(*P(-40, -46, 40, 0), fill=body, outline=edge)
        c.create_polygon(*P(-48, -46, 0, -88, 48, -46), fill=body, outline=edge)
        c.create_rectangle(*P(-66, -72, -42, 0), fill=body, outline=edge)
        c.create_polygon(*P(-71, -72, -54, -106, -37, -72), fill=body, outline=edge)
        c.create_rectangle(*P(22, -88, 33, -60), fill=body, outline=edge)
        c.create_rectangle(*P(40, -30, 62, 0), fill=body, outline=edge)
        c.create_polygon(*P(38, -30, 51, -46, 64, -30), fill=body, outline=edge)
        c.create_rectangle(*P(-6, -18, 6, 0), fill="#1a0d28", outline="")
        for wx1, wy1, wx2, wy2 in ((-28, -38, -16, -22), (14, -38, 26, -22),
                                   (-59, -62, -49, -48), (-4, -74, 4, -60),
                                   (46, -22, 56, -10)):
            c.create_rectangle(*P(wx1 - 4, wy1 - 4, wx2 + 4, wy2 + 4), fill="#ff9a1a",
                               outline="", stipple="gray12")   # soft halo
            item = c.create_rectangle(*P(wx1, wy1, wx2, wy2), fill="#ffcf5a", outline="")
            self.windows.append([item, random.uniform(0, 6.28)])

    def _tree(self, x, base, s):
        segs = [(0, 0, 0, -44), (0, -30, -16, -52), (0, -34, 15, -56), (-9, -44, -22, -58),
                (8, -46, 20, -70), (0, -44, 3, -74), (-14, -50, -18, -72)]
        for x1, y1, x2, y2 in segs:
            width = max(1.0, (3.2 if y1 == 0 else 1.8) * s)
            self.c.create_line(x + x1 * s, base + y1 * s, x + x2 * s, base + y2 * s,
                               fill="#0a0510", width=width, capstyle="round")

    def _tomb(self, x, base, w, h):
        c = self.c
        fill, edge = "#2d2243", "#41325f"
        c.create_rectangle(x, base - h, x + w, base, fill=fill, outline=edge)
        c.create_oval(x, base - h - w / 2, x + w, base - h + w / 2, fill=fill, outline=edge)
        c.create_rectangle(x + 1, base - h, x + w - 1, base - h + 4, fill=fill, outline="")
        c.create_text(x + w / 2, base - h + 5, text="RIP", fill="#7b679d",
                      font=("Segoe UI", 5, "bold"))

    def _cross(self, x, base, s):
        c = self.c
        c.create_line(x, base, x, base - 24 * s, fill="#41325f", width=3 * s)
        c.create_line(x - 7 * s, base - 17 * s, x + 7 * s, base - 17 * s, fill="#41325f",
                      width=3 * s)

    def _make_fog(self, rnd, y, color="#4a336f", stipple="gray50", slow=1.0):
        fw = rnd.uniform(110, 190)
        fx = rnd.uniform(-60, self.W)
        item = self.c.create_oval(fx, y, fx + fw, y + rnd.uniform(14, 22),
                                  fill=color, outline="", stipple=stipple)
        self.fog.append({"id": item, "w": fw,
                         "speed": rnd.choice((-1, 1)) * rnd.uniform(5, 12) * slow})

    def _make_bat(self, tag, x, y, size):
        c = self.c

        def pts(coords):
            return [(x + v * size) if i % 2 == 0 else (y + v * size) for i, v in enumerate(coords)]

        up_l = (-2, 0, -9, -8, -16, -6, -12, -2, -9, -3, -6, 0)
        dn_l = (-2, 0, -9, 6, -16, 5, -12, 1, -9, 2, -6, 0)

        def mirror(coords):
            return tuple(-v if i % 2 == 0 else v for i, v in enumerate(coords))

        kw = dict(fill=self.BAT_FILL, outline=self.BAT_EDGE)
        for shape, sub in ((up_l, "u"), (mirror(up_l), "u"), (dn_l, "d"), (mirror(dn_l), "d")):
            c.create_polygon(pts(shape), tags=(tag, f"{tag}_{sub}"), **kw)
        c.create_oval(pts((-3, -3, 3, 4)), tags=(tag,), **kw)
        c.create_polygon(pts((-3, -3, -2.5, -8, 0, -4.5, 2.5, -8, 3, -3)), tags=(tag,), **kw)
        for ex in (-1.6, 1.6):
            c.create_oval(pts((ex - 0.7, -2, ex + 0.7, -0.6)), fill="#ff4040", outline="",
                          tags=(tag,))
        c.itemconfigure(f"{tag}_d", state="hidden")
        return {"tag": tag, "x": x, "y": y, "up": True, "vis": True}

    def _make_ghost(self, i, x, y, size):
        c, tag = self.c, f"ghost{i}"
        body = [(-10, 0), (-9, -12), (0, -17), (9, -12), (10, 0), (10, 13), (6, 9),
                (3, 14), (0, 9), (-3, 14), (-6, 9), (-10, 13)]
        flat = []
        for bx, by in body:
            flat += [x + bx * size, y + by * size]
        c.create_polygon(flat, smooth=True, fill="#e6defa", outline="", stipple="gray75",
                         tags=(tag,))
        for ex in (-4, 4):
            c.create_oval(x + (ex - 1.8) * size, y + (-8) * size, x + (ex + 1.8) * size,
                          y + (-3.5) * size, fill="#150b22", outline="", tags=(tag,))
        c.create_oval(x - 2 * size, y - 1 * size, x + 2 * size, y + 4 * size,
                      fill="#150b22", outline="", tags=(tag,))
        self.ghosts.append({"tag": tag, "x": x, "y": y, "base": y,
                            "vx": random.choice((-1, 1)) * random.uniform(8, 18),
                            "ph": random.uniform(0, 6.28), "vis": True})

    def _make_pumpkin(self, cx, base, r):
        c = self.c
        cy = base - r * 0.85
        glow = c.create_oval(cx - r * 2.0, cy - r * 1.9, cx + r * 2.0, cy + r * 1.1,
                             fill="#ff7518", outline="", stipple="gray12")
        c.create_oval(cx - r * 1.2, cy - r * 0.85, cx + r * 0.1, cy + r * 0.85,
                      fill="#e8650f", outline="#9a3f06")
        c.create_oval(cx - r * 0.1, cy - r * 0.85, cx + r * 1.2, cy + r * 0.85,
                      fill="#e8650f", outline="#9a3f06")
        c.create_oval(cx - r * 0.85, cy - r * 0.92, cx + r * 0.85, cy + r * 0.92,
                      fill="#ff7518", outline="#b84a08")
        c.create_polygon(cx - r * 0.18, cy - r * 0.9, cx + r * 0.1, cy - r * 1.45,
                         cx + r * 0.38, cy - r * 0.9, fill="#3f6b17", outline="")
        face = [
            c.create_polygon(cx - r * 0.55, cy - r * 0.15, cx - r * 0.2, cy - r * 0.15,
                             cx - r * 0.38, cy - r * 0.55, fill="#ffe066", outline=""),
            c.create_polygon(cx + r * 0.55, cy - r * 0.15, cx + r * 0.2, cy - r * 0.15,
                             cx + r * 0.38, cy - r * 0.55, fill="#ffe066", outline=""),
            c.create_polygon(cx, cy - r * 0.05, cx - r * 0.1, cy + r * 0.15,
                             cx + r * 0.1, cy + r * 0.15, fill="#ffe066", outline=""),
            c.create_polygon(
                cx - r * 0.6, cy + r * 0.25, cx - r * 0.35, cy + r * 0.55, cx - r * 0.18,
                cy + r * 0.32, cx, cy + r * 0.58, cx + r * 0.18, cy + r * 0.32,
                cx + r * 0.35, cy + r * 0.55, cx + r * 0.6, cy + r * 0.25,
                cx + r * 0.3, cy + r * 0.12, cx - r * 0.3, cy + r * 0.12,
                fill="#ffe066", outline=""),
        ]
        return {"face": face, "glow": glow, "cx": cx, "cy": cy, "r": r,
                "ph": random.uniform(0, 6.28)}

    def _make_skeleton(self, x, s, phase):
        c = self.c
        line = dict(fill=self.BONE, width=max(1.5, 2.1 * s), capstyle="round")
        sk = {"x": x, "s": s, "ph": phase}
        sk["skull"] = c.create_oval(0, 0, 1, 1, fill=self.BONE, outline="#b9b29c")
        sk["jaw"] = c.create_rectangle(0, 0, 1, 1, fill=self.BONE, outline="#b9b29c")
        sk["eyeL"] = c.create_oval(0, 0, 1, 1, fill="#120a1c", outline="")
        sk["eyeR"] = c.create_oval(0, 0, 1, 1, fill="#120a1c", outline="")
        sk["nose"] = c.create_oval(0, 0, 1, 1, fill="#120a1c", outline="")
        sk["spine"] = c.create_line(0, 0, 1, 1, **line)
        sk["pelvis"] = c.create_line(0, 0, 1, 1, **line)
        sk["ribs"] = [c.create_line(0, 0, 1, 1, fill=self.BONE, width=max(1.2, 1.7 * s),
                                    capstyle="round") for _ in range(3)]
        sk["arms"] = {side: [c.create_line(0, 0, 1, 1, **line), c.create_line(0, 0, 1, 1, **line)]
                      for side in (-1, 1)}
        sk["legs"] = {side: [c.create_line(0, 0, 1, 1, **line), c.create_line(0, 0, 1, 1, **line)]
                      for side in (-1, 1)}
        return sk

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------

    def start(self):
        if self._job is None and self.enabled:
            self._last = time.perf_counter()
            self._job = self.c.after(1, self._tick)

    def stop(self):
        if self._job is not None:
            try:
                self.c.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def set_enabled(self, on):
        self.enabled = bool(on)
        if self.enabled:
            self.start()
        else:
            self.stop()
            self._clear_particles()

    def set_level(self, level):
        self.level = max(1, min(10, int(level)))
        self.apply_level()

    def set_fear(self, fear):
        self.fear = max(0.0, min(1.0, float(fear)))

    def set_parallax(self, on):
        self.parallax = bool(on)

    def _on_leave(self, _event=None):
        self._hover = False

    def _apply_parallax(self, dt):
        """Slide each depth layer a different amount so the scene has depth."""
        if self.parallax:
            if self._hover:
                tx, ty = self._mx, self._my
            else:  # idle: a slow, gentle camera drift
                tx = 0.5 * math.sin(self._t * 0.27)
                ty = 0.5 * math.sin(self._t * 0.19 + 1.0)
        else:
            tx = ty = 0.0
        k = min(1.0, dt * 5.0)
        self._px += (tx - self._px) * k
        self._py += (ty - self._py) * k
        for name, (ax, ay) in self.AMP.items():
            nx, ny = -self._px * ax, -self._py * ay
            ox, oy = self.off[name]
            dx, dy = nx - ox, ny - oy
            if abs(dx) > 0.05 or abs(dy) > 0.05:
                self.c.move("L_" + name, dx, dy)
                self.off[name] = (nx, ny)

    def apply_level(self):
        L = self.level
        n_bats = min(len(self.bats), 1 + L // 2)
        for i, bat in enumerate(self.bats):
            self._show_bat(bat, i < n_bats)
        n_ghosts = 0 if L < 3 else 1 if L < 6 else 2 if L < 9 else 3
        for i, ghost in enumerate(self.ghosts):
            ghost["vis"] = i < n_ghosts
            self.c.itemconfigure(ghost["tag"], state="normal" if ghost["vis"] else "hidden")
        for i, wisp in enumerate(self.wisps):
            vis = i < L // 2
            wisp["vis"] = vis
            state = "normal" if vis else "hidden"
            self.c.itemconfigure(wisp["glow"], state=state)
            self.c.itemconfigure(wisp["core"], state=state)
        spider_state = "normal" if L >= 3 else "hidden"
        self.c.itemconfigure(self.spider_thread, state=spider_state)
        for item in self.spider:
            self.c.itemconfigure(item, state=spider_state)
        fog_state = "normal" if L >= 2 else "hidden"
        for fog in self.fog:
            self.c.itemconfigure(fog["id"], state=fog_state)

    def _show_bat(self, bat, visible):
        bat["vis"] = visible
        if not visible:
            self.c.itemconfigure(bat["tag"], state="hidden")
            return
        self.c.itemconfigure(bat["tag"], state="normal")
        self.c.itemconfigure(f"{bat['tag']}_u", state="normal" if bat["up"] else "hidden")
        self.c.itemconfigure(f"{bat['tag']}_d", state="hidden" if bat["up"] else "normal")

    # ------------------------------------------------------------------
    # Mouse effects
    # ------------------------------------------------------------------

    def _on_motion(self, event):
        self._hover = True
        self._mx = max(-1.0, min(1.0, (event.x / self.W - 0.5) * 2))
        self._my = max(-1.0, min(1.0, (event.y / self.H - 0.5) * 2))
        if not (self.enabled and self.sparks):
            return
        now = time.perf_counter()
        if now - self._last_spark < 0.02 or len(self.particles) > 70:
            return
        self._last_spark = now
        for _ in range(2):
            self._spawn_particle(
                event.x + random.uniform(-3, 3), event.y + random.uniform(-3, 3),
                random.uniform(-14, 14), random.uniform(-26, -4),
                random.uniform(0.45, 0.9), random.uniform(1.6, 3.2),
                random.choice(("#ff7518", "#ffb347", "#b46bff", "#7dff4f")), gravity=18)

    def _on_click(self, event):
        now = time.perf_counter()
        if not self.enabled or now - self._last_burst < 0.8:
            return
        self._last_burst = now
        for k in range(5):
            ang = random.uniform(0, 6.283)
            speed = random.uniform(60, 120)
            bat = self._make_bat(f"tbat{int(now * 1000) % 100000}_{k}", event.x, event.y,
                                 random.uniform(0.8, 1.2))
            bat.update(vx=math.cos(ang) * speed, vy=math.sin(ang) * speed - 20,
                       life=random.uniform(1.6, 2.4), flap=0.0)
            self.temp_bats.append(bat)

    def _spawn_particle(self, x, y, vx, vy, life, size, color, gravity=0.0):
        item = self.c.create_oval(x - size, y - size, x + size, y + size, fill=color, outline="")
        self.particles.append({"id": item, "x": x, "y": y, "vx": vx, "vy": vy, "life": life,
                               "max": life, "size": size, "color": color, "g": gravity})

    def _clear_particles(self):
        for p in self.particles:
            self.c.delete(p["id"])
        self.particles = []
        for bat in self.temp_bats:
            self.c.delete(bat["tag"])
        self.temp_bats = []

    # ------------------------------------------------------------------
    # Animation
    # ------------------------------------------------------------------

    def _tick(self):
        self._job = None
        started = time.perf_counter()
        dt = min(0.1, started - self._last)
        self._last = started
        delay = 300
        try:
            if self.c.winfo_viewable():
                self._t += dt
                try:
                    self._update(dt, started)
                except Exception as e:
                    if not self._err_logged:
                        print(f"Scene update error: {e}")
                        self._err_logged = True
                spent = (time.perf_counter() - started) * 1000
                delay = max(1, int(1000 / self.FPS - spent))
        except tk.TclError:
            return  # window is closing
        if self.enabled:
            try:
                self._job = self.c.after(delay, self._tick)
            except tk.TclError:
                pass

    def _update(self, dt, now):
        c, t, W = self.c, self._t, self.W
        fear = self.fear
        speed_mul = 1.0 + fear * 0.8 + (0.6 if self.thinking else 0.0)

        self._apply_parallax(dt)

        # Stars twinkle.
        for item, ph, sp in self.stars:
            bright = 0.5 + 0.5 * math.sin(t * sp + ph)
            c.itemconfigure(item, fill=lerp_color("#4a3d66", "#fff6d8", bright))

        # Moon glow pulses.
        mx, my, mr = self.moon
        fox, foy = self.off["far"]
        mx, my = mx + fox, my + foy
        for k, (gid, extra) in enumerate(self.moon_glow):
            r = mr + extra + 1.8 * math.sin(t * 1.3 + k)
            c.coords(gid, mx - r, my - r, mx + r, my + r)

        # Moonlight shafts.
        gy = self.ground - 6
        for ray in self.rays:
            sway = math.sin(t * 0.4 + ray["ph"]) * 9
            bx = mx + ray["dx"] + sway
            c.coords(ray["id"], mx - 4, my + 8, mx + 4, my + 8,
                     bx + ray["w"], gy, bx - ray["w"], gy)

        # Window and pumpkin flicker.
        for win in self.windows:
            if random.random() < 0.05:
                win[1] = random.random()
                c.itemconfigure(win[0], fill=random.choice(
                    ("#ffcf5a", "#ff9a1a", "#ffe49a", "#ffcf5a", "#6b3a10")))
        for pk in self.pumpkins:
            f = 0.5 + 0.5 * math.sin(t * (7 + (14 if self.thinking else 0)) + pk["ph"]) * random.uniform(0.6, 1.0)
            col = lerp_color("#ff9a00", "#fff3a0", f)
            for face in pk["face"]:
                c.itemconfigure(face, fill=col)

        # Fog drifts.
        for fog in self.fog:
            c.move(fog["id"], fog["speed"] * dt, 0)
            x1 = c.coords(fog["id"])[0]
            if fog["speed"] > 0 and x1 > W:
                c.move(fog["id"], -(W + fog["w"] + 20), 0)
            elif fog["speed"] < 0 and x1 + fog["w"] < 0:
                c.move(fog["id"], W + fog["w"] + 20, 0)

        # Bats.
        for bat in self.bats:
            if bat["vis"]:
                self._update_bat(bat, dt, speed_mul)

        # Ghosts.
        for ghost in self.ghosts:
            if not ghost["vis"]:
                continue
            nx = ghost["x"] + ghost["vx"] * dt
            ny = ghost["base"] + math.sin(t * 1.1 + ghost["ph"]) * 9
            if nx > W + 25:
                nx = -25
            elif nx < -25:
                nx = W + 25
            c.move(ghost["tag"], nx - ghost["x"], ny - ghost.get("py", ghost["y"]))
            ghost["x"], ghost["py"] = nx, ny

        # Wisps.
        for wisp in self.wisps:
            if not wisp["vis"]:
                continue
            x = (wisp["cx"] + self.off["near"][0]
                 + wisp["ax"] * math.sin(t * wisp["sp"] + wisp["ph"]))
            y = (wisp["cy"] + self.off["near"][1]
                 + wisp["ay"] * math.sin(t * wisp["sp"] * 1.7 + wisp["ph"]))
            pulse = 3.4 + 0.9 * math.sin(t * 3 + wisp["ph"])
            c.coords(wisp["glow"], x - pulse * 2, y - pulse * 2, x + pulse * 2, y + pulse * 2)
            c.coords(wisp["core"], x - 1.8, y - 1.8, x + 1.8, y + 1.8)

        # Spider bobs on its thread.
        if self.level >= 3:
            sy = 26 + 20 * math.sin(t * 0.7)
            sx = self.spider_x + self.off["mid"][0]
            c.coords(self.spider_thread, sx, 0, sx, sy)
            c.coords(self.spider[0], sx - 3.4, sy - 3, sx + 3.4, sy + 3.4)
            idx = 1
            for side in (-1, 1):
                for k in range(4):
                    wig = math.sin(t * 6 + k) * 1.2
                    c.coords(self.spider[idx], sx, sy + 0.5,
                             sx + side * (5 + k * 1.1), sy - 2 + k * 2 + wig,
                             sx + side * (8 + k * 1.3), sy + 3 + k * 2 + wig)
                    idx += 1

        # Skeletons dance.
        for sk in self.skeletons:
            self._update_skeleton(sk, t)

        # Embers rise from the pumpkins.
        if len(self.particles) < 60 and random.random() < 0.05 * self.level * dt * 30 / 6:
            pk = random.choice(self.pumpkins)
            self._spawn_particle(pk["cx"] + self.off["near"][0] + random.uniform(-4, 4),
                                 pk["cy"] + self.off["near"][1] - pk["r"],
                                 random.uniform(-8, 8), random.uniform(-40, -22),
                                 random.uniform(1.2, 2.0), random.uniform(1.0, 1.8),
                                 random.choice(("#ffb347", "#ff7518", "#ffe066")))

        self._update_particles(dt)
        self._update_temp_bats(dt)
        self._update_lightning(now)
        self._update_title(now)
        self._update_fear_glow()
        self._update_rain(dt)
        self._update_toasts(dt)

    def _update_bat(self, bat, dt, speed_mul):
        c, t, W = self.c, self._t, self.W
        nx = bat["x"] + bat["vx"] * speed_mul * dt
        ny = bat["base"] + math.sin(t * bat["freq"] + bat["phase"]) * bat["amp"]
        if nx > W + 30:
            nx, bat["base"] = -30, random.uniform(14, 100)
        elif nx < -30:
            nx, bat["base"] = W + 30, random.uniform(14, 100)
        c.move(bat["tag"], nx - bat["x"], ny - bat["y"])
        bat["x"], bat["y"] = nx, ny
        bat["flap"] += dt
        if bat["flap"] > 0.11:
            bat["flap"] = 0.0
            bat["up"] = not bat["up"]
            c.itemconfigure(f"{bat['tag']}_u", state="normal" if bat["up"] else "hidden")
            c.itemconfigure(f"{bat['tag']}_d", state="hidden" if bat["up"] else "normal")

    def _update_temp_bats(self, dt):
        c = self.c
        for bat in list(self.temp_bats):
            bat["life"] -= dt
            if bat["life"] <= 0:
                c.delete(bat["tag"])
                self.temp_bats.remove(bat)
                continue
            c.move(bat["tag"], bat["vx"] * dt, bat["vy"] * dt)
            bat["x"] += bat["vx"] * dt
            bat["y"] += bat["vy"] * dt
            bat["flap"] += dt
            if bat["flap"] > 0.08:
                bat["flap"] = 0.0
                bat["up"] = not bat["up"]
                c.itemconfigure(f"{bat['tag']}_u", state="normal" if bat["up"] else "hidden")
                c.itemconfigure(f"{bat['tag']}_d", state="hidden" if bat["up"] else "normal")

    def _update_particles(self, dt):
        c = self.c
        for p in list(self.particles):
            p["life"] -= dt
            if p["life"] <= 0:
                c.delete(p["id"])
                self.particles.remove(p)
                continue
            p["vy"] += p["g"] * dt
            p["x"] += p["vx"] * dt
            p["y"] += p["vy"] * dt
            frac = p["life"] / p["max"]
            r = max(0.4, p["size"] * frac)
            c.coords(p["id"], p["x"] - r, p["y"] - r, p["x"] + r, p["y"] + r)
            c.itemconfigure(p["id"], fill=lerp_color("#1b0f2b", p["color"], frac))

    def _update_skeleton(self, sk, t):
        c, s, G = self.c, sk["s"], self.ground
        w = t * 4.2 + sk["ph"]
        bob = -abs(math.sin(w)) * 2.6 * s
        sway = math.sin(w * 0.5) * 2.2 * s
        nox, noy = self.off["near"]
        base = G + 10 + noy  # feet stand a little in front of the ground line
        x0 = sk["x"] + nox
        hipx, hipy = x0 + sway * 0.6, base - 27 * s + bob
        nx, ny = x0 + sway, base - 52 * s + bob
        hx, hy = x0 + sway * 1.2, base - 62 * s + bob

        c.coords(sk["skull"], hx - 6.8 * s, hy - 7.2 * s, hx + 6.8 * s, hy + 6 * s)
        c.coords(sk["eyeL"], hx - 4.4 * s, hy - 3.2 * s, hx - 1.2 * s, hy + 0.6 * s)
        c.coords(sk["eyeR"], hx + 1.2 * s, hy - 3.2 * s, hx + 4.4 * s, hy + 0.6 * s)
        c.coords(sk["nose"], hx - 0.9 * s, hy + 1.4 * s, hx + 0.9 * s, hy + 3.2 * s)
        clack = abs(math.sin(w * 2)) * 1.8 * s
        c.coords(sk["jaw"], hx - 4.6 * s, hy + 5.2 * s, hx + 4.6 * s, hy + 7.6 * s + clack)
        c.coords(sk["spine"], nx, ny, hipx, hipy)
        for k, ((dy, wid), rib) in enumerate(zip(((4, 7.5), (8.5, 7.8), (13, 7)), sk["ribs"])):
            c.coords(rib, nx - wid * s, ny + dy * s, nx + wid * s, ny + dy * s)
        c.coords(sk["pelvis"], hipx - 4.5 * s, hipy, hipx + 4.5 * s, hipy)

        for side in (-1, 1):
            shx, shy = nx + side * 5.5 * s, ny + 3 * s
            a1 = side * 0.55 + 1.15 * math.sin(w + (0 if side < 0 else math.pi))
            elx, ely = shx + math.sin(a1) * 12 * s, shy + math.cos(a1) * 12 * s
            a2 = a1 + 0.9 * math.sin(w + 1.2)
            hdx, hdy = elx + math.sin(a2) * 11 * s, ely + math.cos(a2) * 11 * s
            upper, fore = sk["arms"][side]
            c.coords(upper, shx, shy, elx, ely)
            c.coords(fore, elx, ely, hdx, hdy)

            hpx, hpy = hipx + side * 3.2 * s, hipy
            b1 = side * 0.16 + 0.4 * math.sin(w + (0 if side < 0 else math.pi))
            knx, kny = hpx + math.sin(b1) * 13 * s, hpy + math.cos(b1) * 13 * s
            b2 = b1 * 0.4 - max(0.0, math.sin(w + (0 if side < 0 else math.pi))) * 0.6
            ftx, fty = knx + math.sin(b2) * 13 * s, kny + math.cos(b2) * 13 * s
            thigh, shin = sk["legs"][side]
            c.coords(thigh, hpx, hpy, knx, kny)
            c.coords(shin, knx, kny, ftx, fty)

    def _update_lightning(self, now):
        if self.level >= 4 and not self._flash_windows and now >= self._next_flash:
            self._flash_windows = [(now, now + 0.08), (now + 0.17, now + 0.29)]
            self._next_flash = now + random.uniform(8, 20) / (1 + self.fear * 2.5)
            self._make_bolt()
        if self._flash_windows:
            on = any(a <= now <= b for a, b in self._flash_windows)
            self.c.itemconfigure(self.flash, state="normal" if on else "hidden")
            if self._bolt is not None:
                self.c.itemconfigure(self._bolt, state="normal" if on else "hidden")
            if now > self._flash_windows[-1][1]:
                self._flash_windows = []
                self.c.itemconfigure(self.flash, state="hidden")
                self._drop_bolt()

    def _make_bolt(self):
        """A jagged lightning bolt that flashes with the sky."""
        self._drop_bolt()
        x = random.uniform(self.W * 0.15, self.W * 0.8)
        y = 0.0
        pts = [x, y]
        while y < self.ground - 20:
            y += random.uniform(10, 18)
            x += random.uniform(-14, 14)
            pts += [x, y]
        self._bolt = self.c.create_line(pts, fill="#efe6ff", width=2)
        self.c.tag_raise(self._bolt, self.flash)

    def _drop_bolt(self):
        if self._bolt is not None:
            self.c.delete(self._bolt)
            self._bolt = None

    def _update_rain(self, dt):
        on = self.fear >= 0.5 and self.level >= 3
        if on != self._rain_on:
            self._rain_on = on
            for d in self.rain:
                self.c.itemconfigure(d["id"], state="normal" if on else "hidden")
        if not on:
            return
        for d in self.rain:
            d["y"] += d["v"] * dt
            d["x"] -= d["v"] * 0.22 * dt
            if d["y"] > self.H:
                d["y"] = -10
                d["x"] = random.uniform(0, self.W + 40)
            if d["x"] < -10:
                d["x"] = self.W + 10
            self.c.coords(d["id"], d["x"], d["y"], d["x"] - 2, d["y"] + 8)

    def toast(self, text, color="#ffd24a"):
        """A message that floats up from the bottom of the scene and fades."""
        if not self.enabled:
            return
        for old in self.toasts:
            self.c.delete(old["id"])
            self.c.delete(old["sh"])
        self.toasts = []
        x, y = self.W / 2, self.H - 44
        font = ("Segoe UI", 12, "bold")
        shadow = self.c.create_text(x + 1, y + 1, text=text, font=font, fill="#05020a")
        item = self.c.create_text(x, y, text=text, font=font, fill=color)
        self.toasts.append({"id": item, "sh": shadow, "y": y, "life": 2.0,
                            "max": 2.0, "color": color})

    def _update_toasts(self, dt):
        for ts in list(self.toasts):
            ts["life"] -= dt
            if ts["life"] <= 0:
                self.c.delete(ts["id"])
                self.c.delete(ts["sh"])
                self.toasts.remove(ts)
                continue
            ts["y"] -= 14 * dt
            frac = min(1.0, ts["life"] / ts["max"] * 2.0)
            self.c.coords(ts["id"], self.W / 2, ts["y"])
            self.c.coords(ts["sh"], self.W / 2 + 1, ts["y"] + 1)
            self.c.itemconfigure(ts["id"], fill=lerp_color("#1b0f2b", ts["color"], frac))

    def _update_title(self, now):
        if not self._title_flicker and now >= self._next_title_flicker:
            self._title_flicker = [(now + 0.00, now + 0.06), (now + 0.12, now + 0.17),
                                   (now + 0.26, now + 0.30)]
            self._next_title_flicker = now + random.uniform(3, 8) / (1 + self.fear)
        if self._title_flicker:
            dim = any(a <= now <= b for a, b in self._title_flicker)
            col = "#7a3a12" if dim else "#ff7518"
            for item in (self.title, self.skull_l, self.skull_r):
                self.c.itemconfigure(item, fill=col)
            if now > self._title_flicker[-1][1]:
                self._title_flicker = []
                for item in (self.title, self.skull_l, self.skull_r):
                    self.c.itemconfigure(item, fill="#ff7518")

    def _update_fear_glow(self):
        f = self.fear
        bucket = 0 if f < 0.3 else 1 if f < 0.5 else 2 if f < 0.75 else 3
        if bucket != self._fear_bucket:
            self._fear_bucket = bucket
            if bucket == 0:
                self.c.itemconfigure(self.fear_glow, state="hidden")
            else:
                stip = ("", "gray12", "gray25", "gray50")[bucket]
                self.c.itemconfigure(self.fear_glow, state="normal", stipple=stip)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

BG_PALETTES = {
    # top colour, bottom colour, three glow pools, dark colour particles fade into
    "Haunted": dict(top="#0a0610", bottom="#210f33", dark="#1b0f2b",
                    pools=("#3b1a5e", "#2a1445", "#4b1d78")),
    "Blood Moon": dict(top="#0d0405", bottom="#2e0d12", dark="#1f0a0d",
                       pools=("#5e1a24", "#45141c", "#781d2b")),
    "Forest": dict(top="#030d08", bottom="#0f2e1c", dark="#0a1f14",
                   pools=("#14532d", "#0e3a20", "#1f6b3a")),
    "Abyss": dict(top="#02060e", bottom="#0b2140", dark="#08162b",
                  pools=("#12437a", "#0b2f58", "#1b5a9e")),
    "Dusk": dict(top="#0f0a14", bottom="#3a1a2e", dark="#26121f",
                 pools=("#6b2a4d", "#4a1d37", "#8a3a63")),
    "Void": dict(top="#000000", bottom="#0b0b10", dark="#050508",
                 pools=("#15151f", "#101018", "#1c1c2a")),
}
BG_STYLES = ["Motes", "Embers", "Fireflies", "Rain", "Ash", "Stars", "Off"]
BG_PARTICLE_COLORS = {
    "Motes": ("#b46bff", "#ff7518", "#7dff4f", "#ffd24a", "#b46bff"),
    "Embers": ("#ff7518", "#ffb347", "#ff4d1a", "#ffd24a"),
    "Fireflies": ("#d6ff6a", "#ffe066", "#9dff7a"),
    "Rain": ("#6f8fd6", "#8aa8e8", "#5b78bd"),
    "Ash": ("#8a8196", "#6b6478", "#a79fb3"),
    "Stars": ("#fff6d8", "#c9b8ff", "#ffd9a8"),
}
BG_DIM_STIPPLES = (None, "gray12", "gray25", "gray50", "gray75")


class Backdrop:
    """The animated layer behind every other widget: a colour gradient (or your
    own PNG/GIF picture), soft light pools and drifting particles. Everything
    about it can be changed live with configure()."""

    COLORS = BG_PARTICLE_COLORS["Motes"]

    def __init__(self, canvas, width, height, style="Motes", amount=36,
                 speed=1.0, palette="Haunted", image="", dim=3):
        self.c = canvas
        self.W, self.H = width, height
        self.enabled = True
        self._job = None
        self._t = 0.0
        self._last = time.perf_counter()
        self._rnd = random.Random(5)
        self._img = None          # keeps the PhotoImage alive
        self.image_error = ""
        self.motes = []
        self.style = style if style in BG_STYLES else "Motes"
        self.amount = max(0, min(120, int(amount)))
        self.speed = max(0.2, min(3.0, float(speed)))
        self.palette = palette if palette in BG_PALETTES else "Haunted"
        self.image = image or ""
        self.dim = max(0, min(4, int(dim)))
        self._build()

    @property
    def FPS(self):
        return 18 if self.style in ("Rain", "Embers") else 12

    def configure(self, **opts):
        """Change any of style / amount / speed / palette / image / dim."""
        full = particles = False
        if "style" in opts and opts["style"] in BG_STYLES and opts["style"] != self.style:
            self.style = opts["style"]
            particles = True
        if "amount" in opts and int(opts["amount"]) != self.amount:
            self.amount = max(0, min(120, int(opts["amount"])))
            particles = True
        if "speed" in opts:
            self.speed = max(0.2, min(3.0, float(opts["speed"])))   # live, no rebuild
        if "palette" in opts and opts["palette"] in BG_PALETTES and opts["palette"] != self.palette:
            self.palette = opts["palette"]
            full = True
        if "image" in opts and (opts["image"] or "") != self.image:
            self.image = opts["image"] or ""
            full = True
        if "dim" in opts and int(opts["dim"]) != self.dim:
            self.dim = max(0, min(4, int(opts["dim"])))
            full = True
        if full:
            self.c.delete("all")
            self._build()
        elif particles:
            self.c.delete("mote")
            self._make_particles()
        self.set_enabled(self.enabled)

    # ----- construction -----

    def _load_image(self):
        self.image_error = ""
        self._img = None
        if not self.image:
            return None
        try:
            img = tk.PhotoImage(master=self.c, file=self.image)
            iw, ih = img.width(), img.height()
            if iw <= 0 or ih <= 0:
                raise ValueError("empty image")
            if iw >= self.W and ih >= self.H:        # shrink to just cover the window
                f = max(1, int(min(iw / self.W, ih / self.H)))
                if f > 1:
                    img = img.subsample(f, f)
            else:                                     # enlarge small pictures
                f = int(math.ceil(max(self.W / iw, self.H / ih)))
                img = img.zoom(f, f)
            self._img = img
            return img
        except Exception as err:
            self.image_error = f"{type(err).__name__}: {err}"
            return None

    def _build(self):
        c, W, H = self.c, self.W, self.H
        pal = BG_PALETTES[self.palette]
        img = self._load_image()
        if img is not None:
            c.create_image(W / 2, H / 2, image=img, anchor="center")
            stip = BG_DIM_STIPPLES[self.dim]
            if stip:
                c.create_rectangle(0, 0, W, H, fill="#000000", outline="", stipple=stip)
        else:
            bands = 30
            for i in range(bands):
                col = lerp_color(pal["top"], pal["bottom"], (i / (bands - 1)) ** 1.5)
                c.create_rectangle(0, i * H / bands, W, (i + 1) * H / bands + 1,
                                   fill=col, outline="")
            for (x, y, r), col in zip(((W * 0.08, H * 0.16, 190),
                                       (W * 0.96, H * 0.52, 220),
                                       (W * 0.22, H * 0.98, 240)), pal["pools"]):
                for k, stip in enumerate(("gray12", "gray12", "gray25")):
                    rr = r * (1 - 0.3 * k)
                    c.create_oval(x - rr, y - rr, x + rr, y + rr, fill=col,
                                  outline="", stipple=stip)
        self._make_particles()

    def _make_particles(self):
        c, rnd, W, H = self.c, self._rnd, self.W, self.H
        self.motes = []
        if self.style == "Off":
            return
        colors = BG_PARTICLE_COLORS[self.style]
        dark = BG_PALETTES[self.palette]["dark"]
        for _ in range(self.amount):
            base = rnd.choice(colors)
            x, y = rnd.uniform(0, W), rnd.uniform(0, H)
            if self.style == "Rain":
                item = c.create_line(x, y, x - 3, y + 12, fill=dark, width=1,
                                     tags=("mote",))
            else:
                item = c.create_oval(0, 0, 1, 1, fill=base, outline="", tags=("mote",))
            self.motes.append({"id": item, "x": x, "y": y, "vy": rnd.uniform(5, 16),
                               "ph": rnd.uniform(0, 6.28), "r": rnd.uniform(0.9, 2.3),
                               "color": base})

    # ----- loop -----

    def start(self):
        if self._job is None and self.enabled:
            self._last = time.perf_counter()
            self._job = self.c.after(1, self._tick)

    def stop(self):
        if self._job is not None:
            try:
                self.c.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def set_enabled(self, on):
        self.enabled = bool(on)
        try:
            self.c.itemconfigure("mote", state="normal" if self.enabled else "hidden")
        except tk.TclError:
            return
        if self.enabled and self.motes:
            self.start()
        else:
            self.stop()

    def _tick(self):
        self._job = None
        now = time.perf_counter()
        dt = min(0.2, now - self._last)
        self._last = now
        delay = 400
        try:
            if self.c.winfo_viewable():
                self._t += dt
                try:
                    self._update(dt)
                except Exception:
                    pass
                delay = int(1000 / self.FPS)
        except tk.TclError:
            return
        if self.enabled and self.motes:
            try:
                self._job = self.c.after(delay, self._tick)
            except tk.TclError:
                pass

    def _update(self, dt):
        t, W, H, sp, style = self._t, self.W, self.H, self.speed, self.style
        dark = BG_PALETTES[self.palette]["dark"]
        c = self.c
        for m in self.motes:
            r = m["r"]
            if style == "Rain":
                m["y"] += m["vy"] * 9 * sp * dt
                m["x"] -= m["vy"] * 2.2 * sp * dt
                if m["y"] > H + 14 or m["x"] < -6:
                    m["y"] = -14
                    m["x"] = self._rnd.uniform(0, W + 40)
                length = 6 + m["vy"] * 0.7
                c.coords(m["id"], m["x"], m["y"], m["x"] - length * 0.25, m["y"] + length)
                c.itemconfigure(m["id"], fill=lerp_color(dark, m["color"], 0.55))
                continue
            if style == "Embers":
                m["y"] -= m["vy"] * 2.4 * sp * dt
                m["x"] += math.sin(t * 1.4 * sp + m["ph"]) * 16 * sp * dt
                if m["y"] < -6:
                    m["y"] = H + 6
                    m["x"] = self._rnd.uniform(0, W)
                life = max(0.0, min(1.0, m["y"] / H))
                r = r * (0.5 + 0.7 * life)
                col = lerp_color(dark, m["color"], 0.25 + 0.75 * life)
            elif style == "Fireflies":
                m["x"] += math.cos(t * 0.5 * sp + m["ph"]) * 20 * sp * dt
                m["y"] += math.sin(t * 0.37 * sp + m["ph"] * 1.3) * 15 * sp * dt
                m["x"] %= W
                m["y"] %= H
                glow = max(0.0, math.sin(t * 1.2 * sp + m["ph"] * 2)) ** 3
                r = r * (1.0 + 0.8 * glow)
                col = lerp_color(dark, m["color"], 0.12 + 0.88 * glow)
            elif style == "Ash":
                m["y"] += m["vy"] * 0.7 * sp * dt
                m["x"] += math.sin(t * 0.5 * sp + m["ph"]) * 11 * sp * dt
                if m["y"] > H + 6:
                    m["y"] = -6
                    m["x"] = self._rnd.uniform(0, W)
                col = lerp_color(dark, m["color"], 0.55)
            elif style == "Stars":
                glow = 0.3 + 0.7 * math.sin(t * 1.1 * sp + m["ph"]) ** 2
                col = lerp_color(dark, m["color"], glow)
            else:  # Motes
                m["y"] -= m["vy"] * sp * dt
                m["x"] += math.sin(t * 0.6 * sp + m["ph"]) * 6 * sp * dt
                if m["y"] < -6:
                    m["y"] = H + 6
                    m["x"] = self._rnd.uniform(0, W)
                glow = 0.45 + 0.55 * math.sin(t * 1.5 + m["ph"]) ** 2
                col = lerp_color(dark, m["color"], glow)
            c.coords(m["id"], m["x"] - r, m["y"] - r, m["x"] + r, m["y"] + r)
            c.itemconfigure(m["id"], fill=col)


class LiveChessDashboard(ctk.CTk):

    def __init__(self):
        _ok, _why = _lic.check_saved()
        if not _ok:
            _license_die(_why)
        super().__init__()
        self.after(random.randint(40000, 90000), self._license_tick)

        self.title(f"Haunted Stockfish {APP_VERSION}")
        cfg = load_config()
        self.cockpit_left_width = num_setting(
            cfg, "cockpit_left_width", 620, 580, 4000, int)
        screen_h = self.winfo_screenheight()
        self._win_h = max(420, min(1000, screen_h - 90))
        self._compact_h = max(360, min(640, screen_h - 80))
        self.geometry(f"580x{self._win_h}")
        self.resizable(True, True)
        self.minsize(580, min(600, self._win_h))
        self.configure(fg_color=BG)

        self.panel_sizes = {}
        saved_sizes = cfg.get("panel_sizes", {})
        if isinstance(saved_sizes, dict):
            for key, (lo, hi) in {
                "scene": (90, 420), "info": (140, 360),
                "tabs": (160, 800), "log": (100, 1200),
            }.items():
                try:
                    if key in saved_sizes:
                        self.panel_sizes[key] = max(lo, min(hi, int(saved_sizes[key])))
                except (TypeError, ValueError):
                    pass

        # ----- runtime state -----
        self.is_running = False
        self.bot_thread = None

        self.last_spoken_text = None
        self.last_move_uci = None
        self.last_moves = []  # best-to-worst list of UCI moves

        # ----- settings (plain attributes so worker threads can read them) -----
        color = str(cfg.get("color", "Auto"))
        self.s_color = color if color in ("Auto", "White", "Black") else "Auto"
        self.s_time = num_setting(cfg, "time", 0.5, 0.1, 5.0)
        self.s_depth = num_setting(cfg, "depth", 15, 0, 40, int)
        self.s_use_elo = bool(cfg.get("use_elo", False))
        self.s_elo = num_setting(cfg, "elo", 1500, 1320, 3000, int)
        self.s_skill = num_setting(cfg, "skill", 20, 0, 20, int)
        self.s_overlay = bool(cfg.get("overlay", True))
        self.s_lines = num_setting(cfg, "lines", 1, 1, 3, int)
        style = str(cfg.get("style", "Arrows"))
        self.s_style = style if style in ("Arrows", "Squares") else "Arrows"
        self.s_test_mode = False  # never saved: you opt in each launch

        mode = OLD_MODE_NAMES.get(str(cfg.get("reveal", INSTANT_MODE)), None)
        if mode is None:
            mode = str(cfg.get("reveal", INSTANT_MODE))
        self.reveal_mode = mode if mode in REVEAL_MODES else INSTANT_MODE
        self.arrow_delay = num_setting(cfg, "arrow_delay", 1.0, 0.2, 3.0)
        self.hold_to_hide = num_setting(cfg, "hold", 0.6, 0.3, 1.5)
        self.arrow_opacity = num_setting(cfg, "opacity", 0.8, 0.3, 1.0)
        self.voice_rate = num_setting(cfg, "voice_rate", 0, -5, 8, int)
        self.voice_volume = num_setting(cfg, "voice_volume", 100, 0, 100, int)

        # Spooky extras
        self.s_scene_on = bool(cfg.get("scene_on", True))
        self.s_scene_level = num_setting(cfg, "scene_level", 6, 1, 10, int)
        self.s_sparks = bool(cfg.get("sparks", True))
        self.s_skull_cursor = bool(cfg.get("skull_cursor", True))
        self.s_ambience = bool(cfg.get("ambience", False))
        persona = str(cfg.get("persona", "Plain"))
        self.persona = persona if persona in PERSONAS else "Plain"
        self.s_neural = bool(cfg.get("neural", False))
        self.s_quality = bool(cfg.get("quality", True))
        self.s_quality_voice = bool(cfg.get("quality_voice", True))
        self.s_quality_big_only = bool(cfg.get("quality_big_only", False))
        self.s_auto_move = False  # never saved: auto-move is always OFF at launch
        self.auto_delay = num_setting(cfg, "auto_delay", 1.2, 0.3, 4.0)
        self.play_now_until = 0.0  # "play it once" key is armed until this time
        self.s_silent_hidden = bool(cfg.get("silent_hidden", True))
        self.win_alpha = num_setting(cfg, "alpha", 1.0, 0.4, 1.0)
        self.last_grade = None     # the opponent's last graded move + explanation
        self.opening_book = build_opening_book()
        self.opening_name = ""
        self.match_hits = 0        # times you played the suggested move
        self.match_total = 0
        self.game_pgn = ""
        self.s_compact = False     # never saved: you always start in the full view
        self.s_theme = THEME_NAME
        nv = str(cfg.get("neural_voice", "Persona default"))
        self.neural_voice = nv if nv in EDGE_VOICE_CHOICES else "Persona default"
        globals()["EDGE_VOICE_OVERRIDE"] = (
            None if self.neural_voice == "Persona default" else self.neural_voice
        )
        self._wake = threading.Event()  # wakes the worker loop right away
        self._console_was_visible = False
        self.quality_tally = {}
        self._telemetry_text = "Engine: idle"
        self.s_parallax = bool(cfg.get("parallax", True))
        self.s_backdrop = bool(cfg.get("backdrop", True))
        _style = str(cfg.get("bg_style", "Motes"))
        self.bg_style = _style if _style in BG_STYLES else "Motes"
        _pal = str(cfg.get("bg_palette", "Haunted"))
        self.bg_palette = _pal if _pal in BG_PALETTES else "Haunted"
        self.bg_amount = num_setting(cfg, "bg_amount", 36, 0, 120, int)
        self.bg_speed = num_setting(cfg, "bg_speed", 1.0, 0.2, 3.0)
        self.bg_dim = num_setting(cfg, "bg_dim", 3, 0, 4, int)
        _img = str(cfg.get("bg_image", "") or "")
        self.bg_image = _img if _img and os.path.isfile(_img) else ""
        self.s_topmost = bool(cfg.get("topmost", False))
        self.s_attach_only = bool(cfg.get("attach_only", True))
        self.s_muted = False
        self.paused = False
        self.grim_count = 0
        self.eval_history = []   # centipawns from your side, one per suggestion
        self.page_typing = False       # typing in a box on the web page
        self.fear_value = 0.0
        self.speaker = SpeechQueue(lambda m: self.log(f"[VOICE ERROR] {m}"))
        self.ambience = Ambience()

        self.reveal_count = 0        # arrows currently revealed (step mode)
        self.arrow_show_at = 0.0     # when arrows may appear (delayed mode)

        # ----- keys -----
        self.hotkey = str(cfg.get("hotkey", DEFAULT_HOTKEY)).lower()
        self.reveal_key = str(cfg.get("reveal_key", DEFAULT_REVEAL_KEY)).lower()
        self.hide_key = str(cfg.get("hide_key", "f9")).lower()
        self.hidden = False  # dashboard minimized + arrows cleared
        self.extra_keys = {
            a: str(cfg.get("key_" + a, default)).lower()
            for a, (_label, default) in EXTRA_ACTIONS.items()
        }
        self.extra_btns = {}
        self.recording_target = None  # "repeat", "reveal", or None
        self._rebind_token = 0
        self._held = set()
        self._typing = False          # a text box in this window has focus
        self._hwnd = None             # this window's handle (Windows)
        self._hook = None
        self.key_test_until = 0.0
        self._reveal_timer = None
        self._reveal_down = False
        self._reveal_hold_fired = False
        self._save_job = None
        self._backdrop_resize_job = None
        self._backdrop_paused_for_resize = False
        self._scene_paused_for_resize = False
        self._panel_resize_job = None
        self._panel_resize_target = None
        self._panel_resize_active = None
        self._cockpit_resize_job = None
        self._cockpit_resize_target = None
        self._cockpit_redraw_job = None
        self._ui_queue = queue.Queue()  # worker threads post UI work here

        self._build_ui()

        # Animated backdrop behind every other widget.
        self.backdrop_canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.backdrop_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        tk.Misc.lower(self.backdrop_canvas)
        self.backdrop = Backdrop(self.backdrop_canvas, 580, self._win_h,
                                 **self._bg_opts())
        self.backdrop.set_enabled(self.s_backdrop)
        self.bind("<Configure>", self._on_window_configure, add="+")
        self.after(200, self._resize_scene_to_canvas)
        if self.backdrop.image_error:
            self.log("[ERROR] Background picture couldn't be loaded: "
                     + self.backdrop.image_error)

        # Track whether a text box in this window has focus, so a shortcut
        # doesn't fire while you type. (Only applies while this window is in
        # front - see _dashboard_in_front.)
        self.bind("<FocusIn>", self._schedule_focus_check)
        self.bind("<FocusOut>", self._schedule_focus_check)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._cache_window_handle()
        self._install_hotkey_hook()
        self.after(50, self._drain_ui_queue)

        self.scene.set_level(self.s_scene_level)
        self.scene.sparks = self.s_sparks
        self.scene.set_parallax(self.s_parallax)
        self.scene.set_enabled(self.s_scene_on)
        try:
            self.attributes("-topmost", self.s_topmost)
            self.attributes("-alpha", self.win_alpha)
        except tk.TclError:
            pass
        self._apply_cursor()
        if self.s_ambience:
            self.after(900, lambda: self.on_ambience_toggle(True, announce=False))
        self.after(700, self._startup_report)
        self.bind_all("<Control-f>", lambda _e: self.focus_search())
        self.bind("<Escape>", self._on_escape)
        if keyboard is None:
            self.bind("<F11>", lambda _e: self.run_extra_action("fullscreen"))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self._reg = []            # every setting, for the Find tab
        self._tab_of = {}         # frame id -> tab name
        self._tab_names = []
        self._last_title = ""
        self._orb = {"fen": None, "moves": [], "flipped": False, "last": []}
        self.s_orb_moves = True
        self._search_first = None
        self._fs = False
        self._windowed_geometry = None
        self._cp_job = None
        self._session_start = time.time()
        self.games_seen = 0
        self.s_orb_threats = True
        _bs = str(load_config().get("board_style", "Haunted"))
        self.s_board_style = _bs if _bs in BOARD_STYLES else "Haunted"
        self.f_title = ctk.CTkFont(family="Chiller", size=40)
        self.f_sub = ctk.CTkFont(family="Segoe UI", size=12)
        self.f_section = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self.f_label = ctk.CTkFont(family="Segoe UI", size=13)
        self.f_value = ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
        self.f_small = ctk.CTkFont(family="Segoe UI", size=11)
        self.f_button = ctk.CTkFont(family="Segoe UI", size=14, weight="bold")

        # ----- animated banner -----
        banner_w = 544
        banner_h = self.panel_sizes.get("scene", 172)
        self.scene_canvas = tk.Canvas(
            self,
            width=banner_w,
            height=banner_h,
            bg=BG,
            highlightthickness=2,
            highlightbackground=PURPLE_DARK,
            bd=0,
        )
        self.scene_canvas.pack(fill="x", padx=16, pady=(14, 6))
        self.scene = HauntedScene(self, self.scene_canvas, banner_w, banner_h)
        self._add_panel_resize_handle(
            "scene", self, self.scene_canvas, 90, 420)

        # ----- status + fear meter -----
        info = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=12,
                            border_width=2, border_color=PURPLE_DARK)
        self.info_frame = info
        info.pack(fill="x", padx=16, pady=(0, 6))
        info.grid_columnconfigure(1, weight=1)
        self.status_label = ctk.CTkLabel(
            info,
            text="  Idle  ",
            font=self.f_value,
            text_color=STATUS_COLORS["idle"],
            fg_color=SURFACE2,
            corner_radius=12,
        )
        self.status_label.grid(row=0, column=0, padx=(10, 8), pady=8)
        self.fear_bar = ctk.CTkProgressBar(
            info, height=10, progress_color=SLIME, fg_color=SURFACE2
        )
        self.fear_bar.grid(row=0, column=1, sticky="ew", padx=6, pady=8)
        self.fear_bar.set(0)
        self.fear_label = ctk.CTkLabel(
            info, text="Fear: Calm", font=self.f_value, text_color=SLIME, width=110
        )
        self.fear_label.grid(row=0, column=2, padx=(6, 12), pady=8)
        self.telemetry_label = ctk.CTkLabel(
            info, text="Engine: idle", font=self.f_small, text_color=MUTED,
            anchor="w", justify="left",
        )
        self.telemetry_label.grid(
            row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 4)
        )
        self.eval_canvas = tk.Canvas(
            info, height=44, bg=SURFACE, highlightthickness=0, bd=0
        )
        self.eval_canvas.grid(row=2, column=0, columnspan=3, sticky="ew",
                              padx=12, pady=(0, 6))
        self.eval_canvas.bind("<Configure>", lambda _e: self._draw_eval())

        # Command deck: live switches you can click (or use the shortcut keys).
        deck = ctk.CTkFrame(info, fg_color="transparent")
        deck.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 6))
        deck.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="chip")
        deck.grid_columnconfigure(4, weight=0)
        chip_opts = dict(height=28, corner_radius=14, border_width=1,
                         font=self.f_small)
        self.chip_auto = ctk.CTkButton(
            deck, text="", command=lambda: self.run_extra_action("auto"), **chip_opts)
        self.chip_auto.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.chip_voice = ctk.CTkButton(
            deck, text="", command=lambda: self.run_extra_action("quality"), **chip_opts)
        self.chip_voice.grid(row=0, column=1, sticky="ew", padx=3)
        self.chip_sound = ctk.CTkButton(
            deck, text="", command=lambda: self.run_extra_action("mute"), **chip_opts)
        self.chip_sound.grid(row=0, column=2, sticky="ew", padx=3)
        self.chip_view = ctk.CTkButton(
            deck, text="", command=lambda: self.run_extra_action("compact"), **chip_opts)
        self.chip_view.grid(row=0, column=3, sticky="ew", padx=3)
        self.chip_search = ctk.CTkButton(
            deck, text="Find", width=48, command=self.focus_search,
            fg_color=PURPLE_DARK, hover_color=PURPLE, text_color=TEXT, **chip_opts)
        self.chip_search.grid(row=0, column=4, sticky="e", padx=(3, 0))

        # The opponent's last move, graded, with the reason underneath.
        self.grade_card = ctk.CTkFrame(
            info, fg_color=SURFACE2, corner_radius=10,
            border_width=2, border_color=PURPLE_DARK)
        self.grade_card.grid(row=4, column=0, columnspan=3, sticky="ew",
                             padx=10, pady=(0, 10))
        self.grade_title = ctk.CTkLabel(
            self.grade_card, text="Opponent's last move: waiting...",
            font=self.f_value, text_color=MUTED, anchor="w")
        self.grade_title.pack(fill="x", padx=10, pady=(6, 0))
        self.grade_body = ctk.CTkLabel(
            self.grade_card, text="", font=self.f_small, text_color=TEXT,
            anchor="w", justify="left", wraplength=500)
        self.grade_body.pack(fill="x", padx=10, pady=(0, 6))
        self._refresh_chips()
        if "info" in self.panel_sizes:
            info.configure(height=self.panel_sizes["info"])
            info.pack_propagate(False)
        self._add_panel_resize_handle(
            "info", self, info, 140, 360)

        # ----- tabs -----
        self.tabs = ctk.CTkTabview(
            self,
            height=self.panel_sizes.get("tabs", 300),
            fg_color=SURFACE,
            segmented_button_fg_color=SURFACE2,
            segmented_button_selected_color=PUMPKIN,
            segmented_button_selected_hover_color=PUMPKIN_HOVER,
            segmented_button_unselected_color=SURFACE2,
            segmented_button_unselected_hover_color=PURPLE_DARK,
            text_color=TEXT,
            corner_radius=14,
            border_width=1,
            border_color=PURPLE_DARK,
        )
        self.tabs.pack(fill="x", padx=16, pady=4)
        try:  # smaller tab labels so eight tabs fit
            self.tabs._segmented_button.configure(
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"))
        except Exception:
            pass
        play = self._tab_frame("\u263d Play")
        engine = self._tab_frame("\u2699 Engine")
        board = self._tab_frame("\u265e Board")
        orb = self._tab_frame("\u25c9 Orb")
        keys = self._tab_frame("\u2328 Keys")
        spooky = self._tab_frame("\u2620 Spooky")
        grim = self._tab_frame("\u269c Grimoire")
        find = self._tab_frame("\u2315 Find")
        self._find_tab = "\u2315 Find"

        self._build_play_tab(play)
        self._build_engine_tab(engine)
        self._build_board_tab(board)
        self._build_orb_tab(orb)
        self._build_keys_tab(keys)
        self._build_spooky_tab(spooky)
        self._build_grimoire_tab(grim)
        self._build_search_tab(find)   # last, so it can see every setting
        self._add_panel_resize_handle(
            "tabs", self, self.tabs, 160, 800)

        # ----- start / stop -----
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(8, 2))
        self.btn_frame = btn_frame
        btn_frame.grid_columnconfigure((0, 1), weight=1)

        self.start_btn = ctk.CTkButton(
            btn_frame,
            text="\u25b6  Awaken the Assistant",
            command=self.start_assistant,
            fg_color=SLIME_DARK,
            hover_color="#2f7a18",
            text_color="#f4fff0",
            font=self.f_button,
            height=42,
            corner_radius=12,
        )
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_btn = ctk.CTkButton(
            btn_frame,
            text="\u25a0  Banish It",
            command=self.stop_assistant,
            fg_color=BLOOD,
            hover_color=BLOOD_HOVER,
            text_color="#fff0f0",
            font=self.f_button,
            height=42,
            corner_radius=12,
            state="disabled",
        )
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self.progress = ctk.CTkProgressBar(
            self,
            mode="indeterminate",
            height=5,
            progress_color=SURFACE2,   # hidden until the engine is thinking
            fg_color=SURFACE2,
        )
        self.progress.pack(fill="x", padx=18, pady=(8, 4))

        # ----- activity log -----
        log_frame = ctk.CTkFrame(
            self, fg_color=SURFACE, corner_radius=14,
            border_width=1, border_color=PURPLE_DARK,
            height=self.panel_sizes.get("log", 240))
        log_frame.pack(
            fill="both", expand=("log" not in self.panel_sizes),
            padx=16, pady=(4, 14))
        self.log_frame = log_frame
        if "log" in self.panel_sizes:
            log_frame.pack_propagate(False)
        ctk.CTkLabel(
            log_frame, text="Spirit Log", font=self.f_section, text_color=PUMPKIN
        ).pack(anchor="w", padx=14, pady=(8, 0))
        self._add_panel_resize_handle(
            "log", log_frame, log_frame, 100, 1200,
            side="bottom", padx=12, pady=(0, 3))
        self.log_box = ctk.CTkTextbox(
            log_frame,
            font=("Consolas", 11),
            fg_color="#0b0710",
            text_color=TEXT,
            border_width=1,
            border_color=PURPLE_DARK,
            corner_radius=10,
        )
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        for tag, color in (
            ("move", PUMPKIN),
            ("error", "#ff6b6b"),
            ("good", SLIME),
            ("key", "#c79bff"),
            ("warn", CANDLE),
            ("dim", "#6f5d88"),
            ("brill", "#4dd8ff"),
        ):
            self.log_box.tag_config(tag, foreground=color)
        self.log_box.insert(
            "0.0", "The crypt is quiet. Adjust your charms, then awaken the assistant.\n", "dim"
        )
        self.log_box.configure(state="disabled")
        self._build_cockpit()

    def _tab_frame(self, name):
        tab = self.tabs.add(name)
        frame = ctk.CTkScrollableFrame(
            tab,
            fg_color="transparent",
            scrollbar_button_color=PURPLE_DARK,
            scrollbar_button_hover_color=PURPLE,
        )
        frame.pack(fill="both", expand=True)
        self._tab_of[id(frame)] = name
        self._tab_names.append(name)
        return frame

    def _add_panel_resize_handle(self, name, parent, target, min_size, max_size,
                                 side="top", padx=16, pady=(0, 4)):
        handle = tk.Frame(
            parent, height=9, bg=PURPLE_DARK, cursor="sb_v_double_arrow")
        handle.pack_propagate(False)
        handle.pack(side=side, fill="x", padx=padx, pady=pady)
        handle.bind("<Button-1>",
                    lambda event, key=name: self._start_panel_resize(key, event))
        handle.bind("<B1-Motion>",
                    lambda event, key=name: self._drag_panel_resize(key, event))
        handle.bind("<ButtonRelease-1>", self._finish_panel_resize)
        handle.bind("<Enter>", lambda _e, w=handle: w.configure(bg=PURPLE))
        handle.bind("<Leave>", lambda _e, w=handle: w.configure(bg=PURPLE_DARK))
        if not hasattr(self, "_panel_resize_widgets"):
            self._panel_resize_widgets = {}
            self._panel_resize_handles = {}
        self._panel_resize_widgets[name] = target
        self._panel_resize_handles[name] = handle
        self._panel_resize_limits = getattr(self, "_panel_resize_limits", {})
        self._panel_resize_limits[name] = (min_size, max_size)
        return handle

    def _panel_current_size(self, name, widget):
        if name == "tabs":
            try:
                return int(widget.cget("height"))
            except Exception:
                pass
        actual = int(widget.winfo_height())
        if actual > 1:
            return actual
        try:
            return int(widget.winfo_reqheight())
        except Exception:
            return 180

    def _start_panel_resize(self, name, event):
        widget = self._panel_resize_widgets.get(name)
        if widget is None:
            return "break"
        self._begin_resize_animation_pause()
        if name in ("info", "log"):
            widget.pack_propagate(False)
        if name == "log":
            widget.pack_configure(expand=False)
        lo, hi = self._panel_resize_limits[name]
        self._panel_resize_active = {
            "name": name,
            "widget": widget,
            "start_y": event.y_root,
            "start_size": self._panel_current_size(name, widget),
            "min": lo,
            "max": hi,
        }
        return "break"

    def _drag_panel_resize(self, name, event):
        active = self._panel_resize_active
        if active is None or active["name"] != name:
            return "break"
        self._panel_resize_target = max(
            active["min"], min(active["max"],
                               active["start_size"] + event.y_root - active["start_y"]))
        if self._panel_resize_job is None:
            self._panel_resize_job = self.after(25, self._apply_panel_resize)
        return "break"

    def _apply_panel_resize(self):
        self._panel_resize_job = None
        active = self._panel_resize_active
        size = self._panel_resize_target
        if active is None or size is None:
            return
        name, widget = active["name"], active["widget"]
        if name == "tabs":
            widget.configure(height=size)
        else:
            widget.configure(height=size)
        self.panel_sizes[name] = size
        self._panel_resize_target = None

    def _finish_panel_resize(self, _event=None):
        if self._panel_resize_job is not None:
            try:
                self.after_cancel(self._panel_resize_job)
            except Exception:
                pass
            self._panel_resize_job = None
            self._apply_panel_resize()
        active = self._panel_resize_active
        self._panel_resize_active = None
        if active and active["name"] == "scene":
            self.update_idletasks()
            self._resize_scene_to_canvas()
        self._schedule_animation_resume()
        self.schedule_save()
        return "break"

    # ----- widget helpers -----

    def _tab_name(self, parent):
        node = parent
        for _ in range(8):
            if node is None:
                break
            name = self._tab_of.get(id(node))
            if name:
                return name
            node = getattr(node, "master", None)
        return ""

    def _reg_setting(self, parent, name, kind, widget, **kw):
        self._reg.append(dict(name=name, kind=kind, widget=widget,
                              tab=self._tab_name(parent), hint="", **kw))

    def _section(self, parent, text):
        self._last_title = text
        ctk.CTkLabel(
            parent, text=text, font=self.f_section, text_color=PUMPKIN, anchor="w"
        ).pack(fill="x", padx=10, pady=(12, 2))

    def _note(self, parent, text):
        if self._reg and self._reg[-1]["tab"] == self._tab_name(parent):
            self._reg[-1]["hint"] += " " + text
        ctk.CTkLabel(
            parent,
            text=text,
            font=self.f_small,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=470,
        ).pack(fill="x", padx=10, pady=(0, 4))

    def _label(self, parent, text):
        self._last_title = text
        ctk.CTkLabel(
            parent, text=text, font=self.f_label, text_color=TEXT, anchor="w"
        ).pack(fill="x", padx=10, pady=(8, 2))

    def _segmented(self, parent, values, current, command):
        seg = ctk.CTkSegmentedButton(
            parent,
            values=values,
            command=command,
            fg_color=SURFACE2,
            selected_color=PUMPKIN,
            selected_hover_color=PUMPKIN_HOVER,
            unselected_color=SURFACE2,
            unselected_hover_color=PURPLE_DARK,
            text_color=TEXT,
            font=self.f_label,
        )
        seg.pack(fill="x", padx=10, pady=(0, 4))
        seg.set(current)

        def _set(v, seg=seg, command=command):
            seg.set(v)
            command(v)

        self._reg_setting(parent, self._last_title, "choice", seg, get=seg.get, set=_set)
        return seg

    def _menu(self, parent, values, current, command):
        menu = ctk.CTkOptionMenu(
            parent,
            values=list(values),
            command=command,
            fg_color=SURFACE2,
            button_color=PURPLE,
            button_hover_color=PURPLE_DARK,
            text_color=TEXT,
            dropdown_fg_color=SURFACE2,
            dropdown_text_color=TEXT,
            dropdown_hover_color=PURPLE_DARK,
            font=self.f_label,
            dropdown_font=self.f_label,
        )
        menu.pack(fill="x", padx=10, pady=(0, 4))
        menu.set(current)

        def _set(v, menu=menu, command=command):
            menu.set(v)
            command(v)

        self._reg_setting(parent, self._last_title, "choice", menu, get=menu.get, set=_set)
        return menu

    def _switch(self, parent, text, current, command):
        var = ctk.BooleanVar(value=current)
        sw = ctk.CTkSwitch(
            parent,
            text=text,
            variable=var,
            command=lambda: command(bool(var.get())),
            progress_color=PUMPKIN,
            button_color=TEXT,
            button_hover_color="#ffffff",
            fg_color=SURFACE2,
            text_color=TEXT,
            font=self.f_label,
        )
        sw.pack(anchor="w", padx=10, pady=(8, 4))

        def _set(v, sw=sw, command=command):
            if v:
                sw.select()
            else:
                sw.deselect()
            command(bool(v))

        self._reg_setting(parent, text, "switch", sw,
                       get=lambda: bool(var.get()), set=_set)
        return sw

    def _slider(self, parent, label, lo, hi, steps, initial, fmt, on_change):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="x", padx=10, pady=(6, 2))
        box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            box, text=label, font=self.f_label, text_color=TEXT, anchor="w"
        ).grid(row=0, column=0, sticky="w")
        value_lbl = ctk.CTkLabel(
            box, text=fmt(initial), font=self.f_value, text_color=PUMPKIN
        )
        value_lbl.grid(row=0, column=1, sticky="e")

        def changed(value):
            value_lbl.configure(text=fmt(value))
            on_change(value)
            self.schedule_save()

        slider = ctk.CTkSlider(
            box,
            from_=lo,
            to=hi,
            number_of_steps=steps,
            command=changed,
            button_color=PUMPKIN,
            button_hover_color=PUMPKIN_HOVER,
            progress_color=PUMPKIN,
            fg_color=SURFACE2,
        )
        slider.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        slider.set(initial)

        def _set(v, slider=slider, changed=changed):
            slider.set(v)
            changed(v)

        self._reg_setting(parent, label, "slider", slider, get=slider.get, set=_set,
                       lo=lo, hi=hi, steps=steps, fmt=fmt)
        return slider

    def _button(self, parent, text, command, **kw):
        defaults = dict(
            fg_color=PURPLE,
            hover_color=PURPLE_DARK,
            text_color=TEXT,
            font=self.f_label,
            height=32,
            corner_radius=10,
        )
        defaults.update(kw)
        return ctk.CTkButton(parent, text=text, command=command, **defaults)

    # ----- tabs -----

    def _build_play_tab(self, tab):
        self._section(tab, "Who are you playing as?")
        self._segmented(
            tab,
            ["Auto", "White", "Black"],
            self.s_color,
            lambda v: self._set("s_color", v),
        )
        self._note(
            tab,
            "Auto reads which side is at the bottom of the board, so it keeps"
            " working when a new bot gives you a new color.",
        )

        if ALLOW_TEST_MODE:
            self._section(tab, "Online play")
            self.test_switch = self._switch(
                tab,
                "Online play",
                False,
                self.on_test_mode_toggle,
            )
        self._note(
            tab,
            "Use with caution (im not responsible for any bans)",
        )

        self._section(tab, "\u26a1 Auto-move")
        self.auto_switch = self._switch(
            tab, "Play the suggested move for me", self.s_auto_move,
            self.on_auto_toggle,
        )
        self._slider(
            tab, "Auto-move pace (randomised a little)", 0.3, 4.0, 37,
            self.auto_delay,
            lambda v: f"{float(v):.1f} s",
            lambda v: setattr(self, "auto_delay", round(float(v), 1)),
        )
        self._note(
            tab,
            "Clicks the suggested move on the board for you. Always OFF when the"
            " app starts. The Auto-move key toggles it; the Play-once key plays"
            " just the current suggestion. Auto-move never runs while the panic"
            " key has everything hidden.",
        )

        self._section(tab, "Stealth (panic key)")
        self._switch(
            tab, "Stay silent while hidden", self.s_silent_hidden,
            lambda v: self._set("s_silent_hidden", bool(v)),
        )
        self._note(
            tab,
            "Press the Hide key (F9 by default): the dashboard vanishes - no"
            " taskbar icon, not in Alt+Tab - and the arrows clear. While hidden,"
            " tap the Reveal key to step through suggested arrows one at a time."
            " The voice and ambience stop and auto-move pauses. Press the key"
            " again to bring it all back. Start with pythonw so there is no"
            " console window.",
        )

        self._section(tab, "Browser")
        self._switch(
            tab, "Never open Chrome myself - use the one already running",
            self.s_attach_only, lambda v: self._set("s_attach_only", bool(v)),
        )
        self._button(tab, "Make a Chrome launcher file", self.make_chrome_launcher,
                     width=210).pack(anchor="w", padx=10, pady=(6, 2))
        self._note(
            tab,
            "Chrome can only be used by this app if it was started with a"
            " debugging port. Press the button once: it saves 'Start Chrome for"
            " Haunted Stockfish.bat' next to the app. Open Chrome with that file,"
            " leave it open, then press Awaken. The assistant then never starts"
            " a browser of its own. Turn the switch off to let it launch Chrome"
            " for you.",
        )

        self._section(tab, "Window")
        self._switch(tab, "Keep this window on top", self.s_topmost, self.on_topmost_toggle)
        self.fs_switch = self._switch(
            tab, "Fullscreen cockpit (big board, history, stats)", False,
            self.set_fullscreen,
        )
        self._slider(
            tab, "Window opacity", 0.4, 1.0, 12, self.win_alpha,
            lambda v: f"{int(round(float(v) * 100))}%",
            lambda v: self._apply_alpha(float(v)),
        )

        self._section(tab, "How to use")
        self._note(
            tab,
            "1. Open Chrome with the launcher file (Play tab > Browser), then"
            " Awaken the assistant - it attaches to that Chrome.\n"
            "2. Open a bot game on chess.com and start playing.\n"
            "3. On your turn it speaks the move and marks it on the board.",
        )

    def _build_engine_tab(self, tab):
        self._section(tab, "Thinking")
        self._slider(
            tab, "Think time", 0.1, 5.0, 49, self.s_time,
            lambda v: f"{float(v):.1f} s",
            lambda v: setattr(self, "s_time", round(float(v), 1)),
        )
        self._slider(
            tab, "Max depth", 0, 40, 40, self.s_depth,
            lambda v: "Unlimited" if int(round(float(v))) == 0 else f"{int(round(float(v)))}",
            lambda v: setattr(self, "s_depth", int(round(float(v)))),
        )

        self._section(tab, "Telemetry")
        self._switch(
            tab, "Grade the opponent's moves in the log", self.s_quality,
            self.on_quality_toggle,
        )
        self._note(
            tab,
            "Depth, NPS and hash usage show under the fear meter. Grading runs a"
            " short extra analysis after each of your suggestions (Brilliant /"
            " Best / Good / Inaccuracy / Mistake / Blunder).",
        )

        self._section(tab, "Voice callouts")
        self.qvoice_switch = self._switch(
            tab, "Say out loud how good their last move was",
            self.s_quality_voice, self.on_qvoice_toggle,
        )
        self._switch(
            tab, "Only speak for brilliant moves, mistakes and blunders",
            self.s_quality_big_only, self.on_qbig_toggle,
        )
        self._note(
            tab,
            "After each opponent move you hear a short verdict. Press the Explain"
            " key (F2) and it tells you WHY: what they hung, what they missed, or"
            " what made the move strong.",
        )

        self._section(tab, "Strength (when showing 1 move)")
        self._switch(
            tab,
            "Limit strength by Elo rating",
            self.s_use_elo,
            self.on_elo_toggle,
        )
        self.elo_slider = self._slider(
            tab, "Target Elo", 1320, 3000, 168, self.s_elo,
            lambda v: f"{int(round(float(v)))}",
            lambda v: setattr(self, "s_elo", int(round(float(v)))),
        )
        self.skill_slider = self._slider(
            tab, "Skill level", 0, 20, 20, self.s_skill,
            lambda v: f"{int(round(float(v)))}",
            lambda v: setattr(self, "s_skill", int(round(float(v)))),
        )
        self._apply_strength_widgets()
        self._note(
            tab,
            "When 2 or 3 moves are shown the engine uses full strength.",
        )

    def _build_board_tab(self, tab):
        self._section(tab, "On the board")
        self._switch(
            tab, "Show the recommended moves", self.s_overlay, self.on_overlay_toggle
        )

        self._label(tab, "Moves to show")
        self.lines_seg = self._segmented(
            tab, ["1", "2", "3"], str(self.s_lines), self.on_lines_change
        )
        ctk.CTkLabel(
            tab,
            text="Green = best     Yellow = 2nd     Red = 3rd",
            font=self.f_small,
            text_color=MUTED,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 4))

        self._label(tab, "Style")
        self._segmented(
            tab, ["Arrows", "Squares"], self.s_style, self.on_style_change
        )

        self._section(tab, "When do they appear?")
        self._segmented(tab, REVEAL_MODES, self.reveal_mode, self.on_reveal_mode_change)
        self._note(
            tab,
            "Instant: right away.   Delayed: after the wait below.   Step by"
            " step: tap your Reveal key for each arrow (see Keys tab).",
        )
        self._slider(
            tab, "Delay before arrows (Delayed mode)", 0.2, 3.0, 28, self.arrow_delay,
            lambda v: f"{float(v):.1f} s",
            lambda v: setattr(self, "arrow_delay", round(float(v), 1)),
        )
        self._slider(
            tab, "Arrow opacity", 0.3, 1.0, 14, self.arrow_opacity,
            lambda v: f"{int(round(float(v) * 100))}%",
            lambda v: setattr(self, "arrow_opacity", round(float(v), 2)),
        )

    def _build_keys_tab(self, tab):
        self._section(tab, "Shortcuts")
        self.keys_status = ctk.CTkLabel(
            tab, text="", font=self.f_small, anchor="w", justify="left", wraplength=470
        )
        self.keys_status.pack(fill="x", padx=10, pady=(0, 4))

        row1 = ctk.CTkFrame(tab, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=4)
        row1.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            row1, text="Repeat the last move", font=self.f_label, text_color=TEXT
        ).grid(row=0, column=0, sticky="w")
        self.hotkey_btn = self._button(
            row1, self.hotkey_label(), lambda: self.begin_rebind("repeat"), width=150
        )
        self.hotkey_btn.configure(fg_color=SURFACE2)
        self.hotkey_btn.grid(row=0, column=1, sticky="e")

        row2 = ctk.CTkFrame(tab, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=4)
        row2.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            row2, text="Reveal arrows (Step by step)", font=self.f_label, text_color=TEXT
        ).grid(row=0, column=0, sticky="w")
        self.reveal_btn = self._button(
            row2, self.reveal_label(), lambda: self.begin_rebind("reveal"), width=150
        )
        self.reveal_btn.configure(fg_color=SURFACE2)
        self.reveal_btn.grid(row=0, column=1, sticky="e")

        row3 = ctk.CTkFrame(tab, fg_color="transparent")
        row3.pack(fill="x", padx=10, pady=4)
        row3.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            row3, text="Hide / show (minimize + clear arrows)", font=self.f_label,
            text_color=TEXT,
        ).grid(row=0, column=0, sticky="w")
        self.hide_btn = self._button(
            row3, self.hide_key.upper(), lambda: self.begin_rebind("hide"), width=150
        )
        self.hide_btn.configure(fg_color=SURFACE2)
        self.hide_btn.grid(row=0, column=1, sticky="e")

        for action, (label, _default) in EXTRA_ACTIONS.items():
            row = ctk.CTkFrame(tab, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=4)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row, text=label, font=self.f_label, text_color=TEXT
            ).grid(row=0, column=0, sticky="w")
            btn = self._button(
                row, self.extra_keys[action].upper(),
                lambda a=action: self.begin_rebind("x:" + a), width=150,
            )
            btn.configure(fg_color=SURFACE2)
            btn.grid(row=0, column=1, sticky="e")
            self.extra_btns[action] = btn

        self._note(
            tab,
            "Click a key button, then press the key you want (Esc cancels)."
            " While the dashboard is hidden (panic key) the Reveal key always works"
            " this way. Otherwise it only does something when Board > When do they"
            " appear? is set to Step by step. Tap = next arrow, hold = hide all.",
        )
        self._slider(
            tab, "Hold-to-hide time", 0.3, 1.5, 12, self.hold_to_hide,
            lambda v: f"{float(v):.1f} s",
            lambda v: setattr(self, "hold_to_hide", round(float(v), 1)),
        )
        self._button(tab, "Test my keys", self.test_keys, width=150).pack(
            anchor="w", padx=10, pady=(8, 2)
        )
        self._note(
            tab,
            "Test my keys: for 8 seconds, every key the listener hears is"
            " shown in the log - so you can see if it hears Delete and Q.",
        )

        self._section(tab, "Voice")
        self._slider(
            tab, "Speed", -5, 8, 13, self.voice_rate,
            lambda v: f"{int(round(float(v))):+d}",
            lambda v: setattr(self, "voice_rate", int(round(float(v)))),
        )
        self._slider(
            tab, "Volume", 0, 100, 20, self.voice_volume,
            lambda v: f"{int(round(float(v)))}%",
            lambda v: setattr(self, "voice_volume", int(round(float(v)))),
        )
        self.mute_switch = self._switch(
            tab, "Mute the voice", self.s_muted, self.on_mute_toggle
        )
        self._button(tab, "Test the voice", self.test_voice, width=150).pack(
            anchor="w", padx=10, pady=(8, 8)
        )

    def _build_spooky_tab(self, tab):
        self._section(tab, "Theme")
        self.theme_menu = self._menu(
            tab, list(THEMES), self.s_theme, self.on_theme_change
        )
        self._note(
            tab,
            "Colours for the whole app. Applies the next time you start it (the"
            " banner scene stays haunted).",
        )

        self._section(tab, "Haunted scene")
        self._switch(tab, "Animate the haunted scene", self.s_scene_on, self.on_scene_toggle)
        self._slider(
            tab, "Spookiness", 1, 10, 9, self.s_scene_level,
            lambda v: f"{int(round(float(v)))}",
            lambda v: self.on_scene_level(int(round(float(v)))),
        )
        self._switch(
            tab, "Sparks follow your cursor over the scene", self.s_sparks,
            self.on_sparks_toggle,
        )
        self._switch(
            tab, "Skull-and-crossbones cursor", self.s_skull_cursor,
            self.on_skull_cursor_toggle,
        )
        self._switch(
            tab, "Depth: scene layers slide with your mouse", self.s_parallax,
            self.on_parallax_toggle,
        )
        self._switch(
            tab, "Drifting motes in the window background", self.s_backdrop,
            self.on_backdrop_toggle,
        )
        self._section(tab, "Window background")
        self._label(tab, "Background particles")
        self.bg_style_menu = self._menu(tab, BG_STYLES, self.bg_style, self.on_bg_style)
        self._label(tab, "Background colours")
        self.bg_palette_menu = self._menu(
            tab, list(BG_PALETTES), self.bg_palette, self.on_bg_palette)
        self._slider(
            tab, "How many particles", 0, 120, 24, self.bg_amount,
            lambda v: f"{int(round(float(v)))}",
            lambda v: self.on_bg_amount(int(round(float(v)))),
        )
        self._slider(
            tab, "Particle speed", 0.2, 3.0, 28, self.bg_speed,
            lambda v: f"{float(v):.1f}x",
            lambda v: self.on_bg_speed(round(float(v), 1)),
        )
        irow = ctk.CTkFrame(tab, fg_color="transparent")
        irow.pack(fill="x", padx=10, pady=(8, 2))
        self._button(irow, "Choose picture...", self.choose_bg_image, width=140).pack(
            side="left")
        self._button(irow, "Remove", self.clear_bg_image, width=80).pack(
            side="left", padx=6)
        self._button(irow, "Surprise me", self.surprise_background, width=110).pack(
            side="left")
        self.bg_image_lbl = ctk.CTkLabel(
            tab, text=(os.path.basename(self.bg_image) if self.bg_image
                       else "No picture - using the colour gradient"),
            font=self.f_small, text_color=MUTED, anchor="w")
        self.bg_image_lbl.pack(fill="x", padx=10, pady=(0, 2))
        self._slider(
            tab, "Picture darkness", 0, 4, 4, self.bg_dim,
            lambda v: ("None", "Light", "Medium", "Dark", "Very dark")[int(round(float(v)))],
            lambda v: self.on_bg_dim(int(round(float(v)))),
        )
        self._note(
            tab,
            "Use any PNG or GIF as the window wallpaper (JPG is not supported by"
            " this version of Tk - convert it to PNG first). The picture is"
            " darkened so the text stays readable. 'Surprise me' picks a random"
            " colour set and particle style.",
        )
        self._note(
            tab,
            "Click the scene for a surprise. More spookiness = more bats, ghosts,"
            " wisps and lightning. Turn the scene off if you want the lightest"
            " possible app.",
        )

        self._section(tab, "Voice persona")
        self.persona_seg = self._segmented(
            tab, list(PERSONAS), self.persona, self.on_persona_change
        )
        self._switch(
            tab, "Neural voice (edge-tts, free, needs internet)", self.s_neural,
            self.on_neural_toggle,
        )
        self._label(tab, "Which neural voice")
        self.voice_menu = self._menu(
            tab, EDGE_VOICE_CHOICES, self.neural_voice, self.on_neural_voice
        )
        self._button(tab, "Hear the persona", self.test_voice, width=150).pack(
            anchor="w", padx=10, pady=(6, 2)
        )
        self._note(
            tab,
            "Personas change the voice (pitch, speed, and male/female if Windows"
            " has a matching voice) and add commentary on checks, captures,"
            " castling, promotions and checkmate.",
        )

        self._section(tab, "Ambience")
        self._switch(
            tab, "Haunted wind and creaking wood (Windows)", self.s_ambience,
            self.on_ambience_toggle,
        )

        self._section(tab, "Fear meter")
        self._note(
            tab,
            "The bar under the scene shows how scared you should be, from the"
            " engine's evaluation of your position. As fear rises the sky turns"
            " red, lightning strikes more often, and the bats fly faster. Past"
            " 'Terrified' it starts to rain.",
        )

    def on_parallax_toggle(self, value):
        self.s_parallax = bool(value)
        self.scene.set_parallax(value)
        self.schedule_save()

    def _bg_opts(self):
        return dict(style=self.bg_style, amount=self.bg_amount, speed=self.bg_speed,
                    palette=self.bg_palette, image=self.bg_image, dim=self.bg_dim)

    def _apply_backdrop(self):
        try:
            self.backdrop.configure(**self._bg_opts())
            if self.backdrop.image_error:
                self.log("[ERROR] Background picture couldn't be loaded: "
                         + self.backdrop.image_error)
                self.bg_image = ""
                self.bg_image_lbl.configure(text="No picture - using the colour gradient")
                self.backdrop.configure(image="")
        except Exception as err:
            self.log(f"[ERROR] Couldn't change the background: {err}")
        self.schedule_save()

    def on_bg_style(self, value):
        self.bg_style = value
        self._apply_backdrop()

    def on_bg_palette(self, value):
        self.bg_palette = value
        self._apply_backdrop()

    def on_bg_amount(self, value):
        self.bg_amount = int(value)
        self._apply_backdrop()

    def on_bg_speed(self, value):
        self.bg_speed = float(value)
        self.backdrop.configure(speed=self.bg_speed)   # no rebuild needed
        self.schedule_save()

    def on_bg_dim(self, value):
        self.bg_dim = int(value)
        self._apply_backdrop()

    def choose_bg_image(self):
        try:
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title="Choose a background picture",
                filetypes=[("PNG or GIF pictures", "*.png *.gif"), ("All files", "*.*")],
            )
        except Exception as err:
            self.log(f"[ERROR] Couldn't open the file picker: {err}")
            return
        if not path:
            return
        try:
            probe = tk.PhotoImage(master=self, file=path)
            del probe
        except Exception:
            self.log("[ERROR] That picture can't be used. Pick a PNG or GIF file.")
            self.toast("Use a PNG or GIF", "#ff6b6b")
            return
        self.bg_image = path
        self.bg_image_lbl.configure(text=os.path.basename(path))
        self._apply_backdrop()

    def clear_bg_image(self):
        self.bg_image = ""
        self.bg_image_lbl.configure(text="No picture - using the colour gradient")
        self._apply_backdrop()

    def surprise_background(self):
        self.bg_palette = random.choice(list(BG_PALETTES))
        self.bg_style = random.choice([x for x in BG_STYLES if x != "Off"])
        self.bg_palette_menu.set(self.bg_palette)
        self.bg_style_menu.set(self.bg_style)
        self._apply_backdrop()
        self.toast(f"{self.bg_palette} / {self.bg_style}", "#c79bff")

    def on_backdrop_toggle(self, value):
        self.s_backdrop = bool(value)
        self.backdrop.set_enabled(value)
        self.schedule_save()

    def on_topmost_toggle(self, value):
        self.s_topmost = bool(value)
        try:
            self.attributes("-topmost", self.s_topmost)
        except tk.TclError:
            pass
        self.schedule_save()

    def on_mute_toggle(self, value):
        self.s_muted = bool(value)
        if value:
            self.speaker.cancel_current()
        self._refresh_chips()
        self.log(f"[INFO] Voice {'muted' if value else 'unmuted'}.")

    def on_scene_toggle(self, value):
        self.s_scene_on = value
        self.scene.set_enabled(value)
        self.schedule_save()

    def on_scene_level(self, level):
        self.s_scene_level = level
        self.scene.set_level(level)
        self.schedule_save()

    def on_sparks_toggle(self, value):
        self.s_sparks = value
        self.scene.sparks = value
        self.schedule_save()

    def on_skull_cursor_toggle(self, value):
        self.s_skull_cursor = value
        self._apply_cursor()
        self.schedule_save()

    def _apply_cursor(self):
        name = "pirate" if self.s_skull_cursor else ""
        try:
            tk.Misc.configure(self, cursor=name)
            self.scene_canvas.configure(cursor=name)
        except tk.TclError:
            # This system has no skull cursor; use the normal one.
            try:
                tk.Misc.configure(self, cursor="")
                self.scene_canvas.configure(cursor="")
            except tk.TclError:
                pass

    def on_persona_change(self, value):
        self.persona = value
        self.schedule_save()
        self.log(f"[INFO] Voice persona: {value}.")

    def on_neural_toggle(self, value):
        self.s_neural = bool(value)
        self.schedule_save()
        if value and edge_tts is None:
            self.log(
                "[INFO] edge-tts isn't installed, so the Windows voice is used. Run: "
                f"{sys.executable} -m pip install edge-tts"
            )
        else:
            self.log(f"[INFO] Neural voice {'on' if value else 'off'}.")

    def on_quality_toggle(self, value):
        self.s_quality = bool(value)
        self.schedule_save()
        self.log(f"[INFO] Opponent move grading {'on' if value else 'off'}.")

    def on_theme_change(self, value):
        self.s_theme = value
        self.schedule_save()
        self.log(f"[INFO] Theme '{value}' saved - restart the app to see it.")
        self.toast("Restart to apply the theme", "#c79bff")

    def on_neural_voice(self, value):
        self.neural_voice = value
        globals()["EDGE_VOICE_OVERRIDE"] = None if value == "Persona default" else value
        self.schedule_save()
        self.log(f"[INFO] Neural voice: {value}.")

    def voice_mode(self):
        """Which speech engine to use: True (edge-tts) or False (Windows)."""
        return bool(self.s_neural and edge_tts is not None)

    def set_compact(self, value):
        """Compact view: just the banner, status card and buttons."""
        self.s_compact = bool(value)
        try:
            if self.s_compact:
                self.tabs.pack_forget()
                self._panel_resize_handles["tabs"].pack_forget()
                self.log_frame.pack_forget()
                if not self._fs:
                    min_w, _min_h = self.minsize()
                    self.geometry(f"{max(min_w, self.winfo_width())}x{self._compact_h}")
            else:
                self.tabs.pack(fill="x", padx=16, pady=4, before=self.btn_frame)
                self._panel_resize_handles["tabs"].pack(
                    fill="x", padx=16, pady=(0, 4), before=self.btn_frame)
                if "log" in self.panel_sizes:
                    self.log_frame.configure(height=self.panel_sizes["log"])
                    self.log_frame.pack_propagate(False)
                    self.log_frame.pack(fill="both", expand=False,
                                        padx=16, pady=(4, 14))
                else:
                    self.log_frame.pack(fill="both", expand=True,
                                        padx=16, pady=(4, 14))
                if not self._fs:
                    min_w, _min_h = self.minsize()
                    self.geometry(f"{max(min_w, self.winfo_width())}x{self._win_h}")
        except tk.TclError:
            pass
        self._refresh_chips()

    def announce_game_over(self, board, user_turn):
        won = None
        if board.is_checkmate():
            won = board.turn != user_turn
            line = "Checkmate. You won!" if won else "Checkmate. You were mated."
        elif board.is_stalemate():
            line = "Stalemate. It's a draw."
        elif board.is_insufficient_material():
            line = "Draw by insufficient material."
        else:
            line = "The game is over."
        spoken = line
        if self.match_total:
            pct = round(100 * self.match_hits / self.match_total)
            spoken += f" You matched the engine on {pct} percent of your moves."
        self.log(f"[SUCCESS] {line}")
        self.add_grimoire(f"---- {line} ----", "good" if won else "dim")
        self.toast(line, SLIME if won else (CANDLE if won is None else "#ff4d4d"))
        self.say(spoken, interrupt=False)
        if won:
            self.post(self.scene._on_click, _Pt(self.scene.W / 2, self.scene.H / 2))

    def copy_pgn(self):
        if not self.game_pgn:
            self.log("[INFO] No game has been recorded yet.")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self.game_pgn)
            self.update()
            self.log("[SUCCESS] Game copied as PGN - paste it into chess.com or Lichess analysis.")
            self.toast("PGN copied", SLIME)
        except tk.TclError as e:
            self.log(f"[ERROR] Couldn't copy: {e}")

    def save_pgn(self):
        if not self.game_pgn:
            self.log("[INFO] No game has been recorded yet.")
            return
        try:
            folder = os.path.join(DATA_DIR, "games")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, time.strftime("game_%Y%m%d_%H%M%S.pgn"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.game_pgn + "\n")
            self.log(f"[SUCCESS] Saved {path}")
            self.toast("Game saved", SLIME)
        except OSError as e:
            self.log(f"[ERROR] Couldn't save: {e}")

    def on_qvoice_toggle(self, value):
        self.s_quality_voice = bool(value)
        self.schedule_save()
        self._refresh_chips()
        self.log(f"[INFO] Spoken move callouts {'on' if value else 'off'}.")

    def on_qbig_toggle(self, value):
        self.s_quality_big_only = bool(value)
        self.schedule_save()

    def on_auto_toggle(self, value):
        self.s_auto_move = bool(value)
        self._wake.set()
        self._refresh_chips()
        self.log(f"[AUTO] Auto-move {'ON' if value else 'off'}.")

    def _apply_alpha(self, value):
        self.win_alpha = round(max(0.4, min(1.0, value)), 2)
        try:
            self.attributes("-alpha", self.win_alpha)
        except tk.TclError:
            pass

    def _refresh_chips(self):
        if not hasattr(self, "chip_auto"):
            return

        def style(chip, on, text):
            chip.configure(
                text=text,
                fg_color=PUMPKIN if on else SURFACE2,
                hover_color=PUMPKIN_HOVER if on else PURPLE_DARK,
                text_color="#1a0d00" if on else MUTED,
                border_color=PUMPKIN if on else PURPLE_DARK,
            )

        style(self.chip_auto, self.s_auto_move,
              "\u26a1 AUTO-MOVE ON" if self.s_auto_move else "Auto-move off")
        style(self.chip_voice, self.s_quality_voice,
              "CALLOUTS ON" if self.s_quality_voice else "Callouts off")
        style(self.chip_sound, not self.s_muted,
              "SOUND ON" if not self.s_muted else "MUTED")
        style(self.chip_view, self.s_compact,
              "COMPACT" if self.s_compact else "Full view")

    def _show_grade(self, label, san, text):
        self._show_grade_main(label, san, text)
        try:
            if label:
                self.cp_grade.configure(
                    text=f"Opponent played {san} - {label.upper()}\n{text}",
                    text_color=GRADE_COLORS.get(label, TEXT))
            else:
                self.cp_grade.configure(
                    text="Opponent's last move: waiting...", text_color=MUTED)
        except Exception:
            pass

    def _show_grade_main(self, label, san, text):
        """Show the opponent's last graded move and the reason, in the card."""
        if not label:
            self.grade_title.configure(
                text="Opponent's last move: waiting...", text_color=MUTED)
            self.grade_body.configure(text="")
            self.grade_card.configure(border_color=PURPLE_DARK)
            return
        color = GRADE_COLORS.get(label, TEXT)
        self.grade_title.configure(
            text=f"Opponent played {san}  \u2014  {label.upper()}", text_color=color)
        short = text if len(text) <= 230 else text[:227].rsplit(" ", 1)[0] + "..."
        self.grade_body.configure(text=short)
        self.grade_card.configure(border_color=color)

    def on_ambience_toggle(self, value, announce=True):
        self.s_ambience = bool(value)
        self.schedule_save()
        if not self.ambience.available:
            if announce:
                self.log("[INFO] Ambience needs Windows (winsound).")
            return
        if value:
            threading.Thread(target=self._start_ambience, daemon=True).start()
        else:
            self.ambience.stop()

    def _start_ambience(self):
        if not self.ambience.start():
            self.log("[INFO] Couldn't start the ambience.")

    def set_fear(self, fear):
        self.post(self._set_fear_now, float(fear))

    def _build_grimoire_tab(self, tab):
        self._section(tab, "Grimoire")
        self._note(
            tab,
            "A running record: each move suggested (with your evaluation) and how"
            " each opponent reply was graded.",
        )
        self.grim_box = ctk.CTkTextbox(
            tab, height=190, font=("Consolas", 11), fg_color="#0b0710",
            text_color=TEXT, border_width=1, border_color=PURPLE_DARK, corner_radius=10,
        )
        self.grim_box.pack(fill="x", padx=10, pady=(4, 6))
        for tag, color in (("move", PUMPKIN), ("good", SLIME), ("warn", CANDLE),
                           ("error", "#ff6b6b"), ("brill", "#4dd8ff"),
                           ("dim", "#6f5d88")):
            self.grim_box.tag_config(tag, foreground=color)
        self.grim_box.configure(state="disabled")
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(0, 8))
        self._button(row, "Clear", self.clear_grimoire, width=90).pack(side="left")
        self._button(row, "Copy game (PGN)", self.copy_pgn, width=140).pack(
            side="left", padx=6
        )
        self._button(row, "Save game (.pgn)", self.save_pgn, width=140).pack(
            side="left"
        )

    def add_grimoire(self, line, tag=None):
        self.post(self._grim_now, line, tag)

    def _grim_now(self, line, tag):
        box = self.grim_box
        box.configure(state="normal")
        if tag:
            box.insert(ctk.END, line + "\n", tag)
        else:
            box.insert(ctk.END, line + "\n")
        box.see(ctk.END)
        box.configure(state="disabled")

    def clear_grimoire(self):
        self.grim_box.configure(state="normal")
        self.grim_box.delete("1.0", ctk.END)
        self.grim_box.configure(state="disabled")
        self.grim_count = 0

    def record_eval(self, cp):
        self.eval_history.append(int(cp))
        self.post(self._draw_eval)
        self.post(self._draw_orb)

    def _draw_eval(self):
        """The 'fate line': your evaluation after each suggestion, newest at right."""
        cv = self.eval_canvas
        cv.delete("all")
        w = max(20, cv.winfo_width())
        h = int(cv.cget("height"))
        mid = h / 2
        cv.create_line(0, mid, w, mid, fill=PURPLE_DARK, dash=(3, 3))
        data = self.eval_history[-40:]
        if len(data) < 2:
            cv.create_text(w / 2, mid, text="Fate line: your evaluation appears here",
                           fill=MUTED, font=self.f_small)
            return
        n = len(data)
        step = (w - 8) / (n - 1)
        pts = []
        for i, cp in enumerate(data):
            v = max(-500, min(500, cp)) / 500.0
            pts += [4 + i * step, mid - v * (mid - 4)]
        last = data[-1]
        col = SLIME if last >= 0 else "#ff4d4d"
        cv.create_polygon([4, mid] + pts + [4 + (n - 1) * step, mid],
                          fill=lerp_color(SURFACE, col, 0.25), outline="")
        cv.create_line(pts, fill=col, width=2, smooth=True)
        x, y = pts[-2], pts[-1]
        cv.create_oval(x - 3, y - 3, x + 3, y + 3, fill=col, outline="")
        cv.create_text(w - 6, 8, text=f"{last / 100:+.1f}", fill=col, anchor="e",
                       font=self.f_small)

    def set_telemetry(self, info):
        self._telemetry_text = telemetry_text(info)
        self.post(self._refresh_telemetry)

    def _refresh_telemetry(self):
        text = self._telemetry_text
        extra = []
        if self.opening_name:
            extra.append(f"Opening: {self.opening_name}")
        if self.match_total:
            pct = round(100 * self.match_hits / self.match_total)
            extra.append(
                f"Engine match: {pct}% ({self.match_hits}/{self.match_total})"
            )
        if extra:
            text += "\n" + "   |   ".join(extra)
        if self.quality_tally:
            tally = ", ".join(
                f"{self.quality_tally[k]} {k}" for k in QUALITY_ORDER if k in self.quality_tally
            )
            text += "\nOpponent moves: " + tally
        self.telemetry_label.configure(text=text)

    def _set_fear_now(self, fear):
        self.fear_value = fear
        self.fear_bar.set(fear)
        if fear < 0.25:
            label, color = "Calm", SLIME
        elif fear < 0.5:
            label, color = "Uneasy", CANDLE
        elif fear < 0.75:
            label, color = "Terrified", PUMPKIN
        else:
            label, color = "Doomed", "#ff4d4d"
        self.fear_bar.configure(progress_color=color)
        self.fear_label.configure(text=f"Fear: {label}", text_color=color)
        self.scene.set_fear(fear)
        self.info_frame.configure(
            border_color=lerp_color(PURPLE_DARK, color, 0.35 + 0.45 * fear)
        )

    # ------------------------------------------------------------------
    # Setting callbacks (main thread)
    # ------------------------------------------------------------------

    def _set(self, name, value):
        setattr(self, name, value)
        self.schedule_save()

    # ------------------------------------------------------------------
    # v8: crystal ball, presets, find tab
    # ------------------------------------------------------------------

    def _startup_report(self):
        sf = find_stockfish()
        if sf:
            self.log(f"[SUCCESS] Stockfish found: {os.path.basename(sf)}")
        else:
            self.log(f"[ERROR] No stockfish.exe found. Put it in: {APP_DIR}")
        if find_chrome():
            self.log("[INFO] Chrome found.")
        else:
            self.log("[ERROR] Google Chrome was not found. Install it first.")
        if keyboard is None:
            self.log("[ERROR] Shortcut keys are off (keyboard package missing).")

    # ----- crystal ball (mini board) -----

    def _build_orb_tab(self, tab):
        self._section(tab, "Crystal ball")
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(2, 4))
        side_w = 8 * ORB_SQ
        self.orb_canvas = tk.Canvas(
            row, width=side_w + 16, height=side_w, bg=SURFACE,
            highlightthickness=1, highlightbackground=PURPLE_DARK, bd=0)
        self.orb_canvas.pack(side="left")
        side = ctk.CTkFrame(row, fg_color="transparent")
        side.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.orb_caption = ctk.CTkLabel(
            side, text="Waiting for a game...", font=self.f_value,
            text_color=PUMPKIN, anchor="w", justify="left", wraplength=190)
        self.orb_caption.pack(fill="x", padx=4, pady=(2, 4))
        self._switch(side, "Show suggested moves", self.s_orb_moves,
                     self.on_orb_toggle)
        ctk.CTkLabel(
            side, font=self.f_small, text_color=MUTED, anchor="w", justify="left",
            wraplength=190,
            text="Green = best, yellow = 2nd, red = 3rd.\nThe thin bar on the right of"
                 " the board is your evaluation. Gold squares show the last move.",
        ).pack(fill="x", padx=4, pady=(4, 0))
        self._switch(
            tab, "Flag loose pieces (red = yours, green = targets)",
            self.s_orb_threats, self.on_orb_threats,
        )
        self._label(tab, "Board colours")
        self._menu(tab, list(BOARD_STYLES), self.s_board_style, self.on_board_style)
        self._draw_orb()

    def on_board_style(self, value):
        self.s_board_style = value
        self.schedule_save()
        self._draw_orb()

    def on_orb_threats(self, value):
        self.s_orb_threats = bool(value)
        self._draw_orb()

    def on_orb_toggle(self, value):
        self.s_orb_moves = bool(value)
        self._draw_orb()

    def update_orb(self, fen, flipped, highlights):
        last = []
        for h in highlights or []:
            try:
                last.append(chess.square(int(h[0]) - 1, int(h[1]) - 1))
            except Exception:
                pass
        if fen != self._orb["fen"]:
            self._orb["moves"] = []
        self._orb.update(fen=fen, flipped=bool(flipped), last=last)
        self._draw_orb()

    def update_orb_moves(self, moves):
        self._orb["moves"] = list(moves)
        self._draw_orb()

    def _board_colors(self):
        pair = BOARD_STYLES.get(self.s_board_style)
        if pair:
            return pair
        return (lerp_color(SURFACE2, TEXT, 0.18), lerp_color(SURFACE, PURPLE_DARK, 0.7))

    def _paint_board(self, cv, S, ox=0, oy=0, bw=10, gap=8):
        cv.delete("all")
        st = self._orb
        flipped = st["flipped"]
        light, dark = self._board_colors()

        def cr(sq):
            f = chess.square_file(sq)
            r = chess.square_rank(sq)
            return (7 - f if flipped else f), (r if flipped else 7 - r)

        def corner(sq):
            col, row = cr(sq)
            return ox + col * S, oy + row * S

        small = ("Segoe UI", max(7, S // 5))
        for sq in chess.SQUARES:
            x, y = corner(sq)
            f = chess.square_file(sq)
            r = chess.square_rank(sq)
            base = dark if (f + r) % 2 == 0 else light
            if sq in st["last"]:
                base = lerp_color(base, CANDLE, 0.4)
            cv.create_rectangle(x, y, x + S, y + S, fill=base, outline="")
            if S >= 38:
                col, row = cr(sq)
                lab = dark if (f + r) % 2 else light
                if row == 7:
                    cv.create_text(x + S - 3, y + S - 2, text="abcdefgh"[f],
                                   anchor="se", font=small, fill=lab)
                if col == 0:
                    cv.create_text(x + 3, y + 2, text=str(r + 1), anchor="nw",
                                   font=small, fill=lab)

        board = chess.Board(st["fen"]) if st["fen"] else None

        if board is not None and self.s_orb_threats:
            mine = chess.BLACK if flipped else chess.WHITE
            ring = max(2, S // 12)
            try:
                for color, tint in ((mine, "#ff4d4d"), (not mine, SLIME)):
                    for sq, _p in hanging_pieces(board, color):
                        x, y = corner(sq)
                        cv.create_rectangle(x + 2, y + 2, x + S - 2, y + S - 2,
                                            outline=tint, width=ring)
            except Exception:
                pass

        if board is not None:
            font = ("Segoe UI Symbol", max(12, int(S * 0.57)))
            for sq, piece in board.piece_map().items():
                x, y = corner(sq)
                glyph = chess.Piece(piece.piece_type, chess.BLACK).unicode_symbol()
                white = piece.color == chess.WHITE
                fill = "#f7efdc" if white else "#120a1c"
                edge = "#120a1c" if white else "#c7b3e6"
                cx, cy = x + S / 2, y + S / 2 + 1
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    cv.create_text(cx + dx, cy + dy, text=glyph, font=font, fill=edge)
                cv.create_text(cx, cy, text=glyph, font=font, fill=fill)

        colors = ("#81b64c", "#ffaa00", "#eb6150")
        if self.s_orb_moves and st["moves"]:
            for k in range(min(3, len(st["moves"])) - 1, -1, -1):
                try:
                    mv = chess.Move.from_uci(st["moves"][k])
                    x1, y1 = corner(mv.from_square)
                    x2, y2 = corner(mv.to_square)
                    cv.create_line(x1 + S / 2, y1 + S / 2, x2 + S / 2, y2 + S / 2,
                                   fill=colors[k], width=max(2, S // 6 - k),
                                   arrow=tk.LAST,
                                   arrowshape=(S * 0.33, S * 0.4, S * 0.17),
                                   capstyle="round")
                except Exception:
                    pass

        H = 8 * S
        bx = ox + H + gap
        cv.create_rectangle(bx, oy, bx + bw, oy + H, fill=SURFACE2, outline="")
        if self.eval_history:
            cp = max(-500, min(500, self.eval_history[-1]))
            frac = 0.5 + cp / 1000.0
            cv.create_rectangle(bx, oy + H * (1 - frac), bx + bw, oy + H,
                                fill=SLIME if cp >= 0 else "#ff4d4d", outline="")
        cv.create_line(bx, oy + H / 2, bx + bw, oy + H / 2, fill=TEXT)
        return board

    def _orb_caption(self, board):
        st = self._orb
        if board is None:
            caption = "Waiting for a game..."
        elif st["moves"]:
            names = []
            for u in st["moves"][:3]:
                try:
                    names.append(board.san(chess.Move.from_uci(u)))
                except Exception:
                    names.append(u)
            caption = "Suggested: " + "   ".join(names)
        else:
            caption = "Position tracked."
        if self.opening_name:
            caption += "\n" + self.opening_name
        return caption

    def _draw_orb(self):
        cv = getattr(self, "orb_canvas", None)
        if cv is not None:
            try:
                board = self._paint_board(cv, ORB_SQ, 0, 0, 10, 4)
                self.orb_caption.configure(text=self._orb_caption(board))
            except Exception:
                pass
        if getattr(self, "_fs", False):
            self._draw_cockpit()

    # ----- fullscreen cockpit -----

    def _build_cockpit(self):
        cp = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=14,
                          border_width=2, border_color=PURPLE_DARK)
        self.cockpit = cp
        self.cockpit_sash = tk.Frame(
            self, width=10, bg=PURPLE_DARK, cursor="sb_h_double_arrow")
        self.cockpit_sash.pack_propagate(False)
        self.cockpit_sash.bind("<Button-1>", self._start_cockpit_resize)
        self.cockpit_sash.bind("<B1-Motion>", self._drag_cockpit_resize)
        self.cockpit_sash.bind("<ButtonRelease-1>", self._finish_cockpit_resize)
        self.cockpit_sash.bind(
            "<Enter>", lambda _e: self.cockpit_sash.configure(bg=PURPLE))
        self.cockpit_sash.bind(
            "<Leave>", lambda _e: self.cockpit_sash.configure(bg=PURPLE_DARK))
        ctk.CTkLabel(cp, text="Cockpit", font=ctk.CTkFont(family="Chiller", size=30),
                     text_color=PUMPKIN).pack(pady=(8, 0))
        self.cp_stats = ctk.CTkLabel(cp, text="", font=self.f_small, text_color=MUTED,
                                     justify="left", anchor="w", wraplength=700)
        self.cp_stats.pack(side="bottom", fill="x", padx=14, pady=(0, 10))
        self.cp_hist = ctk.CTkTextbox(
            cp, height=110, wrap="word", font=("Consolas", 12), fg_color="#0b0710",
            text_color=TEXT, border_width=1, border_color=PURPLE_DARK, corner_radius=10)
        self.cp_hist.pack(side="bottom", fill="x", padx=14, pady=(0, 8))
        self.cp_hist.configure(state="disabled")
        self.cp_grade = ctk.CTkLabel(
            cp, text="Opponent's last move: waiting...", font=self.f_label,
            text_color=MUTED, justify="left", anchor="w", wraplength=700)
        self.cp_grade.pack(side="bottom", fill="x", padx=14, pady=(0, 6))
        self.cp_caption = ctk.CTkLabel(cp, text="Waiting for a game...",
                                       font=self.f_value, text_color=PUMPKIN, anchor="w")
        self.cp_caption.pack(side="bottom", fill="x", padx=14, pady=(0, 4))
        self.cp_canvas = tk.Canvas(cp, bg=SURFACE, highlightthickness=0, bd=0)
        self.cp_canvas.pack(fill="both", expand=True, padx=8, pady=6)
        self.cp_canvas.bind("<Configure>", self._queue_cockpit_redraw)

    def _draw_cockpit(self):
        cv = getattr(self, "cp_canvas", None)
        if cv is None or not self._fs:
            return
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 80 or h < 80:
                return
            S = max(20, min((w - 40) // 8, (h - 10) // 8))
            bw = max(10, S // 3)
            total = 8 * S + 8 + bw
            ox = max(0, (w - total) // 2)
            oy = max(0, (h - 8 * S) // 2)
            board = self._paint_board(cv, S, ox, oy, bw, 8)
            self.cp_caption.configure(
                text=self._orb_caption(board).replace("\n", "     |     "))
        except Exception:
            pass

    def _on_escape(self, _event=None):
        if self._fs:
            try:
                self.fs_switch.deselect()
            except Exception:
                pass
            self.set_fullscreen(False)

    def _rebuild_backdrop(self, w, h):
        try:
            self.backdrop.stop()
            self.backdrop_canvas.delete("all")
            self.backdrop = Backdrop(self.backdrop_canvas, w, h, **self._bg_opts())
            self.backdrop.set_enabled(self.s_backdrop)
            self._backdrop_paused_for_resize = False
        except Exception:
            pass

    def _resize_scene_to_canvas(self):
        canvas = getattr(self, "scene_canvas", None)
        old = getattr(self, "scene", None)
        if canvas is None or old is None:
            return
        width = max(300, canvas.winfo_width())
        height = max(90, canvas.winfo_height())
        if (width, height) == (old.W, old.H):
            return
        thinking = old.thinking
        old.stop()
        canvas.delete("all")
        self.scene = HauntedScene(self, canvas, width, height)
        self.scene.set_level(self.s_scene_level)
        self.scene.sparks = self.s_sparks
        self.scene.set_parallax(self.s_parallax)
        self.scene.thinking = thinking
        self.scene.set_fear(self.fear_value)
        self.scene.set_enabled(self.s_scene_on)

    def _on_window_configure(self, event):
        if event.widget is not self or self._fs:
            return
        backdrop = getattr(self, "backdrop", None)
        if backdrop is None:
            return
        w, h = self.winfo_width(), self.winfo_height()
        if w < 100 or h < 100 or (w == backdrop.W and h == backdrop.H):
            return
        self._schedule_backdrop_resize()

    def _schedule_backdrop_resize(self):
        if self._fs or not hasattr(self, "backdrop"):
            return
        self._pause_animations_for_resize()
        if self._backdrop_resize_job is not None:
            try:
                self.after_cancel(self._backdrop_resize_job)
            except Exception:
                pass
        self._backdrop_resize_job = self.after(180, self._finish_backdrop_resize)

    def _pause_animations_for_resize(self):
        if hasattr(self, "backdrop") and not self._backdrop_paused_for_resize:
            # Freeze decorative motion while geometry is in flux.
            self.backdrop.stop()
            self._backdrop_paused_for_resize = True
        scene = getattr(self, "scene", None)
        if scene is not None and not self._scene_paused_for_resize:
            scene.stop()
            self._scene_paused_for_resize = True

    def _begin_resize_animation_pause(self):
        if self._backdrop_resize_job is not None:
            try:
                self.after_cancel(self._backdrop_resize_job)
            except Exception:
                pass
            self._backdrop_resize_job = None
        self._pause_animations_for_resize()

    def _schedule_animation_resume(self):
        if self._backdrop_resize_job is not None:
            try:
                self.after_cancel(self._backdrop_resize_job)
            except Exception:
                pass
        self._backdrop_resize_job = self.after(180, self._finish_backdrop_resize)

    def _finish_backdrop_resize(self):
        self._backdrop_resize_job = None
        if not hasattr(self, "backdrop"):
            self._backdrop_paused_for_resize = False
            self._resume_scene_after_resize()
            return
        if self._fs:
            self.backdrop.set_enabled(self.s_backdrop)
            self._backdrop_paused_for_resize = False
            self._resize_scene_to_canvas()
            self._resume_scene_after_resize()
            return
        w, h = self.winfo_width(), self.winfo_height()
        if w > 100 and h > 100 and (w != self.backdrop.W or h != self.backdrop.H):
            self._rebuild_backdrop(w, h)
        else:
            self.backdrop.set_enabled(self.s_backdrop)
            self._backdrop_paused_for_resize = False
        self._resize_scene_to_canvas()
        self._resume_scene_after_resize()

    def _resume_scene_after_resize(self):
        if not self._scene_paused_for_resize:
            return
        self._scene_paused_for_resize = False
        if self.s_scene_on and hasattr(self, "scene"):
            self.scene.start()

    def _set_cockpit_left_width(self, width):
        """Resize the controls/cockpit split while fullscreen."""
        sw = max(800, self.winfo_width())
        min_left = 580
        min_cockpit = 420
        max_left = max(min_left, sw - min_cockpit - 26)
        self.cockpit_left_width = max(min_left, min(max_left, int(width)))
        new_width = max(min_cockpit, sw - self.cockpit_left_width - 26)
        if getattr(self, "_last_cockpit_width", None) != new_width:
            self.cockpit.configure(width=new_width)
            self._last_cockpit_width = new_width

    def _start_cockpit_resize(self, event=None):
        self._dragging_cockpit_sash = True
        self._begin_resize_animation_pause()
        if event is not None:
            self._cockpit_drag_offset = event.x_root - self.cockpit_sash.winfo_rootx()
        return "break"

    def _drag_cockpit_resize(self, event):
        if not getattr(self, "_dragging_cockpit_sash", False):
            return "break"
        offset = getattr(self, "_cockpit_drag_offset", 0)
        self._cockpit_resize_target = event.x_root - offset - self.winfo_rootx()
        if self._cockpit_resize_job is None:
            self._cockpit_resize_job = self.after(35, self._apply_cockpit_resize)
        return "break"

    def _apply_cockpit_resize(self):
        self._cockpit_resize_job = None
        if self._cockpit_resize_target is not None:
            self._set_cockpit_left_width(self._cockpit_resize_target)
            self._cockpit_resize_target = None

    def _queue_cockpit_redraw(self, _event=None):
        if not self._fs or self._cockpit_redraw_job is not None:
            return
        self._cockpit_redraw_job = self.after(45, self._draw_cockpit_after_resize)

    def _draw_cockpit_after_resize(self):
        self._cockpit_redraw_job = None
        self._draw_cockpit()

    def _finish_cockpit_resize(self, _event=None):
        self._dragging_cockpit_sash = False
        if self._cockpit_resize_job is not None:
            try:
                self.after_cancel(self._cockpit_resize_job)
            except Exception:
                pass
            self._cockpit_resize_job = None
            self._apply_cockpit_resize()
        if self._backdrop_resize_job is not None:
            try:
                self.after_cancel(self._backdrop_resize_job)
            except Exception:
                pass
        self.update_idletasks()
        self._resize_scene_to_canvas()
        self._schedule_animation_resume()
        self.schedule_save()
        return "break"

    def set_fullscreen(self, value):
        value = bool(value)
        if value == self._fs:
            return
        self._fs = value
        try:
            if value:
                if self._backdrop_resize_job is not None:
                    try:
                        self.after_cancel(self._backdrop_resize_job)
                    except Exception:
                        pass
                    self._backdrop_resize_job = None
                self._backdrop_paused_for_resize = False
                self._resume_scene_after_resize()
                self._windowed_geometry = self.geometry()
                self.resizable(True, True)
                self.attributes("-fullscreen", True)
                self.update_idletasks()
                sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
                self._set_cockpit_left_width(self.cockpit_left_width)
                self.cockpit.pack_propagate(False)
                self.cockpit.pack(side="right", fill="y", padx=(0, 16), pady=14,
                                  before=self.scene_canvas)
                self.cockpit_sash.pack(side="right", fill="y", pady=14,
                                       before=self.scene_canvas)
                self._rebuild_backdrop(sw, sh)
                self._tick_cockpit()
                key = self.extra_keys.get("fullscreen", "f11").upper()
                self.log(f"[INFO] Fullscreen cockpit on ({key} or Esc to leave).")
            else:
                self.cockpit.pack_forget()
                self.cockpit_sash.pack_forget()
                self.attributes("-fullscreen", False)
                self.resizable(True, True)
                h = self._compact_h if self.s_compact else self._win_h
                self.geometry(self._windowed_geometry or f"580x{h}")
                self.after_idle(self._schedule_backdrop_resize)
                self._tick_cockpit()
                self.log("[INFO] Fullscreen cockpit off.")
        except tk.TclError:
            pass
        self.after(200, self._draw_cockpit)
        if value:
            self.after(220, self._resize_scene_to_canvas)

    def _stats_text(self):
        t = int(time.time() - self._session_start)
        line = (f"Session {t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}    "
                f"Games {self.games_seen}    Suggestions this game {self.grim_count}")
        if self.match_total:
            pct = round(100 * self.match_hits / self.match_total)
            line += f"\nEngine match {pct}% ({self.match_hits}/{self.match_total})"
        if self.quality_tally:
            line += "\nOpponent moves: " + ", ".join(
                f"{self.quality_tally[k]} {k}" for k in QUALITY_ORDER
                if k in self.quality_tally)
        return line

    def _tick_cockpit(self):
        if self._cp_job is not None:
            try:
                self.after_cancel(self._cp_job)
            except Exception:
                pass
            self._cp_job = None
        if not self._fs:
            return
        try:
            self.cp_stats.configure(text=self._stats_text())
        except Exception:
            pass
        self._cp_job = self.after(1000, self._tick_cockpit)

    def update_history(self, sans):
        try:
            parts = []
            for i in range(0, len(sans), 2):
                parts.append(f"{i // 2 + 1}. " + " ".join(sans[i:i + 2]))
            box = self.cp_hist
            box.configure(state="normal")
            box.delete("1.0", ctk.END)
            box.insert("1.0", "   ".join(parts) if parts else "No moves yet.")
            box.see(ctk.END)
            box.configure(state="disabled")
        except Exception:
            pass

    def make_chrome_launcher(self):
        """Write a .bat that starts Chrome with the debugging port, so you can
        open your own Chrome yourself and the app only attaches to it."""
        chrome = find_chrome()
        if not chrome:
            self.log("[ERROR] Could not find chrome.exe, so no launcher was made.")
            return
        profile_dir = os.path.join(DATA_DIR, "chrome-debug-profile")
        path = os.path.join(DATA_DIR, "Start Chrome for Haunted Stockfish.bat")
        text = (
            "@echo off\r\n"
            f'start "" "{chrome}" --remote-debugging-port={DEBUG_PORT} '
            f'--user-data-dir="{profile_dir}" "{START_URL}"\r\n'
        )
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except OSError as err:
            self.log(f"[ERROR] Couldn't write the launcher: {err}")
            return
        self.log(f"[SUCCESS] Launcher saved: {path}")
        self.log(
            "[INFO] Open Chrome with that file, sign in to chess.com once (it"
            " keeps its own profile), leave it open, then press Awaken."
        )
        self.toast("Launcher saved", SLIME)

    def open_games_folder(self):
        folder = os.path.join(DATA_DIR, "games")
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
        except AttributeError:
            self.log(f"[INFO] Saved games live in: {folder}")
        except OSError as e:
            self.log(f"[ERROR] Couldn't open the folder: {e}")

    # ----- presets -----

    def apply_preset(self, name):
        wanted = PRESETS.get(name) or {}
        by_name = {e["name"]: e for e in self._reg}
        done = 0
        for label, value in wanted.items():
            e = by_name.get(label)
            if e is None:
                continue
            try:
                e["set"](value)
                done += 1
            except Exception as err:
                self.log(f"[DEBUG] Preset skipped '{label}': {err}")
        self.log(f"[INFO] Preset '{name}' applied ({done} settings).")
        self.toast(f"Preset: {name}", PUMPKIN)
        self._refresh_search()

    # ----- find tab (search every setting) -----

    def _build_search_tab(self, tab):
        self._section(tab, "Find a setting")
        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(
            tab, textvariable=self.search_var, height=34, font=self.f_label,
            placeholder_text="Try: voice, arrows, panic, theme, speed...",
            fg_color=SURFACE2, border_color=PURPLE, text_color=TEXT)
        self.search_entry.pack(fill="x", padx=10, pady=(4, 6))
        self.search_var.trace_add("write", lambda *_: self._refresh_search())
        self.search_entry.bind("<Return>", lambda _e: self._search_enter())
        self.search_results = ctk.CTkFrame(tab, fg_color="transparent")
        self.search_results.pack(fill="x", padx=2)
        self._note(
            tab,
            "Ctrl+F jumps here from anywhere. Switches and sliders work right in"
            " the results. Enter uses the top result. You can also run actions"
            " (panic, copy PGN, test voices) and apply presets from here.",
        )
        self._refresh_search()

    def _goto_tab(self, name):
        if self.s_compact:
            self.set_compact(False)
        try:
            self.tabs.set(name)
        except Exception:
            pass

    def focus_search(self):
        if self.hidden:
            return
        self._goto_tab(self._find_tab)
        self.after(60, lambda: self.search_entry.focus_set())

    def _pick_theme(self, name):
        self.theme_menu.set(name)
        self.on_theme_change(name)

    def _actions(self):
        acts = [
            ("Hide / show everything (panic)", "panic stealth hide boss", self.toggle_hidden),
            ("Copy game as PGN", "export clipboard analysis", self.copy_pgn),
            ("Save game as .pgn file", "export file", self.save_pgn),
            ("Test the voice", "speak hear", self.test_voice),
            ("Test my keys", "keyboard hotkey", self.test_keys),
            ("Clear the grimoire", "history record", self.clear_grimoire),
            ("Open the saved games folder", "pgn files folder", self.open_games_folder),
        ]
        for action, (label, _k) in EXTRA_ACTIONS.items():
            acts.append((label, "shortcut key hotkey",
                         lambda a=action: self.run_extra_action(a)))
        for p in PRESETS:
            acts.append((f"Preset: {p}", "profile mode bundle",
                         lambda p=p: self.apply_preset(p)))
        for t in THEMES:
            acts.append((f"Theme: {t}", "colour color skin look",
                         lambda t=t: self._pick_theme(t)))
        for tn in self._tab_names:
            acts.append((f"Go to the {tn[2:]} tab", "open tab",
                         lambda tn=tn: self._goto_tab(tn)))
        return acts

    def _search_items(self):
        items = [dict(name=e["name"], kind=e["kind"], tab=e["tab"],
                      hint=e.get("hint", ""), entry=e) for e in self._reg]
        for name, kw, run in self._actions():
            items.append(dict(name=name, kind="action", tab="Action", hint=kw, run=run))
        return items

    def _search_rank(self, query, items):
        tokens = [t for t in query.replace(",", " ").split() if t]
        groups = [g.split() for g in SEARCH_SYNONYMS]
        scored = []
        for it in items:
            name = it["name"].lower()
            own = f"{name} {it['tab'].lower()} {it.get('hint', '').lower()}"
            hay = own
            for g in groups:
                if any(t in g for t in tokens) and any(w in own for w in g):
                    hay += " " + " ".join(g)
            score = 0
            ok = True
            for t in tokens:
                if t in name:
                    score += 3
                elif t in hay:
                    score += 1
                else:
                    ok = False
                    break
            if ok and tokens:
                scored.append((score, it))
        if not scored:
            for it in items:
                r = difflib.SequenceMatcher(None, query, it["name"].lower()).ratio()
                if r > 0.4:
                    scored.append((r, it))
        scored.sort(key=lambda p: -p[0])
        return [it for _, it in scored[:10]]

    def _fmt_value(self, e):
        try:
            v = e["get"]()
            if e["kind"] == "slider":
                return str(e["fmt"](v))
            return str(v)
        except Exception:
            return ""

    def _refresh_search(self):
        if not hasattr(self, "search_results"):
            return
        query = self.search_var.get().strip().lower()
        items = self._search_items()
        if not query:
            quick = {"Hide / show everything (panic)", EXTRA_ACTIONS["compact"][0]}
            quick |= {f"Preset: {p}" for p in PRESETS}
            hits = [i for i in items if i["name"] in quick]
            heading = "Quick actions"
        else:
            hits = self._search_rank(query, items)
            heading = f"{len(hits)} match{'es' if len(hits) != 1 else ''}"
        for w in list(self.search_results.winfo_children()):
            w.destroy()
        self._search_first = None
        ctk.CTkLabel(self.search_results, text=heading, font=self.f_small,
                     text_color=MUTED, anchor="w").pack(fill="x", padx=8)
        if not hits:
            ctk.CTkLabel(self.search_results,
                         text="Nothing found. Try a simpler word like 'voice' or 'arrow'.",
                         font=self.f_small, text_color=MUTED, anchor="w").pack(
                fill="x", padx=8, pady=6)
            return
        for it in hits:
            self._search_row(it)

    def _search_row(self, it):
        card = ctk.CTkFrame(self.search_results, fg_color=SURFACE2, corner_radius=10,
                            border_width=1, border_color=PURPLE_DARK)
        card.pack(fill="x", padx=4, pady=3)
        card.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(card, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=10, pady=6)
        ctk.CTkLabel(left, text=it["name"], font=self.f_value, text_color=TEXT,
                     anchor="w", justify="left", wraplength=240).pack(anchor="w")
        kind = it["kind"]
        e = it.get("entry")
        sub_text = "Action" if kind == "action" else it["tab"]
        if e is not None and kind in ("slider", "choice"):
            sub_text += f"   |   {self._fmt_value(e)}"
        sub = ctk.CTkLabel(left, text=sub_text, font=self.f_small, text_color=MUTED,
                           anchor="w")
        sub.pack(anchor="w")

        if kind == "switch":
            var = ctk.BooleanVar(value=bool(e["get"]()))
            ctk.CTkSwitch(
                card, text="", width=46, variable=var,
                command=lambda: e["set"](bool(var.get())),
                progress_color=PUMPKIN, button_color=TEXT, fg_color=SURFACE,
            ).grid(row=0, column=1, padx=10)
            if self._search_first is None:
                self._search_first = lambda: (var.set(not var.get()),
                                              e["set"](bool(var.get())))
        elif kind == "slider":
            def moved(v, e=e, sub=sub, tab=it["tab"]):
                e["set"](v)
                sub.configure(text=f"{tab}   |   {self._fmt_value(e)}")

            sl = ctk.CTkSlider(
                card, width=130, from_=e["lo"], to=e["hi"],
                number_of_steps=e["steps"], command=moved,
                button_color=PUMPKIN, button_hover_color=PUMPKIN_HOVER,
                progress_color=PUMPKIN, fg_color=SURFACE)
            sl.set(e["get"]())
            sl.grid(row=0, column=1, padx=10)
            if self._search_first is None:
                self._search_first = lambda t=it["tab"]: self._goto_tab(t)
        elif kind == "action":
            self._button(card, "Run", it["run"], width=60, height=26).grid(
                row=0, column=1, padx=10)
            if self._search_first is None:
                self._search_first = it["run"]
        else:
            self._button(card, "Open", lambda t=it["tab"]: self._goto_tab(t),
                         width=60, height=26).grid(row=0, column=1, padx=10)
            if self._search_first is None:
                self._search_first = lambda t=it["tab"]: self._goto_tab(t)

    def _search_enter(self):
        if self._search_first:
            self._search_first()

    def _apply_strength_widgets(self):
        elo_on = self.s_use_elo
        self.elo_slider.configure(state="normal" if elo_on else "disabled")
        self.skill_slider.configure(state="disabled" if elo_on else "normal")

    def on_elo_toggle(self, value):
        self.s_use_elo = value
        self._apply_strength_widgets()
        self.schedule_save()

    def on_overlay_toggle(self, value):
        self.s_overlay = value
        self.schedule_save()
        self.log(f"[INFO] Board display turned {'on' if value else 'off'}.")

    def on_lines_change(self, value):
        self.s_lines = int(value)
        self.schedule_save()
        self.log(f"[INFO] Showing {value} move(s) as {self.s_style.lower()}.")

    def on_style_change(self, value):
        self.s_style = value
        self.schedule_save()
        self.log(f"[INFO] Showing {self.s_lines} move(s) as {value.lower()}.")

    def on_test_mode_toggle(self, value):
        self.s_test_mode = bool(value)
        if value:
            if self.s_auto_move:
                self.auto_switch.deselect()
                self.on_auto_toggle(False)
            self.play_now_until = 0.0
            self.log(
                "[INFO] Test mode ON: working on any chess.com game page, for"
                " playing against your own other device or account. While it is"
                " on, panic/hide and step-by-step arrows remain available;"
                " auto-move stays disabled."
            )
        else:
            self.log("[INFO] Test mode OFF: bot games only.")

    def on_reveal_mode_change(self, value):
        self.reveal_mode = value
        self.reveal_count = 0
        self.arrow_show_at = 0.0
        self.schedule_save()
        if value == STEP_MODE:
            if keyboard is None:
                self.log(
                    "[ARROWS] Step by step needs the keyboard package: "
                    f"{sys.executable} -m pip install keyboard"
                )
            else:
                self.log(
                    f"[ARROWS] Tap {self.reveal_label()} to reveal the arrows one"
                    " by one (green, yellow, red). Hold it to hide them all."
                )
        elif value == DELAY_MODE:
            self.log(f"[ARROWS] Arrows appear {self.arrow_delay:.1f}s after the move is found.")
        else:
            self.log("[ARROWS] Arrows appear instantly.")

    def schedule_save(self):
        """Save settings shortly after the last change (main thread only)."""
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.after(400, self._do_save)

    def _do_save(self):
        self._save_job = None
        save_config({
            "color": self.s_color,
            "time": self.s_time,
            "depth": self.s_depth,
            "use_elo": self.s_use_elo,
            "elo": self.s_elo,
            "skill": self.s_skill,
            "overlay": self.s_overlay,
            "lines": self.s_lines,
            "style": self.s_style,
            "reveal": self.reveal_mode,
            "arrow_delay": self.arrow_delay,
            "hold": self.hold_to_hide,
            "opacity": self.arrow_opacity,
            "voice_rate": self.voice_rate,
            "voice_volume": self.voice_volume,
            "hotkey": self.hotkey,
            "reveal_key": self.reveal_key,
            "hide_key": self.hide_key,
            **{"key_" + a: k for a, k in self.extra_keys.items()},
            "scene_on": self.s_scene_on,
            "scene_level": self.s_scene_level,
            "sparks": self.s_sparks,
            "skull_cursor": self.s_skull_cursor,
            "ambience": self.s_ambience,
            "persona": self.persona,
            "neural": self.s_neural,
            "parallax": self.s_parallax,
            "backdrop": self.s_backdrop,
            "bg_style": self.bg_style,
            "bg_palette": self.bg_palette,
            "bg_amount": self.bg_amount,
            "bg_speed": self.bg_speed,
            "bg_dim": self.bg_dim,
            "bg_image": self.bg_image,
            "cockpit_left_width": self.cockpit_left_width,
            "panel_sizes": self.panel_sizes,
            "topmost": self.s_topmost,
            "attach_only": self.s_attach_only,
            "quality": self.s_quality,
            "quality_voice": self.s_quality_voice,
            "quality_big_only": self.s_quality_big_only,
            "auto_delay": self.auto_delay,
            "silent_hidden": self.s_silent_hidden,
            "alpha": self.win_alpha,
            "theme": self.s_theme,
            "neural_voice": self.neural_voice,
            "board_style": self.s_board_style,
        })

    # ------------------------------------------------------------------
    # Log / status (thread-safe)
    # ------------------------------------------------------------------

    def post(self, fn, *args):
        """Ask the main thread to run fn(*args). Safe to call from any thread."""
        self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        """Runs on the main thread: carries out UI work posted by other threads."""
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    print(f"UI update error: {e}")
        except queue.Empty:
            pass
        try:
            self.after(50, self._drain_ui_queue)
        except Exception:
            pass

    def log(self, message):
        self.post(self._log_now, message)

    def _log_now(self, message):
        text = message.strip()
        tag = None
        if ">>>" in text or text.startswith("Best to worst"):
            tag = "move"
        elif text.startswith("[ERROR]") or text.startswith("[VOICE ERROR]"):
            tag = "error"
        elif text.startswith("[SUCCESS]"):
            tag = "good"
        elif text.startswith(("[HOTKEY]", "[ARROWS]", "[KEYTEST]", "[AUTO]")):
            tag = "key"
        elif text.startswith("[PAUSED]"):
            tag = "warn"
        elif text.startswith("[QUALITY]"):
            tag = quality_tag(text)
        elif text.startswith("[STATS]"):
            tag = "dim"
        elif text.startswith("[DEBUG]"):
            tag = "dim"
        self.log_box.configure(state="normal")
        if tag:
            self.log_box.insert(ctk.END, message + "\n", tag)
        else:
            self.log_box.insert(ctk.END, message + "\n")
        self.log_box.see(ctk.END)
        self.log_box.configure(state="disabled")

    def set_status(self, text, kind="idle"):
        self.post(self._set_status_now, text, kind)

    def _set_status_now(self, text, kind):
        self.status_label.configure(
            text=f"  {text}  ", text_color=STATUS_COLORS.get(kind, MUTED)
        )

    def set_thinking(self, on):
        self.post(self._set_thinking_now, on)

    def _set_thinking_now(self, on):
        self.scene.thinking = bool(on)
        if on:
            self.progress.configure(progress_color=PUMPKIN)
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.configure(progress_color=SURFACE2)

    def _set_idle_buttons(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._set_thinking_now(False)
        self._set_status_now("Idle", "idle")
        self._set_fear_now(0.0)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start_assistant(self):
        if self.is_running:
            return
        self.is_running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.set_status("Waking...", "think")

        self.bot_thread = threading.Thread(target=self.run_bot_loop, daemon=True)
        self.bot_thread.start()

    def stop_assistant(self):
        self.is_running = False
        self.post(self._set_idle_buttons)
        self.log("[INFO] Assistant stopped.")

    def on_close(self):
        self.is_running = False
        self._set_console_visible(True)
        try:
            if self._save_job is not None:
                self.after_cancel(self._save_job)
            self._do_save()
        except Exception:
            pass
        try:
            if keyboard is not None:
                keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.scene.stop()
            self.backdrop.stop()
            self.ambience.stop()
            self.speaker.cancel_current()
        except Exception:
            pass
        self.destroy()

    def say(self, text, interrupt=True):
        """Queue speech. A newer move cuts off an older one, so you never hear
        stale advice or two voices at once."""
        if self.s_muted or (self.hidden and self.s_silent_hidden):
            return
        self.speaker.say(
            text, self.voice_rate, self.voice_volume, self.persona, interrupt,
            self.voice_mode(),
        )

    def test_voice(self):
        sample, flags = "Knight to F 3, check", {"check"}
        self.say(compose_speech(self.persona, sample, flags))

    def set_overlay(self, driver, moves, flipped):
        try:
            style = "squares" if self.s_style == "Squares" else "arrows"
            driver.execute_script(
                SET_OVERLAY_JS,
                list(moves) if moves else None,
                bool(flipped),
                style,
                self.arrow_opacity,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def hotkey_label(self):
        return self.hotkey.upper()

    def reveal_label(self):
        return self.reveal_key.upper()

    def moves_to_draw(self):
        """Which of the recommended moves should be on the board right now."""
        # While the dashboard is hidden, arrows only ever appear one at a time,
        # when you tap the Reveal key (like Step by step mode).
        if self.reveal_mode == STEP_MODE or self.hidden:
            return list(self.last_moves[: self.reveal_count])
        if time.time() < self.arrow_show_at:
            return []
        return list(self.last_moves)

    def _cache_window_handle(self):
        """Remember this window's handle so we can tell when it's in front."""
        try:
            import ctypes

            self.update_idletasks()
            self._hwnd = ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)
        except Exception:
            self._hwnd = None

    def _dashboard_in_front(self):
        """True only if THIS window is the one you're typing in."""
        if not self._hwnd:
            return False
        try:
            import ctypes

            return ctypes.windll.user32.GetForegroundWindow() == self._hwnd
        except Exception:
            return False

    def _schedule_focus_check(self, _event=None):
        self.after(50, self._check_focus)

    def _check_focus(self):
        try:
            self._typing = isinstance(self.focus_get(), tk.Entry)
        except Exception:
            self._typing = False

    def _install_hotkey_hook(self):
        if keyboard is None:
            msg = (
                "Key listener is OFF: the 'keyboard' package isn't installed for"
                " this Python. Run:\n"
                f"{sys.executable} -m pip install keyboard\nthen restart this app."
            )
            self.keys_status.configure(text=msg, text_color="#ff6b6b")
            self.log("[HOTKEY] Key listener OFF - 'keyboard' isn't installed.")
            self.log(f"[HOTKEY] Run: {sys.executable} -m pip install keyboard")
            return
        try:
            self._hook = keyboard.hook(self._on_key_event)
            self.keys_status.configure(
                text="Key listener is ON. Use 'Test my keys' if a shortcut seems dead.",
                text_color=SLIME,
            )
            self.log(
                f"[HOTKEY] Key listener ON. {self.hotkey_label()} repeats the last"
                f" move; {self.reveal_label()} steps through arrows (Step by step"
                " mode)."
            )
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            self.keys_status.configure(
                text=f"Key listener failed to start - {detail}", text_color="#ff6b6b"
            )
            self.log(f"[HOTKEY] Could not start the key listener - {detail}")

    def test_keys(self):
        if keyboard is None:
            self.log(
                "[KEYTEST] The key listener is off. Run: "
                f"{sys.executable} -m pip install keyboard"
            )
            return
        self.key_test_until = time.time() + 8
        self.log(
            f"[KEYTEST] For 8 seconds: click on Chrome or anywhere, then press"
            f" {self.hotkey_label()} and {self.reveal_label()}. Heard keys show here."
        )

    def _on_key_event(self, event):
        """Runs on the keyboard library's own thread, for every key event."""
        try:
            name = (event.name or "").lower()
            if event.event_type == "up":
                self._held.discard(name)
                if name == self.reveal_key:
                    self._reveal_key_up()
                return
            if name in self._held:
                return  # ignore auto-repeat while the key is held down
            self._held.add(name)

            if time.time() < self.key_test_until:
                tag = ""
                if name == self.hotkey:
                    tag = "  <- repeat key"
                elif name == self.reveal_key:
                    tag = "  <- reveal key"
                self.log(f"[KEYTEST] heard '{name}'{tag}")

            if self.recording_target:
                self._finish_rebind(name)
                return

            # Don't fire while you're typing in one of this window's text
            # boxes - but only if this window is actually in front.
            if self._typing and self._dashboard_in_front():
                return
            if self.page_typing:
                return  # you're typing in a box on the web page

            if name == self.hotkey:
                threading.Thread(target=self.repeat_last_move, daemon=True).start()
            elif name == self.reveal_key:
                self._reveal_key_down()
            elif name == self.hide_key:
                self.post(self.toggle_hidden)
            else:
                for action, key in self.extra_keys.items():
                    if name == key:
                        self.post(self.run_extra_action, action)
                        break
        except Exception as e:
            self.log(f"[HOTKEY] Key handler error: {e}")

    # Step-by-step reveal: tap = show the next arrow, hold = hide them all.

    def _reveal_key_down(self):
        if self.reveal_mode != STEP_MODE and not self.hidden:
            return
        self._reveal_down = True
        self._reveal_hold_fired = False
        timer = threading.Timer(self.hold_to_hide, self._reveal_hold)
        timer.daemon = True
        self._reveal_timer = timer
        timer.start()

    def _reveal_hold(self):
        if self._reveal_down:
            self._reveal_hold_fired = True
            self.reveal_count = 0
            self.log("[ARROWS] Hidden. Tap the key to show them again.")

    def _reveal_key_up(self):
        if not self._reveal_down:
            return
        self._reveal_down = False
        if self._reveal_timer is not None:
            self._reveal_timer.cancel()
        if self._reveal_hold_fired:
            return  # it was a hold, not a tap
        if self.last_moves and self.reveal_count < len(self.last_moves):
            self.reveal_count += 1

    def toast(self, text, color=CANDLE):
        """A short message that floats up over the haunted scene."""
        self.post(self.scene.toast, text, color)

    def run_extra_action(self, action):
        """Runs on the main thread when one of the extra shortcuts is pressed."""
        if action == "mute":
            value = not self.s_muted
            if value:
                self.mute_switch.select()
            else:
                self.mute_switch.deselect()
            self.on_mute_toggle(value)
            self.toast("Voice muted" if value else "Voice on", MUTED if value else SLIME)
        elif action == "lines":
            value = self.s_lines % 3 + 1
            self.lines_seg.set(str(value))
            self.on_lines_change(str(value))
            self.toast(f"Showing {value} move{'s' if value > 1 else ''}", PUMPKIN)
        elif action == "pause":
            self.paused = not self.paused
            if self.is_running:
                self.set_status("Paused" if self.paused else "Watching",
                                "pause" if self.paused else "run")
            self.log(f"[INFO] Assistant {'paused' if self.paused else 'resumed'}.")
            self.toast("Paused" if self.paused else "Awake again",
                       CANDLE if self.paused else SLIME)
        elif action == "compact":
            self.set_compact(not self.s_compact)
        elif action == "auto":
            value = not self.s_auto_move
            if value:
                self.auto_switch.select()
            else:
                self.auto_switch.deselect()
            self.on_auto_toggle(value)
            self.toast("Auto-move ON" if value else "Auto-move off",
                       PUMPKIN if value else MUTED)
        elif action == "quality":
            value = not self.s_quality_voice
            if value:
                self.qvoice_switch.select()
            else:
                self.qvoice_switch.deselect()
            self.on_qvoice_toggle(value)
            self.toast("Callouts on" if value else "Callouts off",
                       SLIME if value else MUTED)
        elif action == "explain":
            threading.Thread(target=self.explain_last_move, daemon=True).start()
        elif action == "playnow":
            if self.last_move_uci:
                self.play_now_until = time.time() + 8
                self._wake.set()
                self.log("[AUTO] Playing the suggested move once...")
            else:
                self.log("[AUTO] No move has been suggested yet.")
        elif action == "fullscreen":
            if self.hidden:
                return
            value = not self._fs
            if value:
                self.fs_switch.select()
            else:
                self.fs_switch.deselect()
            self.set_fullscreen(value)
        elif action == "copyfen":
            fen = self._orb.get("fen")
            if not fen:
                self.log("[INFO] No position to copy yet.")
            else:
                try:
                    self.clipboard_clear()
                    self.clipboard_append(fen)
                    self.update()
                    self.log(f"[SUCCESS] Copied FEN: {fen}")
                    self.toast("FEN copied", SLIME)
                except tk.TclError as e:
                    self.log(f"[ERROR] Couldn't copy: {e}")
        elif action == "persona":
            names = list(PERSONAS)
            value = names[(names.index(self.persona) + 1) % len(names)]
            self.persona_seg.set(value)
            self.on_persona_change(value)
            self.toast(f"Voice: {value}", "#c79bff")

    def _set_console_visible(self, visible):
        """Hide / restore the black console window (only if there is one)."""
        try:
            import ctypes

            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            if not visible:
                self._console_was_visible = bool(user32.IsWindowVisible(hwnd))
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
            elif self._console_was_visible:
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        except Exception:
            pass

    def toggle_hidden(self):
        """PANIC KEY. Hidden = the dashboard is withdrawn completely (no window,
        no taskbar button, not in Alt+Tab), the console is hidden, the voice and
        ambience stop, the board arrows are cleared and auto-move pauses. Press
        the key again to bring it all back. Main thread only."""
        self.hidden = not self.hidden
        self.reveal_count = 0  # hidden = no arrows until you tap the Reveal key
        self._wake.set()  # the worker loop clears the arrows immediately
        try:
            if self.hidden:
                self.speaker.silence()
                if self.s_ambience:
                    self.ambience.stop()
                self.withdraw()
                self._set_console_visible(False)
            else:
                self._set_console_visible(True)
                self.deiconify()
                self.lift()
                self.attributes("-topmost", True)
                self.after(300, lambda: self.attributes("-topmost", self.s_topmost))
                try:
                    self.focus_force()
                except tk.TclError:
                    pass
                self._refresh_chips()
                if self.s_ambience and self.ambience.available:
                    threading.Thread(target=self._start_ambience, daemon=True).start()
        except tk.TclError:
            pass

    def explain_last_move(self):
        """Say WHY the opponent's last move was good or bad."""
        g = self.last_grade
        if not g:
            self.log("[HOTKEY] No opponent move has been graded yet.")
            self.say("I have nothing to explain yet.")
            return
        self.log(f"[HOTKEY] Explaining: {g['text']}")
        self.post(self._show_grade, g["label"], g["san"], g["text"])
        self.say(g["text"])

    def repeat_last_move(self):
        text = self.last_spoken_text
        if not text:
            self.log("[HOTKEY] No move has been recommended yet.")
            return
        if self.speaker.is_speaking(text):
            return  # that exact line is already playing
        try:
            self.log(f"[HOTKEY] Repeating: {text}")
        except Exception:
            pass
        self.say(text)

    # ----- rebinding -----

    def begin_rebind(self, target="repeat"):
        if keyboard is None:
            self.log(
                "[HOTKEY] Install the keyboard package first: "
                f"{sys.executable} -m pip install keyboard"
            )
            return
        self.recording_target = target
        self._rebind_token += 1
        token = self._rebind_token
        if target.startswith("x:"):
            button = self.extra_btns[target[2:]]
        else:
            button = {"repeat": self.hotkey_btn, "reveal": self.reveal_btn,
                      "hide": self.hide_btn}[target]
        button.configure(text="Press a key...", fg_color=PUMPKIN, hover_color=PUMPKIN)
        self.log("[HOTKEY] Press the key you want to use (Esc cancels).")
        self.after(10000, self._rebind_timeout, token)

    def _finish_rebind(self, name):
        target = self.recording_target
        self.recording_target = None
        if name == "esc":
            self.post(self._rebind_done, "[HOTKEY] Cancelled.")
            return
        current = {"repeat": self.hotkey, "reveal": self.reveal_key, "hide": self.hide_key}
        current.update({"x:" + a: k for a, k in self.extra_keys.items()})
        current.pop(target, None)
        if name in current.values():
            self.post(
                self._rebind_done,
                f"[HOTKEY] {name.upper()} is already used by the other shortcut."
                " Pick a different key.",
            )
            return
        if target == "repeat":
            self.hotkey = name
            message = f"[HOTKEY] Now using {name.upper()} to repeat the move."
        elif target == "reveal":
            self.reveal_key = name
            message = f"[HOTKEY] Now using {name.upper()} to reveal the arrows."
        elif target == "hide":
            self.hide_key = name
            message = f"[HOTKEY] Now using {name.upper()} to hide / show the dashboard."
        else:
            action = target[2:]
            self.extra_keys[action] = name
            message = f"[HOTKEY] Now using {name.upper()} for: {EXTRA_ACTIONS[action][0]}."
        self.post(self._rebind_done, message)

    def _rebind_timeout(self, token):
        if self.recording_target and token == self._rebind_token:
            self.recording_target = None
            self._rebind_done("[HOTKEY] Timed out. Keys unchanged.")

    def _rebind_done(self, message):
        self.hotkey_btn.configure(
            text=self.hotkey_label(), fg_color=SURFACE2, hover_color=PURPLE_DARK
        )
        self.reveal_btn.configure(
            text=self.reveal_label(), fg_color=SURFACE2, hover_color=PURPLE_DARK
        )
        self.hide_btn.configure(
            text=self.hide_key.upper(), fg_color=SURFACE2, hover_color=PURPLE_DARK
        )
        for a, b in self.extra_btns.items():
            b.configure(text=self.extra_keys[a].upper(), fg_color=SURFACE2,
                        hover_color=PURPLE_DARK)
        self.schedule_save()
        self._log_now(message)

    # ------------------------------------------------------------------
    # Browser attach
    # ------------------------------------------------------------------

    def attach_to_chrome(self):
        """Attach to a real Chrome started with a debugging port."""
        script_dir = APP_DIR

        if not port_is_open(DEBUG_PORT) and self.s_attach_only:
            raise RuntimeError(
                "No Chrome with a debugging port is running, and 'Never open"
                " Chrome myself' is on.\nStart Chrome with the launcher file"
                " (Play tab > Browser > Make a Chrome launcher file), keep it"
                " open, then press Awaken again."
            )
        if not port_is_open(DEBUG_PORT):
            chrome = find_chrome()
            if not chrome:
                raise RuntimeError(
                    "Could not find chrome.exe. Edit CHROME_PATHS at the top "
                    "of the script."
                )
            profile_dir = os.path.join(DATA_DIR, "chrome-debug-profile")
            self.log("[INFO] Starting your Chrome with a debugging port...")
            self.log(
                "[INFO] If Chrome was already open, close ALL Chrome windows "
                "first."
            )
            subprocess.Popen([
                chrome,
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={profile_dir}",
                START_URL,
            ])

            for _ in range(30):
                if port_is_open(DEBUG_PORT):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    "Chrome didn't open the debugging port. Close every Chrome "
                    "window (check the system tray too) and try again."
                )
        else:
            self.log("[INFO] Found Chrome already running with debug port.")

        opts = Options()
        opts.debugger_address = f"127.0.0.1:{DEBUG_PORT}"
        driver = webdriver.Chrome(service=make_chrome_service(), options=opts)

        # Make sure we're controlling the chess.com tab, not a blank one.
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            if "chess.com" in driver.current_url:
                break
        self.log(f"[INFO] Attached. Current page: {driver.current_url}")
        return driver

    # ------------------------------------------------------------------
    # Main loop (worker thread)
    # ------------------------------------------------------------------

    def grade_opponent_move(self, engine, before, after, game_token, think_time):
        """Compare the opponent's last move with Stockfish's best move for that position."""
        move = find_played_move(before, after)
        if move is None:
            self.log("[DEBUG] Couldn't work out the opponent's last move - not graded.")
            return
        mover = before.turn
        san = before.san(move)
        limit = chess.engine.Limit(time=max(0.1, min(0.4, float(think_time))))
        engine.configure({"UCI_LimitStrength": False, "Skill Level": 20})
        info_before = engine.analyse(before, limit, game=game_token)
        info_after = engine.analyse(after, limit, game=game_token)
        if "score" not in info_before or "score" not in info_after:
            return
        best_cp = score_cp(info_before["score"], mover)
        after_cp = score_cp(info_after["score"], mover)
        pv = info_before.get("pv") or []
        best_move = pv[0] if pv else None
        pv_after = info_after.get("pv") or []
        reply = pv_after[0] if pv_after else None
        loss = 0 if best_move == move else max(0, best_cp - after_cp)
        label = classify_move(loss, looks_like_sacrifice(before, move), best_cp, after_cp)

        note = f"[QUALITY] Opponent played {san}: {label} (their eval {after_cp / 100:+.2f}"
        if loss >= 60:
            note += f", gave up {loss / 100:.2f}"
        note += ")"
        if label in ("Inaccuracy", "Mistake", "Blunder") and best_move is not None:
            note += f" - best was {before.san(best_move)}"
        self.log(note)
        self.add_grimoire(note.replace("[QUALITY] ", "   "), quality_tag(note))
        if label == "Brilliant":
            self.toast("Brilliant move!", "#4dd8ff")
        elif label == "Blunder":
            self.toast("Opponent blundered!", SLIME)
        elif label == "Mistake":
            self.toast("Opponent slipped", CANDLE)
        self.quality_tally[label] = self.quality_tally.get(label, 0) + 1
        try:
            explanation = explain_move(
                before, move, after, label, loss, best_cp, after_cp, best_move, reply
            )
        except Exception as explain_err:
            explanation = f"Their move was {LABEL_PHRASES.get(label, label.lower())}."
            self.log(f"[DEBUG] Couldn't build an explanation: {explain_err}")
        self.last_grade = {"label": label, "san": san, "text": explanation}
        self.post(self._show_grade, label, san, explanation)
        self.post(self._refresh_telemetry)
        if self.s_quality_voice and (
            not self.s_quality_big_only or label in BIG_LABELS
        ):
            self.say(quality_speech(label), interrupt=False)

    def _pick_promotion(self, driver, piece_type):
        """Click the piece in chess.com's promotion pop-up."""
        letter = chess.piece_symbol(piece_type)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            for color in ("w", "b"):
                found = driver.find_elements(
                    By.CSS_SELECTOR,
                    f".promotion-window .promotion-piece.{color}{letter}",
                )
                if found:
                    try:
                        ActionChains(driver).move_to_element(found[0]).pause(
                            0.12).click().perform()
                    except Exception:
                        driver.execute_script("arguments[0].click();", found[0])
                    return
            time.sleep(0.1)

    def auto_play(self, driver, board, move, flipped):
        """Click `move` on the chess.com board. True if the position changed."""
        if move not in board.legal_moves:
            self.log("[AUTO] That move isn't legal here - skipped.")
            return False
        try:
            fresh = board_from_dom(driver.execute_script(READ_BOARD_JS))
        except Exception:
            fresh = None
        if fresh is None or fresh.fen() != board.fen():
            self.log("[AUTO] The position changed - skipped.")
            return False

        san = board.san(move)
        root = driver.find_element(By.CSS_SELECTOR, "wc-chess-board, chess-board")
        driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", root)
        size = root.size
        w, h = float(size["width"]), float(size["height"])

        def spot(square):
            # Offset from the board's centre, with a little human wobble.
            f = chess.square_file(square)
            r = chess.square_rank(square)
            col = (7 - f) if flipped else f
            row = r if flipped else (7 - r)
            x = (col + 0.5 + random.uniform(-0.2, 0.2)) * (w / 8.0) - w / 2.0
            y = (row + 0.5 + random.uniform(-0.2, 0.2)) * (h / 8.0) - h / 2.0
            return int(round(x)), int(round(y))

        x1, y1 = spot(move.from_square)
        x2, y2 = spot(move.to_square)
        chain = ActionChains(driver)
        chain.move_to_element_with_offset(root, x1, y1).pause(
            random.uniform(0.05, 0.15)).click()
        chain.pause(random.uniform(0.12, 0.35))
        chain.move_to_element_with_offset(root, x2, y2).pause(
            random.uniform(0.05, 0.15)).click()
        chain.perform()
        if move.promotion:
            self._pick_promotion(driver, move.promotion)

        time.sleep(0.6)
        try:
            after = board_from_dom(driver.execute_script(READ_BOARD_JS))
        except Exception:
            after = None
        moved = after is None or after.fen() != board.fen()
        if moved:
            self.log(f"[AUTO] Played {san}.")
            self.toast(f"Auto-played {san}", PUMPKIN)
        else:
            self.log(
                "[AUTO] The click didn't register. Press the Play-once key to"
                " try again (click an empty square first if a piece is selected)."
            )
        return moved

    def run_bot_loop(self):
        script_dir = APP_DIR
        stockfish_path = find_stockfish()

        if not stockfish_path:
            self.log(f"[ERROR] Could not find stockfish.exe in: {script_dir}")
            self.set_status("No engine", "error")
            self.stop_assistant()
            return

        self.log("[INFO] Starting (your side is auto-detected from the board)...")

        driver = None
        engine = None
        try:
            driver = self.attach_to_chrome()

            self.log("[INFO] Loading Stockfish engine...")
            engine = chess.engine.SimpleEngine.popen_uci(stockfish_path, **no_window_kwargs())
            try:
                engine.configure({"Hash": 128})
            except Exception:
                pass

            self.log(
                "[SUCCESS] Attached! Start a bot game and play. Moves will be"
                " announced on your turn."
            )
            self.set_status("Watching", "run")
            self.say("Stockfish active and watching the board.", interrupt=False)

            user_turn = chess.WHITE
            last_side = None
            was_at_start = False
            game_token = object()
            last_acted_key = None
            rec_key = None
            last_lines = None
            redo_silent = False
            pending_key = None
            last_logged_key = None
            last_error = None
            paused_logged = False
            prev_fear = None
            opp_pre = None  # last position seen with the opponent to move
            auto_due_key = None   # position the auto-move timer is running for
            auto_due_at = 0.0
            auto_done_key = None  # position we already tried to auto-play
            my_pre = None         # position you were suggested a move for
            my_best = None        # the move that was suggested
            tracker = None        # follows the game move by move for the PGN
            tracked_fen = None
            game_over_done = False
            last_orb_key = None

            while self.is_running:
                try:
                    if self.paused:
                        self.set_overlay(driver, None, False)
                        time.sleep(0.3)
                        continue

                    # Only operate on chess.com bot games (or test mode).
                    if not find_computer_tab(driver, self.s_test_mode):
                        if not paused_logged:
                            self.log(
                                "[PAUSED] Not on a chess.com bot game. Open a"
                                " game at chess.com/play/computer, or turn on Test mode to use it"
                                " against your own other device."
                            )
                            paused_logged = True
                            self.set_status("Paused", "pause")
                        self.set_overlay(driver, None, False)
                        time.sleep(1.0)
                        continue
                    if paused_logged:
                        self.log("[INFO] Bot game page found - resuming.")
                        paused_logged = False
                        self.set_status("Watching", "run")

                    use_elo = self.s_use_elo
                    elo = max(1320, min(3000, int(self.s_elo)))
                    skill = max(0, min(20, int(self.s_skill)))
                    think_time = float(self.s_time)
                    depth = int(self.s_depth) or None

                    state = driver.execute_script(READ_BOARD_JS)
                    self.page_typing = bool((state or {}).get("typing"))
                    board = board_from_dom(state)

                    if board is None:
                        time.sleep(0.5)
                        continue

                    # Which side are you? "Auto" reads the board orientation,
                    # so it updates itself when a new game gives you a new color.
                    choice = self.s_color
                    if choice == "White":
                        user_turn = chess.WHITE
                    elif choice == "Black":
                        user_turn = chess.BLACK
                    elif state.get("flipped") is None:
                        time.sleep(0.5)
                        continue
                    else:
                        user_turn = chess.BLACK if state["flipped"] else chess.WHITE

                    if user_turn != last_side:
                        self.log(
                            "[INFO] You are playing"
                            f" {'White' if user_turn == chess.WHITE else 'Black'}."
                        )
                        last_side = user_turn

                    # New game: the starting position just appeared.
                    at_start = board.board_fen() == chess.STARTING_BOARD_FEN
                    if at_start and not was_at_start:
                        if last_acted_key is not None:
                            self.log("[INFO] New game detected - resetting.")
                        last_acted_key = None
                        game_token = object()
                        prev_fear = None
                        opp_pre = None
                        auto_due_key = None
                        auto_done_key = None
                        self.last_grade = None
                        self.post(self._show_grade, None, "", "")
                        my_pre = None
                        my_best = None
                        self.match_hits = 0
                        self.match_total = 0
                        self.opening_name = ""
                        tracker = None
                        tracked_fen = None
                        self.game_pgn = ""
                        game_over_done = False
                        self.quality_tally = {}
                        self.post(self._refresh_telemetry)
                        self.eval_history = []
                        self.post(self._draw_eval)
                        self.grim_count = 0
                        self.add_grimoire("---- new game ----", "dim")
                        self.games_seen += 1
                        self.post(self.update_history, [])
                        self.toast("A new game begins", "#c79bff")
                    was_at_start = at_start

                    key = board.fen() + (" me-w" if user_turn == chess.WHITE else " me-b")

                    if key != last_logged_key:
                        self.log(f"[DEBUG] Position: {board.fen()}")
                        last_logged_key = key

                    # Board display: only shown while the position still
                    # matches the one the moves were recommended for.
                    flipped = state.get("flipped")
                    if flipped is None:
                        flipped = user_turn == chess.BLACK
                    show_arrow = (
                        self.s_overlay and rec_key == key and bool(self.last_moves)
                    )
                    self.set_overlay(
                        driver,
                        self.moves_to_draw() if show_arrow else None,
                        flipped,
                    )
                    if key != last_orb_key:
                        last_orb_key = key
                        self.post(self.update_orb, board.fen(), flipped,
                                  list(state.get("highlights") or []))

                    # Debounce: the position must be identical on two reads
                    # in a row (avoids reacting mid-animation).
                    if key != pending_key:
                        pending_key = key
                        time.sleep(0.3)
                        continue

                    # Engine match: did you play the move that was suggested?
                    if my_pre is not None and board.turn != user_turn:
                        played = find_played_move(my_pre, board)
                        if played is not None:
                            self.match_total += 1
                            if played.uci() == my_best:
                                self.match_hits += 1
                            self.post(self._refresh_telemetry)
                        my_pre = None

                    # Opening name, and follow the moves for the PGN export.
                    fen_now = board.board_fen()
                    if fen_now != tracked_fen:
                        tracked_fen = fen_now
                        book_name = self.opening_book.get(fen_now)
                        if book_name and book_name != self.opening_name:
                            self.opening_name = book_name
                            self.log(f"[INFO] Opening: {book_name}")
                            self.add_grimoire(f"   opening: {book_name}", "dim")
                            self.toast(book_name, "#c79bff")
                            self.post(self._refresh_telemetry)
                        fresh_tracker = tracker is None
                        if fresh_tracker:
                            tracker = chess.Board()
                        if advance_tracker(tracker, board):
                            self.game_pgn = tracker_to_pgn(tracker, user_turn)
                            self.post(self.update_history, san_list(tracker))
                        else:
                            tracker = None
                            if not fresh_tracker:
                                self.log(
                                    "[DEBUG] Move tracking lost sync; the saved"
                                    " PGN may be incomplete."
                                )

                    # Game over (checkmate / draw): say so once.
                    if board.is_game_over():
                        if not game_over_done:
                            game_over_done = True
                            self.announce_game_over(board, user_turn)
                    else:
                        game_over_done = False

                    # Remember the position the opponent is about to move from,
                    # so their reply can be graded on your next turn.
                    if board.turn != user_turn and not board.is_game_over():
                        opp_pre = board.copy(stack=False)

                    # If you change "Moves to show" mid-position, redo the
                    # current position silently so the board updates.
                    cur_lines = max(1, min(3, int(self.s_lines)))
                    if (
                        last_lines is not None
                        and cur_lines != last_lines
                        and rec_key == key
                    ):
                        last_acted_key = None
                        redo_silent = True
                    last_lines = cur_lines

                    if (
                        key != last_acted_key
                        and board.turn == user_turn
                        and not board.is_game_over()
                    ):
                        n_lines = cur_lines
                        score = None
                        stats = {}
                        self.set_status("Thinking...", "think")
                        self.set_thinking(True)
                        try:
                            if n_lines == 1:
                                # Single move: honours the Elo / Skill settings.
                                if use_elo:
                                    engine.configure({
                                        "UCI_LimitStrength": True,
                                        "UCI_Elo": elo,
                                    })
                                    strength = f"Elo: {elo}"
                                else:
                                    engine.configure({
                                        "UCI_LimitStrength": False,
                                        "Skill Level": skill,
                                    })
                                    strength = f"Skill: {skill}"

                                self.log(
                                    f"\nYour turn! Thinking ({strength}, Time:"
                                    f" {think_time}s, Depth: {depth or 'Max'})..."
                                )
                                result = engine.play(
                                    board,
                                    chess.engine.Limit(time=think_time, depth=depth),
                                    game=game_token,
                                    info=chess.engine.INFO_ALL,
                                )
                                best = result.move
                                moves_uci = [best.uci()]
                                score = (result.info or {}).get("score")
                                stats = result.info or {}
                            else:
                                # Several moves: the engine's top lines, best
                                # first, at full strength.
                                engine.configure({
                                    "UCI_LimitStrength": False,
                                    "Skill Level": 20,
                                })
                                self.log(
                                    f"\nYour turn! Thinking (top {n_lines} moves,"
                                    f" full strength, Time: {think_time}s, Depth:"
                                    f" {depth or 'Max'})..."
                                )
                                infos = engine.analyse(
                                    board,
                                    chess.engine.Limit(time=think_time, depth=depth),
                                    multipv=n_lines,
                                    game=game_token,
                                )
                                if isinstance(infos, dict):
                                    infos = [infos]
                                found = [i["pv"][0] for i in infos if i.get("pv")]
                                if not found:
                                    raise RuntimeError("The engine returned no moves.")
                                best = found[0]
                                moves_uci = [m.uci() for m in found]
                                score = next(
                                    (i.get("score") for i in infos if i.get("pv")), None
                                )
                                stats = next((i for i in infos if i.get("pv")), {})
                        finally:
                            self.set_thinking(False)
                            self.set_status("Watching", "run")

                        self.set_telemetry(stats)
                        self.log("[STATS] " + telemetry_text(stats))

                        # You (or a premove) may have moved while the engine was
                        # thinking. If so this advice is stale - drop it.
                        try:
                            fresh = board_from_dom(driver.execute_script(READ_BOARD_JS))
                        except Exception:
                            fresh = None
                        if fresh is None or fresh.fen() != board.fen():
                            self.log("[DEBUG] The position changed while thinking - dropped that suggestion.")
                            last_acted_key = None
                            time.sleep(0.2)
                            continue

                        move_str = best.uci()

                        # Natural speech ("Knight to F 3"), flavoured by the persona,
                        # and the fear meter from the engine's evaluation.
                        text, flags = describe_move(board, best)
                        fear = eval_to_fear(score, user_turn) if score is not None else None
                        alarm = (
                            fear is not None
                            and prev_fear is not None
                            and fear - prev_fear > 0.3
                        )
                        if fear is not None:
                            prev_fear = fear
                            self.set_fear(fear)
                        cp_now = None
                        if score is not None:
                            try:
                                cp_now = score_cp(score, user_turn)
                                self.record_eval(cp_now)
                            except Exception:
                                cp_now = None
                        self.grim_count += 1
                        self.add_grimoire(
                            f"#{self.grim_count}  {text}  [{move_str}]"
                            + (f"  {cp_now / 100:+.1f}" if cp_now is not None else ""),
                            "move",
                        )
                        spoken = compose_speech(self.persona, text, flags, alarm)

                        # Remember it for the hotkey and the board display.
                        self.last_spoken_text = spoken
                        self.last_move_uci = move_str
                        self.last_moves = moves_uci
                        self.post(self.update_orb_moves, list(moves_uci))
                        rec_key = key
                        last_acted_key = key
                        my_pre = board.copy(stack=False)
                        my_best = move_str

                        self.log(f">>> RECOMMENDED MOVE: {move_str} <<<")
                        if len(moves_uci) > 1:
                            self.log("Best to worst: " + ", ".join(moves_uci))

                        # Decide when the arrows appear.
                        if not redo_silent:
                            self.reveal_count = 0
                            delay = (
                                self.arrow_delay
                                if self.reveal_mode == DELAY_MODE
                                else 0.0
                            )
                            self.arrow_show_at = time.time() + delay
                        else:
                            self.reveal_count = min(self.reveal_count, len(moves_uci))
                            self.arrow_show_at = 0.0

                        # Instant mode draws right now; the other modes are
                        # handled by the loop above.
                        if self.s_overlay and self.reveal_mode == INSTANT_MODE and not self.hidden:
                            self.set_overlay(driver, moves_uci, flipped)

                        # Speech runs on its own thread, so the loop keeps going.
                        was_redo = redo_silent
                        if not redo_silent:
                            self.say(spoken)
                        redo_silent = False

                        # Grade the opponent's reply that led to this position.
                        if (
                            (self.s_quality or self.s_quality_voice)
                            and not was_redo
                            and opp_pre is not None
                        ):
                            try:
                                self.grade_opponent_move(
                                    engine, opp_pre, board, game_token, think_time
                                )
                            except Exception as grade_err:
                                self.log(f"[DEBUG] Move grading skipped: {grade_err}")
                        opp_pre = None

                    # ----- auto-move (and the one-shot Play-once key) -----
                    forced = time.time() < self.play_now_until
                    if (
                        rec_key == key
                        and board.turn == user_turn
                        and not board.is_game_over()
                        and self.last_move_uci
                        and (forced or auto_done_key != key)
                    ):
                        if not self.s_test_mode and (forced or (self.s_auto_move and not self.hidden)):
                            if auto_due_key != key:
                                auto_due_key = key
                                base = float(self.auto_delay)
                                auto_due_at = time.time() + random.uniform(
                                    base * 0.6, base * 1.4
                                )
                            if forced or time.time() >= auto_due_at:
                                self.play_now_until = 0.0
                                auto_done_key = key
                                try:
                                    ok = self.auto_play(
                                        driver, board,
                                        chess.Move.from_uci(self.last_move_uci),
                                        flipped,
                                    )
                                except Exception as auto_err:
                                    ok = False
                                    first = str(auto_err).splitlines()
                                    self.log(
                                        "[AUTO] Couldn't play the move: "
                                        + (first[0] if first else repr(auto_err))
                                    )
                                if ok:
                                    time.sleep(0.3)
                        else:
                            auto_due_key = None

                    last_error = None

                except Exception as loop_err:
                    # Show errors, but don't spam the same one every 0.5s.
                    msg = str(loop_err).splitlines()[0] if str(loop_err) else repr(loop_err)
                    if msg != last_error:
                        self.log(f"[DEBUG] {msg}")
                        last_error = msg

                waiting = (
                    self.reveal_mode == DELAY_MODE and time.time() < self.arrow_show_at
                ) or (
                    auto_due_key is not None
                    and auto_done_key != auto_due_key
                    and time.time() < auto_due_at + 0.5
                )
                # Wakes early when you press the panic / auto / play-once keys.
                self._wake.wait(0.1 if waiting else 0.5)
                self._wake.clear()

        except Exception as e:
            self.log(f"[ERROR] {e}")
            self.set_status("Error", "error")
        finally:
            # Remove the arrows. Intentionally NOT calling driver.quit():
            # that would close your real Chrome window.
            if driver is not None:
                self.set_overlay(driver, None, False)
            if engine is not None:
                try:
                    engine.quit()
                except Exception:
                    pass
            if self.is_running:
                self.is_running = False
                self.post(self._set_idle_buttons)


def _license_tick(self):
    ok, why = _lic.check_saved()
    if not ok:
        _license_die(why)
    self.after(random.randint(40000, 90000), self._license_tick)


LiveChessDashboard._license_tick = _license_tick


def _report_crash():
    import traceback
    text = traceback.format_exc()
    try:
        with open(os.path.join(DATA_DIR, "crash_log.txt"), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    print(text)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "Haunted Stockfish crashed on start:\n\n" + text[-1200:] +
            "\n\n(Saved to crash_log.txt next to the app.)",
            "Haunted Stockfish", 0x10)
    except Exception:
        pass


def run():
    try:
        app = LiveChessDashboard()
        app.mainloop()
    except Exception:
        _report_crash()
        raise SystemExit(1)


if __name__ == "__main__":
    run()
