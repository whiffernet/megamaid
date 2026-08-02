"""Tier logic: can this file be safely replaced?"""

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from megamaid_setup.manifest import Manifest  # noqa: E402
from megamaid_setup.upgrade import Tier, classify  # noqa: E402

KNOWN_SOURCE = "x = 1\n"


def _manifest(tmp_path):
    """A manifest whose only history is KNOWN_SOURCE, recorded under "a.py"."""
    import ast
    import hashlib

    return Manifest(
        hashes={"a.py": frozenset({hashlib.sha256(KNOWN_SOURCE.encode()).hexdigest()})},
        asts={"a.py": frozenset({ast.dump(ast.parse(KNOWN_SOURCE))})},
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


def test_matching_content_under_a_different_filename_is_divergent(tmp_path):
    """The manifest is keyed by filename. Byte- or AST-identical content
    recorded under a *different* file's history must not vouch for this one —
    otherwise a historically-empty __init__.py would excuse an edited cli.py
    that happens to reduce to the same bytes or the same empty AST."""
    f = tmp_path / "b.py"
    f.write_text(KNOWN_SOURCE)
    assert classify(f, _manifest(tmp_path)) is Tier.DIVERGENT


def test_unknown_filename_is_divergent_not_a_crash(tmp_path):
    """A filename the manifest has no history for at all must not raise —
    an empty lookup, not a KeyError, and it lands on DIVERGENT."""
    f = tmp_path / "never-seen.py"
    f.write_text("# anything\n")
    assert classify(f, _manifest(tmp_path)) is Tier.DIVERGENT


def test_classifying_a_file_with_a_regex_escape_emits_no_warning(tmp_path, recwarn):
    """Six `SyntaxWarning: invalid escape sequence '\\d'` lines printed ahead of
    the report on the real fleet, from project files with a regex in a non-raw
    string. They are not this tool's finding, the user cannot act on them, and
    they arrived attributed to "<unknown>" because ast.parse was called without
    a filename. Report output is the product here; noise in it is a defect."""
    f = tmp_path / "a.py"
    f.write_text('import re\nPAT = re.compile("\\d+")\n')

    classify(f, _manifest(tmp_path))

    offenders = [w for w in recwarn if issubclass(w.category, SyntaxWarning)]
    assert not offenders, (
        f"SyntaxWarning leaked into the report: {[str(w.message) for w in offenders]}"
    )


def test_a_file_that_does_not_parse_is_still_divergent(tmp_path):
    """Silencing warnings must not silence the SyntaxError that makes an
    unparseable file refuse rather than get overwritten."""
    f = tmp_path / "a.py"
    f.write_text("def broken( :\n")
    assert classify(f, _manifest(tmp_path)) is Tier.DIVERGENT
