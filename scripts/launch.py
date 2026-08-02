#!/usr/bin/env python3
"""megamaid plugin launcher — the only imperative code in the install path.

Claude Code's plugin system registers components; it does not create venvs or
executables. This script lazily builds a venv the first time the MCP server (or
the CLI) is needed, then execs into it.

Two modes:
    launch.py                 ensure the venv, exec megamaid-mcp  (stdio MCP)
    launch.py --cli <args…>   ensure the venv, exec megamaid <args…>

STDLIB ONLY. This runs before any dependency exists; an import of anything
third-party here is a defect, and tests/test_launch.py enforces it.

Timing matters in MCP mode: MCP_TIMEOUT defaults to 30000 ms and is a hard
connect deadline, after which Claude Code SIGTERMs then SIGKILLs this process.
The [mcp] extra is kept light for exactly this reason, and every attempt is
logged before slow work starts so /megamaid-doctor can explain a killed run.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import venv
from datetime import datetime, timezone

PIP_CACHE = pathlib.Path.home() / ".cache" / "megamaid-pip"

# Both console scripts must exist and be executable before a venv is trusted:
# megamaid-mcp is this file's own exec target, megamaid is Task 8's --cli target.
REQUIRED_SCRIPTS = ("megamaid-mcp", "megamaid")


class LaunchError(Exception):
    """A bootstrap failure carrying a stable MM-xx code for the docs table."""

    def __init__(self, code: str, message: str, fix: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


def plugin_root() -> pathlib.Path:
    """The plugin's install directory.

    Returns:
        CLAUDE_PLUGIN_ROOT when Claude Code sets it, else this file's parent's
        parent (which is the repo root during development).
    """
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return pathlib.Path(env).resolve()
    return pathlib.Path(__file__).resolve().parent.parent


def plugin_version(root: pathlib.Path) -> str:
    """Read the authoritative version from the plugin manifest.

    Args:
        root: the plugin install directory.

    Returns:
        Bare semver string, e.g. "0.9.0".

    Raises:
        LaunchError: MM-15 when the manifest is missing or malformed.
    """
    manifest = root / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(manifest.read_text())["version"]
    except (OSError, ValueError, KeyError) as exc:
        raise LaunchError(
            "MM-15",
            f"Could not read a version from {manifest}: {exc}",
            "Reinstall the plugin: claude plugin install megamaid@whiffernet",
        )


def state_dir() -> pathlib.Path:
    """Where the bootstrapped venv and the launch log live."""
    env = os.environ.get("MEGAMAID_STATE_DIR")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".local" / "state" / "megamaid"


def _log(message: str) -> None:
    """Append a timestamped line to the launch log, and mirror it to stderr.

    The log is the only durable diagnostic: on MCP timeout Claude Code kills
    this process and surfaces nothing but "Failed to connect".
    """
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{stamp}  {message}"
    with (directory / "launch.log").open("a") as handle:
        handle.write(line + "\n")
    print(line, file=sys.stderr)


#: Non-.py files pip copies out of the plugin root, for fingerprinting.
#: `packages.find` ships `src/**/*.py`; these are the rest — declared
#: package-data, plus the two files deciding what gets installed and under what
#: version. known_hashes.json is regenerated on its own in `chore:` commits that
#: touch no .py file at all, so leaving it out would let a stale manifest ride
#: into a venv that believed itself current.
_FINGERPRINTED = (
    "pyproject.toml",
    ".claude-plugin/VERSION.txt",
    "src/megamaid_setup/known_hashes.json",
)


def source_fingerprint(root: pathlib.Path) -> str:
    """A digest of the source pip would install from `root`.

    Args:
        root: the plugin install directory.

    Returns:
        A short hex digest, stable across machines and checkout order.
    """
    paths = sorted((root / "src").rglob("*.py")) if (root / "src").is_dir() else []
    paths += [root / name for name in _FINGERPRINTED if (root / name).is_file()]

    digest = hashlib.sha256()
    for path in sorted(paths):
        # Hash the name as well as the bytes: without it, renaming a module or
        # deleting one whose content is duplicated elsewhere leaves the digest
        # unchanged.
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def venv_stamp(root: pathlib.Path, version: str) -> str:
    """What `.plugin-version` should contain for this plugin checkout.

    Keyed on a source digest as well as the version, because keying on the
    version alone assumes the version always advances with the code — and when
    it did not, `claude plugin update` silently left every user running the code
    they already had. v0.9.0 through v0.9.4 all reported `0.9.0`, so the stamp
    matched forever and the venv, which holds a non-editable copy, was never
    rebuilt. See issue #26.

    Args:
        root: the plugin install directory.
        version: the version read from the plugin manifest.

    Returns:
        The stamp line, e.g. "0.10.0 3f2a1c9e8b7d6a54".
    """
    return f"{version} {source_fingerprint(root)}"


def venv_is_current(venv_path: pathlib.Path, stamp_value: str) -> bool:
    """True when the venv exists and was built from exactly this source.

    Args:
        venv_path: the state venv.
        stamp_value: what `venv_stamp` says the stamp should be.
    """
    stamp = venv_path / ".plugin-version"
    if not (venv_path / "bin").is_dir() or not stamp.is_file():
        return False
    return stamp.read_text().strip() == stamp_value


def _is_executable(path: pathlib.Path) -> bool:
    """True when path exists as a regular file with an execute bit set."""
    return path.is_file() and os.access(path, os.X_OK)


def _require_entry_points(venv_path: pathlib.Path) -> None:
    """Verify every console script the launcher depends on actually got built.

    pip can exit 0 without leaving a usable environment behind — a partial
    install, a build-backend quirk, filesystem lag. Checking here, before the
    version stamp is written, is what makes a broken install self-heal on the
    next launch instead of being marked current forever.

    Raises:
        LaunchError: MM-16 naming the first missing script and the venv path.
    """
    for name in REQUIRED_SCRIPTS:
        script = venv_path / "bin" / name
        if not _is_executable(script):
            raise LaunchError(
                "MM-16",
                f"pip reported success but {script} is missing or not "
                f"executable (venv={venv_path}).",
                f"Delete {state_dir() / 'venv'} and retry — it will rebuild on next launch.",
            )


def ensure_venv(
    root: pathlib.Path,
    venv_path: pathlib.Path,
    version: str,
    runner=subprocess.run,
    log=_log,
) -> pathlib.Path:
    """Build or refresh the state venv, then return its bin directory.

    Keying the stamp on plugin version means `claude plugin update` self-heals
    the venv on the next start — there is no separate dependency-update step.

    Args:
        root: plugin install directory (the package source).
        venv_path: where the venv lives.
        version: plugin version to stamp.
        runner: subprocess.run replacement, injected for tests.
        log: logging callable, injected for tests.

    Returns:
        Path to the venv's bin directory.

    Raises:
        LaunchError: MM-12 on venv creation failure, MM-13 on pip failure,
            MM-16 when pip exits 0 but a required console script is missing.
    """
    stamp_value = venv_stamp(root, version)
    if venv_is_current(venv_path, stamp_value):
        return venv_path / "bin"

    log(f"build start  stamp={stamp_value}  venv={venv_path}")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_path)
    except Exception as exc:  # surfaced with a stable code
        raise LaunchError(
            "MM-12",
            f"Could not create a virtualenv at {venv_path}: {exc}",
            "On Debian/Ubuntu install python3-venv, then retry.",
        )

    pip = venv_path / "bin" / "pip"
    try:
        # stdout/stderr are captured, not inherited: fd 1 is what os.execv
        # later hands the MCP server for JSON-RPC framing, and any pip
        # resolver or build-backend chatter landing on it would corrupt the
        # handshake. Captured text is folded into the log and the MM-13
        # message on failure so nothing is lost.
        runner(
            [str(pip), "install", "--quiet", "--cache-dir", str(PIP_CACHE), f"{root}[mcp,cli]"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
    except LaunchError:
        raise
    except Exception as exc:  # surfaced with a stable code
        output = getattr(exc, "output", None) or getattr(exc, "stdout", None) or ""
        if output:
            log(f"pip output:\n{output}")
        raise LaunchError(
            "MM-13",
            f"Dependency install failed: {exc}",
            f"See {state_dir() / 'launch.log'}, fix the pip error, then retry.",
        )

    # Verify the install actually produced usable entry points before
    # trusting it — see _require_entry_points.
    _require_entry_points(venv_path)

    # Stamp LAST. A stamp written before a successful, verified install would
    # make a broken venv look current forever.
    (venv_path / ".plugin-version").write_text(stamp_value + "\n")
    log(f"build ok     stamp={stamp_value}")
    return venv_path / "bin"


def main(argv: list[str] | None = None) -> None:
    """Ensure the venv, then exec either the MCP server or the CLI.

    Args:
        argv: argument list excluding the program name. `--cli` must be first
            when present; everything after it is forwarded to `megamaid`
            untouched, so megamaid's own flags (e.g. `--dry-run`) never reach
            this launcher's own argument handling.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    cli_mode = bool(args) and args[0] == "--cli"
    forwarded = args[1:] if cli_mode else []

    root = plugin_root()
    try:
        version = plugin_version(root)
        bin_dir = ensure_venv(root, state_dir() / "venv", version)
        target = bin_dir / ("megamaid" if cli_mode else "megamaid-mcp")
        if not _is_executable(target):
            # Defence in depth: ensure_venv already verified this before
            # stamping, but the file could have been removed since then.
            raise LaunchError(
                "MM-16",
                f"{target} is missing or not executable.",
                f"Delete {state_dir() / 'venv'} and retry — it will rebuild on next launch.",
            )
    except LaunchError as err:
        _log(f"FAILED [{err.code}] {err.message}")
        print(f"\n  ✗  [{err.code}] {err.message}\n\n     Fix: {err.fix}\n", file=sys.stderr)
        raise SystemExit(1)

    os.execv(str(target), [str(target), *forwarded])


if __name__ == "__main__":
    main()
