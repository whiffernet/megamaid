"""Plugin and marketplace manifests are well-formed and agree on version."""

import json
import re


def test_plugin_manifest_has_semver_version(repo_root):
    manifest = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "megamaid"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]), (
        f"version must be bare semver with no 'v' prefix, got {manifest['version']!r}"
    )


def test_marketplace_lists_this_plugin_from_repo_root(repo_root):
    market = json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "whiffernet"
    entries = market["plugins"]
    assert len(entries) == 1, "marketplace should host exactly one plugin"
    assert entries[0]["name"] == "megamaid"
    assert entries[0]["source"] == "./", "plugin is self-hosted at the repo root"


def test_version_file_is_gone(repo_root):
    """VERSION was a third, always-stale version source. plugin.json is authoritative."""
    assert not (repo_root / "VERSION").exists()
