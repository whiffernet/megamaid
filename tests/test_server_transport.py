"""stdio is the only transport. The process boundary is the authentication.

This is an enforced invariant, not an assumption: auth binds at FastMCP
construction, so any surviving network listener would be unauthenticated.
"""

import ast


def _server_source(repo_root):
    path = repo_root / "src" / "megamaid_mcp" / "server.py"
    assert path.exists(), f"missing {path}"
    return path.read_text()


def test_no_network_listener_in_shipped_code(repo_root):
    source = _server_source(repo_root)
    assert "streamable-http" not in source
    assert "0.0.0.0" not in source
    assert "port=8000" not in source


def test_no_bearer_token_auth(repo_root):
    source = _server_source(repo_root)
    assert "StaticTokenVerifier" not in source
    assert "MCP_BEARER_TOKEN" not in source


def test_main_entrypoint_exists(repo_root):
    tree = ast.parse(_server_source(repo_root))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "main" in names, "console script megamaid-mcp needs a main() entry point"
