"""Meet join audit seam (Code #14 R14-8) -- READ-ONLY Reports API reader.

Every test drives the REAL read / parse / classify code over a fake Reports
service; the suite-wide conftest redirect makes sure nothing here can mint a
token or reach the endpoint (dark:test). Fixtures are synthetic (D-145): the
addresses are .test placeholders, the names are invented.
"""
from __future__ import annotations

import inspect
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora.connectors import meet_audit as ma  # noqa: E402

T0 = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)


def _event(code="VOTCADENCE", *, ident="pal@example.test", name="Pal Example",
           endpoint="ep-01", cal_id="gc-1_20260826T163000Z", start=1_787_770_800,
           dur=1500, ip="203.0.113.9", with_code=True, with_endpoint=True):
    params = [
        {"name": "calendar_event_id", "value": cal_id},
        {"name": "identifier", "value": ident},
        {"name": "identifier_type", "value": "email_address"},
        {"name": "display_name", "value": name},
        {"name": "ip_address", "value": ip},
        {"name": "location_country", "value": "ZZQ"},
        {"name": "duration_seconds", "intValue": str(dur)},
        {"name": "start_timestamp_seconds", "intValue": str(start)},
        {"name": "conference_id", "value": "conf-xyz"},
    ]
    if with_code:
        params.append({"name": "meeting_code", "value": code})
    if with_endpoint:
        params.append({"name": "endpoint_id", "value": endpoint})
    return {
        "id": {"time": datetime.fromtimestamp(start + dur, timezone.utc)
               .strftime("%Y-%m-%dT%H:%M:%S.000Z"), "applicationName": "meet"},
        "actor": {"email": ident, "profileId": "prof-9z"},
        "ipAddress": ip,
        "events": [{"type": "call", "name": "call_ended", "parameters": params}],
    }


class _Svc:
    """A fake Reports service: records every list() kwargs, serves pages in order
    (or raises), and supports the chained activities().list(...).execute() call."""

    def __init__(self, pages=None, *, exc=None):
        self.pages = list(pages or [{"items": []}])
        self.exc = exc
        self.calls: list[dict] = []

    def activities(self):
        return self

    def list(self, **kw):
        self.calls.append(kw)
        return self

    def execute(self, num_retries=0):
        if self.exc is not None:
            raise self.exc
        return self.pages[len(self.calls) - 1]


def _http_error(status, body: bytes):
    import httplib2
    from googleapiclient.errors import HttpError

    return HttpError(resp=httplib2.Response({"status": status}), content=body)


# ── the credential ───────────────────────────────────────────────────────────

class _FakeCreds:
    def __init__(self, log):
        self.log = log

    def with_subject(self, subject):
        self.log.append(("with_subject", subject))
        return self


@pytest.fixture
def cred_capture(monkeypatch, tmp_path):
    key = tmp_path / "sa.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(key))
    log: list = []
    from google.oauth2 import service_account

    def _from_file(path, scopes=None):
        log.append(("scopes", list(scopes or [])))
        return _FakeCreds(log)

    monkeypatch.setattr(service_account.Credentials, "from_service_account_file",
                        staticmethod(_from_file))
    import googleapiclient.discovery as disc

    monkeypatch.setattr(disc, "build", lambda *a, **k: log.append(("build", a)) or "svc")
    return log


def test_requests_exactly_the_reports_audit_scope_on_its_own_credential(cred_capture):
    assert ma._build_reports_service_impl() == "svc"
    assert ("scopes", ["https://www.googleapis.com/auth/admin.reports.audit.readonly"]) in cred_capture
    assert ("build", ("admin", "reports_v1")) in cred_capture
    assert ma._SCOPES == [ma.REPORTS_SCOPE]


def test_subject_defaults_to_harrison_resolved_per_call(cred_capture, monkeypatch):
    ma._build_reports_service_impl()
    assert ("with_subject", "harrison@hjrglobal.com") in cred_capture
    monkeypatch.setenv(ma.ADMIN_SUBJECT_ENV, "Admin.Two@HJRGlobal.com ")
    ma._build_reports_service_impl()
    assert ("with_subject", "admin.two@hjrglobal.com") in cred_capture


