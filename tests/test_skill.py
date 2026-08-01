"""The skill sits where the plugin loader looks for it, with valid frontmatter."""

import re

from conftest import find_bare_megamaid_invocations

# Top-level directories of the plugin repo. A path in SKILL.md *prose* that
# starts with one of these is a plugin-repo path, and must carry the
# ${CLAUDE_PLUGIN_ROOT}/ prefix — see
# test_prose_paths_into_the_plugin_repo_are_plugin_root_prefixed. Hardcoded
# rather than globbed off disk so an untracked build artifact can't widen the
# rule; test_the_repo_directory_list_is_not_stale keeps the list honest.
PLUGIN_REPO_DIRS = ("src", "templates", "scripts", "skills", "tests", "commands", "assets")

PLUGIN_ROOT_PREFIX = "${CLAUDE_PLUGIN_ROOT}/"

# Inline code spans, minus fenced blocks. Spans containing whitespace are shell
# commands or markup, never a bare path reference.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCED = re.compile(r"```.*?```", re.DOTALL)

REQUIRED_PATTERN_FILES = {
    "auth_wall.md",
    "graphql_api.md",
    "image_downloads.md",
    "load_more_infinite.md",
    "paginated_html.md",
    "pdf_downloads.md",
    "rest_json_api.md",
    "rss_atom_feed.md",
    "search_seed.md",
    "shopify_json.md",
    "sitemap_crawl.md",
    "spa_hydration.md",
}


def _skill(repo_root):
    path = repo_root / "skills" / "megamaid" / "SKILL.md"
    assert path.exists(), f"plugin skills live at skills/<name>/SKILL.md, missing {path}"
    return path.read_text()


def test_frontmatter_has_name_and_description(repo_root):
    text = _skill(repo_root)
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must open with YAML frontmatter"
    block = match.group(1)
    assert re.search(r"^name:\s*megamaid\s*$", block, re.MULTILINE)
    assert re.search(r"^description:\s*\S", block, re.MULTILINE)


def test_description_has_no_angle_bracket_placeholders(repo_root):
    """Angle-bracket placeholders in a description are rejected by the Desktop validator."""
    block = re.match(r"^---\n(.*?)\n---\n", _skill(repo_root), re.DOTALL).group(1)
    description = re.search(r"^description:\s*(.+)$", block, re.MULTILINE).group(1)
    assert "<" not in description and ">" not in description


def test_pattern_playbooks_moved_with_the_skill(repo_root):
    present = {p.name for p in (repo_root / "skills" / "megamaid" / "patterns").glob("*.md")}
    assert REQUIRED_PATTERN_FILES <= present, f"missing {REQUIRED_PATTERN_FILES - present}"


def test_no_stale_top_level_copies(repo_root):
    for stale in ("SKILL.md", "patterns", "references"):
        assert not (repo_root / stale).exists(), f"{stale} should have moved under skills/megamaid/"


