# Pattern: Sitemap Crawl

> _"Use the sitemap, Lone Starr. Use the sitemap."_

## When to use

The site publishes a `sitemap.xml` (or `sitemap_index.xml`) that
enumerates the URLs you care about. This is the happy path — no
pagination drift, no "Next" buttons, just a flat URL list with
`<lastmod>` timestamps for free.

Detection: `curl https://<domain>/sitemap.xml` or
`curl https://<domain>/robots.txt | grep -i sitemap`.

## URL discovery

A sitemap is either:

- A **urlset** (`<urlset>` root) — flat list of `<url><loc>...</loc></url>`
- A **sitemapindex** (`<sitemapindex>` root) — list of sub-sitemaps; fetch each and recurse.

## Example

```python
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Page

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class SitemapTarget(BaseScraper):
    target_name = "example_sitemap"
    base_url = "https://example.com"
    rate_limit_seconds = 2.0

    async def scrape(self, page: Page, max_items=None):
        urls = self._collect_urls(f"{self.base_url}/sitemap.xml")
        if max_items:
            urls = urls[:max_items]
        docs = []
        for url, lastmod in urls:
            await self._navigate(page, url)
            html = await page.content()
            docs.append(self._parse(html, url, lastmod))
        return docs

    def _collect_urls(self, sitemap_url: str) -> list[tuple[str, str]]:
        xml = httpx.get(sitemap_url, timeout=30.0).text
        root = ET.fromstring(xml)
        tag = root.tag.split("}", 1)[-1]
        out: list[tuple[str, str]] = []
        if tag == "sitemapindex":
            for sm in root.findall("sm:sitemap", NS):
                loc = sm.findtext("sm:loc", default="", namespaces=NS)
                out.extend(self._collect_urls(loc))
        else:
            for url in root.findall("sm:url", NS):
                loc = url.findtext("sm:loc", default="", namespaces=NS)
                lastmod = url.findtext("sm:lastmod", default="", namespaces=NS)
                if loc:
                    out.append((loc, lastmod))
        return out

    def _parse(self, html: str, url: str, lastmod: str) -> ScrapedDoc:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else url
        body = soup.get_text("\n", strip=True)
        return ScrapedDoc(
            id=slug_from_url(url),
            source_url=url,
            title=title,
            content_md=body,
            metadata={"lastmod": lastmod},
        )
```

## Gotchas

- Sitemaps can be **gzipped** (`sitemap.xml.gz`). `httpx` decodes
  `Content-Encoding: gzip` automatically, but if the URL ends in `.gz`
  you need to `gzip.decompress(response.content)`.
- Some sitemaps list URLs not actually reachable (stale). Tolerate 404s
  gracefully — don't let one bad URL fail the run.
- Filter by URL prefix if the sitemap covers more than your scope:
  `[(u, l) for u, l in urls if "/articles/" in u]`.
- `<lastmod>` is gold for delta detection — stash it in `metadata` and
  you can skip re-fetching unchanged items on subsequent runs.
