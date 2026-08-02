"""The `upgrade` command: the CLI wiring around plan/apply/rollback.

`plan_project` is proven pure in tests/test_upgrade_plan.py — this file's job
is to prove the *command* does not undo that guarantee one layer up. Every
test here drives the real `megamaid upgrade` entry point via CliRunner, never
calling `impl.plan_project` etc. directly, so a regression in the CLI wiring
(an accidental `back_up()` call before the dry-run check, a swallowed
exception, a wrong exit code) is caught even if the underlying functions are
still individually correct.
"""

from __future__ import annotations

import pathlib
import shutil
import sys

import pytest
from click.testing import CliRunner

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid.cli import _sanitize_backup_component, cli  # noqa: E402

RUNTIME = SRC / "megamaid"


def _snapshot(root: pathlib.Path) -> dict[pathlib.Path, tuple[int, int]]:
    """Every path under `root` mapped to (size, mtime_ns): a changed key set
    catches a stray mkdir/create, changed values catch a stray rewrite."""
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def _project(tmp_path: pathlib.Path, name: str = "megamaid-proj") -> pathlib.Path:
    """A realistic scaffolded project: current runtime, bespoke targets/, and
    scraper output that must never be touched."""
    proj = tmp_path / name
    shutil.copytree(RUNTIME, proj / "megamaid")
    (proj / "targets").mkdir()
    (proj / "targets" / "example.py").write_text("TARGET_URL = 'https://example.com'\n")
    (proj / "staging").mkdir()
    (proj / "staging" / "run.json").write_text('{"irreplaceable": true}\n')
    return proj


def _mixed_project(tmp_path: pathlib.Path, name: str = "megamaid-mixed") -> pathlib.Path:
    """Same as `_project`, but with one hand-edited file and one deleted one —
    the case that must produce a refusal and, via ENTRY_POINTS, an
    unreachable add."""
    proj = _project(tmp_path, name)
    (proj / "megamaid" / "cli.py").write_text("# hand-written CLI\nx = 1\n")
    (proj / "megamaid" / "recon.py").unlink()
    return proj


def _run(args: list[str], **kwargs):
    return CliRunner().invoke(cli, args, **kwargs)


# --- --dry-run purity -------------------------------------------------------


def test_dry_run_writes_nothing_on_a_clean_project(tmp_path):
    """The property the whole feature depends on, proven through the real
    command line, not just the pure function underneath it."""
    proj = _project(tmp_path)
    before = _snapshot(tmp_path)

    result = _run(["upgrade", "--dry-run", str(proj)])

    after = _snapshot(tmp_path)
    assert before == after, "the tree changed under --dry-run"
    assert not (proj / ".megamaid-backups").exists()
    assert not (proj / ".megamaid-version").exists()
    assert result.exit_code == 0, result.output


def test_dry_run_writes_nothing_on_a_mixed_project(tmp_path):
    """Same guarantee, but on the path that actually has something to
    refuse and something to report unreachable — the branch most likely to
    tempt a stray write."""
    proj = _mixed_project(tmp_path)
    before = _snapshot(tmp_path)

    result = _run(["upgrade", "--dry-run", str(proj)])

    after = _snapshot(tmp_path)
    assert before == after, "the tree changed under --dry-run"
    assert not (proj / ".megamaid-backups").exists()
    # Not a clean converge: cli.py was refused, so exit code must say so.
    assert result.exit_code == 1, result.output
    assert "MM-32" in result.output
    assert "cli.py" in result.output
    assert "MM-35" in result.output


def test_dry_run_on_a_non_project_writes_nothing_and_reports_mm31(tmp_path):
    not_a_project = tmp_path / "not-a-project"
    not_a_project.mkdir()
    (not_a_project / "unrelated.txt").write_text("hello\n")
    before = _snapshot(tmp_path)

    result = _run(["upgrade", "--dry-run", str(not_a_project)])

    after = _snapshot(tmp_path)
    assert before == after
    assert result.exit_code == 2, result.output
    assert "MM-31" in result.output


# --- applying -----------------------------------------------------------


