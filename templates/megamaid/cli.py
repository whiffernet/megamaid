"""megamaid CLI entry point.

Commands:
    suck    — scrape the target (honors robots.txt, manifest-tracked)
    status  — show the last run's manifest summary
    diff    — compare the last two runs
    init    — print scaffold instructions

> "Switch to ludicrous speed? No. Regular speed is fine."
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import click

from .base import create_browser
from .manifest import Manifest, ManifestItem, compute_delta, get_latest_manifest
from .models import slug_from_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("megamaid")

STAGING_DIR = Path("staging")


def _load_target():
    """Import the user's target class.

    Convention: the project has exactly one module under targets/ that
    defines a BaseScraper subclass. If multiple subclasses are found,
    raises an error listing them so the user can remove extras.
    """
    from importlib import import_module
    from pkgutil import iter_modules

    import targets

    from .base import BaseScraper

    found: list[tuple[str, type]] = []
    for _, name, _ in iter_modules(targets.__path__):
        module = import_module(f"targets.{name}")
        for attr in dir(module):
            obj = getattr(module, attr)
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseScraper)
                and obj is not BaseScraper
            ):
                found.append((f"targets.{name}.{attr}", obj))
    if not found:
        raise RuntimeError("No BaseScraper subclass found under targets/")
    if len(found) > 1:
        names = ", ".join(f for f, _ in found)
        raise RuntimeError(
            f"Multiple BaseScraper subclasses found: {names}. "
            f"Remove extras so only one target remains."
        )
    return found[0][1]


def _check_robots(url: str, user_agent: str) -> tuple[bool, str]:
    """Check whether robots.txt permits scraping the given URL.

    Works around a CPython RobotFileParser bug where ``Crawl-delay``
    or other extension directives cause ``can_fetch`` to return False
    for all paths.  We strip non-standard lines before parsing.

    Returns:
        (allowed, reason). allowed is False if the site explicitly
        disallows the path for the given User-Agent.
    """
    import urllib.request

    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return True, f"could not fetch robots.txt ({e}), proceeding"

    # Strip non-standard directives that confuse RobotFileParser
    clean_lines = []
    for line in raw.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith(("crawl-delay", "request-rate", "host:")):
            continue
        clean_lines.append(line)

    rp = RobotFileParser()
    rp.parse(clean_lines)

    if rp.can_fetch(user_agent, url):
        return True, "robots.txt permits this path"
    return False, f"robots.txt disallows {parsed.path} for {user_agent}"


@click.group()
def cli() -> None:
    """megamaid — consume a planet's content, politely."""


@cli.command()
@click.argument("url")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text).",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(),
    default=None,
    help="Write JSON report to file.",
)
@click.option("--user-agent", default=None, help="Override User-Agent.")
@click.option(
    "--timeout", type=float, default=10.0, help="Per-request timeout (seconds)."
)
@click.option(
    "--quiet", is_flag=True, help="Suppress progress, print only final report."
)
def recon(
    url: str,
    fmt: str,
    output_path: str | None,
    user_agent: str | None,
    timeout: float,
    quiet: bool,
) -> None:
    """Recon a target URL and recommend a scraping pattern.

    Probes robots.txt, sitemaps, anti-bot systems, structured data, and
    API endpoints with 3-6 HTTP requests. Outputs a pattern recommendation
    with confidence level.
    """
    from .recon import run_recon, format_text_report, format_json_report
    from .base import DEFAULT_USER_AGENT

    ua = user_agent or DEFAULT_USER_AGENT
    if not quiet:
        click.echo(f"Recon: {url} ...", err=True)

    report = asyncio.run(run_recon(url, user_agent=ua, timeout=timeout))

    if fmt == "json":
        click.echo(format_json_report(report))
    else:
        click.echo(format_text_report(report))

    if output_path:
        Path(output_path).write_text(format_json_report(report))
        if not quiet:
            click.echo(f"Report written to {output_path}", err=True)


