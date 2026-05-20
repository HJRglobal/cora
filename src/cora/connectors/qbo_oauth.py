"""QuickBooks Online OAuth 2.0 — per-entity token management with refresh loop.

Phase 2 #10 scope:
- Per-entity tokens stored in .credentials/qbo-tokens.json (gitignored)
- One-time browser-based OAuth bootstrap via start_oauth_flow(entity)
- Transparent access-token refresh via get_valid_access_token(entity)
- Refresh-token rotation (each refresh yields a new refresh token; we persist both)

Token lifetimes (per Intuit OAuth 2.0 spec, 2026):
- Access token:  1 hour
- Refresh token: 100 days (rolling — each successful refresh extends to +100 days)

If a refresh token is unused for 100 days, manual re-OAuth is required for that
entity. The token-refresh scheduled task (`cowork-cora-qbo-token-refresh`, runs
daily ~2:00 AM AZ) keeps things rotated long before that window closes.

Files referenced:
- `.env`                          QBO_CLIENT_ID / QBO_CLIENT_SECRET / QBO_REDIRECT_URI / QBO_ENVIRONMENT
- `.credentials/qbo-tokens.json`  per-entity tokens + realm_id

Token file shape:
    {
        "HJRG": {
            "realm_id":                       "9341454488648842",
            "access_token":                   "...",
            "refresh_token":                  "...",
            "access_token_expires_at":        1716100000,   # unix ts
            "refresh_token_expires_at":       1779200000,   # unix ts
            "last_refreshed_at":              1716096400,
            "environment":                    "production"
        },
        "F3E": {...},
        ...
    }
"""

from __future__ import annotations

import base64
import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class QboAuthError(Exception):
    """Raised on any QBO OAuth / token-refresh failure."""


# Intuit OAuth 2.0 endpoints (same for sandbox and production accounts —
# the environment switch is about which COMPANY data the tokens access,
# not which auth endpoint to hit).
_AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_DEFAULT_SCOPE = "com.intuit.quickbooks.accounting"

# Access tokens live 1 hour. Refresh ~10 min before expiry to avoid races.
_ACCESS_TOKEN_TTL_SEC = 3600
_REFRESH_LEAD_TIME_SEC = 600

# Local callback server (used during start_oauth_flow only).
_CALLBACK_HOST = "localhost"
_CALLBACK_PORT = 8765
_CALLBACK_PATH = "/qbo-oauth-callback"

# Token store location — kept in repo as gitignored `.credentials/` dir, alongside the
# Calendar service-account JSON (existing pattern).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOKEN_FILE = _REPO_ROOT / ".credentials" / "qbo-tokens.json"


# ────────────────────────────────────────────────────────────────────────────
# Token store — atomic read/write, single JSON file keyed by entity code
# ────────────────────────────────────────────────────────────────────────────


def _load_all_tokens() -> dict[str, dict[str, Any]]:
    """Return the entire token map. Empty dict if the file doesn't exist yet."""
    if not _TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QboAuthError(f"Failed to read {_TOKEN_FILE}: {exc}") from exc


def _save_all_tokens(tokens: dict[str, dict[str, Any]]) -> None:
    """Atomic write — temp file + rename so we never leave a partial JSON."""
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _TOKEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tokens, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_TOKEN_FILE)
    # Best-effort permission tighten on POSIX; no-op on Windows.
    try:
        os.chmod(_TOKEN_FILE, 0o600)
    except OSError:
        pass


def _get_entity_tokens(entity: str) -> dict[str, Any]:
    tokens = _load_all_tokens()
    if entity not in tokens:
        raise QboAuthError(
            f"No QBO tokens found for entity {entity!r}. "
            f"Run: uv run python scripts/qbo_oauth_flow.py --entity {entity}"
        )
    return tokens[entity]


def _set_entity_tokens(entity: str, entry: dict[str, Any]) -> None:
    tokens = _load_all_tokens()
    tokens[entity] = entry
    _save_all_tokens(tokens)


# ────────────────────────────────────────────────────────────────────────────
# Config helpers — pull from cora.config so we get prefix validation + .env loading
# ────────────────────────────────────────────────────────────────────────────


