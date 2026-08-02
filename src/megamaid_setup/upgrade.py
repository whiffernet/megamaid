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
import collections
import enum
import hashlib
import os
import pathlib
import re
import shutil
import warnings
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

    Lookups are scoped to `path.name`: a match only counts against that same
    file's own history, never another runtime file's. A filename the manifest
    has never seen has no historical versions to match, so it falls straight
    through to DIVERGENT rather than crashing or matching by accident.

    Args:
        path: a file inside a project's vendored `megamaid/` directory.
        manifest: the historical record from `load_manifest()`.

    Returns:
        ABSENT if it does not exist, EXACT on a sha256 match, COSMETIC when only
        formatting or comments differ, DIVERGENT otherwise — including files that
        do not parse, and files with no history under this name at all, which
        are never overwritten silently.
    """
    if not path.is_file():
        return Tier.ABSENT

    known_hashes = manifest.hashes.get(path.name, frozenset())
    known_asts = manifest.asts.get(path.name, frozenset())

    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() in known_hashes:
        return Tier.EXACT

    try:
        # Parsing arbitrary user code emits SyntaxWarnings the user can do
        # nothing about and that are not this tool's finding — six of them
        # ("invalid escape sequence '\\d'", from regexes in non-raw strings)
        # printed ahead of the report on the real fleet. `filename` is passed
        # so that anything which does escape names the file it came from
        # rather than "<unknown>".
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(raw.decode(), filename=str(path))
        if ast.dump(tree) in known_asts:
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

#: The project-root file recording which runtime version the project is on.
#: A copy rides inside each backup so `rollback()` can put the old one back —
#: see `back_up()`.
VERSION_STAMP = ".megamaid-version"

#: Exactly the names `back_up()` produces: `YYYYMMDD-HHMMSS-<version>`.
#: `.megamaid-backups/` is a plain directory in the user's project, so anything
#: can be sitting in it — a note, an editor swapfile, a tarball parked there on
#: purpose, or a partial copytree left behind by a back_up() that died. Only a
#: directory matching this may be restored or pruned; everything else belongs
#: to the user. `back_up()` validates against the same pattern before writing,
#: so a backup that exists can always be found again.
#: `\Z`, not `$`: `$` also matches before a trailing newline, which would let a
#: directory named "20260101-000000-1.0\n" qualify for restore and for deletion
#: by the retention prune. Unknown entries are the user's, not ours.
_BACKUP_NAME = re.compile(r"^\d{8}-\d{6}-.+\Z")

#: Prefixes `rollback()` stages under, as siblings of `megamaid/`.
_SCRATCH_PREFIXES = (".megamaid-restoring.", ".megamaid-replaced.")


def sweep_scratch(project: pathlib.Path) -> list[pathlib.Path]:
    """Remove staging directories a previous rollback could not clean up.

    `rollback()` removes its own scratch on every path it can reach, but a
    process killed between staging and the swap — SIGKILL, a power cut — leaves
    one behind. A tool whose whole premise is not leaving a mess in someone's
    project should not quietly accumulate `.megamaid-restoring.<pid>`
    directories in it.

    Every scratch is swept, not only this process's: two rollbacks of the same
    project at once are already unsafe (both mutate `megamaid/`), so this
    introduces no hazard that concurrency did not already have.

    Args:
        project: the project directory.

    Returns:
        The directories removed, oldest name first.
    """
    removed = []
    for entry in sorted(project.glob(".megamaid-*")):
        if entry.name.startswith(_SCRATCH_PREFIXES) and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
    return removed


def parse_backup_name(name: str) -> tuple[str, str]:
    """Split a backup directory's basename back into (timestamp, version).

    Names are `<now>-<version>`, where `now` is the fixed-width
    `YYYYMMDD-HHMMSS` (15 characters) `back_up()` always produces. Splitting
    at that fixed offset — rather than on the first or last `-` — is what
    keeps this correct when `version` itself contains a `-` (a prerelease
    tag like `1.0.0-rc1`), which is also why `back_up()` puts the timestamp
    first in the name at all.

    This is presentation only (used to describe a backup in a report), never
    load-bearing for a read or write — deciding *which* entries are backups at
    all is `_is_backup()`'s job, against a stricter pattern than this one.

    Args:
        name: a backup directory's basename, as produced by `back_up()`.

    Returns:
        (timestamp, version). If `name` doesn't have a `-` at the expected
        fixed offset — a name from some other source — returns (name, "")
        rather than raising.
    """
    if len(name) > 16 and name[15] == "-":
        return name[:15], name[16:]
    return name, ""


def _is_backup(path: pathlib.Path) -> bool:
    """True only for an entry `back_up()` itself could have produced.

    Symlinks are excluded even when correctly named: following one would let
    a restore read from outside the project, and let the retention prune
    delete whatever it points at.
    """
    return bool(_BACKUP_NAME.match(path.name)) and path.is_dir() and not path.is_symlink()


def backups(project: pathlib.Path) -> list[pathlib.Path]:
    """Every real backup this project has, oldest first.

    The single place `.megamaid-backups/` is turned into a list of things that
    may be restored or deleted — shared by `latest_backup()` and `back_up()`'s
    retention prune so the two can never disagree about what counts.

    Ordering is a plain lexicographic sort of the directory names, which is
    chronological because `back_up()` names them `<now>-<version>` — timestamp
    first. See `back_up()`.

    Args:
        project: the project directory.

    Returns:
        Backup directories in chronological order; empty when there is no
        `.megamaid-backups/`, or nothing in it that this tool wrote.
    """
    root = project / BACKUP_DIR
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if _is_backup(p)), key=lambda p: p.name)


class BackupFailed(Exception):
    """`apply_plan` could not even finish making its safety copy.

    Distinct from any exception raised once the copy loop has started: by
    the time this can happen, `back_up()` has not returned, so nothing in
    `megamaid/` has been overwritten yet, and the backup itself may not
    exist or may be incomplete. A caller catching this specifically —
    rather than a bare exception from `apply_plan` — knows the correct
    answer is "nothing changed, fix the problem and retry", not "restore
    from the backup that was just written."
    """


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

    The project's `.megamaid-version` stamp is copied in alongside the runtime,
    because `apply_plan()` overwrites it and `rollback()` has to be able to put
    the old value back. It lives *inside* the backup directory rather than
    beside it so the retention prune carries it off with the backup it belongs
    to; `rollback()` restores it to the project root and keeps it out of
    `megamaid/`. A project with no stamp (one scaffolded before the stamping
    scheme) records nothing, and rolls back to having none.

    Args:
        project: the project directory.
        version: the runtime version being applied.
        now: a timestamp string, injected so tests are deterministic.

    Returns:
        The directory the copy was written to.

    Raises:
        ValueError: if `now` is not `YYYYMMDD-HHMMSS`. The name would not be
            recognised as a backup afterwards, so the copy would exist on disk
            and be impossible to restore — a silent failure, caught here at
            creation instead.
    """
    dest_name = f"{now}-{version}"
    if not _BACKUP_NAME.match(dest_name):
        raise ValueError(
            f"refusing to write a backup named {dest_name!r}: `now` must be YYYYMMDD-HHMMSS "
            "and `version` must be non-empty, or the backup could never be found again"
        )

    root = project / BACKUP_DIR
    root.mkdir(exist_ok=True)
    dest = root / dest_name
    shutil.copytree(project / "megamaid", dest, ignore=shutil.ignore_patterns("__pycache__"))
    stamp = project / VERSION_STAMP
    if stamp.is_file():
        shutil.copy2(stamp, dest / VERSION_STAMP)

    for stale in backups(project)[:-RETAIN]:
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
        BackupFailed: if `back_up()` itself raises — nothing in `megamaid/`
            has been touched yet when this happens.
    """
    if plan.error:
        raise ValueError(plan.error)

    sweep_scratch(plan.project)

    try:
        backup = back_up(plan.project, version, now)
    except (Exception, KeyboardInterrupt) as exc:
        # KeyboardInterrupt is caught here, not left to propagate, because the
        # caller's whole "was anything touched?" answer depends on which side
        # of back_up() the failure landed on. A Ctrl-C during the backup has
        # touched nothing in megamaid/ and must not be reported as the mixed
        # state an interrupt in the copy loop below leaves. The original is
        # kept as __cause__ so the caller can still tell an interrupt from a
        # full disk and exit accordingly.
        raise BackupFailed(str(exc) or type(exc).__name__) from exc

    for act in plan.actions:
        if act.action == "refuse":
            continue
        shutil.copy2(runtime / act.name, plan.project / "megamaid" / act.name)
    (plan.project / VERSION_STAMP).write_text(version + "\n")
    return backup


def latest_backup(project: pathlib.Path) -> pathlib.Path | None:
    """The most recent backup directory for a project, or None if there is none.

    Pure — only reads `.megamaid-backups/`'s directory listing, never writes
    anything. This is what `rollback()` would restore, without restoring it;
    shared by `rollback()` itself and by anything that only needs to preview
    what a rollback would do (e.g. `upgrade --dry-run --rollback`).

    Args:
        project: the project directory.

    Returns:
        The newest backup directory (see `backups()` for what counts as one),
        or None when there is nothing this tool wrote to restore.
    """
    found = backups(project)
    return found[-1] if found else None


def rollback(project: pathlib.Path) -> pathlib.Path:
    """Restore the most recent backup, atomically.

    The replacement runtime is built beside `megamaid/` first and swapped in
    with `os.replace`, rather than deleting `megamaid/` and copying over the
    hole it leaves. Delete-then-copy has no safe failure point: anything that
    goes wrong between the two — an unreadable backup, a full disk — leaves
    the project with no runtime at all, which is the one outcome rollback
    exists to prevent. Staged-then-swapped, a restore either happens or does
    not, and a failure leaves the project exactly as it was found.

    The project's `.megamaid-version` is reverted along with the runtime,
    from the copy `back_up()` saved inside the backup. A stamp naming a
    version the project is not running is silent and undetectable, and the
    stamp is the only record of drift the scheme has.

    Args:
        project: the project directory.

    Returns:
        The backup that was restored.

    Raises:
        RuntimeError: MM-36 when there is no backup to restore.
    """
    newest = latest_backup(project)
    if newest is None:
        raise RuntimeError(f"MM-36 no backup found in {project / BACKUP_DIR}")

    vendored = project / "megamaid"
    staged = project / f"{_SCRATCH_PREFIXES[0]}{os.getpid()}"
    retired = project / f"{_SCRATCH_PREFIXES[1]}{os.getpid()}"
    sweep_scratch(project)

    # Stage. Everything that can go wrong goes wrong here, with megamaid/
    # still untouched. VERSION_STAMP is excluded because it belongs at the
    # project root, not inside the runtime; __pycache__ because a backup
    # written before back_up() started excluding it may still carry one.
    try:
        shutil.copytree(newest, staged, ignore=shutil.ignore_patterns("__pycache__", VERSION_STAMP))
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    # Swap. Two renames on the same filesystem; megamaid/ is the old tree or
    # the restored one at every instant, never a partial mix of the two.
    try:
        if vendored.exists():
            os.replace(vendored, retired)
        os.replace(staged, vendored)
    except Exception:
        # Put the original back if it has already been moved aside. If even
        # that fails, `retired` stays on disk: it is then the only copy of the
        # project's previous runtime, and deleting it to tidy up would be the
        # exact loss this function exists to prevent. sweep_scratch() will not
        # touch it either, because the next run reaches it only after this one
        # has been dealt with by hand.
        if retired.is_dir() and not vendored.exists():
            os.replace(retired, vendored)
        shutil.rmtree(staged, ignore_errors=True)
        raise

    shutil.rmtree(retired, ignore_errors=True)

    saved_stamp = newest / VERSION_STAMP
    live_stamp = project / VERSION_STAMP
    if saved_stamp.is_file():
        shutil.copy2(saved_stamp, live_stamp)
    elif live_stamp.exists():
        live_stamp.unlink()
    return newest


def shared_variants(plans: list[ProjectPlan]) -> dict[tuple[str, str], list[str]]:
    """Group refused files by (filename, content hash) across projects.

    A variant appearing in several independent projects is not several hand
    edits — it is template work done in a project that never flowed upstream.
    Surfacing it turns N project decisions into one backport review.

    Args:
        plans: the planned projects.

    Returns:
        {(filename, short-hash): [project names]} for variants shared by 2+.
    """
    contents: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for plan in plans:
        for act in plan.refused:
            path = plan.project / "megamaid" / act.name
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
                contents[(act.name, digest)].append(plan.project.name)
    return {k: v for k, v in contents.items() if len(v) > 1}


def render(plans: list[ProjectPlan]) -> str:
    """Turn plans into the report a human reads.

    Refusals lead the report, ahead of everything upgrade actually did. The
    asymmetry that drives this whole design — overwriting a hand edit is
    unrecoverable in spirit, skipping a file only costs an explanation — means
    a refusal is the headline, never a number folded into a summary line.

    Args:
        plans: one `ProjectPlan` per project passed on the command line.

    Returns:
        The full text report, ready to print.
    """
    ok = [p for p in plans if p.converges and not p.error]
    dirty = [p for p in plans if p.refused]
    errored = [p for p in plans if p.error]

    lines = [f"  {len(plans)} project{'' if len(plans) == 1 else 's'}"]
    lines.append(f"  {len(ok)} converge{'s' if len(ok) == 1 else ''} cleanly")
    if dirty:
        verb = "has" if len(dirty) == 1 else "have"
        lines.append(f"  {len(dirty)} {verb} divergent files - upgraded around them")
    if errored:
        lines.append(f"  {len(errored)} could not be read")

    if errored:
        lines.append("")
        lines.append("  Could not be read")
        for plan in errored:
            lines.append(f"    {plan.project.name}: {plan.error}")

    # Refusals first, in full, with the file name and the project it belongs
    # to — never just a count. This is what a human has to act on.
    refusals = [(p, a) for p in plans for a in p.refused]
    if refusals:
        lines.append("")
        lines.append(f"  {len(refusals)} file(s) refused - left exactly as found   [MM-32]")
        for plan, act in refusals:
            lines.append(
                f"    {plan.project.name}: {act.name}  (diverges from every known release)"
            )
        lines.append("    -> review by hand: keep the edit, or replace it yourself if it should")
        lines.append("       have been the runtime file all along.")

    # What upgrade replaced, which the report used to leave out entirely.
    # EXACT overwrites lose nothing a human wrote, so they stay a count.
    # COSMETIC is the case where a person's own text does not survive — a
    # comment-only edit classifies COSMETIC and is overwritten — so those are
    # named, with the file, the project, and where the bytes went.
    replaced = [(p, a) for p in plans for a in p.actions if a.action == "overwrite"]
    cosmetic = [(p, a) for p, a in replaced if a.tier is Tier.COSMETIC]
    if replaced:
        lines.append("")
        lines.append(f"  {len(replaced)} file(s) replaced with the current runtime")
        if cosmetic:
            lines.append(f"    {len(cosmetic)} differed only in comments or formatting - that text")
            lines.append("    was replaced. The originals are in .megamaid-backups/:")
            for plan, act in cosmetic:
                lines.append(f"      {plan.project.name}: {act.name}")

    adds: collections.Counter = collections.Counter()
    for plan in plans:
        for act in plan.actions:
            if act.action == "add":
                adds[act.name] += 1
    if adds:
        lines.append("")
        lines.append("  Files added")
        for name, count in sorted(adds.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {name:<16} -> {count}")

    blocked = [(p, a) for p in plans for a in p.unreachable]
    if blocked:
        lines.append("")
        noun = "file" if len(blocked) == 1 else "files"
        lines.append(f"  {len(blocked)} added {noun} cannot be invoked   [MM-35]")
        for plan, act in blocked:
            entries = ", ".join(ENTRY_POINTS.get(act.name, ()))
            lines.append(f"    {plan.project.name}: {act.name} (needs {entries})")

    variants = shared_variants(plans)
    if variants:
        lines.append("")
        lines.append("  Shared variants - candidates to backport upstream")
        for (name, digest), projects in sorted(variants.items(), key=lambda kv: -len(kv[1])):
            lines.append(
                f"    {name:<14} {digest}  x{len(projects):<3} {' '.join(sorted(projects))}"
            )

    return "\n".join(lines)
