"""Shared discovery strategies for product URL extraction.

Composable functions that target classes call to discover product URLs
without reimplementing pagination, modal dismissal, or link extraction
for each site.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from .constants import DEFAULT_USER_AGENT

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

#: Access mode this module provides beyond the default. Declared, not hidden:
#: `tests/test_honest_identification.py` permits browser-shaped headers only in
#: modules listed in its capability registry, so a new bypass cannot be added
#: without appearing in the table users read.
ACCESS_MODE = "browser-headers"

#: A browser-shaped header set. The `Sec-Fetch-*` values are what matter: they
#: describe a top-level navigation, which is what anti-bot systems check for when
#: deciding whether a request came from a page load or a script.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


@asynccontextmanager
async def browser_headers_client(
    base_url: str,
    *,
    timeout: float = 30.0,
    extra_headers: dict | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client shaped like a browser navigation, with its session warmed.

    This is the `browser-headers` access mode. It does two things the default
    does not: sends the header set above, and fetches `base_url` first so the
    site can set the cookies it expects a real visitor to already have. The
    warmup is the load-bearing half — the headers alone are frequently not
    enough, because the cookie is what the next request is checked against.

    **Measured reachability, 2026-08-02.** Three modes, one target URL each.
    The `browser` column is playwright-stealth under xvfb — see
    `skills/megamaid/patterns/image_downloads.md`, which is the strongest mode
    megamaid has and the one this module does *not* provide:

    ================  ====================  ==================  =================
    site              `identify`            `browser-headers`   `browser`
    ================  ====================  ==================  =================
    williams-sonoma   read timeout, 2x60s   reachable, 760 KB   not needed
    american eagle    reachable, 1045 KB    no gain             not needed
    lululemon         read timeout, 2x60s   HTTP 400            homepage 200 [1]
    walmart           -> /blocked           -> /blocked         homepage 200 [2]
    ================  ====================  ==================  =================

    [1] Full Akamai clearance (`_abck`, `ak_bmsc`, `bm_sv`, `bm_sz`). The site's
        GraphQL endpoint answers `GE401001 Bad Request` to the query document
        captured months ago — an application-layer rejection, not a block. The
        bot wall is passed; the saved query needs recapturing.
    [2] Homepage reachable at 439 KB with its real title. Browse pages still
        redirect to a page titled "Robot or human?", so the homepage warmup no
        longer buys catalog access the way it did when this helper was written.

    An earlier version of this docstring claimed Walmart worked outright. Anti-bot
    deployments move: treat this as a dated measurement, not a guarantee, and run
    `megamaid recon` against a target rather than trusting it.

    This mode is not a default. It identifies as a browser, so a run using it is
    recorded as such in the manifest — see `ACCESS_MODE`.

    Args:
        base_url: Site root, fetched once to seed session cookies.
        timeout: Per-request timeout in seconds.
        extra_headers: Merged over the browser defaults.

    Yields:
        A warmed `httpx.AsyncClient`.

    Example::

        async with browser_headers_client("https://www.williams-sonoma.com") as client:
            resp = await client.get("https://www.williams-sonoma.com/shop/cookware/")
    """
    headers = {**_BROWSER_HEADERS, **(extra_headers or {})}
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=timeout,
    ) as client:
        try:
            await client.get(base_url)
            logger.info("browser_headers_client: session warmed for %s", base_url)
        except Exception as exc:
            # Non-fatal: some sites serve the target fine without a seeded
            # cookie, and failing here would remove a mode that still works.
            logger.warning("browser_headers_client: warmup failed for %s: %s", base_url, exc)
        yield client


#: The pre-0.10 name. Kept so scaffolded projects written against it keep working
#: — they hold a frozen copy of this module and `megamaid upgrade` will not
#: replace a file the user has edited.
stealth_http_client = browser_headers_client


@dataclass
class SitemapProduct:
    """A product discovered from a sitemap, with optional image URLs.

    Attributes:
        url: Canonical product page URL from the sitemap <loc> tag.
        image_urls: Image URLs extracted from <image:image> tags in the
            same <url> entry. Empty if the sitemap has no image extensions.
    """

    url: str
    image_urls: list[str] = field(default_factory=list)


