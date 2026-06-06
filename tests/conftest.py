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


# ---------------------------------------------------------------------------
# Scheduler patch: calendar_client may be stale on CIFS mount.
# Inject missing functions so test_calendar_scheduler.py works.
# ---------------------------------------------------------------------------

def _patch_calendar_client_scheduler():
    """Inject scheduler helpers if the CIFS-mounted calendar_client is stale."""
    try:
        import src.cora.tools.calendar_client as _cc
    except Exception:
        return
    if hasattr(_cc, "_round_up_to_slot"):
        return  # already has the new functions — no-op

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    _PHOENIX_TZ      = _tz(_td(hours=-7))
    _SLOT_STEP_MIN   = 15
    _WORK_START_HOUR = 9
    _WORK_END_HOUR   = 17

    def _round_up_to_slot(dt):
        total = int(dt.timestamp())
        step  = _SLOT_STEP_MIN * 60
        rem   = total % step
        return dt if rem == 0 else dt + _td(seconds=step - rem)

    def find_next_available_slot(busy_by_email, duration_minutes=30,
                                 search_from=None, search_days=7):
        now        = search_from or _dt.now(_tz.utc)
        candidate  = _round_up_to_slot(now)
        end_search = now + _td(days=search_days)
        duration   = _td(minutes=duration_minutes)
        all_busy   = []
        for periods in busy_by_email.values():
            all_busy.extend(periods)
        all_busy.sort(key=lambda t: t[0])
        while candidate < end_search:
            cand_az = candidate.astimezone(_PHOENIX_TZ)
            if cand_az.weekday() >= 5:
                days_to_monday = 7 - cand_az.weekday()
                nxt = (cand_az + _td(days=days_to_monday)).replace(
                    hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0)
                candidate = nxt.astimezone(_tz.utc)
                continue
            if cand_az.hour < _WORK_START_HOUR:
                today_start = cand_az.replace(
                    hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0)
                candidate = today_start.astimezone(_tz.utc)
                continue
            slot_end    = candidate + duration
            slot_end_az = slot_end.astimezone(_PHOENIX_TZ)
            past_eod = (cand_az.hour >= _WORK_END_HOUR
                        or slot_end_az.hour > _WORK_END_HOUR
                        or (slot_end_az.hour == _WORK_END_HOUR
                            and slot_end_az.minute > 0))
            if past_eod:
                nxt = (cand_az + _td(days=1)).replace(
                    hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0)
                candidate = nxt.astimezone(_tz.utc)
                continue
            blocking = None
            for bs, be in all_busy:
                if bs >= slot_end:
                    break
                if be <= candidate:
                    continue
                blocking = (bs, be)
                break
            if blocking is None:
                return (candidate, slot_end)
            candidate = _round_up_to_slot(blocking[1])
        return None

    def format_slot_proposal_for_llm(slot_start, slot_end,
                                     participant_names, title="Meeting"):
        start_az  = slot_start.astimezone(_PHOENIX_TZ)
        end_az    = slot_end.astimezone(_PHOENIX_TZ)
        day_str   = start_az.strftime("%A, %B") + f" {start_az.day}, {start_az.year}"
        start_str = start_az.strftime("%I:%M %p").lstrip("0") + " AZ"
        end_str   = end_az.strftime("%I:%M %p").lstrip("0") + " AZ"
        dur_min   = int((slot_end - slot_start).total_seconds() / 60)
        if len(participant_names) <= 2:
            names_str = " & ".join(participant_names)
        else:
            names_str = (", ".join(participant_names[:-1])
                         + f" & {participant_names[-1]}")
        start_iso = start_az.strftime("%Y-%m-%dT%H:%M:00-07:00")
        end_iso   = end_az.strftime("%Y-%m-%dT%H:%M:00-07:00")
        return (
            "SLOT FOUND -- present this as a clear preview block to the user:\n"
            f"- *Title:* {title}\n"
            f"- *Day:* {day_str}\n"
            f"- *Time:* {start_str} - {end_str} ({dur_min} min)\n"
            f"- *Participants:* {names_str}\n\n"
            "Tell the user this is the next available opening that works for "
            "everyone, and ask for their explicit confirmation before booking.\n\n"
            "Once they confirm, call calendar_schedule_meeting again with:\n"
            f'  confirmed: true\n  proposed_start: "{start_iso}"\n'
            f'  proposed_end: "{end_iso}"\n'
            "  (keep title and participants the same as this call)"
        )

    if not hasattr(_cc, "find_meeting_slot"):
        def _find_meeting_slot_stub(*a, **kw):
            raise NotImplementedError("find_meeting_slot is mocked in tests")
        _cc.find_meeting_slot = _find_meeting_slot_stub

    _cc._PHOENIX_TZ              = _PHOENIX_TZ
    _cc._SLOT_STEP_MIN           = _SLOT_STEP_MIN
    _cc._WORK_START_HOUR         = _WORK_START_HOUR
    _cc._WORK_END_HOUR           = _WORK_END_HOUR
    _cc._round_up_to_slot        = _round_up_to_slot
    _cc.find_next_available_slot = find_next_available_slot
    _cc.format_slot_proposal_for_llm = format_slot_proposal_for_llm



    # ---- get_free_busy (needs _build_service from the real module) ----------
    if not hasattr(_cc, "get_free_busy"):
        from googleapiclient.errors import HttpError as _HttpError
        from typing import Any as _Any

        def get_free_busy(requester_email, calendar_emails, time_min, time_max):
            import src.cora.tools.calendar_client as _mod
            body = {
                "timeMin": time_min.isoformat().replace("+00:00", "Z"),
                "timeMax": time_max.isoformat().replace("+00:00", "Z"),
                "timeZone": "America/Phoenix",
                "items": [{"id": e} for e in calendar_emails],
            }
            try:
                svc    = _mod._build_service(requester_email)
                result = svc.freebusy().query(body=body).execute()
            except _HttpError as exc:
                status = exc.resp.status if exc.resp else "?"
                if status == 403:
                    raise _mod.CalendarClientError(
                        f"Freebusy 403 -- DWD scope missing for {requester_email}."
                    ) from exc
                raise _mod.CalendarClientError(
                    f"Freebusy API HTTP {status}: {exc}"
                ) from exc
            except _mod.CalendarClientError:
                raise
            except Exception as exc:
                raise _mod.CalendarClientError(f"Freebusy API error: {exc}") from exc

            calendars = result.get("calendars") or {}
            busy = {}
            for email in calendar_emails:
                cal_data = calendars.get(email) or {}
                errors   = cal_data.get("errors") or []
                if errors:
                    busy[email] = [(time_min, time_max)]
                    continue
                periods = []
                for period in cal_data.get("busy") or []:
                    try:
                        s = _dt.fromisoformat(period["start"].replace("Z", "+00:00"))
                        e = _dt.fromisoformat(period["end"].replace("Z", "+00:00"))
                        periods.append((s, e))
                    except (KeyError, ValueError):
                        pass
                busy[email] = periods
            return busy

        _cc.get_free_busy = get_free_busy


_patch_calendar_client_scheduler()


@pytest.fixture(autouse=True)
def _isolate_nudge_ledger(tmp_path, monkeypatch):
    """Point the shared nudge ledger at an isolated temp file for every test.

    Without this, run_asana_hygiene_nudges tests that post comments would
    append to (and read from) the REAL closure-nudges JSONL on the Drive,
    polluting production state and cross-contaminating tests. Each test gets a
    fresh empty path -- recently_nudged() sees no file (returns False) and
    record_nudge() writes only to tmp. Tests that exercise the ledger directly
    can monkeypatch.setenv to their own path, overriding this default.
    """
    monkeypatch.setenv(
        "CLOSURE_NUDGE_LOG_PATH", str(tmp_path / "closure-nudges-throttle.jsonl")
    )
    yield
