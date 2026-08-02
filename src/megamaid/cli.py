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
import importlib.metadata
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import click

from .image_index import ImageIndex
from .manifest import Manifest, ManifestItem, compute_delta, get_latest_manifest
from .models import slug_from_url

if TYPE_CHECKING:
    # megamaid_setup is not vendored into scraped projects (see `upgrade`
    # below), so this import must never run at module scope for real — only
    # mypy sees it, via `from __future__ import annotations` postponing
    # evaluation of the annotations that use it.
    from megamaid_setup.upgrade import ProjectPlan

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
            if isinstance(obj, type) and issubclass(obj, BaseScraper) and obj is not BaseScraper:
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
@click.option("--timeout", type=float, default=10.0, help="Per-request timeout (seconds).")
@click.option("--quiet", is_flag=True, help="Suppress progress, print only final report.")
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
    from .base import DEFAULT_USER_AGENT
    from .recon import format_json_report, format_text_report, run_recon

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
    # Images live in a per-target shared store so identical bytes are
    # downloaded once and reused across runs via the image index.
    images_dir = staging / target.target_name / "images"
    index_path = staging / target.target_name / "image_index.json"
    for d in (raw_dir, docs_dir, debug_dir, images_dir):
        d.mkdir(parents=True, exist_ok=True)
    image_index = ImageIndex.load(index_path)

    manifest = Manifest(
        run_id=run_id,
        target=target.target_name,
        started_at=datetime.now(timezone.utc).isoformat(),
        status="running",
    )
    manifest_path = run_dir / "manifest.json"
    manifest.save(manifest_path)

    async def _run() -> None:
        from .base import create_browser

        pw, browser = await create_browser()
        target._debug_dir = debug_dir
        target._images_dir = images_dir
        target._image_index = image_index
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
        # Persist the image index even on failure, so a re-run resumes.
        image_index.save(index_path)

    click.echo(json.dumps(manifest.stats.to_dict(), indent=2))
    total_images = sum(item.image_count for item in manifest.items)
    if total_images:
        unique_hashes = set()
        for item in manifest.items:
            unique_hashes.update(item.image_hashes)
        image_bytes = 0
        for h in unique_hashes:
            for f in images_dir.glob(f"{h[:16]}.*"):
                image_bytes += f.stat().st_size
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
@click.option("--run", "run_id", default=None, help="Specific run ID (default: latest).")
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
@click.option("--filter", "url_filter", default=None, help="Only URLs containing this substring.")
@click.option("--output", "output_file", type=click.Path(path_type=Path), default=None)
def map(url: str, max_urls: int, url_filter: str | None, output_file: Path | None) -> None:
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

    # Layer 2: If the sitemap yielded fewer than --max URLs, crawl links.
    #
    # This layer is OPTIONAL, and the whole command is documented as
    # browser-free. It fires whenever the sitemap came up short of --max
    # (default 500), which is the common case, and it needs playwright — which
    # the launcher's [mcp,cli] state venv deliberately does not install. So a
    # missing browser degrades `map` to its sitemap results, which are
    # complete, valid output on their own, instead of ending the command in a
    # traceback after the work is already done.
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
                    await page.goto(current, wait_until="domcontentloaded", timeout=15000)
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

        try:
            asyncio.run(_crawl_links())
        except ImportError as exc:
            click.echo(
                f"Link crawl skipped: it needs a browser and playwright is not "
                f"available here ({exc}).\n"
                f"The {sitemap_count} URL(s) found via sitemap are still complete, "
                f"valid output.\n"
                f"To crawl links too, run map from a project venv that has the "
                f"scraper extra: pip install -e '.[scraper]' && playwright install chromium",
                err=True,
            )
        else:
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


