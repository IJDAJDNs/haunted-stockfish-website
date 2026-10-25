# Haunted Stockfish website starter

This is a small static website: plain HTML, CSS, and JavaScript. It needs no build tools or server-side code. It markets practice against Chess.com computer opponents, with candidate move arrows, voice grades and explanations, engine settings, an optional Crystal Ball board, PGN export, and the Haunted theme. The `assets` folder contains app screenshots supplied for this page and board / scene previews rendered from the assistant’s v9 drawing code. The prices and checkout links are examples until you connect your store.

## Preview it

Open `index.html` in a browser. Before launch, read through the copy and make sure every feature described matches the release version of the app.

## Set up payment and the download

1. Create three products or variants in your payment provider: **$3 for 7 days**, **$10 for one month**, and **$20 lifetime**. The website currently describes these as one-time purchases with no automatic renewal. Make the checkout settings match.
2. If you use license keys, set the expiry/renewal behavior and device activation limit in the provider, then integrate license activation and validation into the Windows app. A checkout button by itself does not activate or enforce an app license.
3. Create a separate hosted HTTPS checkout link for each plan. Configure the provider's post-purchase page or email to deliver the license key and Windows download.
4. Open `site-config.js` and replace each plan's `checkoutUrl`, price, term, and billing description with the offer you actually sell. Add an inbox you monitor as `supportEmail`.
5. To add your tutorial, upload it to YouTube or Vimeo, copy its player/embed URL, and paste it into `tutorialEmbedUrl`. For YouTube, use a URL like `https://www.youtube-nocookie.com/embed/VIDEO_ID`; for Vimeo, use `https://player.vimeo.com/video/VIDEO_ID`.
6. Replace the images in `assets` with current screenshots when the app UI changes; update the alt text/captions if a screenshot shows different controls.
7. Add your final terms, privacy notice, refund/cancellation details, support contact, and open-source notices before taking payments. The use notice on the page is product guidance, not a complete legal agreement.

Each plan button stays disabled until it has a real HTTPS checkout URL. Never put a private payment-provider API key in this public folder. If you decide to make the month plan a recurring subscription, update the checkout settings and the displayed billing language so renewal is unmistakable.

The bug / feature request form uses the same `supportEmail` setting. It prepares an email in the visitor's mail app; visitors must press Send themselves. The static site does not upload or store feedback. If you want submissions to work without a mail app, connect a hosted form service or a server endpoint before launch.

## Publish it

For the simplest launch, use a static host. With Cloudflare Pages Direct Upload, create a Pages project and upload this folder (or a ZIP of its files) in the dashboard. The host gives you a preview address. Once you are happy with the preview, connect a domain in the host's domain settings and follow its DNS instructions. See [Cloudflare Pages Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/).

The site files need to stay together: `index.html`, `styles.css`, `site-config.js`, `site.js`, and the `assets` folder.

## Low-cost launch marketing

- Make a short screen recording showing an online computer game, spoken and visual move suggestions, an engine setting change, and completed-game review.
- Show the actual product interface in screenshots and video. Avoid adding made-up customer reviews, ratings, endorsements, or security badges.
- Send the demo to chess-learning creators and offer a review copy or a tracked discount code.
- Share useful practice clips in communities that permit product posts; identify yourself as the maker and avoid repetitive promotion.
- Use the same clear message everywhere: online computer-game practice and completed-game review. Keep the use notice visible near pricing and checkout.

## Important before launch

The public app build and the website should describe the same allowed use. Keep any private testing controls out of the customer release if they permit a use that the website says is unsupported. Review all third-party software licenses and include the required notices and source information with the Windows distribution.
