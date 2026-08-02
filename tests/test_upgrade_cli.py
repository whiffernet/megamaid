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

import hashlib
import pathlib
import shlex
import shutil
import sys

import pytest
from click.testing import CliRunner

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid.cli import INTERRUPTED_EXIT, _sanitize_backup_component, cli  # noqa: E402

RUNTIME = SRC / "megamaid"


def _snapshot(root: pathlib.Path) -> dict[pathlib.Path, tuple[int, int]]:
    """Every path under `root` mapped to (size, mtime_ns): a changed key set
    catches a stray mkdir/create, changed values catch a stray rewrite."""
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def _hash_snapshot(root: pathlib.Path) -> dict[pathlib.Path, str]:
    """Every file under `root` mapped to its sha256. Stronger than `_snapshot`:
    proves content genuinely did not change, rather than inferring it from
    size/mtime — the two of which could coincidentally line up (same size,
    an mtime a filesystem doesn't record at high enough resolution) in a way
    a swapped-in file of identical size would slip through undetected."""
    return {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _printed_recovery_command(output: str) -> str:
    """The command line the mid-apply failure message told the user to run.

    Pulled out of the real output rather than reconstructed, so the test can
    execute what was actually printed. The line following "Recover with:" is
    the command and nothing else — that shape is the contract this asserts.
    """
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.rstrip().endswith("Recover with:"):
            assert index + 1 < len(lines), f"'Recover with:' names no command:\n{output}"
            return lines[index + 1].strip()
    raise AssertionError(f"no 'Recover with:' line in the failure message:\n{output}")


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


# --- --dry-run --rollback: the critical regression -------------------------
#
# The brief's own skeleton put the `do_rollback` branch ahead of the
# `dry_run` check, with nothing inside it consulting `dry_run` at all — so
# `--dry-run --rollback` fell straight through to the real, destructive
# `impl.rollback()`: a hand edit sitting on top of a backup gets silently
# replaced by that backup's older content, exit 0, no warning. Snapshotted
# by content hash (not mtime/size, which a same-size restore could satisfy
# coincidentally) specifically because that is what actually proves nothing
# changed.


def test_dry_run_rollback_writes_nothing_even_with_a_real_backup_present(tmp_path):
    """Recreates the exact destructive scenario: apply once (creating a
    backup that captures the refused file's *first* hand edit), then make a
    *second* hand edit on top of it. A real rollback would blow the second
    edit away and restore the first. --dry-run --rollback must touch none
    of it."""
    proj = _mixed_project(tmp_path)
    apply_result = _run(["upgrade", str(proj)])
    assert apply_result.exit_code == 1, apply_result.output
    assert (proj / ".megamaid-backups").is_dir()

    second_edit = "# a SECOND hand edit made after the backup exists\nx = 2\n"
    (proj / "megamaid" / "cli.py").write_text(second_edit)

    before = _hash_snapshot(tmp_path)
    result = _run(["upgrade", "--dry-run", "--rollback", str(proj)])
    after = _hash_snapshot(tmp_path)

    assert before == after, "content changed under --dry-run --rollback"
    assert (proj / "megamaid" / "cli.py").read_text() == second_edit
    assert result.exit_code == 0, result.output
    assert "would restore" in result.output
    assert "Nothing written" in result.output


def test_dry_run_rollback_names_the_backup_it_would_restore(tmp_path):
    proj = _project(tmp_path)
    apply_result = _run(["upgrade", str(proj)])
    assert apply_result.exit_code == 0, apply_result.output

    before = _hash_snapshot(tmp_path)
    result = _run(["upgrade", "--dry-run", "--rollback", str(proj)])
    after = _hash_snapshot(tmp_path)

    assert before == after
    assert result.exit_code == 0, result.output
    assert "would restore" in result.output
    assert str(proj) in result.output
    # The backup dir's timestamp, parsed the same way the CLI itself does —
    # so the preview is concrete (names an actual timestamp), not vague.
    from megamaid_setup.upgrade import parse_backup_name

    backup_dir = next((proj / ".megamaid-backups").iterdir())
    timestamp, _version = parse_backup_name(backup_dir.name)
    assert timestamp in result.output


def test_dry_run_rollback_reports_mm36_when_there_is_no_backup(tmp_path):
    proj = _project(tmp_path)  # never applied — nothing to roll back to

    before = _hash_snapshot(tmp_path)
    result = _run(["upgrade", "--dry-run", "--rollback", str(proj)])
    after = _hash_snapshot(tmp_path)

    assert before == after
    assert result.exit_code == 2, result.output
    assert "MM-36" in result.output
    assert "Nothing written" in result.output


# --- other flag combinations audited for a write reaching --dry-run --------


def test_dry_run_with_yes_still_writes_nothing(tmp_path):
    """--yes only ever suppresses the multi-project confirmation prompt; it
    must never itself unlock a write when --dry-run is also set."""
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    before = _hash_snapshot(tmp_path)

    result = _run(["upgrade", "--dry-run", "--yes", str(a), str(b)])

    after = _hash_snapshot(tmp_path)
    assert before == after
    assert result.exit_code == 0, result.output


def test_dry_run_rollback_with_multiple_projects_writes_nothing(tmp_path):
    a, b = _mixed_project(tmp_path, "a"), _project(tmp_path, "b")
    _run(["upgrade", str(a)])
    _run(["upgrade", str(b)])
    before = _hash_snapshot(tmp_path)

    result = _run(["upgrade", "--dry-run", "--rollback", str(a), str(b)])

    after = _hash_snapshot(tmp_path)
    assert before == after
    assert result.exit_code == 0, result.output
    assert result.output.count("would restore") == 2


# --- mid-apply failure: which phase, and does the recovery actually work ---


def test_apply_failure_after_the_backup_completes_names_a_recovery_command_that_works(
    tmp_path, monkeypatch
):
    """A failure in the copy loop, once back_up() has already finished: the
    message must say "partially applied" (not "nothing modified"), name the
    exact --rollback invocation, and that invocation must genuinely restore
    the project — not just look plausible in the printed text.

    The printed line is parsed with shlex and its arguments are handed
    straight to the runner, so what is executed here *is* what was printed.
    Completeness is asserted on the parsed tokens rather than by substring,
    because the two ways this line can be wrong are both invisible to a
    substring check: a bare `megamaid …`, which is not on PATH after a plugin
    install and so cannot run at all, and a relative project path, which
    silently resolves against whatever directory the reader happens to be in
    when they paste it.

    Everything is typed *relatively* here — `./megamaid upgrade megamaid-mixed`
    from the parent directory — which is what makes those two assertions bite.
    Run with an already-absolute `tmp_path` and an absolute `sys.argv[0]`, they
    pass whether or not `_recovery_command()` absolutises anything, so removing
    either `.resolve()` or `os.path.abspath()` leaves the test green and the
    guard is decorative. With a relative cwd-dependent invocation, removing
    either one turns it red."""
    proj = _mixed_project(tmp_path)
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}

    # `cd <parent> && ./megamaid upgrade megamaid-mixed`
    launched_as = tmp_path / "megamaid"
    launched_as.write_text("#!/bin/sh\n")
    launched_as.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["./megamaid", "upgrade", proj.name])

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def _flaky_copy2(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", _flaky_copy2)
    result = _run(["upgrade", proj.name])  # relative, as the user typed it
    monkeypatch.undo()  # restore the real copy2 before touching the project again

    assert result.exit_code == 2, result.output
    assert "apply failed after the backup completed" in result.output
    assert "Nothing in megamaid/ was modified" not in result.output

    printed = _printed_recovery_command(result.output)
    executable, *arguments = shlex.split(printed)

    assert pathlib.Path(executable).is_absolute(), (
        f"the recovery command names {executable!r}, which is not a runnable path — a plugin "
        f"install puts nothing on PATH, and a cwd-relative path breaks the moment the reader "
        f"is somewhere else: {printed!r}"
    )
    assert arguments == ["upgrade", "--rollback", str(proj.resolve())], (
        f"the recovery command is incomplete or does not name the project by absolute path: "
        f"{printed!r}"
    )

    backups = sorted((proj / ".megamaid-backups").iterdir())
    assert len(backups) == 1, "a complete backup must exist for the printed command to work"

    # Run what was printed. The runner stands in for the executable token;
    # every argument after it is the printed line's own, unmodified.
    rollback_result = _run(arguments)
    assert rollback_result.exit_code == 0, rollback_result.output
    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after == before, "the exact command the CLI printed did not actually restore the project"


def test_the_recovery_command_is_quoted_for_a_path_containing_a_space(tmp_path, monkeypatch):
    """Project directories live wherever the user put them. An unquoted path
    with a space in it parses as two arguments and the paste fails."""
    proj = _mixed_project(tmp_path, "mega maid spaced")

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def _flaky_copy2(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", _flaky_copy2)
    result = _run(["upgrade", str(proj)])
    monkeypatch.undo()

    _, *arguments = shlex.split(_printed_recovery_command(result.output))
    assert arguments == ["upgrade", "--rollback", str(proj.resolve())]


def test_apply_failure_during_the_backup_itself_says_nothing_was_modified(tmp_path, monkeypatch):
    """A failure inside back_up(), before the copy loop ever starts: the
    message must say nothing was modified, and must NOT point at --rollback
    — the backup that command would restore may not exist or be
    incomplete."""
    proj = _project(tmp_path)
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}

    def _raise(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", _raise)
    result = _run(["upgrade", str(proj)])
    monkeypatch.undo()

    assert result.exit_code == 2, result.output
    assert "backup failed" in result.output
    assert "Nothing in megamaid/ was modified" in result.output
    assert "--rollback" not in result.output

    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after == before


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


# --- Ctrl-C mid-apply: an interrupt is not a traceback ---------------------
#
# KeyboardInterrupt is a BaseException, so `except Exception` never saw it: an
# interrupt escaped the handler and the user got a bare traceback at the exact
# moment their runtime was half-written and they most needed the --rollback
# line. Which of the two messages is correct depends on which side of back_up()
# the interrupt landed on, and both directions are asserted here.


def test_interrupt_in_the_copy_loop_prints_the_recovery_command(tmp_path, monkeypatch):
    """Past back_up(): megamaid/ is genuinely mid-write and a complete backup
    exists, so the user gets the same guidance a crash would give them."""
    proj = _mixed_project(tmp_path)

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def _interrupt_on_second(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", _interrupt_on_second)
    result = _run(["upgrade", str(proj)])
    monkeypatch.undo()

    assert "Traceback" not in result.output, result.output
    assert "interrupted after the backup completed" in result.output
    assert "may be left in a mixed state" in result.output
    assert result.exit_code == INTERRUPTED_EXIT, result.output


def test_the_recovery_command_printed_on_an_interrupt_actually_works(tmp_path, monkeypatch):
    """Same standard the crash path is held to: the line is executed, not
    merely inspected."""
    proj = _mixed_project(tmp_path)
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def _interrupt_on_second(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", _interrupt_on_second)
    result = _run(["upgrade", str(proj)])
    monkeypatch.undo()

    _, *arguments = shlex.split(_printed_recovery_command(result.output))
    assert _run(arguments).exit_code == 0

    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after == before


def test_interrupt_during_the_backup_says_nothing_was_modified(tmp_path, monkeypatch):
    """Before the copy loop: nothing in megamaid/ has been touched, and the
    backup itself may be a half-written copytree. Pointing this user at
    --rollback would restore a partial tree over an intact one."""
    proj = _project(tmp_path)
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}

    def _interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(shutil, "copytree", _interrupt)
    result = _run(["upgrade", str(proj)])
    monkeypatch.undo()

    assert "Traceback" not in result.output, result.output
    assert "Nothing in megamaid/ was modified" in result.output
    assert "--rollback" not in result.output
    assert result.exit_code == INTERRUPTED_EXIT, result.output

    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after == before


def test_an_interrupt_stops_the_run_instead_of_moving_to_the_next_project(tmp_path, monkeypatch):
    """Ctrl-C means stop. A crash on project A is a reason to carry on to B;
    an interrupt is an instruction not to."""
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")

    def _interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(shutil, "copytree", _interrupt)
    result = _run(["upgrade", "--yes", str(a), str(b)])
    monkeypatch.undo()

    assert result.exit_code == INTERRUPTED_EXIT, result.output
    assert not (b / ".megamaid-backups").exists(), "the second project was still processed"
