"""Tests for the hubspot_email_sync connector DM gate (audit N6, Phase 0.1).

The ambiguous-match "confirm attachment / no active deals" DM prompts were
relentless and memory-less. Phase 0.1 gates them OFF by default behind
CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED; full Alex+Tommy scoping lands in Phase 1.8
(where the sync_user behavioral tests will be added).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import hubspot_email_sync as sync  # noqa: E402


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on", " true "])
def test_dm_prompts_enabled_true(monkeypatch, value):
    monkeypatch.setenv("CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED", value)
    assert sync._dm_prompts_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  ", "disabled"])
def test_dm_prompts_enabled_false(monkeypatch, value):
    monkeypatch.setenv("CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED", value)
    assert sync._dm_prompts_enabled() is False


def test_dm_prompts_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED", raising=False)
    assert sync._dm_prompts_enabled() is False


# ---------------------------------------------------------------------------
# Phase 1.8: Alex+Tommy scoping (run_sync)
# ---------------------------------------------------------------------------

def test_load_scope_ships_alex_and_tommy():
    # The committed config scopes the sync to exactly these two mailboxes.
    scope = sync._load_scope()
    assert "alex@hjrglobal.com" in scope
    assert "tommy@f3energy.com" in scope


def test_run_sync_scopes_to_allowlist(monkeypatch):
    users = [
        {"hubspot_email": "alex@hjrglobal.com", "display_name": "Alex"},
        {"hubspot_email": "tommy@f3energy.com", "display_name": "Tommy"},
        {"hubspot_email": "hannah@hjrglobal.com", "display_name": "Hannah"},
        {"hubspot_email": "harrison@hjrglobal.com", "display_name": "Harrison"},
    ]
    monkeypatch.setattr(sync, "_load_users", lambda: users)
    monkeypatch.setattr(sync, "_load_scope", lambda: {"alex@hjrglobal.com", "tommy@f3energy.com"})
    monkeypatch.setattr(sync, "_load_state", lambda: {})
    monkeypatch.setattr(sync, "_load_skipped", lambda: {})
    monkeypatch.setattr(sync.time, "sleep", lambda *a, **k: None)
    synced: list[str] = []
    monkeypatch.setattr(
        sync, "sync_user",
        lambda u, st, dry_run=False, skipped=None: synced.append(u["hubspot_email"])
        or {"threads": 0, "logged": 0, "skipped": 0, "dm_sent": 0},
    )
    sync.run_sync(dry_run=True)
    assert sorted(synced) == ["alex@hjrglobal.com", "tommy@f3energy.com"]


def test_run_sync_empty_scope_scans_nobody(monkeypatch):
    monkeypatch.setattr(sync, "_load_users", lambda: [{"hubspot_email": "x@y.com"}])
    monkeypatch.setattr(sync, "_load_scope", lambda: set())
    called: list[int] = []
    monkeypatch.setattr(sync, "sync_user", lambda *a, **k: called.append(1) or {})
    sync.run_sync(dry_run=True)
    assert called == []


# ---------------------------------------------------------------------------
# Phase 1.8: active-deal gate + skip ledger (sync_user)
# ---------------------------------------------------------------------------

_USER = {
    "hubspot_email": "tommy@f3energy.com",
    "hubspot_owner_id": "162944825",
    "slack_user_id": "U_T",
    "display_name": "Tommy Anderson",
}

_FUTURE_TS = 2_000_000_000  # always > the 7-day lookback watermark


def _msg(sender, recipients, ts=_FUTURE_TS, subject="Re: order"):
    return {"sender": sender, "recipients": recipients, "subject": subject,
            "body_text": "body", "date_ts": ts}


def _wire(monkeypatch, *, threads, messages, contact_for, open_deals_for, dm_recorder=None):
    import cora.connectors.gmail_reader as gr
    import cora.tools.hubspot_client as hs
    monkeypatch.setattr(gr, "list_threads_since", lambda *a, **k: threads)
    monkeypatch.setattr(gr, "get_full_thread_text", lambda *a, **k: messages)
    monkeypatch.setattr(hs, "search_contact_by_email", contact_for)
    monkeypatch.setattr(hs, "get_open_deal_ids_for_contact", open_deals_for)
    monkeypatch.setattr(sync.time, "sleep", lambda *a, **k: None)
    logged: list[dict] = []
    monkeypatch.setattr(hs, "log_email_engagement", lambda **kw: logged.append(kw) or "E1")
    if dm_recorder is not None:
        monkeypatch.setattr(sync, "_dm_user_with_ts", dm_recorder)
    return logged


def test_active_deal_thread_is_logged(monkeypatch):
    logged = _wire(
        monkeypatch,
        threads=["T1"],
        messages=[_msg("buyer@acme.com", "tommy@f3energy.com")],
        contact_for=lambda e: {"id": "C1", "properties": {"firstname": "Buyer", "lastname": "Acme"}},
        open_deals_for=lambda cid: ["D9"],
    )
    state, skipped = {}, {}
    stats = sync.sync_user(_USER, state, dry_run=False, skipped=skipped)
    assert stats["logged"] == 1
    assert logged and logged[0]["deal_ids"] == ["D9"]
    assert "T1" not in skipped  # logged threads are NOT recorded (new msgs must still log)


def test_no_active_deal_thread_skipped_and_remembered(monkeypatch):
    dm_called: list = []
    logged = _wire(
        monkeypatch,
        threads=["T1"],
        messages=[_msg("buyer@acme.com", "tommy@f3energy.com")],
        contact_for=lambda e: {"id": "C1", "properties": {"firstname": "Buyer", "lastname": "Acme"}},
        open_deals_for=lambda cid: [],  # no OPEN deal
        dm_recorder=lambda *a, **k: dm_called.append(1) or "ts",
    )
    skipped: dict = {}
    stats = sync.sync_user(_USER, {}, dry_run=False, skipped=skipped)
    assert stats["logged"] == 0 and stats["skipped"] == 1
    assert logged == []          # never "log to contact only"
    assert dm_called == []       # never prompt on no active deal
    assert skipped.get("T1", {}).get("reason") == "no_active_deal"


def test_skipped_thread_not_reprocessed(monkeypatch):
    searched: list = []
    _wire(
        monkeypatch,
        threads=["T1"],
        messages=[_msg("buyer@acme.com", "tommy@f3energy.com")],
        contact_for=lambda e: searched.append(e) or {"id": "C1", "properties": {}},
        open_deals_for=lambda cid: ["D9"],
    )
    skipped = {"T1": {"reason": "no_active_deal", "ts": 1}}
    stats = sync.sync_user(_USER, {}, dry_run=False, skipped=skipped)
    assert searched == []        # bailed at the seen-guard, never looked up a contact
    assert stats["logged"] == 0


def test_transient_open_deal_error_is_not_recorded(monkeypatch):
    from cora.tools.hubspot_client import HubSpotClientError

    def _boom(cid):
        raise HubSpotClientError("429")

    logged = _wire(
        monkeypatch,
        threads=["T1"],
        messages=[_msg("buyer@acme.com", "tommy@f3energy.com")],
        contact_for=lambda e: {"id": "C1", "properties": {}},
        open_deals_for=_boom,
    )
    skipped: dict = {}
    stats = sync.sync_user(_USER, {}, dry_run=False, skipped=skipped)
    assert logged == [] and stats["logged"] == 0
    assert "T1" not in skipped    # transient error -> retry next run, not remembered


def test_ambiguous_active_no_dm_by_default(monkeypatch):
    monkeypatch.delenv("CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED", raising=False)
    dm_called: list = []
    logged = _wire(
        monkeypatch,
        threads=["T1"],
        messages=[_msg("a@ext.com", "tommy@f3energy.com, b@ext.com")],
        contact_for=lambda e: {"id": f"C-{e}", "properties": {}},
        open_deals_for=lambda cid: ["D"],   # both contacts on an active deal
        dm_recorder=lambda *a, **k: dm_called.append(1) or "ts",
    )
    skipped: dict = {}
    stats = sync.sync_user(_USER, {}, dry_run=False, skipped=skipped)
    assert dm_called == []        # DMs OFF by default -> no prompt even when ambiguous
    assert logged == []           # never auto-log an ambiguous active match
    assert stats["skipped"] == 1
    assert skipped.get("T1", {}).get("reason") == "ambiguous_active"
