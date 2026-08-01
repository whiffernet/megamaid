"""The [mcp] extra must stay light — it is installed inside the MCP connect budget."""

import json
import subprocess
import sys
import tomllib


def _pyproject(repo_root):
    return tomllib.loads((repo_root / "pyproject.toml").read_text())


HEAVY = {"playwright", "trafilatura", "beautifulsoup4"}


def test_mcp_extra_excludes_heavy_scraper_dependencies(repo_root):
    extras = _pyproject(repo_root)["project"]["optional-dependencies"]
    named = {dep.split(">")[0].split("=")[0].split("[")[0].strip() for dep in extras["mcp"]}
    assert not (named & HEAVY), f"[mcp] must stay light; found {named & HEAVY}"


def test_scraper_extra_contains_playwright(repo_root):
    extras = _pyproject(repo_root)["project"]["optional-dependencies"]
    named = {dep.split(">")[0].split("=")[0].split("[")[0].strip() for dep in extras["scraper"]}
    assert "playwright" in named


def test_base_dependencies_are_empty(repo_root):
    """Everything optional. A bare install must not pull playwright in by default."""
    assert _pyproject(repo_root)["project"].get("dependencies", []) == []


def test_console_scripts_declared(repo_root):
    scripts = _pyproject(repo_root)["project"]["scripts"]
    assert scripts["megamaid"] == "megamaid.cli:cli"
    assert scripts["megamaid-mcp"] == "megamaid_mcp.server:main"


def test_version_is_sourced_from_plugin_manifest(repo_root):
    """One version source. pyproject must not hardcode a second.

    Deliberately asserts no literal version: this file used to pin "0.9.0",
    which meant the first `claude plugin update`-worthy bump turned the suite
    red for no reason. What matters is the wiring — that setuptools reads the
    mirror of plugin.json's version rather than carrying its own copy — and
    tests/test_manifests.py separately pins mirror == plugin.json.
    """
    pyproject = _pyproject(repo_root)
    project = pyproject["project"]
    assert "version" not in project, "version must be dynamic, not literal"
    assert "version" in project["dynamic"]

    dynamic_source = pyproject["tool"]["setuptools"]["dynamic"]["version"]["file"]
    assert dynamic_source == ".claude-plugin/VERSION.txt", (
        f"setuptools must read the plugin-manifest mirror, got {dynamic_source!r}"
    )

    manifest = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    mirror = (repo_root / dynamic_source).read_text().strip()
    assert mirror == manifest["version"]


def test_cli_extra_contains_click_and_excludes_heavy_scraper_dependencies(repo_root):
    """[cli] serves the URL-scoped commands from the state venv — click, no browser."""
    extras = _pyproject(repo_root)["project"]["optional-dependencies"]
    named = {dep.split(">")[0].split("=")[0].split("[")[0].strip() for dep in extras["cli"]}
    assert "click" in named
    assert not (named & HEAVY), f"[cli] must stay light; found {named & HEAVY}"


def test_megamaid_cli_importable_with_mcp_and_cli_extras_only(repo_root):
    """megamaid.cli must import cleanly from the state venv, which never gets [scraper].

    A later task's launcher execs <venv>/bin/megamaid for URL-scoped commands
    (recon, map, init) out of a venv built with [mcp,cli] only. If importing
    megamaid.cli reaches playwright at module scope, that binary is unusable.
    Runs in a fresh subprocess so we measure the real import graph, not
    whatever pytest happens to have cached in sys.modules.
    """
    cli_path = repo_root / "src" / "megamaid" / "cli.py"
    assert cli_path.exists(), f"missing runtime module {cli_path}"
    runtime_parent = cli_path.parent.parent  # src/

    script = f"""
import sys
sys.path.insert(0, {str(runtime_parent)!r})
import megamaid.cli
print('PLAYWRIGHT_LOADED' if 'playwright' in sys.modules else 'CLEAN')
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"megamaid.cli import failed with exit code {result.returncode}. stderr: {result.stderr}"
    )

    assert result.stdout.strip() == "CLEAN", (
        f"Importing megamaid.cli loaded playwright into sys.modules. "
        f"This breaks the state-venv URL-scoped CLI commands. "
        f"stdout: {result.stdout}, stderr: {result.stderr}"
    )
