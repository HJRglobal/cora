"""Pytest configuration and shared fixtures.

Sets up the minimum environment variables required by cora.config at import
time so test modules can import cora packages without a real .env file.

The dummy tokens are formatted to pass the prefix-validation rules in
config.py but will never authenticate against any real service.

Also clears SOCKS/HTTP proxy environment variables that the Cowork sandbox
injects — these cause anthropic/httpx to fail when instantiating clients even
when the client creation is under test mocks.
"""

import os
import sys
import types
from unittest.mock import patch

import pytest


def _install_fake_tiktoken() -> None:
    """Register a network-free tiktoken stub in sys.modules before any source
    module is imported.

    chunker.py calls tiktoken.get_encoding("cl100k_base") at module load time.
    In CI / sandbox environments the encoding file cannot be fetched from
    openaipublic.blob.core.windows.net (403 / network-blocked), which causes a
    collection error for any test that transitively imports chunker.

    The stub treats each Unicode code-point as one token (len(text) tokens),
    which is deterministic and sufficient for the chunker's correctness tests.
    The encode/decode pair is reversible for ASCII inputs so the hard-truncation
    path in chunk_text() also works correctly.
    """
    if "tiktoken" in sys.modules:
        return

    class _FakeEncoder:
        def encode(self, text, disallowed_special=()):
            return [ord(c) for c in text]

        def decode(self, tokens):
            return "".join(chr(t) for t in tokens)

    _encoder = _FakeEncoder()
    fake = types.SimpleNamespace(get_encoding=lambda name: _encoder)
    sys.modules["tiktoken"] = fake  # type: ignore[assignment]


# ── Set required env vars at MODULE LOAD TIME ─────────────────────────────────
# Must happen before _patch_calendar_client_scheduler() (also module-level) and
# before any src.cora.* imports, because config._load() runs at module import
# time and raises if ANTHROPIC_API_KEY is missing.  pytest_configure() is too
# late -- it fires after module-level conftest code has already run.
#
# Use unconditional assignment (NOT setdefault) for keys that config._load()
# marks as required.  In the Cowork sandbox, these vars are already present but
# set to empty string ""; setdefault won't overwrite them, causing _load() to
# raise "ANTHROPIC_API_KEY: missing" even though the key technically exists.
os.environ["SLACK_BOT_TOKEN"]      = os.environ.get("SLACK_BOT_TOKEN") or "xoxb-test-dummy-token-for-ci"
os.environ["SLACK_APP_TOKEN"]      = os.environ.get("SLACK_APP_TOKEN") or "xapp-1-test-dummy-token-for-ci"
os.environ["SLACK_SIGNING_SECRET"] = os.environ.get("SLACK_SIGNING_SECRET") or "test-signing-secret-for-ci"
os.environ["ANTHROPIC_API_KEY"]    = os.environ.get("ANTHROPIC_API_KEY") or "sk-ant-test-dummy-key-for-ci"
os.environ["ASANA_PAT"]            = os.environ.get("ASANA_PAT") or "0/dummy-asana-pat-for-ci"

# Disable the HubSpot D-030 portal guard in unit tests. The guard probes
# account-info/v3/details on first request; HubSpot test modules mock httpx.Client
# with deal-search payloads (no portalId), which would otherwise trip a false
# mismatch. The guard's own logic is exercised explicitly in test_hubspot_portal_guard.py
# (which clears this flag).
os.environ["CORA_DISABLE_HUBSPOT_PORTAL_GUARD"] = "1"

_install_fake_tiktoken()

# Import cora.config NOW (env vars already set above) so that
# test_f3e_inventory_location.py's "if 'cora.config' not in sys.modules" guard
# sees it already loaded and skips injecting its fake _Config module, which
# would pollute the real config object for subsequent tests.
try:
    import cora.config as _  # noqa: F401
except Exception:
    pass  # best-effort; tests that need config will re-import it


def _mock_slack_auth_test() -> None:
    """Prevent the Bolt App() constructor from making a live auth.test call.

    Bolt calls slack_sdk's auth.test immediately when App(token=...) is
    constructed.  In tests we use a dummy token, so that call would reach
    Slack's servers and fail.  This patch intercepts it at the SDK level and
    returns a minimal successful response so any test file that imports
    cora.app can do so safely without a network connection.

    The patcher is never stopped — the mock remains in effect for the whole
    pytest session.  Real Slack interaction is never needed in unit tests.
    """
    fake_response = {
        "ok": True,
        "url": "https://test.slack.com/",
        "user_id": "U_CORA_TEST",
        "team": "TestWorkspace",
        "user": "testbot",
        "team_id": "T_TEST",
        "bot_id": "B_TEST",
    }
    patcher = patch(
        "slack_sdk.web.client.WebClient.auth_test",
        return_value=fake_response,
    )
    patcher.start()


_mock_slack_auth_test()


