# Pattern: RSS / Atom Feed

> _"The stuff keeps arriving! Marvelous! I didn't even have to ask!"_
>
> — President Skroob, probably

## When to use

The site publishes its content via an RSS or Atom feed. Feeds are the
original self-publishing API — curated by the source, rate-limit-free
in spirit, and stable across decades. When a target has a good feed,
**prefer it over HTML scraping**.

Detection:

- HTML `<link rel="alternate" type="application/rss+xml">` or
  `type="application/atom+xml">` in the `<head>`
- Common URL suffixes: `/feed`, `/rss`, `/feed.xml`, `/atom.xml`,
  `/index.xml`, `/rss.xml`, `/feed/atom/`
- Content-Type response header `application/rss+xml`,
  `application/atom+xml`, or `application/xml`
- WordPress: `<domain>/feed/` and `<domain>/comments/feed/`
- Substack: `<publication>.substack.com/feed`
- GitHub: `github.com/<user>/<repo>/releases.atom` (releases), or
  `commits.atom` (commits)
- Reddit: `reddit.com/r/<sub>/.rss`
- arXiv, NPR, BBC, NYT — all publish topical feeds

If you see `<rss version="2.0">` or `<feed xmlns="http://www.w3.org/2005/Atom">`
at the top of the payload, you're in.

## How to discover the feed

1. `curl -s https://<domain> | grep -oE 'application/(rss|atom)\+xml[^>]*href="[^"]+"'`
2. Fall back to common paths in order: `/feed`, `/rss`, `/atom.xml`, `/feed.xml`
3. Some sites have **multiple feeds** — topic-specific, author-specific,
   or comments vs. posts. Inspect `<link>` tags to pick the right one.

## Key difference from other patterns

Feeds are **already-normalized content**. No HTML parsing, no selector
drift — the publisher did the work for you. The only trade-off: feeds
usually only contain the **most recent N items** (typically 10-50),
not the full archive. For backfill you still need sitemap or
paginated_html.

Feeds are a **polling pattern** — ideal for continuous monitoring. On
every run, dedupe against the manifest's identity hashes to pick up
only new items.

```python
import httpx
from playwright.async_api import Browser
from xml.etree import ElementTree as ET

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class MyFeedTarget(BaseScraper):
    target_name = "example_blog"
    base_url = "https://example.com"
    feed_url = "https://example.com/feed"
    rate_limit_seconds = 2.0

    async def scrape(self, page, max_items=None):
        raise NotImplementedError("Use run() directly for feed targets")

    async def run(self, browser: Browser, max_items=None):
        async with httpx.AsyncClient(
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            return await self._scrape_feed(client, max_items)

    async def _scrape_feed(self, client, max_items):
        xml_text = await self._fetch_xml(client, self.feed_url)
        if not xml_text:
            return []
        root = ET.fromstring(xml_text)
        items = _iter_feed_items(root)
        docs = []
        for item in items:
            docs.append(self._parse(item))
            if max_items and len(docs) >= max_items:
                break
        return docs

    def _parse(self, item: dict) -> ScrapedDoc:
        return ScrapedDoc(
            id=slug_from_url(item["url"]),
            source_url=item["url"],
            title=item["title"],
            content_md=item["summary"],
            metadata={
                "published": item.get("published"),
                "author": item.get("author"),
                "categories": item.get("categories", []),
            },
        )
```

## Dual RSS + Atom parser

RSS 2.0 and Atom 1.0 have different element names. Write one iterator
that handles both:

```python
from xml.etree import ElementTree as ET

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _iter_feed_items(root: ET.Element) -> list[dict]:
    """Yield normalized items from either RSS 2.0 or Atom 1.0."""
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    if tag == "rss":
        # RSS 2.0: rss > channel > item
        items = root.findall("./channel/item")
        return [_rss_item(item) for item in items]

    if tag == "feed":
        # Atom 1.0: feed > entry
        entries = root.findall("atom:entry", _ATOM_NS)
        return [_atom_entry(entry) for entry in entries]

    return []


def _rss_item(item: ET.Element) -> dict:
    """Extract normalized fields from an RSS 2.0 <item>."""
    return {
        "url": (item.findtext("link") or "").strip(),
        "title": (item.findtext("title") or "").strip(),
        "summary": (item.findtext("description") or "").strip(),
        "published": item.findtext("pubDate"),
        "author": item.findtext("{http://purl.org/dc/elements/1.1/}creator")
        or item.findtext("author"),
        "categories": [c.text for c in item.findall("category") if c.text],
    }


def _atom_entry(entry: ET.Element) -> dict:
    """Extract normalized fields from an Atom 1.0 <entry>."""
    link_el = entry.find("atom:link[@rel='alternate']", _ATOM_NS) or entry.find(
        "atom:link", _ATOM_NS
    )
    link = link_el.get("href") if link_el is not None else ""
    author_el = entry.find("atom:author/atom:name", _ATOM_NS)
    return {
        "url": link,
        "title": (entry.findtext("atom:title", default="", namespaces=_ATOM_NS)).strip(),
        "summary": (
            entry.findtext("atom:summary", default="", namespaces=_ATOM_NS)
            or entry.findtext("atom:content", default="", namespaces=_ATOM_NS)
        ).strip(),
        "published": entry.findtext("atom:published", namespaces=_ATOM_NS)
        or entry.findtext("atom:updated", namespaces=_ATOM_NS),
        "author": author_el.text if author_el is not None else None,
        "categories": [
            c.get("term") for c in entry.findall("atom:category", _ATOM_NS) if c.get("term")
        ],
    }
```

## Polling and delta detection

Feeds shine for ongoing monitoring. On each run:

1. Fetch the feed.
2. Parse all items into `ScrapedDoc` objects.
3. Let the manifest's identity-hash delta detection do the rest —
   already-seen items are skipped automatically.

Schedule via cron or `megamaid suck` in a loop with `--delta` to see
just what's new. No special code needed beyond the base pattern.

## Optional: enrich with full content

Many feeds include only a summary. For full article text, use the
feed's `<link>` as a seed and fetch the full page (hybrid pattern,
same idea as `rest_json_api.md`'s hybrid section):

```python
# Phase 1: feed for URLs + metadata
items = _iter_feed_items(root)

# Phase 2: Playwright for full article content
page = await browser.new_page()
try:
    for item in items:
        await self._navigate(page, item["url"])
        html = await page.content()
        item["content_md"] = _extract_article(html)  # trafilatura, readability, etc.
finally:
    await page.close()
```

## Gotchas

- **Feeds are a moving window.** Typically 10-50 most recent items.
  For historical backfill, combine with a sitemap or `paginated_html`.
- **Feed format sniffing.** Don't trust the URL suffix — check the
  root element (`<rss>` vs `<feed>`) before parsing.
- **Namespaces matter for Atom.** Atom elements are namespaced under
  `http://www.w3.org/2005/Atom`; RSS 2.0 is namespace-less. Use
  namespace maps in `findall`/`findtext`.
- **CDATA in summaries.** RSS `<description>` often wraps HTML in
  CDATA. `ET` returns the raw text content — strip HTML via
  BeautifulSoup or trafilatura before storing.
- **Podcast feeds are RSS.** With the iTunes extension
  (`xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"`) adding
  `<itunes:duration>`, `<itunes:image>`, `<enclosure url="...mp3">`.
  Parse those via the extension namespace.
- **`<link>` vs `<link rel="alternate">`.** Atom can have multiple
  `<link>` elements with different `rel` values (`self`, `alternate`,
  `edit`). The one you want is usually `rel="alternate"` or the first
  `<link>` without a `rel`.
- **Conditional GETs save bandwidth.** Feeds honor `If-Modified-Since`
  and `If-None-Match` (ETag). Worth implementing if polling frequently.
- **`<atom:updated>` vs `<atom:published>`.** Both may appear.
  `updated` is when the entry was last changed; `published` is
  first-publication time. Treat `published` as the canonical date.
- **RFC 5005 pagination is rare.** Most feeds don't paginate. If you
  see `<link rel="next">` in Atom, that's RFC 5005 — follow it like
  any other pagination.
- **Feed burn services proxy feeds.** FeedBurner (`feeds.feedburner.com`)
  adds tracking pixels and sometimes breaks relative URLs. Prefer the
  original feed URL when available.

## Public test targets

- `https://hnrss.org/newest` — Hacker News newest (RSS 2.0, fast refresh)
- `https://export.arxiv.org/rss/cs.LG` — arXiv machine-learning (RSS 2.0)
- `https://www.reddit.com/r/programming/.rss` — Reddit (Atom 1.0, rate-limited)
- `https://github.com/python/cpython/releases.atom` — GitHub releases (Atom 1.0)
- `https://blog.python.org/feeds/posts/default?alt=atom` — classic Atom blog