async def dismiss_modals(page: Page, timeout: int = 3000) -> None:
    """Dismiss common popups: cookie banners, welcome modals, email signups.

    Tries ID-based selectors first (fast), then text-based fallbacks.
    Fails silently if no modals are found.

    Args:
        page: Playwright page instance.
        timeout: Max wait for the first selector batch (ms).
    """
    # ID/class selectors for known CMPs and promo modals
    id_selectors = ", ".join(
        [
            "#onetrust-accept-btn-handler",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            ".cc-compliance .cc-btn",
            'dialog button[aria-label="Close"]',
            '[data-testid="modal-close"]',
            '[class*="modal-close"]',
            '[class*="popup-close"]',
            'button[aria-label="close"]',
        ]
    )
    try:
        btn = await page.wait_for_selector(id_selectors, timeout=timeout)
        if btn and await btn.is_visible():
            await btn.click()
            logger.info("Dismissed modal (ID selector)")
            await asyncio.sleep(0.5)
            return
    except Exception:
        pass

    # Text-based fallbacks
    for text in (
        "Accept All",
        "Accept",
        "No Thanks",
        "Close",
        "Not Now",
        "Maybe Later",
    ):
        try:
            loc = page.get_by_text(text, exact=True).first
            if await loc.is_visible():
                await loc.click()
                logger.info(f"Dismissed modal (text: {text})")
                await asyncio.sleep(0.5)
                return
        except Exception:
            continue


async def detect_pagination_style(page: Page) -> dict:
    """Inspect the current page for pagination signals.

    Args:
        page: Playwright page on a product listing page.

    Returns:
        Dict with keys: style ("page_param", "load_more", "infinite_scroll",
        "none"), param_name (e.g. "page"), next_url (if found).
    """
    result = await page.evaluate("""() => {
        const info = {style: 'none', paramName: null, nextUrl: null, totalText: null};

        // Check for ?page= or ?p= links
        const pageLinks = document.querySelectorAll(
            'a[href*="?page="], a[href*="&page="], a[href*="?p="], a[href*="&p="], ' +
            'a[href*="?pageNumber="], a[href*="&pageNumber="]'
        );
        if (pageLinks.length > 0) {
            info.style = 'page_param';
            const href = pageLinks[0].href;
            if (href.includes('pageNumber=')) info.paramName = 'pageNumber';
            else if (href.includes('page=')) info.paramName = 'page';
            else if (href.includes('p=')) info.paramName = 'p';
            // Find "next" link
            for (const a of pageLinks) {
                if (a.textContent.trim() === '2' || a.getAttribute('aria-label')?.includes('Next')
                    || a.getAttribute('rel') === 'next') {
                    info.nextUrl = a.href;
                    break;
                }
            }
        }

        // Check for load more / view more buttons
        if (info.style === 'none') {
            const buttons = document.querySelectorAll('button, a');
            for (const b of buttons) {
                const text = b.textContent.trim().toLowerCase();
                if (text.includes('load more') || text.includes('view more') ||
                    text.includes('show more')) {
                    info.style = 'load_more';
                    break;
                }
            }
        }

        // Check for item count text
        const bodyText = document.body.innerText;
        const countMatch = bodyText.match(/([\\d,]+)\\s*(?:items|products|results|styles)/i);
        if (countMatch) info.totalText = countMatch[0];

        return info;
    }""")
    return result


async def extract_product_links(
    page: Page,
    patterns: list[str] | None = None,
) -> list[str]:
    """Extract product detail links from the current page.

    Args:
        page: Playwright page on a product listing page.
        patterns: URL substrings that identify product links.
            Defaults to common e-commerce patterns.

    Returns:
        Deduplicated list of product URLs.
    """
    if patterns is None:
        patterns = ["/p/", "/product/", "/products/", "/dp/", "/ip/", "/pd/", "/pdp/"]

    patterns_js = ",".join(f'"{p}"' for p in patterns)
    urls = await page.evaluate(f"""() => {{
        const patterns = [{patterns_js}];
        const links = new Set();
        for (const a of document.querySelectorAll('a[href]')) {{
            const href = a.href.split('?')[0].split('#')[0];
            for (const p of patterns) {{
                if (href.includes(p)) {{
                    links.add(href);
                    break;
                }}
            }}
        }}
        return Array.from(links);
    }}""")
    return urls


