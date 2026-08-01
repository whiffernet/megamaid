"""The skill sits where the plugin loader looks for it, with valid frontmatter."""

import re

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
