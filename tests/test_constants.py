"""The recon import path must not drag in playwright.

The MCP server imports megamaid.recon. If that transitively imports playwright,
the MCP venv needs a 40 MB wheel plus a 150 MB chromium download, and cold start
blows Claude Code's 30-second connect budget.
"""

import ast
import pathlib


def _module_path(repo_root, name):
    """Locate a runtime module whether or not the src/ move has happened yet."""
    for base in ("src/megamaid", "templates/megamaid"):
        candidate = repo_root / base / name
        if candidate.exists():
            return candidate
    raise AssertionError(f"could not find {name} under src/megamaid or templates/megamaid")


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
