"""Expected Invoice Check: the portal-only owner nudge (Code #13 slice 9a; D-302).

Properties:
  1. NUDGE and FLAG are separate semantics. A MISSING known_undelivered row owes
     the OWNER a DM (nudge_candidates) and still does not count toward the
     channel flags (flag_count -- its existing test stays untouched).
  2. The owner comes from the roster, never from src/. 'tessa' resolves to
     U0B3KH5UZJ7 against the LIVE org-roles.yaml; an unknown or ambiguous handle
     fails closed to Harrison with a WARN.
  3. The DM names the vendor, the period, the portal, the drop instructions and
     the human-edit rule; PHI-flagged text is refused, never sent.
  4. The runner: dry-run sends nothing and writes no ledger row; --post sends
     exactly one DM per candidate and nothing for PRESENT vendors; MISSING rows
     become repeat signals.
"""

from __future__ import annotations

import datetime
import json
import logging
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import expected_invoices as ei  # noqa: E402
from cora import org_roles  # noqa: E402
from cora import repeat_signal as rs  # noqa: E402

import run_expected_invoice_check as runner  # noqa: E402

TESSA = "U0B3KH5UZJ7"
HARRISON = "U0B2RM2JYJ1"
_AZ = datetime.timezone(datetime.timedelta(hours=-7))


def _ts(y, m, d, h=0):
    return int(datetime.datetime(y, m, d, h, tzinfo=_AZ).timestamp())


