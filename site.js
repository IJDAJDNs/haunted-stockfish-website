(() => {
  const config = window.SITE_CONFIG || window.HAUNTED_SITE_CONFIG || {};
  const plans = config.plans || {};
  if (config.checkoutUrls) {
    ["week", "month", "lifetime"].forEach((key) => {
      plans[key] = Object.assign({}, plans[key], { checkoutUrl: config.checkoutUrls[key] });
    });
  }
  const planKeys = ["week", "month", "lifetime"];

  const setText = (selector, value) => {
    if (value == null || value === "") return;
    document.querySelectorAll(selector).forEach((node) => { node.textContent = value; });
  };

  const isHttpsUrl = (value) => {
    try { return new URL(value).protocol === "https:"; }
    catch { return false; }
  };

  setText("[data-version]", config.appVersion);

  const checkoutUrls = {};
  planKeys.forEach((key) => {
    const plan = plans[key] || {};
    setText(`[data-plan-name="${key}"]`, plan.name);
    setText(`[data-plan-price="${key}"]`, plan.price);
    setText(`[data-plan-term="${key}"]`, plan.term);
    setText(`[data-plan-billing="${key}"]`, plan.billing);

    const url = plan.checkoutUrl || (key === "week" ? config.checkoutUrl : "");
    checkoutUrls[key] = isHttpsUrl(url) ? url : "";

    const button = document.querySelector(`[data-plan-checkout="${key}"]`);
    if (!button) return;
    if (checkoutUrls[key]) {
      button.href = checkoutUrls[key];
      button.target = "_blank";
      button.rel = "noopener noreferrer";
      button.removeAttribute("aria-disabled");
    } else {
      button.href = "#pricing";
      button.setAttribute("aria-disabled", "true");
      button.addEventListener("click", (event) => {
        event.preventDefault();
        const note = document.querySelector("[data-checkout-note]");
        if (note) note.textContent = `Add the ${plan.name || key} hosted checkout URL in site-config.js before publishing.`;
      });
    }
  });

  const checkoutNote = document.querySelector("[data-checkout-note]");
  if (checkoutNote && planKeys.every((key) => checkoutUrls[key])) checkoutNote.hidden = true;

  const weekCheckout = checkoutUrls.week;
  document.querySelectorAll("[data-checkout]").forEach((link) => {
    if (weekCheckout) {
      link.href = weekCheckout;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.removeAttribute("aria-disabled");
    } else {
      link.href = "#pricing";
      link.setAttribute("aria-disabled", "true");
      link.addEventListener("click", (event) => {
        event.preventDefault();
        document.querySelector("#pricing")?.scrollIntoView({ behavior: "smooth" });
        const note = document.querySelector("[data-checkout-note]");
        if (note) note.textContent = "Add the 7-Day Pass checkout URL in site-config.js before publishing.";
      });
    }
  });

  const frame = document.querySelector("[data-video-frame]");
  const placeholder = document.querySelector("[data-video-placeholder]");
  if (frame && config.tutorialEmbedUrl) {
    try {
      const videoUrl = new URL(config.tutorialEmbedUrl);
      const youtube = ["www.youtube-nocookie.com", "youtube-nocookie.com", "www.youtube.com"].includes(videoUrl.hostname)
        && /^\/embed\/[\w-]+$/.test(videoUrl.pathname);
      const vimeo = videoUrl.hostname === "player.vimeo.com" && /^\/video\/\d+$/.test(videoUrl.pathname);
      if (videoUrl.protocol === "https:" && (youtube || vimeo)) {
        const player = document.createElement("iframe");
        player.src = videoUrl.href;
        player.title = "Haunted Stockfish tutorial video";
        player.loading = "lazy";
        player.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
        player.referrerPolicy = "strict-origin-when-cross-origin";
        player.allowFullscreen = true;
        frame.appendChild(player);
        if (placeholder) placeholder.hidden = true;
      }
    } catch { /* Keep the setup placeholder visible if the URL is invalid. */ }
  }

  const support = document.querySelector("[data-support]");
  if (support) {
    if (config.supportEmail) {
      support.href = `mailto:${config.supportEmail}`;
    } else {
      support.href = "#pricing";
      support.setAttribute("aria-label", "Add a support email address before launch");
      support.textContent = "Add support email before launch";
    }
  }

  const feedbackForm = document.querySelector("[data-feedback-form]");
  const feedbackStatus = document.querySelector("[data-feedback-status]");
  if (feedbackForm && feedbackStatus) {
    const recipient = String(config.supportEmail || "").trim();
    const ready = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(recipient);
    if (ready) {
      feedbackStatus.textContent = "Your email app will open with a draft. Review it and press Send to submit; this website does not upload or store your report.";
      feedbackStatus.classList.add("is-ready");
    }
    feedbackForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!ready) {
        feedbackStatus.textContent = "Feedback is not connected yet. Add a valid inbox to supportEmail in site-config.js before publishing the form.";
        feedbackStatus.classList.add("is-error");
        return;
      }
      const values = new FormData(feedbackForm);
      const type = String(values.get("type") || "Feedback").trim();
      const title = String(values.get("subject") || "").trim();
      const details = String(values.get("details") || "").trim();
      const version = String(values.get("version") || "Not provided").trim();
      const replyEmail = String(values.get("replyEmail") || "Not provided").trim();
      const subject = `[Haunted Stockfish] ${type}: ${title}`;
      const body = [
        `Type: ${type}`,
        `App version: ${version || "Not provided"}`,
        `Reply email: ${replyEmail || "Not provided"}`,
        "",
        details,
      ].join("\n");
      feedbackStatus.textContent = "Your email app should open a draft now. Press Send there to submit your feedback.";
      feedbackStatus.classList.remove("is-error");
      feedbackStatus.classList.add("is-ready");
      window.location.href = `mailto:${recipient}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    });
  }

  const menuButton = document.querySelector(".menu-toggle");
  const nav = document.querySelector(".site-nav");
  if (menuButton && nav) {
    menuButton.addEventListener("click", () => {
      const isOpen = nav.classList.toggle("open");
      menuButton.setAttribute("aria-expanded", String(isOpen));
    });
    nav.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => {
      nav.classList.remove("open");
      menuButton.setAttribute("aria-expanded", "false");
    }));
  }

  document.querySelectorAll("[data-download]").forEach((link) => {
    if (isHttpsUrl(config.downloadUrl)) {
      link.href = config.downloadUrl;
      link.setAttribute("download", "");
      link.rel = "noopener noreferrer";
    }
  });

  const year = document.querySelector("#year");
  if (year) year.textContent = new Date().getFullYear();
})();