def pytest_configure(config):
    """Called by pytest before any test collection or execution begins.

    Sets dummy env vars so cora.config._load() succeeds, and clears
    proxy vars that interfere with the anthropic SDK in CI/sandbox envs.
    """
    # ── Required tokens (format must match config._PREFIX_RULES) ──────────────
    # Use "or" fallback so empty-string env vars (Cowork sandbox) get overwritten.
    os.environ["SLACK_BOT_TOKEN"]      = os.environ.get("SLACK_BOT_TOKEN") or "xoxb-test-dummy-token-for-ci"
    os.environ["SLACK_APP_TOKEN"]      = os.environ.get("SLACK_APP_TOKEN") or "xapp-1-test-dummy-token-for-ci"
    os.environ["SLACK_SIGNING_SECRET"] = os.environ.get("SLACK_SIGNING_SECRET") or "test-signing-secret-for-ci"
    os.environ["ANTHROPIC_API_KEY"]    = os.environ.get("ANTHROPIC_API_KEY") or "sk-ant-test-dummy-key-for-ci"
    os.environ["ASANA_PAT"]            = os.environ.get("ASANA_PAT") or "0/dummy-asana-pat-for-ci"
    os.environ["CORA_DISABLE_HUBSPOT_PORTAL_GUARD"] = "1"

    # ── Proxy vars that break anthropic/httpx in sandbox/CI environments ──────
    # The Cowork sandbox sets all_proxy=socks5h://localhost:1080 which causes
    # anthropic.Anthropic() to try to configure SOCKS support and fail with
    # ImportError when 'socksio' is not installed.  Unset all proxy vars here;
    # tests that actually need network access should set them explicitly.
    for var in (
        "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
        "HTTPS_PROXY", "https_proxy",
        "FTP_PROXY", "ftp_proxy",
        "GRPC_PROXY", "grpc_proxy",
        "RSYNC_PROXY",
        "DOCKER_HTTP_PROXY", "DOCKER_HTTPS_PROXY",
    ):
        os.environ.pop(var, None)


# (Calendar-scheduler conftest injection removed — W7-05. The shipped
# cora.tools.calendar_client already exports _round_up_to_slot /
# find_next_available_slot / format_slot_proposal_for_llm / get_free_busy,
# so the CIFS-staleness workaround was dead-on-host and only risked a false
# green — tests now always exercise the real module.)


@pytest.fixture(autouse=True)
def _isolate_cross_test_global_state(tmp_path, monkeypatch):
    """Isolate module-global state that otherwise leaks between tests.

    1. Nudge ledger: point CLOSURE_NUDGE_LOG_PATH at an isolated temp file so
       run_asana_hygiene_nudges tests never read/write the REAL closure-nudges
       JSONL on the Drive. Tests exercising the ledger directly override it.

    2. HubSpot portal guard: test_hubspot_portal_guard.py enables the live guard
       (deletes CORA_DISABLE_HUBSPOT_PORTAL_GUARD and flips _portal_verified),
       and one test sets _portal_verified raw. Under some collection orders that
       leaked into test_hubspot_two_way, which then made a live /account-info
       call. Force the guard back to disabled + reset the flag after every test
       so portal state can never leak across tests.
    """
    monkeypatch.setenv(
        "CLOSURE_NUDGE_LOG_PATH", str(tmp_path / "closure-nudges-throttle.jsonl")
    )
    # WS-1 gap detection: isolate the dedup/cap state file and the gap log so
    # app-level tests that drive _dispatch_qa can never write the repo's real
    # data/state/gap_detection_state.json or logs/knowledge-gaps.jsonl. Tests
    # exercising these directly override the same env vars.
    monkeypatch.setenv(
        "GAP_DETECTION_STATE_PATH", str(tmp_path / "gap_detection_state.json")
    )
    monkeypatch.setenv(
        "KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "knowledge-gaps.jsonl")
    )
    # WS-3 golden-set auto-growth: executor tests that drive
    # _execute_approved_update fire the auto-growth hook -- isolate its target
    # so a test fixture's fake fact can never land in the repo's real
    # data/evals/golden-set-auto.yaml (it did, once, before this line).
    monkeypatch.setenv(
        "GOLDEN_SET_AUTO_PATH", str(tmp_path / "golden-set-auto.yaml")
    )
    # WS-4 drive-extractor pause: .env carries DRIVE_EXTRACTOR_PROPOSALS_ENABLED=0
    # (the D-066 production pause) and config.py's import-time load_dotenv() pulls
    # it into the test process, short-circuiting run_proposal_loop and reddening
    # every proposal-path test. Clear it so tests run against the CODE default
    # (enabled); the pause-gate tests set/clear the var explicitly themselves.
    monkeypatch.delenv("DRIVE_EXTRACTOR_PROPOSALS_ENABLED", raising=False)
    try:
        import cora.gap_detection as _gd
        _gd._THREAD_LOGGED.clear()
    except Exception:
        pass
    yield
    os.environ["CORA_DISABLE_HUBSPOT_PORTAL_GUARD"] = "1"
    try:
        import cora.tools.hubspot_client as _hc
        _hc._portal_verified = False
    except Exception:
        pass
