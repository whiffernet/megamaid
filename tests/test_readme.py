"""README.md's repo-relative references stay in sync with the actual layout.

The `skills/megamaid/` restructure moved `patterns/` and `references/` (and
`SKILL.md` itself) out of the repo root. SKILL.md's own Directory Reference
tree caught one round of this rot (see test_skill.py); this file guards the
other place a moved path can go stale silently: README.md, which sits at the
repo root and therefore needs *different* relative paths than files that
moved under skills/megamaid/ do.
"""

import re

REPO_FILE_EXTENSIONS = (".md", ".py", ".toml", ".json", ".yml", ".yaml")

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
