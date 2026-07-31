"""Local streamable-HTTP bridge for Cora's MCP surface (scripts/run_mcp_server_http.py).

Extends D-092 (see src/cora/mcp_server.py + tests/test_mcp_server.py). Kickoff of
record: `_notes/2026-07-30_fndr_cora-code-prompt-mcp-http-bridge.md` (Harrison
"locked as recommended", 2026-07-30). Covers the D-051-light NETWORK-SURFACE lens:

  * the bind host is a hard-coded loopback literal — no code path (env, config,
    or otherwise) can ever produce a non-loopback bind,
  * a configured bearer token is enforced (401, no Authorization detail leaked)
    and no check runs when unset,
  * the SAME tool surface as the stdio lane is served (all 6 tools, including
    the sole write tool cora_code_queue_seed) — no new tool, no behavior change,
  * the stdio entrypoint (scripts/run_mcp_server.py / mcp_server.build_server)
    stays regression-free.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("uvicorn")

import run_mcp_server_http as http_bridge  # noqa: E402


# ── A. Bind is loopback, hard-coded — structural (no server start needed) ────
def test_bind_host_is_loopback_literal():
    assert http_bridge._BIND_HOST == "127.0.0.1"


def test_bind_host_never_derived_from_environ():
    """The bind host must be a bare literal, never read from any config source.
    Grep the source: os.environ may appear (for the PORT / TOKEN), but never on
    the same statement that assigns _BIND_HOST, and uvicorn.run must pass the
    module-level constant, not a computed value."""
    src = Path(http_bridge.__file__).read_text(encoding="utf-8")
    assert '_BIND_HOST = "127.0.0.1"' in src
    # No other assignment to _BIND_HOST exists anywhere in the file.
    assert src.count("_BIND_HOST = ") == 1
    # uvicorn.run is called with the literal constant name, not a re-derived value.
    assert "uvicorn.run(app, host=_BIND_HOST" in src


def test_default_port_does_not_collide_with_health_endpoint():
    assert http_bridge._DEFAULT_PORT == 8791
    assert http_bridge._DEFAULT_PORT != 8787  # Cora's bot health endpoint


# ── B. Token enforcement (constant-time; no detail leaked) ───────────────────
def _make_client(monkeypatch, token: str | None):
    from starlette.testclient import TestClient

    if token is None:
        monkeypatch.delenv("CORA_MCP_HTTP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CORA_MCP_HTTP_TOKEN", token)
    app = http_bridge.build_app()
    return TestClient(app)


_LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_no_token_configured_allows_any_request(monkeypatch):
    with _make_client(monkeypatch, None) as client:
        r = client.post("/mcp", json=_LIST_TOOLS, headers=_HEADERS)
        assert r.status_code == 200


def test_token_configured_rejects_missing_header(monkeypatch):
    with _make_client(monkeypatch, "sekret") as client:
        r = client.post("/mcp", json=_LIST_TOOLS, headers=_HEADERS)
        assert r.status_code == 401
        assert "sekret" not in r.text


def test_token_configured_rejects_wrong_token(monkeypatch):
    with _make_client(monkeypatch, "sekret") as client:
        r = client.post("/mcp", json=_LIST_TOOLS,
                         headers={**_HEADERS, "Authorization": "Bearer wrong"})
        assert r.status_code == 401


def test_token_configured_accepts_correct_token(monkeypatch):
    with _make_client(monkeypatch, "sekret") as client:
        r = client.post("/mcp", json=_LIST_TOOLS,
                         headers={**_HEADERS, "Authorization": "Bearer sekret"})
        assert r.status_code == 200


def test_extract_bearer_handles_missing_and_malformed_headers():
    assert http_bridge._extract_bearer({"headers": []}) == ""
    assert http_bridge._extract_bearer({"headers": [(b"authorization", b"Basic xyz")]}) == ""
    assert http_bridge._extract_bearer(
        {"headers": [(b"authorization", b"Bearer abc123")]}
    ) == "abc123"


def test_token_check_uses_constant_time_compare():
    src = Path(http_bridge.__file__).read_text(encoding="utf-8")
    assert "hmac.compare_digest" in src
    assert "provided == token" not in src
    assert "token == provided" not in src


# ── C. Surface parity with the stdio lane — all 6 tools, nothing new ────────
def test_http_lists_same_six_tools_as_stdio(monkeypatch):
    import cora.mcp_server as mcp_server

    stdio_names = {s["name"] for s in mcp_server._TOOL_SPECS}
    assert len(stdio_names) == 6

    with _make_client(monkeypatch, None) as client:
        r = client.post("/mcp", json=_LIST_TOOLS, headers=_HEADERS)
    assert r.status_code == 200
    # SSE-framed body: "data: {...}" line carries the JSON-RPC payload.
    body = r.text
    data_line = next(line for line in body.splitlines() if line.startswith("data:"))
    import json
    payload = json.loads(data_line[len("data:"):].strip())
    http_names = {t["name"] for t in payload["result"]["tools"]}
    assert http_names == stdio_names


def test_http_tool_call_round_trips_health(monkeypatch):
    import json
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "cora_health", "arguments": {}}}
    with _make_client(monkeypatch, None) as client:
        r = client.post("/mcp", json=call, headers=_HEADERS)
    assert r.status_code == 200
    data_line = next(line for line in r.text.splitlines() if line.startswith("data:"))
    payload = json.loads(data_line[len("data:"):].strip())
    assert "result" in payload
    assert payload["result"]["isError"] is False


# ── D. Stdio entrypoint stays regression-free ────────────────────────────────
def test_stdio_entrypoint_unaffected():
    """Importing/using the HTTP bridge must not have altered the stdio module's
    tool surface, build_server, or main()."""
    import importlib

    import cora.mcp_server as mcp_server
    importlib.reload(mcp_server)
    assert len(mcp_server._TOOL_SPECS) == 6
    srv = mcp_server.build_server()
    assert srv is not None


def test_run_mcp_server_stdio_script_still_imports():
    """scripts/run_mcp_server.py (the stdio entrypoint) must remain importable
    and untouched by the HTTP bridge's addition."""
    src_path = Path(__file__).resolve().parent.parent / "scripts" / "run_mcp_server.py"
    src = src_path.read_text(encoding="utf-8")
    assert "from cora.mcp_server import main" in src
    assert "stdio" in src.lower()
