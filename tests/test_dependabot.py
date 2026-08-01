"""Dependabot's config points at directories that exist, and covers pip.

Two defects reached the branch together:

* a `package-ecosystem: docker` entry aimed at `/mcp`, a directory this branch
  deleted. Dependabot errors on every run against a missing directory, and a
  job that always errors is a job nobody reads.
* no `pip` ecosystem at all, while the spec justifies the launcher's unpinned
  runtime `pip install .[mcp,cli]` with "Dependabot continues to cover the
  declared ranges". With no pip entry, nothing covered them.

Parsed with a deliberately small line scanner rather than PyYAML: the config
is flat and hand-written, and the suite has no third-party test dependency to
skip on. `test_the_parse_found_the_real_file` is the vacuity guard that keeps
the scanner honest.
"""

import re

CONFIG = ".github/dependabot.yml"

_ECOSYSTEM = re.compile(r"^-\s*package-ecosystem:\s*(\S+)\s*$")
_DIRECTORY = re.compile(r"^\s*directory:\s*(\S+)\s*$")


def _updates(repo_root) -> list[tuple[str, str | None]]:
    """Return (ecosystem, directory) for each entry in `updates`.

    Args:
        repo_root: repository root.

    Returns:
        One tuple per `- package-ecosystem:` block, in file order. The
        directory is None when a block declares none.
    """
    entries: list[tuple[str, str | None]] = []
    current: str | None = None
    directory: str | None = None

    for line in (repo_root / CONFIG).read_text().splitlines():
        match = _ECOSYSTEM.match(line)
        if match:
            if current is not None:
                entries.append((current, directory))
            current, directory = match.group(1), None
            continue
        if current is not None and directory is None:
            dir_match = _DIRECTORY.match(line)
            if dir_match:
                directory = dir_match.group(1)

    if current is not None:
        entries.append((current, directory))
    return entries


def test_the_parse_found_the_real_file(repo_root):
    """Vacuity guard: a scanner that matches nothing would pass everything."""
    entries = _updates(repo_root)
    assert len(entries) >= 2, f"parsed only {entries} from {CONFIG}"
    assert all(directory for _, directory in entries), (
        f"every ecosystem must declare a directory; got {entries}"
    )


def test_every_configured_directory_exists(repo_root):
    """The docker entry pointed at /mcp, deleted on this branch."""
    missing = [
        (ecosystem, directory)
        for ecosystem, directory in _updates(repo_root)
        if not (repo_root / directory.lstrip("/")).is_dir()
    ]
    assert not missing, (
        "dependabot.yml points at directories that do not exist — Dependabot "
        f"errors on every run for these: {missing}"
    )


def test_pip_ecosystem_covers_the_repo_root(repo_root):
    """The launcher pip-installs at runtime with no lockfile.

    pyproject.toml's declared ranges are the only thing holding that install
    current, so they have to be what Dependabot watches.
    """
    entries = dict(_updates(repo_root))
    assert "pip" in entries, (
        "no pip ecosystem: the runtime `pip install .[mcp,cli]` is unpinned and "
        "nothing is updating the ranges in pyproject.toml"
    )
    assert entries["pip"] == "/", f"pyproject.toml is at the repo root, got {entries['pip']!r}"


def test_no_docker_ecosystem_survives_the_container_retirement(repo_root):
    entries = dict(_updates(repo_root))
    assert "docker" not in entries, (
        "the Docker MCP container was retired on this branch; a docker "
        "ecosystem has nothing left to track"
    )
