"""Applying: back up first, never touch what we did not promise to touch."""

import pathlib
import shutil
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid_setup.manifest import load_manifest  # noqa: E402
from megamaid_setup.upgrade import (  # noqa: E402
    BACKUP_DIR,
    RETAIN,
    BackupFailed,
    apply_plan,
    back_up,
    latest_backup,
    parse_backup_name,
    plan_project,
    rollback,
)

RUNTIME = SRC / "megamaid"


def _project(tmp_path, name="megamaid-t") -> pathlib.Path:
    """A project on an older runtime, with output and bespoke code beside it."""
    proj = tmp_path / name
    (proj / "megamaid").mkdir(parents=True)
    for f in ("base.py", "cli.py", "__init__.py"):
        (proj / "megamaid" / f).write_text("# old\n")
    (proj / "targets").mkdir()
    (proj / "targets" / "site.py").write_text("# bespoke, must survive\n")
    (proj / "staging").mkdir()
    (proj / "staging" / "run.json").write_text('{"irreplaceable": true}')
    return proj


def test_backup_is_a_sibling_not_a_child(tmp_path):
    """Inside megamaid/ it would be swept into the next backup, and rollback
    would delete the directory it is about to read."""
    proj = _project(tmp_path)
    dest = back_up(proj, "0.9.1", "20260802-000000")
    assert dest.parent == proj / BACKUP_DIR
    assert BACKUP_DIR not in [p.name for p in (proj / "megamaid").rglob("*")]


