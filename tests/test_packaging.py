"""The [mcp] extra must stay light — it is installed inside the MCP connect budget."""

import json
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
    """One version source. pyproject must not hardcode a second."""
    project = _pyproject(repo_root)["project"]
    assert "version" not in project, "version must be dynamic, not literal"
    assert "version" in project["dynamic"]
    manifest = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == "0.9.0"
