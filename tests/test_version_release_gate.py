"""The gate that stops a release from shipping under a stale version.

`.claude-plugin/VERSION.txt` sat at 0.9.0 while tags advanced to v0.9.4 (#26).
Nothing failed. The consequences were all silent:

* Claude Code caches the plugin under a version-keyed directory, so `plugin
  update` saw nothing new to fetch;
* `launch.py` stamped the state venv with that version, so the venv — which
  holds a *non-editable* copy of the code — was never rebuilt, leaving users on
  v0.9.0 code no matter what they installed;
* `importlib.metadata.version("megamaid")` returned 0.9.0, so `upgrade` stamped
  every project it touched with a version that was four releases wrong.

These tests run in CI's `test` job, which checks out at `fetch-depth: 0` and so
can see tags. They are the only thing standing between a code change and a
release that silently never reaches anyone.
"""

import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / ".claude-plugin" / "VERSION.txt"

#: What pip installs, plus the files deciding how. A change under any of these
#: reaches users and therefore obliges a version bump. Docs, tests, CI config
#: and the plan/spec workspace deliberately do not.
SHIPPED = ("src/", "scripts/", "pyproject.toml", ".claude-plugin/")

# Leading zeros rejected — setuptools normalizes 0.10.01 to 0.10.1, so the
# declared version and the installed one would silently disagree.
SEMVER = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def semver(text):
    """Parse `1.2.3` or `v1.2.3` into a comparable tuple, or None."""
    match = SEMVER.match(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def git(*args):
    """Run git in the repo and return stripped stdout ('' on failure)."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        # Returning "" would make a broken git call look like "no tags" or "no
        # files changed", so the gate would pass precisely when it cannot see.
        pytest.fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@pytest.fixture(scope="module")
def declared():
    """The version the repo claims, from VERSION.txt."""
    return VERSION_FILE.read_text().strip()


@pytest.fixture(scope="module")
def latest_tag():
    """The highest `v*` tag, or None when the clone has no tags."""
    tags = [semver(line) for line in git("tag", "--list", "v*").splitlines()]
    found = sorted(tag for tag in tags if tag)
    return found[-1] if found else None


def test_declared_version_is_parseable(declared):
    assert semver(declared), f"VERSION.txt holds {declared!r}, which is not bare semver"


def test_readme_pins_the_current_version(declared):
    """The pipx install line pins a tag, and a stale pin installs old code.

    Same failure as #26, one file over: nothing errors, the reader just gets a
    version that is not the one being documented around it.
    """
    readme = (ROOT / "README.md").read_text()
    pinned = re.findall(r"megamaid@v(\d+\.\d+\.\d+)#egg=", readme)
    assert pinned, "expected a pipx install line pinning megamaid@v<version>#egg="
    stale = [version for version in pinned if version != declared]
    assert not stale, (
        f"README pins {', '.join(stale)} but VERSION.txt is {declared}. "
        "Update the pipx install line."
    )


def test_version_is_never_behind_the_latest_tag(declared, latest_tag):
    """The exact failure of #26: tags advanced, VERSION.txt did not."""
    if latest_tag is None:
        pytest.fail(
            "no v* tags visible — this clone is shallow, so the gate cannot run. "
            "CI needs `fetch-depth: 0` on this job."
        )
    assert semver(declared) >= latest_tag, (
        f"VERSION.txt is {declared} but v{'.'.join(map(str, latest_tag))} is already "
        "released. A version behind its own tags means plugin update, the venv "
        "stamp and upgrade's project stamps are all wrong. "
        "Fix with: python3 scripts/bump_version.py --set <version above the tag>"
    )


def test_shipped_changes_since_the_last_release_carry_a_bump(declared, latest_tag):
    """Changing installed code without bumping would release it invisibly.

    On `main` immediately after a merge the tag is cut at HEAD, so nothing has
    changed since it and this is vacuously true. On a branch it bites.
    """
    if latest_tag is None:
        pytest.fail("no v* tags visible — CI needs `fetch-depth: 0` on this job.")

    tag = "v" + ".".join(str(part) for part in latest_tag)
    changed = [
        line
        for line in git("diff", "--name-only", f"{tag}...HEAD").splitlines()
        if any(line.startswith(prefix) for prefix in SHIPPED)
    ]
    if not changed:
        return

    assert semver(declared) > latest_tag, (
        f"{len(changed)} shipped file(s) changed since {tag} "
        f"(e.g. {', '.join(changed[:3])}) but VERSION.txt is still {declared}. "
        "Releasing this would leave every installed user on the old code, "
        "because the plugin cache and the venv stamp both key on the version. "
        "Fix with: python3 scripts/bump_version.py --patch  (or --minor/--major)"
    )
