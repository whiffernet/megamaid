"""Load the generated record of every historical runtime file.

Shipped as package data rather than computed at runtime: an installed plugin has
no git history to walk.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Manifest:
    """Every sha256 and ast.dump the runtime has ever had, keyed by filename.

    Keyed per filename basename rather than pooled into one flat set: a match
    only counts against that *same* file's own history. Without the key, an
    empty historical `__init__.py` would vouch for any other file reduced to
    a comment, silently overwriting a hand-edited `cli.py` or `base.py`.

    Attributes:
        hashes: filename -> sha256 of every historical version of that file.
        asts: filename -> ast.dump of the same, for cosmetic-only drift.
        generated_from: the commit the manifest was built at.
    """

    hashes: Mapping[str, frozenset[str]]
    asts: Mapping[str, frozenset[str]]
    generated_from: str


def load_manifest() -> Manifest:
    """Read known_hashes.json from package data.

    Returns:
        The parsed Manifest.

    Raises:
        FileNotFoundError: if the package was built without the manifest.
    """
    raw = resources.files("megamaid_setup").joinpath("known_hashes.json").read_text()
    data = json.loads(raw)
    return Manifest(
        hashes={name: frozenset(vals) for name, vals in data["hashes"].items()},
        asts={name: frozenset(vals) for name, vals in data["asts"].items()},
        generated_from=data["generated_from"],
    )
