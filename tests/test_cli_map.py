"""`map` is documented browser-free, so it must survive having no browser.

SKILL.md and README.md both place `map` in the URL-scoped group — the commands
that run from the launcher's own state venv, which is built from the light
`[mcp,cli]` extras and deliberately installs no browser. But `map`'s Layer 2
link crawl fires whenever the sitemap yields fewer than `--max` URLs (default
500, i.e. almost always) and imports playwright, so from the launcher the
command ended in a ModuleNotFoundError traceback *after* the sitemap work was
already done, discarding results it had in hand.

The fix keeps `map` URL-scoped and makes the documentation true: a missing
browser degrades the command to its sitemap results instead of failing it.
Keeping it URL-scoped is the right side of that trade — `map` needs no
project, no `targets/` package and no `staging/` directory, so filing it under
the project-scoped commands would swap one false statement for another.
"""

import importlib
import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/a.html</loc></url>
  <url><loc>https://example.test/b.html</loc></url>
  <url><loc>https://example.test/c.html</loc></url>
</urlset>
"""


@pytest.fixture(scope="module")
def cli_module():
    """megamaid.cli imported with src/ on sys.path."""
    pytest.importorskip("click")
    pytest.importorskip("httpx")
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    return importlib.import_module("megamaid.cli")


class _FakeResponse:
    """Minimal stand-in for the httpx.Response attributes `map` touches."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _fake_get(url, **kwargs):
    """Serve a 3-URL sitemap and 404 everything else."""
    if url.endswith("/sitemap.xml"):
        return _FakeResponse(200, SITEMAP_XML)
    return _FakeResponse(404)


def _combined_output(result) -> str:
    """stdout plus stderr, across click versions.

    click < 8.2 folds stderr into `output` and raises on `.stderr`; 8.2+
    separates them. The assertions below care about both streams.
    """
    text = result.output
    try:
        text += result.stderr
    except ValueError:
        pass
    return text


def _run_map(cli_module, monkeypatch, playwright_available: bool):
    import httpx
    from click.testing import CliRunner

    monkeypatch.setattr(httpx, "get", _fake_get)
    if not playwright_available:
        # A None entry in sys.modules makes the import raise ImportError
        # ("import of X halted; None in sys.modules") — the same exception
        # class a genuinely absent playwright raises, without needing an
        # environment that lacks it.
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    return CliRunner().invoke(cli_module.cli, ["map", "https://example.test/"])


def test_map_returns_sitemap_results_when_there_is_no_browser(cli_module, monkeypatch):
    """The regression: exit 0 with the URLs, not a traceback with nothing."""
    result = _run_map(cli_module, monkeypatch, playwright_available=False)

    assert result.exit_code == 0, (
        f"map exited {result.exit_code} without a browser.\n{_combined_output(result)}"
    )
    for path in ("a.html", "b.html", "c.html"):
        assert f"https://example.test/{path}" in result.output, (
            f"sitemap URL {path} was discovered but not emitted:\n{result.output}"
        )


def test_map_explains_the_skipped_crawl_rather_than_failing_silently(cli_module, monkeypatch):
    """Degrading quietly would be its own defect — the user must learn why."""
    output = _combined_output(_run_map(cli_module, monkeypatch, playwright_available=False))

    assert "Link crawl skipped" in output
    assert "scraper" in output, "the message must name the extra that provides the browser"
    assert "still complete, valid output" in output


def test_the_missing_browser_is_actually_simulated(cli_module, monkeypatch):
    """Negative control for the fixture, not for the fix.

    playwright happens to be importable in some dev environments. If the
    sys.modules trick above ever stopped producing an ImportError, the two
    tests above would pass by exercising the *browser present* path and
    certify nothing. This asserts the simulated absence really bites.
    """
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    with pytest.raises(ImportError):
        from playwright.async_api import async_playwright  # noqa: F401