def test_backup_excludes_pycache(tmp_path):
    proj = _project(tmp_path)
    (proj / "megamaid" / "__pycache__").mkdir()
    (proj / "megamaid" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    dest = back_up(proj, "0.9.1", "20260802-000000")
    assert not (dest / "__pycache__").exists()


def test_apply_never_touches_targets_or_staging(tmp_path):
    proj = _project(tmp_path)
    before = {
        p: p.read_bytes()
        for p in list((proj / "targets").rglob("*")) + list((proj / "staging").rglob("*"))
        if p.is_file()
    }
    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    after = {
        p: p.read_bytes()
        for p in list((proj / "targets").rglob("*")) + list((proj / "staging").rglob("*"))
        if p.is_file()
    }
    assert before == after


def test_refused_files_are_left_alone(tmp_path):
    proj = _project(tmp_path)
    original = (proj / "megamaid" / "cli.py").read_bytes()
    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    assert (proj / "megamaid" / "cli.py").read_bytes() == original


def test_rollback_restores_byte_for_byte(tmp_path):
    proj = _project(tmp_path)
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    rollback(proj)
    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after == before


def test_rollback_without_a_backup_is_an_error(tmp_path):
    proj = _project(tmp_path)
    with pytest.raises(RuntimeError, match="MM-36"):
        rollback(proj)


def test_repeated_upgrades_do_not_compound(tmp_path):
    """Backups must not accumulate inside each other, and only RETAIN are kept."""
    proj = _project(tmp_path)
    for i in range(RETAIN + 2):
        plan = plan_project(proj, RUNTIME, load_manifest())
        apply_plan(plan, RUNTIME, "0.9.1", f"20260802-00000{i}")
    kept = sorted((proj / BACKUP_DIR).iterdir())
    assert len(kept) == RETAIN
    counts = {len(list(b.rglob("*.py"))) for b in kept}
    assert len(counts) == 1, f"backups are compounding: {counts}"


def test_rollback_picks_the_chronologically_newest_backup_across_a_version_bump(tmp_path):
    """Version 0.10.0 sorts before 0.9.1 as a plain string: the char '1' sorts
    below '9'. If backup directories were ordered by version, rollback would
    restore the older release even though it was backed up first and a newer
    one exists. Naming must sort by timestamp, not version, for
    `sorted(...)[-1]` to mean "most recent"."""
    proj = _project(tmp_path)

    (proj / "megamaid" / "cli.py").write_text("# state at 0.9.1\n")
    back_up(proj, "0.9.1", "20260101-000000")  # older release, backed up first

    (proj / "megamaid" / "cli.py").write_text("# state at 0.10.0\n")
    back_up(proj, "0.10.0", "20260601-000000")  # newer release, backed up second

    (proj / "megamaid" / "cli.py").write_text("# live edit after both backups\n")

    restored = rollback(proj)
    assert restored.name.endswith("0.10.0"), f"restored the wrong backup: {restored.name}"
    assert (proj / "megamaid" / "cli.py").read_text() == "# state at 0.10.0\n"


def test_retention_prunes_the_chronologically_oldest_backup_across_a_version_bump(tmp_path):
    """Same version-string sort trap, for back_up()'s retention prune: it must
    drop the oldest backup by creation time, not whichever version string
    happens to sort first."""
    proj = _project(tmp_path)
    releases = [
        ("0.9.1", "20260101-000000"),  # oldest — must be pruned
        ("0.9.2", "20260201-000000"),
        ("0.10.0", "20260301-000000"),  # would sort FIRST as a string if version led
        ("0.10.1", "20260401-000000"),  # newest
    ]
    for version, now in releases:
        back_up(proj, version, now)

    kept = sorted(p.name for p in (proj / BACKUP_DIR).iterdir())
    assert len(kept) == RETAIN
    assert not any(name.endswith("0.9.1") for name in kept), (
        f"the chronologically oldest backup should have been pruned, got {kept}"
    )
    for version, _ in releases[1:]:
        assert any(name.endswith(version) for name in kept), f"{version} missing from {kept}"


# --- latest_backup() — the pure, read-only preview of what rollback() would do ---


def test_latest_backup_is_none_when_there_is_no_backups_dir(tmp_path):
    proj = _project(tmp_path)
    assert latest_backup(proj) is None


def test_latest_backup_is_none_when_the_backups_dir_is_empty(tmp_path):
    proj = _project(tmp_path)
    (proj / BACKUP_DIR).mkdir()
    assert latest_backup(proj) is None


def test_latest_backup_matches_what_rollback_would_restore(tmp_path):
    """latest_backup() must never disagree with rollback()'s own choice —
    it exists specifically so a caller can preview that choice without
    making it."""
    proj = _project(tmp_path)
    back_up(proj, "0.9.1", "20260101-000000")
    back_up(proj, "0.10.0", "20260601-000000")  # newest, despite the version-string trap

    newest = latest_backup(proj)
    assert newest is not None
    assert newest.name.endswith("0.10.0")

    restored = rollback(proj)
    assert restored == newest


def test_latest_backup_never_writes_anything(tmp_path):
    proj = _project(tmp_path)
    back_up(proj, "0.9.1", "20260101-000000")
    before = {p: p.stat().st_mtime_ns for p in sorted(proj.rglob("*"))}
    latest_backup(proj)
    latest_backup(proj / "nonexistent")
    after = {p: p.stat().st_mtime_ns for p in sorted(proj.rglob("*"))}
    assert before == after


# --- parse_backup_name() — presentation only, must degrade rather than raise ---


def test_parse_backup_name_splits_the_fixed_width_timestamp_from_the_version():
    timestamp, version = parse_backup_name("20260802-064155-0.9.1")
    assert timestamp == "20260802-064155"
    assert version == "0.9.1"


def test_parse_backup_name_handles_a_prerelease_version_containing_a_dash():
    """The exact case back_up()'s own docstring calls out: version may itself
    contain '-'. A naive split-on-first/last-dash would get this wrong;
    the fixed 15-character offset must not."""
    timestamp, version = parse_backup_name("20260802-064155-1.0.0-rc1")
    assert timestamp == "20260802-064155"
    assert version == "1.0.0-rc1"


def test_parse_backup_name_degrades_gracefully_on_an_unrecognized_name():
    """Presentation only, never load-bearing: an unexpected name must not
    raise, just come back with no parsed version."""
    timestamp, version = parse_backup_name("not-a-backup-name")
    assert timestamp == "not-a-backup-name"
    assert version == ""


# --- BackupFailed — apply_plan must say WHICH phase failed ---------------


def test_apply_plan_wraps_a_backup_phase_failure_in_backup_failed(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    plan = plan_project(proj, RUNTIME, load_manifest())

    def _raise(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", _raise)

    with pytest.raises(BackupFailed):
        apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")


def test_apply_plan_touches_nothing_in_megamaid_when_the_backup_phase_fails(tmp_path, monkeypatch):
    """The whole reason BackupFailed is a distinct exception: by definition,
    nothing in megamaid/ can have changed yet when it's raised."""
    proj = _project(tmp_path)
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    plan = plan_project(proj, RUNTIME, load_manifest())

    def _raise(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", _raise)

    with pytest.raises(BackupFailed):
        apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")

    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after == before


def test_apply_plan_does_not_wrap_a_copy_loop_failure_as_backup_failed(tmp_path, monkeypatch):
    """A failure once the copy loop has started is a different situation —
    the backup already succeeded — and must NOT be mistaken for BackupFailed,
    or a caller would wrongly tell the user nothing was touched."""
    proj = _project(tmp_path)
    plan = plan_project(proj, RUNTIME, load_manifest())

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def _flaky_copy2(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", _flaky_copy2)

    with pytest.raises(OSError) as excinfo:
        apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    assert not isinstance(excinfo.value, BackupFailed)
    # And the backup it left behind is real and complete, not partial.
    backups = sorted((proj / BACKUP_DIR).iterdir())
    assert len(backups) == 1
    assert {p.name for p in backups[0].glob("*.py")} == {
        p.name for p in (proj / "megamaid").glob("*.py")
    }