def _sanitize_backup_component(value: str, label: str) -> str:
    """Refuse a version/timestamp string that could escape `.megamaid-backups`.

    `back_up()` joins its destination as `<project>/.megamaid-backups/<now>-<version>`
    with no validation of its own (by design — it is not the layer that owns
    untrusted input). The CLI is the only place these two strings originate,
    so it is the layer responsible for keeping them inside that directory.

    Args:
        value: the raw string headed into the backup directory name.
        label: which argument this is, used only in the error message.

    Returns:
        value, unchanged, once it has been proven safe.

    Raises:
        SystemExit: if value is empty or could traverse outside the backup
            directory (a path separator or a `..` segment).
    """
    if not value or os.sep in value or (os.altsep and os.altsep in value) or ".." in value:
        raise SystemExit(f"  x refusing unsafe {label} {value!r}: would escape .megamaid-backups")
    return value


def _exit_code(plans: list[ProjectPlan], failures: list[Path]) -> int:
    """Map a batch of plans, plus any apply-time failures, to a process exit code.

    Three outcomes a calling script can tell apart:
        0 — every project converges cleanly (or, on --dry-run, would).
        1 — nothing crashed, but at least one project has a refused file or
            an unreachable add that needs a human decision.
        2 — a project could not even be read, or an apply/rollback call
            actually raised.

    Args:
        plans: the plans that were reported.
        failures: projects whose `apply_plan()` call raised.

    Returns:
        The process exit code.
    """
    if failures or any(p.error for p in plans):
        return 2
    if any(not p.converges for p in plans):
        return 1
    return 0


def _preview_rollback(impl: ModuleType, projects: tuple[Path, ...]) -> int:
    """Report what `--rollback` would restore, for each project, untouched.

    Reads only `latest_backup()` — pure, a directory listing — and never
    calls `impl.rollback()`, which is destructive (`shutil.rmtree` then
    `shutil.copytree`). This is what makes `--dry-run --rollback` safe: the
    write-performing call is never reached on this path, not merely skipped
    by a flag check inside it.

    Args:
        impl: the lazily-imported `megamaid_setup.upgrade` module.
        projects: project directories named on the command line.

    Returns:
        0 if every project has a backup to preview, 2 if any does not
        (MM-36) — the same exit codes a real `--rollback` would give.
    """
    exit_code = 0
    for p in projects:
        newest = impl.latest_backup(p)
        if newest is None:
            click.echo(f"  x {p}: MM-36 no backup found in {p / impl.BACKUP_DIR}")
            exit_code = 2
            continue
        timestamp, version = impl.parse_backup_name(newest.name)
        file_count = sum(1 for f in newest.rglob("*") if f.is_file())
        click.echo(
            f"  would restore {p}/megamaid <- backup {timestamp} "
            f"(version {version}, {file_count} files)"
        )
    click.echo("\n  Nothing written. Re-run without --dry-run.")
    return exit_code


def _do_rollback(impl: ModuleType, projects: tuple[Path, ...]) -> int:
    """Actually restore each project's most recent backup.

    Args:
        impl: the lazily-imported `megamaid_setup.upgrade` module.
        projects: project directories named on the command line.

    Returns:
        0 if every project restored cleanly, 2 if any raised (MM-36).
    """
    exit_code = 0
    for p in projects:
        try:
            restored = impl.rollback(p)
        except RuntimeError as exc:
            click.echo(f"  x {p}: {exc}")
            exit_code = 2
        else:
            click.echo(f"  restored {p} <- {restored}")
    return exit_code


