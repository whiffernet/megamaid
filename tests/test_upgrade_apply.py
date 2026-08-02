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
    VERSION_STAMP,
    BackupFailed,
    apply_plan,
    back_up,
    backups,
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


# --- backup-directory hygiene: only a real backup may be restored ---------
#
# `.megamaid-backups/` is a plain directory in the user's project. Anything can
# land in it: a note, an editor swapfile, a tarball parked there on purpose, or
# a partial copytree left behind by a back_up() that died on a full disk. An
# unfiltered `sorted(root.iterdir())` treats every one of those as a backup.
# The stray *directory* case is the dangerous one — it restores cleanly, exit 0,
# and leaves the project with an empty runtime.


def _strays(root: pathlib.Path) -> None:
    """Drop one of every kind of non-backup entry into a backups directory.

    Every name sorts after a `YYYYMMDD-HHMMSS-<version>` timestamp, so an
    unfiltered sort picks one of these as "most recent".
    """
    (root / "zzz-NOTES.txt").write_text("remember to backport images.py\n")
    (root / "zzz-archive.tar.gz").write_bytes(b"\x1f\x8b")
    partial = root / "zzz-tmp-partial"
    partial.mkdir()
    (partial / "half-copied.py").write_text("# a copytree that died on a full disk\n")


def test_latest_backup_ignores_stray_entries(tmp_path):
    """A note, a tarball and a half-written directory are not backups."""
    proj = _project(tmp_path)
    real = back_up(proj, "0.9.1", "20260101-000000")
    _strays(proj / BACKUP_DIR)

    assert latest_backup(proj) == real


def test_latest_backup_is_none_when_only_strays_are_present(tmp_path):
    """No backup at all is MM-36, which rollback() reports and recovers from.
    Picking a stray instead is what turns "nothing to restore" into data loss."""
    proj = _project(tmp_path)
    (proj / BACKUP_DIR).mkdir()
    _strays(proj / BACKUP_DIR)

    assert latest_backup(proj) is None


def test_latest_backup_ignores_a_symlink_pointing_at_a_backup(tmp_path):
    """A symlink named like a backup would make rollback follow a path out of
    the project entirely, and retention prune it as though it owned the target."""
    proj = _project(tmp_path)
    real = back_up(proj, "0.9.1", "20260101-000000")
    (proj / BACKUP_DIR / "20260601-000000-9.9.9").symlink_to(real)

    assert latest_backup(proj) == real


def test_rollback_leaves_the_runtime_intact_when_a_stray_directory_sorts_last(tmp_path):
    """The silent case. `sorted(iterdir())[-1]` picks the stray directory,
    rmtree deletes the runtime, copytree restores an empty tree, and the
    command reports success. Reproduced on a copy of megamaid-amazon: 17
    runtime files before, 1 after, exit 0, "restored"."""
    proj = _project(tmp_path)
    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert before, "fixture is vacuous — no runtime files to lose"

    _strays(proj / BACKUP_DIR)
    rollback(proj)

    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after, "the runtime was silently emptied"
    assert "half-copied.py" not in {p.name for p in (proj / "megamaid").glob("*")}


def test_rollback_leaves_the_runtime_intact_when_a_stray_file_sorts_last(tmp_path):
    """The loud variant: NotADirectoryError out of copytree, *after* rmtree has
    already deleted megamaid/. Reproduced on a copy of megamaid-allrecipes,
    which was left with no megamaid/ directory at all."""
    proj = _project(tmp_path)
    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")

    (proj / BACKUP_DIR / "zzz-NOTES.txt").write_text("not a backup\n")
    rollback(proj)

    assert (proj / "megamaid").is_dir()
    assert {p.name for p in (proj / "megamaid").glob("*.py")}


def test_retention_prune_survives_a_stray_file_in_the_backups_directory(tmp_path):
    """Same unfiltered listing, in back_up()'s prune: a stray file landing in
    the `[:-RETAIN]` slice raises NotADirectoryError out of shutil.rmtree."""
    proj = _project(tmp_path)
    (proj / BACKUP_DIR).mkdir()
    (proj / BACKUP_DIR / "000-earliest-note.txt").write_text("sorts first\n")

    for i in range(RETAIN + 2):
        back_up(proj, "0.9.1", f"2026080{i}-000000")

    assert len(backups(proj)) == RETAIN


def test_retention_prune_never_deletes_a_users_stray_entry(tmp_path):
    """`.megamaid-backups/` is the user's directory. Pruning is scoped to the
    backups this tool made; anything else a user parked there is not ours to
    delete."""
    proj = _project(tmp_path)
    (proj / BACKUP_DIR).mkdir()
    keepsake = proj / BACKUP_DIR / "000-earliest-note.txt"
    keepsake.write_text("sorts first, must survive\n")

    for i in range(RETAIN + 2):
        back_up(proj, "0.9.1", f"2026080{i}-000000")

    assert keepsake.is_file(), "pruning deleted a file the user put there"


