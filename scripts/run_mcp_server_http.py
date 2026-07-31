#!/usr/bin/env python3
"""Local-only streamable-HTTP bridge for Cora's read-only(+1) MCP tool surface.

Extends D-092 (see memory/decisions.md 2026-07-28 and the read-only stdio
server, src/cora/mcp_server.py + scripts/run_mcp_server.py). The Claude Code
lane already works over stdio (.mcp.json spawns run_mcp_server.py on demand);
the Cowork desktop app's Add-connector UI is remote-URL-only and cannot spawn
a stdio child or read claude_desktop_config.json, so it needs an HTTP
endpoint to hit instead. This script serves the EXACT SAME tool surface
(cora.mcp_server.build_server()) over the MCP SDK's streamable-HTTP
transport — no new tools, no changed tool behavior, nothing this process can
do that the stdio lane could not already do.

Kickoff of record: `_notes/2026-07-30_fndr_cora-code-prompt-mcp-http-bridge.md`
(Harrison "locked as recommended", 4 forks, 2026-07-30). Locked forks that
shape this file:
  1. Transport = local streamable-HTTP only (no remote hosting).
  2. Security = loopback-only, HARD-CODED (no config path may ever produce a
     non-loopback bind) + a static bearer token from CORA_MCP_HTTP_TOKEN,
     enforced with a constant-time compare when the var is set. If unset, the
     loopback bind is the only gate (v1-accepted posture on a single-user
     desktop for a read-only+1, PHI-scrubbed surface).
  3. Scope = Cowork + Claude Code (both can reach 127.0.0.1). claude.ai web is
     explicitly OUT — it cannot reach localhost, and reaching it would force
     remote hosting, a different, not-yet-made decision.
  4. The ONE write tool is `cora_code_queue_seed` (see src/cora/mcp_server.py);
     everything else is read-only, unchanged from the stdio lane.

This is a SEPARATE, standalone process from the stdio lane (both can run
concurrently against the same on-disk KB — read-only handles do not
contend) and from the always-on Cora bot (a different process entirely) —
shipping or restarting this script needs NO Cora bot restart.

Run (manual smoke, blocks in the foreground):
    .venv\\Scripts\\python.exe scripts\\run_mcp_server_http.py

Then point a client at http://127.0.0.1:<port>/mcp (port default 8791 --
CORA_MCP_HTTP_PORT overrides; 8787 is the bot's own health endpoint, do not
reuse it). If CORA_MCP_HTTP_TOKEN is set in .env, the client must send
`Authorization: Bearer <token>`.

Register as a standing background task: deployment\\setup-cora-mcp-http-task.ps1

--- TLS fallback (2026-07-30 GO/NO-GO follow-up) ---

The first GO/NO-GO smoke found the Cowork Add-connector UI rejects a plain
`http://` URL outright ("URL must start with 'https'") -- a scheme check, not
a localhost block. `CORA_MCP_HTTP_CERT` + `CORA_MCP_HTTP_KEY` (both set) make
this process terminate TLS itself and serve `https://127.0.0.1:<port>/mcp`
instead, with EVERY other invariant unchanged: same hard-coded `_BIND_HOST`,
same Host-header allowlist, same optional bearer token. TLS here is a
transport-scheme requirement some client UIs impose, not a new trust
boundary -- the peer is still only ever reachable from this machine.
Generate the self-signed pair with `deployment\\new-mcp-https-cert.ps1`
(loopback-SAN leaf, gitignored under `data\\state\\mcp-tls\\`). If either env
var is unset, this process serves plain HTTP exactly as before (Claude Code's
stdio lane is unaffected either way).
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Make `import cora...` work when launched as a bare script (repo-root cwd or not).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

log = logging.getLogger("cora.mcp_http")

# ─────────────────────────────────────────────────────────────────────────────
# Bind + port. _BIND_HOST is a bare literal — it is never read from os.environ,
# never passed through a CLI flag, never assembled from a config file. This is
# deliberate (D-051-light network-surface review, 2026-07-30, locked fork #2):
# there must be NO code path, present or future, that can make this listener
# reachable off the local machine. Only the port is configurable.
# ─────────────────────────────────────────────────────────────────────────────
_BIND_HOST = "127.0.0.1"
_DEFAULT_PORT = 8791  # 8787 is Cora's health endpoint -- do not collide.


def _bearer_token() -> str:
    """The configured bridge token, or "" if none is set (no auth gate beyond
    the loopback bind itself)."""
    return (os.environ.get("CORA_MCP_HTTP_TOKEN") or "").strip()


def _tls_paths() -> tuple[str, str] | None:
    """(certfile, keyfile) if BOTH CORA_MCP_HTTP_CERT and CORA_MCP_HTTP_KEY are
    set, else None (plain HTTP -- unchanged v1 behavior). Optional TLS is a
    transport-scheme accommodation for client UIs that refuse a bare `http://`
    URL (2026-07-30 GO/NO-GO finding); it does not relax or replace the
    loopback bind, the Host allowlist, or the bearer-token gate -- all three
    apply identically over TLS."""
    cert = (os.environ.get("CORA_MCP_HTTP_CERT") or "").strip()
    key = (os.environ.get("CORA_MCP_HTTP_KEY") or "").strip()
    if cert and key:
        return cert, key
    return None


def _extract_bearer(scope: dict[str, Any]) -> str:
    """Pull the bearer credential out of a raw ASGI scope's headers. Returns ""
    if absent or malformed -- callers compare against "" too, so an absent
    header never accidentally matches an empty configured token (the token
    check is only entered when a token IS configured)."""
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            raw = v.decode("latin-1", errors="replace")
            if raw.startswith("Bearer "):
                return raw[len("Bearer "):]
            return ""
    return ""


# D-051-light network-surface finding (2026-07-30): a loopback BIND alone does
# not stop DNS rebinding -- a page at http://evil.example running in the
# operator's own browser can have evil.example's DNS record point at
# 127.0.0.1, and the browser will still send same-origin fetch()es carrying
# `Host: evil.example:<port>` to this real loopback socket. The TCP peer really
# is local (rebinding cannot reach an off-machine bind), but the request is
# attacker-controlled. Browsers set Host from the URL's hostname, never from
# the resolved IP, so an allowlist on Host is the standard mitigation -- reject
# anything whose Host is not literally 127.0.0.1 / localhost / ::1.
_ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _host_header(scope: dict[str, Any]) -> str:
    for k, v in scope.get("headers") or []:
        if k == b"host":
            return v.decode("latin-1", errors="replace")
    return ""


def _host_is_allowed(host_header: str) -> bool:
    """True iff the request's Host header names this machine, not an
    attacker-chosen hostname that merely resolved (via rebinding) to 127.0.0.1."""
    hostname = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    return hostname in _ALLOWED_HOSTNAMES


def build_app():
    """Wrap cora.mcp_server's tool surface (the SAME Server the stdio lane
    serves) in a Starlette ASGI app exposing it over streamable-HTTP at
    `/mcp`. Imports mcp/starlette/uvicorn lazily -- importing this module
    (e.g. for the bind-literal structural test) never requires them."""
    import contextlib

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount

    from cora.mcp_server import build_server

    server = build_server()
    # Stateless: every HTTP POST is a fully self-contained JSON-RPC round trip
    # (its own implicit handshake + call), so there is no session id to issue,
    # track, or leak across requests -- the smallest surface that still serves
    # tool calls correctly.
    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)
    token = _bearer_token()

    async def mcp_asgi(scope: dict[str, Any], receive, send) -> None:
        if scope["type"] != "http":
            # This bridge only ever serves the streamable-HTTP transport (plain
            # HTTP request/response). Never dispatch any other ASGI scope type
            # (e.g. websocket) to the session manager -- D-051-light finding:
            # a non-http scope must not be able to skip the checks below by
            # construction, not by falling through an `and` on scope type.
            return
        if not _host_is_allowed(_host_header(scope)):
            resp = PlainTextResponse("Unauthorized", status_code=401)
            await resp(scope, receive, send)
            return
        if token:
            provided = _extract_bearer(scope)
            # Constant-time compare (locked fork #2); a mismatch gets a bare
            # 401 with no further detail -- never "bad token" vs "no header"
            # vs "wrong length", which would leak information to a probe.
            if not hmac.compare_digest(provided, token):
                resp = PlainTextResponse("Unauthorized", status_code=401)
                await resp(scope, receive, send)
                return
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            log.info("cora MCP HTTP bridge ready (loopback-only; token_required=%s)",
                      bool(token))
            yield

    return Starlette(routes=[Mount("/mcp", app=mcp_asgi)], lifespan=lifespan)


def main() -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        port = int(os.environ.get("CORA_MCP_HTTP_PORT", str(_DEFAULT_PORT)))
    except ValueError:
        port = _DEFAULT_PORT

    app = build_app()
    tls = _tls_paths()
    scheme = "https" if tls else "http"
    log.info("Starting cora MCP HTTP bridge on %s://%s:%s/mcp (loopback-only, hard-coded)",
              scheme, _BIND_HOST, port)
    if tls:
        certfile, keyfile = tls
        uvicorn.run(app, host=_BIND_HOST, port=port, log_level="info",
                    ssl_certfile=certfile, ssl_keyfile=keyfile)
    else:
        uvicorn.run(app, host=_BIND_HOST, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
