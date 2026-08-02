"""Applying: back up first, never touch what we did not promise to touch."""

import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid_setup.manifest import load_manifest  # noqa: E402
from megamaid_setup.upgrade import (  # noqa: E402
    BACKUP_DIR,
    RETAIN,
    apply_plan,
    back_up,
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