@pytest.mark.parametrize("subject", ["cora@hjrglobal.com", " CORA@hjrglobal.com", "not-an-address"])
def test_cora_is_hard_refused_and_never_reaches_with_subject(cred_capture, monkeypatch, subject):
    """D-307: the capture identity is never an admin subject. The refusal happens
    before any credential is loaded, and a read reports it as an error (so the
    health check WARNs) with a reason class that carries no address."""
    monkeypatch.setenv(ma.ADMIN_SUBJECT_ENV, subject)
    with pytest.raises(ma.MeetAuditRefused):
        ma._build_reports_service_impl()
    assert cred_capture == []
    monkeypatch.setattr(ma, "_build_reports_service", ma._build_reports_service_impl)
    r = ma.read_call_ended(T0, T1)
    assert r.state == ma.STATE_ERROR and r.reason == "subject refused"
    assert cred_capture == [] and "cora" not in r.reason


def test_missing_key_file_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    monkeypatch.setattr(ma, "_build_reports_service", ma._build_reports_service_impl)
    r = ma.read_call_ended(T0, T1)
    assert r.state == ma.STATE_ERROR and "not configured" in r.reason


def test_the_suite_redirect_keeps_every_default_read_dark(monkeypatch):
    """No test can mint a token: with no service passed, the conftest redirect
    answers dark:test and the real builder never runs."""
    called: list = []
    monkeypatch.setattr(ma, "_build_reports_service_impl", lambda: called.append(1))
    r = ma.read_call_ended(T0, T1)
    assert r.state == ma.STATE_DARK_TEST and r.joins == () and called == []
    assert ma.probe_lane().state == ma.STATE_DARK_TEST


# ── only Meet is ever queried ────────────────────────────────────────────────

def test_only_meet_is_ever_queried_source_pin():
    """The scope reads EVERY audit log; this pin is the narrowing. Exactly one
    list() call site, its applicationName is the module constant, the constant is
    'meet', and no other applicationName value appears anywhere in the module."""
    import ast

    tree = ast.parse(inspect.getsource(ma))
    assert ma._APPLICATION == "meet" and ma._EVENT_NAME == "call_ended" and ma._USER_KEY == "all"
    # exactly one `<x>.activities().list(...)` call in CODE (docstrings do not count)
    list_calls = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "list" and isinstance(n.func.value, ast.Call)
                  and isinstance(n.func.value.func, ast.Attribute)
                  and n.func.value.func.attr == "activities"]
    assert len(list_calls) == 1
    # every applicationName keyword in the module is the constant -- no literal,
    # no parameter, and no string key that could smuggle one in via a dict
    kws = [k for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords
           if k.arg == "applicationName"]
    assert len(kws) == 1 and isinstance(kws[0].value, ast.Name) and kws[0].value.id == "_APPLICATION"
    consts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
              and isinstance(n.value, str) and n.value == "applicationName"]
    assert consts == []
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "_APPLICATION" for t in n.targets)]
    assert len(assigns) == 1 and isinstance(assigns[0].value, ast.Constant)
    assert assigns[0].value.value == "meet"
    sig = inspect.signature(ma.read_call_ended)
    assert "application" not in " ".join(sig.parameters).lower()


def test_list_call_carries_meet_call_ended_all_users_and_the_window():
    svc = _Svc()
    r = ma.read_call_ended(T0, T1, service=svc)
    assert r.state == ma.STATE_LIVE and len(svc.calls) == 1
    kw = svc.calls[0]
    assert kw["applicationName"] == "meet" and kw["eventName"] == "call_ended"
    assert kw["userKey"] == "all" and kw["maxResults"] == 1000
    assert kw["startTime"] == "2026-08-26T06:00:00.000Z"
    assert kw["endTime"] == "2026-08-27T13:00:00.000Z"
    assert "pageToken" not in kw


# ── lane detection ───────────────────────────────────────────────────────────

def test_unauthorized_client_refresh_error_is_dark_scope():
    from google.auth.exceptions import RefreshError

    exc = RefreshError(
        "unauthorized_client: Client is unauthorized to retrieve access tokens using "
        "this method, or client not authorized for any of the scopes requested.",
        {"error": "unauthorized_client"},
    )
    r = ma.read_call_ended(T0, T1, service=_Svc(exc=exc))
    assert r.state == ma.STATE_DARK_SCOPE and r.joins == ()
    assert "unauthorized" not in r.reason.lower()      # a class, not the text


def test_other_refresh_error_is_an_error():
    from google.auth.exceptions import RefreshError

    r = ma.read_call_ended(T0, T1, service=_Svc(exc=RefreshError("invalid_grant: x")))
    assert r.state == ma.STATE_ERROR and r.reason == "token refresh failed"


