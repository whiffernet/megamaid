# Pattern: Auth Wall

> _"What's the combination?"_ _"One, two, three, four, five."_ _"That's the stupidest combination I've ever heard!"_
>
> Don't guess combinations. Let the user log in.

## When to use

The content you want is behind a login. You have **the user's own
credentials**, freely given, for their own account. You do **not**
have someone else's credentials, and you are not guessing.

If the user doesn't have legitimate access — stop. This skill won't
help you here, and neither will I.

## Approach: manual login + storage_state reuse

Playwright can save and replay a browser session. The user logs in
once, by hand, in a headed browser. We save the cookies/localStorage
to `storage_state.json`. Subsequent runs load that state and start
already logged in. No credentials ever touch your code.

### One-time setup script

Write this as `scripts/login_once.py` in the user's project:

```python
"""Manual one-time login. Saves session to storage_state.json."""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

STATE_PATH = Path("storage_state.json")
LOGIN_URL = "https://example.com/login"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(LOGIN_URL)
        print("Log in manually in the browser window, then press Enter here.")
        input()
        await context.storage_state(path=STATE_PATH)
        print(f"Saved session to {STATE_PATH}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
```

Run it once: `python scripts/login_once.py`. A browser window opens;
the user logs in; they press Enter; the session is saved.

### In the target

Override `run()` to use the saved state:

```python
from pathlib import Path
from playwright.async_api import Browser, Page
from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc

STATE_PATH = Path("storage_state.json")


class AuthedTarget(BaseScraper):
    target_name = "example_authed"
    base_url = "https://example.com"
    rate_limit_seconds = 2.0

    async def run(self, browser: Browser, max_items=None):
        if not STATE_PATH.exists():
            raise RuntimeError(
                "No storage_state.json — run scripts/login_once.py first."
            )
        context = await browser.new_context(storage_state=str(STATE_PATH))
        page = await context.new_page()
        try:
            return await self.scrape(page, max_items=max_items)
        finally:
            await context.close()

    async def scrape(self, page: Page, max_items=None):
        await self._navigate(page, f"{self.base_url}/my-data")
        # ... parse as normal; you're logged in.
        return []
```

## Session expiry

Sessions expire. When yours does, the site redirects you to a login
page and your selectors start failing. Detect it:

```python
if "login" in page.url.lower() or await page.query_selector("input[type=password]"):
    raise RuntimeError("Session expired — re-run scripts/login_once.py")
```

Surface that error clearly so the user knows what to do — don't retry
silently.

## Hard no's

- **No credential guessing.** No password lists, no username enumeration.
- **No credentials in source control.** `storage_state.json` goes in
  `.gitignore` immediately.
- **No shared accounts** unless the account's owner explicitly consents.
- **No CAPTCHA bypass.** If the login form has a CAPTCHA, the user
  solves it once in the headed window. That's it. If the post-login
  content also throws CAPTCHAs, the site is telling you to stop.
- **Check the ToS.** Some sites explicitly forbid automated access even
  with a valid account. Respect that.
