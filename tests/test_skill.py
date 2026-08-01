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
