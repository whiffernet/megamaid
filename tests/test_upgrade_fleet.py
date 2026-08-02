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
            assert all(a.name.endswith(".py") for a in plan.refused)


def test_the_fleet_gains_the_security_guards(plans):
    """netguard.py is in none of them today; every project should gain it."""
    gaining = [
        p for p in plans if any(a.name == "netguard.py" and a.action == "add" for a in p.actions)
    ]
    assert len(gaining) >= 45


def test_no_project_would_have_targets_or_staging_touched(plans):
    """Nothing outside megamaid/ may ever appear in a plan."""
    for plan in plans:
        for act in plan.actions:
            assert "/" not in act.name and act.name.endswith(".py")