def _client_creds() -> tuple[str, str]:
    from ..config import config  # local import to avoid circular import at module load
    if not config.qbo_client_id or not config.qbo_client_secret:
        raise QboAuthError(
            "QBO_CLIENT_ID and/or QBO_CLIENT_SECRET missing from .env — "
            "QBO tool-use disabled. Create an Intuit Developer app at "
            "https://developer.intuit.com → My Apps → Create an app → "
            "QuickBooks Online and Payments."
        )
    return config.qbo_client_id, config.qbo_client_secret


def _redirect_uri() -> str:
    from ..config import config
    return config.qbo_redirect_uri


def _basic_auth_header() -> str:
    client_id, client_secret = _client_creds()
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ────────────────────────────────────────────────────────────────────────────
# OAuth flow — one-time bootstrap per entity
# ────────────────────────────────────────────────────────────────────────────


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the ?code & ?realmId from Intuit's redirect and shuts the server down."""

    # Set by start_oauth_flow on the server instance.
    expected_state: str = ""
    captured: dict[str, str] = {}  # populated in do_GET

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — std lib signature
        # Silence the default stderr access log — we use our own logger.
        log.debug("oauth callback: " + format, *args)

    def do_GET(self) -> None:  # noqa: N802 — std lib name
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != _CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        code = (params.get("code") or [""])[0]
        realm_id = (params.get("realmId") or [""])[0]
        error = (params.get("error") or [""])[0]

        if error:
            self.captured["error"] = error
            self._respond(f"Authorization error: {error}. You can close this tab.")
            return

        if state != self.expected_state:
            self.captured["error"] = "state mismatch — possible CSRF"
            self._respond("State mismatch — refusing. You can close this tab.")
            return

        if not code or not realm_id:
            self.captured["error"] = "missing code or realmId"
            self._respond("Missing code or realmId. You can close this tab.")
            return

        self.captured["code"] = code
        self.captured["realm_id"] = realm_id
        self._respond(
            "<h1>QBO authorization captured ✓</h1>"
            "<p>You can close this tab. The CLI will finish exchanging tokens.</p>"
        )

    def _respond(self, body: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


def _run_callback_server(state: str, timeout_sec: int = 300) -> dict[str, str]:
    """Spin up the local callback server, wait for the redirect, return captured params."""
    handler_class = type(
        "BoundHandler",
        (_OAuthCallbackHandler,),
        {"expected_state": state, "captured": {}},
    )

    with socketserver.TCPServer((_CALLBACK_HOST, _CALLBACK_PORT), handler_class) as server:
        server.timeout = 1
        deadline = time.monotonic() + timeout_sec

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            while time.monotonic() < deadline:
                if handler_class.captured:
                    break
                time.sleep(0.5)
            else:
                raise QboAuthError(
                    f"OAuth callback timed out after {timeout_sec}s — no redirect received"
                )
        finally:
            server.shutdown()

    captured = handler_class.captured
    if "error" in captured:
        raise QboAuthError(f"OAuth callback failed: {captured['error']}")
    return captured


def _exchange_code_for_tokens(code: str) -> dict[str, Any]:
    """POST to Intuit's token endpoint, exchanging an auth code for access+refresh tokens."""
    resp = httpx.post(
        _TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise QboAuthError(
            f"Token exchange failed: HTTP {resp.status_code} — {resp.text[:300]}"
        )
    return resp.json()


def start_oauth_flow(entity: str, environment: str | None = None) -> dict[str, Any]:
    """Run the full browser-based OAuth flow for a single entity, persist tokens.

    Returns the saved token entry (including realm_id, access_token, refresh_token).
    Raises QboAuthError on any failure.
    """
    from ..config import config

    env = environment or config.qbo_environment or "production"
    if env not in ("production", "sandbox"):
        raise QboAuthError(
            f"Invalid environment {env!r}. Must be 'production' or 'sandbox'."
        )

    client_id, _ = _client_creds()
    state = secrets.token_urlsafe(16)

    authorize_url = _AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "scope": _DEFAULT_SCOPE,
        "redirect_uri": _redirect_uri(),
        "state": state,
    })

    log.info("Starting QBO OAuth flow for entity=%s env=%s", entity, env)
    log.info("Opening browser to: %s", authorize_url)
    webbrowser.open(authorize_url, new=2)
    print(
        "\n  → A browser window should have opened. Sign in to Intuit, pick the "
        f"QBO company that maps to '{entity}', authorize the app.\n"
        "  → If the browser didn't open, paste this URL manually:\n\n"
        f"    {authorize_url}\n"
    )

    captured = _run_callback_server(state)
    code = captured["code"]
    realm_id = captured["realm_id"]

    log.info("Captured auth code (realm_id=%s) — exchanging for tokens", realm_id)
    token_resp = _exchange_code_for_tokens(code)

    now = int(time.time())
    entry = {
        "realm_id":                  realm_id,
        "access_token":              token_resp["access_token"],
        "refresh_token":             token_resp["refresh_token"],
        # Intuit returns expires_in (sec) for access and x_refresh_token_expires_in for refresh.
        "access_token_expires_at":   now + int(token_resp.get("expires_in", _ACCESS_TOKEN_TTL_SEC)),
        "refresh_token_expires_at":  now + int(token_resp.get("x_refresh_token_expires_in", 8640000)),
        "last_refreshed_at":         now,
        "environment":               env,
    }
    _set_entity_tokens(entity, entry)
    log.info("Persisted QBO tokens for entity=%s realm_id=%s", entity, realm_id)
    return entry


