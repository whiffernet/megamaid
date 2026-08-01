"""Response bodies recon reads are bounded, not just the gzip it decompresses.

The spec paired `safe_gunzip` with a cap on `resp.content`; only the
decompression half landed. recon read `resp.text` / `resp.content` whole, and
`megamaid_recon` runs inside the MCP server's own process — with the retired
container's `mem_limit: 512m` gone in the same branch, a multi-GB reply from
an attacker-controlled page was an in-process OOM. httpx's `timeout` bounds
idle time, not transfer size, and a `Content-Length` pre-check cannot help
because chunked responses omit the header.

Everything here is hermetic: httpx.MockTransport stands in for the network,
and the clients are built without the netguard request hook so no test
depends on DNS.
"""

import asyncio
import sys

import httpx
import pytest

sys.path.insert(0, "src")

from megamaid import recon  # noqa: E402
from megamaid.netguard import MAX_COMPRESSED_BYTES, NetGuardError  # noqa: E402

CHUNK = b"A" * 64_000


class _CountingStream(httpx.AsyncByteStream):
    """Yields `count` chunks, recording how many were actually pulled.

    The count is the point: it proves the cap trips *during* the transfer
    rather than after the whole body has already been materialised, which is
    the difference between a bounded read and a truncated one.
    """

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk = chunk
        self.count = count
        self.yielded = 0

    async def __aiter__(self):
        for _ in range(self.count):
            self.yielded += 1
            yield self.chunk


def _client(handler) -> httpx.AsyncClient:
    """A client with no request hook — these tests are about size, not SSRF."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_the_default_cap_is_the_shared_ceiling():
    """One knob for "the largest payload recon will hold"."""
    assert recon.MAX_RESPONSE_BYTES == MAX_COMPRESSED_BYTES


def test_a_body_over_the_cap_is_refused_mid_stream():
    """The regression: an oversized response raises MM-43 instead of being read.

    The stream offers 40 MB in 64 KB chunks against a 200 KB cap. Fewer than
    ten chunks may be pulled; anything approaching 640 would mean the body was
    being materialised first and checked second.
    """
    stream = _CountingStream(CHUNK, count=640)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async def go():
        async with _client(handler) as client:
            return await recon._get_capped(
                client, "https://example.test/big", timeout=5.0, limit=200_000
            )

    with pytest.raises(NetGuardError) as excinfo:
        asyncio.run(go())

    assert excinfo.value.code == "MM-43"
    assert stream.yielded <= 10, (
        f"pulled {stream.yielded} chunks ({stream.yielded * len(CHUNK)} bytes) for a "
        "200,000-byte cap — the body is being materialised before it is checked"
    )


def test_a_body_over_the_real_default_cap_is_refused():
    """Same property at the shipped default, so the wiring is proven too.

    Offers MAX_RESPONSE_BYTES + 1 chunk worth of data with no explicit limit
    argument.
    """
    over_by_one_chunk = (recon.MAX_RESPONSE_BYTES // len(CHUNK)) + 2
    stream = _CountingStream(CHUNK, count=over_by_one_chunk)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async def go():
        async with _client(handler) as client:
            return await recon._get_capped(client, "https://example.test/big", timeout=5.0)

    with pytest.raises(NetGuardError) as excinfo:
        asyncio.run(go())
    assert excinfo.value.code == "MM-43"


def test_a_lying_content_length_does_not_get_a_body_through():
    """A chunked response carries no Content-Length at all.

    A header pre-check is therefore not a substitute for counting bytes; this
    pins that the cap is enforced on what actually arrives.
    """
    stream = _CountingStream(CHUNK, count=640)

    def handler(request: httpx.Request) -> httpx.Response:
        # No content-length; httpx treats this as a chunked/streaming body.
        return httpx.Response(200, stream=stream, headers={"transfer-encoding": "chunked"})

    async def go():
        async with _client(handler) as client:
            return await recon._get_capped(
                client, "https://example.test/chunked", timeout=5.0, limit=100_000
            )

    with pytest.raises(NetGuardError) as excinfo:
        asyncio.run(go())
    assert excinfo.value.code == "MM-43"


def test_a_body_under_the_cap_is_returned_fully_readable():
    """The cap must not cost callers `.text`, `.headers`, `.status_code` or `.url`.

    probe_anti_bot reads all four off the response the fetch returns, so a
    streamed read that left `.content` unpopulated would break it.
    """
    body = b"<html>hello</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    async def go():
        async with _client(handler) as client:
            return await recon._get_capped(client, "https://example.test/page", timeout=5.0)

    resp = asyncio.run(go())
    assert resp.status_code == 200
    assert resp.content == body
    assert resp.text == body.decode()
    assert resp.headers["content-type"] == "text/html"
    assert str(resp.url) == "https://example.test/page"
    assert resp.history == []


def test_a_gzip_bomb_is_bounded_by_the_decoded_byte_count():
    """`aiter_bytes` yields decoded bytes, so Content-Encoding: gzip is covered.

    Complements safe_gunzip, which handles sitemaps served as .gz *files*;
    this is the transport-level encoding, which httpx decompresses for us and
    would otherwise expand unbounded before anything counted it.
    """
    import gzip

    bomb = gzip.compress(b"B" * 20_000_000)
    assert len(bomb) < 100_000, "sanity: the compressed bomb is small"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"})

    async def go():
        async with _client(handler) as client:
            return await recon._get_capped(
                client, "https://example.test/bomb", timeout=5.0, limit=500_000
            )

    with pytest.raises(NetGuardError) as excinfo:
        asyncio.run(go())
    assert excinfo.value.code == "MM-43"


def test_an_oversized_robots_txt_degrades_the_probe_instead_of_aborting_recon():
    """MM-43 must behave like any other fetch failure at the probe layer."""
    stream = _CountingStream(CHUNK, count=640)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async def go():
        async with _client(handler) as client:
            return await recon.probe_robots(client, "https://example.test")

    result = asyncio.run(go())
    assert result.status == "skip"
    assert "Could not fetch robots.txt" in result.summary


def test_no_recon_fetch_bypasses_the_cap(repo_root):
    """A new `await client.get(...)` here would silently reopen the hole."""
    source = (repo_root / "src" / "megamaid" / "recon.py").read_text()
    assert "client.get(" not in source, (
        "every recon fetch must go through _get_capped; a direct client.get() reads the whole body"
    )
    assert source.count("_get_capped(") >= 6, "the helper plus its five call sites"