# --- rollback() atomicity: never leave the project with no runtime ---------


def test_rollback_leaves_the_runtime_untouched_when_the_restore_fails(tmp_path):
    """rmtree-then-copytree means any failure between the two leaves the
    project with no runtime at all. Staging the replacement first and swapping
    it in means a failed restore is a no-op, not a deletion."""
    proj = _project(tmp_path)
    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}

    def _raise(*args, **kwargs):
        raise OSError(28, "No space left on device")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shutil, "copytree", _raise)
        with pytest.raises(OSError):
            rollback(proj)

    after = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    assert after == before, "a failed rollback destroyed the runtime it could not replace"


def test_a_failed_rollback_leaves_no_scratch_directories_behind(tmp_path):
    """Staging happens beside megamaid/, in the project. A failure must not
    leave the user staring at a half-restored directory they have to identify
    and clean up themselves."""
    proj = _project(tmp_path)
    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    expected = {p.name for p in proj.iterdir()}

    def _raise(*args, **kwargs):
        raise OSError(28, "No space left on device")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shutil, "copytree", _raise)
        with pytest.raises(OSError):
            rollback(proj)

    assert {p.name for p in proj.iterdir()} == expected


def test_rollback_does_not_restore_pycache_into_the_runtime(tmp_path):
    """back_up() excludes __pycache__; restoring must not smuggle one back in
    from a backup written before that exclusion existed."""
    proj = _project(tmp_path)
    dest = back_up(proj, "0.9.1", "20260802-000000")
    (dest / "__pycache__").mkdir()
    (dest / "__pycache__" / "stale.pyc").write_bytes(b"\x00")

    rollback(proj)

    assert not (proj / "megamaid" / "__pycache__").exists()


# --- .megamaid-version: the stamp must not outlive the runtime it names ----


def test_rollback_reverts_the_version_stamp(tmp_path):
    """apply_plan writes .megamaid-version. A rollback that restores the old
    runtime but leaves the new stamp makes the project claim a version it is
    not running — silently, with nothing to detect it by."""
    proj = _project(tmp_path)
    (proj / VERSION_STAMP).write_text("0.8.4\n")

    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    assert (proj / VERSION_STAMP).read_text().strip() == "0.9.1"

    rollback(proj)

    assert (proj / VERSION_STAMP).read_text().strip() == "0.8.4", (
        "the stamp still claims the version the rolled-back runtime is not running"
    )


def test_rollback_removes_the_stamp_when_the_project_never_had_one(tmp_path):
    """A project scaffolded before the stamping scheme has no .megamaid-version
    (SKILL.md step 3 says so). Rolling back to that state must leave it with
    none, not with the stamp upgrade invented."""
    proj = _project(tmp_path)
    assert not (proj / VERSION_STAMP).exists()

    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")
    assert (proj / VERSION_STAMP).exists()

    rollback(proj)

    assert not (proj / VERSION_STAMP).exists(), (
        "rollback invented a version stamp the project never had"
    )


def test_the_saved_stamp_is_not_restored_into_the_runtime_directory(tmp_path):
    """The pre-upgrade stamp rides along inside the backup directory. It
    belongs at the project root on restore, never inside megamaid/."""
    proj = _project(tmp_path)
    (proj / VERSION_STAMP).write_text("0.8.4\n")
    plan = plan_project(proj, RUNTIME, load_manifest())
    apply_plan(plan, RUNTIME, "0.9.1", "20260802-000000")

    rollback(proj)

    assert not (proj / "megamaid" / VERSION_STAMP).exists()


# --- back_up() names what latest_backup() can find -------------------------


def test_back_up_refuses_a_timestamp_it_could_never_find_again(tmp_path):
    """The two halves of the naming contract must not drift apart: a backup
    written under a name latest_backup() filters out is a backup that exists on
    disk and can never be restored. Fail at creation instead."""
    proj = _project(tmp_path)
    with pytest.raises(ValueError, match="YYYYMMDD-HHMMSS"):
        back_up(proj, "0.9.1", "not-a-timestamp")


def test_apply_plan_reports_a_bad_timestamp_as_a_backup_phase_failure(tmp_path):
    """...and it must arrive as BackupFailed, so the CLI tells the user
    nothing in megamaid/ was touched — which is true, since the rejection
    happens before any copy."""
    proj = _project(tmp_path)
    before = {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")}
    plan = plan_project(proj, RUNTIME, load_manifest())

    with pytest.raises(BackupFailed):
        apply_plan(plan, RUNTIME, "0.9.1", "not-a-timestamp")

    assert {p.name: p.read_bytes() for p in (proj / "megamaid").glob("*.py")} == before
