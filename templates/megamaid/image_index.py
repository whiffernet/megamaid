"""Persistent URL-to-content-hash index for cross-run image deduplication.

The index maps an image's source URL to the content hash, extension, and
HTTP validators of the copy already on disk, plus when it was last seen.
``download_images`` consults it before fetching: a fresh hit means the
image is already in the shared store and the network request is skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ImageRef


# An index entry seen within this many days is trusted without revalidation.
FRESHNESS_DAYS = 30

_INDEX_VERSION = 1


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IndexEntry:
    """A single URL's cached-image record.

    Attributes:
        content_hash: Full SHA-256 hex digest of the image bytes.
        ext: File extension including the leading dot (e.g. ".jpg").
        last_seen: ISO 8601 timestamp of when the URL was last fetched
            or revalidated.
        etag: Value of the response ETag header, if any.
        last_modified: Value of the response Last-Modified header, if any.
    """

    content_hash: str
    ext: str
    last_seen: str = field(default_factory=_now_iso)
    etag: str | None = None
    last_modified: str | None = None

    def is_fresh(self, now: datetime, window_days: int = FRESHNESS_DAYS) -> bool:
        """Report whether this entry is recent enough to trust as-is.

        Args:
            now: The current time (timezone-aware).
            window_days: Freshness window in days.

        Returns:
            True if ``last_seen`` is within ``window_days`` of ``now``.
        """
        seen = datetime.fromisoformat(self.last_seen)
        return now - seen <= timedelta(days=window_days)

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage."""
        return {
            "content_hash": self.content_hash,
            "ext": self.ext,
            "last_seen": self.last_seen,
            "etag": self.etag,
            "last_modified": self.last_modified,
        }

    @classmethod
    def from_dict(cls, data: dict) -> IndexEntry:
        """Deserialize from a stored dict, ignoring unknown keys."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ImageIndex:
    """A persistent map of image source URL to IndexEntry."""

    def __init__(self, entries: dict[str, IndexEntry] | None = None) -> None:
        """Initialize the index.

        Args:
            entries: Optional pre-populated URL-to-entry mapping.
        """
        self._entries: dict[str, IndexEntry] = entries or {}

    def __len__(self) -> int:
        """Return the number of indexed URLs."""
        return len(self._entries)

    def get(self, url: str) -> IndexEntry | None:
        """Return the entry for ``url``, or None if it is not indexed."""
        return self._entries.get(url)

    def put(
        self,
        url: str,
        content_hash: str,
        ext: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        seen_at: str | None = None,
    ) -> None:
        """Record (or overwrite) the entry for ``url``.

        Args:
            url: Image source URL.
            content_hash: Full SHA-256 hex digest of the image bytes.
            ext: File extension including the leading dot.
            etag: Response ETag header value, if any.
            last_modified: Response Last-Modified header value, if any.
            seen_at: ISO 8601 timestamp; defaults to now.
        """
        self._entries[url] = IndexEntry(
            content_hash=content_hash,
            ext=ext,
            last_seen=seen_at or _now_iso(),
            etag=etag,
            last_modified=last_modified,
        )

    def save(self, path: Path) -> None:
        """Atomically write the index to ``path`` as JSON (tmp + rename)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": _INDEX_VERSION,
            "entries": {u: e.to_dict() for u, e in self._entries.items()},
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)

    @classmethod
    def load(cls, path: Path) -> ImageIndex:
        """Load an index from ``path``, or return an empty one if absent.

        Args:
            path: Path to the index JSON file.

        Returns:
            The loaded ImageIndex (empty if the file does not exist).
        """
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        entries = {
            url: IndexEntry.from_dict(rec)
            for url, rec in data.get("entries", {}).items()
        }
        return cls(entries)


def cached_imageref(
    index: ImageIndex,
    url: str,
    store_dir: Path,
    now: datetime,
    *,
    alt_text: str = "",
    width: int | None = None,
    height: int | None = None,
) -> ImageRef | None:
    """Return an ImageRef for ``url`` if its image can be reused without fetching.

    A cache hit requires three things: the URL is indexed, the entry is
    fresh (within the freshness window), and the hashed file still exists
    in the shared store. Any miss returns None, so the caller falls back
    to a normal download.

    Args:
        index: The loaded image index.
        url: Image source URL.
        store_dir: The shared content-addressed image store directory.
        now: Current time (timezone-aware), for the freshness check.
        alt_text: Alt text from the current page's candidate.
        width: Image width from the current page's candidate.
        height: Image height from the current page's candidate.

    Returns:
        An ImageRef pointing at the stored file, or None on any miss.
    """
    entry = index.get(url)
    if entry is None or not entry.is_fresh(now):
        return None

    filename = f"{entry.content_hash[:16]}{entry.ext}"
    stored = Path(store_dir) / filename
    if not stored.exists():
        return None

    return ImageRef(
        source_url=url,
        local_path=str(stored.relative_to(Path(store_dir).parent)),
        content_hash=entry.content_hash,
        alt_text=alt_text,
        width=width,
        height=height,
    )
