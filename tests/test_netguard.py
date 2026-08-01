"""SSRF and decompression guards.

These replace protections the retired container was providing incidentally:
its bridge network made host loopback unreachable, and its 512 MB memory cap
bounded an otherwise unbounded gzip.decompress.
"""

import asyncio
import gzip
import sys

import httpx
import pytest

sys.path.insert(0, "src")

from megamaid import netguard  # noqa: E402
from megamaid.netguard import (  # noqa: E402
    NetGuardError,
    assert_public_url,
    is_private_host,
    safe_gunzip,
)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "localhost",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fd00::1",
        # RFC 6598 Shared Address Space ("CGNAT") — not flagged by any stdlib
        # is_* property (see netguard._EXTRA_PRIVATE_NETWORKS). Concretely,
        # this is Tailscale's default mesh address space.
        "100.64.0.1",
        "100.127.255.254",
        # IPv6 unique-local, RFC 4193 — the far end of fc00::/7.
        "fdff:ffff:ffff:ffff::1",
        # IPv4-mapped IPv6 wrapping a private/loopback/CGNAT address must
        # still be refused after being unwrapped to the plain v4 form.
        "::ffff:127.0.0.1",
        "::ffff:100.64.0.1",
    ],
)
def test_private_and_loopback_hosts_are_rejected(host):
    assert is_private_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "93.184.216.34",
        "8.8.8.8",
        # Just outside the CGNAT range on both ends.
        "100.63.255.255",
        "100.128.0.1",
        # A genuine public IPv6 address, to confirm fc00::/7 is not overbroad.
        "2001:4860:4860::8888",
        # IPv4-mapped IPv6 wrapping a public address. Every address in
        # ::ffff:0:0/96 has is_reserved unconditionally True in stdlib
        # (verified directly), so without unwrapping this would be a false
        # positive — a legitimate public target wrongly refused.
        "::ffff:93.184.216.34",
    ],
)
def test_public_hosts_are_allowed(host):
    assert is_private_host(host) is False


def test_assert_public_url_raises_mm42_for_loopback():
    with pytest.raises(NetGuardError) as excinfo:
        assert_public_url("http://127.0.0.1:8080/admin")
    assert excinfo.value.code == "MM-42"


def test_cloud_metadata_endpoint_is_rejected():
    """169.254.169.254 is the single most valuable SSRF target."""
    with pytest.raises(NetGuardError) as excinfo:
        assert_public_url("http://169.254.169.254/latest/meta-data/")
    assert excinfo.value.code == "MM-42"


def test_escape_hatch_allows_private_networks(monkeypatch):
    monkeypatch.setenv("MEGAMAID_ALLOW_PRIVATE_NETWORKS", "1")
    assert_public_url("http://192.168.1.10/")  # must not raise


def test_public_url_passes():
    assert_public_url("https://example.com/sitemap.xml")


def test_safe_gunzip_returns_small_payloads_intact():
    payload = b"<urlset></urlset>"
    assert safe_gunzip(gzip.compress(payload), limit=500_000) == payload


def test_safe_gunzip_refuses_a_bomb_without_materialising_it():
    """A 10 MB decompressed payload must not be fully materialised for a 1 KB cap."""
    bomb = gzip.compress(b"A" * 10_000_000)
    assert len(bomb) < 100_000, "sanity: the compressed bomb is small"
    with pytest.raises(NetGuardError) as excinfo:
        safe_gunzip(bomb, limit=1_000)
    assert excinfo.value.code == "MM-43"


def test_safe_gunzip_rejects_oversized_compressed_input():
    with pytest.raises(NetGuardError) as excinfo:
        safe_gunzip(b"\x1f\x8b" + b"\x00" * 20_000_000, limit=500_000)
    assert excinfo.value.code == "MM-43"


def test_recon_client_installs_the_request_guard(repo_root):
    """The guard must run per redirect hop, not just on the initial URL."""
    source = (repo_root / "src" / "megamaid" / "recon.py").read_text()
    assert "event_hooks" in source
    assert "guard_request" in source


def test_recon_no_longer_calls_unbounded_gzip_decompress(repo_root):
    source = (repo_root / "src" / "megamaid" / "recon.py").read_text()
    assert "gzip.decompress(" not in source
    assert "safe_gunzip(" in source


def test_guard_fires_on_the_redirect_hop_not_just_the_initial_url(monkeypatch):
    """The event hook must run again on the redirect target, not only on the
    URL the caller originally asked for.

    This exists because a pre-flight check on the initial URL cannot see
    where a redirect leads: a page that looks public can 302 to a private
    address, and only a check that re-runs on every hop catches that. Do NOT
    delete this as redundant with test_assert_public_url_raises_mm42_for_loopback
    — that test only proves the underlying check is correct in isolation, not
    that httpx is actually wired to call it again per hop.

    Hermetic: httpx.MockTransport stands in for the network (the transport
    must never be asked for the private redirect target — reaching it is a
    hard test failure), and is_private_host is monkeypatched to a fixed
    public/private mapping so nothing here depends on live DNS.
    """
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"Location": "http://internal.example/admin"})
        raise AssertionError(
            f"transport reached for {request.url} — the guard should have blocked "
            "this hop before it was ever dispatched"
        )

    def fake_is_private_host(host: str) -> bool:
        return {"public.example": False, "internal.example": True}[host]

    monkeypatch.setattr(netguard, "is_private_host", fake_is_private_host)

    async def make_request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [netguard.guard_request]},
        ) as client:
            return await client.get("http://public.example/")

    with pytest.raises(NetGuardError) as excinfo:
        asyncio.run(make_request())

    assert excinfo.value.code == "MM-42"
    assert seen_urls == ["http://public.example/"], (
        f"transport must be reached only for the public hop; saw {seen_urls}"
    )
