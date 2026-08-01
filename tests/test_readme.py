"""The repo-root user docs stay in sync with the code that shipped.

Three kinds of rot are guarded here, all of which reached the branch:

1. **Paths.** The `skills/megamaid/` restructure moved `patterns/` and
   `references/` (and `SKILL.md` itself) out of the repo root. SKILL.md's own
   Directory Reference tree caught one round of it (see test_skill.py); this
   file guards README.md, which sits at the repo root and therefore needs
   *different* relative paths than files that moved under skills/megamaid/.

2. **Retired transport.** The bearer-token HTTP lane on port 8305 was deleted
   from the code on this branch, but EXAMPLES.md still told readers to POST to
   it — the branch's founding defect (documented MCP usage that cannot work)
   reproduced one file over, because the sweep that cleaned README.md was a
   one-off grep and not a test.

3. **Bare invocations.** `claude plugin install` puts nothing on PATH, so a
   bare `megamaid <sub>` line in the docs is an instruction that cannot run.
   The detector for this shipped with the branch; README.md simply was not in
   the scanned set.
"""

import re

from conftest import find_bare_megamaid_invocations

REPO_FILE_EXTENSIONS = (".md", ".py", ".toml", ".json", ".yml", ".yaml")

# Strings belonging to the deleted HTTP transport. Any reappearance in a
# user-facing doc is a documented-but-impossible instruction.
RETIRED_TRANSPORT_STRINGS = ("8305", "localhost:8305", "MCP_BEARER_TOKEN", "Bearer ")

# Root docs whose command lines must be runnable as written: either through
# the launcher, or through a scraped project's own .venv/bin/megamaid.
GUARDED_DOCS = ("README.md", "EXAMPLES.md")

# Documented exclusion, in the same spirit as the leading-slash rule below.
#
# templates/README.md is not a doc *about* the plugin — it is copied verbatim
# into every scraped project and read from inside it, where its own Setup
# section (`python -m venv .venv && source .venv/bin/activate && pip install
# -e .`) has genuinely put `megamaid` on PATH a few lines above the usage
# block. A bare `megamaid suck` is correct there and the launcher form would
# be actively wrong: the launcher's state venv has no `targets/` package and
# cannot run the project-scoped commands at all. The exclusion is asserted to
# still name a real file (test_the_documented_exclusion_is_not_stale) so it
# cannot quietly outlive the file it excuses.
EXCLUDED_FROM_INVOCATION_GUARD = ("templates/README.md",)

# Markdown link targets: `](path)`.
_LINK_TARGET = re.compile(r"\]\(([^)]+)\)")

# Inline code spans that look like a repo-relative file reference: a
# contiguous non-whitespace, non-backtick run ending in one of the
# extensions above. This intentionally also matches spans inside link text
# or elsewhere in prose, e.g. `references/troubleshooting.md`.
_CODE_SPAN_PATH = re.compile(
    r"`([^`\s]+(?:" + "|".join(re.escape(ext) for ext in REPO_FILE_EXTENSIONS) + r"))`"
)


def _extract_candidate_paths(text: str) -> list[str]:
    """Pull repo-relative path candidates out of README prose.

    Skips:

    - link targets starting with ``http``, ``mailto:``, or ``#`` — external
      URLs and same-page anchors, never repo paths.
    - anything starting with ``/`` — treated as an absolute-path web
      endpoint or URL fragment (e.g. the Shopify `/products.json` pattern
      table entry), not a repo reference. No repo-relative reference in this
      README needs a leading slash, so this exclusion is deliberate: a
      future root-absolute repo path would need a different rule, not a
      silent pass here.

    Args:
        text: the full contents of README.md.

    Returns:
        Deduplicated candidate path strings, in first-seen order.
    """
    found: list[str] = []
    seen: set[str] = set()

    for pattern in (_LINK_TARGET, _CODE_SPAN_PATH):
        for match in pattern.findall(text):
            candidate = match.strip()
            if not candidate:
                continue
            if candidate.startswith(("http", "mailto:", "#", "/")):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            found.append(candidate)

    return found


def test_referenced_paths_exist(repo_root):
    """Every repo-relative path README.md names must exist on disk.

    Vacuity guard included: if the extraction regexes silently stopped
    matching anything (e.g. after a README rewrite changes the markup
    style), this test must not pass by finding nothing to check.
    """
    readme_path = repo_root / "README.md"
    text = readme_path.read_text()

    candidates = _extract_candidate_paths(text)

    assert len(candidates) >= 3, (
        f"only found {len(candidates)} candidate path(s) in README.md; "
        "the extraction pattern may have stopped matching — this guard "
        "exists so a broken regex can't silently check nothing"
    )

    missing = [path for path in candidates if not (repo_root / path).exists()]

    assert not missing, (
        f"README.md references {len(missing)} path(s) that don't exist "
        "relative to the repo root (a common cause: a file moved under "
        "skills/megamaid/ during the plugin restructure but the README "
        "link was not updated):\n" + "\n".join(f"  - {p}" for p in missing)
    )


def test_root_docs_do_not_document_the_retired_http_transport(repo_root):
    """The port, the token and the endpoint were all deleted from the code.

    EXAMPLES.md kept POSTing to `http://localhost:8305/mcp` with an
    `Authorization: Bearer` header long after the branch removed the listener,
    the auth verifier and the container that published the port — and
    README.md linked readers straight to it. Both files are in scope now, so
    the next transport change cannot leave one of them behind.
    """
    offenders = []
    for name in GUARDED_DOCS:
        path = repo_root / name
        assert path.exists(), f"guarded doc {name} is missing"
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for needle in RETIRED_TRANSPORT_STRINGS:
                if needle in line:
                    offenders.append(f"{name}:{lineno}: {needle!r} in {line.strip()!r}")

    assert not offenders, (
        "retired HTTP-transport references in user-facing docs (the server is "
        "stdio-only; there is no port and no token):\n" + "\n".join(f"  - {o}" for o in offenders)
    )


def test_root_docs_never_instruct_a_bare_megamaid_invocation(repo_root):
    """A plugin install puts nothing on PATH.

    Uses the same shared detector as the skill and slash-command guards (see
    tests/conftest.py) so there is exactly one notion of "bare invocation"
    across the suite.
    """
    for name in GUARDED_DOCS:
        offenders = find_bare_megamaid_invocations((repo_root / name).read_text())
        assert not offenders, (
            f"{name}: bare megamaid invocation(s) — route URL-scoped commands "
            f"through the launcher and project-scoped ones through "
            f".venv/bin/megamaid: {offenders}"
        )


def test_the_documented_exclusion_is_not_stale(repo_root):
    """EXCLUDED_FROM_INVOCATION_GUARD must keep naming files that exist.

    An exclusion outliving its file is how a guard silently narrows.
    """
    for name in EXCLUDED_FROM_INVOCATION_GUARD:
        assert (repo_root / name).exists(), (
            f"{name} is excluded from the invocation guard but no longer exists; drop the exclusion"
        )
        assert name not in GUARDED_DOCS, f"{name} cannot be both guarded and excluded"
