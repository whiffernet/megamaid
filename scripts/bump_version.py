#!/usr/bin/env python3
"""Raise the plugin version everywhere it is written down.

The repo is the source of truth for the released version: `release-tag.yml`
cuts `v$(cat .claude-plugin/VERSION.txt)` at merge rather than incrementing
whatever tag it finds. That inversion is what makes drift impossible — a tag
can no longer advance without the tree advancing with it.

Three files record the version, and this script is the only thing that should
edit them. Each is checked by `tests/test_version_release_gate.py`:

    .claude-plugin/VERSION.txt   read by setuptools (pyproject `dynamic`),
                                 so it becomes `importlib.metadata.version`
    .claude-plugin/plugin.json   read by Claude Code and by `launch.py`
                                 (its agreement with VERSION.txt is checked
                                 by tests/test_manifests.py)
    README.md                    the pinned pipx install line

Every replacement is computed and validated before anything is written, so a
failure cannot leave them disagreeing — which is the drift this exists to stop.

Usage:
    python3 scripts/bump_version.py --patch
    python3 scripts/bump_version.py --minor
    python3 scripts/bump_version.py --major
    python3 scripts/bump_version.py --set 1.2.3
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / ".claude-plugin" / "VERSION.txt"
PLUGIN_FILE = ROOT / ".claude-plugin" / "plugin.json"
README_FILE = ROOT / "README.md"

# Leading zeros are rejected: setuptools normalizes "0.10.01" to PEP 440
# "0.10.1", so plugin.json would say 0.10.01 while importlib.metadata said
# 0.10.1 — issue #26 all over again, from a single typo.
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse(text: str) -> tuple[int, int, int]:
    """Split a bare semver string into its three integer parts.

    Args:
        text: e.g. "0.9.4".

    Returns:
        (major, minor, patch).

    Raises:
        SystemExit: when the string is not bare three-part semver.
    """
    match = SEMVER.match(text.strip())
    if not match:
        raise SystemExit(f"  x not a bare semver version: {text.strip()!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def bump(current: tuple[int, int, int], level: str) -> str:
    """Apply one bump level.

    Args:
        current: (major, minor, patch).
        level: "major", "minor" or "patch".

    Returns:
        The new version string.
    """
    major, minor, patch = current
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def latest_tag() -> tuple[int, int, int] | None:
    """The highest released `v*` tag, or None when git or tags are unavailable.

    Returns:
        (major, minor, patch), or None.
    """
    result = subprocess.run(
        ["git", "-C", str(ROOT), "tag", "--list", "v*"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    found: list[tuple[int, int, int]] = []
    for line in result.stdout.split():
        match = SEMVER.match(line.lstrip("v"))
        if match:
            major, minor, patch = (int(part) for part in match.groups())
            found.append((major, minor, patch))
    return max(found) if found else None


def _patched_plugin(new: str) -> str:
    """plugin.json with its version key set to `new`.

    Patched as text rather than via `json.dump`: dumping would reformat the
    whole manifest — indentation, key order, unicode escaping — and bury a
    one-line version change in an unreviewable diff.

    Raises:
        SystemExit: when the key is missing, ambiguous, or the wrong one matched.
    """
    original = PLUGIN_FILE.read_text()
    # Anchored to the top-level key's exact two-space indent so a nested
    # "version" inside, say, an mcpServers block cannot be the one rewritten.
    patched, count = re.subn(
        r'^(  "version"\s*:\s*")[^"]*(")', rf"\g<1>{new}\g<2>", original, count=1, flags=re.M
    )
    if count != 1:
        raise SystemExit(f"  x found {count} top-level version keys in {PLUGIN_FILE.name}, want 1")
    if json.loads(patched)["version"] != new:
        raise SystemExit(f"  x patching {PLUGIN_FILE.name} would not have set version to {new}")
    return patched


def _patched_readme(new: str) -> str:
    """README with every pinned install tag set to `new`.

    The gate asserts this pin tracks VERSION.txt, so a bump that skipped it
    would turn the build red on every release — the tool would be prescribing
    its own failure.

    Raises:
        SystemExit: when no pin is found, so a silent no-op cannot pass for success.
    """
    original = README_FILE.read_text()
    patched, count = re.subn(r"(megamaid@v)\d+\.\d+\.\d+(#egg=)", rf"\g<1>{new}\g<2>", original)
    if count < 1:
        raise SystemExit(f"  x no megamaid@v<version>#egg= pin found in {README_FILE.name}")
    return patched


def write(new: str) -> None:
    """Set `new` in every file that records the version.

    Every replacement is computed and validated before anything is written. A
    partial write is the exact drift this script exists to prevent: an earlier
    version bumped VERSION.txt first, so a failure patching plugin.json left the
    two disagreeing.

    Args:
        new: the version to write.
    """
    staged = {
        VERSION_FILE: new + "\n",
        PLUGIN_FILE: _patched_plugin(new),
        README_FILE: _patched_readme(new),
    }
    for path, content in staged.items():
        path.write_text(content)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--major", action="store_true", help="x.0.0")
    group.add_argument("--minor", action="store_true", help="0.x.0")
    group.add_argument("--patch", action="store_true", help="0.0.x")
    group.add_argument("--set", metavar="VERSION", help="an explicit version")
    args = parser.parse_args(argv[1:])

    current = VERSION_FILE.read_text().strip()

    # Bump from whichever is higher, the file or the newest tag. When the file
    # has fallen behind — which is the whole reason this script exists — bumping
    # from it alone regenerates a version that is already released: 0.9.0 with
    # a --patch yields 0.9.1, and v0.9.1 was cut months ago.
    baseline = max(parse(current), latest_tag() or (0, 0, 0))

    if args.set:
        new = args.set.strip()
        parse(new)  # rejects non-semver with a message rather than a TypeError below
        if parse(new) <= baseline:
            raise SystemExit(f"  x {new} does not advance on {'.'.join(str(p) for p in baseline)}")
    else:
        level = "major" if args.major else "minor" if args.minor else "patch"
        new = bump(baseline, level)

    write(new)
    touched = ", ".join(f.name for f in (VERSION_FILE, PLUGIN_FILE, README_FILE))
    print(f"  ok {current} -> {new}  ({touched})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
