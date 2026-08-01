"""Pin find_bare_megamaid_invocations directly.

Two task briefs (this one's commands/*.md guard, and the next one's SKILL.md
guard) both depend on this helper to actually catch a bare `megamaid <sub>`
instruction rather than rubber-stamp it. It is tested on its own, not just
through the thin wrapper in test_plugin_wiring.py, so a change that weakens
detection fails here first.
"""

from conftest import find_bare_megamaid_invocations

MUST_BE_FLAGGED = [
    "megamaid suck --max 5",
    "$ megamaid recon https://example.com",
    "    megamaid export --format csv",
    "Run `megamaid suck` to start.",
    ">>> megamaid status",
]

MUST_NOT_BE_FLAGGED = [
    'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/launch.py" --cli recon https://example.com',
    "megamaid-mcp",
    "the megamaid skill scaffolds scrapers",
    "See the `megamaid recon` feature for details.",
    ".megamaid-version",
    # Additional prose-mention cases from the general requirement, not just
    # the pinned list: a subcommand whitelist (not "megamaid + any word") is
    # what keeps these unflagged even though they share megamaid's shape.
    "megamaid is a scraper",
    "megamaid_mcp",
    "megamaid.cli",
    "megamaid/scaffold.py imports the shared templates",
]


def test_flags_bare_invocations():
    for line in MUST_BE_FLAGGED:
        assert find_bare_megamaid_invocations(line), f"should have flagged: {line!r}"


def test_does_not_flag_mentions_and_launcher_invocations():
    for line in MUST_NOT_BE_FLAGGED:
        assert find_bare_megamaid_invocations(line) == [], f"should not have flagged: {line!r}"


def test_returns_the_offending_lines_for_reporting():
    text = "intro line\nmegamaid suck --max 5\nanother line\n$ megamaid recon https://x\n"
    assert find_bare_megamaid_invocations(text) == [
        "megamaid suck --max 5",
        "$ megamaid recon https://x",
    ]


def test_the_adversarial_backtick_mention_is_a_documented_limitation():
    """See find_bare_megamaid_invocations' docstring for the trade-off.

    An imperative verb immediately before the command is the only signal
    used to tell an instruction from a reference. "Run `megamaid suck`" and
    "See the `megamaid recon` feature" have the same backticked-command
    shape; only the preceding verb distinguishes them.
    """
    instruction = "Run `megamaid suck` to start."
    reference = "See the `megamaid recon` feature for details."
    assert find_bare_megamaid_invocations(instruction) == [instruction]
    assert find_bare_megamaid_invocations(reference) == []
