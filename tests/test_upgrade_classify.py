"""Tier logic: can this file be safely replaced?"""

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid_setup.manifest import Manifest  # noqa: E402
from megamaid_setup.upgrade import Tier, classify  # noqa: E402

KNOWN_SOURCE = "x = 1\n"
KNOWN_HASH = "9d4a8a4b6a2d1e1c9c1f0a8f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b"


def _manifest(tmp_path):
    """A manifest containing exactly one known file: KNOWN_SOURCE."""
    import ast
    import hashlib

    return Manifest(
        hashes=frozenset({hashlib.sha256(KNOWN_SOURCE.encode()).hexdigest()}),
        asts=frozenset({ast.dump(ast.parse(KNOWN_SOURCE))}),
        generated_from="deadbeef",
    )


def test_absent_file_is_absent(tmp_path):
    assert classify(tmp_path / "nope.py", _manifest(tmp_path)) is Tier.ABSENT


def test_byte_identical_is_exact(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(KNOWN_SOURCE)
    assert classify(f, _manifest(tmp_path)) is Tier.EXACT


def test_whitespace_only_change_is_cosmetic(tmp_path):
    """A stray trailing newline is the real-world case: 17 projects have one."""
    f = tmp_path / "a.py"
    f.write_text(KNOWN_SOURCE + "\n")
    assert classify(f, _manifest(tmp_path)) is Tier.COSMETIC


def test_comment_only_change_is_cosmetic(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("# a note\n" + KNOWN_SOURCE)
    assert classify(f, _manifest(tmp_path)) is Tier.COSMETIC


def test_real_edit_is_divergent(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 2\n")
    assert classify(f, _manifest(tmp_path)) is Tier.DIVERGENT


def test_unparseable_file_is_divergent(tmp_path):
    """Never silently overwrite something we cannot even parse."""
    f = tmp_path / "a.py"
    f.write_text("def broken(\n")
    assert classify(f, _manifest(tmp_path)) is Tier.DIVERGENT


def test_undecodable_bytes_are_divergent(tmp_path):
    """Not every syntax problem is a SyntaxError: a file saved in the wrong
    encoding raises UnicodeDecodeError from `raw.decode()`, a separate branch
    from ast.parse's SyntaxError. Both land on DIVERGENT, never a crash."""
    f = tmp_path / "a.py"
    f.write_bytes(b"x = '\xff\xfe not valid utf-8'\n")
    assert classify(f, _manifest(tmp_path)) is Tier.DIVERGENT
