"""Load the generated record of every historical runtime file.

Shipped as package data rather than computed at runtime: an installed plugin has
no git history to walk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class Manifest:
    """Every sha256 and ast.dump the runtime has ever had.

    Attributes:
        hashes: sha256 of every historical version of every runtime module.
        asts: ast.dump of the same, for detecting cosmetic-only drift.
        generated_from: the commit the manifest was built at.
    """

    hashes: frozenset[str]
    asts: frozenset[str]
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
        hashes=frozenset(data["hashes"]),
        asts=frozenset(data["asts"]),
        generated_from=data["generated_from"],
    )