@cli.command()
@click.option("--max", "max_items", type=int, default=None, help="Cap items (dry-run).")
@click.option(
    "--ignore-robots",
    is_flag=True,
    default=False,
    help="Skip robots.txt check. You must have the site owner's permission.",
)
@click.option(
    "--staging",
    type=click.Path(path_type=Path),
    default=STAGING_DIR,
    help="Staging directory (default: ./staging).",
)
def suck(max_items: int | None, ignore_robots: bool, staging: Path) -> None:
    """Scrape the target. Writes raw/, docs/, and manifest.json."""
    target_cls = _load_target()
    target = target_cls()

    if not ignore_robots:
        allowed, reason = _check_robots(target.base_url, target.user_agent)
        if not allowed:
            click.echo(
                f"[refused] {reason}\n"
                f"The site is asking you not to scrape. If you have written\n"
                f"permission from the owner, re-run with --ignore-robots.",
                err=True,
            )
            sys.exit(2)
        logger.info(f"robots.txt: {reason}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = staging / target.target_name / run_id
    raw_dir = run_dir / "raw"
    docs_dir = run_dir / "docs"
    debug_dir = run_dir / "debug"
    images_dir = run_dir / "images"
    for d in (raw_dir, docs_dir, debug_dir, images_dir):
        d.mkdir(parents=True, exist_ok=True)

    manifest = Manifest(
        run_id=run_id,
        target=target.target_name,
        started_at=datetime.now(timezone.utc).isoformat(),
        status="running",
    )
    manifest_path = run_dir / "manifest.json"
    manifest.save(manifest_path)

    async def _run() -> None:
        pw, browser = await create_browser()
        target._debug_dir = debug_dir
        target._images_dir = images_dir
        try:
            docs = await target.run(browser, max_items=max_items)
        finally:
            await browser.close()
            await pw.stop()

        # Post-process: clean content if opted in
        if target.clean_content:
            from trafilatura import extract as _traf_extract

            for doc in docs:
                if doc.raw_path:
                    raw_file = run_dir / doc.raw_path
                    if raw_file.exists():
                        html = raw_file.read_text(errors="replace")
                        cleaned = _traf_extract(html, output_format="markdown")
                        if cleaned:
                            doc.content_md = cleaned

        for doc in docs:
            if not doc.id:
                doc.id = slug_from_url(doc.source_url)
            doc.compute_identity_hash()
            doc_file = docs_dir / f"{doc.id}.json"
            doc_file.write_text(doc.model_dump_json(indent=2))
            manifest.items.append(
                ManifestItem(
                    id=doc.id,
                    source_url=doc.source_url,
                    doc_file=str(doc_file.relative_to(run_dir)),
                    raw_file=doc.raw_path,
                    identity_hash=doc.identity_hash,
                    scrape_status="success",
                    image_count=len(doc.images),
                    image_hashes=[img.content_hash for img in doc.images],
                )
            )

    try:
        asyncio.run(_run())
        previous = get_latest_manifest(staging, target.target_name)
        manifest.items = compute_delta(manifest.items, previous)
        manifest.recompute_stats()
        manifest.status = "completed"
    except Exception:
        logger.exception("Scrape failed")
        manifest.status = "failed"
        raise
    finally:
        manifest.completed_at = datetime.now(timezone.utc).isoformat()
        manifest.save(manifest_path)

    click.echo(json.dumps(manifest.stats.to_dict(), indent=2))
    total_images = sum(item.image_count for item in manifest.items)
    if total_images:
        unique_hashes = set()
        for item in manifest.items:
            unique_hashes.update(item.image_hashes)
        image_bytes = sum(f.stat().st_size for f in images_dir.iterdir() if f.is_file())
        click.echo(
            f"Images: {total_images} ({len(unique_hashes)} unique, "
            f"{image_bytes / 1024 / 1024:.1f} MB)"
        )
    click.echo(f"Manifest: {manifest_path}")


@cli.command()
@click.option("--staging", type=click.Path(path_type=Path), default=STAGING_DIR)
def status(staging: Path) -> None:
    """Show the latest completed run's stats."""
    if not staging.exists():
        click.echo("No staging directory yet. Run `megamaid suck` first.")
        return
    for target_dir in sorted(staging.iterdir()):
        latest = get_latest_manifest(staging, target_dir.name)
        if latest is None:
            continue
        click.echo(f"\n== {target_dir.name} ==")
        click.echo(f"run_id: {latest.run_id}   status: {latest.status}")
        click.echo(json.dumps(latest.stats.to_dict(), indent=2))


@cli.command()
@click.option("--staging", type=click.Path(path_type=Path), default=STAGING_DIR)
def diff(staging: Path) -> None:
    """Show items new/changed/unchanged in the latest run."""
    for target_dir in sorted(staging.iterdir()):
        latest = get_latest_manifest(staging, target_dir.name)
        if latest is None:
            continue
        click.echo(f"\n== {target_dir.name} ({latest.run_id}) ==")
        buckets: dict[str, list[str]] = {"new": [], "changed": [], "unchanged": []}
        for item in latest.items:
            buckets.setdefault(item.change_status, []).append(item.id)
        for k in ("new", "changed", "unchanged"):
            click.echo(f"  {k}: {len(buckets.get(k, []))}")
            for name in buckets.get(k, [])[:10]:
                click.echo(f"    - {name}")
            if len(buckets.get(k, [])) > 10:
                click.echo(f"    ... and {len(buckets[k]) - 10} more")


@cli.command(name="export")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["csv", "jsonl", "json"]),
    default="jsonl",
    help="Output format (default: jsonl).",
)
@click.option(
    "--run", "run_id", default=None, help="Specific run ID (default: latest)."
)
@click.option("--staging", type=click.Path(path_type=Path), default=STAGING_DIR)
def export_cmd(fmt: str, run_id: str | None, staging: Path) -> None:
    """Export scraped docs as CSV, JSONL, or consolidated JSON."""
    import csv
    import io

    if not staging.exists():
        click.echo("No staging directory yet. Run `megamaid suck` first.")
        return

    for target_dir in sorted(staging.iterdir()):
        if not target_dir.is_dir():
            continue
        if run_id:
            run_dir = target_dir / run_id
            if not run_dir.exists():
                continue
        else:
            manifest = get_latest_manifest(staging, target_dir.name)
            if manifest is None:
                continue
            run_dir = target_dir / manifest.run_id

        docs_dir = run_dir / "docs"
        if not docs_dir.exists():
            continue

        docs = []
        for doc_file in sorted(docs_dir.glob("*.json")):
            docs.append(json.loads(doc_file.read_text()))

        if not docs:
            continue

        out_path = run_dir / f"export.{fmt}"

        if fmt == "jsonl":
            lines = [json.dumps(d, ensure_ascii=False) for d in docs]
            out_path.write_text("\n".join(lines) + "\n")

        elif fmt == "json":
            out_path.write_text(json.dumps(docs, indent=2, ensure_ascii=False))

        elif fmt == "csv":
            # Flatten: core fields + metadata keys as columns
            all_meta_keys: set[str] = set()
            for d in docs:
                all_meta_keys.update(d.get("metadata", {}).keys())
            meta_keys = sorted(all_meta_keys)

            fieldnames = ["id", "source_url", "title", "content_md"] + meta_keys
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for d in docs:
                row = {
                    "id": d.get("id", ""),
                    "source_url": d.get("source_url", ""),
                    "title": d.get("title", ""),
                    "content_md": d.get("content_md", ""),
                }
                for k in meta_keys:
                    val = d.get("metadata", {}).get(k, "")
                    row[k] = json.dumps(val) if isinstance(val, (list, dict)) else val
                writer.writerow(row)
            out_path.write_text(buf.getvalue())

        click.echo(f"Exported {len(docs)} docs to {out_path}")


