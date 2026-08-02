"""The manifest must match HEAD, or upgrade silently does nothing.

A stale known_hashes.json makes every project classify as divergent: upgrade
then refuses every file while reporting confidently that it protected your work.
That failure is silent and self-justifying, so it is a CI gate rather than a
release-checklist line.
"""

import hashlib
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


# --- what the manifest is allowed to trust ---------------------------------
#
# The manifest is the trust set: anything in it is "an old release, safe to
# replace". Building it from `git rev-list --all` meant *any content that ever
# existed on any ref* vouched for itself — including a branch someone pushed,
# thought better of, and abandoned. That is the one place the design leaned the
# wrong way on its own asymmetry: everything else resolves ambiguity toward
# refusing, this resolved "someone once typed this on a branch" toward
# overwriting it.


def _repo(tmp_path):
    """A throwaway git repo shaped like this one: src/megamaid/, tags, branches.

    Identity is set repo-locally so this never reads or writes global git
    config, and the default branch is named explicitly so the test does not
    depend on the host's `init.defaultBranch`.
    """
    root = tmp_path / "repo"
    (root / "src" / "megamaid").mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    return root, git


def _commit(root, git, contents: str, message: str):
    (root / "src" / "megamaid" / "images.py").write_text(contents)
    git("add", "-A")
    git("commit", "-q", "-m", message)


def test_a_blob_only_ever_on_an_abandoned_branch_is_not_trusted(tmp_path):
    """The exploit is the workflow the tool's own report recommends.

    `render()` prints "Shared variants - candidates to backport upstream" and
    names five projects' hand-edited images.py. Take the suggestion: paste that
    variant into src/megamaid/images.py on a spike branch, commit, decide
    against it, abandon the branch without merging. HEAD is unchanged — but a
    walk of every ref has now recorded the variant, and those five projects'
    files flip from DIVERGENT (refused, protected) to EXACT (overwritten
    silently). Pushing the branch, or opening and closing a PR from it, spreads
    the poisoned trust set to every clone that fetches it.
    """
    module = _load_build_script()
    root, git = _repo(tmp_path)

    _commit(root, git, "SHIPPED = 1\n", "release")
    git("tag", "v0.1.0")

    hand_edit = "HAND_EDITED_IN_A_PROJECT = 1\n"
    git("checkout", "-q", "-b", "spike")
    _commit(root, git, hand_edit, "try the backport")
    git("checkout", "-q", "main")

    manifest = module.build_manifest(root)
    poisoned = hashlib.sha256(hand_edit.encode()).hexdigest()

    assert poisoned not in manifest["hashes"]["images.py"], (
        "a variant that only ever existed on an abandoned branch is in the trust set — "
        "every project holding that exact file would now be silently overwritten"
    )
    assert hashlib.sha256(b"SHIPPED = 1\n").hexdigest() in manifest["hashes"]["images.py"], (
        "narrowing dropped released content too"
    )


def test_content_that_shipped_in_a_tag_stays_trusted_after_it_leaves_head(tmp_path):
    """Narrowing must not throw away real releases. A file replaced on main
    long ago is still what an old project has on disk, and is exactly what the
    manifest exists to recognise."""
    module = _load_build_script()
    root, git = _repo(tmp_path)

    _commit(root, git, "V1 = 1\n", "v1")
    git("tag", "v0.1.0")
    _commit(root, git, "V2 = 1\n", "v2")
    git("tag", "v0.2.0")

    hashes = module.build_manifest(root)["hashes"]["images.py"]
    for source in (b"V1 = 1\n", b"V2 = 1\n"):
        assert hashlib.sha256(source).hexdigest() in hashes, f"{source!r} fell out of the manifest"


def test_the_current_runtime_is_always_in_its_own_manifest(tmp_path):
    """Whatever HEAD ships must classify EXACT against the manifest shipped
    beside it, or `upgrade` refuses to overwrite files it wrote itself and a
    second run reports the whole runtime as divergent."""
    module = _load_build_script()
    root, git = _repo(tmp_path)

    _commit(root, git, "V1 = 1\n", "v1")
    git("tag", "v0.1.0")
    _commit(root, git, "UNRELEASED = 1\n", "work since the last tag")

    hashes = module.build_manifest(root)["hashes"]["images.py"]
    assert hashlib.sha256(b"UNRELEASED = 1\n").hexdigest() in hashes


def test_local_scratch_branches_do_not_change_what_a_contributor_generates(tmp_path):
    """The manifest must be a function of the repo, not of whichever branches
    a given clone happens to have lying around."""
    module = _load_build_script()
    root, git = _repo(tmp_path)

    _commit(root, git, "SHIPPED = 1\n", "release")
    git("tag", "v0.1.0")
    before = module.build_manifest(root)["hashes"]

    git("checkout", "-q", "-b", "scratch")
    _commit(root, git, "SCRATCH = 1\n", "wip")
    git("checkout", "-q", "main")

    assert module.build_manifest(root)["hashes"] == before


# --- a shallow checkout must say so, not cry "stale" -----------------------


def test_a_shallow_checkout_is_reported_as_a_checkout_problem(tmp_path):
    """CI checks out at fetch-depth 1, which starves the walk: the gate then
    fails with a hash-count mismatch that reads exactly like a stale manifest
    and sends the reader off regenerating a file that is already correct."""
    module = _load_build_script()
    root, git = _repo(tmp_path)
    _commit(root, git, "V1 = 1\n", "v1")
    git("tag", "v0.1.0")
    _commit(root, git, "V2 = 1\n", "v2")
    git("tag", "v0.2.0")

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--no-tags", f"file://{root}", str(shallow)],
        check=True,
        capture_output=True,
    )

    reason = module.truncated_checkout(shallow)
    assert reason, "a --depth 1 clone was not recognised as a truncated checkout"
    assert module.truncated_checkout(root) is None, "the full checkout was called truncated"


def test_the_shallow_diagnostic_names_fetch_depth_zero(tmp_path, monkeypatch, capsys):
    """The message has to carry the fix. A reader who sees only "N hashes vs M"
    has no way to know the checkout, not the file, is what is wrong."""
    module = _load_build_script()
    root, git = _repo(tmp_path)
    _commit(root, git, "V1 = 1\n", "v1")
    git("tag", "v0.1.0")
    _commit(root, git, "V2 = 1\n", "v2")
    git("tag", "v0.2.0")

    full_manifest = module.build_manifest(root)

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--no-tags", f"file://{root}", str(shallow)],
        check=True,
        capture_output=True,
    )
    target = shallow / "src" / "megamaid_setup" / "known_hashes.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(full_manifest))

    monkeypatch.setattr(module, "REPO_ROOT", shallow)

    assert module.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "fetch-depth: 0" in err, f"the diagnostic does not name the fix:\n{err}"
    assert "is out of date" not in err, f"a truncated checkout was reported as staleness:\n{err}"
    assert "do NOT regenerate" in err, (
        f"the message must stop the reader regenerating a correct file:\n{err}"
    )
