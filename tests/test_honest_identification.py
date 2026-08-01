"""The shipped scaffold identifies itself; it does not impersonate a browser.

megamaid shipped a spoofed Chrome User-Agent as `sitemap_discovery`'s default —
the function behind `sitemap_crawl`, the skill's first-preference pattern —
while SKILL.md and references/troubleshooting.md both forbade exactly that in
the scaffold. Prohibition in prose did not prevent the drift, so it is enforced
here.

The one legitimate `Mozilla/5.0` in the codebase is inside DEFAULT_USER_AGENT,
where it is a conventional compatibility token preceded by megamaid's own name
and repo URL. That is identification, not impersonation.
"""

import pathlib
import sys

import httpx

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid.constants import DEFAULT_USER_AGENT  # noqa: E402
from megamaid.discovery import sitemap_discovery  # noqa: E402

_SCANNED_DIRS = ("src", "templates")
_HONEST_MARKER = "megamaid/"


def _shipped_python_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Every .py file the project ships to users."""
    files: list[pathlib.Path] = []
    for directory in _SCANNED_DIRS:
        files.extend(sorted((repo_root / directory).rglob("*.py")))
    return files


def test_no_spoofed_browser_user_agent_in_shipped_runtime(repo_root):
    """No shipped module may claim to be a browser it is not.

    A line carrying `Mozilla/5.0` is allowed only when it also carries
    `megamaid/`, i.e. it is the honest UA's compatibility token.
    """
    offenders: list[str] = []
    honest_hits = 0

    for path in _shipped_python_files(repo_root):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "Mozilla/5.0" not in line:
                continue
            if _HONEST_MARKER in line:
                honest_hits += 1
                continue
            offenders.append(f"{path.relative_to(repo_root)}:{lineno}: {line.strip()}")

    assert honest_hits >= 1, (
        "the scan found no honest User-Agent anywhere — the pattern is probably "
        "broken, which would make this test pass while checking nothing"
    )
    assert not offenders, "spoofed browser User-Agent in the shipped runtime:\n  " + "\n  ".join(
        offenders
    )


def test_sitemap_discovery_sends_the_honest_user_agent(monkeypatch):
    """The default must be the honest UA, asserted on the wire, not by reading source.

    A source-level check would pass if the constant were right but never sent.
    """
    seen: dict[str, str] = {}

    def fake_get(url, **kwargs):
        seen["user_agent"] = kwargs.get("headers", {}).get("User-Agent", "")
        return httpx.Response(200, text="<urlset></urlset>", request=httpx.Request("GET", url))

    monkeypatch.setattr("megamaid.discovery.httpx.get", fake_get)

    import asyncio

    asyncio.run(sitemap_discovery("https://example.com"))

    assert seen["user_agent"] == DEFAULT_USER_AGENT, (
        f"sitemap_discovery sent {seen['user_agent']!r}, expected the honest {DEFAULT_USER_AGENT!r}"
    )
    assert "megamaid/" in seen["user_agent"]
