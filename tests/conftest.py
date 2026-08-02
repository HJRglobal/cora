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
    # MED-3 (2026-07-10): the DTC inventory write tool appends an audit line to
    # logs/shopify-inventory-writes.jsonl. Redirect that path to a tmp file for
    # EVERY test (a build-session suite run polluted the real file with 3 fixture
    # rows), and clear the in-memory pending-confirmation store so it never leaks
    # across tests. Belt: the session guard below fails the run if logs/ is touched.
    try:
        import cora.tools.tool_dispatch as _td
        monkeypatch.setattr(
            _td, "_SHOPIFY_WRITE_AUDIT_PATH",
            tmp_path / "shopify-inventory-writes.jsonl", raising=False,
        )
        _td._PENDING_SHOPIFY_WRITES.clear()
    except Exception:
        pass
    # Slice 5 (2026-07-29 audit): the two rollout flags live in .env
    # (CORA_AUTOWRITE_LIVE=all, CORA_CODE_QUEUE=live) and config.py's import-time
    # load_dotenv() pulls them into the test process. Both writers read their flag
    # per-call (knowledge_review.autowrite_level / code_queue.code_queue_level), so
    # reset both to "off" for EVERY test -- a test that needs a live value sets it
    # explicitly (test_code_queue's qenv sets "live"; test_kb_autowrite / the three
    # test_run_knowledge_review cases set the value they need). This is the
    # ROOT-CAUSE fix for the ledger test-pollution that contaminated
    # logs/cora-autowrite-audit.jsonl + data/state/code-session-queue*.jsonl.
    monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "off")
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")
    # Lexicon flag: the live .env will carry CORA_LEXICON=resolve after rollout;
    # pin every test to "off" (legacy behavior) -- a test that needs a level sets
    # it explicitly. Telemetry path redirected to tmp as a belt (the writer is
    # fail-soft per-call env-read, same class as the flags above).
    monkeypatch.setenv("CORA_LEXICON", "off")
    monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH",
                       str(tmp_path / "lexicon-resolutions.jsonl"))
    # Web-tools knobs are read per-call from the environment; a live .env flip
    # (CORA_WEB_TOOLS=off, a custom cap) would otherwise redden web_guard tests.
    # Delete them so every test starts from the code defaults (tools ON, cap 40).
    for _wv in (
        "CORA_WEB_TOOLS", "CORA_WEB_SEARCH_MAX_USES", "CORA_WEB_FETCH_MAX_USES",
        "CORA_WEB_SEARCH_DAILY_CAP", "CORA_WEB_KB_MISS_DISTANCE",
    ):
        monkeypatch.delenv(_wv, raising=False)
    # cq-d9432f552a33 (bug-hunt Slice 10): the known-answers WRITE targets resolve
    # via PER-CALL env reads (gap_autofill._known_answers_dir/_resolved_path), so
    # the module-constant belt below cannot cover them -- and .env carries the
    # LIVE Drive store (KNOWN_ANSWERS_DIR=..._brain/known-answers), which is how a
    # 2026-07-25 suite run auto-wrote the U-TOMMY/"lives in Polar" fixture into
    # the PRODUCTION f3e.md. Redirect both for EVERY test; a test that needs a
    # specific value sets it explicitly (its monkeypatch wins).
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path / "known-answers"))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(tmp_path / "resolved-gaps.jsonl"))
    # Belt: even if a test flips a flag live but forgets to isolate the path,
    # redirect every module-constant ledger writer to tmp so a test can NEVER touch
    # a real logs/ or data/state/ file. Each in its own try/except (a missing or
    # renamed module must never break the fixture). A test that patches one of
    # these itself wins -- its monkeypatch runs after this autouse one.
    import importlib as _importlib
    _LEDGER_CONSTS = [
        ("cora.code_queue", "_EVENT_LEDGER", "code-session-queue.jsonl"),
        ("cora.code_queue", "_FINGERPRINT_LEDGER", "code-queue-fingerprints.jsonl"),
        ("cora.code_queue", "_SIGNALS_LEDGER", "code-queue-signals.jsonl"),
        ("cora.knowledge_review", "_AUTOWRITE_AUDIT_PATH", "cora-autowrite-audit.jsonl"),
        ("cora.pm_metrics", "_ACTION_LOG", "pm-actions.jsonl"),
        ("cora.pm_metrics", "_SNAPSHOT_DIR", "pm-adoption-snapshots"),
        ("cora.finance_receipts", "_AUDIT_LOG_PATH", "finance-access-audit.jsonl"),
        ("cora.historical_access", "_AUDIT_LOG_PATH", "historical-access-audit.jsonl"),
        ("cora.session_capture", "LEDGER_PATH", "session-captures.jsonl"),
        ("cora.feedback_log", "_LOG_PATH", "feedback.jsonl"),
        ("cora.user_feedback_tracker", "_LOG_PATH", "cora-user-feedback.jsonl"),
        ("cora.connectors.fireflies_connector", "_DEDUP_LEDGER_PATH",
         "fireflies-dedup-ledger.json"),
        ("cora.connectors.fireflies_action_extractor", "_WATERMARK_PATH",
         "meeting_action_watermark.json"),
        ("cora.web_guard", "_USAGE_LEDGER", "web-search-usage.jsonl"),
        ("cora.delegated_work", "_BOT_LEDGER", "delegated-work.jsonl"),
        ("cora.delegated_work", "_RUNNER_LEDGER", "delegated-work-runner.jsonl"),
        ("cora.delegated_work", "_STAGING_ROOT", "delegated-work-staging"),
    ]
    for _mod_name, _attr, _fname in _LEDGER_CONSTS:
        try:
            _mod = _importlib.import_module(_mod_name)
            if hasattr(_mod, _attr):
                monkeypatch.setattr(_mod, _attr, tmp_path / _fname, raising=False)
        except Exception:
            pass
    yield
    os.environ["CORA_DISABLE_HUBSPOT_PORTAL_GUARD"] = "1"
    try:
        import cora.tools.hubspot_client as _hc
        _hc._portal_verified = False
    except Exception:
        pass
    try:
        import cora.tools.tool_dispatch as _td
        _td._PENDING_SHOPIFY_WRITES.clear()
        _td._PENDING_DELEGATED_WORK.clear()
    except Exception:
        pass


