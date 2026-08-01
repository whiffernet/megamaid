"""SSRF and decompression guards.

These replace protections the retired container was providing incidentally:
its bridge network made host loopback unreachable, and its 512 MB memory cap
bounded an otherwise unbounded gzip.decompress.
"""

import gzip
import sys

import pytest

sys.path.insert(0, "src")

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
    ],
)
def test_private_and_loopback_hosts_are_rejected(host):
    assert is_private_host(host) is True


@pytest.mark.parametrize("host", ["example.com", "93.184.216.34", "8.8.8.8"])
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
