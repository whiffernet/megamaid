# Pattern: REST / JSON API

> _"We're at now now."_ _"When?"_ _"Now."_ — And the API just gave us
> all the data. No selectors, no waiting, no DOM. Just JSON, right now.

## When to use

The site has a JSON API behind the UI. This is **almost always the
better path** when it exists — faster, more reliable, no selector drift,
structured data for free.

Detection:

- Open DevTools → Network → filter XHR/Fetch → reload the page, scroll,
  click through navigation. Watch for JSON responses.
- URLs containing `/api/`, `/v1/`, `/v2/`, `/graphql`, `?format=json`
- `<script id="__NEXT_DATA__">` (Next.js) — the full page data in one blob
- `window.__NUXT__` (Nuxt.js) — same idea
- WordPress REST API: `/wp-json/wp/v2/posts`
- Squarespace: append `?format=json-pretty` to any URL

If you see a clean JSON endpoint returning the data you need, **skip
HTML scraping entirely**.

## How to discover the API

1. Open DevTools → Network tab → filter by XHR/Fetch
2. Reload the page and watch requests appear
3. Scroll down, click "Load more", change pages — watch for pagination
4. Click on a promising request and inspect:
   - **Request URL** — this is your endpoint
   - **Query params** — look for `page`, `offset`, `cursor`, `limit`, `after`
   - **Response body** — confirm it contains the data you want
5. Test with curl:
   ```bash
   curl -s "https://example.com/api/items?page=1&limit=10" | python3 -m json.tool | head -30
   ```
6. Check if auth is needed — try without cookies first. If you get 401/403,
   you need an API key, bearer token, or session cookie.

## Key difference from other patterns

This is the first megamaid pattern that **skips Playwright for data
fetching**. The target class overrides `run()` to use httpx directly:

```python
import httpx
from playwright.async_api import Browser

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class MyAPITarget(BaseScraper):
    target_name = "example_api"
    base_url = "https://example.com"
    rate_limit_seconds = 1.0

    async def scrape(self, page, max_items=None):
        raise NotImplementedError("Use run() directly for API targets")

    async def run(self, browser: Browser, max_items=None):
        # Skip Playwright — use httpx for JSON APIs
        async with httpx.AsyncClient(
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            return await self._scrape_api(client, max_items)

    async def _scrape_api(self, client, max_items):
        docs = []
        page = 1
        while True:
            data = await self._fetch_json(
                client,
                f"{self.base_url}/api/items?page={page}&limit=50",
            )
            if not data or not data.get("results"):
                break
            for item in data["results"]:
                docs.append(self._parse(item))
                if max_items and len(docs) >= max_items:
                    return docs
            page += 1
        return docs

    def _parse(self, item: dict) -> ScrapedDoc:
        return ScrapedDoc(
            id=slug_from_url(item["url"]),
            source_url=item["url"],
            title=item["title"],
            content_md=item.get("description", ""),
            metadata=item,
        )
```

`_fetch_json()` is a BaseScraper helper that handles rate limiting,
retries with exponential backoff, and `429 Retry-After` headers. You
don't need to manage any of that yourself.

## Pagination styles

### Page / offset

The simplest. Increment `page` or `offset` until the response is empty.

```python
# Page-based: ?page=1, ?page=2, ...
url = f"{base}/api/items?page={page_num}&limit=50"

# Offset-based: ?offset=0&limit=50, ?offset=50&limit=50, ...
url = f"{base}/api/items?offset={offset}&limit=50"
offset += 50
```

Stop when the results array is empty or shorter than `limit`.

### Cursor-based

The response includes a cursor/token for the next page:

```python
cursor = None
while True:
    url = f"{base}/api/items?limit=50"
    if cursor:
        url += f"&cursor={cursor}"
    data = await self._fetch_json(client, url)
    if not data:
        break
    # Process items...
    cursor = data.get("next_cursor") or data.get("next_page_token")
    if not cursor:
        break
```

### Link header

GitHub-style APIs use `Link: <url>; rel="next"`:

```python
import re

async def _fetch_with_link(self, client, url):
    """Fetch JSON and parse Link header for next page URL."""
    await self._rate_limit()
    resp = await client.get(url, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    # Parse Link header
    link = resp.headers.get("Link", "")
    match = re.search(r'<([^>]+)>;\s*rel="next"', link)
    next_url = match.group(1) if match else None
    return data, next_url
```

## Rate limit headers

Many APIs advertise their limits. Check for these response headers:

