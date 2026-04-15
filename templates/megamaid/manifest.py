"""Manifest tracking for scrape runs.

Each run produces a manifest.json that tracks:
- Per-item scrape status and identity hash
- Aggregate stats
- Delta against the previous run

The manifest is written incrementally so that a crash mid-run can be
resumed without re-scraping completed items.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ManifestItem:
    """Tracking record for a single scraped document."""

    id: str
    source_url: str
    doc_file: str = ""
    raw_file: str = ""
    identity_hash: str = ""
    scrape_status: str = "pending"  # pending, success, failed, skipped
    scrape_error: str = ""
    previous_identity_hash: str | None = None
    change_status: str = "unknown"  # unknown, new, changed, unchanged
    image_count: int = 0
    image_hashes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "id": self.id,
            "source_url": self.source_url,
            "doc_file": self.doc_file,
            "raw_file": self.raw_file,
            "identity_hash": self.identity_hash,
            "scrape_status": self.scrape_status,
            "scrape_error": self.scrape_error,
            "previous_identity_hash": self.previous_identity_hash,
            "change_status": self.change_status,
            "image_count": self.image_count,
            "image_hashes": self.image_hashes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestItem:
        """Deserialize from dict, ignoring unknown keys."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ManifestStats:
    """Aggregate statistics for a scrape run."""

    total: int = 0
    scraped_ok: int = 0
    scrape_failed: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0

    def to_dict(self) -> dict[str, int]:
        """Serialize to dict."""
        return {
            "total": self.total,
            "scraped_ok": self.scraped_ok,
            "scrape_failed": self.scrape_failed,
            "new": self.new,
            "changed": self.changed,
            "unchanged": self.unchanged,
        }


@dataclass
class Manifest:
    """Master tracking file for a single scrape run."""

    run_id: str
    target: str
    started_at: str = ""
    completed_at: str = ""
    status: str = "running"  # running, completed, failed, partial
    items: list[ManifestItem] = field(default_factory=list)
    stats: ManifestStats = field(default_factory=ManifestStats)

    def save(self, path: Path) -> None:
        """Atomically write manifest to disk (tmp + rename)."""
        data = {
            "run_id": self.run_id,
            "target": self.target,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "stats": self.stats.to_dict(),
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2))
        tmp_path.rename(path)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """Load manifest from disk.

        Args:
            path: Path to the manifest.json file.

        Returns:
            Loaded Manifest instance.

        Raises:
            FileNotFoundError: If manifest does not exist.
        """
        data = json.loads(path.read_text())
        manifest = cls(
            run_id=data["run_id"],
            target=data["target"],
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            status=data.get("status", "unknown"),
        )
        manifest.items = [
            ManifestItem.from_dict(item) for item in data.get("items", [])
        ]
        stats_data = data.get("stats", {})
        manifest.stats = ManifestStats(
            **{
                k: v
                for k, v in stats_data.items()
                if k in ManifestStats.__dataclass_fields__
            }
        )
        return manifest

    def recompute_stats(self) -> None:
        """Recompute aggregate stats from item-level data."""
        self.stats = ManifestStats(total=len(self.items))
        for item in self.items:
            if item.scrape_status == "success":
                self.stats.scraped_ok += 1
            elif item.scrape_status == "failed":
                self.stats.scrape_failed += 1
            if item.change_status == "new":
                self.stats.new += 1
            elif item.change_status == "changed":
                self.stats.changed += 1
            elif item.change_status == "unchanged":
                self.stats.unchanged += 1

    def find_item(self, id_: str) -> ManifestItem | None:
        """Look up an item by id."""
        for item in self.items:
            if item.id == id_:
                return item
        return None


def get_latest_manifest(staging_dir: Path, target: str) -> Manifest | None:
    """Find the most recent completed manifest for a target.

    Scans staging/{target}/ directories sorted by run_id (timestamp) and
    returns the latest manifest with status 'completed'.

    Args:
        staging_dir: Root staging directory.
        target: Target slug.

    Returns:
        The latest completed Manifest, or None if no prior runs exist.
    """
    target_dir = staging_dir / target
    if not target_dir.exists():
        return None
    run_dirs = sorted(target_dir.iterdir(), reverse=True)
    for run_dir in run_dirs:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = Manifest.load(manifest_path)
                if manifest.status == "completed":
                    return manifest
            except (json.JSONDecodeError, KeyError):
                continue
    return None


def compute_delta(
    current_items: list[ManifestItem],
    previous_manifest: Manifest | None,
) -> list[ManifestItem]:
    """Populate change_status and previous_identity_hash against a prior run.

    Args:
        current_items: Items from the current scrape run.
        previous_manifest: Previous run's manifest, or None for first run.

    Returns:
        The same items list with delta fields populated.
    """
    if previous_manifest is None:
        for item in current_items:
            item.previous_identity_hash = None
            item.change_status = "new"
        return current_items

    prev_lookup: dict[str, ManifestItem] = {p.id: p for p in previous_manifest.items}

    for item in current_items:
        prev = prev_lookup.get(item.id)
        if prev is None:
            item.previous_identity_hash = None
            item.change_status = "new"
        elif prev.identity_hash == item.identity_hash:
            item.previous_identity_hash = prev.identity_hash
            item.change_status = "unchanged"
        else:
            item.previous_identity_hash = prev.identity_hash
            item.change_status = "changed"

    return current_items