async def paginated_discovery(
    page: Page,
    start_url: str,
    *,
    navigate_fn=None,
    param_name: str = "page",
    max_pages: int = 100,
    product_patterns: list[str] | None = None,
) -> list[str]:
    """Discover product URLs by iterating ?page=N pagination.

    Args:
        page: Playwright page instance.
        start_url: The first page URL (page 1).
        navigate_fn: Async function to navigate (e.g. BaseScraper._navigate).
            If None, uses page.goto directly.
        param_name: Query parameter name for pagination (default "page").
        max_pages: Safety cap on pages to iterate.
        product_patterns: URL substrings for product links.

    Returns:
        Deduplicated list of product URLs.
    """
    from megamaid.images import scroll_and_wait

    all_urls: set[str] = set()

    for page_num in range(1, max_pages + 1):
        sep = "&" if "?" in start_url else "?"
        url = f"{start_url}{sep}{param_name}={page_num}" if page_num > 1 else start_url

        try:
            if navigate_fn:
                await navigate_fn(page, url)
            else:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            logger.warning(f"Pagination failed on page {page_num}: {e}")
            break

        await asyncio.sleep(2)
        await dismiss_modals(page, timeout=2000)
        await scroll_and_wait(page, pause=0.8)

        new_urls = await extract_product_links(page, product_patterns)
        before = len(all_urls)
        all_urls.update(new_urls)
        added = len(all_urls) - before

        logger.info(f"Page {page_num}: {len(new_urls)} links, {added} new (total: {len(all_urls)})")

        if added == 0 or len(new_urls) == 0:
            break

    return sorted(all_urls)


async def sitemap_discovery(
    base_url: str,
    product_patterns: list[str] | None = None,
    *,
    user_agent: str | None = None,
    extract_images: bool = False,
) -> list[str] | list[SitemapProduct]:
    """Discover product URLs from sitemap.xml.

    Fetches the sitemap index and all child sitemaps, filtering URLs that
    match the given product patterns. Optionally extracts ``<image:image>``
    tags embedded in each ``<url>`` entry.

    Args:
        base_url: Site root URL (e.g. https://example.com).
        product_patterns: URL substrings to filter product pages.
        user_agent: Custom User-Agent header for sitemap requests.
            Defaults to ``megamaid.constants.DEFAULT_USER_AGENT`` if not specified.
        extract_images: If True, also parse ``<image:image>`` tags and
            return ``SitemapProduct`` objects instead of plain URL strings.

    Returns:
        If ``extract_images`` is False: list of product URL strings.
        If ``extract_images`` is True: list of ``SitemapProduct`` objects
        containing both the product URL and any image URLs from the sitemap.
    """
    from xml.etree import ElementTree as ET

    if product_patterns is None:
        product_patterns = ["/p/", "/product/", "/products/", "/dp/", "/ip/", "/pd/"]

    if user_agent is None:
        user_agent = DEFAULT_USER_AGENT

    ns = {
        "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
        "image": "http://www.google.com/schemas/sitemap-image/1.1",
    }

    products: list[SitemapProduct] = []
    seen_urls: set[str] = set()

    headers = {"User-Agent": user_agent}

    def _parse(sm_url: str) -> None:
        try:
            resp = httpx.get(sm_url, timeout=30.0, follow_redirects=True, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"Sitemap fetch returned {resp.status_code} for {sm_url}")
                return
            root = ET.fromstring(resp.text)
            tag = root.tag.split("}", 1)[-1]
            if tag == "sitemapindex":
                for sm in root.findall("sm:sitemap", ns):
                    loc = sm.findtext("sm:loc", default="", namespaces=ns)
                    if loc:
                        _parse(loc)
            else:
                for u in root.findall("sm:url", ns):
                    loc = u.findtext("sm:loc", default="", namespaces=ns)
                    if not loc or loc in seen_urls:
                        continue
                    if not any(p in loc for p in product_patterns):
                        continue
                    seen_urls.add(loc)

                    image_urls: list[str] = []
                    if extract_images:
                        for img_el in u.findall("image:image", ns):
                            img_loc = img_el.findtext("image:loc", default="", namespaces=ns)
                            if img_loc:
                                image_urls.append(img_loc)

                    products.append(SitemapProduct(url=loc, image_urls=image_urls))
        except Exception as e:
            logger.warning(f"Sitemap parse error for {sm_url}: {e}")

    _parse(f"{base_url}/sitemap.xml")
    if not products:
        _parse(f"{base_url}/sitemap_index.xml")

    logger.info(
        f"Sitemap discovery: {len(products)} product URLs"
        + (f", {sum(len(p.image_urls) for p in products)} image URLs" if extract_images else "")
    )

    if extract_images:
        return products
    return [p.url for p in products]
