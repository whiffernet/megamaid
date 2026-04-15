# Pattern: Load More / Infinite Scroll

> _"Keep firing, assholes!"_ — what the Load More button is saying, basically.

## When to use

Items appear as you scroll (infinite scroll) or when you click a "Load
more" button. No numbered pagination, no sitemap of items.

**Before implementing this pattern**, open DevTools → Network tab and
trigger the load. Nine times out of ten, clicking "Load more" fires an
XHR to `/api/items?offset=N` or similar — **call that endpoint directly
instead.** Much faster, much more reliable.

## If you must drive the UI

Use Playwright to click/scroll until the list stops growing.

```python
from bs4 import BeautifulSoup
from playwright.async_api import Page

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class LoadMoreTarget(BaseScraper):
    target_name = "example_loadmore"
    base_url = "https://example.com/feed"
    rate_limit_seconds = 2.0

    async def scrape(self, page: Page, max_items=None):
        await self._navigate(page, self.base_url)
        last_count = 0
        while True:
            items = await page.query_selector_all("article.item")
            if max_items and len(items) >= max_items:
                break
            if len(items) == last_count:
                break
            last_count = len(items)
            # Try "Load more" button first
            btn = await page.query_selector('button:has-text("Load more")')
            if btn and await btn.is_visible():
                await btn.click()
            else:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            try:
                await page.wait_for_function(
                    f"document.querySelectorAll('article.item').length > {last_count}",
                    timeout=8000,
                )
            except Exception:
                break  # No more items loaded

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        docs = []
        for art in soup.select("article.item")[: max_items or None]:
            link = art.select_one("a")
            href = link.get("href", "")
            url = href if href.startswith("http") else self.base_url + href
            title = link.get_text(strip=True)
            docs.append(
                ScrapedDoc(
                    id=slug_from_url(url),
                    source_url=url,
                    title=title,
                    content_md="",
                    metadata={},
                )
            )
        return docs
```

## Gotchas

- **Fixed-height virtualized lists** (React-window, etc.) unmount
  off-screen items as you scroll. You must scrape items as they appear,
  not after. Loop: scroll → capture visible items → scroll more.
- **Rate limit doesn't help here.** The page loads items client-side.
  Set `rate_limit_seconds` to 0 for the single page, but throttle your
  _item detail_ fetches afterwards.
- **Stopping condition.** The classic "scroll until length stops
  growing" can break if the site re-renders. Also watch for a
  `"No more results"` element.
- If the underlying XHR returns JSON, always prefer calling it directly.
