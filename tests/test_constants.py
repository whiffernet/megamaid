"""The recon import path must not drag in playwright.

The MCP server imports megamaid.recon. If that transitively imports playwright,
the MCP venv needs a 40 MB wheel plus a 150 MB chromium download, and cold start
blows Claude Code's 30-second connect budget.

This file also owns the User-Agent/version relationship — see
test_user_agent_version_tracks_the_plugin_manifest.
"""

import ast
import json
import pathlib
import re
import subprocess
import sys


def _module_path(repo_root, name):
    """Locate a runtime module."""
    candidate = repo_root / "src" / "megamaid" / name
    assert candidate.exists(), f"missing runtime module {candidate}"
    return candidate


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Every module named by an import statement in this file, top-level names only."""
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_constants_module_imports_nothing_heavy(repo_root):
    imports = _imported_modules(_module_path(repo_root, "constants.py"))
    assert "playwright" not in imports
    assert imports <= {"__future__"}, f"constants.py must stay dependency-free, got {imports}"


def test_user_agent_version_tracks_the_plugin_manifest(repo_root):
    """DEFAULT_USER_AGENT's version segment must match plugin.json's major.minor.

    The constant hardcodes `megamaid/0.9`, which stops matching the plugin at
    0.10.0. Two ways to fix that; this project takes the second:

    1. Derive it at runtime from `importlib.metadata.version("megamaid")`.
       Rejected: constants.py is copied *verbatim* into every scaffolded
       project, and those projects rename the distribution (a real one on this
       machine declares `name = "megamaid-scraper"`), so the lookup raises
       PackageNotFoundError exactly where the runtime is vendored. Wrapping it
       in a try/except with a literal fallback reintroduces the same drift
       behind more machinery, and the fallback is the branch that would
       actually run in a scraped project.

    2. Keep the literal and pin the relationship here. A version bump then
       fails one test with an obvious fix, instead of silently shipping a
       User-Agent that misidentifies the tool to every site it visits.
    """
    version = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())["version"]
    major, minor = version.split(".")[:2]

    source = _module_path(repo_root, "constants.py").read_text()
    match = re.search(r"megamaid/(\d+\.\d+)", source)
    assert match, "DEFAULT_USER_AGENT must identify as megamaid/<major>.<minor>"
    assert match.group(1) == f"{major}.{minor}", (
        f"DEFAULT_USER_AGENT says megamaid/{match.group(1)} but plugin.json is "
        f"{version} — update src/megamaid/constants.py to megamaid/{major}.{minor}"
    )


def test_recon_does_not_import_base(repo_root):
    """base.py imports playwright; recon must reach DEFAULT_USER_AGENT another way."""
    source = _module_path(repo_root, "recon.py").read_text()
    assert "from .base import" not in source
    assert "from .constants import DEFAULT_USER_AGENT" in source


def test_default_user_agent_is_defined_once(repo_root):
    """The constant lives in exactly one place — base.py must import, not redefine."""
    base = _module_path(repo_root, "base.py").read_text()
    assert "DEFAULT_USER_AGENT = (" not in base, "base.py must import the constant, not define it"
    assert "from .constants import DEFAULT_USER_AGENT" in base


def test_recon_runtime_no_playwright(repo_root):
    """Verify that importing megamaid.recon does not load playwright into sys.modules.

    This test runs in a fresh subprocess because the pytest process may already have
    playwright loaded by other tests. A subprocess ensures we measure whether recon
    truly avoids the dependency, not whether pytest happens to have it cached.
    The invariant this protects is stronger than any source-code check: nothing
    reachable from megamaid.recon's import graph may import playwright.
    """
    # Locate the runtime — works whether the package is at templates/megamaid or src/megamaid
    recon_path = _module_path(repo_root, "recon.py")
    runtime_parent = recon_path.parent.parent  # templates/ or src/

    # Subprocess script: import megamaid.recon, then report whether playwright loaded
    script = f"""
import sys
sys.path.insert(0, {str(runtime_parent)!r})
import megamaid.recon
print('PLAYWRIGHT_LOADED' if 'playwright' in sys.modules else 'CLEAN')
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"megamaid.recon import failed with exit code {result.returncode}. stderr: {result.stderr}"
    )

    assert result.stdout.strip() == "CLEAN", (
        f"Importing megamaid.recon loaded playwright into sys.modules. "
        f"This breaks the MCP cold-start optimization. "
        f"stdout: {result.stdout}, stderr: {result.stderr}"
    )