@cli.command()
@click.argument("projects", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True, help="Report what would change; write nothing.")
@click.option("--rollback", "do_rollback", is_flag=True, help="Restore the most recent backup.")
@click.option("--yes", is_flag=True, help="Skip the confirmation when several projects match.")
def upgrade(projects: tuple[Path, ...], dry_run: bool, do_rollback: bool, yes: bool) -> None:
    """Converge scaffolded projects onto the current runtime.

    Runs from the installed plugin, not from inside a scraped project — the
    implementation is not vendored.
    """
    try:
        from megamaid_setup import upgrade as impl
        from megamaid_setup.manifest import load_manifest
    except ImportError:
        raise SystemExit(
            "  x `upgrade` runs from the megamaid plugin, not from inside a project.\n"
            '     Use:  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/launch.py" --cli upgrade ...'
        )

    if do_rollback:
        # Rollback needs neither a manifest nor a plan — it only reads
        # `.megamaid-backups/`. Keeping this branch independent means a
        # broken/missing manifest can never stand between a user and
        # recovering from a bad upgrade. --dry-run is checked FIRST, right
        # here, before either helper runs: _preview_rollback never calls the
        # destructive impl.rollback(), so there is no path from
        # `--dry-run --rollback` to a write, structurally, not by relying on
        # a flag check inside the write path itself.
        if dry_run:
            raise SystemExit(_preview_rollback(impl, projects))
        raise SystemExit(_do_rollback(impl, projects))

    runtime = Path(impl.__file__).resolve().parent.parent / "megamaid"
    manifest = load_manifest()
    plans = [impl.plan_project(p, runtime, manifest) for p in projects]

    click.echo(impl.render(plans))

    if dry_run:
        click.echo("\n  Nothing written. Re-run without --dry-run.")
        raise SystemExit(_exit_code(plans, []))

    if len(projects) > 1 and not yes:
        click.confirm(f"\n  Apply to {len(projects)} projects?", abort=True)

    # The version tag that lands in each backup's directory name and in
    # `.megamaid-version`. `importlib.metadata` reads it straight from the
    # installed distribution — built from `.claude-plugin/VERSION.txt` at
    # package-build time — so it works the same way whether this command is
    # running from the launcher's state venv or an editable dev install,
    # with no assumption about where the plugin's repo checkout lives. If
    # the package metadata is unavailable (e.g. running from a raw source
    # checkout that was never pip-installed), fall back to the manifest's
    # own commit — already loaded above, and already unique per release.
    try:
        version = importlib.metadata.version("megamaid")
    except importlib.metadata.PackageNotFoundError:
        version = manifest.generated_from
    version = _sanitize_backup_component(version, "version")
    now = _sanitize_backup_component(
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), "timestamp"
    )

    failures: list[Path] = []
    for plan in plans:
        if plan.error:
            click.echo(f"  skipped {plan.project.name}: {plan.error}")
            continue
        try:
            impl.apply_plan(plan, runtime, version, now)
        except impl.BackupFailed as exc:
            # back_up() itself raised: apply_plan never reached the copy
            # loop, so nothing in megamaid/ has been touched. Pointing this
            # user at --rollback would be wrong — the backup it would
            # restore may not exist, or may be a half-written copytree.
            click.echo(
                f"  x {plan.project.name}: backup failed ({exc})\n"
                f"     Nothing in megamaid/ was modified. Fix the underlying\n"
                f"     problem (e.g. free disk space) and re-run upgrade."
            )
            failures.append(plan.project)
        except Exception as exc:
            # The backup completed (apply_plan calls it first and only
            # reaches the copy loop after it returns), so megamaid/ may now
            # be a mix of upgraded and pre-upgrade files, and a good,
            # complete backup genuinely exists to restore from.
            click.echo(
                f"  x {plan.project.name}: apply failed after the backup completed ({exc})\n"
                f"     megamaid/ may be left in a mixed state — some files upgraded,\n"
                f"     some not. This does not self-heal. Recover with:\n"
                f"       megamaid upgrade --rollback {plan.project}"
            )
            failures.append(plan.project)

    applied = sum(1 for p in plans if not p.error) - len(failures)
    click.echo(f"\n  Applied to {applied} project(s).")
    if failures:
        click.echo(f"  {len(failures)} project(s) failed mid-apply - see above to recover.")

    raise SystemExit(_exit_code(plans, failures))


if __name__ == "__main__":
    cli()
