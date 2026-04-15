"""Example target scaffold. Rename this file and class for your target.

Replace the body of scrape() with URL discovery + parsing logic. Consult
the matching playbook under the megamaid skill's patterns/ directory:

    shopify_json.md       — /collections/*/products.json endpoints
    paginated_html.md     — numbered pagination
    load_more_infinite.md — "load more" button or infinite scroll
    sitemap_crawl.md      — sitemap.xml driven
    pdf_downloads.md      — index page listing PDFs
    spa_hydration.md      — JS-rendered SPAs
    auth_wall.md          — sites requiring a logged-in session
"""

from __future__ import annotations

from playwright.async_api import Page

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class ExampleTarget(BaseScraper):
    """Minimal target scaffold. Replace with your target's specifics."""

    target_name = "example"
    base_url = "https://example.com"
    rate_limit_seconds = 2.0

    async def scrape(
        self, page: Page, max_items: int | None = None
    ) -> list[ScrapedDoc]:
        """Discover URLs and scrape each one.

        Args:
            page: Playwright page instance. Use self._navigate(page, url)
                for each page load — it handles rate limiting and retries.
            max_items: Optional cap for dry-runs.

        Returns:
            List of ScrapedDoc models.
        """
        urls = await self._discover_urls(page)
        if max_items is not None:
            urls = urls[:max_items]

        docs: list[ScrapedDoc] = []
        for url in urls:
            await self._navigate(page, url)
            title = await page.title()
            body_md = await page.inner_text("body")
            doc = ScrapedDoc(
                id=slug_from_url(url),
                source_url=url,
                title=title,
                content_md=body_md,
                metadata={},
            )
            docs.append(doc)
        return docs

    async def _discover_urls(self, page: Page) -> list[str]:
        """Return the list of item URLs to scrape.

        Replace this with your pattern: sitemap crawl, pagination walk,
        JSON API fetch, etc.
        """
        return [self.base_url]