def test_apply_upgrades_a_clean_project_and_exits_zero(tmp_path):
    """A project already on today's exact runtime: every file classifies
    EXACT -> overwrite, nothing is refused, and the apply still runs (a
    same-content overwrite is a correct no-op, not something to special-case)."""
    proj = _project(tmp_path)

    result = _run(["upgrade", str(proj)])

    assert result.exit_code == 0, result.output
    assert (proj / "megamaid" / "base.py").read_bytes() == (RUNTIME / "base.py").read_bytes()
    assert (proj / ".megamaid-backups").is_dir()
    assert (proj / ".megamaid-version").exists()
    assert "Applied to 1 project(s)" in result.output


def test_apply_leaves_refused_files_alone_and_exits_one(tmp_path):
    proj = _mixed_project(tmp_path)
    original_cli = (proj / "megamaid" / "cli.py").read_bytes()

    result = _run(["upgrade", str(proj)])

    assert result.exit_code == 1, result.output
    assert (proj / "megamaid" / "cli.py").read_bytes() == original_cli
    # recon.py's only entry point (cli.py) is refused, so it lands unreachable.
    assert (proj / "megamaid" / "recon.py").exists()
    assert "MM-35" in result.output


def test_apply_never_touches_targets_or_staging(tmp_path):
    proj = _mixed_project(tmp_path)
    before = {
        p: p.read_bytes()
        for p in list((proj / "targets").rglob("*")) + list((proj / "staging").rglob("*"))
        if p.is_file()
    }

    _run(["upgrade", str(proj)])

    after = {
        p: p.read_bytes()
        for p in list((proj / "targets").rglob("*")) + list((proj / "staging").rglob("*"))
        if p.is_file()
    }
    assert before == after


def test_multiple_projects_ask_for_confirmation_unless_yes(tmp_path):
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")

    declined = _run(["upgrade", str(a), str(b)], input="n\n")
    assert declined.exit_code != 0
    assert not (a / ".megamaid-backups").exists()

    confirmed = _run(["upgrade", "--yes", str(a), str(b)])
    assert confirmed.exit_code == 0, confirmed.output
    assert (a / ".megamaid-backups").is_dir()
    assert (b / ".megamaid-backups").is_dir()


# --- rollback -------------------------------------------------------------


def test_rollback_round_trips_through_the_cli(tmp_path):
    proj = _mixed_project(tmp_path)
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}

    apply_result = _run(["upgrade", str(proj)])
    assert apply_result.exit_code == 1, apply_result.output

    rollback_result = _run(["upgrade", "--rollback", str(proj)])
    assert rollback_result.exit_code == 0, rollback_result.output

    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after == before


def test_rollback_with_no_backup_reports_mm36_not_a_traceback(tmp_path):
    proj = _project(tmp_path)

    result = _run(["upgrade", "--rollback", str(proj)])

    assert result.exit_code == 2, result.output
    assert "MM-36" in result.output
    # A readable message, not a bare traceback: no exception class name leaked.
    assert "RuntimeError" not in result.output


# --- the vendored-cli.py guard ---------------------------------------------


def test_missing_implementation_is_a_readable_message_not_an_importerror(tmp_path, monkeypatch):
    """`cli.py` is vendored into every scraped project; `megamaid_setup` is
    not. Simulate that: `upgrade` must explain itself, not dump a traceback."""
    monkeypatch.setitem(sys.modules, "megamaid_setup", None)
    monkeypatch.setitem(sys.modules, "megamaid_setup.upgrade", None)
    monkeypatch.setitem(sys.modules, "megamaid_setup.manifest", None)

    result = _run(["upgrade", str(tmp_path)])

    assert result.exit_code != 0
    assert "runs from the megamaid plugin" in result.output
    assert "launch.py" in result.output
    assert "ImportError" not in result.output


def test_the_missing_implementation_is_actually_simulated(monkeypatch):
    """Negative control: if this stopped raising ImportError, the test above
    would be certifying nothing."""
    monkeypatch.setitem(sys.modules, "megamaid_setup", None)
    with pytest.raises(ImportError):
        from megamaid_setup import upgrade  # noqa: F401


# --- input hygiene ----------------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ""])
def test_sanitize_backup_component_rejects_path_escapes(bad):
    with pytest.raises(SystemExit):
        _sanitize_backup_component(bad, "version")


@pytest.mark.parametrize("good", ["0.9.1", "1.0.0-rc1", "20260801-130000", "deadbeef1234"])
def test_sanitize_backup_component_passes_safe_values(good):
    assert _sanitize_backup_component(good, "version") == good
