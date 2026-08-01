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

# Ranges the stdlib ipaddress classification flags do not cover for our purposes.
#
#   RFC 6598 Shared Address Space, 100.64.0.0/10 ("CGNAT"). The stdlib docs for
#   is_private/is_global spell out an explicit exception: "``is_private`` has
#   value opposite to ``is_global``, except for the ``100.64.0.0/10`` IPv4
#   range where they are [both False]." Confirmed empirically on this Python
#   (3.12.3): ipaddress.ip_address("100.64.0.1").is_private is False and
#   .is_global is also False, so neither flag this module already checks
#   catches it. This range matters concretely here: it is Tailscale's default
#   mesh address space, so treating it as public would let a page redirect
#   straight into a private overlay network.
_EXTRA_PRIVATE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),)


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


def _refuse_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when a single already-resolved address must be refused.

    Checks the stdlib classification flags plus the extra ranges those flags
    miss (see _EXTRA_PRIVATE_NETWORKS) — currently just CGNAT.

    Args:
        address: a resolved, concrete IPv4 or IPv6 address (already unwrapped
            if it was IPv4-mapped — see is_private_host).

    Returns:
        True when this exact address must not be fetched.
    """
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_unspecified
        or address.is_reserved
        or address.is_multicast
    ):
        return True
    return any(address in network for network in _EXTRA_PRIVATE_NETWORKS)


def is_private_host(host: str) -> bool:
    """True when host resolves to any address we refuse to fetch.

    Covers loopback, link-local (including the 169.254.169.254 cloud metadata
    endpoint), private ranges, unspecified, reserved, multicast, and CGNAT
    (100.64.0.0/10 — see _EXTRA_PRIVATE_NETWORKS), for both IPv4 and IPv6. A
    hostname is resolved first, and is considered private if ANY of its
    addresses is — a name resolving to both a public and a private address
    must not be treated as safe (DNS rebinding).

    IPv4-mapped IPv6 addresses (``::ffff:a.b.c.d``) are unwrapped and checked
    as the plain IPv4 address underneath. Empirically, on this Python
    (3.12.3) the *wrapped* form is unreliable in both directions: every
    address in ``::ffff:0:0/96`` has ``is_reserved`` unconditionally True —
    including ``::ffff:93.184.216.34``, a wrapped *public* address — while
    ``is_loopback``/``is_private`` are False for a wrapped loopback or CGNAT
    address. Checking the wrapped form directly would both over-block public
    targets and under-block private ones; unwrapping first fixes both.

    IPv6 unique-local addresses (``fc00::/7``, RFC 4193) need no special
    handling: stdlib's ``is_private`` already covers the full range (verified
    at both the ``fc00::`` and ``fdff:ffff:ffff:ffff::`` ends; a genuine
    public IPv6 address just outside it, e.g. ``2001:4860:4860::8888``, is
    correctly not private — ``fe00::``, though also outside fc00::/7, is a
    poor boundary probe because it lands in IANA's separate reserved
    ``fe00::/9`` block and trips ``is_reserved`` for an unrelated reason).

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
            # info[4] is the sockaddr tuple: (address, port) for IPv4,
            # (address, port, flowinfo, scope_id) for IPv6 — element 0 is the
            # address string in both forms. Typeshed types it str | int
            # (sockaddr is a generic tuple), so coerce explicitly; str() on an
            # already-str value is a no-op.
            candidates = [str(info[4][0]) for info in socket.getaddrinfo(host, None)]
        except socket.gaierror:
            # Unresolvable: refuse rather than hand it to the HTTP client.
            return True
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return True
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if _refuse_address(address):
            return True
    return False


def assert_public_url(url: str) -> None:
    """Refuse URLs that target the local host or a private network.

    Args:
        url: absolute URL about to be fetched.

    Raises:
        NetGuardError: MM-42 when the scheme is not http(s) or the host is private.
    """
    # Scheme first, and deliberately ABOVE the escape hatch. The env var means
    # "I am scraping an intranet", which is a statement about network reach,
    # not about protocols: file:// and ftp:// are refused either way. With the
    # order reversed, MEGAMAID_ALLOW_PRIVATE_NETWORKS=1 also silently turned
    # off scheme validation, so file:///etc/passwd sailed through a guard
    # nobody thought they had disabled.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise NetGuardError("MM-42", f"Refusing non-HTTP(S) URL: {url}")
    if _private_networks_allowed():
        return
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