def test_403_not_authorized_is_dark_subject():
    body = (b'{"error":{"code":403,"message":"Not Authorized to access this resource/api",'
            b'"errors":[{"message":"Not Authorized to access this resource/api",'
            b'"domain":"global","reason":"forbidden"}]}}')
    r = ma.read_call_ended(T0, T1, service=_Svc(exc=_http_error(403, body)))
    assert r.state == ma.STATE_DARK_SUBJECT


def test_access_not_configured_is_dark_api():
    body = (b'{"error":{"code":403,"message":"Admin SDK API has not been used in project '
            b'1234 before or it is disabled.","errors":[{"reason":"accessNotConfigured"}],'
            b'"status":"PERMISSION_DENIED","details":[{"reason":"SERVICE_DISABLED"}]}}')
    r = ma.read_call_ended(T0, T1, service=_Svc(exc=_http_error(403, body)))
    assert r.state == ma.STATE_DARK_API


def test_other_http_error_is_error_with_status_only():
    body = b'{"error":{"code":500,"message":"backend error for harrison@hjrglobal.com"}}'
    r = ma.read_call_ended(T0, T1, service=_Svc(exc=_http_error(500, body)))
    assert r.state == ma.STATE_ERROR and r.reason == "http 500"
    assert "@" not in r.reason


def test_an_unexpected_exception_never_escapes():
    r = ma.read_call_ended(T0, T1, service=_Svc(exc=ValueError("boom pal@example.test")))
    assert r.state == ma.STATE_ERROR and r.reason == "ValueError"


# ── pagination ───────────────────────────────────────────────────────────────

def test_pagination_runs_to_exhaustion():
    pages = [
        {"items": [_event(endpoint="ep-1")], "nextPageToken": "p2"},
        {"items": [_event(endpoint="ep-2")], "nextPageToken": "p3"},
        {"items": [_event(endpoint="ep-3")]},
    ]
    svc = _Svc(pages)
    r = ma.read_call_ended(T0, T1, service=svc)
    assert r.state == ma.STATE_LIVE and r.pages == 3 and r.events_read == 3
    assert [c.get("pageToken") for c in svc.calls] == [None, "p2", "p3"]
    assert len({j.endpoint_key for j in r.joins}) == 3


def test_page_cap_is_partial_never_live():
    pages = [{"items": [_event()], "nextPageToken": f"p{i}"} for i in range(5)]
    r = ma.read_call_ended(T0, T1, service=_Svc(pages), max_pages=3)
    assert r.state == ma.STATE_PARTIAL and r.pages == 3 and not r.decisive
    assert "page cap" in r.reason


def test_garbage_items_are_skipped_not_fatal():
    pages = [{"items": ["x", {"events": "nope"}, {"events": [{"name": "call_started"}]},
                        _event(with_code=False, cal_id=""), _event(with_endpoint=False, ident=""),
                        _event()]}]
    r = ma.read_call_ended(T0, T1, service=_Svc(pages))
    assert r.state == ma.STATE_LIVE
    assert r.events_read == 3 and len(r.joins) == 1       # only the usable one joins


# ── parse: identifiers dropped ───────────────────────────────────────────────

def test_identifiers_are_dropped_at_parse():
    r = ma.read_call_ended(T0, T1, service=_Svc([{"items": [_event()]}]))
    (j,) = r.joins
    flat = repr(r)
    for leak in ("pal@example.test", "Pal Example", "203.0.113.9", "ep-01", "prof-9z", "ZZQ"):
        assert leak not in flat, leak
    assert re.fullmatch(r"[0-9a-f]{12}", j.endpoint_key)
    assert j.meeting_code == "votcadence" and j.calendar_event_id == "gc-1_20260826T163000Z"
    assert j.start_ts == 1_787_770_800 and j.end_ts == 1_787_770_800 + 1500
    assert j.is_human is True
    # the probe's key-name view carries names, never values
    assert "identifier" in r.param_keys and "pal@example.test" not in " ".join(r.param_keys)


@pytest.mark.parametrize("ident,name", [
    ("cora@hjrglobal.com", "Cora Global"),
    ("notetaker@fireflies.ai", "Fireflies.ai Notetaker"),
    ("fred@fireflies.ai", "Fred"),
    ("", "Cora NoteTaker"),
    ("x@example.test", "Fireflies Notetaker (Pal)"),
])
def test_notetaker_endpoints_are_not_human(ident, name):
    r = ma.read_call_ended(T0, T1, service=_Svc([{"items": [_event(ident=ident, name=name)]}]))
    assert [j.is_human for j in r.joins] == [False]


