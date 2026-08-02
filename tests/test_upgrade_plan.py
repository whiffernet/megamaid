"""Planning is pure, per-file, and honest about reachability."""

import pathlib
import shutil
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid_setup.manifest import load_manifest  # noqa: E402
from megamaid_setup.upgrade import Tier, plan_project  # noqa: E402

RUNTIME = SRC / "megamaid"


def _project(tmp_path, files: dict[str, str]) -> pathlib.Path:
    """A fake scaffolded project whose megamaid/ holds exactly `files`."""
    proj = tmp_path / "megamaid-fake"
    (proj / "megamaid").mkdir(parents=True)
    for name, body in files.items():
        (proj / "megamaid" / name).write_text(body)
    return proj


def test_missing_directory_is_an_error(tmp_path):
    plan = plan_project(tmp_path / "nothing", RUNTIME, load_manifest())
    assert plan.error is not None
    assert "MM-31" in plan.error
    assert plan.actions == []


def test_absent_files_are_added(tmp_path):
    proj = _project(tmp_path, {})
    plan = plan_project(proj, RUNTIME, load_manifest())
    assert plan.actions, "expected an action per runtime file"
    assert all(a.action == "add" for a in plan.actions)


def test_current_runtime_copy_is_all_exact(tmp_path):
    """A project holding today's runtime verbatim must classify entirely EXACT."""
    proj = tmp_path / "megamaid-current"
    shutil.copytree(RUNTIME, proj / "megamaid")
    plan = plan_project(proj, RUNTIME, load_manifest())
    assert all(a.tier is Tier.EXACT for a in plan.actions), [
        (a.name, a.tier) for a in plan.actions if a.tier is not Tier.EXACT
    ]
    assert plan.converges


def test_one_divergent_file_does_not_block_its_siblings(tmp_path):
    proj = tmp_path / "megamaid-mixed"
    shutil.copytree(RUNTIME, proj / "megamaid")
    (proj / "megamaid" / "images.py").write_text("# locally rewritten\nx = 1\n")
    plan = plan_project(proj, RUNTIME, load_manifest())
    refused = {a.name for a in plan.refused}
    assert refused == {"images.py"}
    assert any(a.name == "base.py" and a.action == "overwrite" for a in plan.actions)


def test_added_file_is_unreachable_when_its_entry_point_is_refused(tmp_path):
    """recon.py is dispatched from cli.py. A divergent cli.py means recon lands
    on disk but cannot be invoked — the plan must say so."""
    proj = tmp_path / "megamaid-blocked"
    shutil.copytree(RUNTIME, proj / "megamaid")
    (proj / "megamaid" / "cli.py").write_text("# hand-written CLI\nx = 1\n")
    (proj / "megamaid" / "recon.py").unlink()
    plan = plan_project(proj, RUNTIME, load_manifest())
    recon = next(a for a in plan.actions if a.name == "recon.py")
    assert recon.action == "add"
    assert recon.reachable is False
    assert recon in plan.unreachable


def test_cosmetic_drift_still_overwrites(tmp_path):
    """A trailing-newline-only edit (COSMETIC) must map to the same "overwrite"
    action as an EXACT match. Misclassifying a real user edit as COSMETIC would
    make Task 3 overwrite it — this pins the tier-to-action mapping for the
    tier that sits right next to that failure mode."""
    proj = tmp_path / "megamaid-cosmetic"
    shutil.copytree(RUNTIME, proj / "megamaid")
    constants = proj / "megamaid" / "constants.py"
    constants.write_text(constants.read_text() + "\n")
    plan = plan_project(proj, RUNTIME, load_manifest())
    action = next(a for a in plan.actions if a.name == "constants.py")
    assert action.tier is Tier.COSMETIC
    assert action.action == "overwrite"


def test_added_file_stays_reachable_when_only_one_of_two_entry_points_is_refused(
    tmp_path,
):
    """image_index.py has two dispatchers (cli.py, images.py). Reachability
    requires ALL of a file's entry points to be refused before it is reported
    unreachable — one working dispatcher is enough to invoke it."""
    proj = tmp_path / "megamaid-partially-blocked"
    shutil.copytree(RUNTIME, proj / "megamaid")
    (proj / "megamaid" / "images.py").write_text("# hand-written images\nx = 1\n")
    (proj / "megamaid" / "image_index.py").unlink()
    plan = plan_project(proj, RUNTIME, load_manifest())
    image_index = next(a for a in plan.actions if a.name == "image_index.py")
    assert image_index.action == "add"
    assert image_index.reachable is True
    assert image_index not in plan.unreachable


