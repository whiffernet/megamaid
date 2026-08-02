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


# --- what the report says it replaced --------------------------------------
#
# The report itemized refusals, adds, unreachable adds and shared variants, and
# never once mentioned an overwrite. Applied to a copy of megamaid-acehardware
# it said "4 files added" while seven files were replaced on disk, cli.py among
# them, 12.5 KB larger than the one it displaced. Because adds *are* named, a
# reader reasonably concluded four new files was the whole story.


def test_report_says_how_many_files_it_replaced():
    """The acehardware case: four adds must not be the only thing reported
    when seven files were overwritten."""
    plan = _plan(
        "acehardware",
        [
            FileAction("netguard.py", Tier.ABSENT, "add"),
            FileAction("cli.py", Tier.EXACT, "overwrite"),
            FileAction("base.py", Tier.EXACT, "overwrite"),
        ],
    )
    out = render([plan])
    assert "2 file(s) replaced" in out, f"overwrites are still invisible:\n{out}"


def test_report_is_silent_about_replacements_when_there_are_none():
    plan = _plan("g", [FileAction("netguard.py", Tier.ABSENT, "add")])
    assert "replaced" not in render([plan])


def test_report_names_every_cosmetic_overwrite_with_its_project():
    """EXACT overwrites lose nothing a human wrote and can stay a count.
    COSMETIC is the case where a person's own text does not survive: a
    comment-only edit — `# DO NOT RE-ENABLE screenshots, allrecipes
    rate-limits us to death` prepended to base.py — classifies COSMETIC and is
    overwritten. Those must be named, with their file and their project."""
    a = _plan(
        "allrecipes",
        [
            FileAction("base.py", Tier.COSMETIC, "overwrite"),
            FileAction("cli.py", Tier.EXACT, "overwrite"),
        ],
    )
    b = _plan("amazon", [FileAction("discovery.py", Tier.COSMETIC, "overwrite")])
    out = render([a, b])

    assert "allrecipes: base.py" in out
    assert "amazon: discovery.py" in out
    assert "allrecipes: cli.py" not in out, "EXACT overwrites lose nothing and stay summarized"


def test_report_says_where_the_replaced_comments_went():
    """Naming the file is only half of it — the reader needs to know the text
    is recoverable and where from."""
    plan = _plan("h", [FileAction("base.py", Tier.COSMETIC, "overwrite")])
    out = render([plan])
    assert ".megamaid-backups" in out, f"no route back to the replaced text:\n{out}"


def test_cosmetic_overwrites_are_reported_even_when_nothing_was_refused():
    """A project that converges cleanly still loses comments. The clean case is
    exactly where a reader is least likely to go looking."""
    plan = _plan("i", [FileAction("base.py", Tier.COSMETIC, "overwrite")])
    out = render([plan])
    assert "MM-32" not in out
    assert "i: base.py" in out


def test_replacements_are_reported_before_files_added():
    """Same ordering principle as refusals: what cost the user something comes
    before what happened automatically."""
    plan = _plan(
        "j",
        [
            FileAction("base.py", Tier.COSMETIC, "overwrite"),
            FileAction("netguard.py", Tier.ABSENT, "add"),
        ],
    )
    out = render([plan])
    assert out.index("replaced") < out.index("Files added")


# --- pluralization: the single-project case is the common one --------------


def test_a_single_project_is_not_reported_as_1_projects():
    out = render([_plan("k", [FileAction("base.py", Tier.EXACT, "overwrite")])])
    assert "1 projects" not in out
    assert "1 project" in out


def test_a_single_converging_project_reads_as_a_sentence():
    out = render([_plan("l", [FileAction("base.py", Tier.EXACT, "overwrite")])])
    assert "1 converges cleanly" in out, f"expected subject-verb agreement:\n{out}"


def test_several_projects_still_pluralize():
    plans = [_plan(n, [FileAction("base.py", Tier.EXACT, "overwrite")]) for n in ("m", "n")]
    out = render(plans)
    assert "2 projects" in out
    assert "2 converge cleanly" in out
