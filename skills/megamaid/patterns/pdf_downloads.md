# Pattern: PDF Downloads

> _"We have received your PDFs. All of them."_ — President Skroob, probably.

## When to use

The content you want is packaged as PDFs linked from an index page.
Common for government open-data portals, technical spec sheets,
research libraries, annual reports.

## Approach

1. Discover PDF URLs from the index page (typically `a[href$=".pdf"]`
   or by filtering by `Content-Type`).
2. Download each PDF as binary to `raw/`.
3. Extract text with `pypdf` (pure-Python, no system deps) into
   `content_md`.

## Dependencies

Add to `pyproject.toml`:

```toml
dependencies = [
    ...
    "pypdf>=4.0",
]
```

## Example

```python
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Page
from pypdf import PdfReader

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class PdfIndexTarget(BaseScraper):
    target_name = "example_pdfs"
    base_url = "https://example.gov/reports"
    rate_limit_seconds = 2.0

    async def scrape(self, page: Page, max_items=None):
        await self._navigate(page, self.base_url)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        pdf_links = [
            a["href"] for a in soup.select("a[href$='.pdf']") if a.get("href")
        ]
        pdf_links = [
            href if href.startswith("http") else self.base_url.rstrip("/") + "/" + href.lstrip("/")
            for href in pdf_links
        ]
        if max_items:
            pdf_links = pdf_links[:max_items]

        raw_root = Path("staging") / self.target_name / "_raw"
        raw_root.mkdir(parents=True, exist_ok=True)

        docs = []
        async with httpx.AsyncClient(
            timeout=60.0, headers={"User-Agent": self.user_agent}
        ) as client:
            for url in pdf_links:
                slug = slug_from_url(url)
                pdf_path = raw_root / f"{slug}.pdf"
                if not pdf_path.exists():
                    resp = await client.get(url)
                    resp.raise_for_status()
                    pdf_path.write_bytes(resp.content)
                try:
                    reader = PdfReader(str(pdf_path))
                    text = "\n\n".join(p.extract_text() or "" for p in reader.pages)
                    title = (reader.metadata.title or slug) if reader.metadata else slug
                except Exception:
                    text = ""
                    title = slug
                docs.append(
                    ScrapedDoc(
                        id=slug,
                        source_url=url,
                        title=title,
                        content_md=text,
                        raw_path=str(pdf_path),
                        metadata={"bytes": pdf_path.stat().st_size},
                    )
                )
        return docs
```

## Gotchas

- **Image-only PDFs** (scans) return empty text from `pypdf`. You need
  OCR (`pytesseract` + `pdf2image`). That's a separate dependency chain
  — only add it when you hit a scanned PDF in the wild.
- **Chunked / javascript-rendered links.** If the index is rendered by
  JS, discover links via Playwright (`page.query_selector_all`) rather
  than BeautifulSoup on the initial HTML.
- **Direct-download PDFs** sometimes require a referer header or a
  session cookie. Carry those via `httpx.AsyncClient(headers=..., cookies=...)`.
- **Large files.** Stream with `client.stream("GET", url)` and write in
  chunks if the PDFs exceed a few MB.
- **Filename collisions.** Use `slug_from_url()` as the filename, not
  the last path segment — two reports can both be called `report.pdf`.