def _write_list(tmp_path, entries):
    p = tmp_path / "expected.yaml"
    lines = ["expected:"]
    for e in entries:
        lines.append(f"  - name: {e['name']}")
        for key in ("entity", "owner", "portal_url"):
            if e.get(key):
                lines.append(f"    {key}: {e[key]}")
        lines.append("    match:")
        for m in e.get("match") or []:
            lines.append(f"      - {m}")
        if e.get("known_undelivered"):
            lines.append("    known_undelivered: true")
        if e.get("note"):
            lines.append(f"    note: {e['note']}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_ledger(tmp_path, rows):
    p = tmp_path / "ledger.jsonl"
    out = [json.dumps({"_schema": "cora email-filer content ledger (key=md5)"})]
    out += [json.dumps(r) for r in rows]
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p


PORTAL = "https://example.invalid/billing/documents"

ADS = {"name": "Marigold Ads invoice", "entity": "F3E", "match": ["marigold-ads"],
       "known_undelivered": True, "owner": "tessa", "portal_url": PORTAL,
       "note": "Billing contact is outside the org."}
WORKSPACE = {"name": "Marigold Workspace invoice", "entity": "HJRG",
             "match": ["marigold-workspace"]}


@pytest.fixture(autouse=True)
def _fresh_roster():
    org_roles.invalidate_cache()
    yield
    org_roles.invalidate_cache()


@pytest.fixture()
def fixture_paths(tmp_path, monkeypatch):
    """Point the module defaults (what the runner reads) at fixtures: the Ads
    row MISSING + known_undelivered, the Workspace row PRESENT."""
    lst = _write_list(tmp_path, [ADS, WORKSPACE])
    ledger = _write_ledger(tmp_path, [
        {"drive_path": "01-HJR-Global/invoices/marigold-workspace-monthly-invoice.pdf",
         "filed_at": _ts(2026, 7, 2)}])
    monkeypatch.setattr(ei, "EXPECTATIONS_PATH", lst)
    monkeypatch.setattr(ei, "LEDGER_PATH", ledger)
    monkeypatch.setenv("REPEAT_SIGNAL_LEDGER_PATH", str(tmp_path / "repeat-signals.jsonl"))
    return lst, ledger


# ── 1. nudge vs flag ─────────────────────────────────────────────────────────

def test_a_known_undelivered_missing_row_is_a_nudge_candidate_but_not_a_flag(fixture_paths):
    res = ei.assess("2026-07")
    cands = ei.nudge_candidates(res)
    assert [c["name"] for c in cands] == ["Marigold Ads invoice"]
    assert cands[0]["owner"] == "tessa" and cands[0]["portal_url"] == PORTAL
    assert ei.flag_count(res) == 0, "flag semantics untouched: a tracked gap is not a flag"


def test_a_present_vendor_and_an_unflagged_missing_vendor_nudge_nobody(tmp_path):
    lst = _write_list(tmp_path, [WORKSPACE, {"name": "Marigold Ads invoice", "entity": "F3E",
                                              "match": ["marigold-ads"], "owner": "tessa"}])
    res = ei.assess("2026-07", expectations_path=lst,
                    ledger_path=_write_ledger(tmp_path, [
                        {"drive_path": "invoices/marigold-workspace.pdf",
                         "filed_at": _ts(2026, 7, 2)}]))
    statuses = {r["name"]: r["status"] for r in res["results"]}
    assert statuses == {"Marigold Workspace invoice": ei.STATUS_PRESENT,
                        "Marigold Ads invoice": ei.STATUS_MISSING}
    assert ei.nudge_candidates(res) == []          # un-flagged MISSING keeps the channel alarm
    assert ei.flag_count(res) == 1


def test_an_unavailable_check_nudges_nobody(tmp_path):
    res = ei.assess("2026-07", expectations_path=tmp_path / "nope.yaml",
                    ledger_path=_write_ledger(tmp_path, []))
    assert res["available"] is False and ei.nudge_candidates(res) == []


def test_owner_and_portal_ride_every_result_shape(tmp_path):
    lst = _write_list(tmp_path, [ADS, {"name": "Mystery vendor", "owner": "tessa"}])
    empty = ei.assess("2026-07", expectations_path=lst, ledger_path=tmp_path / "absent.jsonl")
    assert all(r["owner"] == "tessa" for r in empty["results"])          # UNKNOWN (empty ledger)
    res = ei.assess("2026-07", expectations_path=lst,
                    ledger_path=_write_ledger(tmp_path, [{"drive_path": "x.pdf",
                                                          "filed_at": _ts(2026, 7, 2)}]))
    by = {r["name"]: r for r in res["results"]}
    assert by["Mystery vendor"]["status"] == ei.STATUS_UNKNOWN and by["Mystery vendor"]["owner"] == "tessa"
    assert by["Marigold Ads invoice"]["portal_url"] == PORTAL


# ── 2. owner resolution through the roster ───────────────────────────────────

def test_tessa_resolves_to_her_live_roster_slack_id():
    """Pinned against the LIVE org-roles.yaml: the yaml says `owner: tessa`, the
    roster says who that is. Never hardcode U0B3KH5UZJ7 in src/."""
    rec = org_roles.find_by_handle("tessa")
    assert rec is not None and rec.slack_id == TESSA and rec.name == "Tessa Miller"
    assert ei.resolve_owner("tessa") == (TESSA, "Tessa Miller", True)
    assert ei.resolve_owner("Tessa Miller") == (TESSA, "Tessa Miller", True)
    assert ei.resolve_owner("@Tessa") == (TESSA, "Tessa Miller", True)
    assert ei.resolve_owner(TESSA)[0] == TESSA


def test_src_never_hardcodes_the_owner_slack_id():
    for rel in ("src/cora/expected_invoices.py", "src/cora/repeat_signal.py",
                "src/cora/org_roles.py", "scripts/run_expected_invoice_check.py",
                "scripts/nightly_health_check.py"):
        assert TESSA not in (_REPO_ROOT / rel).read_text(encoding="utf-8"), rel


def test_an_unresolvable_owner_fails_closed_to_harrison_with_a_warn(caplog):
    with caplog.at_level(logging.WARNING):
        sid, name, resolved = ei.resolve_owner("nobody-on-this-roster")
    assert (sid, resolved) == (HARRISON, False) and name == "Harrison"
    assert any("not resolvable" in r.getMessage() for r in caplog.records)
    assert ei.resolve_owner("")[0] == HARRISON


def test_an_ambiguous_first_name_fails_closed_not_first_match(tmp_path, monkeypatch):
    p = tmp_path / "roles.yaml"
    p.write_text(textwrap.dedent("""
        users:
          - slack_id: U0AAAAAAAA1
            name: Sam Quill
            role: Ops
            entity: F3E
          - slack_id: U0AAAAAAAA2
            name: Sam Rook
            role: Ops
            entity: OSN
          - slack_id: U0AAAAAAAA3
            name: Uma Vale
            role: Ops
            entity: HJRG
    """), encoding="utf-8")
    monkeypatch.setattr(org_roles, "_ROLES_PATH", p)
    org_roles.invalidate_cache()
    assert org_roles.find_by_handle("sam") is None            # ambiguous -> None
    assert org_roles.find_by_handle("Sam Rook").slack_id == "U0AAAAAAAA2"
    assert org_roles.find_by_handle("sam.quill").slack_id == "U0AAAAAAAA1"
    assert org_roles.find_by_handle("uma").slack_id == "U0AAAAAAAA3"
    assert org_roles.find_by_handle("") is None
    assert ei.resolve_owner("sam")[0] == HARRISON


def test_the_shipped_yaml_names_the_ads_owner_and_portal():
    items = {i["name"]: i for i in (ei.load_expectations() or [])}
    ads = items["Google Ads invoice"]
    assert ads.get("owner") == "tessa"
    assert ads.get("portal_url") == "https://ads.google.com/aw/billing/documents"
    assert org_roles.find_by_handle(ads["owner"]).slack_id == TESSA


# ── 3. the DM text ───────────────────────────────────────────────────────────

def test_the_nudge_names_vendor_period_portal_drop_point_and_the_human_edit_rule(fixture_paths):
    res = ei.assess("2026-07")
    row = ei.nudge_candidates(res)[0]
    text = ei.format_owner_nudge(row, "2026-07", owner_name="Tessa Miller")
    assert "Marigold Ads invoice" in text and "2026-07" in text and "[F3E]" in text
    assert PORTAL in text
    assert ei.RECEIPTS_INBOX_FOLDER_ID in text and ei.RECEIPTS_MAILBOX in text
    assert ("known_undelivered clears only when a human edits the yaml after the first "
            "download lands") in text
    assert text.startswith(":page_facing_up: Hi Tessa --")


def test_a_missing_portal_url_is_said_out_loud_not_blank():
    text = ei.format_owner_nudge({"name": "Marigold Ads invoice", "entity": "F3E"}, "2026-07")
    assert "no `portal_url`" in text


def test_format_report_suppressed_line_replaces_the_alarm_only_when_asked(tmp_path):
    sub = tmp_path / "unflagged"
    sub.mkdir()
    lst = _write_list(sub, [{"name": "Marigold Ads invoice", "entity": "F3E",
                             "match": ["marigold-ads"]}])
    ledger = _write_ledger(sub, [{"drive_path": "x/other.pdf", "filed_at": _ts(2026, 7, 2)}])
    res = ei.assess("2026-07", expectations_path=lst, ledger_path=ledger)
    plain = ei.format_report(res)
    assert "rotating_light" in plain
    quiet = ei.format_report(res, suppressed={"Marigold Ads invoice": "rs-abc"})
    assert "rotating_light" not in quiet and "no_bell" in quiet and "rs-abc" in quiet
    assert ei.format_report(res, suppressed={}) == plain


# ── 4. the runner ────────────────────────────────────────────────────────────

def _fake_client():
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D0MARIGOLD"}}
    client.chat_postMessage.return_value = {"ts": "1758290000.000100"}
    return client


def test_dry_run_prints_the_plan_but_sends_nothing_and_writes_no_ledger_row(
        fixture_paths, monkeypatch, capsys):
    client = _fake_client()
    monkeypatch.setattr(runner, "_client", lambda: client)
    assert runner.main(["--period", "2026-07"]) == 0
    out = capsys.readouterr().out
    assert "[nudge -> Tessa Miller (" + TESSA + ")]" in out
    assert "expected-invoice|Marigold Ads invoice|F3E -> tier 1" in out
    assert "dry run" in out
    client.chat_postMessage.assert_not_called()
    client.conversations_open.assert_not_called()
    assert rs.read_rows() == []


def test_post_sends_one_owner_dm_plus_the_channel_report_and_nothing_for_present(
        fixture_paths, monkeypatch):
    client = _fake_client()
    monkeypatch.setattr(runner, "_client", lambda: client)
    assert runner.main(["--post", "--period", "2026-07"]) == 0
    client.conversations_open.assert_called_once_with(users=[TESSA])
    calls = client.chat_postMessage.call_args_list
    channels = [c.kwargs["channel"] for c in calls]
    assert sorted(channels) == sorted(["D0MARIGOLD", runner.HJRG_FINANCE_CHANNEL])
    dm = next(c for c in calls if c.kwargs["channel"] == "D0MARIGOLD").kwargs["text"]
    assert "Marigold Ads invoice" in dm and "2026-07" in dm and PORTAL in dm
    assert "Marigold Workspace" not in dm
    # the signal was recorded once, tier 1, keyed on the period
    rows = rs.read_rows()
    assert len(rows) == 1 and rows[0]["fire_id"] == "2026-07" and rows[0]["tier"] == 1
    assert rows[0]["signal_key"] == "expected-invoice|Marigold Ads invoice|F3E"
    assert rows[0]["owner_slack_id"] == TESSA and rows[0]["normal_surface"] == "dm:owner"


def test_post_with_everything_present_sends_only_the_channel_report(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "EXPECTATIONS_PATH", _write_list(tmp_path, [WORKSPACE]))
    monkeypatch.setattr(ei, "LEDGER_PATH", _write_ledger(tmp_path, [
        {"drive_path": "invoices/marigold-workspace.pdf", "filed_at": _ts(2026, 7, 2)}]))
    client = _fake_client()
    monkeypatch.setattr(runner, "_client", lambda: client)
    assert runner.main(["--post", "--period", "2026-07"]) == 0
    assert client.chat_postMessage.call_count == 1
    assert client.chat_postMessage.call_args.kwargs["channel"] == runner.HJRG_FINANCE_CHANNEL
    assert rs.read_rows() == []


def test_the_same_period_re_run_is_idempotent_and_consecutive_months_escalate(
        fixture_paths, monkeypatch):
    client = _fake_client()
    monkeypatch.setattr(runner, "_client", lambda: client)
    runner.main(["--post", "--period", "2026-07"])
    runner.main(["--post", "--period", "2026-07"])         # a retried task
    key = "expected-invoice|Marigold Ads invoice|F3E"
    assert rs.state(key)["consecutive"] == 1
    runner.main(["--post", "--period", "2026-08"])
    assert rs.state(key)["consecutive"] == 2
    runner.main(["--post", "--period", "2026-09"])
    st = rs.state(key)
    assert st["consecutive"] == 3 and st["suppressed"] is True and st["card_update_id"]
    # tier 3: the owner DM (the original surface) is suppressed pending Harrison's ack
    dm_count_before = sum(1 for c in client.chat_postMessage.call_args_list
                          if c.kwargs["channel"] == "D0MARIGOLD")
    runner.main(["--post", "--period", "2026-10"])
    dm_count_after = sum(1 for c in client.chat_postMessage.call_args_list
                         if c.kwargs["channel"] == "D0MARIGOLD")
    assert dm_count_after == dm_count_before
    ads_rows = [r for r in rs.read_rows() if r["signal_key"] == key]
    assert ads_rows[-1]["event"] == rs.EVENT_SUPPRESSED and ads_rows[-1]["fire_id"] == "2026-10"
    # the Workspace row (filed only in July in this fixture) escalated on its OWN
    # key -- two signals, two independent ladders
    ws_key = "expected-invoice|Marigold Workspace invoice|HJRG"
    assert rs.state(ws_key)["consecutive"] == 3


def test_an_unflagged_missing_row_becomes_a_channel_surface_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "EXPECTATIONS_PATH", _write_list(tmp_path, [
        {"name": "Marigold Ads invoice", "entity": "F3E", "match": ["marigold-ads"]}]))
    monkeypatch.setattr(ei, "LEDGER_PATH", _write_ledger(tmp_path, [
        {"drive_path": "x/other.pdf", "filed_at": _ts(2026, 7, 2)}]))
    client = _fake_client()
    monkeypatch.setattr(runner, "_client", lambda: client)
    runner.main(["--post", "--period", "2026-07"])
    client.conversations_open.assert_not_called()          # no DM: not a nudge candidate
    rows = rs.read_rows()
    assert len(rows) == 1 and rows[0]["normal_surface"] == "#hjrg-finance"
    assert rows[0]["owner_slack_id"] == HARRISON             # no owner -> fail-closed