@cli.command()
@click.argument("url")
@click.option("--max", "max_urls", type=int, default=500, help="Max URLs to discover.")
@click.option(
    "--filter", "url_filter", default=None, help="Only URLs containing this substring."
)
@click.option("--output", "output_file", type=click.Path(path_type=Path), default=None)
def map(
    url: str, max_urls: int, url_filter: str | None, output_file: Path | None
) -> None:
    """Discover all URLs on a domain (sitemap + link crawl)."""
    from xml.etree import ElementTree as ET

    import httpx

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    urls: set[str] = set()

    # Layer 1: Try sitemap.xml
    sitemap_urls_to_check = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]

    # Check robots.txt for Sitemap: entries
    try:
        robots = httpx.get(f"{base}/robots.txt", timeout=10.0, follow_redirects=True)
        if robots.status_code == 200:
            for line in robots.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    sm_url = line.split(":", 1)[1].strip()
                    if sm_url not in sitemap_urls_to_check:
                        sitemap_urls_to_check.append(sm_url)
    except Exception:
        pass

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def _parse_sitemap(sm_url: str) -> None:
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
                        _parse_sitemap(loc)
            else:
                for u in root.findall("sm:url", ns):
                    loc = u.findtext("sm:loc", default="", namespaces=ns)
                    if loc:
                        urls.add(loc)
        except Exception:
            pass

    for sm_url in sitemap_urls_to_check:
        _parse_sitemap(sm_url)
        if len(urls) >= max_urls:
            break

    sitemap_count = len(urls)
    if sitemap_count:
        logger.info(f"Sitemap: found {sitemap_count} URLs")

    # Layer 2: If sitemap yielded few/no results, crawl links
    if len(urls) < max_urls:
        logger.info("Crawling links from start URL...")

        async def _crawl_links() -> None:
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-gpu"]
            )
            page = await browser.new_page()
            to_visit = [url]
            visited: set[str] = set()

            while to_visit and len(urls) < max_urls:
                current = to_visit.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                try:
                    await page.goto(
                        current, wait_until="domcontentloaded", timeout=15000
                    )
                    links = await page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)"
                    )
                    for link in links:
                        link_parsed = urlparse(link)
                        if link_parsed.netloc == parsed.netloc and link not in visited:
                            urls.add(link.split("#")[0].rstrip("/"))
                            if len(urls) < max_urls and link not in visited:
                                to_visit.append(link)
                except Exception:
                    continue

            await browser.close()
            await pw.stop()

        asyncio.run(_crawl_links())
        logger.info(f"Link crawl: found {len(urls) - sitemap_count} additional URLs")

    # Filter
    if url_filter:
        urls = {u for u in urls if url_filter in u}

    sorted_urls = sorted(urls)[:max_urls]

    # Output
    output = "\n".join(sorted_urls) + "\n"
    if output_file:
        output_file.write_text(output)
        click.echo(f"Wrote {len(sorted_urls)} URLs to {output_file}")
    else:
        click.echo(output, nl=False)
        click.echo(f"\n# {len(sorted_urls)} URLs discovered", err=True)


@cli.command()
def init() -> None:
    """Print setup instructions for a fresh scaffold."""
    click.echo(
        "# megamaid scaffold setup\n"
        "python -m venv .venv\n"
        "source .venv/bin/activate\n"
        "pip install -e .\n"
        "playwright install chromium\n"
        "\n"
        "# then edit targets/<your_target>.py and:\n"
        "megamaid suck --max 5   # dry-run\n"
        "megamaid suck           # full run\n"
    )


if __name__ == "__main__":
    cli()
