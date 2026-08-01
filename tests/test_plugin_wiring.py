"""The plugin's MCP declaration and commands point at the launcher, never a bare binary."""

import json


def test_mcp_json_has_no_mcpservers_wrapper(repo_root):
    """Plugin .mcp.json is a bare {name: config} mapping — see the playwright plugin."""
    config = json.loads((repo_root / ".mcp.json").read_text())
    assert "mcpServers" not in config
    assert "megamaid" in config


def test_mcp_json_invokes_the_launcher_via_plugin_root(repo_root):
    server = json.loads((repo_root / ".mcp.json").read_text())["megamaid"]
    assert server["command"] == "python3"
    assert server["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/launch.py"]


def test_commands_never_invoke_a_bare_megamaid(repo_root):
    """claude plugin install puts nothing on PATH; every invocation goes via --cli."""
    for path in (repo_root / "commands").glob("*.md"):
        text = path.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("megamaid ") or stripped.startswith("$ megamaid "):
                raise AssertionError(f"{path.name}: bare megamaid invocation: {stripped!r}")


def test_doctor_command_exists_with_frontmatter(repo_root):
    text = (repo_root / "commands" / "megamaid-doctor.md").read_text()
    assert text.startswith("---")
    assert "description:" in text
    assert "launch.py" in text
