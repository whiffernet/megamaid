#!/usr/bin/env python3
"""Generate known_hashes.json — every historical version of every runtime file.

Walks the full git history collecting the sha256 and ast.dump of each runtime
module ever committed. `megamaid upgrade` uses this to tell "an old release,
safe to replace" from "someone edited this, leave it alone".

BOTH historical paths are walked. The runtime moved from templates/megamaid/ to
src/megamaid/ in v0.9.0; git tracked the rename, so a walk covering only the
current path would classify every pre-v0.9.0 project as divergent.

Usage:  python3 scripts/build_known_hashes.py [--check]

  --check  exit 1 if the committed manifest differs from what HEAD produces,
           printing nothing else. Used by CI.
"""

import argparse
import ast
import hashlib
import json
import pathlib
import subprocess
import sys

RUNTIME_PATHS = ("templates/megamaid", "src/megamaid")
MANIFEST = pathlib.Path("src/megamaid_setup/known_hashes.json")


def _git(repo_root: pathlib.Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo_root, capture_output=True, check=True).stdout


def _total(by_file: dict[str, list[str]]) -> int:
    """Sum of per-file counts, for human-readable reporting only."""
    return sum(len(v) for v in by_file.values())


def build_manifest(repo_root: pathlib.Path) -> dict:
    """Collect every historical sha256 and ast.dump of the runtime modules.

    Keyed per filename basename, not a flat pool: a match must be against that
    *same* file's own history, or an empty historical `__init__.py` ends up
    vouching for any other file that has been reduced to a comment. The rename
    from templates/megamaid/ to src/megamaid/ still folds into one key, since
    both paths' basenames land in the same dict entry.

    Args:
        repo_root: the repository root (must be a git checkout).

    Returns:
        {"generated_from": <head sha>, "hashes": {filename: [...]}, "asts":
        {filename: [...]}} with keys and value lists sorted, so the output is
        deterministic and diffable.
    """
    hashes: dict[str, set[str]] = {}
    asts: dict[str, set[str]] = {}

    for commit in _git(repo_root, "rev-list", "--all").decode().split():
        for base in RUNTIME_PATHS:
            listing = _git(repo_root, "ls-tree", "-r", commit, f"{base}/").decode()
            for line in listing.splitlines():
                parts = line.split()
                if len(parts) < 4 or parts[1] != "blob":
                    continue
                name = pathlib.PurePosixPath(parts[3]).name
                blob = _git(repo_root, "cat-file", "blob", parts[2])
                hashes.setdefault(name, set()).add(hashlib.sha256(blob).hexdigest())
                try:
                    asts.setdefault(name, set()).add(ast.dump(ast.parse(blob.decode())))
                except (SyntaxError, UnicodeDecodeError):
                    # A historical file that no longer parses is still a valid
                    # hash match; it just cannot contribute an AST.
                    pass

    head = _git(repo_root, "rev-parse", "HEAD").decode().strip()
    return {
        "generated_from": head,
        "hashes": {name: sorted(vals) for name, vals in sorted(hashes.items())},
        "asts": {name: sorted(vals) for name, vals in sorted(asts.items())},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_known_hashes")
    parser.add_argument("--check", action="store_true", help="exit 1 if the manifest is stale")
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    fresh = build_manifest(repo_root)
    target = repo_root / MANIFEST

    if args.check:
        if not target.exists():
            print(f"{MANIFEST} does not exist — run this script without --check", file=sys.stderr)
            return 1
        committed = json.loads(target.read_text())
        if committed.get("hashes") != fresh["hashes"] or committed.get("asts") != fresh["asts"]:
            print(
                f"{MANIFEST} is stale.\n"
                f"  committed: {_total(committed.get('hashes', {}))} hashes across "
                f"{len(committed.get('hashes', {}))} files\n"
                f"  from HEAD: {_total(fresh['hashes'])} hashes across "
                f"{len(fresh['hashes'])} files\n"
                f"Regenerate with: python3 scripts/build_known_hashes.py",
                file=sys.stderr,
            )
            return 1
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fresh, indent=2) + "\n")
    print(
        f"wrote {MANIFEST}: {_total(fresh['hashes'])} hashes, {_total(fresh['asts'])} ASTs, "
        f"across {len(fresh['hashes'])} files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
