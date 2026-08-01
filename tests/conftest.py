"""Shared pytest fixtures and helpers for the megamaid test suite."""

import pathlib
import re

import pytest


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    """Absolute path to the repository root.

    Returns:
        Path to the directory containing pyproject.toml and .claude-plugin/.
    """
    return pathlib.Path(__file__).resolve().parent.parent


# megamaid's own console-script subcommands (src/megamaid/cli.py). Requiring
# one of these — rather than "megamaid" followed by any word — is what keeps
# prose mentions unflagged: "the megamaid skill" and "megamaid is a scraper"
# both have the same shape as a command line ("megamaid" + whitespace + word),
# but "skill" and "is" are not subcommands, so neither ever matches.
_MEGAMAID_SUBCOMMANDS = ("recon", "suck", "status", "diff", "export", "map", "init")
_COMMAND = r"\bmegamaid\b\s+(?:" + "|".join(_MEGAMAID_SUBCOMMANDS) + r")\b"

# Command position: start of line, optionally after a shell prompt marker,
# optionally backticked. E.g. `megamaid suck`, `$ megamaid recon …`, indented
# `    megamaid export …`, `>>> megamaid status`. ">>>" must precede ">" in
# the alternation, or ">" alone would consume it and leave "megamaid status"
# unmatched behind a stray ">>".
_PROMPT_INVOCATION = re.compile(rf"^\s*(?:\$|>>>|>|%|#)?\s*`?(?:{_COMMAND})")

# Mid-sentence, introduced by an imperative verb, with or without backticks:
# "Run `megamaid suck` to start.", "Then run megamaid suck now." The verb
# must sit immediately (modulo whitespace/a backtick) before the command —
# see find_bare_megamaid_invocations' docstring for the trade-off this makes.
_IMPERATIVE_INVOCATION = re.compile(
    rf"\b(?:run|execute|invoke|call)\b\s*`?(?:{_COMMAND})", re.IGNORECASE
)


def find_bare_megamaid_invocations(text: str) -> list[str]:
    """Find lines that instruct a reader to run a bare `megamaid` command.

    `claude plugin install` puts nothing on PATH, so a bare `megamaid <sub>`
    invocation in user-facing docs (slash commands, skills) is an instruction
    that cannot work — the reader has to go through the launcher instead:
    `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/launch.py" --cli <sub>`.

    A line is flagged when "megamaid" is followed by whitespace and one of
    megamaid's real subcommands, in command position: at the start of the
    line, after a shell prompt marker (`$`, `>`, `>>>`, `%`, `#`), optionally
    backticked; or anywhere on the line immediately after an imperative verb
    (run/execute/invoke/call). A trailing hyphen, underscore, dot, or slash
    right after "megamaid" (as in `megamaid-mcp`, `.megamaid-version`) never
    matches, because the pattern requires whitespace there.

    Known, deliberate trade-off: a bare mention *inside backticks*, in prose,
    with no preceding imperative verb — "See the `megamaid recon` feature
    for details." — is treated as a reference and left unflagged, while "Run
    `megamaid suck` to start." is flagged. The presence of an imperative verb
    immediately before the command is the only signal distinguishing an
    instruction from a reference; phrasing that separates the verb from the
    command ("Please run this via `megamaid suck`") or uses a verb outside
    the small whitelist ("you'll want megamaid suck next") will not be
    caught. This is a guard against the common phrasings, not a proof.

    Args:
        text: file contents to scan, checked one line at a time.

    Returns:
        The offending lines, stripped of surrounding whitespace, in the
        order they appear in `text`. Empty when nothing is flagged.
    """
    offenders = []
    for line in text.splitlines():
        if _PROMPT_INVOCATION.match(line) or _IMPERATIVE_INVOCATION.search(line):
            offenders.append(line.strip())
    return offenders
