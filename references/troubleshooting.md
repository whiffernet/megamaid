# Troubleshooting

> _"The radar's been jammed."_ _"Jammed? With what?"_ _"Raspberry jam, sir."_
>
> Most scraper bugs are the equivalent of raspberry jam. Here's how to
> get it off the dish.

## My selectors return nothing

**Check the raw HTML first.** Look in `staging/<slug>/<run>/raw/` for
the file that came back. If the content you're looking for is not in
there, selectors won't save you — it's SPA hydration (see
`patterns/spa_hydration.md`) or the page served different HTML to
your scraper than to your browser.

If the content _is_ in there but your selector misses:

- Print what BeautifulSoup sees: `print(soup.select_one("..."))` and
  walk up the tree.
- Check for nested iframes — `soup.select("iframe")` — the content
  may be in one.
- Check for tag case: `<TABLE>` vs `<table>`. BeautifulSoup normalizes,
  but XPath in Playwright doesn't.
- Check for class collisions: `.product` may appear on wrappers too.
  Anchor your selector: `article.product-card > h2.product-title`.

## Selectors used to work, now they don't

The site changed. Happens.

1. Pull a fresh copy of the page into `raw/` (delete the old one and
   re-run with `--max 1`).
2. Diff against the previous run's raw file. `diff old.html new.html`
   usually tells you the story in 30 seconds.
3. If the class names changed (common with Tailwind-based rebuilds),
   anchor on stable elements: ARIA roles, `data-*` attributes, or
   text content (`page.get_by_text("Price")`).
4. If the site switched frameworks, re-run the recon (`references/recon.md`)
   — you may need a different pattern entirely.

## Timeouts

- **`TimeoutError: page.goto exceeded 30000ms`** — the site is slow or
  blocking you. Try raising the timeout first (`page.set_default_timeout(60000)`).
  If that doesn't help, you're being blocked; see "I'm being blocked"
  below.
- **`wait_for_selector` timeout** — the selector never appeared.
  Common in SPAs. Either the selector is wrong, the content depends
  on auth, or the hydration failed. Take a screenshot (`page.screenshot(path="debug.png")`)
  and look at what actually rendered.
- **`networkidle` never fires** — analytics pixels keep firing. Don't
  wait for networkidle; wait for a content selector instead.

## I'm being blocked

Signs: 403/429 responses, Cloudflare challenge pages, "unusual traffic"
interstitials, sudden shift to CAPTCHAs.

1. **Slow down.** Double `rate_limit_seconds`. Wait an hour. Try again.
2. **Check your User-Agent.** Some sites block generic `Mozilla/5.0`
   with no full string. The megamaid default identifies itself —
   some sites actually whitelist that over obvious fakes.
3. **Check `robots.txt`.** If it disallows you, the block is correct.
   Stop.
4. **Stealth plugins.** `playwright-stealth` patches the headless
   browser fingerprint (`navigator.webdriver`, WebGL vendor, etc.)
   that bot-detection services like Akamai and Cloudflare check. The
   block is usually **TLS fingerprinting**, not IP-based — even
   residential IPs get blocked in headless mode.

   Install as opt-in; do not bake into the scaffold.

   ```bash
   pip install playwright-stealth
   # On headless Linux (no display), also install xvfb:
   sudo apt install xvfb          # Debian/Ubuntu
   sudo dnf install xorg-x11-server-Xvfb  # Fedora/RHEL
   # Mac and Windows don't need xvfb — they have a display natively.
   ```

   Override `run()` in your target class to apply stealth:

   ```python
   from playwright.async_api import Browser
   from playwright_stealth import Stealth

   class ProtectedTarget(BaseScraper):
       async def run(self, browser: Browser, max_items=None):
           stealth = Stealth()
           context = await browser.new_context(
               viewport={"width": 1920, "height": 1080},
           )
           page = await context.new_page()
           await stealth.apply_stealth_async(page)
           try:
               return await self.scrape(page, max_items=max_items)
           finally:
               await context.close()
   ```

   On headless Linux, run with `xvfb-run` to provide a virtual display:

   ```bash
   xvfb-run megamaid suck --max 5
   ```

   The combination of stealth + headed mode (`headless=False` in
   `create_browser()`) + xvfb produces the most authentic TLS
   fingerprint. If stealth alone doesn't work, try switching to headed
   mode.

5. **Proxy rotation** is out of scope for this skill. If you need it,
   you are trying to scrape something that really doesn't want to be
   scraped. Reconsider whether this is the right approach.

## The browser won't launch

- **`BrowserType.launch: Executable doesn't exist`** — run
  `playwright install chromium`.
- **`Host system is missing dependencies`** — on Linux,
  `sudo playwright install-deps` (or `apt install <listed deps>`).
- **`EACCES` / permission errors** — don't run as root; make sure your
  user owns the `.venv` directory.

## Manifest is stale / items won't unskip

The delta detection marks an item `unchanged` when its `identity_hash`
matches the previous run. If you changed what `identity_hash` covers
(by editing `ScrapedDoc.compute_identity_hash`), every item will look
"changed" on the next run. Delete the latest manifest or run with a
clean staging directory to reset.

## Playwright is slow

Blocking analytics and asset domains speeds things up dramatically:

```python
await page.route(
    "**/{analytics,gtm,googletagmanager,hotjar,mixpanel,segment}/**",
    lambda r: r.abort(),
)
```

Also: disabling images if you don't need them.

```python
await page.route("**/*.{png,jpg,jpeg,gif,webp,svg}", lambda r: r.abort())
```

## Things that are not bugs

- First run reports everything as "new" — correct, no previous manifest
  to diff against.
- `unchanged` count equals `total` on a re-run — means nothing changed,
  which is usually what you want.
- Pydantic validation error on a ScrapedDoc — your parser returned
  something wrong. Look at the field; the error tells you exactly which.

## Everything is broken

Delete `staging/<slug>/` and re-run with `--max 3`. Fresh state,
small sample, one problem at a time.
