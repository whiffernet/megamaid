# Pattern: SPA Hydration

> _"The radar, sir. It appears to be jammed."_ — No sir, it's client-side rendering.

## When to use

Fetching the raw HTML returns a near-empty shell (`<div id="root"></div>`

- a pile of JS bundles). The actual content appears only after
  JavaScript runs. React, Vue, Next.js, Nuxt, SvelteKit in CSR mode, etc.

Detection: `curl <url> | grep -c <expected_text>` returns 0, but the
page looks fine in a real browser.

## Two approaches (try them in this order)

### 1. Find the data API

SPAs fetch their data from a backend, usually JSON. DevTools → Network
→ filter XHR/Fetch → reload. Nine times out of ten there's a clean
endpoint like `/api/articles?cursor=...`. Call that directly and
forget the HTML.

### 2. Wait for hydration in Playwright

If there is no clean API, let Playwright load the page and wait for a
selector that only exists after data is fetched.

```python
from bs4 import BeautifulSoup
from playwright.async_api import Page

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class SpaTarget(BaseScraper):
    target_name = "example_spa"
    base_url = "https://example.com"
    rate_limit_seconds = 2.0

    async def scrape(self, page: Page, max_items=None):
        urls = await self._discover(page)
        if max_items:
            urls = urls[:max_items]

        docs = []
        for url in urls:
            await self._navigate(page, url)
            # Wait for the hydrated content, not just DOMContentLoaded
            await page.wait_for_selector("article.loaded", timeout=15000)
            html = await page.content()
            docs.append(self._parse(html, url))
        return docs

    async def _discover(self, page: Page) -> list[str]:
        await self._navigate(page, f"{self.base_url}/list")
        await page.wait_for_selector("a.item-link", timeout=15000)
        links = await page.eval_on_selector_all(
            "a.item-link", "els => els.map(e => e.href)"
        )
        return list(set(links))

    def _parse(self, html: str, url: str) -> ScrapedDoc:
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.select_one("h1")
        body_el = soup.select_one("article.loaded")
        return ScrapedDoc(
            id=slug_from_url(url),
            source_url=url,
            title=title_el.get_text(strip=True) if title_el else url,
            content_md=body_el.get_text("\n", strip=True) if body_el else "",
            metadata={},
        )
```

## Next.js-specific shortcut

Next.js pages embed the full page data in `<script id="__NEXT_DATA__" type="application/json">`.
Parse that directly — no waiting, no selectors, no fragility:

```python
import json
from bs4 import BeautifulSoup

soup = BeautifulSoup(html, "html.parser")
next_data = soup.find("script", id="__NEXT_DATA__")
if next_data:
    data = json.loads(next_data.string)
    page_props = data["props"]["pageProps"]
    # ... extract from page_props
```

Nuxt has a similar `window.__NUXT__`.

## Gotchas

- **`wait_for_selector` timing.** Don't use `networkidle` — analytics
  pixels keep firing forever on many SPAs and you'll time out. Wait for
  an actual content selector.
- **Infinite analytics retries.** Block outbound requests to analytics
  domains via `page.route("**/analytics/**", lambda r: r.abort())` to
  speed up page loads.
- **Hydration errors.** Sometimes React hydration fails and the page
  looks "fine" but data is missing. If you see empty content, bump the
  timeout and check `page.content()` manually.
- **Headless detection.** Some SPAs behave differently under
  `headless=True`. If the page renders in `headless=False` but not
  headless, see `references/troubleshooting.md`.
