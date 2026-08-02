"""Dry-run the classifier over the real scaffolded projects.

Read-only by construction: plan_project performs no writes (asserted in
tests/test_upgrade_plan.py). Skipped when the fleet is absent, so the suite
still passes on any other machine.
"""

import os
import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid_setup.manifest import load_manifest  # noqa: E402
from megamaid_setup.upgrade import plan_project  # noqa: E402

RUNTIME = SRC / "megamaid"
FLEET = sorted(pathlib.Path(os.environ.get("MEGAMAID_FLEET", "/home/e")).glob("megamaid-*"))

pytestmark = pytest.mark.skipif(
    len(FLEET) < 10, reason="no scaffolded-project fleet on this machine"
)


@pytest.fixture(scope="module")
def plans():
    manifest = load_manifest()
    return [plan_project(p, RUNTIME, manifest) for p in FLEET if (p / "megamaid").is_dir()]


def test_most_of_the_fleet_converges(plans):
    """Thresholds, not exact counts — the fleet grows as more sites are scraped."""
    converging = [p for p in plans if p.converges]
    assert len(converging) >= 45, (
        f"only {len(converging)}/{len(plans)} converge; "
        "if the manifest is stale everything looks divergent"
    )


def test_every_divergent_project_names_its_own_files(plans):
    for plan in plans:
        if plan.refused:
            offenders = [a.name for a in plan.refused if not a.name.endswith(".py")]
            assert not offenders, (
                f"{plan.project.name}: refused action(s) named {offenders!r} do not end in "
                "'.py' — a refusal should always name one of the runtime's own files, not "
                "something else entirely"
            )


def test_the_fleet_gains_the_security_guards(plans):
    """netguard.py is in none of them today; every project should gain it."""
    gaining = [
        p for p in plans if any(a.name == "netguard.py" and a.action == "add" for a in p.actions)
    ]
    assert len(gaining) >= 45, (
        f"only {len(gaining)}/{len(plans)} projects would gain netguard.py, expected almost "
        "all of them since no project has it yet; if this drops, either the manifest is stale "
        "(everything refuses instead of adding) or a scaffold started shipping netguard.py "
        "already"
    )


def test_every_planned_name_is_a_real_runtime_file(plans):
    """Every name a plan emits is one of the runtime's own files — never something a project's
    own targets/, staging/, or anything else could have supplied.

    This replaces an earlier version of this test that asserted a planned name had no '/' and
    ended in '.py'. That was structurally guaranteed to pass no matter what: plan_project only
    ever builds a FileAction from `FileAction(name=source.name, ...)` for
    `source in runtime.glob("*.py")`, so `.name` is always a bare, slash-free ".py" basename by
    construction — the assertion could never fail, which made it decorative rather than a real
    check.

    Comparing against an independently recomputed set of the runtime's actual basenames is a
    check a planner regression could actually trip — e.g. one that started walking a project's
    own tree (`vendored.rglob(...)` instead of `runtime.glob(...)`) would, for any file whose
    name doesn't happen to coincide with a real runtime file, emit a name outside this set.
    Checked per-project (not just as one fleet-wide set) so a failure names the offending
    project directly, rather than only the fleet-wide set difference.

    The proof that plan_project never *writes* outside megamaid/ — what this test's old name
    promised — lives in
    tests/test_upgrade_plan.py::test_plan_project_writes_nothing_anywhere_in_the_tree, which
    fingerprints (size, mtime) a whole synthetic project tree, including a real targets/
    directory and scraped output, before and after planning, and asserts byte-for-byte
    identity. This test is a narrower, fleet-data companion to that one, not a substitute.
    """
    runtime_names = {p.name for p in RUNTIME.glob("*.py")}
    for plan in plans:
        planned_names = {act.name for act in plan.actions}
        stray = planned_names - runtime_names
        assert not stray, (
            f"{plan.project.name}: plan names file(s) {sorted(stray)!r} that are not among "
            f"the runtime's own files ({sorted(runtime_names)!r}) — the planner may have "
            "started reading outside src/megamaid/, or the runtime itself changed without "
            "this test's independent glob picking it up"
        )
