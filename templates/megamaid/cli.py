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
