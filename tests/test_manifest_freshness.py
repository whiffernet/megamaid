"""The manifest must match HEAD, or upgrade silently does nothing.

A stale known_hashes.json makes every project classify as divergent: upgrade
then refuses every file while reporting confidently that it protected your work.
That failure is silent and self-justifying, so it is a CI gate rather than a
release-checklist line.
"""

import importlib.util
import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_build_script():
    """Load scripts/build_known_hashes.py as a module, matching the pattern
    tests/test_launch.py uses for scripts/launch.py."""
    path = REPO_ROOT / "scripts" / "build_known_hashes.py"
    spec = importlib.util.spec_from_file_location("mm_build_known_hashes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_known_hashes_matches_head():
    result = subprocess.run(
        [sys.executable, "scripts/build_known_hashes.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        "known_hashes.json is stale.\n"
        f"{result.stderr}\n"
        "Regenerate with: python3 scripts/build_known_hashes.py"
    )


def test_manifest_is_keyed_by_filename():
    """hashes/asts must be {filename: [sha256, ...]}, not a flat pool — a flat
    pool lets one file's history vouch for a completely different file (an
    empty historical __init__.py excusing a comment-only cli.py)."""
    data = json.loads((REPO_ROOT / "src" / "megamaid_setup" / "known_hashes.json").read_text())

    assert isinstance(data["hashes"], dict), "hashes must be filename-keyed, not a flat list"
    assert isinstance(data["asts"], dict), "asts must be filename-keyed, not a flat list"

    runtime_files = {p.name for p in (REPO_ROOT / "src" / "megamaid").glob("*.py")}
    assert runtime_files <= data["hashes"].keys(), (
        f"missing runtime files as keys: {runtime_files - data['hashes'].keys()}"
    )
    for name, hashes in data["hashes"].items():
        assert name.endswith(".py"), f"unexpected key {name!r} — expected a .py basename"
        assert hashes == sorted(hashes), f"{name}'s hash list is not sorted"
        assert len(hashes) == len(set(hashes)), f"{name}'s hash list has duplicates"
    assert data["hashes"] == dict(sorted(data["hashes"].items())), "hashes keys are not sorted"
    assert data["asts"] == dict(sorted(data["asts"].items())), "asts keys are not sorted"


def test_check_flag_detects_a_corrupted_manifest(tmp_path, monkeypatch):
    """Redirects --check's target file at a throwaway path so this never
    reads or writes the real committed known_hashes.json, then confirms
    --check still goes red against a manifest that is shaped correctly (the
    new filename-keyed format) but simply wrong — not just against a
    manifest with the old, stale shape."""
    module = _load_build_script()

    fake_target = tmp_path / "known_hashes.json"
    fake_target.write_text(json.dumps({"generated_from": "0" * 40, "hashes": {}, "asts": {}}))
    monkeypatch.setattr(module, "MANIFEST", fake_target)

    assert module.main(["--check"]) == 1, "a wrong-but-correctly-shaped manifest must fail --check"


def test_check_flag_passes_when_the_redirected_target_matches_head(tmp_path, monkeypatch):
    """Companion to the corruption test above: confirms --check's redirection
    itself works (a matching manifest at the fake path passes), so a passing
    result in the real committed-file case isn't just an artifact of the
    check never actually running."""
    module = _load_build_script()

    fresh = module.build_manifest(REPO_ROOT)
    fake_target = tmp_path / "known_hashes.json"
    fake_target.write_text(json.dumps(fresh))
    monkeypatch.setattr(module, "MANIFEST", fake_target)

    assert module.main(["--check"]) == 0
