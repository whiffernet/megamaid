"""megamaid MCP Server — web scraping tools for agents and automations.

Exposes four tools:
  megamaid_recon      — probe a URL and recommend a scraping pattern
  megamaid_run        — run a scaffolded project and return stats + optional docs
  megamaid_status     — latest run stats for a project (disk read, no network)
  megamaid_list_docs  — list scraped docs from a run (disk read, no network)

Projects are read from MEGAMAID_PROJECTS_DIR_INTERNAL (default /projects).
Pass either a bare name ("megamaid-walmart") or an absolute container path
("/projects/megamaid-walmart") to any tool that takes a project argument.

Self-contained: no dependency on the mcp/shared/ utilities in the parent
mcp repo. Auth and logging are inlined below.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import Field

from megamaid.manifest import Manifest
from megamaid.recon import run_recon

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECTS_DIR = Path(os.environ.get("MEGAMAID_PROJECTS_DIR_INTERNAL", "/projects"))
TIMEOUT = float(os.environ.get("MEGAMAID_TIMEOUT", "300"))

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth (inlined — no dependency on mcp/shared/)
# ---------------------------------------------------------------------------


def _load_secret(name: str, env_var: str | None = None) -> str:
    """Load a secret from Docker secrets, env var, or local .secrets/ file."""
    secret_path = Path(f"/run/secrets/{name}")
    if secret_path.exists():
        return secret_path.read_text().strip()
    if env_var:
        value = os.environ.get(env_var)
        if value:
            return value
    local_path = Path(__file__).parent / ".secrets" / name
    if local_path.exists():
        return local_path.read_text().strip()
    raise FileNotFoundError(
        f"Secret '{name}' not found. Checked {secret_path}, "
        f"env:{env_var or 'N/A'}, {local_path}"
    )


def _create_auth() -> StaticTokenVerifier:
    token = _load_secret("mcp_bearer_token", env_var="MCP_BEARER_TOKEN")
    return StaticTokenVerifier(
        tokens={token: {"client_id": "mcp-client", "scopes": ["full"]}},
        required_scopes=["full"],
    )


# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------

mcp = FastMCP(name="megamaid", auth=_create_auth())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_project(project: str) -> Path:
    """Resolve a project name or path to a validated container path."""
    p = Path(project)
    if not p.is_absolute():
        p = PROJECTS_DIR / project
    try:
        p.resolve().relative_to(PROJECTS_DIR.resolve())
    except ValueError:
        raise ToolError(f"project must be within {PROJECTS_DIR} — got: {project!r}")
    if not p.exists():
        raise ToolError(
            f"project not found: {p}. "
            f"Confirm MEGAMAID_PROJECTS_DIR_INTERNAL={PROJECTS_DIR} is mounted correctly."
        )
    return p


def _venv_cmd(project_path: Path) -> tuple[list[str], dict]:
    """Return (command, env) to run megamaid suck in the project venv.

    Uses the container's Python (found via shutil.which) rather than the
    venv's python3 symlink, which points to the host's /usr/bin/python3 —
    a path that typically does not exist inside the container. The venv's
    site-packages are injected via PYTHONPATH so project-specific deps work.
    """
    script = project_path / ".venv" / "bin" / "megamaid"
    if not script.exists():
        raise ToolError(
            f"No .venv/bin/megamaid at {project_path}. "
            "Set up first: python3 -m venv .venv && pip install -e ."
        )

    python = shutil.which("python3") or "/usr/local/bin/python3"

    # Build PYTHONPATH with two entries:
    # 1. project_path — so `import targets` finds targets/hnrss.py (not the
    #    example_target bundled in the container's megamaid package). The venv's
    #    editable-install .pth file encodes the host path (/home/e/...) which
    #    doesn't resolve inside the container, so we add the project root directly.
    # 2. venv site-packages — for any project-specific deps (bs4, lxml, etc.)
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_pkgs = project_path / ".venv" / "lib" / ver / "site-packages"
    env = dict(os.environ)
    parts = [str(project_path)]
    if site_pkgs.exists():
        parts.append(str(site_pkgs))
    if existing := env.get("PYTHONPATH", ""):
        parts.append(existing)
    env["PYTHONPATH"] = ":".join(parts)

    return [python, str(script)], env


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

    new_docs, changed_docs = [], []
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
    url: Annotated[
        str, Field(description="Target URL to probe (e.g. https://example.com)")
    ],
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
                "Project directory name (e.g. 'megamaid-walmart') or absolute "
                "container path. Must be a scaffolded megamaid project with "
                ".venv/bin/megamaid present."
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
                "Truncate content_md to 500 characters per doc. "
                "Ignored when include_docs=False."
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
    base_cmd, env = _venv_cmd(project_path)
    cmd = base_cmd + ["suck"]
    if max_items is not None:
        cmd += ["--max", str(max_items)]

    try:
        proc = subprocess.run(
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
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
    staging_dir_str = (
        str(manifest_path.parent) if manifest_path else str(project_path / "staging")
    )

    response: dict = {
        "run_id": run_id,
        "target": target_name,
        "staging_dir": staging_dir_str,
        "stats": {k: v for k, v in stats_dict.items() if k not in ("target", "run_id")},
    }

    if include_docs and manifest_path:
        new_docs, changed_docs = _load_new_changed_docs(
            manifest_path.parent, summary_only
        )
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
        Field(description="Project directory name or absolute container path."),
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
        Field(description="Project directory name or absolute container path."),
    ],
    run_id: Annotated[
        str | None,
        Field(
            description=(
                "Specific run ID (e.g. '20260418T120000Z'). "
                "Defaults to the most recent run."
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


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
