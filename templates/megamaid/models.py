"""Generic document model for scraped content.

A ScrapedDoc is the unit of output: one logical item from the target
(a product, article, PDF, page, whatever). Fields are deliberately
minimal — put anything target-specific into `metadata`.
"""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import BaseModel, Field


class ImageRef(BaseModel):
    """Reference to a downloaded image.

    Attributes:
        source_url: Original URL the image was fetched from.
        local_path: Relative path within the run directory (e.g. images/abc123.jpg).
        content_hash: SHA-256 of the file bytes (full hex digest).
        alt_text: Alt attribute from the img tag, if available.
        width: Image width in pixels (from DOM or srcset descriptor).
        height: Image height in pixels (from DOM).
        role: Target-specific label (e.g. "product", "hero", "thumbnail").
    """

    source_url: str
    local_path: str = ""
    content_hash: str = ""
    alt_text: str = ""
    width: int | None = None
    height: int | None = None
    role: str = ""


class ScrapedDoc(BaseModel):
    """One scraped document.

    Attributes:
        id: Stable slug derived from source_url (used as filename).
        source_url: Canonical URL the document came from.
        title: Human-readable title.
        content_md: Normalized Markdown body (what the user usually wants).
        raw_path: Path to the raw HTML/JSON file saved alongside.
        identity_hash: SHA-256 of (title + content_md + sorted metadata).
            Used to detect changes between runs. Images are deliberately
            excluded — CDN URLs change constantly.
        metadata: Arbitrary target-specific fields (prices, authors, tags, etc.).
        images: Downloaded image references. Not included in identity_hash.
    """

    id: str
    source_url: str
    title: str
    content_md: str = ""
    raw_path: str = ""
    identity_hash: str = ""
    metadata: dict = Field(default_factory=dict)
    images: list[ImageRef] = Field(default_factory=list)

    def compute_identity_hash(self) -> str:
        """Compute and store a stable hash of the document's content.

        Returns:
            The hex-encoded SHA-256 digest. Also assigned to self.identity_hash.
        """
        payload = {
            "title": self.title,
            "content_md": self.content_md,
            "metadata": self.metadata,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        self.identity_hash = hashlib.sha256(encoded).hexdigest()
        return self.identity_hash


def slug_from_url(url: str) -> str:
    """Derive a safe filesystem slug from a URL.

    Args:
        url: The source URL.

    Returns:
        A lowercased slug with only [a-z0-9-_] characters. Truncated to 120 chars.
    """
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"[^a-zA-Z0-9]+", "-", url).strip("-").lower()
    return url[:120] or "doc"
