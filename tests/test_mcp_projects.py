"""The MCP server addresses projects on the host, not inside a dead container.

`PROJECTS_DIR` was `/projects` — the retired Docker container's mount point —
and `_resolve_project` required every path to sit under it. Three of the four
tools (`megamaid_run`, `megamaid_status`, `megamaid_list_docs`) take a project
argument, so on a host where `/projects` does not exist they were all
unreachable: an absolute host path was refused for being outside the root, and
a bare name resolved to a directory that could never exist.

These tests pin both addressing forms, the one containment rule that survives
(bare names stay under the root), and — when the machine has one — a read-only
resolution against a real scraped project, which is the case no tmp_path
fixture can stand in for: it is the exact shape that used to fail.
"""

import asyncio
import importlib
import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


@pytest.fixture(scope="module")
def server():
    """Import megamaid_mcp.server with src/ on sys.path.

    Skips rather than errors when the [mcp] extra is absent, so the suite
    still runs in an environment that only installed the scraper deps.
    """
    pytest.importorskip("fastmcp", reason="megamaid_mcp needs the [mcp] extra")
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    return importlib.import_module("megamaid_mcp.server")


def _tool_error(server):
    """The exception type the server raises for a bad project argument."""
    from fastmcp.exceptions import ToolError

    return ToolError


def _real_host_project() -> pathlib.Path | None:
    """A scaffolded megamaid project on this machine with a readable run.

    Returns:
        The first `~/megamaid-*` directory that has both a project venv and a
        latest run carrying a manifest, or None when the machine has none.
        Purely a read: nothing here writes to, or runs anything inside, the
        project it picks.
    """
    for candidate in sorted(pathlib.Path.home().glob("megamaid-*")):
        if not (candidate / ".venv" / "bin" / "megamaid").exists():
            continue
        staging = candidate / "staging"
        if not staging.is_dir():
            continue
        for target_dir in sorted(staging.iterdir(), reverse=True):
            if not target_dir.is_dir():
                continue
            runs = sorted((d for d in target_dir.iterdir() if d.is_dir()), reverse=True)
            if runs and (runs[0] / "manifest.json").is_file():
                return candidate
    return None


def test_projects_root_is_not_a_container_mount(server, repo_root):
    """The default root must be somewhere that exists on a host."""
    source = (repo_root / "src" / "megamaid_mcp" / "server.py").read_text()
    assert "MEGAMAID_PROJECTS_DIR_INTERNAL" not in source, (
        "the _INTERNAL suffix was container vocabulary; the env var is MEGAMAID_PROJECTS_DIR"
    )
    assert '"/projects"' not in source, "/projects was the container's mount point"
    assert server.PROJECTS_DIR.is_dir(), (
        f"PROJECTS_DIR={server.PROJECTS_DIR} does not exist on this host — "
        "every project-taking tool is unusable in that state"
    )


def test_absolute_path_outside_the_projects_root_is_honoured(server, tmp_path, monkeypatch):
    """The regression itself: an absolute path must not need to sit under the root.

    Before the fix this raised "project must be within /projects".
    """
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path / "root")
    (tmp_path / "root").mkdir()
    elsewhere = tmp_path / "elsewhere" / "megamaid-example"
    elsewhere.mkdir(parents=True)

    assert server._resolve_project(str(elsewhere)) == elsewhere


def test_home_relative_path_is_expanded(server, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path / "root")
    project = tmp_path / "megamaid-tilde"
    project.mkdir()

    assert server._resolve_project("~/megamaid-tilde") == project


def test_bare_name_resolves_under_the_projects_root(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path)
    project = tmp_path / "megamaid-example"
    project.mkdir()

    assert server._resolve_project("megamaid-example") == project


def test_bare_name_cannot_traverse_out_of_the_projects_root(server, tmp_path, monkeypatch):
    """The one containment rule kept: a name stays a name.

    Not a privilege boundary — the server runs as the user and the
    absolute-path form deliberately reaches anywhere they can read. This keeps
    `"../../etc"` from quietly meaning something other than a sibling project.
    """
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path / "root")
    (tmp_path / "root").mkdir()
    (tmp_path / "sibling").mkdir()

    with pytest.raises(_tool_error(server), match="escapes the projects root"):
        server._resolve_project("../sibling")


def test_not_found_error_does_not_blame_a_container_mount(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path)

    with pytest.raises(_tool_error(server)) as excinfo:
        server._resolve_project("megamaid-nope")

    message = str(excinfo.value)
    assert "mounted" not in message, "the container is gone; stop advising a mount fix"
    assert "MEGAMAID_PROJECTS_DIR" in message


def test_project_cli_runs_the_project_venv_script_directly(server, tmp_path):
    """No server-side interpreter, no hand-built PYTHONPATH.

    The console script's shebang already names the project venv's python, and
    the old form derived site-packages from the *server's* sys.version_info —
    silently contributing nothing whenever a project venv sat on a different
    Python minor.
    """
    project = tmp_path / "megamaid-example"
    script = project / ".venv" / "bin" / "megamaid"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")

    assert server._project_cli(project) == [str(script)]


def test_project_cli_errors_when_the_venv_is_missing(server, tmp_path):
    project = tmp_path / "megamaid-bare"
    project.mkdir()

    with pytest.raises(_tool_error(server), match="No .venv/bin/megamaid"):
        server._project_cli(project)


def test_status_resolves_a_real_host_project_both_ways(server, monkeypatch):
    """End-to-end on a project living outside any container path.

    Skips cleanly on a machine with no scaffolded projects. Read-only: this
    calls `megamaid_status`, which only reads a manifest off disk.
    """
    project = _real_host_project()
    if project is None:
        pytest.skip("no scaffolded megamaid project with a completed run on this host")

    by_path = asyncio.run(server.megamaid_status(str(project)))
    assert by_path["status"] not in ("no_runs", "no_manifest"), by_path
    assert by_path["staging_dir"].startswith(str(project))
    assert by_path["stats"]["total"] >= 0

    monkeypatch.setattr(server, "PROJECTS_DIR", project.parent)
    by_name = asyncio.run(server.megamaid_status(project.name))
    assert by_name == by_path, "the bare-name and absolute-path forms must agree"
