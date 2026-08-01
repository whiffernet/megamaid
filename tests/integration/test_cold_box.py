"""Lane 3 — cold-box integration.

Everything the current install got wrong was a never-tested-at-all bug. This
lane runs the bootstrap on a machine with nothing preseeded and asserts the
three properties that matter: it connects, it connects fast enough, and it did
not drag playwright in.

Skipped unless MEGAMAID_COLD_BOX=1, because it builds a real venv.
"""

import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MEGAMAID_COLD_BOX") != "1",
    reason="set MEGAMAID_COLD_BOX=1 to run the cold-box lane",
)

# MCP_TIMEOUT defaults to 30_000 ms and is a hard connect deadline. Gate well below it.
CONNECT_BUDGET_SECONDS = 20.0

# Generous ceiling for the handshake to complete, covering a cold pip install;
# unrelated to CONNECT_BUDGET_SECONDS, which is the gate the test asserts on.
RECEIVE_TIMEOUT_SECONDS = 100.0
SHUTDOWN_GRACE_SECONDS = 10.0

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "cold-box", "version": "1.0"},
    },
}
# Required by the MCP protocol before a server will answer any further
# request. Omitting it isn't a shortcut — the real megamaid_mcp server (via
# the stdio transport in the `mcp` SDK) simply never answers tools/list
# without it, so a handshake missing this notification is not a real one.
INITIALIZED_NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
# megamaid_status needs no network and no scaffolded project — a lookup
# against a name that does not exist under PROJECTS_DIR still exercises a
# full tool call (and, critically, the mcp SDK's own request-dispatch log
# line) without depending on any fixture state.
CALL_STATUS_TOOL = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "megamaid_status", "arguments": {"project": "does-not-exist"}},
}

EXPECTED_TOOLS = {"megamaid_recon", "megamaid_run", "megamaid_status", "megamaid_list_docs"}


@pytest.fixture
def cold_state():
    """A state directory with nothing in it, torn down afterwards."""
    path = pathlib.Path(tempfile.mkdtemp(prefix="mm-coldbox-"))
    shutil.rmtree(path)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _handshake(repo_root, state, extra_args=(), extra_requests=()):
    """Run the launcher and drive a real MCP initialize + tools/list exchange.

    Claude Code holds the child's stdin open for the life of the session; it
    never writes a request and immediately closes the pipe. A naive
    `subprocess.run(input=...)` does exactly that, and it races the server's
    async request handling — the read loop sees EOF and the process tears
    itself down before the in-flight tools/list response is flushed, which
    would make this test fail even when the bootstrap and server are both
    completely healthy. So this drives the pipe the way a real client does:
    write the full handshake, wait for both JSON-RPC responses to actually
    appear, and only then close stdin.

    Args:
        extra_requests: additional JSON-RPC requests (each with a unique
            "id") sent after tools/list, in order. The handshake waits for a
            response to every one of them before closing stdin, exactly as
            it already does for initialize and tools/list.
    """
    env = {**os.environ, "MEGAMAID_STATE_DIR": str(state)}
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(repo_root / "scripts" / "launch.py"), *extra_args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    stdout_lines = []
    out_queue = queue.Queue()

    def _pump_stdout():
        for line in proc.stdout:
            out_queue.put(line)

    threading.Thread(target=_pump_stdout, daemon=True).start()

    requests = [TOOLS_LIST, *extra_requests]
    proc.stdin.write(json.dumps(INITIALIZE) + "\n")
    proc.stdin.write(json.dumps(INITIALIZED_NOTIFICATION) + "\n")
    for request in requests:
        proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()

    expected_ids = {INITIALIZE["id"], *(request["id"] for request in requests)}
    seen_ids = set()
    deadline = time.monotonic() + RECEIVE_TIMEOUT_SECONDS
    while time.monotonic() < deadline and seen_ids != expected_ids:
        try:
            line = out_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        stdout_lines.append(line)
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # a stray log line sharing stdout with the JSON-RPC framing
        if isinstance(message, dict) and message.get("id") in expected_ids:
            seen_ids.add(message["id"])
    elapsed = time.monotonic() - started

    proc.stdin.close()
    try:
        proc.wait(timeout=SHUTDOWN_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    while True:  # drain anything still buffered after shutdown
        try:
            stdout_lines.append(out_queue.get_nowait())
        except queue.Empty:
            break

    result = subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout="".join(stdout_lines),
        stderr=proc.stderr.read(),
    )
    return result, elapsed


def assert_within_budget(elapsed: float) -> None:
    """The lane's timing gate, extracted so it can itself be tested.

    Raises:
        AssertionError: when elapsed meets or exceeds the connect budget.
    """
    if elapsed >= CONNECT_BUDGET_SECONDS:
        raise AssertionError(
            f"cold start took {elapsed:.1f}s; MCP_TIMEOUT kills at 30s, gate is "
            f"{CONNECT_BUDGET_SECONDS}s"
        )