def test_directory_reference_block_documents_real_layout(repo_root):
    """Verify that the Directory Reference tree documents what actually exists.

    A stale directory tree is a recurring footgun — this test ensures every
    path named in the tree is present in the repo. On failure, it names the
    specific missing path so the fixer doesn't have to guess which line is wrong.
    """
    text = _skill(repo_root)

    # Extract the fenced code block after "## Directory Reference"
    match = re.search(
        r"^## Directory Reference\n\n```\n(.*?)\n```",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "Could not find Directory Reference block with fenced tree"

    tree_text = match.group(1)
    lines = tree_text.split("\n")

    # Parse tree structure: extract filesystem paths, tracking nesting via indentation.
    # Each tree-art line represents a path component. Reconstruct full paths by:
    # - Counting │ chars and spacing to determine nesting depth
    # - Maintaining a "path stack" at each depth level
    # - Building full paths from the stack
    paths = []
    path_stack = {}  # depth -> path component
    is_dir_at_depth = {}  # depth -> whether that component is a directory

    for line in lines:
        if not line.strip():
            continue

        # Count leading whitespace/tree characters to determine visual depth
        # Longer leading sequences = deeper nesting
        leading_art = re.match(r"^[\s│├└─]+", line)
        leading_len = len(leading_art.group(0)) if leading_art else 0

        # Remove tree characters and comments
        cleaned = re.sub(r"^[\s│├└─]+", "", line)
        cleaned = re.sub(r"\s*#.*$", "", cleaned)
        is_dir = cleaned.endswith("/")
        cleaned = cleaned.rstrip("/")
        cleaned = cleaned.strip()

        if not cleaned:
            continue

        # Skip conceptual root markers
        if cleaned == "megamaid":
            continue

        # Determine depth: use leading_len (visual indentation) as primary indicator.
        # This naturally encodes the nesting level: more leading art = deeper nesting
        depth = leading_len

        # Update path stack at this depth, remove any deeper levels
        path_stack[depth] = cleaned
        is_dir_at_depth[depth] = is_dir
        for d in list(path_stack.keys()):
            if d > depth:
                del path_stack[d]
                if d in is_dir_at_depth:
                    del is_dir_at_depth[d]

        # Reconstruct full path from all depths
        full_parts = [path_stack[d] for d in sorted(path_stack.keys())]
        full_path = "/".join(full_parts)
        paths.append(full_path)

    # Guard against vacuity: ensure we found substantive paths
    assert len(paths) >= 8, (
        f"Directory reference tree has only {len(paths)} paths; "
        "parser may have failed or tree is too sparse"
    )

    # Verify each path exists relative to repo root
    missing = []
    for path_str in paths:
        full_path = repo_root / path_str
        if not full_path.exists():
            missing.append(path_str)

    assert not missing, (
        f"Directory reference tree names {len(missing)} missing path(s):\n"
        + "\n".join(f"  - {p}" for p in missing)
    )


def test_skill_uses_the_launcher_not_a_bare_binary(repo_root):
    """After a plugin install nothing named `megamaid` is on PATH.

    `find_bare_megamaid_invocations` is the shared detector used across the
    test suite (see tests/conftest.py and tests/test_plugin_wiring.py) — the
    skill must not introduce a second, divergent notion of "bare invocation".
    """
    offenders = find_bare_megamaid_invocations(_skill(repo_root))
    assert not offenders, f"bare megamaid invocations in SKILL.md: {offenders}"


def test_skill_documents_the_launcher_invocation(repo_root):
    assert 'launch.py" --cli' in _skill(repo_root)


def test_version_stamp_does_not_read_the_deleted_version_file(repo_root):
    """The scaffold step used to say "write the contents of this skill's
    `VERSION` file" — a file this branch deleted and test_manifests.py
    guarantees stays deleted, so the skill's core workflow instructed an
    operation the suite pins as impossible. plugin.json is the one version
    source."""
    text = _skill(repo_root)
    assert "`VERSION`" not in text, "VERSION was deleted; read plugin.json's version field"
    assert "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json" in text


# ---------------------------------------------------------------------------
# Prose path references
#
# The Directory Reference tree above is only half the surface. Two dead paths
# (`templates/base.py`, which is `src/megamaid/base.py`) and two unresolvable
# ones (bare `src/megamaid/`, `templates/` — repo-relative, so they resolve
# against the *reader's* cwd, not the installed plugin) survived a passing
# suite because nothing checked SKILL.md's prose. These guards close that,
# following tests/test_readme.py's approach: extract candidates, exclude by a
# documented rule, and refuse to pass vacuously.
# ---------------------------------------------------------------------------


def _prose_code_spans(text: str) -> list[str]:
    """Inline code spans from SKILL.md prose, fenced blocks removed.

    Spans containing whitespace are dropped: those are shell command lines
    (``python -m venv .venv && source ...``) or HTML markup
    (``<link rel="alternate" ...>``), not path references.

    Args:
        text: full SKILL.md contents.

    Returns:
        Candidate spans in first-seen order, deduplicated.
    """
    body = _FENCED.sub("", text)
    found: list[str] = []
    seen: set[str] = set()
    for span in _CODE_SPAN.findall(body):
        if not span or any(char.isspace() for char in span):
            continue
        if span in seen:
            continue
        seen.add(span)
        found.append(span)
    return found


def test_the_repo_directory_list_is_not_stale(repo_root):
    """PLUGIN_REPO_DIRS is hardcoded; a renamed directory must not silently
    shrink what the prefix rule below covers."""
    missing = [name for name in PLUGIN_REPO_DIRS if not (repo_root / name).is_dir()]
    assert not missing, f"PLUGIN_REPO_DIRS names directories that no longer exist: {missing}"


def test_plugin_root_prefixed_prose_paths_exist(repo_root):
    """Every ${CLAUDE_PLUGIN_ROOT}/… path named in prose must be a real file.

    This is the guard that would have caught `templates/base.py` once the path
    carried the prefix that makes it resolvable at all.
    """
    candidates = [
        remainder
        for span in _prose_code_spans(_skill(repo_root))
        if span.startswith(PLUGIN_ROOT_PREFIX)
        # the bare prefix itself is the plugin root, nothing to resolve
        if (remainder := span[len(PLUGIN_ROOT_PREFIX) :].rstrip("/"))
    ]

    assert len(candidates) >= 3, (
        f"only found {len(candidates)} ${{CLAUDE_PLUGIN_ROOT}}-prefixed path(s) in SKILL.md "
        "prose; the extraction may have stopped matching — this guard exists so a "
        "broken regex can't silently check nothing"
    )

    missing = [path for path in candidates if not (repo_root / path).exists()]
    assert not missing, (
        "SKILL.md names ${CLAUDE_PLUGIN_ROOT}-relative path(s) that don't exist:\n"
        + "\n".join(f"  - {p}" for p in missing)
    )


def test_prose_paths_into_the_plugin_repo_are_plugin_root_prefixed(repo_root):
    """A bare repo-relative path in SKILL.md resolves against the reader's cwd.

    Claude runs this skill from wherever the user happens to be, so `src/…`
    or `templates/…` written bare points at nothing. Anything addressing the
    plugin repo must say so explicitly.

    Excluded, deliberately: spans starting with ``http`` (URLs), ``/`` (web
    endpoints like ``/graphql`` and ``/collections/*/products.json``), ``.``
    (``./staging/…``, ``.venv/…``, ``.megamaid-version``), and ``<``
    (placeholders). None of those are plugin-repo paths. Names like
    ``targets/`` and ``staging/`` are also untouched by this rule because they
    describe the *scraped project's* layout, not this repo's — which is why the
    rule keys on this repo's top-level directory names rather than on "looks
    like a path".
    """
    offenders = []
    for span in _prose_code_spans(_skill(repo_root)):
        if span.startswith((PLUGIN_ROOT_PREFIX, "http", "/", ".", "<")):
            continue
        first = span.split("/", 1)[0]
        if first in PLUGIN_REPO_DIRS and "/" in span:
            offenders.append(span)

    assert not offenders, (
        "SKILL.md names plugin-repo path(s) without the "
        "${CLAUDE_PLUGIN_ROOT}/ prefix, so they resolve against the reader's "
        "working directory instead of the installed plugin:\n"
        + "\n".join(f"  - {p}" for p in offenders)
    )


def test_skill_local_prose_paths_exist(repo_root):
    """`patterns/…` and `references/…` are relative to the skill's own directory.

    Unlike README.md (repo root), SKILL.md lives at skills/megamaid/, so its
    own sibling references resolve there — the distinction that made the
    plugin restructure rot paths in the first place.
    """
    skill_dir = repo_root / "skills" / "megamaid"
    candidates = [
        span
        for span in _prose_code_spans(_skill(repo_root))
        if span.startswith(("patterns/", "references/"))
    ]

    assert len(candidates) >= 3, (
        f"only found {len(candidates)} skill-local path(s) in SKILL.md prose; "
        "the extraction may have stopped matching"
    )

    missing = [path for path in candidates if not (skill_dir / path).exists()]
    assert not missing, (
        "SKILL.md names skill-relative path(s) that don't exist under "
        f"{skill_dir}:\n" + "\n".join(f"  - {p}" for p in missing)
    )
