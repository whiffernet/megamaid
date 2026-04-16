"""Image discovery and download utilities.

Provides helpers for scraping images from web pages:

- ``discover_page_images`` extracts image URLs from the current DOM
- ``download_images`` fetches them with content-hash deduplication
- ``scroll_and_wait`` triggers lazy-loaded images by scrolling

Target classes call these explicitly inside their ``scrape()`` method.
The base class does not orchestrate image downloading automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import Page

from .models import ImageRef

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}

TRACKING_DOMAINS = {
    "analytics.",
    "tiktok.com",
    "facebook.com",
    "bing.com",
    "pinterest.com",
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "hotjar",
    "mixpanel",
    "segment.",
    "lantern.",
    "roeye.com",
    "cookielaw.org",
    "onetrust.com",
}

# CDN size parameter names used for resolution grouping.
# When the same base image appears at multiple sizes, these params
# are used to identify the resolution and pick the best variant.
CDN_SIZE_PARAMS = {"wid", "hei", "w", "h", "width", "height", "size"}


@dataclass
class ImageCandidate:
    """A discovered image before downloading.

    Attributes:
        url: Absolute URL to the image.
        alt_text: Alt attribute from the img tag.
        width: Width in pixels (from DOM naturalWidth or srcset descriptor).
        height: Height in pixels (from DOM naturalHeight).
        source_type: How the image was found (img_src, srcset, og_image, bg_image).
    """

    url: str
    alt_text: str = ""
    width: int = 0
    height: int = 0
    source_type: str = ""


def _is_tracking_pixel(url: str) -> bool:
    """Check if a URL belongs to a known tracking/analytics domain."""
    lower = url.lower()
    return any(d in lower for d in TRACKING_DOMAINS)


def _get_base_path(url: str) -> str:
    """Extract the base image path from a CDN URL, stripping query params.

    Used to group the same image served at different resolutions.
    """
    return urlparse(url).path


def _get_size_from_url(url: str) -> int:
    """Extract the largest size parameter value from a CDN URL.

    Looks for common CDN size params (wid, hei, w, h, width, height, size).
    Returns 0 if no size param found.
    """
    from urllib.parse import parse_qs

    qs = parse_qs(urlparse(url).query)
    max_size = 0
    for param in CDN_SIZE_PARAMS:
        vals = qs.get(param, [])
        for v in vals:
            try:
                max_size = max(max_size, int(v))
            except (ValueError, TypeError):
                continue
    return max_size


def _dedup_by_resolution(
    candidates: list[ImageCandidate],
    prefer: str = "largest",
) -> list[ImageCandidate]:
    """Group images by base path and keep only the preferred resolution.

    When the same image appears at multiple CDN sizes (e.g. wid=72 and
    wid=1080), this function keeps only one variant per base image.

    Args:
        candidates: Image candidates, possibly containing duplicates at
            different resolutions.
        prefer: Resolution strategy:
            - ``"largest"`` — keep the highest-resolution variant (default)
            - ``"smallest"`` — keep the lowest-resolution variant
            - A numeric string like ``"1080"`` — keep the variant closest
              to this width

    Returns:
        Deduplicated list with one candidate per base image.
    """
    from collections import defaultdict

    # Group by base path
    groups: dict[str, list[ImageCandidate]] = defaultdict(list)
    for c in candidates:
        base = _get_base_path(c.url)
        groups[base].append(c)

    result = []
    for base, variants in groups.items():
        if len(variants) == 1:
            result.append(variants[0])
            continue

        # Multiple variants — pick based on strategy
        sized = [(c, _get_size_from_url(c.url)) for c in variants]

        if prefer == "largest":
            best = max(sized, key=lambda x: x[1])
        elif prefer == "smallest":
            # Among variants with a size > 0, pick smallest; fallback to first
            with_size = [(c, s) for c, s in sized if s > 0]
            best = min(with_size, key=lambda x: x[1]) if with_size else sized[0]
        else:
            # Numeric target — pick closest to the target
            try:
                target = int(prefer)
            except ValueError:
                target = 1080
            best = min(sized, key=lambda x: abs(x[1] - target))

        result.append(best[0])

    return result


def _content_hash(data: bytes) -> str:
    """SHA-256 of image bytes, truncated to 16 hex chars for the filename."""
    return hashlib.sha256(data).hexdigest()[:16]


def _full_hash(data: bytes) -> str:
    """Full SHA-256 hex digest for storage in ImageRef."""
    return hashlib.sha256(data).hexdigest()


def _guess_extension(url: str, content_type: str | None = None) -> str:
    """Determine file extension from URL path or Content-Type header.

    Args:
        url: The image URL.
        content_type: Value of the Content-Type response header.

    Returns:
        File extension including the dot (e.g. ".jpg").
    """
    path = urlparse(url).path.lower()
    for ext in IMAGE_EXTENSIONS:
        if path.endswith(ext):
            return ext
    if content_type:
        ct = content_type.lower()
        if "jpeg" in ct or "jpg" in ct:
            return ".jpg"
        if "png" in ct:
            return ".png"
        if "webp" in ct:
            return ".webp"
        if "gif" in ct:
            return ".gif"
        if "avif" in ct:
            return ".avif"
    return ".jpg"


async def scroll_and_wait(page: Page, pause: float = 1.5) -> None:
    """Scroll the page in viewport-height increments to trigger lazy loading.

    Most e-commerce sites use intersection observers to lazy-load images.
    This function scrolls the full page height, pausing between steps to
    let images load, then scrolls back to the top.

    Args:
        page: Playwright page instance.
        pause: Seconds to wait between scroll steps.
    """
    height = await page.evaluate("document.body.scrollHeight")
    viewport_h = await page.evaluate("window.innerHeight")
    position = 0
    while position < height:
        position += viewport_h
        await page.evaluate(f"window.scrollTo(0, {position})")
        await asyncio.sleep(pause)
        height = await page.evaluate("document.body.scrollHeight")
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(1)


async def discover_page_images(
    page: Page,
    min_width: int = 100,
    prefer_resolution: str = "largest",
) -> list[ImageCandidate]:
    """Extract image candidates from the current page DOM.

    Uses ``img.currentSrc`` (the browser's resolved srcset choice) rather
    than parsing srcset strings, which avoids breakage on CDN URLs that
    contain commas in query parameters.

    When the same base image appears at multiple CDN sizes (e.g. wid=72
    and wid=1080), only the preferred resolution is kept. This prevents
    downloading both thumbnails and full-res versions of the same image.

    Should be called **after** ``scroll_and_wait()`` so that lazy-loaded
    images have resolved their real URLs.

    Args:
        page: Playwright page instance (already navigated and scrolled).
        min_width: Minimum naturalWidth to include. Filters tracking pixels
            and tiny icons/swatches.
        prefer_resolution: Resolution strategy for multi-size images.
            ``"largest"`` (default), ``"smallest"``, or a width like ``"1080"``.

    Returns:
        List of ImageCandidate objects, deduplicated by URL.
    """
    raw = await page.evaluate(
        """(minWidth) => {
        const results = [];
        const seen = new Set();

        function add(url, alt, w, h, sourceType) {
            if (!url || url.startsWith('data:') || url.startsWith('blob:') || seen.has(url)) return;
            seen.add(url);
            results.push({url, alt: alt || '', width: w || 0, height: h || 0, sourceType});
        }

        // img tags — use currentSrc (browser's resolved choice from srcset)
        for (const img of document.querySelectorAll('img')) {
            const src = img.currentSrc || img.src;
            if (src && !src.startsWith('data:')) {
                const w = img.naturalWidth;
                const h = img.naturalHeight;
                if (w >= minWidth || w === 0) {
                    add(src, img.alt, w, h, 'img_src');
                }
            }
        }

        // picture > source — take the last (largest) srcset entry
        for (const source of document.querySelectorAll('picture source[srcset]')) {
            const parts = source.srcset.split(/,(?=\\s*https?:)/);
            if (parts.length > 0) {
                const last = parts[parts.length - 1].trim().split(/\\s+/)[0];
                const img = source.closest('picture')?.querySelector('img');
                add(last, img?.alt || '', 0, 0, 'picture_source');
            }
        }

        // og:image meta tag
        const og = document.querySelector('meta[property="og:image"]');
        if (og && og.content) add(og.content, 'og:image', 0, 0, 'og_image');

        // CSS background-image on visible elements (capped at 50)
        let bgCount = 0;
        for (const el of document.querySelectorAll('[style*="background-image"]')) {
            if (bgCount >= 50) break;
            const match = el.style.backgroundImage.match(/url\\(["']?(https?[^"')]+)["']?\\)/);
            if (match) {
                add(match[1], '', el.offsetWidth, el.offsetHeight, 'bg_image');
                bgCount++;
            }
        }

        return results;
    }""",
        min_width,
    )

    candidates = []
    for item in raw:
        if _is_tracking_pixel(item["url"]):
            continue
        candidates.append(
            ImageCandidate(
                url=item["url"],
                alt_text=item["alt"],
                width=item["width"],
                height=item["height"],
                source_type=item["sourceType"],
            )
        )

    # Dedup: when the same base image appears at multiple CDN sizes,
    # keep only the preferred resolution.
    candidates = _dedup_by_resolution(candidates, prefer=prefer_resolution)

    return candidates


async def download_images(
    candidates: list[ImageCandidate],
    dest_dir: Path,
    *,
    user_agent: str = "megamaid/0.1",
    max_bytes: int = 10 * 1024 * 1024,
    min_bytes: int = 1024,
    max_count: int = 50,
    concurrency: int = 8,
) -> list[ImageRef]:
    """Download image candidates with content-hash deduplication.

    Images are saved with content-hash filenames (16 hex chars + extension).
    If the same image appears multiple times (e.g. on different product
    pages), only one copy is written to disk.

    Args:
        candidates: Image candidates from ``discover_page_images()``.
        dest_dir: Directory to save image files to.
        user_agent: User-Agent header for download requests.
        max_bytes: Skip images larger than this (bytes).
        min_bytes: Skip images smaller than this (catches placeholders).
        max_count: Maximum number of images to download per call.
        concurrency: Number of parallel downloads.

    Returns:
        List of ImageRef objects for successfully downloaded images.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    results: list[ImageRef] = []
    hash_to_path: dict[str, Path] = {}

    to_download = candidates[:max_count]

    async def _download_one(
        client: httpx.AsyncClient, candidate: ImageCandidate
    ) -> ImageRef | None:
        url = candidate.url
        async with sem:
            try:
                resp = await client.get(url, follow_redirects=True, timeout=30.0)
                resp.raise_for_status()
                data = resp.content

                if len(data) > max_bytes or len(data) < min_bytes:
                    return None

                short_hash = _content_hash(data)
                full = _full_hash(data)

                if short_hash in hash_to_path:
                    return ImageRef(
                        source_url=url,
                        local_path=str(
                            hash_to_path[short_hash].relative_to(dest_dir.parent)
                        ),
                        content_hash=full,
                        alt_text=candidate.alt_text,
                        width=candidate.width or None,
                        height=candidate.height or None,
                    )

                ext = _guess_extension(url, resp.headers.get("content-type"))
                path = dest_dir / f"{short_hash}{ext}"
                path.write_bytes(data)
                hash_to_path[short_hash] = path

                return ImageRef(
                    source_url=url,
                    local_path=str(path.relative_to(dest_dir.parent)),
                    content_hash=full,
                    alt_text=candidate.alt_text,
                    width=candidate.width or None,
                    height=candidate.height or None,
                )
            except Exception as e:
                logger.warning(f"Image download failed: {url[:80]} — {e}")
                return None

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        follow_redirects=True,
    ) as client:
        tasks = [_download_one(client, c) for c in to_download]
        for ref in await asyncio.gather(*tasks):
            if ref is not None:
                results.append(ref)

    unique = len(hash_to_path)
    dupes = len(results) - unique
    total_bytes = sum(p.stat().st_size for p in hash_to_path.values())
    logger.info(
        f"Images: {len(results)} downloaded ({unique} unique, {dupes} duplicates, "
        f"{total_bytes / 1024 / 1024:.1f} MB)"
    )

    return results
