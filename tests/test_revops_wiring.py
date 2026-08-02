"""Wiring assertions: tool registration, channel scoping of the read tool,
snapshot spec (D-094), importer plan, and PHI-at-rest in the mirror
(D-051 lens 7)."""

from __future__ import annotations

import time

import pytest

from cora import session_snapshots
from cora.revops import ledger, send_trust
from cora.tools import tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("CORA_REVOPS_DB", str(tmp_path / "revops_ledger.db"))
    send_trust.clear_caches()
    yield
    send_trust.clear_caches()


def _seed(conn):
    ledger.upsert_thread(
        conn, mailbox="harrison@hjrglobal.com", gmail_thread_id="r1",
        counterparty_name="Josh A. (Wham Foods)", workstream="Retail",
        entity="F3E", state="nudge_due", last_outbound_ts=time.time() - 9 * 86400,
    )
    ledger.upsert_thread(
        conn, mailbox="harrison@hjrglobal.com", gmail_thread_id="f1",
        counterparty_name="Kayley (Trestle Law)", workstream="Finance-Legal",
        entity="HJRG", state="escalated", notes="publication notice",
        hold_reason=None,
    )


# ------------------------------------------------------------- registration

def test_tool_registered_everywhere():
    assert "revops_ledger_status" in td._GLOBAL_CORE_TOOLS
    assert "revops_ledger_status" in td._TOOL_FUNCTIONS
    assert any(t["name"] == "revops_ledger_status" for t in td.TOOL_DEFINITIONS)
    assert td._TOOL_TIMEOUTS["revops_ledger_status"] == 8


def test_tool_exposed_to_lean_entities():
    names = {t["name"] for t in td.tools_for_entity("F3C")}
    assert "revops_ledger_status" in names


# ---------------------------------------------------------- channel scoping

def test_harrison_sees_everything():
    conn = ledger.connect()
    try:
        _seed(conn)
    finally:
        conn.close()
    out = td._tool_revops_ledger_status(HARRISON, "FNDR", {})
    assert "Wham Foods" in out
    assert "Trestle Law" in out


def test_non_harrison_never_sees_finance_legal():
    conn = ledger.connect()
    try:
        _seed(conn)
    finally:
        conn.close()
    out = td._tool_revops_ledger_status("U0B3RU5Q55G", "F3E", {})
    assert "Wham Foods" in out
    assert "Trestle Law" not in out
    # ...and an HJRG channel asker still doesn't get Finance-Legal
    out2 = td._tool_revops_ledger_status("U0B3RU5Q55G", "HJRG", {})
    assert "Trestle Law" not in out2


def test_non_harrison_scoped_to_channel_entity():
    conn = ledger.connect()
    try:
        _seed(conn)
    finally:
        conn.close()
    out = td._tool_revops_ledger_status("U0B3RU5Q55G", "OSN", {})
    assert "Wham Foods" not in out


# ------------------------------------------------------- snapshot (D-094)

def test_snapshot_spec_registered():
    names = {s["name"] for s in session_snapshots._SPECS}
    assert "revops-ledger.json" in names
    spec = next(s for s in session_snapshots._SPECS if s["name"] == "revops-ledger.json")
    assert spec["cadence"] >= 60


def test_snapshot_render_finance_legal_rows_minimal():
    """Lens 7: no body content anywhere; Finance-Legal = counterparty+state only."""
    conn = ledger.connect()
    try:
        _seed(conn)
    finally:
        conn.close()
    payload = session_snapshots._render_revops_ledger()
    rows = {r["thread_key"]: r for r in payload["threads"]}
    fin = rows["harrison@hjrglobal.com:f1"]
    assert set(fin) == {"thread_key", "counterparty", "workstream", "state"}
    retail = rows["harrison@hjrglobal.com:r1"]
    assert "nudge_count" in retail
    assert "notes" not in retail  # notes never mirrored for any row
    assert "counterparty + state only" in payload["note"]


# ------------------------------------------------------------- importer plan

def test_importer_plan_normalizes_and_screens():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "import_reply_watch_state",
        Path(__file__).resolve().parents[1] / "scripts" / "import_reply_watch_state.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    plan = mod.plan_thread(
        {
            "thread_id": "19f61e75",
            "counterparty": "Diane Kimball (Factum + CDC)",
            "workstream": "Finance/Legal",
            "entity": "HJRP",
            "last_outbound_date": "2026-07-14",
            "note": "SBA lien release on policy 8420, still unfiled",
        }
    )
    assert plan["workstream"] == "Finance-Legal"
    assert plan["state"] == "awaiting_reply"

    hold = mod.plan_thread(
        {
            "thread_id": "19f2e538",
            "counterparty": "Seb Bradley (Digiday)",
            "workstream": "Press",
            "entity": "F3E",
            "note": "published without F3; door explicitly open, no nudge warranted",
        }
    )
    assert hold["state"] == "hold"

    esc = mod.plan_thread(
        {
            "thread_id": "19f67cf7",
            "counterparty": "Josh Liberatore (Athletech News)",
            "workstream": "Press",
            "entity": "F3E",
            "note": "live embargo negotiation, replied 2026-08-01",
        }
    )
    assert esc["state"] == "escalated"
    assert esc["escalation_keyword"] == "embargo"
    assert esc["owner"] == HARRISON