def test_missing_start_is_derived_from_end_and_duration():
    ev = _event()
    params = ev["events"][0]["parameters"]
    ev["events"][0]["parameters"] = [p for p in params if p["name"] != "start_timestamp_seconds"]
    (j,) = ma.read_call_ended(T0, T1, service=_Svc([{"items": [ev]}])).joins
    assert j.start_ts == 1_787_770_800 and j.end_ts == 1_787_770_800 + 1500


# ── meeting-code normalization ───────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "https://meet.google.com/vot-cade-nce", "https://meet.google.com/vot-cade-nce?authuser=1",
    "VOT-CADE-NCE", "votcadence", " vot cade nce ", "meet.google.com/VOT-cade-nce/",
])
def test_normalize_meeting_code(raw):
    assert ma.normalize_meeting_code(raw) == "votcadence"


def test_normalize_meeting_code_is_linear_on_degenerate_inputs():
    """D-171: re-time the one regex on 40k of what its class eats and of what it
    keeps, plus a URL-shaped degenerate. Growth shape: 4x input must not cost
    more than ~8x time."""
    for ch in (" ", "-", "a", "/", "?"):
        for s in (ch * 40_000, "meet.google.com/" + ch * 40_000):
            t = time.perf_counter()
            ma.normalize_meeting_code(s)
            assert time.perf_counter() - t < 0.2, repr(ch)
    small, big = "-a" * 10_000, "-a" * 40_000
    t = time.perf_counter(); ma.normalize_meeting_code(small); ts = time.perf_counter() - t
    t = time.perf_counter(); ma.normalize_meeting_code(big); tb = time.perf_counter() - t
    assert tb < max(ts * 8, 0.05)


def test_probe_lane_reads_the_last_hour_through_the_same_seam():
    svc = _Svc()
    r = ma.probe_lane(service=svc)
    assert r.state == ma.STATE_LIVE
    kw = svc.calls[0]
    start = datetime.strptime(kw["startTime"], "%Y-%m-%dT%H:%M:%S.000Z")
    end = datetime.strptime(kw["endTime"], "%Y-%m-%dT%H:%M:%S.000Z")
    assert end - start == timedelta(hours=1)


# -- the staged read-only probe (scripts/probe_meet_audit.py) --

def _load_probe():
    """Exec the script, then restore os.environ: its load_dotenv(override=True)
    must not leak a live .env into later tests."""
    import importlib.util
    import os

    saved = dict(os.environ)
    spec = importlib.util.spec_from_file_location(
        "_probe_meet_audit", _REPO / "scripts" / "probe_meet_audit.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def test_probe_runs_dark_under_the_suite_and_exits_nonzero(capsys):
    mod = _load_probe()
    assert mod.main(["--day", "2026-08-26"]) == 2
    out = capsys.readouterr().out
    assert "state: dark:test" in out and "read-only" in out


def test_probe_prints_counts_and_key_names_never_values(monkeypatch, capsys):
    mod = _load_probe()
    pages = [{"items": [_event(endpoint="ep-1"),
                        _event(endpoint="ep-2", ident="cora@hjrglobal.com", name="Cora NoteTaker")]}]
    real = ma.read_call_ended
    monkeypatch.setattr(mod.ma, "read_call_ended", lambda s, e: real(s, e, service=_Svc(pages)))
    assert mod.main(["--day", "2026-08-26"]) == 0
    out = capsys.readouterr().out
    assert "state: live" in out and "call_ended events read: 2" in out
    assert "joins usable: 2 (human 1, bot 1)" in out and "distinct meetings: 1" in out
    assert "meeting_code" in out and "identifier" in out          # key NAMES
    for leak in ("pal@example.test", "Pal Example", "203.0.113.9", "votcadence", "VOTCADENCE",
                 "gc-1_", "cora@hjrglobal.com", "Cora NoteTaker", "conf-xyz", "ep-1"):
        assert leak not in out, leak
    for j in real(T0, T1, service=_Svc(pages)).joins:
        assert j.endpoint_key not in out


def test_probe_source_is_read_only_ascii_and_calls_only_the_seam():
    src = (_REPO / "scripts" / "probe_meet_audit.py").read_text(encoding="utf-8")
    assert all(ord(c) < 128 for c in src)
    for forbidden in ("write_ledger", "chat_postMessage", "WebClient", "upsert_documents",
                      "open(", "write_text", "requests.", "_build_reports_service"):
        assert forbidden not in src, forbidden
    assert src.count("ma.read_call_ended(") == 1
