"""Launcher unit tests. The launcher runs before dependencies exist, so it is
stdlib-only and every subprocess call is injected."""

import ast
import importlib.util
import json
import subprocess

import pytest

STDLIB_OK = {
    "__future__",
    "argparse",
    "json",
    "os",
    "pathlib",
    "subprocess",
    "sys",
    "venv",
    "shutil",
    "datetime",
    "typing",
}


def _launch_path(repo_root):
    return repo_root / "scripts" / "launch.py"


def _load(repo_root):
    path = _launch_path(repo_root)
    assert path.exists(), f"missing {path}"
    spec = importlib.util.spec_from_file_location("mm_launch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_entry_points(venv_dir):
    """Create fake, executable console scripts so a runner's "pip install"
    looks like it actually produced a usable venv."""
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("megamaid-mcp", "megamaid"):
        script = bin_dir / name
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)


def test_launcher_imports_only_stdlib(repo_root):
    tree = ast.parse(_launch_path(repo_root).read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    assert found <= STDLIB_OK, f"launcher must stay stdlib-only, found {found - STDLIB_OK}"


def test_plugin_version_reads_the_manifest(repo_root):
    """Compared against plugin.json, not a literal.

    A hardcoded "0.9.0" here would turn the suite red on the first version
    bump — a self-breaking test rather than a guard. The stamp values in the
    tmp_path tests below stay literal on purpose: those are arbitrary
    sentinels for stamp-comparison logic and never touch the real manifest.
    """
    mod = _load(repo_root)
    expected = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())["version"]
    assert expected, "plugin.json carries no version — nothing to compare against"
    assert mod.plugin_version(repo_root) == expected


def test_state_dir_honours_env_override(repo_root, tmp_path, monkeypatch):
    mod = _load(repo_root)
    monkeypatch.setenv("MEGAMAID_STATE_DIR", str(tmp_path / "custom"))
    assert mod.state_dir() == tmp_path / "custom"


def test_venv_is_not_current_when_absent(repo_root, tmp_path):
    mod = _load(repo_root)
    assert mod.venv_is_current(tmp_path / "nope", "0.9.0") is False


def test_venv_is_not_current_when_stamp_differs(repo_root, tmp_path):
    mod = _load(repo_root)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".plugin-version").write_text("0.8.0\n")
    assert mod.venv_is_current(venv, "0.9.0") is False


def test_venv_is_current_when_stamp_matches(repo_root, tmp_path):
    mod = _load(repo_root)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".plugin-version").write_text("0.9.0\n")
    assert mod.venv_is_current(venv, "0.9.0") is True


def test_ensure_venv_skips_work_when_current(repo_root, tmp_path):
    mod = _load(repo_root)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".plugin-version").write_text("0.9.0\n")
    calls = []
    mod.ensure_venv(
        repo_root, venv, "0.9.0", runner=lambda *a, **k: calls.append(a), log=lambda m: None
    )
    assert calls == [], "a current venv must not be rebuilt"


def test_ensure_venv_stamps_only_after_successful_install(repo_root, tmp_path):
    """A failed pip install must not leave a stamp claiming the venv is good."""
    mod = _load(repo_root)
    venv = tmp_path / "venv"

    def failing_runner(cmd, **kwargs):
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        raise mod.LaunchError("MM-13", "pip failed", "retry")

    with pytest.raises(mod.LaunchError) as excinfo:
        mod.ensure_venv(repo_root, venv, "0.9.0", runner=failing_runner, log=lambda m: None)
    assert excinfo.value.code == "MM-13"
    assert not (venv / ".plugin-version").exists()


def test_log_is_written_before_slow_work(repo_root, tmp_path, monkeypatch):
    """Claude Code SIGKILLs on MCP timeout; a start record must already be on disk."""
    mod = _load(repo_root)
    monkeypatch.setenv("MEGAMAID_STATE_DIR", str(tmp_path))
    venv = tmp_path / "venv"
    seen = {}

    def runner(cmd, **kwargs):
        seen["log_at_call_time"] = (tmp_path / "launch.log").read_text()
        _stub_entry_points(venv)
        return None

    mod.ensure_venv(repo_root, venv, "0.9.0", runner=runner, log=mod._log)
    assert "build start" in seen["log_at_call_time"]


