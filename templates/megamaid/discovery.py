"""Shared discovery strategies for product URL extraction.

Composable functions that target classes call to discover product URLs
without reimplementing pagination, modal dismissal, or link extraction
for each site.
"""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page

logger = logging.getLogger(__name__)


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
            btn = page.get_by_text(text, exact=True).first
            if await btn.is_visible():
                await btn.click()
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

        logger.info(
            f"Page {page_num}: {len(new_urls)} links, {added} new (total: {len(all_urls)})"
        )

        if added == 0 or len(new_urls) == 0:
            break

    return sorted(all_urls)


async def sitemap_discovery(
    base_url: str,
    product_patterns: list[str] | None = None,
) -> list[str]:
    """Discover product URLs from sitemap.xml.

    Args:
        base_url: Site root URL (e.g. https://example.com).
        product_patterns: URL substrings to filter product pages.

    Returns:
        List of product URLs found in the sitemap.
    """
    import httpx
    from xml.etree import ElementTree as ET

    if product_patterns is None:
        product_patterns = ["/p/", "/product/", "/products/", "/dp/", "/ip/", "/pd/"]

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls: set[str] = set()

    def _parse(sm_url: str) -> None:
        try:
            resp = httpx.get(sm_url, timeout=15.0, follow_redirects=True)
            if resp.status_code != 200:
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
                    if loc and any(p in loc for p in product_patterns):
                        urls.add(loc)
        except Exception as e:
            logger.warning(f"Sitemap parse error for {sm_url}: {e}")

    _parse(f"{base_url}/sitemap.xml")
    if not urls:
        _parse(f"{base_url}/sitemap_index.xml")

    logger.info(f"Sitemap discovery: {len(urls)} product URLs")
    return sorted(urls)
