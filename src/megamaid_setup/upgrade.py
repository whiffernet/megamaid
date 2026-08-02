"""Converge a scaffolded project's vendored runtime onto the current one.

Every scraped project holds a *copy* of the runtime, frozen at the moment it was
scaffolded. This decides, per file, whether that copy is an untouched old release
(safe to replace) or something the user edited (leave it alone), and reports what
it did.

Planning is deliberately separate from applying: `plan_project` performs no
writes, so `--dry-run` is trustworthy and the classifier can be exercised against
real projects without risk.
"""

from __future__ import annotations

import ast
import enum
import hashlib
import pathlib
from dataclasses import dataclass, field

from .manifest import Manifest

#: Which runtime file dispatches which. A file whose only entry point is refused
#: lands on disk but cannot be invoked — reported, not silently shipped.
ENTRY_POINTS: dict[str, tuple[str, ...]] = {
    "recon.py": ("cli.py",),
    "image_index.py": ("cli.py", "images.py"),
}


class Tier(enum.Enum):
    """How a project's copy of a runtime file relates to the ones we shipped."""

    ABSENT = "absent"
    EXACT = "exact"
    COSMETIC = "cosmetic"
    DIVERGENT = "divergent"


@dataclass(frozen=True)
class FileAction:
    """What upgrade intends to do with one file."""

    name: str
    tier: Tier
    action: str  # "add" | "overwrite" | "refuse"
    reachable: bool = True


@dataclass
class ProjectPlan:
    """What upgrade intends to do with one project. Produced without writing."""

    project: pathlib.Path
    actions: list[FileAction] = field(default_factory=list)
    error: str | None = None

    @property
    def refused(self) -> list[FileAction]:
        return [a for a in self.actions if a.action == "refuse"]

    @property
    def unreachable(self) -> list[FileAction]:
        return [a for a in self.actions if not a.reachable]

    @property
    def converges(self) -> bool:
        """True when nothing in this project had to be refused."""
        return self.error is None and not self.refused


def classify(path: pathlib.Path, manifest: Manifest) -> Tier:
    """Decide whether this file may be replaced.

    Args:
        path: a file inside a project's vendored `megamaid/` directory.
        manifest: the historical record from `load_manifest()`.

    Returns:
        ABSENT if it does not exist, EXACT on a sha256 match, COSMETIC when only
        formatting or comments differ, DIVERGENT otherwise — including files that
        do not parse, which are never overwritten silently.
    """
    if not path.is_file():
        return Tier.ABSENT

    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() in manifest.hashes:
        return Tier.EXACT

    try:
        if ast.dump(ast.parse(raw.decode())) in manifest.asts:
            return Tier.COSMETIC
    except (SyntaxError, UnicodeDecodeError):
        return Tier.DIVERGENT

    return Tier.DIVERGENT


def plan_project(project: pathlib.Path, runtime: pathlib.Path, manifest: Manifest) -> ProjectPlan:
    """Decide what would change in one project. Writes nothing.

    Args:
        project: a scaffolded project directory (containing `megamaid/`).
        runtime: the current runtime source directory to converge onto.
        manifest: the historical record.

    Returns:
        A ProjectPlan. `error` is set (and `actions` empty) when the directory is
        not a megamaid project.
    """
    vendored = project / "megamaid"
    if not vendored.is_dir():
        return ProjectPlan(project=project, error="MM-31 not a megamaid project")

    plan = ProjectPlan(project=project)
    tiers: dict[str, Tier] = {}

    for source in sorted(runtime.glob("*.py")):
        tier = classify(vendored / source.name, manifest)
        tiers[source.name] = tier
        action = {
            Tier.ABSENT: "add",
            Tier.EXACT: "overwrite",
            Tier.COSMETIC: "overwrite",
            Tier.DIVERGENT: "refuse",
        }[tier]
        plan.actions.append(FileAction(name=source.name, tier=tier, action=action))

    # A file is unreachable when every entry point that dispatches it is refused.
    # Adding recon.py to a project whose cli.py we cannot touch ships a file the
    # user has no way to invoke; that is reported, not hidden.
    resolved: list[FileAction] = []
    for act in plan.actions:
        entries = ENTRY_POINTS.get(act.name, ())
        blocked = entries and all(tiers.get(e) is Tier.DIVERGENT for e in entries)
        resolved.append(
            FileAction(
                name=act.name,
                tier=act.tier,
                action=act.action,
                reachable=not (blocked and act.action == "add"),
            )
        )
    plan.actions = resolved
    return plan