def test_a_phi_flagged_nudge_is_refused_never_sent(fixture_paths, monkeypatch, caplog):
    import cora.phi_guard as pg
    monkeypatch.setattr(pg, "is_any_phi", lambda text: True)
    client = _fake_client()
    res = ei.assess("2026-07")
    signals = runner.plan_signals(res, post=True)
    with caplog.at_level(logging.ERROR):
        sent = runner.send_nudges(res, signals, post=True, client=client)
    assert sent == 0
    client.chat_postMessage.assert_not_called()
    assert any("REFUSED" in r.getMessage() for r in caplog.records)


def test_the_dm_body_goes_through_the_egress_boundary(fixture_paths, monkeypatch):
    seen = []
    import cora.slack_egress as se
    real = se.sanitize_text
    monkeypatch.setattr(se, "sanitize_text", lambda t: seen.append(t) or real(t))
    client = _fake_client()
    monkeypatch.setattr(runner, "_client", lambda: client)
    runner.main(["--post", "--period", "2026-07"])
    assert len(seen) == 2, "one DM + one channel post, both sanitized"


def test_the_task_day_is_registration_side_not_here():
    """Ruling 1: the check is ALREADY on day 9 (schtasks /D 9, moved 2026-08-25);
    no run-day logic in code, no re-register."""
    ps1 = (_REPO_ROOT / "deployment" / "setup-monthly-finance-report-tasks.ps1").read_text(
        encoding="utf-8")
    assert "/D 9" in ps1
    src = (_REPO_ROOT / "scripts" / "run_expected_invoice_check.py").read_text(encoding="utf-8")
    assert "--min-day" not in src and "day()" not in src
