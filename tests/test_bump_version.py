"""Unit tests for the version bumper's pure logic.

The file-writing half is deliberately not covered here — it reads module-level
paths and writing to them would mutate the repo under the suite. Its failure
paths were exercised by hand against an isolated copy; what these cover is the
arithmetic and the parsing, where a silent wrong answer is most plausible.
"""

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def bump_version():
    spec = importlib.util.spec_from_file_location("bv", ROOT / "scripts" / "bump_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("level", "expected"),
    [("patch", "0.9.5"), ("minor", "0.10.0"), ("major", "1.0.0")],
)
def test_bump_levels(bump_version, level, expected):
    assert bump_version.bump((0, 9, 4), level) == expected


def test_minor_and_major_reset_the_parts_below(bump_version):
    assert bump_version.bump((1, 2, 3), "minor") == "1.3.0"
    assert bump_version.bump((1, 2, 3), "major") == "2.0.0"


@pytest.mark.parametrize("text", ["0.9.4", "1.0.0", "0.0.0", "10.20.30"])
def test_parse_accepts_bare_semver(bump_version, text):
    assert bump_version.parse(text)


@pytest.mark.parametrize("text", ["1.2", "v1.2.3", "1.2.3-rc1", "", "abc", "1.2.3.4"])
def test_parse_rejects_non_semver(bump_version, text):
    with pytest.raises(SystemExit):
        bump_version.parse(text)


@pytest.mark.parametrize("text", ["0.10.01", "01.2.3", "1.02.3"])
def test_parse_rejects_leading_zeros(bump_version, text):
    """setuptools normalizes 0.10.01 to PEP 440 0.10.1.

    Accepting it would put 0.10.01 in plugin.json while
    `importlib.metadata.version` reported 0.10.1 — issue #26's exact shape,
    reached by a single typo, and every other gate here would stay green.
    """
    with pytest.raises(SystemExit):
        bump_version.parse(text)


def test_fingerprinted_paths_cover_the_declared_package_data():
    """`launch.py` hand-lists non-.py installed files; pyproject declares them.

    Nothing else makes the two agree, so package-data added later would be
    installed but not fingerprinted, and a change to it would not rebuild a venv.
    """
    import tomllib

    spec = importlib.util.spec_from_file_location("mm", ROOT / "scripts" / "launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = declared["tool"]["setuptools"]["package-data"]
    expected = {
        f"src/{package}/{name}" for package, names in package_data.items() for name in names
    }

    missing = expected - set(launch._FINGERPRINTED)
    assert not missing, (
        f"pyproject declares {sorted(missing)} as package-data, so pip installs it, "
        "but launch.py does not fingerprint it — a change would not rebuild the venv. "
        "Add it to _FINGERPRINTED in scripts/launch.py."
    )