- `X-RateLimit-Limit` — max requests per window
- `X-RateLimit-Remaining` — requests left in current window
- `X-RateLimit-Reset` — unix timestamp when the window resets
- `Retry-After` — seconds to wait (on 429 responses)

`_fetch_json()` handles `429 + Retry-After` automatically. For
proactive throttling based on `X-RateLimit-Remaining`, add logic in
your target:

```python
remaining = int(resp.headers.get("X-RateLimit-Remaining", 999))
if remaining < 10:
    self.rate_limit_seconds = 5.0  # slow down near the limit
```

## Authentication

- **API key in header**: `headers={"Authorization": "Bearer <key>"}` or
  `headers={"X-API-Key": "<key>"}`
- **API key in query param**: `?api_key=<key>` (less common, less secure)
- **Session cookie**: extract from `storage_state.json` after manual login
  (see `patterns/auth_wall.md`)

Never hardcode API keys in the target class. Use environment variables:

```python
import os
API_KEY = os.environ["MY_API_KEY"]
```

## Hybrid: API for data, Playwright for rendering

Sometimes the API gives you item IDs or URLs, but the detail pages need
a browser for images or JS-rendered content. Use httpx for the listing
API, then switch to Playwright for detail pages:

```python
async def run(self, browser, max_items=None):
    # Phase 1: httpx for the listing API
    async with httpx.AsyncClient(...) as client:
        item_urls = await self._fetch_all_urls(client)
    if max_items:
        item_urls = item_urls[:max_items]

    # Phase 2: Playwright for detail pages
    page = await browser.new_page()
    try:
        docs = []
        for url in item_urls:
            await self._navigate(page, url)
            docs.append(await self._parse_detail(page, url))
        return docs
    finally:
        await page.close()
```

## Example (Open Library search API)

```python
import httpx
from playwright.async_api import Browser

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc


class OpenLibrarySearch(BaseScraper):
    target_name = "openlibrary_search"
    base_url = "https://openlibrary.org"
    rate_limit_seconds = 1.0

    def __init__(self, query: str = "python", **kwargs):
        super().__init__(**kwargs)
        self.query = query

    async def scrape(self, page, max_items=None):
        raise NotImplementedError("Use run() directly for API targets")

    async def run(self, browser: Browser, max_items=None):
        async with httpx.AsyncClient(
            headers={"User-Agent": self.user_agent},
        ) as client:
            return await self._scrape_api(client, max_items)

    async def _scrape_api(self, client, max_items):
        docs = []
        offset = 0
        limit = 50
        while True:
            data = await self._fetch_json(
                client,
                f"{self.base_url}/search.json?q={self.query}"
                f"&offset={offset}&limit={limit}",
            )
            if not data or not data.get("docs"):
                break
            for item in data["docs"]:
                key = item.get("key", "")
                docs.append(ScrapedDoc(
                    id=key.replace("/", "-").strip("-"),
                    source_url=f"{self.base_url}{key}",
                    title=item.get("title", ""),
                    content_md="",
                    metadata={
                        "author": item.get("author_name", []),
                        "first_publish_year": item.get("first_publish_year"),
                        "edition_count": item.get("edition_count"),
                        "subject": item.get("subject", [])[:10],
                    },
                ))
                if max_items and len(docs) >= max_items:
                    return docs
            offset += limit
            if offset >= data.get("numFound", 0):
                break
        return docs
```

## Gotchas

- **CORS doesn't matter.** CORS is browser-enforced. Server-side httpx
  requests are never blocked by CORS headers.
- **Internal APIs change without notice.** The endpoint you found in
  DevTools may not be documented or stable. Handle missing keys
  gracefully (`item.get("field", "")` not `item["field"]`).
- **Pagination off-by-one.** Some APIs are 0-indexed (`offset=0`),
  some are 1-indexed (`page=1`). Test with curl first.
- **Rate limits are stricter on APIs.** Page loads might tolerate 1
  request/second, but the API might cap at 100 requests/minute. Check
  the rate limit headers.
- **Large JSON responses.** If the endpoint returns megabytes per
  request, use `client.stream("GET", url)` and parse incrementally.
- **Nested pagination.** Some APIs need two loops: outer for categories,
  inner for items within each category. Same pattern, just nested.

## Public test targets

- `https://openlibrary.org/search.json?q=python&limit=5` — offset pagination, no auth
- `https://jsonplaceholder.typicode.com/posts?_page=1&_limit=10` — page pagination, no auth
- `https://api.github.com/repos/python/cpython/issues?per_page=5&state=open` — Link header pagination, rate limited