# Real ledger/state files under logs/ and data/ that the test suite must NEVER
# mutate (Slice 5, 2026-07-29 audit: generalized from the single shopify audit
# file). Repo-relative; the autouse fixture above redirects each writer's module
# constant to tmp, so a change here at session end means a test escaped isolation.
_GUARDED_LEDGERS = (
    "logs/shopify-inventory-writes.jsonl",
    "logs/cora-autowrite-audit.jsonl",
    "data/state/code-session-queue.jsonl",
    "data/state/code-queue-fingerprints.jsonl",
    "data/state/code-queue-signals.jsonl",
    "logs/pm-actions.jsonl",
    "logs/finance-access-audit.jsonl",
    "logs/historical-access-audit.jsonl",
    "logs/session-captures.jsonl",
    "logs/feedback.jsonl",
    "logs/cora-user-feedback.jsonl",
    "data/cora-proposed-memory-updates.jsonl",
    "data/cora-reply-log.jsonl",
    "data/state/fireflies-dedup-ledger.json",
    "data/state/meeting_action_watermark.json",
    "data/state/web-search-usage.jsonl",
    # Lexicon Flywheel: the chokepoint telemetry ledger + the three files of
    # record the review-rail writer may append to (writer tests must redirect
    # via LEXICON_* env vars; a mutation here means a test escaped isolation).
    "logs/lexicon-resolutions.jsonl",
    "data/maps/f3e-sku-aliases.yaml",
    "data/maps/user-aliases.yaml",
    "data/state/delegated-work.jsonl",
    "data/state/delegated-work-runner.jsonl",
)


@pytest.fixture(scope="session", autouse=True)
def _guard_logs_untouched():
    """Repo guard (Slice 5): the test suite must NOT mutate any real ledger under
    logs/ or data/. Snapshot each guarded file's (size, mtime) at session start and
    re-check at session end -- if a test writes to a real path instead of its tmp
    redirect, flag it (naming the offending file).

    Live-host safety (review #6): the always-on bot appends to several of these
    files. If the bot is running concurrently (heartbeat fresh), a change is almost
    certainly a legitimate live write, NOT a test regression -- so downgrade to a
    warning rather than false-failing the whole suite. On a quiet host (CI / dev,
    no live bot) the redirects mean tests can't touch these, so a change IS a
    regression -> fail.
    """
    import warnings
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parent.parent
    heartbeat = root / "data" / "health" / "heartbeat.txt"
    guarded = [root / rel for rel in _GUARDED_LEDGERS]
    # cq-d9432f552a33: guard the known-answers stores too -- repo seeds AND the
    # live Drive store. The Drive dir must come from the repo .env parsed
    # DIRECTLY (never os.environ: the autouse redirect deliberately points env
    # at tmp for every test). Every G: touch is BOUNDED via drive_io (D-051
    # bundle review: a plain glob/stat on a degraded mount can hang or raise
    # non-FileNotFoundError OSErrors -- the guard must never wedge or crash the
    # suite). Fail-soft everywhere: no .env line / no G: / drive_io outage ->
    # the Drive files are simply not guarded this session.
    guarded.extend(sorted((root / "design" / "known-answers").glob("*.md")))
    guarded.append(root / "design" / "known-answers" / ".resolved-gaps.jsonl")
    # Lexicon stores: every data/maps/lexicon/*.yaml is a review-rail write
    # target; a suite-run mutation means a writer test escaped its tmp redirect.
    guarded.extend(sorted((root / "data" / "maps" / "lexicon").glob("*.yaml")))
    try:
        from cora import drive_io as _dio
        env_text = (root / ".env").read_text(encoding="utf-8", errors="replace")
        for line in env_text.splitlines():
            if line.strip().startswith("KNOWN_ANSWERS_DIR="):
                live_dir = _Path(line.split("=", 1)[1].strip().strip('"').strip("'"))
                guarded.extend(sorted(
                    _dio.glob(live_dir, "*.md", timeout=5.0, retry_seconds=0)))
                break
    except Exception:
        pass

    def _snap(p):
        # Bounded + broadly fail-soft (D-051 bundle review): a degraded G: mount
        # raises OSErrors beyond FileNotFoundError and a raw stat can hang --
        # the guard is best-effort observability, never a suite-wedger.
        try:
            from cora import drive_io as _dio
            info = _dio.stat_info(p, timeout=5.0, retry_seconds=0)
            return None if info is None else (info[1], int(info[0]))
        except Exception:
            return None

    def _bot_live():
        try:
            import time as _t
            return (_t.time() - heartbeat.stat().st_mtime) < 180
        except Exception:
            return False

    before = {p: _snap(p) for p in guarded}
    yield
    changed = [p for p in guarded if _snap(p) != before[p]]
    if not changed:
        return
    names = ", ".join(str(p) for p in changed)
    msg = f"real ledger(s) changed during the suite: {names}"
    if _bot_live():
        warnings.warn(msg + " -- but the live bot is running (heartbeat fresh), so this "
                      "is most likely a concurrent real write, not a test regression.")
    else:
        raise AssertionError(
            msg + " -- a test wrote to a real ledger instead of a tmp path "
            "(no live bot detected). Extend the autouse ledger-isolation fixture.")
