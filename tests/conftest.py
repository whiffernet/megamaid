"""Shared pytest fixtures for the megamaid test suite."""

import pathlib

import pytest


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    """Absolute path to the repository root.

    Returns:
        Path to the directory containing pyproject.toml and .claude-plugin/.
    """
    return pathlib.Path(__file__).resolve().parent.parent
