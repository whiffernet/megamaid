"""Launcher unit tests. The launcher runs before dependencies exist, so it is
stdlib-only and every subprocess call is injected."""

import ast
import importlib.util

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
    mod = _load(repo_root)
    assert mod.plugin_version(repo_root) == "0.9.0"


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
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        return None

    mod.ensure_venv(repo_root, venv, "0.9.0", runner=runner, log=mod._log)
    assert "build start" in seen["log_at_call_time"]
