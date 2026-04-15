# Pattern: Image Downloads

> _"That's amazing! She's captured their images!"_ — Colonel Sandurz, watching Mega Maid vacuum an entire planet's photo library.

## When to use

The target has product images, gallery photos, or other visual assets you
need saved alongside the text content. You want the actual image files on
disk, not text descriptions or OCR output.

Common scenarios: e-commerce product photos, article hero images, PDF
cover pages, portfolio galleries, recipe photos.

## Quick start

1. Set `download_images = True` on your target class.
2. In `scrape()`, after navigating to a page:
   - Call `scroll_and_wait(page)` to trigger lazy-loaded images.
   - Call `discover_page_images(page)` to find image URLs.
   - Call `download_images(candidates, self._images_dir)` to fetch them.
3. Attach the returned `ImageRef` list to your `ScrapedDoc.images`.

```python
from megamaid.base import BaseScraper
from megamaid.images import discover_page_images, download_images, scroll_and_wait
from megamaid.models import ScrapedDoc, slug_from_url


class MyTarget(BaseScraper):
    target_name = "example_images"
    base_url = "https://example.com"
    download_images = True
    image_min_width = 200  # skip small icons

    async def scrape(self, page, max_items=None):
        urls = await self._discover_urls(page)
        if max_items:
            urls = urls[:max_items]

        docs = []
        for url in urls:
            await self._navigate(page, url)
            await scroll_and_wait(page)

            doc = await self._parse(page, url)

            candidates = await discover_page_images(
                page, min_width=self.image_min_width
            )
            doc.images = await download_images(
                candidates,
                self._images_dir,
                user_agent=self.user_agent,
                max_bytes=self.image_max_bytes,
                min_bytes=self.image_min_bytes,
                max_count=self.image_max_per_doc,
                concurrency=self.image_concurrency,
            )
            docs.append(doc)
        return docs
```

## Child page traversal

The image helpers capture images from whatever page the browser is
currently on. To get images from product detail pages (multiple angles,
zoom views), your target class discovers URLs on the landing page and
visits each one — the same URL discovery pattern every megamaid target
already uses:

```
/catalogue/page-1.html (index page)
  -> discover item URLs: /catalogue/a-light-in-the-attic_1000/, ...
    -> visit /catalogue/a-light-in-the-attic_1000/
      -> scrape text (title, price, description)
      -> scroll_and_wait() + discover + download images (cover art)
    -> visit /catalogue/tipping-the-velvet_999/
      -> scrape text + images
    -> ...
```

No base class changes are needed for this. It's entirely the target
class's `scrape()` method that decides which pages to visit.

## Shopify shortcut

Shopify product JSON includes an `images[]` array with `src` URLs. Skip
DOM discovery entirely and build `ImageCandidate` objects from the JSON:

```python
from megamaid.images import ImageCandidate, download_images

candidates = [
    ImageCandidate(url=img["src"], alt_text=img.get("alt", ""))
    for img in product.get("images", [])
]
doc.images = await download_images(candidates, self._images_dir)
```

This is more reliable than scraping the rendered page — no lazy loading
to deal with, no carousel slides to click through.

## Lazy loading

Most e-commerce sites use intersection observers: images start as
`src="data:,"` with real URLs in `srcset`, and only resolve when
scrolled into view.

**Always call `scroll_and_wait(page)` before `discover_page_images()`.**
The helper uses `img.currentSrc` — the browser's already-resolved URL —
which is only populated after the intersection observer fires.

If you skip scrolling, you'll discover zero or near-zero images.

## Responsive CDN images

Sites serve multiple sizes via `srcset` with CDN query params:

```
?wid=320   # mobile
?wid=750   # tablet
?wid=1440  # desktop
?wid=1920  # retina
```

The browser picks the best match for the current viewport.
`discover_page_images()` uses the browser's resolved choice, which
depends on the viewport size set in `browser.new_context()`. Use
`1920x1080` (the scaffold default) to get large variants.

## Filtering

Three layers remove non-content images automatically:

1. **Tracking pixel domains** — TikTok, Meta, Bing, Pinterest, Google
   Analytics, OneTrust, etc. Caught by domain-name check.
2. **Minimum dimensions** — `image_min_width` (default 100px). Catches
   0x0 tracking pixels, 1x1 beacons, 34x34 color swatches.
3. **Minimum file size** — `image_min_bytes` (default 1 KB). Catches
   placeholder/error responses from CDNs (some return 44-byte empty
   webp files for missing images).

Adjust `image_min_width` on your target class if you're getting too
many or too few images.

## Deduplication

Images are saved with content-hash filenames (`a3f2c1deadbeef01.jpg`).
If the same image appears on 50 product pages, only one file is written.
The `ImageRef.content_hash` field links each document to the shared file.

## Size estimation

Quick formula: `products x avg_images x avg_size_kb`.

Typical e-commerce at 1440w resolution: ~40 KB per image (webp).
A 500-product store with 6 images each: `500 x 6 x 40 KB = ~120 MB`.

Run `--max 5` first to sample, then extrapolate.

## Anti-bot protected sites

Some e-commerce CDNs block headless browsers via TLS fingerprinting
(Akamai, Cloudflare Bot Fight). The block is fingerprint-based, not
IP-based. Workaround:

1. `pip install playwright-stealth`
2. Linux without a display: `sudo apt install xvfb`
3. Override `run()` on your target:

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

Run with `xvfb-run megamaid suck` on headless Linux. Mac and Windows
have a display natively and don't need xvfb.

See `references/troubleshooting.md` for more on stealth.

## Combining with other patterns

Image downloads layer on top of any existing pattern. You can combine
with paginated HTML, sitemap crawl, Shopify JSON, or any other pattern.
The image helpers don't care how you got to the page — they just capture
what's in the DOM when you call them.
