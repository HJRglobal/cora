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


def pytest_collection_finish(session):
    """Belt for the import-time env leak class (2026-09-09): a test module that
    imports scripts/run_kb_evals.py at module level arms CORA_EVAL_MODE=1 for the
    whole session at COLLECTION time, and tools_for_entity() then returns [] for
    every test that lacks its own delenv guard. Collection must leave the flag
    unset; a leak is undone here and named, so it cannot hide behind a sibling
    module that happens to pop it."""
    if os.environ.pop("CORA_EVAL_MODE", None) is not None:
        import warnings
        warnings.warn("CORA_EVAL_MODE was set after collection -- a test module imports "
                      "the eval harness at module level; unset for the session")


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


class _EveryoneIsAMember(frozenset):
    """A member set that contains every id -- the suite-wide default for the G1
    live-membership check (see _inventory_membership_default_member). Truthy and
    non-empty by construction (D-051 lens D LOW #11): a future `if not members`
    branch must never read the default as an EMPTY channel while `in` says yes."""

    def __contains__(self, item) -> bool:  # noqa: D401
        return True

    def __len__(self) -> int:
        return 1

    def __bool__(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _inventory_membership_default_member(monkeypatch):
    """Code #12 G1: an OUT-OF-CHANNEL DTC inventory write requires LIVE membership
    of #f3-hq-inventory-adjustments, read via the bot token. The suite has no token
    and must never call Slack, and every inventory-tool suite that predates the
    rule drives the tool from another channel to exercise resolve / preview /
    confirm -- not authority. So by default every test runs as a channel MEMBER.
    tests/test_inventory_membership_auth.py owns the authority contract and
    restores the real lookup for its own tests."""
    try:
        from cora import inventory_membership as im
    except Exception:  # noqa: BLE001 -- module absent on an old tree
        yield
        return
    im.reset_cache()
    monkeypatch.setattr(im, "member_ids",
                        lambda channel_id, *, client_factory=None, now=None: _EveryoneIsAMember())
    yield
    im.reset_cache()


@pytest.fixture(autouse=True)
def _reset_cashflow_as_of_state():
    """Code #14 S5: gsheets_financials memoizes the sheet modifiedTime per file for
    the cache TTL and latches its as_of-unknown WARNING once per AZ day. Both are
    process-global, so a test that patches the date to "2026-05-22" would leak it
    into a later test expecting "unknown" (and a latched WARN would hide the next
    test's WARN). Reset both around every test -- only when the module is already
    imported, so this costs nothing for the suites that never touch it."""
    import sys as _sys
    _gf = _sys.modules.get("cora.connectors.gsheets_financials")
    if _gf is not None and hasattr(_gf, "_reset_as_of_state"):
        _gf._reset_as_of_state()
    yield
    _gf = _sys.modules.get("cora.connectors.gsheets_financials")
    if _gf is not None and hasattr(_gf, "_reset_as_of_state"):
        _gf._reset_as_of_state()


@pytest.fixture(autouse=True)
def _meet_audit_dark_by_default(monkeypatch):
    """Code #14 R14-8: the Meet join audit seam reads EVERY Workspace audit log's
    API under an admin subject. No test may ever mint that token or reach the
    Reports endpoint, so the service builder is redirected to a `dark:test` lane
    state for every test (the default render is then byte-identical to today's).
    A test that exercises the seam passes `service=` explicitly or patches
    `_build_reports_service` itself (its monkeypatch runs after this one)."""
    try:
        from cora.connectors import meet_audit as _ma
    except Exception:  # noqa: BLE001 -- a missing module must never break the suite
        yield
        return

    def _blocked():
        raise _ma.MeetAuditDark(_ma.STATE_DARK_TEST, "test isolation")

    monkeypatch.setattr(_ma, "_build_reports_service", _blocked)
    monkeypatch.delenv(_ma.ADMIN_SUBJECT_ENV, raising=False)
    yield


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
    # C6 decision fingerprints: pass-5 records a row at PROPOSAL time, so any
    # test that drives reconcile(passes=[5]) writes to the real ledger -- and the
    # SECOND such run then suppresses the gap the first one recorded, turning a
    # green test red for a reason that has nothing to do with the code under
    # test. (Exactly what happened here before this line existed.)
    monkeypatch.setenv(
        "DECISION_FACT_FP_PATH", str(tmp_path / "decision-fact-fingerprints.jsonl")
    )
    # Code #15 S2 (cq-22b84598aee8): the gap-task propose-once / created ledger
    # (writers: reconciliation_engine.record_task_proposals via the runner, the
    # knowledge-review asana_task executor, and the one-time bootstrap). Resolved
    # per call by gap_task_dedup.ledger_path. Redirected in the SAME commit that
    # introduced the writer.
    monkeypatch.setenv(
        "GAP_TASK_FP_PATH", str(tmp_path / "gap-task-fingerprints.jsonl")
    )
    # Same class, same commit: the decision-ALERT state file. decision_alerts
    # resolves its path per call (deliberately -- a module-level constant reading
    # os.environ is the cq-06f4797db4f1 trap), but only if a test actually points
    # it somewhere. Without this every alert test reads and writes the real
    # data/state/decision-alert-pending.json.
    monkeypatch.setenv(
        "DECISION_ALERT_STATE_PATH", str(tmp_path / "decision-alert-pending.json")
    )
    # S3 meeting-ask cards (cq-f52c6b691127) resolve their store per call for the
    # same reason; same redirect for the same failure.
    monkeypatch.setenv(
        "MEETING_ASK_STATE_PATH", str(tmp_path / "meeting-ask-pending.json")
    )
    # Code #15 S1 (cq-d9d0c92cc797): the combined KB purge script's INTENT/APPLIED
    # records (scripts/purge_kb_code15_2026-09.py; default <repo>/logs/). Resolved
    # per call from this env var; redirected in the SAME commit as the writer.
    monkeypatch.setenv("CORA_KB_PURGE_OUT_DIR", str(tmp_path / "kb-purge-code15"))
    # Code #13 slice 5 (cq-7a724ee43964): the meeting-recap card store (append-only
    # events) + its send ledger. Both resolve per call; both born with their
    # redirect in the same change, per the session-#11 S4 rule.
    monkeypatch.setenv(
        "MEETING_RECAP_PENDING_PATH", str(tmp_path / "meeting-recap-pending.jsonl")
    )
    monkeypatch.setenv(
        "MEETING_RECAP_LEDGER_PATH", str(tmp_path / "meeting-recap-ledger.jsonl")
    )
    # F3E blog publish lane (cq-2577936d2809). CORA_DRIVE_ROOT is the important
    # one and it is here because it ALREADY BIT: a publish-card tap advances the
    # human editorial backlog row on Drive, and a card test that set a
    # backlog_row without stubbing drive_io wrote "PUBLISHED (gid 777)" -- a test
    # fixture's gid -- into Harrison's REAL backlog file at
    # G:\My Drive\HJR-Founder-OS\...\learn-editorial-backlog-v1.md. Redirecting
    # the mount ROOT is what makes it structural: a future test that forgets to
    # patch drive_io writes to tmp_path instead of to the Founder OS. Same class
    # as the golden-set line above, which carries the same "it did, once" note.
    monkeypatch.setenv("CORA_DRIVE_ROOT", str(tmp_path / "drive-root"))
    monkeypatch.setenv(
        "CORA_F3E_BLOG_STATE_PATH", str(tmp_path / "f3e-blog-pipeline-state.json")
    )
    monkeypatch.setenv(
        "CORA_F3E_BLOG_CARDS_PATH", str(tmp_path / "f3e-blog-card-events.jsonl")
    )
    monkeypatch.setenv(
        "CORA_F3E_BLOG_LEDGER_PATH", str(tmp_path / "f3e-blog-publish-ledger.jsonl")
    )
    # Deposco order-push pending store + ledger (SONNET-HANDOFF step 4) -- same
    # class as the F3E blog cards/ledger pair above: a per-id JSON store plus an
    # append-only ledger, both env-var-driven so a suite run can never write into
    # data/state/deposco-order-pending/ or the real push ledger.
    monkeypatch.setenv(
        "CORA_DEPOSCO_PENDING_DIR", str(tmp_path / "deposco-order-pending")
    )
    monkeypatch.setenv(
        "CORA_DEPOSCO_PUSH_LEDGER_PATH", str(tmp_path / "deposco-push-ledger.jsonl")
    )
    monkeypatch.setenv(
        "CORA_DEPOSCO_DEMOTION_STATE_PATH", str(tmp_path / "deposco-standing-demotion.json")
    )
    # WS-4 drive-extractor pause: .env carries DRIVE_EXTRACTOR_PROPOSALS_ENABLED=0
    # (the D-066 production pause) and config.py's import-time load_dotenv() pulls
    # it into the test process, short-circuiting run_proposal_loop and reddening
    # every proposal-path test. Clear it so tests run against the CODE default
    # (enabled); the pause-gate tests set/clear the var explicitly themselves.
    monkeypatch.delenv("DRIVE_EXTRACTOR_PROPOSALS_ENABLED", raising=False)
    # Same class, found 2026-08-24: .env carries CORA_MECHANICAL_REVIEW=on (the
    # 2026-08-21 flip), so every test that drives run_knowledge_review.main()
    # silently ran against the LIVE mechanical surface -- which diverts
    # hubspot_note/task_close/asana_task rows away from owner-routing. That
    # reddened test_operational_routed_not_dmd_to_harrison the moment Harrison
    # flipped the flag, and the suite stayed red for three days because nothing
    # re-ran it. Clear it so tests run against the CODE default (off); the
    # surface's own tests set it explicitly (test_review_lanes.py).
    monkeypatch.delenv("CORA_MECHANICAL_REVIEW", raising=False)
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
    # Confirm-buttons (2026-08-02): the 3 Class-B stashes + the ask_stash are
    # ALSO global, process-wide, in-memory dicts (same class of test-pollution
    # risk as the shopify/delegated stores above -- common test user ids like
    # HARRISON recur across many files). Clear before AND after every test.
    try:
        import cora.tools.tool_dispatch as _td2
        _td2._PENDING_REMEMBER.clear()
        _td2._PENDING_FORGET_NOTE.clear()
        _td2._PENDING_SCHEDULE_MEETING.clear()
        _td2._PENDING_ASK_STASH.clear()
    except Exception:
        pass
    try:
        from cora import confirm_cards as _cc
        with _cc._INDEX_LOCK:
            _cc._INDEX.clear()
        with _cc._ASK_INDEX_LOCK:
            _cc._ASK_INDEX.clear()
        # S1 fix (v1.1, 2026-08-02): _TURN_ID is a contextvars.ContextVar set via
        # begin_turn(). A test that calls it directly (not scoped to its own
        # copied Context) mutates pytest's single shared default context, which
        # otherwise leaks into every later test in the session (observed: it
        # made an unrelated test_confirm_cards.py assertion about a FRESH mint's
        # turn_id see a stale non-None value left by a test_confirm_dispatcher.py
        # test that ran earlier in the same process). Reset before AND after.
        _cc._TURN_ID.set(None)
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
    # LEX lane flags (2026-08-06). Same hazard as the three above, and it bites
    # HARDER: these are designed to be flipped ON in the live .env one at a time,
    # and every "LEX is refused by default" test asserts the OFF behaviour. Left
    # unpinned, Harrison flipping a lane turns the suite RED (~11 failures across
    # test_web_guard / test_delegated_work / test_gap_autofill / test_gap_detection)
    # and bricks the standing loop's never-commit-on-red gate on the very day the
    # lane goes live. These tests must pin the CODE default, not the ambient
    # environment; a test that needs a lane ON sets it explicitly.
    monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "off")
    monkeypatch.setenv("CORA_DELEGATED_WORK_LEX", "off")
    monkeypatch.setenv("CORA_GAP_ESCALATION_LEX", "off")
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
    # cq-eba0861fc043 (session #11 S2): these four resolve through os.environ.get()
    # at CALL time (friction_mining.py:178/183, gap_autofill.py:103/108), so they are
    # redirected here rather than in _LEDGER_CONSTS below -- there is no module
    # constant to patch. Un-redirected, apply_efficiency() appends to the real
    # design/efficiency-backlog.md, a shared human file.
    monkeypatch.setenv("EFFICIENCY_BACKLOG_PATH", str(tmp_path / "efficiency-backlog.md"))
    monkeypatch.setenv("FRICTION_LEDGER_PATH", str(tmp_path / "friction-fingerprints.jsonl"))
    monkeypatch.setenv("GAP_AUTOFILL_STATE_PATH", str(tmp_path / "gap_autofill_state.json"))
    monkeypatch.setenv("GAP_ASK_PENDING_PATH", str(tmp_path / "gap_ask_pending.json"))
    # Remaining unambiguous WRITE ledgers/dirs found by the session #11 S2 audit
    # (tests/test_test_prod_isolation.py enumerates the full surface and keeps the
    # read-only remainder declared rather than invisible).
    monkeypatch.setenv("CORA_DECISIONS_INBOX_PATH", str(tmp_path / "decisions-inbox.jsonl"))
    monkeypatch.setenv("MISSED_CATCHUP_LEDGER_PATH", str(tmp_path / "missed-catchup.jsonl"))
    monkeypatch.setenv("FILER_CONTENT_LEDGER_PATH", str(tmp_path / "filer-content.jsonl"))
    monkeypatch.setenv("FILER_MESSAGE_LEDGER_PATH", str(tmp_path / "filer-message.jsonl"))
    monkeypatch.setenv("CORA_GRADUATED_SHADOW_DIR", str(tmp_path / "graduated-shadow"))
    monkeypatch.setenv("LEXICON_CANDIDATES_PATH", str(tmp_path / "lexicon-candidates.jsonl"))
    monkeypatch.setenv("LEXICON_FINGERPRINTS_PATH", str(tmp_path / "lexicon-fingerprints.jsonl"))
    monkeypatch.setenv("SYNTHESIS_SNAPSHOT_DIR", str(tmp_path / "synthesis-snapshots"))
    monkeypatch.setenv("STRATEGY_SNAPSHOT_DIR", str(tmp_path / "strategy-snapshots"))
    monkeypatch.setenv("STRATEGY_MEMO_DIR", str(tmp_path / "strategy-memos"))
    monkeypatch.setenv("MATERIALIZATION_WATERMARK_PATH", str(tmp_path / "materialization-wm.json"))
    monkeypatch.setenv("CORA_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("CORA_SNAPSHOT_MIRROR_DIR", str(tmp_path / "snapshot-mirror"))
    monkeypatch.setenv("FLYWHEEL_MIRROR_DIR", str(tmp_path / "flywheel-mirror"))
    monkeypatch.setenv("LEXICON_ROSTER_PATH", str(tmp_path / "lexicon-roster.yaml"))
    monkeypatch.setenv("STRATEGY_HEARTBEAT_PATH", str(tmp_path / "strategy-heartbeat.json"))
    # Code #15 R8 (cq-592baba613f1): the weekly Drive-hygiene runner
    # (scripts/run_hygiene_drive_weekly.py) -- its inventory PS1, the audited
    # root, the Downloads out-dir (inventory CSVs, sidecars, desktop.ini
    # manifest, keep-4 rotation deletes), the Drive report dir and its own stamp
    # ledger. Redirected in the SAME commit that introduced the writer. The PS1
    # and root point at paths that do not exist, so a test that forgets its own
    # setup fails closed (no walk, no report) instead of reaching G:.
    monkeypatch.setenv("HYGIENE_INVENTORY_PS1", str(tmp_path / "hygiene" / "no-such-inventory.ps1"))
    monkeypatch.setenv("HYGIENE_AUDIT_ROOT", str(tmp_path / "hygiene" / "no-such-root"))
    monkeypatch.setenv("HYGIENE_AUDIT_OUTDIR", str(tmp_path / "hygiene" / "outdir"))
    monkeypatch.setenv("HYGIENE_REPORT_DIR", str(tmp_path / "hygiene" / "report-dir"))
    monkeypatch.setenv("HYGIENE_STAMP_LEDGER_PATH", str(tmp_path / "hygiene" / "stamps.jsonl"))
    # Code #13 slice 9b: the repeat-signal escalation ledger (cora.repeat_signal;
    # writers: the expected-invoice check, the nightly decision-gate check, the
    # decision-card tap). Redirected in the SAME commit that introduced it.
    monkeypatch.setenv("REPEAT_SIGNAL_LEDGER_PATH", str(tmp_path / "repeat-signals.jsonl"))
    # session #11 S4: the run-marker ledger. A new write path needs its conftest
    # redirect in the SAME commit that introduces it, or the next suite run is
    # what discovers the omission -- by writing to it.
    monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
    # Code #13 slice 2: the missed-nightly catch-up lane's own ledger (redirected
    # in the SAME commit that introduced the writer, per the S4 doctrine above).
    monkeypatch.setenv("NIGHTLY_CATCHUP_LEDGER_PATH", str(tmp_path / "nightly-catchup.jsonl"))
    # Code #13 Rider 1 S-A: the cora@ mailbox intake sweep's per-mailbox watermark
    # (writer: scripts/run_mailbox_intake_sweep.py --apply). Redirected in the
    # SAME patch that introduced the writer.
    monkeypatch.setenv("MAILBOX_INTAKE_WATERMARK_PATH",
                       str(tmp_path / "mailbox-intake-watermark.json"))
    # Code #14 S2: the nightly health check's run-artifact directory (writer:
    # scripts/nightly_health_check.main on a real run; pruner: scripts/compact_logs
    # prune_reports). Redirected in the SAME commit that introduced the writer --
    # a main() test with argv=[] reaches the artifact write.
    monkeypatch.setenv("CORA_HEALTH_REPORT_DIR", str(tmp_path / "health-reports"))
    # Found by WIDENING the isolation rail's suffix list to include *_LEDGER
    # (session #11 S4). Both were live, unredirected write paths that the
    # PATH/DIR/ROOT-only scanner could not see -- and decision_inbox has TWO env
    # vars, so redirecting only its *_PATH half had left the other half open.
    monkeypatch.setenv("CORA_DECISIONS_INBOX_LEDGER", str(tmp_path / "decisions-inbox-ledger.jsonl"))
    monkeypatch.setenv("CORA_MEETING_CAPTURE_LEDGER", str(tmp_path / "meeting-capture-ledger.jsonl"))
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
        # Code #12 C3: the Monday-menu run artifact (maybe_send_weekly_menu writes
        # one row per fire). A new write path needs its redirect the day it is born.
        ("cora.code_queue", "_MENU_RUNS_LEDGER", "code-queue-menu-runs.jsonl"),
        # Code #12 S3' coverage: the bot's armed-rails record (egress_rails.record_armed).
        ("cora.egress_rails", "ARMED_STATE_PATH", "egress-rails-armed.json"),
        # Code #14 S3 (cq-439d89a84de4): the phantom-claim adjudication ledger. Rows
        # are written only when slack_egress.arm_rail_ledger() ran (the bot's main);
        # this redirect is the belt behind that gate, born with the write path.
        ("cora.slack_egress", "PHANTOM_CLAIMS_LEDGER", "phantom-write-claims.jsonl"),
        # Code #13 slice 9 integration: decision_lane's delivery ledger is a BARE
        # module constant (no env override) read at call time by delivery_index /
        # record_delivery -- the nightly decision-gate check now records a ping row
        # through it every run, and the file sat in _GUARDED_LEDGERS (detect-only)
        # with no redirect. Surfaced as a session-guard error in a fresh worktree.
        ("cora.decision_lane", "DELIVERY_LEDGER", "decision-deliveries.jsonl"),
        ("cora.knowledge_review", "_AUTOWRITE_AUDIT_PATH", "cora-autowrite-audit.jsonl"),
        # cq-eba0861fc043 (session #11 S2): these THREE sat un-redirected right beside
        # _AUTOWRITE_AUDIT_PATH above. propose_update() appends to
        # _PROPOSED_UPDATES_PATH unconditionally, so a suite run put two synthetic
        # "gapfill-*" proposals into the LIVE review ledger; one was later one-tapped
        # into live canon (fndr.md on Drive). The file WAS in _GUARDED_LEDGERS -- so it
        # was detect-only, and the detector is defeated on this host (see _bot_live).
        # The same constant took a lexicon-teach fixture on 2026-08-02, three weeks
        # before the incident that got noticed.
        ("cora.knowledge_review", "_PROPOSED_UPDATES_PATH", "cora-proposed-memory-updates.jsonl"),
        ("cora.knowledge_review", "_ARCHIVE_PATH", "cora-proposed-memory-updates.archive.jsonl"),
        ("cora.knowledge_review", "_REPLY_LOG_PATH", "cora-reply-log.jsonl"),

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
        # D-051 review of session #11 S2: the env-var rail below is BLIND to
        # module constants, and these three were writing real files during green
        # runs. photoroom's path is the worst shape -- Path("logs/...") is
        # RELATIVE, so it resolves against the pytest CWD; a 30-test file was
        # measured appending 10 rows to the real spend ledger. main._HEARTBEAT_FILE
        # is the most dangerous: _guard_logs_untouched reads that same file to
        # decide _bot_live, so a test writing it would permanently DISARM the
        # backstop. revops _AUDIT_PATH is the email SEND audit.
        ("cora.connectors.photoroom_client", "_SPEND_LOG_PATH", "photoroom-spend.jsonl"),
        ("cora.revops.sender", "_AUDIT_PATH", "cora-send-audit.jsonl"),
        ("cora.main", "_HEARTBEAT_FILE", "heartbeat.txt"),
    ]
    for _mod_name, _attr, _fname in _LEDGER_CONSTS:
        try:
            _mod = _importlib.import_module(_mod_name)
            if hasattr(_mod, _attr):
                monkeypatch.setattr(_mod, _attr, tmp_path / _fname, raising=False)
        except Exception:
            pass
    # SCRIPT-module constants (D-051 lens D HIGH #3 / lens E F7): the loop above
    # imports by name, which for a `scripts.*` module would run its import-time
    # load_dotenv(override=True) on EVERY test. So these are redirected only when a
    # test has ALREADY imported the script -- the only way it could write through
    # them. Found: 10 pre-existing tests reach the C2 batch-card writer through
    # rkr.main() with a stubbed DM sender; on a Monday they would stamp the REAL
    # data/state/mechanical-batch-card.json and suppress the real 07:00 card.
    import sys as _sys
    _SCRIPT_CONSTS = [
        ("scripts.run_knowledge_review", "_MECHANICAL_BATCH_STATE_PATH", "mechanical-batch-card.json"),
        ("run_knowledge_review", "_MECHANICAL_BATCH_STATE_PATH", "mechanical-batch-card.json"),
        # Code #15 D-051 r1 lens-dryrun#0: the N2 run lock. ~15 tests drive
        # rkr.main() without --dry-run; each TAKES the lock (os.open O_EXCL) and
        # registers its release at interpreter exit. Unredirected, a test that
        # did not patch _LOCK_PATH itself created -- and at exit unlinked -- the
        # REAL data/state/knowledge-review.lock, which in the primary checkout is
        # the live 07:00 run's race guard. main() now also binds the path at
        # registration, so the exit hook unlinks the lock the run actually took.
        ("scripts.run_knowledge_review", "_LOCK_PATH", "knowledge-review.lock"),
        ("run_knowledge_review", "_LOCK_PATH", "knowledge-review.lock"),
    ]
    for _mod_name, _attr, _fname in _SCRIPT_CONSTS:
        _mod = _sys.modules.get(_mod_name)
        if _mod is not None and hasattr(_mod, _attr):
            monkeypatch.setattr(_mod, _attr, tmp_path / _fname, raising=False)
    # Code #15 RIDER B (cq-59c5048d0891): the combined KB purge script's Drive lanes
    # build a REAL Drive service on first use -- after load_dotenv(<repo>/.env,
    # override=True), which would overwrite this fixture's env redirects with the live
    # .env values. Whenever a test has loaded the script (the tests load it as
    # purge_kb_code15_2026_09), its factory is replaced by one that raises, so no
    # test reaches a real Drive or the repo .env; a test that needs Drive installs an
    # in-memory fake on top. (Its only new WRITE path -- the id-only folder manifests
    # -- lives under the CORA_KB_PURGE_OUT_DIR redirect above.)
    _kbp = _sys.modules.get("purge_kb_code15_2026_09")
    if _kbp is not None and hasattr(_kbp, "DRIVE_SERVICE_FACTORY"):
        def _no_real_drive():
            raise RuntimeError("tests/conftest.py: the real Drive service is disabled in tests")
        monkeypatch.setattr(_kbp, "DRIVE_SERVICE_FACTORY", _no_real_drive, raising=False)
    # Code #15 rider (c): D-243 fixture pollution -- three writers found by the
    # 2026-09-24 worktree ROOT differential (touch a marker, run the suite, list
    # logs/ data/ design/ newer than it), each with measured LIVE damage in the
    # primary: hygiene-deferred.jsonl 22,000 of 22,117 rows fixture,
    # cora-finance-queries.jsonl 2,448 of 2,505 rows fixture, and the
    # info-for-cora run-state stamped fresh by a test (nightly_health_check then
    # reads a dead sweep as alive for 48h). Self-contained; a test that patches
    # one of these itself still wins (its monkeypatch runs after this one).
    #   (1)+(3) SCRIPT modules: same sys.modules-only rule as _SCRIPT_CONSTS above
    #   (importing a script here would run its import-time load_dotenv on every
    #   test). Both spellings are listed because the suite imports
    #   each script BOTH ways (`import run_info_for_cora_sweep` in
    #   test_info_for_cora_liveness, `import scripts.run_info_for_cora_sweep` in
    #   test_info_for_cora_sweep) and those are two distinct module objects. Every
    #   current importer does so at module top, i.e. at COLLECTION, so the module
    #   is in sys.modules before this fixture first runs (pinned by
    #   tests/test_conftest_d243_rider_c.py). A future LAZY importer is caught by
    #   the _GUARDED_LEDGERS rows added below, not silently missed. THROTTLE_FILE,
    #   _WATERMARK_PATH and _LOCK_PATH ride as belts beside the measured writers:
    #   same modules, same class, every existing writer test already patches them.
    for _mod_name, _attr, _fname in (
        ("run_asana_hygiene_nudges", "DEFERRED_FILE", "hygiene-deferred.jsonl"),
        ("scripts.run_asana_hygiene_nudges", "DEFERRED_FILE", "hygiene-deferred.jsonl"),
        ("run_asana_hygiene_nudges", "THROTTLE_FILE", "hygiene_nudge_throttle.json"),
        ("scripts.run_asana_hygiene_nudges", "THROTTLE_FILE", "hygiene_nudge_throttle.json"),
        ("run_info_for_cora_sweep", "_RUNSTATE_PATH", "info-for-cora-runstate.json"),
        ("scripts.run_info_for_cora_sweep", "_RUNSTATE_PATH", "info-for-cora-runstate.json"),
        ("run_info_for_cora_sweep", "_WATERMARK_PATH", "info-for-cora-watermark.json"),
        ("scripts.run_info_for_cora_sweep", "_WATERMARK_PATH", "info-for-cora-watermark.json"),
        ("run_info_for_cora_sweep", "_LOCK_PATH", "info_for_cora_sweep.lock"),
        ("scripts.run_info_for_cora_sweep", "_LOCK_PATH", "info_for_cora_sweep.lock"),
    ):
        _mod = _sys.modules.get(_mod_name)
        if _mod is not None and hasattr(_mod, _attr):
            monkeypatch.setattr(_mod, _attr, tmp_path / _fname, raising=False)
    #   (2) src module cora.tools.financial_client: _audit_log_path() and
    #   _throttle_path() are FUNCTIONS of _repo_root(), so the redirect is at that
    #   ROOT (D-243: mount root, not per path) -- both files, and any future path
    #   built on it, land under tmp. tests/test_financial_client.py's own autouse
    #   _repo_root patch (and every per-test _throttle_path patch) still wins.
    try:
        import cora.tools.financial_client as _fcl
        monkeypatch.setattr(_fcl, "_repo_root", lambda: tmp_path / "financial-client-root",
                            raising=False)
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
        _td._PENDING_REMEMBER.clear()
        _td._PENDING_FORGET_NOTE.clear()
        _td._PENDING_SCHEDULE_MEETING.clear()
        _td._PENDING_ASK_STASH.clear()
    except Exception:
        pass
    try:
        from cora import confirm_cards as _cc
        with _cc._INDEX_LOCK:
            _cc._INDEX.clear()
        with _cc._ASK_INDEX_LOCK:
            _cc._ASK_INDEX.clear()
        _cc._TURN_ID.set(None)
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
    "data/state/meeting-ask-pending.json",
    "data/state/meeting-ask-watermark.json",
    # Code #13 slice 5: the recap card store + its per-recipient send ledger.
    "data/state/meeting-recap-pending.jsonl",
    "logs/meeting-recap-ledger.jsonl",
    # One Cora capture lane (cq-ffcf6e4ffe7c). Both the ensure lane and the daily
    # auditor append here, so an unredirected test would write real rows.
    "logs/meeting-capture-ledger.jsonl",
    "data/state/web-search-usage.jsonl",
    # Lexicon Flywheel: the chokepoint telemetry ledger + the three files of
    # record the review-rail writer may append to (writer tests must redirect
    # via LEXICON_* env vars; a mutation here means a test escaped isolation).
    "logs/lexicon-resolutions.jsonl",
    "data/maps/f3e-sku-aliases.yaml",
    "data/maps/user-aliases.yaml",
    "data/state/delegated-work.jsonl",
    "data/state/delegated-work-runner.jsonl",
    # Instance ledger (cq-7915a8647cff): the live bot appends a start row per
    # process. A suite-run mutation means a test wrote the real path instead of
    # monkeypatching instance_ledger.LEDGER_FILE.
    "logs/cora-instances.jsonl",
    "data/health/instance.json",
    "logs/decision-deliveries.jsonl",
    # Code #13 slice 9b: the repeat-signal escalation ledger (cora.repeat_signal).
    "logs/repeat-signals.jsonl",
    "logs/fireflies-diarization.jsonl",
    # Code #12 (D-051 review): the three write paths this bundle added.
    "data/state/code-queue-menu-runs.jsonl",
    "data/state/mechanical-batch-card.json",
    "data/state/egress-rails-armed.json",
    "data/state/phantom-write-claims.jsonl",
    # Code #13 slice 2: the missed-nightly catch-up ledger (writer: scripts/check_missed_nightly.py).
    "logs/nightly-catchup.jsonl",
    # Code #13 Rider 1 S-A: the cora@ mailbox intake sweep's watermark (writer:
    # scripts/run_mailbox_intake_sweep.py --apply; redirected via MAILBOX_INTAKE_WATERMARK_PATH).
    "data/state/mailbox-intake-watermark.json",
    # Code #15 rider (c): the D-243 root-differential writers redirected in the
    # autouse fixture above (see the "Code #15 rider (c)" block there).
    "data/state/hygiene-deferred.jsonl",
    "data/state/hygiene_nudge_throttle.json",
    "logs/cora-finance-queries.jsonl",
    "data/cache/finance-notify-throttle.json",
    "data/state/info-for-cora-runstate.json",
    "data/state/info-for-cora-watermark.json",
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
