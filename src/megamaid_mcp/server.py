"""megamaid MCP Server — web scraping tools for agents and automations.

Exposes four tools:
  megamaid_recon      — probe a URL and recommend a scraping pattern
  megamaid_run        — run a scaffolded project and return stats + optional docs
  megamaid_status     — latest run stats for a project (disk read, no network)
  megamaid_list_docs  — list scraped docs from a run (disk read, no network)

Projects live on the host filesystem. Pass either an absolute path
("/home/you/megamaid-walmart", "~/megamaid-walmart") or a bare name
("megamaid-walmart"), which is resolved under MEGAMAID_PROJECTS_DIR
(default: the user's home directory).

Transport is stdio ONLY, and that is load-bearing. FastMCP binds auth at
construction, so a network listener here would be unauthenticated on every
interface while megamaid_run subprocesses a project venv python. The process
boundary is the authentication. tests/test_server_transport.py enforces it.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from megamaid.manifest import Manifest
from megamaid.recon import run_recon

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Where a bare project *name* is resolved. This used to default to the retired
# Docker container's mount point, which does not exist on a host; the natural
# root here is the user's home directory. An absolute path argument bypasses
# this entirely — see _resolve_project.
PROJECTS_DIR = Path(os.environ.get("MEGAMAID_PROJECTS_DIR", "~")).expanduser()
TIMEOUT = float(os.environ.get("MEGAMAID_TIMEOUT", "300"))

# stderr, not stdout, is mandatory here. stdout is the JSON-RPC wire for a
# stdio MCP server (Claude Code reads framed protocol messages from it), and
# basicConfig() configures the ROOT logger — every third-party library's own
# logger.info/warning/etc (fastmcp, mcp, anyio, ...) propagates to root by
# default and would inherit this handler too. Routing that to stdout was
# found to interleave library log lines (e.g. the mcp SDK's own
# "Processing request of type ..." line, emitted mid-tool-call) into the
# protocol stream, corrupting the framing a real client parses. Do not
# switch this back to stdout.
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------

mcp = FastMCP(name="megamaid")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_project(project: str) -> Path:
    """Resolve a project name or path to an existing directory on this host.

    Two accepted forms:

    * an **absolute path** (``/home/you/megamaid-walmart``, or ``~/...``,
      which expands first) — honoured as given, anywhere the caller can read.
    * a **bare name** (``megamaid-walmart``) — resolved under PROJECTS_DIR and
      required to stay under it.

    The containment check survives only for the bare-name form, and it is a
    *correctness* guard, not a security boundary. This server is a stdio
    subprocess running as the user, with exactly the user's own filesystem
    rights, and megamaid_run already hands control to a script inside the
    resolved project — a path check cannot fence in a caller who could just as
    easily read the file directly. Confining every path to one root on such a
    host buys no privilege separation; it only breaks the absolute-path form,
    which is what this fix restores. What containment still buys is that a
    *name* stays a name: ``"../../etc"`` cannot quietly mean something other
    than a sibling of the other scraped projects.

    Args:
        project: bare project directory name, or an absolute (or ``~``-rooted)
            path to the project.

    Returns:
        The resolved, existing project directory.

    Raises:
        ToolError: when a bare name escapes PROJECTS_DIR, or the directory
            does not exist.
    """
    p = Path(project).expanduser()
    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (PROJECTS_DIR / p).resolve()
        try:
            resolved.relative_to(PROJECTS_DIR.resolve())
        except ValueError:
            raise ToolError(
                f"project name {project!r} escapes the projects root {PROJECTS_DIR}. "
                "Pass an absolute path if the project lives elsewhere."
            )
    if not resolved.exists():
        raise ToolError(
            f"project not found: {resolved}. Pass an absolute path, or point "
            f"MEGAMAID_PROJECTS_DIR (currently {PROJECTS_DIR}) at the directory "
            "holding your scraped projects."
        )
    return resolved


def _project_cli(project_path: Path) -> list[str]:
    """Return the command that runs a scraped project's own megamaid CLI.

    The console script's shebang already names the project venv's interpreter,
    so executing it directly is all that is needed: the venv's site-packages
    and its editable-install finder (which is what makes ``import targets``
    resolve to the project's own targets/ package) come along automatically.

    The previous form — the *server's* ``python3`` plus a hand-built
    PYTHONPATH — was a workaround for a container that had no host interpreter
    at the shebang's path. On a host it is wrong twice: it discards the venv's
    interpreter, and it derived site-packages from the server's own
    ``sys.version_info``, so a project venv on a different Python minor
    silently contributed nothing.

    Args:
        project_path: resolved project directory.

    Returns:
        argv for subprocess, ready to have a subcommand appended.

    Raises:
        ToolError: when the project has no .venv/bin/megamaid.
    """
    script = project_path / ".venv" / "bin" / "megamaid"
    if not script.exists():
        raise ToolError(
            f"No .venv/bin/megamaid at {project_path}. "
            "Set up first: python3 -m venv .venv && .venv/bin/pip install -e ."
        )
    return [str(script)]


def _parse_suck_stdout(stdout: str) -> tuple[dict, Path | None]:
    """Parse stats dict and manifest path from megamaid suck stdout."""
    stats: dict = {}
    manifest_path: Path | None = None

    for line in stdout.splitlines():
        if line.startswith("Manifest: "):
            raw = line.removeprefix("Manifest: ").strip()
            if raw:
                manifest_path = Path(raw)

    match = re.search(r"(\{[^{}]+\})", stdout, re.DOTALL)
    if match:
        try:
            stats = json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    return stats, manifest_path


def _load_new_changed_docs(run_dir: Path, summary_only: bool) -> tuple[list, list]:
    """Load new and changed docs from a run directory."""
    docs_dir = run_dir / "docs"
    if not docs_dir.exists():
        return [], []

    change_map: dict[str, str] = {}
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        try:
            m = Manifest.load(manifest_path)
            change_map = {item.id: item.change_status for item in m.items}
        except Exception:
            pass

    new_docs: list[dict] = []
    changed_docs: list[dict] = []
    for doc_file in sorted(docs_dir.glob("*.json")):
        try:
            data = json.loads(doc_file.read_text())
            doc_id = data.get("id", doc_file.stem)
            status = change_map.get(doc_id, "new")
            if status not in ("new", "changed"):
                continue
            content = data.get("content_md", "")
            entry = {
                "id": doc_id,
                "source_url": data.get("source_url", ""),
                "title": data.get("title", ""),
                "content_md": content[:500] if summary_only else content,
                "metadata": data.get("metadata", {}),
            }
            (new_docs if status == "new" else changed_docs).append(entry)
        except (json.JSONDecodeError, OSError):
            continue

    return new_docs, changed_docs


def _latest_run_dir(staging_dir: Path) -> Path | None:
    if not staging_dir.exists():
        return None
    for target_dir in sorted(staging_dir.iterdir(), reverse=True):
        if not target_dir.is_dir():
            continue
        run_dirs = sorted(target_dir.iterdir(), reverse=True)
        if run_dirs:
            return run_dirs[0]
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def megamaid_recon(
    url: Annotated[str, Field(description="Target URL to probe (e.g. https://example.com)")],
) -> dict:
    """Probe a URL and recommend a megamaid scraping pattern.

    Makes 3-6 HTTP requests (robots.txt, sitemap, homepage) with no
    filesystem writes. Returns recommended_pattern, confidence, anti_bot
    assessment, recommended_rate_limit, and warnings.

    All 12 patterns are scored: shopify_json, sitemap_crawl, paginated_html,
    load_more_infinite, pdf_downloads, rest_json_api, graphql_api,
    rss_atom_feed, search_seed, spa_hydration, auth_wall, image_downloads.
    """
    start = time.monotonic()
    if not url.startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")
    try:
        report = await run_recon(url)
        result = asdict(report)
        logger.info(
            json.dumps(
                {
                    "tool": "megamaid_recon",
                    "url": url,
                    "ms": round((time.monotonic() - start) * 1000),
                }
            )
        )
        return result
    except Exception as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def megamaid_run(
    project: Annotated[
        str,
        Field(
            description=(
                "Project directory name (e.g. 'megamaid-walmart', resolved "
                "under MEGAMAID_PROJECTS_DIR) or an absolute path. Must be a "
                "scaffolded megamaid project with .venv/bin/megamaid present."
            )
        ),
    ],
    max_items: Annotated[
        int | None,
        Field(description="Maximum items to scrape. Omit to scrape everything.", ge=1),
    ] = None,
    include_docs: Annotated[
        bool,
        Field(
            description=(
                "Include new and changed document content in the response. "
                "Adds new_docs[] and changed_docs[]."
            )
        ),
    ] = False,
    summary_only: Annotated[
        bool,
        Field(
            description=(
                "Truncate content_md to 500 characters per doc. Ignored when include_docs=False."
            )
        ),
    ] = True,
) -> dict:
    """Run an existing scaffolded megamaid project and return scrape stats.

    Calls the project's own .venv/bin/megamaid suck via subprocess so each
    project uses its own Python environment and Playwright install.

    Always returns: run_id, target, staging_dir, stats (total/new/changed/
    unchanged/failed).

    With include_docs=True: also returns new_docs[] and changed_docs[] with
    id, source_url, title, content_md (500-char summary if summary_only),
    and metadata.
    """
    start = time.monotonic()
    project_path = _resolve_project(project)
    cmd = _project_cli(project_path) + ["suck"]
    if max_items is not None:
        cmd += ["--max", str(max_items)]

    try:
        proc = subprocess.run(
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"megamaid suck timed out after {int(TIMEOUT)}s")

    if proc.returncode not in (0, 1):
        raise ToolError(
            f"megamaid suck exited {proc.returncode}: {proc.stderr[-500:] or '(no stderr)'}"
        )

    stats_dict, manifest_path = _parse_suck_stdout(proc.stdout)

    # Resolve relative paths (suck prints "Manifest: staging/...") against project_path
    if manifest_path and not manifest_path.is_absolute():
        manifest_path = project_path / manifest_path

    run_id = manifest_path.parent.name if manifest_path else ""
    target_name = manifest_path.parent.parent.name if manifest_path else ""
    staging_dir_str = str(manifest_path.parent) if manifest_path else str(project_path / "staging")

    response: dict = {
        "run_id": run_id,
        "target": target_name,
        "staging_dir": staging_dir_str,
        "stats": {k: v for k, v in stats_dict.items() if k not in ("target", "run_id")},
    }

    if include_docs and manifest_path:
        new_docs, changed_docs = _load_new_changed_docs(manifest_path.parent, summary_only)
        response["new_docs"] = new_docs
        response["changed_docs"] = changed_docs

    logger.info(
        json.dumps(
            {
                "tool": "megamaid_run",
                "project": project,
                "stats": response.get("stats"),
                "ms": round((time.monotonic() - start) * 1000),
            }
        )
    )
    return response


@mcp.tool
async def megamaid_status(
    project: Annotated[
        str,
        Field(description="Project directory name or an absolute path to the project."),
    ],
) -> dict:
    """Return stats for the most recent run of a megamaid project.

    Reads from disk — fast, no network. Returns run_id, target, status,
    started_at, completed_at, staging_dir, and aggregate stats.
    """
    project_path = _resolve_project(project)
    run_dir = _latest_run_dir(project_path / "staging")

    if not run_dir:
        return {
            "status": "no_runs",
            "message": f"No runs found in {project_path / 'staging'}",
        }

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return {"status": "no_manifest", "run_dir": str(run_dir)}

    try:
        m = Manifest.load(manifest_path)
    except Exception as exc:
        raise ToolError(f"Failed to load manifest: {exc}") from exc

    return {
        "run_id": m.run_id,
        "target": m.target,
        "status": m.status,
        "started_at": m.started_at,
        "completed_at": m.completed_at,
        "staging_dir": str(run_dir),
        "stats": {
            "total": m.stats.total,
            "scraped_ok": m.stats.scraped_ok,
            "scrape_failed": m.stats.scrape_failed,
            "new": m.stats.new,
            "changed": m.stats.changed,
            "unchanged": m.stats.unchanged,
        },
    }


@mcp.tool
async def megamaid_list_docs(
    project: Annotated[
        str,
        Field(description="Project directory name or an absolute path to the project."),
    ],
    run_id: Annotated[
        str | None,
        Field(
            description=(
                "Specific run ID (e.g. '20260418T120000Z'). Defaults to the most recent run."
            )
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum number of docs to return.", ge=1, le=500),
    ] = 50,
) -> dict:
    """List scraped documents from a megamaid project run.

    Reads from disk — fast, no network. Returns an array of docs with
    id, title, source_url, content_md (first 500 chars), and metadata.
    """
    project_path = _resolve_project(project)
    staging_dir = project_path / "staging"

    if not staging_dir.exists():
        raise ToolError(f"No staging directory at {staging_dir}")

    run_dir: Path | None = None
    if run_id:
        for target_dir in staging_dir.iterdir():
            candidate = target_dir / run_id
            if candidate.is_dir():
                run_dir = candidate
                break
        if not run_dir:
            raise ToolError(f"Run ID '{run_id}' not found in {staging_dir}")
    else:
        run_dir = _latest_run_dir(staging_dir)

    if not run_dir:
        raise ToolError(f"No runs found in {staging_dir}")

    docs_dir = run_dir / "docs"
    if not docs_dir.exists():
        return {
            "run_id": run_dir.name,
            "target": run_dir.parent.name,
            "docs": [],
            "total": 0,
            "returned": 0,
        }

    doc_files = sorted(docs_dir.glob("*.json"))
    docs = []
    for doc_file in doc_files[:limit]:
        try:
            data = json.loads(doc_file.read_text())
            docs.append(
                {
                    "id": data.get("id", doc_file.stem),
                    "title": data.get("title", ""),
                    "source_url": data.get("source_url", ""),
                    "content_md": data.get("content_md", "")[:500],
                    "metadata": data.get("metadata", {}),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue

    return {
        "run_id": run_dir.name,
        "target": run_dir.parent.name,
        "docs": docs,
        "total": len(doc_files),
        "returned": len(docs),
    }


def main() -> None:
    """Console-script entry point. stdio only — see the module docstring."""
    mcp.run()


if __name__ == "__main__":
    main()