def test_ensure_venv_captures_pip_stdout_so_it_cannot_corrupt_mcp_framing(repo_root, tmp_path):
    """pip must not inherit fd 1: os.execv later hands that fd to the MCP server
    for JSON-RPC framing, and any pip resolver/build chatter on it would corrupt
    the handshake."""
    mod = _load(repo_root)
    venv = tmp_path / "venv"
    seen = {}

    def runner(cmd, **kwargs):
        seen["kwargs"] = kwargs
        _stub_entry_points(venv)
        return None

    mod.ensure_venv(repo_root, venv, "0.9.0", runner=runner, log=lambda m: None)
    assert seen["kwargs"]["stdout"] == subprocess.PIPE
    assert seen["kwargs"]["stderr"] == subprocess.STDOUT
    assert seen["kwargs"]["text"] is True


def test_ensure_venv_wraps_a_real_called_process_error_as_mm13(repo_root, tmp_path):
    """The except-Exception branch must actually be reachable, not just the
    LaunchError passthrough — exercise it with a realistic pip failure."""
    mod = _load(repo_root)
    venv = tmp_path / "venv"

    def failing_runner(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, output="ERROR: Could not find a version that satisfies the requirement\n"
        )

    with pytest.raises(mod.LaunchError) as excinfo:
        mod.ensure_venv(repo_root, venv, "0.9.0", runner=failing_runner, log=lambda m: None)
    assert excinfo.value.code == "MM-13"
    assert "launch.log" in excinfo.value.fix
    assert not (venv / ".plugin-version").exists()


def test_ensure_venv_requires_entry_points_before_stamping(repo_root, tmp_path):
    """A pip that exits 0 without producing the console scripts must not be
    trusted — the venv should stay un-stamped so the next launch retries."""
    mod = _load(repo_root)
    venv = tmp_path / "venv"

    def runner(cmd, **kwargs):
        # "Successful" pip that does not actually produce console scripts.
        return None

    with pytest.raises(mod.LaunchError) as excinfo:
        mod.ensure_venv(repo_root, venv, "0.9.0", runner=runner, log=lambda m: None)
    assert excinfo.value.code == "MM-16"
    assert not (venv / ".plugin-version").exists()


def test_cli_mode_targets_the_megamaid_script(repo_root, tmp_path, monkeypatch):
    mod = _load(repo_root)
    monkeypatch.setenv("MEGAMAID_STATE_DIR", str(tmp_path))
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".plugin-version").write_text("0.9.0\n")
    # Must be executable, not just present: main()'s exec guard checks X_OK
    # before calling os.execv, so a plain write_text() (mode 0644) would trip
    # the guard and never reach the exec call this test is asserting on.
    _stub_entry_points(venv)

    captured = {}
    monkeypatch.setattr(mod.os, "execv", lambda path, args: captured.update(path=path, args=args))
    mod.main(["--cli", "recon", "https://example.com"])

    assert captured["path"].endswith("/bin/megamaid")
    assert captured["args"][1:] == ["recon", "https://example.com"]


def test_default_mode_targets_the_mcp_script(repo_root, tmp_path, monkeypatch):
    mod = _load(repo_root)
    monkeypatch.setenv("MEGAMAID_STATE_DIR", str(tmp_path))
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".plugin-version").write_text("0.9.0\n")
    _stub_entry_points(venv)

    captured = {}
    monkeypatch.setattr(mod.os, "execv", lambda path, args: captured.update(path=path, args=args))
    mod.main([])

    assert captured["path"].endswith("/bin/megamaid-mcp")


def test_cli_mode_passes_flags_through_untouched(repo_root, tmp_path, monkeypatch):
    """--dry-run belongs to megamaid, not to the launcher's own parser."""
    mod = _load(repo_root)
    monkeypatch.setenv("MEGAMAID_STATE_DIR", str(tmp_path))
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".plugin-version").write_text("0.9.0\n")
    _stub_entry_points(venv)

    captured = {}
    monkeypatch.setattr(mod.os, "execv", lambda path, args: captured.update(path=path, args=args))
    mod.main(["--cli", "upgrade", "--dry-run", "~/megamaid-foo"])

    assert captured["args"][1:] == ["upgrade", "--dry-run", "~/megamaid-foo"]
