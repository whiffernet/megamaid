"""Abstract base scraper with rate limiting, retry, and screenshot-on-error.

All target scrapers inherit from BaseScraper and implement the single
scrape() method. The base class handles everything else: browser setup,
polite rate limiting, exponential-backoff retries, and debug screenshots
when something goes sideways.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright

from .models import ScrapedDoc

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "megamaid/0.1 (+https://github.com/whiffernet/megamaid) "
    "Mozilla/5.0 (compatible; Chromium/131)"
)


class BaseScraper(ABC):
    """Abstract base class for target scrapers.

    Provides rate limiting, retry logic, screenshot-on-error, and a
    consistent interface for all target implementations.

    Attributes:
        target_name: Short identifier for this target (e.g. "books_toscrape").
        base_url: Root URL for the target website.
        rate_limit_seconds: Minimum delay between page navigations.
        user_agent: User-Agent header sent on every request.
    """

    target_name: str = ""
    base_url: str = ""
    rate_limit_seconds: float = 2.0
    user_agent: str = DEFAULT_USER_AGENT

    # Content cleaning (opt-in per target)
    clean_content: bool = False  # strip nav/ads/boilerplate from content_md

    # Image download configuration (opt-in per target)
    download_images: bool = False
    image_max_bytes: int = 10 * 1024 * 1024  # 10 MB per image
    image_min_bytes: int = 1024  # 1 KB — skip empty placeholder responses
    image_prefer_resolution: str = "largest"  # "largest", "smallest", or e.g. "1080"
    image_max_per_doc: int = 50
    image_min_width: int = 100  # skip tracking pixels and swatches
    image_concurrency: int = 8

    # Cookie consent dismissal (enabled by default)
    dismiss_consent: bool = True

    def __init__(self, debug_dir: Path | None = None) -> None:
        """Initialize the scraper.

        Args:
            debug_dir: Directory for error screenshots. None disables screenshots.
        """
        self._debug_dir = debug_dir
        self._last_request_time: float = 0.0
        self._consent_dismissed: bool = False
        if self._debug_dir:
            self._debug_dir.mkdir(parents=True, exist_ok=True)

    async def run(
        self, browser: Browser, max_items: int | None = None
    ) -> list[ScrapedDoc]:
        """Scrape all documents from this target.

        Args:
            browser: A Playwright browser instance.
            max_items: Optional cap for dry-runs.

        Returns:
            List of scraped documents.
        """
        page = await browser.new_page()
        page.set_default_timeout(30000)
        await page.set_extra_http_headers({"User-Agent": self.user_agent})

        try:
            docs = await self.scrape(page, max_items=max_items)
            logger.info(f"[{self.target_name}] Scraped {len(docs)} documents")
            return docs
        except Exception:
            logger.exception(f"[{self.target_name}] Scrape failed")
            raise
        finally:
            await page.close()

    async def _rate_limit(self) -> None:
        """Wait to honor rate_limit_seconds between requests.

        Can be called from any async context — does not require a
        Playwright page. Useful for targets that call APIs directly
        via httpx instead of navigating with Playwright. Updates
        ``_last_request_time`` after the wait so the next call
        measures from the right point.
        """
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < self.rate_limit_seconds:
            await asyncio.sleep(self.rate_limit_seconds - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def _fetch_json(
        self,
        client: "httpx.AsyncClient",
        url: str,
        retries: int = 3,
    ) -> dict | list | None:
        """Fetch a JSON endpoint with rate limiting and retry.

        Convenience method for REST/JSON API targets that use httpx
        instead of Playwright. Handles rate limiting, retries with
        exponential backoff, and Retry-After headers.

        Args:
            client: An httpx.AsyncClient instance.
            url: URL to fetch.
            retries: Number of retry attempts.

        Returns:
            Parsed JSON (dict or list), or None if all retries fail.
        """
        import httpx as _httpx

        for attempt in range(retries):
            await self._rate_limit()
            try:
                resp = await client.get(url, timeout=30.0)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 2**attempt))
                    logger.warning(
                        f"[{self.target_name}] Rate limited (429), "
                        f"waiting {retry_after}s"
                    )
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp.json()
            except _httpx.HTTPStatusError as e:
                logger.warning(
                    f"[{self.target_name}] HTTP {e.response.status_code} "
                    f"on attempt {attempt + 1}/{retries} for {url[:80]}"
                )
                if attempt < retries - 1:
                    await asyncio.sleep(2**attempt)
            except Exception as e:
                logger.warning(
                    f"[{self.target_name}] Fetch attempt {attempt + 1}/{retries} "
                    f"failed for {url[:80]}: {e}"
                )
                if attempt < retries - 1:
                    await asyncio.sleep(2**attempt)
        return None

    async def _navigate(self, page: Page, url: str, retries: int = 3) -> None:
        """Navigate to a URL with rate limiting and retry logic.

        Args:
            page: Playwright page instance.
            url: URL to navigate to.
            retries: Number of retry attempts on failure.

        Raises:
            Exception: If all retries fail. Last error is re-raised.
        """
        await self._rate_limit()

        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                self._last_request_time = asyncio.get_event_loop().time()
                if self.dismiss_consent and not self._consent_dismissed:
                    await self._dismiss_consent(page)
                    self._consent_dismissed = True
                return
            except Exception as e:
                last_error = e
                logger.warning(
                    f"[{self.target_name}] Navigate attempt {attempt + 1}/{retries} "
                    f"failed for {url}: {e}"
                )
                if attempt < retries - 1:
                    await asyncio.sleep(2**attempt)

        await self._screenshot_on_error(page, url)
        assert last_error is not None
        raise last_error

    async def _dismiss_consent(self, page: Page) -> None:
        """Attempt to dismiss cookie consent banners. Fails silently.

        Uses a single combined selector to find any visible accept button
        from major consent management platforms (OneTrust, Cookiebot,
        Osano, generic patterns). Gives up after 2 seconds total if no
        banner is found — does not add latency for sites without banners.
        """
        combined = ", ".join(
            [
                "#onetrust-accept-btn-handler",
                "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                ".cc-compliance .cc-btn",
                '[data-testid="cookie-policy-manage-dialog-btn-accept"]',
            ]
        )
        try:
            btn = await page.wait_for_selector(combined, timeout=2000)
            if btn and await btn.is_visible():
                await btn.click()
                logger.info(f"[{self.target_name}] Dismissed consent banner")
                return
        except Exception:
            pass

        # Fallback: text-based selectors (can't combine with CSS selectors)
        for text in ("Accept All", "Accept", "I agree", "Got it"):
            try:
                btn = page.get_by_text(text, exact=True).first
                if await btn.is_visible():
                    await btn.click()
                    logger.info(f"[{self.target_name}] Dismissed consent banner")
                    return
            except Exception:
                continue

    async def _screenshot_on_error(self, page: Page, context: str) -> None:
        """Save a screenshot for debugging when an error occurs.

        Args:
            page: Playwright page instance.
            context: Description of what was being attempted (used in filename).
        """
        if not self._debug_dir:
            return
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safe_ctx = context.replace("/", "_").replace(":", "")[:80]
            path = self._debug_dir / f"{self.target_name}_{ts}_{safe_ctx}.png"
            await page.screenshot(path=str(path), full_page=True)
            logger.info(f"[{self.target_name}] Error screenshot saved: {path}")
        except Exception:
            logger.warning(f"[{self.target_name}] Failed to save error screenshot")

    @abstractmethod
    async def scrape(
        self, page: Page, max_items: int | None = None
    ) -> list[ScrapedDoc]:
        """Scrape all documents from the target.

        Implementations are responsible for:
        - URL discovery (sitemap, pagination, load-more, JSON API, etc.)
        - Per-page parsing into ScrapedDoc models
        - Honoring max_items for dry-runs

        Use self._navigate(page, url) for every page load — it handles
        rate limiting and retries.

        Args:
            page: Playwright page instance.
            max_items: Optional cap on number of documents to return.

        Returns:
            List of ScrapedDoc models.
        """
        raise NotImplementedError


async def create_browser() -> tuple:
    """Launch a headless Chromium browser.

    Returns:
        Tuple of (playwright_instance, browser). Caller is responsible
        for calling await browser.close() and await pw.stop().
    """
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-gpu"],
    )
    return pw, browser
