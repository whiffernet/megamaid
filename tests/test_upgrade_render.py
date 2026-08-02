"""The report has to answer: what landed, what did not, what needs a human."""

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid_setup.upgrade import (  # noqa: E402
    FileAction,
    ProjectPlan,
    Tier,
    render,
)


def _plan(name, actions, error=None):
    return ProjectPlan(project=pathlib.Path(f"/tmp/{name}"), actions=actions, error=error)


def test_report_counts_converging_and_divergent_projects():
    clean = _plan("a", [FileAction("base.py", Tier.EXACT, "overwrite")])
    dirty = _plan("b", [FileAction("cli.py", Tier.DIVERGENT, "refuse")])
    out = render([clean, dirty])
    assert "1 converge" in out
    assert "1 " in out and "divergent" in out


def test_report_names_unreachable_files_with_the_code():
    blocked = _plan(
        "c",
        [
            FileAction("cli.py", Tier.DIVERGENT, "refuse"),
            FileAction("recon.py", Tier.ABSENT, "add", reachable=False),
        ],
    )
    out = render([blocked])
    assert "MM-35" in out
    assert "recon.py" in out
    assert "c" in out


def test_report_is_silent_about_unreachability_when_there_is_none():
    fine = _plan("d", [FileAction("recon.py", Tier.ABSENT, "add")])
    assert "MM-35" not in render([fine])


def test_report_names_every_refused_file_with_the_code_not_just_a_count():
    """Refusals are the headline: the report must name each file and the
    project it belongs to, tagged MM-32, not bury a count in a summary line."""
    dirty = _plan(
        "walmart",
        [
            FileAction("cli.py", Tier.DIVERGENT, "refuse"),
            FileAction("base.py", Tier.DIVERGENT, "refuse"),
        ],
    )
    out = render([dirty])
    assert "MM-32" in out
    assert "walmart: cli.py" in out
    assert "walmart: base.py" in out
    assert "2 file(s) refused" in out


def test_report_is_silent_about_mm32_when_nothing_was_refused():
    clean = _plan("e", [FileAction("base.py", Tier.EXACT, "overwrite")])
    assert "MM-32" not in render([clean])


def test_refusals_are_reported_before_files_added_not_buried_after():
    """The whole point of leading with refusals: a reader scanning top-down
    hits "what needs me" before "what happened automatically"."""
    dirty = _plan(
        "f",
        [
            FileAction("cli.py", Tier.DIVERGENT, "refuse"),
            FileAction("recon.py", Tier.ABSENT, "add"),
        ],
    )
    out = render([dirty])
    assert out.index("MM-32") < out.index("Files added")


def test_report_names_the_errored_project_and_its_mm31_code():
    """A project that could not even be read must not be reduced to a bare
    count either — the code and the reason belong in the visible report."""
    broken = ProjectPlan(
        project=pathlib.Path("/tmp/not-a-project"),
        actions=[],
        error="MM-31 not a megamaid project",
    )
    out = render([broken])
    assert "MM-31" in out
    assert "not-a-project" in out


def test_report_surfaces_shared_variants_across_projects(tmp_path):
    """A refused file appearing identically in 2+ projects is template work
    that never flowed upstream — the report groups it into one backport
    review instead of leaving it as N separate per-project refusals."""
    for name in ("proj-a", "proj-b"):
        megamaid_dir = tmp_path / name / "megamaid"
        megamaid_dir.mkdir(parents=True)
        (megamaid_dir / "cli.py").write_text("# identical hand edit\n")

    a = ProjectPlan(
        project=tmp_path / "proj-a",
        actions=[FileAction("cli.py", Tier.DIVERGENT, "refuse")],
    )
    b = ProjectPlan(
        project=tmp_path / "proj-b",
        actions=[FileAction("cli.py", Tier.DIVERGENT, "refuse")],
    )
    out = render([a, b])
    assert "Shared variants" in out
    assert "cli.py" in out
    assert "proj-a" in out and "proj-b" in out
