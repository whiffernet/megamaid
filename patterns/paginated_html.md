# Pattern: Paginated HTML

> _"Page one. Page two. Page three. ...Prepare to jump to ludicrous speed."_ — No. Regular speed.

## When to use

The index of items lives across numbered pages:

- `?page=2`, `?p=2`, `/page/2/`, `/2/`, `/items/page-2`
- A visible "Next" link or page number navigation at the bottom

## URL discovery

Two approaches, pick one:

1. **Walk "Next" links.** Robust against page-count changes. Stop when
   no "Next" link exists.
2. **Enumerate page numbers.** Faster if you can read the last page
   number from the UI upfront. Fragile if pagination changes.

## Example (books.toscrape.com — a public scraping sandbox)

```python
from bs4 import BeautifulSoup
from playwright.async_api import Page
from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class BooksToScrape(BaseScraper):
    target_name = "books_toscrape"
    base_url = "https://books.toscrape.com"
    rate_limit_seconds = 1.5

    async def scrape(self, page: Page, max_items=None):
        item_urls = await self._discover(page)
        if max_items:
            item_urls = item_urls[:max_items]
        docs = []
        for url in item_urls:
            await self._navigate(page, url)
            docs.append(await self._parse(page, url))
        return docs

    async def _discover(self, page: Page) -> list[str]:
        urls: list[str] = []
        index_url = f"{self.base_url}/catalogue/page-1.html"
        while index_url:
            await self._navigate(page, index_url)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            for link in soup.select("article.product_pod h3 a"):
                urls.append(f"{self.base_url}/catalogue/{link['href']}")
            next_btn = soup.select_one("li.next a")
            index_url = (
                f"{self.base_url}/catalogue/{next_btn['href']}" if next_btn else None
            )
        return urls

    async def _parse(self, page: Page, url: str) -> ScrapedDoc:
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        title = soup.select_one("div.product_main h1").get_text(strip=True)
        price = soup.select_one("p.price_color").get_text(strip=True)
        desc_el = soup.select_one("#product_description ~ p")
        desc = desc_el.get_text(strip=True) if desc_el else ""
        return ScrapedDoc(
            id=slug_from_url(url),
            source_url=url,
            title=title,
            content_md=desc,
            metadata={"price": price},
        )
```

## Gotchas

- **Stopping condition.** Always stop when "Next" disappears, not at a
  hardcoded page number.
- **URL canonicalization.** `/page/1/` and `/` may be the same page —
  check or you'll double-scrape.
- **Deduplication.** If you walk both pagination _and_ a sitemap, dedupe
  on source URL before parsing.
- **Rate limit.** `books.toscrape.com` tolerates 1.5s. Smaller sites
  need 2–3s.