# ────────────────────────────────────────────────────────────────────────────
# Token refresh — silent rotation when access token is near/expired
# ────────────────────────────────────────────────────────────────────────────


def _refresh_access_token(entity: str) -> dict[str, Any]:
    """Use the refresh token to get a new access+refresh pair. Persist both."""
    entry = _get_entity_tokens(entity)
    refresh_token = entry.get("refresh_token")
    if not refresh_token:
        raise QboAuthError(
            f"No refresh token stored for entity {entity!r} — re-run start_oauth_flow."
        )

    resp = httpx.post(
        _TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise QboAuthError(
            f"Refresh failed for entity={entity}: HTTP {resp.status_code} — {resp.text[:300]}"
        )

    payload = resp.json()
    now = int(time.time())
    entry["access_token"] = payload["access_token"]
    entry["refresh_token"] = payload["refresh_token"]
    entry["access_token_expires_at"] = now + int(payload.get("expires_in", _ACCESS_TOKEN_TTL_SEC))
    entry["refresh_token_expires_at"] = now + int(
        payload.get("x_refresh_token_expires_in", 8640000)
    )
    entry["last_refreshed_at"] = now
    _set_entity_tokens(entity, entry)
    log.info("Refreshed QBO tokens for entity=%s", entity)
    return entry


def get_valid_access_token(entity: str) -> tuple[str, str]:
    """Return (access_token, realm_id) for entity. Refresh transparently if near expiry.

    This is the single function tool code should call — it never returns a stale token.
    """
    entry = _get_entity_tokens(entity)
    now = int(time.time())

    if entry["access_token_expires_at"] - now < _REFRESH_LEAD_TIME_SEC:
        log.info(
            "Access token for entity=%s within %ds of expiry — refreshing",
            entity, _REFRESH_LEAD_TIME_SEC,
        )
        entry = _refresh_access_token(entity)

    return entry["access_token"], entry["realm_id"]


def refresh_all_entities() -> dict[str, str]:
    """Force-refresh every entity's access token. Used by the daily rotation task.

    Returns a {entity: status} map — "ok" or an error message — for logging.
    Does not raise on individual entity failures; logs them and continues.
    """
    out: dict[str, str] = {}
    tokens = _load_all_tokens()
    for entity in tokens:
        try:
            _refresh_access_token(entity)
            out[entity] = "ok"
        except QboAuthError as exc:
            log.error("Refresh failed for entity=%s: %s", entity, exc)
            out[entity] = f"error: {exc}"
    return out


def list_provisioned_entities() -> list[str]:
    """Return entity codes that have stored tokens."""
    return sorted(_load_all_tokens().keys())