def test_cold_start_connects_and_lists_four_tools(repo_root, cold_state):
    proc, elapsed = _handshake(repo_root, cold_state)
    assert "serverInfo" in proc.stdout, f"no initialize response.\nstderr:\n{proc.stderr}"
    found = {name for name in EXPECTED_TOOLS if name in proc.stdout}
    assert found == EXPECTED_TOOLS, f"missing tools: {EXPECTED_TOOLS - found}"
    assert_within_budget(elapsed)


def test_mcp_venv_excludes_playwright(repo_root, cold_state):
    """The regression that would silently restore a ~3 minute start."""
    _handshake(repo_root, cold_state)
    pip = cold_state / "venv" / "bin" / "pip"
    installed = subprocess.run(
        [str(pip), "list", "--format=json"], capture_output=True, text=True, check=True
    )
    names = {pkg["name"].lower() for pkg in json.loads(installed.stdout)}
    assert "playwright" not in names
    assert "trafilatura" not in names


def test_second_start_reuses_the_venv(repo_root, cold_state):
    """The stamp must make the warm path fast — no rebuild on every launch."""
    _handshake(repo_root, cold_state)
    _, warm = _handshake(repo_root, cold_state)
    assert warm < 10.0, f"warm start took {warm:.1f}s; the version stamp is not being honoured"


def test_the_timing_gate_rejects_an_over_budget_start():
    """Negative control for the gate itself.

    Without this, `assert_within_budget` could be silently broken (an inverted
    comparison, a budget of infinity) and the lane would pass forever while
    certifying builds that Claude Code kills on connect. Asserting on a
    synthetic measurement tests the gate's logic without burning 30s of CI.
    """
    assert_within_budget(CONNECT_BUDGET_SECONDS - 0.1)  # under budget: must not raise

    with pytest.raises(AssertionError, match="MCP_TIMEOUT kills at 30s"):
        assert_within_budget(CONNECT_BUDGET_SECONDS + 0.1)

    with pytest.raises(AssertionError):
        assert_within_budget(31.0)  # past the real platform ceiling


def _is_json_rpc_envelope(text: str) -> bool:
    """True when `text` is a JSON-RPC 2.0 request, response, or notification.

    Deliberately stricter than "parses as JSON": it must be an object,
    carry `"jsonrpc": "2.0"`, and carry at least one of id/method/result/
    error. Plain "valid JSON" is not a strong enough test here — see
    `_is_protocol_frame`'s docstring for why that distinction matters.
    """
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(message, dict):
        return False
    if message.get("jsonrpc") != "2.0":
        return False
    return any(key in message for key in ("id", "method", "result", "error"))


def _is_protocol_frame(line: str) -> bool:
    """True when a stdout line is legitimate MCP wire traffic.

    A stdio MCP server's stdout IS the transport: Claude Code reads it as
    newline-delimited JSON-RPC envelopes (or, on transports that use it,
    SSE-style `data:`/`event:`/`id:` framing carrying the same envelopes)
    and nothing else.

    "Parses as JSON" is not a strong enough test: megamaid's own tool
    handlers log via `logger.info(json.dumps({...}))` (see megamaid_recon
    and megamaid_run in megamaid_mcp/server.py). A JSON *log line* parses
    cleanly too, so a bare `json.loads` check would wave through exactly
    the class of stdout corruption this test exists to catch — it would
    just never have caught anything but a bare-text log line, and gone
    blind the moment a regression routed one of those JSON-shaped log
    calls to stdout instead. So this checks the JSON-RPC envelope shape
    specifically (`_is_json_rpc_envelope`), not mere JSON-ness. A `data:`
    line's payload is held to the same envelope rule; `event:`/`id:` are
    SSE structural fields, not envelopes themselves, so their prefix alone
    is accepted.
    """
    if not line.strip():
        return True  # blank lines carry no data; not corruption
    if line.startswith("data:"):
        return _is_json_rpc_envelope(line[len("data:") :].strip())
    if line.startswith(("event:", "id:")):
        return True
    return _is_json_rpc_envelope(line)


def test_stdout_carries_only_protocol_frames_during_a_tool_call(repo_root, cold_state):
    """A stdio MCP server's stdout is the wire, not a log.

    This test exists because a library log line was once found there:
    `logging.basicConfig(..., stream=sys.stdout)` in megamaid_mcp/server.py
    configured the ROOT logger, and Python loggers propagate to root by
    default — so a *third-party* dependency's own `logger.info(...)` call
    (the mcp SDK's request dispatcher logs "Processing request of type
    CallToolRequest" for every tool call) rode along on the same fd Claude
    Code parses as JSON-RPC. Under the retired HTTP transport that would
    have been harmless; on stdio-only it silently corrupts the session the
    moment anything logs. A tool call is required (not just tools/list) to
    reach the dispatch path where that log line was actually observed.
    """
    proc, _ = _handshake(repo_root, cold_state, extra_requests=[CALL_STATUS_TOOL])
    lines = proc.stdout.splitlines()
    offending = [line for line in lines if not _is_protocol_frame(line)]
    assert not offending, "non-protocol data on stdout (the MCP wire):\n" + "\n".join(
        repr(line) for line in offending
    )
