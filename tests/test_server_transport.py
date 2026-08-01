"""stdio is the only transport. The process boundary is the authentication.

This is an enforced invariant, not an assumption: auth binds at FastMCP
construction, so any surviving network listener would be unauthenticated on
every interface while megamaid_run subprocesses a project venv python. That
holds for the mcp = FastMCP(...) call wherever it lives in this package, not
just in server.py — so the forbidden-string scan below is package-wide by
design. Do not narrow it back to a single file: a new module added to
src/megamaid_mcp/ (e.g. a transport.py or http_server.py) is exactly the kind
of place this invariant could quietly regress.
"""

import ast

FORBIDDEN_LISTENER_STRINGS = ("streamable-http", "0.0.0.0", "port=8000")
FORBIDDEN_AUTH_STRINGS = ("StaticTokenVerifier", "MCP_BEARER_TOKEN")


def _package_dir(repo_root):
    path = repo_root / "src" / "megamaid_mcp"
    assert path.is_dir(), f"missing package directory {path}"
    return path


def _package_py_files(repo_root):
    """Collect every *.py file in src/megamaid_mcp/ as (path, text) pairs.

    Scanning the whole package — not just server.py — is the point: a
    forbidden string reintroduced in any new module here would otherwise
    slip past these tests entirely.
    """
    package_dir = _package_dir(repo_root)
    files = sorted(package_dir.rglob("*.py"))
    assert files, (
        f"found zero .py files under {package_dir} — "
        "a helper that silently finds nothing would make every test below pass vacuously"
    )
    return [(path, path.read_text()) for path in files]


def _server_source(repo_root):
    path = repo_root / "src" / "megamaid_mcp" / "server.py"
    assert path.exists(), f"missing {path}"
    return path.read_text()


def test_no_network_listener_in_shipped_code(repo_root):
    for path, text in _package_py_files(repo_root):
        for forbidden in FORBIDDEN_LISTENER_STRINGS:
            assert forbidden not in text, f"found {forbidden!r} in {path}"


def test_no_bearer_token_auth(repo_root):
    for path, text in _package_py_files(repo_root):
        for forbidden in FORBIDDEN_AUTH_STRINGS:
            assert forbidden not in text, f"found {forbidden!r} in {path}"


def test_main_entrypoint_exists(repo_root):
    # File-scoped, not package-wide: the console script points specifically
    # at megamaid_mcp.server:main (see pyproject.toml [project.scripts]).
    tree = ast.parse(_server_source(repo_root))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "main" in names, "console script megamaid-mcp needs a main() entry point"
