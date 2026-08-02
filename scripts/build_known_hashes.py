#!/usr/bin/env python3
"""Generate known_hashes.json — every released version of every runtime file.

Walks git history collecting the sha256 and ast.dump of each runtime module,
building the trust set `megamaid upgrade` uses to tell "an old release, safe to
replace" from "someone edited this, leave it alone".

WHICH history is walked is a safety property, not a detail. This is the only
input that can talk `upgrade` into overwriting a file, so it must contain
content that actually shipped and nothing else. See TRUST_REVS.

BOTH historical paths are walked. The runtime moved from templates/megamaid/ to
src/megamaid/ in v0.9.0; git tracked the rename, so a walk covering only the
current path would classify every pre-v0.9.0 project as divergent.

Usage:  python3 scripts/build_known_hashes.py [--check]

  --check  exit 1 if the committed manifest differs from what this checkout
           produces. Used by CI. Needs full history — see truncated_checkout().
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
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The history the trust set is built from: every tag, plus the line of
#: development HEAD is on.
#:
#: NOT `--all`. That walks every ref, so any commit on any branch — including
#: one someone pushed, thought better of, and abandoned — vouched for its own
#: content. The exploit is the workflow the report itself recommends: it prints
#: "Shared variants - candidates to backport upstream" and names the projects,
#: so pasting one of those hand-edited files upstream on a spike branch and
#: abandoning it silently flipped exactly those projects' files from DIVERGENT
#: (refused, protected) to EXACT (overwritten, and not even mentioned in the
#: report). Everything else in this design resolves ambiguity toward refusing;
#: that resolved "someone once typed this on a branch" toward overwriting.
#:
#: HEAD is included alongside the tags, not replaced by them, for two reasons.
#: It keeps whatever HEAD ships inside its own manifest, so a project upgraded
#: to this runtime classifies EXACT on the next run instead of having every
#: file it was just given refused as divergent. And it keeps the walk stable
#: across a merge: a branch's blobs are already in the set before the merge, so
#: landing it does not change what the manifest should contain. Tags alone
#: would grow the set at every release and leave the gate red on main.
#:
#: Both are deterministic given the checkout, so the manifest no longer depends
#: on which branches a particular clone happens to have.
TRUST_REVS = ("--tags", "HEAD")


def _git(repo_root: pathlib.Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo_root, capture_output=True, check=True).stdout


def _total(by_file: dict[str, list[str]]) -> int:
    """Sum of per-file counts, for human-readable reporting only."""
    return sum(len(v) for v in by_file.values())


def _unreachable(committed: dict[str, list[str]], fresh: dict[str, list[str]]) -> int:
    """How many recorded hashes this checkout's walk could not find.

    The direction matters. A manifest that is genuinely stale is *smaller* than
    what the walk produces (someone edited a runtime file and did not
    regenerate). A checkout missing history produces a manifest smaller than
    the committed one. Only the second is a checkout problem.
    """
    return sum(len(set(v) - set(fresh.get(name, []))) for name, v in committed.items())


def truncated_checkout(repo_root: pathlib.Path) -> str | None:
    """Why this checkout cannot rebuild the manifest, or None if it can.

    `actions/checkout` defaults to `fetch-depth: 1`, which leaves the walk with
    one commit and no tags. The gate then fails with a hash-count mismatch that
    reads exactly like a stale manifest, sending the reader off to regenerate a
    file that was already correct — a gate that is red for an unrelated reason
    is not a gate.

    Args:
        repo_root: the repository root (must be a git checkout).

    Returns:
        A human-readable reason, or None when the checkout has enough history.
    """
    if _git(repo_root, "rev-parse", "--is-shallow-repository").decode().strip() == "true":
        return "this is a shallow clone (`git rev-parse --is-shallow-repository` is true)"
    if not _git(repo_root, "tag", "--list").decode().strip():
        return "this checkout has no tags, and the manifest is built from them"
    return None


def build_manifest(repo_root: pathlib.Path) -> dict:
    """Collect the sha256 and ast.dump of every runtime module that shipped.

    Scoped to TRUST_REVS — tags plus HEAD's own line of development, never
    every ref. See TRUST_REVS for why that scope is a safety property.

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

    for commit in _git(repo_root, "rev-list", *TRUST_REVS).decode().split():
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

    repo_root = REPO_ROOT
    fresh = build_manifest(repo_root)
    target = repo_root / MANIFEST

    if args.check:
        if not target.exists():
            print(f"{MANIFEST} does not exist — run this script without --check", file=sys.stderr)
            return 1
        committed = json.loads(target.read_text())
        if committed.get("hashes") == fresh["hashes"] and committed.get("asts") == fresh["asts"]:
            return 0

        # Distinguish "the file is out of date" from "this checkout cannot
        # answer the question". Both fail, but only one is fixed by
        # regenerating, and being sent to regenerate a correct file is how a
        # gate trains people to ignore it.
        lost = _unreachable(committed.get("hashes", {}), fresh["hashes"])
        reason = truncated_checkout(repo_root) if lost else None
        if reason:
            print(
                f"{MANIFEST} cannot be checked here: {reason}.\n"
                f"  the manifest is built from `git rev-list {' '.join(TRUST_REVS)}`, and "
                f"{lost} of the {_total(committed.get('hashes', {}))} hashes it records\n"
                f"  are not reachable in this checkout.\n"
                "This is the checkout's shape, not a stale manifest — do NOT regenerate.\n"
                "  In CI, give the job's checkout step:\n"
                "      with:\n"
                "        fetch-depth: 0\n"
                "  Locally:  git fetch --unshallow --tags",
                file=sys.stderr,
            )
            return 1

        print(
            f"{MANIFEST} is out of date.\n"
            f"  committed:      {_total(committed.get('hashes', {}))} hashes across "
            f"{len(committed.get('hashes', {}))} files\n"
            f"  this checkout:  {_total(fresh['hashes'])} hashes across "
            f"{len(fresh['hashes'])} files\n"
            f"Regenerate with: python3 scripts/build_known_hashes.py",
            file=sys.stderr,
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fresh, indent=2) + "\n")
    print(
        f"wrote {MANIFEST}: {_total(fresh['hashes'])} hashes, {_total(fresh['asts'])} ASTs, "
        f"across {len(fresh['hashes'])} files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
