"""Network and decompression guards for recon.

megamaid fetches attacker-controlled pages, and `megamaid_recon` runs in the
MCP server's own process. Two protections that the retired Docker container
provided incidentally are re-implemented here explicitly:

  * its bridge network gave the container its own 127.0.0.1, so host loopback
    services were structurally unreachable  -> assert_public_url / guard_request
  * its 512 MB memory cap bounded gzip.decompress -> safe_gunzip

Stdlib only: this module is in the light [mcp] dependency closure.
"""

from __future__ import annotations

import gzip
import io
import ipaddress
import os
import socket
from urllib.parse import urlparse

# A compressed sitemap larger than this is refused before decompression is attempted.
MAX_COMPRESSED_BYTES = 8_000_000

ALLOW_PRIVATE_ENV = "MEGAMAID_ALLOW_PRIVATE_NETWORKS"


class NetGuardError(Exception):
    """A guard refused a request or a payload.

    Attributes:
        code: stable MM-xx code — MM-42 (blocked target), MM-43 (size cap).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _private_networks_allowed() -> bool:
    return os.environ.get(ALLOW_PRIVATE_ENV, "") not in ("", "0", "false", "False")


def is_private_host(host: str) -> bool:
    """True when host resolves to any address we refuse to fetch.

    Covers loopback, link-local (including the 169.254.169.254 cloud metadata
    endpoint), private ranges, unspecified, reserved and multicast, for both
    IPv4 and IPv6. A hostname is resolved first, and is considered private if
    ANY of its addresses is — a name resolving to both a public and a private
    address must not be treated as safe.

    Args:
        host: hostname or literal IP address.

    Returns:
        True when the host must not be fetched.
    """
    if not host:
        return True
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            candidates = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except socket.gaierror:
            # Unresolvable: refuse rather than hand it to the HTTP client.
            return True
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return True
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_unspecified
            or address.is_reserved
            or address.is_multicast
        ):
            return True
    return False


def assert_public_url(url: str) -> None:
    """Refuse URLs that target the local host or a private network.

    Args:
        url: absolute URL about to be fetched.

    Raises:
        NetGuardError: MM-42 when the scheme is not http(s) or the host is private.
    """
    if _private_networks_allowed():
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise NetGuardError("MM-42", f"Refusing non-HTTP(S) URL: {url}")
    if is_private_host(parsed.hostname or ""):
        raise NetGuardError(
            "MM-42",
            f"Refusing to fetch a private or loopback address: {url}. "
            f"Set {ALLOW_PRIVATE_ENV}=1 if you are deliberately scraping an intranet.",
        )


async def guard_request(request) -> None:  # httpx.Request, untyped to avoid the import
    """httpx request event hook. Fires once per request INCLUDING redirect hops.

    This is why the hook is used instead of a single pre-flight check: with
    follow_redirects=True, a public URL can redirect to 127.0.0.1, and only a
    per-hop check catches it.

    Must be async: httpx.AsyncClient awaits every "request" event hook
    (`await hook(request)`), so a plain sync function whose non-raising
    return value is `None` breaks every legitimate request with
    `TypeError: object NoneType can't be used in 'await' expression`.

    Raises:
        NetGuardError: MM-42 when this hop targets a blocked address.
    """
    assert_public_url(str(request.url))


def safe_gunzip(raw: bytes, limit: int) -> bytes:
    """Decompress at most `limit` bytes, refusing bombs.

    The previous code called gzip.decompress(raw) and truncated afterwards, so
    a small archive expanding to gigabytes was fully materialised in memory
    before anything was discarded.

    Args:
        raw: compressed bytes.
        limit: maximum number of decompressed bytes to produce.

    Returns:
        Up to `limit` decompressed bytes.

    Raises:
        NetGuardError: MM-43 when the compressed input is oversized, when the
            stream expands past `limit`, or when it is not valid gzip.
    """
    if len(raw) > MAX_COMPRESSED_BYTES:
        raise NetGuardError(
            "MM-43", f"Compressed payload is {len(raw)} bytes, over the {MAX_COMPRESSED_BYTES} cap"
        )
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            # Read one byte past the limit so overflow is detectable without
            # materialising the whole stream.
            out = stream.read(limit + 1)
    except Exception as exc:
        # Broad on purpose: this is a trust boundary on attacker-controlled
        # bytes. gzip.GzipFile.read() can raise gzip.BadGzipFile/OSError for
        # a bad header, EOFError for truncated input, or zlib.error for a
        # corrupt (but not oversized) DEFLATE stream — all must degrade to
        # the same typed refusal rather than crash the caller.
        raise NetGuardError("MM-43", f"Could not decompress payload: {exc}") from exc
    if len(out) > limit:
        raise NetGuardError("MM-43", f"Decompressed payload exceeds the {limit}-byte cap")
    return out