def test_added_file_is_unreachable_when_both_of_two_entry_points_are_refused(
    tmp_path,
):
    """Same file, but this time both of its dispatchers are divergent — now it
    is genuinely unreachable."""
    proj = tmp_path / "megamaid-fully-blocked"
    shutil.copytree(RUNTIME, proj / "megamaid")
    (proj / "megamaid" / "cli.py").write_text("# hand-written CLI\nx = 1\n")
    (proj / "megamaid" / "images.py").write_text("# hand-written images\nx = 1\n")
    (proj / "megamaid" / "image_index.py").unlink()
    plan = plan_project(proj, RUNTIME, load_manifest())
    image_index = next(a for a in plan.actions if a.name == "image_index.py")
    assert image_index.action == "add"
    assert image_index.reachable is False
    assert image_index in plan.unreachable


def test_planning_writes_nothing(tmp_path):
    """The property --dry-run depends on."""
    proj = tmp_path / "megamaid-untouched"
    shutil.copytree(RUNTIME, proj / "megamaid")
    before = {p: p.stat().st_mtime_ns for p in sorted((proj / "megamaid").rglob("*"))}
    plan_project(proj, RUNTIME, load_manifest())
    after = {p: p.stat().st_mtime_ns for p in sorted((proj / "megamaid").rglob("*"))}
    assert before == after
    assert not (proj / ".megamaid-backups").exists()


def _snapshot(root: pathlib.Path) -> dict[pathlib.Path, tuple[int, int]]:
    """Every path under `root` mapped to (size, mtime_ns). A changed set of keys
    catches a stray mkdir/create; changed values catch a stray rewrite."""
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def test_plan_project_writes_nothing_anywhere_in_the_tree(tmp_path):
    """Purity is the property upgrade --dry-run depends on across 63 real
    projects holding 13.7 GB of irreplaceable scraped output — "the dry run
    wrote something" is the one bug that cannot be walked back.

    Unlike test_planning_writes_nothing (mtimes only, scoped to megamaid/),
    this snapshots paths + mtimes + sizes for the *whole* project tree —
    including non-runtime files a scraper would hold, like scraped output and
    a targets/ package — and exercises both the mixed happy path (one
    divergent file, one missing/add file) and the not-a-project error path,
    since a write on an error branch would be just as unrecoverable.
    """
    manifest = load_manifest()

    # A realistic mixed project: current runtime, one locally-edited file, one
    # runtime file the user deleted, plus scraper output and bespoke code that
    # plan_project must never so much as touch.
    proj = tmp_path / "megamaid-purity"
    shutil.copytree(RUNTIME, proj / "megamaid")
    (proj / "megamaid" / "images.py").write_text("# edited locally\nx = 1\n")
    (proj / "megamaid" / "recon.py").unlink()
    (proj / "output.jsonl").write_text('{"scraped": true}\n')
    (proj / "targets").mkdir()
    (proj / "targets" / "example.py").write_text("TARGET_URL = 'https://example.com'\n")

    before = _snapshot(tmp_path)
    plan = plan_project(proj, RUNTIME, manifest)
    after = _snapshot(tmp_path)
    assert before == after
    assert plan.actions, "sanity: the plan should have found something to report"

    # The error path (no megamaid/ directory) must be equally inert — a plan
    # that bails out with MM-31 is not license to touch the filesystem first.
    not_a_project = tmp_path / "not-a-project"
    not_a_project.mkdir()
    (not_a_project / "unrelated.txt").write_text("hello\n")

    before_err = _snapshot(tmp_path)
    err_plan = plan_project(not_a_project, RUNTIME, manifest)
    after_err = _snapshot(tmp_path)
    assert before_err == after_err
    assert err_plan.error is not None

    assert not (proj / ".megamaid-backups").exists()
