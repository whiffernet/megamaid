# Pattern: Search-Seeded Crawl

> _"Comb the desert!"_ _"We ain't found shit."_ _"Keep combing."_
>
> — Dark Helmet — because sometimes there's no map, only keywords

## When to use

The site has **no usable sitemap** and **no linear pagination** that
enumerates everything — but it has a **working search box**. You seed
the crawl with a list of queries, iterate the results pages, and
collect the items that come out the other side.

Detection:

- `robots.txt` forbids the sitemap, or sitemap returns 404 / is empty
- Browsing is facet-driven (filter by category/price/date) rather than
  paginated lists
- A search field exists and returns pages like `/search?q=shoes&page=2`
- The site has a JSON search API (check DevTools) — common in e-commerce

Classic examples: Home Depot, Macy's, most job boards, academic paper
search (Semantic Scholar, CORE, PubMed when you don't have CID lists),
most modern sites whose sitemap is behind anti-bot.

## Key difference from other patterns

`paginated_html` and `sitemap_crawl` assume you can enumerate **every**
URL. `search_seed` assumes you **cannot**, and instead covers the
corpus by iterating a list of queries whose result sets overlap. Your
coverage is only as good as your query list — so pick queries that are
broad, varied, and plausible.

This pattern's main work is **query curation**, not scraping
mechanics. The scraping mechanics reuse `paginated_html` or
`rest_json_api` underneath.

## Query list strategies

- **Category words**: for e-commerce, use the site's own category
  vocabulary (scraped once from the nav). "shoes", "jackets",
  "lamps", "nails".
- **Alphabet sweep**: search "a", "b", ..., "z". Noisy but covers
  things category vocabularies miss. Best for directory sites.
- **Brand list**: if the site indexes by brand, dump the brand list
  and search each.
- **External vocabulary**: use a domain taxonomy — NAICS codes, ICD-10
  codes, product GS1 categories, RFC section titles.
- **Stopwords as queries**: "the", "and", "a" — surprisingly effective
  on search engines that treat queries as OR. Last resort.

Write the query list to `queries.txt` in the project root. Keep it
editable — the user will tune it.

## Example

```python
import httpx
from pathlib import Path
from playwright.async_api import Browser

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class MySearchTarget(BaseScraper):
    target_name = "example_search"
    base_url = "https://example.com"
    search_api = "https://example.com/api/search?q={query}&page={page}&per_page=50"
    rate_limit_seconds = 2.0

    def __init__(self, queries_file: str | Path = "queries.txt", **kwargs):
        super().__init__(**kwargs)
        self.queries = [
            line.strip()
            for line in Path(queries_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    async def scrape(self, page, max_items=None):
        raise NotImplementedError("Use run() directly for search-seed targets")

    async def run(self, browser: Browser, max_items=None):
        async with httpx.AsyncClient(
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            return await self._scrape_queries(client, max_items)

    async def _scrape_queries(self, client, max_items):
        seen_ids: set[str] = set()
        docs: list[ScrapedDoc] = []
        for query in self.queries:
            async for item in self._iter_query(client, query):
                doc_id = item["id"]
                if doc_id in seen_ids:
                    continue  # Cross-query dedup
                seen_ids.add(doc_id)
                docs.append(self._parse(item))
                if max_items and len(docs) >= max_items:
                    return docs
        return docs

    async def _iter_query(self, client, query: str):
        page = 1
        while True:
            url = self.search_api.format(query=query, page=page)
            data = await self._fetch_json(client, url)
            if not data or not data.get("results"):
                return
            for item in data["results"]:
                yield item
            if len(data["results"]) < 50:  # Last page
                return
            page += 1

    def _parse(self, item: dict) -> ScrapedDoc:
        return ScrapedDoc(
            id=slug_from_url(item["url"]),
            source_url=item["url"],
            title=item["title"],
            content_md=item.get("snippet", ""),
            metadata={
                "discovered_via_query": item.get("_query"),
                "score": item.get("relevance_score"),
            },
        )
```

## Cross-query deduplication

The same item will appear in multiple query result sets. **Always
dedupe**, or you'll write the same doc hundreds of times.

- Cheap: in-memory `set()` of IDs or normalized URLs (as above)
- Durable: the manifest's identity-hash delta detection — already-seen
  docs are skipped on write anyway
- Best: both — in-memory dedup during the run avoids redundant
  re-fetching; manifest dedup catches anything that slips through

## Coverage estimation

Since you'll never enumerate every URL, estimate coverage:

1. **New items per query** — track how many genuinely new items each
   query surfaces. When the new-item rate drops to zero, you're probably
   near saturation.
2. **Total unique items** — log `len(seen_ids)` after each query.
   Plateau = diminishing returns.
3. **Spot-check against known IDs** — if the target has a few URLs you
   know about, check whether your crawl found them. If not, expand
   your query list.

Ship the coverage report in `manifest.json`:

```python
manifest["coverage"] = {
    "queries_run": len(self.queries),
    "unique_items": len(seen_ids),
    "items_per_query": {q: count for q, count in per_query_counts.items()},
}
```

## HTML-only search (no JSON API)

If the search endpoint returns HTML, fall back to `paginated_html`:

```python
async def _iter_query_html(self, page, query: str):
    page_num = 1
    while True:
        url = f"{self.base_url}/search?q={query}&page={page_num}"
        await self._navigate(page, url)
        html = await page.content()
        items = _parse_result_page(html)  # BeautifulSoup or selectors
        if not items:
            return
        for item in items:
            yield item
        page_num += 1
```

## Gotchas

- **Search results decay.** Engines deprioritize deep pages. Page 20
  of a query often returns garbage or the same items as page 1.
  Cap `max_pages_per_query` (20-50 is usually plenty).
- **Query injection.** URL-encode queries before interpolation
  (`urllib.parse.quote_plus`). An unquoted `&` or `#` in a query
  breaks the URL.
- **Fuzzy matching surprises.** "chair" might return tables because
  "chair table" matched a title. Check snippets, not just titles.
- **Rate limits hit search APIs harder.** A page load is 1 request;
  a 100-query crawl is 100+ requests. Increase `rate_limit_seconds`
  or add backoff.
- **Some engines cap at N pages.** Google, Bing, many e-commerce
  engines stop paginating at 40-100 pages regardless of result count.
  Split broad queries into narrower ones rather than chasing page 50.
- **Personalization.** Logged-in search results differ from
  anonymous ones. Always scrape anonymously unless the user
  explicitly wants logged-in results.
- **Query explosion.** 10,000-query lists will take days at 2s/req
  with multi-page results. Estimate runtime before starting:
  `queries × avg_pages × rate_limit`.
- **Empty queries.** Most engines ignore empty queries, some return
  random items, a few return everything. Strip empty lines from the
  query file.

## Public test targets

- `https://openlibrary.org/search.json?q=<query>&limit=10` — JSON
  search API, no auth, offset pagination
- `https://api.semanticscholar.org/graph/v1/paper/search?query=<q>&limit=20`
  — academic search, rate limited
- `https://www.googleapis.com/books/v1/volumes?q=<query>&maxResults=40`
  — Google Books, no auth for low volume
