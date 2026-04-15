# Recon: surveying an unknown target

> _"Colonel Sandurz, are we being too literal?"_ No. Literal is good.

Before writing any parser code, spend five minutes understanding the
target. The recon determines which pattern to use and catches the
showstoppers (auth walls, JS-only rendering, rate limits, ToS) before
you sink an hour into scaffolding that won't work.

## The five-minute recon checklist

1. **`robots.txt`** — `curl https://<domain>/robots.txt`.
   - Note any `Disallow` entries that cover your target paths.
   - Note `Crawl-delay: N` if present; honor it as the minimum rate limit.
   - Look for `Sitemap:` lines — jackpot if they exist.

2. **`sitemap.xml`** — `curl https://<domain>/sitemap.xml` or
   `/sitemap_index.xml`.
   - If it exists and covers your scope: use `patterns/sitemap_crawl.md`.
   - If it's huge, grep for a sub-sitemap matching your scope.

3. **Fetch one representative page.**

   ```bash
   curl -sL -A "Mozilla/5.0" https://<domain>/some/item | head -200
   ```

   - If the HTML contains your target content: static HTML — use
     `paginated_html.md` or `sitemap_crawl.md`.
   - If you see `<div id="root"></div>` and a bunch of `<script>` tags
     with no content: SPA — use `spa_hydration.md`.
   - If you see `<meta name="shopify-checkout-api-token">`: Shopify —
     try `shopify_json.md` first.
   - If you see `<script id="__NEXT_DATA__">`: Next.js — parse that JSON
     directly (see `spa_hydration.md`).

4. **Open DevTools → Network → XHR/Fetch.** Reload the page and scroll
   through the list. If you see a clean JSON endpoint delivering the
   data: **skip HTML entirely**, call that JSON endpoint. This is the
   single biggest win in scraping.

5. **Look at the ToS.** Most sites have a `/terms` page. If it forbids
   automated access or scraping, and the user doesn't have explicit
   written permission from the site owner: stop.

## Framework fingerprints

Quick visual tells when you view source:

| Signal in HTML/headers                  | Framework            | Notes                                            |
| --------------------------------------- | -------------------- | ------------------------------------------------ |
| `<script id="__NEXT_DATA__">`           | Next.js              | Full page data is in that JSON blob.             |
| `window.__NUXT__`                       | Nuxt.js              | Similar — check the inline script.               |
| `<meta name="shopify-...">`             | Shopify              | Try `/collections/all/products.json?limit=250`.  |
| `cdn.shopifycloud.com`                  | Shopify Plus         | Same.                                            |
| `wp-content/` in asset URLs             | WordPress            | Often has `/wp-json/wp/v2/posts` REST API.       |
| `cdn.squarespace.com`                   | Squarespace          | Try `?format=json-pretty` on any URL.            |
| `Server: cloudflare` + challenge page   | Cloudflare Bot Fight | You may be blocked; rate-limit harder or stop.   |
| `<div id="app"></div>` + Vue Router     | Vue SPA              | `spa_hydration.md`.                              |
| `data-reactroot` or `data-react-helmet` | React SSR            | HTML likely has content; try static parse first. |

## When the target doesn't fit a pattern

- Site is a mix (paginated HTML index → SPA detail pages): use
  `paginated_html.md` for discovery and `spa_hydration.md` for per-page.
- Site serves different HTML to bots vs. browsers: set `headless=False`
  temporarily to compare; if the browser-view content matters, stay
  in Playwright (headed or not).
- The content you want is only available via a user-triggered download
  (CSV export, "Download all"): ask the user to grab it manually. Not
  every problem is a scraping problem.

## Red flags — stop and discuss with the user

- `robots.txt` disallows your paths and you don't have written permission.
- ToS explicitly forbids automated access.
- Site throws CAPTCHA on anonymous traffic.
- Site serves Cloudflare's "Checking your browser..." challenge.
- Content is behind a paywall and the user doesn't have a subscription.

In every one of those cases, the right move is to tell the user what
you found and ask how they want to proceed.
