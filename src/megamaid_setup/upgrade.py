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
import shutil
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


#: Sibling of megamaid/, never inside it — see back_up().
BACKUP_DIR = ".megamaid-backups"

#: How many previous runtimes to keep per project.
RETAIN = 3


def back_up(project: pathlib.Path, version: str, now: str) -> pathlib.Path:
    """Copy the project's vendored runtime aside, then prune old backups.

    The destination is `<project>/.megamaid-backups/<now>-<version>/`, a sibling
    of `megamaid/`. Writing it inside `megamaid/` would mean the next backup
    swept up the previous one, and rollback would delete the directory it was
    about to read from.

    The timestamp comes first in the directory name, not the version, so that
    plain lexicographic sort — used both here and in `rollback()` — is also
    chronological order. `now` is a fixed-width `YYYYMMDD-HHMMSS` string;
    `version` is not fixed-width and does not sort chronologically (`"0.10.0"`
    sorts before `"0.9.1"` as a string), and may itself contain `-`
    (prerelease tags like `1.0.0-rc1`), so it cannot be the sort key.

    Args:
        project: the project directory.
        version: the runtime version being applied.
        now: a timestamp string, injected so tests are deterministic.

    Returns:
        The directory the copy was written to.
    """
    root = project / BACKUP_DIR
    root.mkdir(exist_ok=True)
    dest = root / f"{now}-{version}"
    shutil.copytree(project / "megamaid", dest, ignore=shutil.ignore_patterns("__pycache__"))

    for stale in sorted(root.iterdir())[:-RETAIN]:
        shutil.rmtree(stale)
    return dest


def apply_plan(plan: ProjectPlan, runtime: pathlib.Path, version: str, now: str) -> pathlib.Path:
    """Back up, then carry out a plan's adds and overwrites.

    Refused files are not touched. `targets/`, `staging/`, `manifest.json`,
    `.venv/` and `pyproject.toml` are never opened.

    Args:
        plan: from `plan_project`.
        runtime: the current runtime source directory.
        version: the runtime version being applied.
        now: timestamp string, injected for determinism.

    Returns:
        The backup directory.

    Raises:
        ValueError: if the plan carries an error.
    """
    if plan.error:
        raise ValueError(plan.error)

    backup = back_up(plan.project, version, now)
    for act in plan.actions:
        if act.action == "refuse":
            continue
        shutil.copy2(runtime / act.name, plan.project / "megamaid" / act.name)
    (plan.project / ".megamaid-version").write_text(version + "\n")
    return backup


def rollback(project: pathlib.Path) -> pathlib.Path:
    """Restore the most recent backup.

    "Most recent" is determined by lexicographic sort of the backup directory
    names, which is chronological because `back_up()` names them
    `<now>-<version>` — timestamp first. See `back_up()`.

    Args:
        project: the project directory.

    Returns:
        The backup that was restored.

    Raises:
        RuntimeError: MM-36 when there is no backup to restore.
    """
    root = project / BACKUP_DIR
    backups = sorted(root.iterdir()) if root.is_dir() else []
    if not backups:
        raise RuntimeError(f"MM-36 no backup found in {root}")

    newest = backups[-1]
    shutil.rmtree(project / "megamaid")
    shutil.copytree(newest, project / "megamaid")
    return newest
