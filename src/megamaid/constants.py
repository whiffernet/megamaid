"""Dependency-free constants shared across the megamaid runtime.

This module exists so that light consumers — notably `recon`, which the MCP
server imports — can reach shared values without pulling in `base`, and
therefore without pulling in playwright. Keep it import-free: anything added
here lands in the MCP server's dependency closure.
"""

from __future__ import annotations

DEFAULT_USER_AGENT = (
    "megamaid/0.9 (+https://github.com/whiffernet/megamaid) Mozilla/5.0 (compatible; Chromium/131)"
)
