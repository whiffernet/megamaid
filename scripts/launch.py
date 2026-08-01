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

import argparse
import json
import os
import pathlib
import subprocess
import sys
import venv
from datetime import datetime, timezone

PIP_CACHE = pathlib.Path.home() / ".cache" / "megamaid-pip"


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
        return pathlib.Path(env)
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


def venv_is_current(venv_path: pathlib.Path, version: str) -> bool:
    """True when the venv exists and was built for this plugin version."""
    stamp = venv_path / ".plugin-version"
    if not (venv_path / "bin").is_dir() or not stamp.is_file():
        return False
    return stamp.read_text().strip() == version


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
        LaunchError: MM-12 on venv creation failure, MM-13 on pip failure.
    """
    if venv_is_current(venv_path, version):
        return venv_path / "bin"

    log(f"build start  version={version}  venv={venv_path}")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_path)
    except Exception as exc:  # noqa: BLE001 - surfaced with a stable code
        raise LaunchError(
            "MM-12",
            f"Could not create a virtualenv at {venv_path}: {exc}",
            "On Debian/Ubuntu install python3-venv, then retry.",
        )

    pip = venv_path / "bin" / "pip"
    try:
        runner(
            [str(pip), "install", "--quiet", "--cache-dir", str(PIP_CACHE), f"{root}[mcp,cli]"],
            check=True,
        )
    except LaunchError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced with a stable code
        raise LaunchError(
            "MM-13",
            f"Dependency install failed: {exc}",
            f"See {state_dir() / 'launch.log'}, fix the pip error, then retry.",
        )

    # Stamp LAST. A stamp written before a successful install would make a
    # broken venv look current forever.
    (venv_path / ".plugin-version").write_text(version + "\n")
    log(f"build ok     version={version}")
    return venv_path / "bin"


def main(argv: list[str] | None = None) -> None:
    """Ensure the venv, then exec the MCP server."""
    parser = argparse.ArgumentParser(prog="megamaid-launch", add_help=False)
    parser.parse_known_args(argv)

    root = plugin_root()
    try:
        version = plugin_version(root)
        bin_dir = ensure_venv(root, state_dir() / "venv", version)
    except LaunchError as err:
        _log(f"FAILED [{err.code}] {err.message}")
        print(f"\n  ✗  [{err.code}] {err.message}\n\n     Fix: {err.fix}\n", file=sys.stderr)
        raise SystemExit(1)

    target = bin_dir / "megamaid-mcp"
    os.execv(str(target), [str(target)])


if __name__ == "__main__":
    main()
