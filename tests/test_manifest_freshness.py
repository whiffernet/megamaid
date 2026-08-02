"""The manifest must match HEAD, or upgrade silently does nothing.

A stale known_hashes.json makes every project classify as divergent: upgrade
then refuses every file while reporting confidently that it protected your work.
That failure is silent and self-justifying, so it is a CI gate rather than a
release-checklist line.
"""

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


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
