"""Tests for the tap orchestration: authority, exactly-once claim, live
drift re-check, and the five-way outcome classification.
"""

from __future__ import annotations

import pytest

from cora.connectors import deposco_client as dc
from cora.connectors import deposco_push as dpush
from cora.deposco_orders import handler, payload as payload_mod, pending, preflight
from cora.deposco_orders import spec as spec_mod

JUSTIN_ID = "U0B3AEJCYGP"  # a real non-approver id, per repo convention

SPEC = spec_mod.OrderSpec(
    channel="wholesale", buyer_or_fc_code="GOTHAM", reference="4471",
    authored_by="Harrison",
    lines=[spec_mod.OrderSpecLine(sku="PURE-Original", qty=208, unit_price="21.70")],
)
PASSED_PREFLIGHT = preflight.PreflightResult(
    passed=True, checked_at="2026-09-23T10:00:00-07:00",
    checks=[preflight.PreflightCheck("number_miss", True)],
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_DEPOSCO_PENDING_DIR", str(tmp_path / "pending"))
    monkeypatch.setenv("CORA_DEPOSCO_PUSH_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("CORA_DEPOSCO_DEMOTION_STATE_PATH", str(tmp_path / "demotion.json"))
    monkeypatch.delenv("CORA_DEPOSCO_STANDING_CHANNELS", raising=False)
    monkeypatch.delenv("CORA_DEPOSCO_PUSH_CHANNELS", raising=False)


def _stage(order_spec=SPEC, *, created_at=None):
    payload = payload_mod.build_payload(order_spec)
    number = payload_mod.build_order_number(order_spec)
    entry = pending.stage_entry(
        channel=order_spec.channel, number=number, payload=payload, env="prod",
        preflight_result=PASSED_PREFLIGHT, spec_raw=_spec_raw(order_spec),
        authored_by=order_spec.authored_by,
    )
    if created_at:
        entry["created_at"] = created_at
        pending._write_entry(entry)
    return entry


def _spec_raw(order_spec):
    return {
        "channel": order_spec.channel, "buyer_or_fc_code": order_spec.buyer_or_fc_code,
        "reference": order_spec.reference, "authored_by": order_spec.authored_by,
        "freight_terms": order_spec.freight_terms,
        "lines": [{"sku": l.sku, "qty": l.qty, "unit_price": l.unit_price} for l in order_spec.lines],
    }


class FakeReadClient:
    def __init__(self, *, atp=None, missing_items=(), find_order_detail_sequence=None, env="prod"):
        self._atp = atp or {"PURE-Original": 500}
        self._missing_items = set(missing_items)
        self._sequence = list(find_order_detail_sequence or [])
        self.env = env

    def find_order_detail(self, order_type, number):
        if self._sequence:
            return self._sequence.pop(0)
        return None

    def search_orders(self, order_type, **kw):
        return dc.DeposcoResponse("prod", "/search/Order", 200, "<orders/>")

    def item_exists(self, item_number):
        return item_number not in self._missing_items

    def get_enterprise_availability(self, item_numbers=None, **kw):
        rows = [dc.EnterpriseInventoryRow(item_number=sku, measures={"atpQty": qty})
                for sku, qty in self._atp.items()]
        return dc.AvailabilityResult(env="prod", rows=rows)


class FakePushClient:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    def push_order(self, payload, *, channel, claimed_pending_id):
        self.calls.append((payload, channel, claimed_pending_id))
        return self._outcome


def _patch_clients(monkeypatch, *, read_client=None, push_client=None, reference_hit=False):
    monkeypatch.setattr(handler.preflight, "_reference_exists",
                        lambda client, reference: reference_hit)
    if read_client is not None:
        monkeypatch.setattr(handler.dc, "DeposcoClient", lambda **kw: read_client)
    if push_client is not None:
        monkeypatch.setattr(handler.dpush, "DeposcoPushClient", lambda **kw: push_client)


def _record(number, atp_ok=True):
    return dc.OrderHeaderRecord(
        number=number, customer_order_number="4471", ship_to_postal_code="11101",
        lines=[dc.OrderHeaderLine(item_number="PURE-Original", order_pack_quantity=208)],
    )


# ── Staging ────────────────────────────────────────────────────────────────


class TestStageOrder:
    def test_a_clean_stage_produces_an_entry_and_a_card(self, monkeypatch):
        _patch_clients(monkeypatch, reference_hit=False)
        client = FakeReadClient(atp={"PURE-Original": 500})
        outcome = handler.stage_order(SPEC, env="prod", spec_raw=_spec_raw(SPEC), client=client)
        assert outcome.ok, outcome.errors
        assert outcome.entry["state"] == pending.STATE_STAGED
        assert outcome.card_blocks

    def test_a_failed_preflight_refuses_to_stage(self, monkeypatch):
        _patch_clients(monkeypatch, reference_hit=False)
        client = FakeReadClient(missing_items={"PURE-Original"}, atp={"PURE-Original": 500})
        outcome = handler.stage_order(SPEC, env="prod", spec_raw=_spec_raw(SPEC), client=client)
        assert not outcome.ok
        assert outcome.errors
        assert outcome.entry is None

    def test_a_not_clean_channel_history_blocks_staging(self, monkeypatch):
        _patch_clients(monkeypatch, reference_hit=False)
        pending.append_ledger_row(channel="wholesale", number="F3E-W-GOTHAM-OLD",
                                  event=pending.STATE_MISMATCH, clean=False)
        client = FakeReadClient(atp={"PURE-Original": 500})
        outcome = handler.stage_order(SPEC, env="prod", spec_raw=_spec_raw(SPEC), client=client)
        assert not outcome.ok
        assert "root-cause note" in outcome.blocked_reason


# ── Authority ────────────────────────────────────────────────────────────────


class TestAuthority:
    def test_harrison_can_push_supervised(self, monkeypatch):
        entry = _stage()
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None, _record(entry["number"])]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created")),
        )
        outcome, _ = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "confirmed"

    def test_a_non_approver_is_refused_before_anything_happens(self, monkeypatch):
        entry = _stage()
        outcome, msg = handler.process_push_tap(entry["id"], JUSTIN_ID)
        assert outcome == "not_authorized"
        refreshed = pending.get_entry(entry["id"])
        assert refreshed["state"] == pending.STATE_STAGED, "an unauthorized tap must not touch state"

    def test_missing_pending_id_is_orphaned(self):
        outcome, _ = handler.process_push_tap("", handler.HARRISON_ID)
        assert outcome == "orphaned"

    def test_unknown_id_is_orphaned(self):
        outcome, _ = handler.process_push_tap("deposco-doesnotexist", handler.HARRISON_ID)
        assert outcome == "orphaned"


# ── TTL ──────────────────────────────────────────────────────────────────────


class TestTTL:
    def test_a_stale_entry_is_dismissed_not_pushed(self, monkeypatch):
        stale_created = "2020-01-01T00:00:00-07:00"
        entry = _stage(created_at=stale_created)
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "stale_dismissed"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_DISMISSED

    def test_a_malformed_created_at_does_not_crash_never_raises(self, monkeypatch):
        """A corrupted-but-still-valid-JSON entry (not the get_entry-level
        corruption test in test_deposco_orders_pending.py) must fail safe --
        the TTL check is skipped, not raised, and the live re-preflight
        (which independently re-checks ATP/existence) still runs."""
        entry = _stage(created_at="not-a-real-timestamp")
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None, _record(entry["number"])]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created")),
        )
        outcome, _ = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome != "stale_dismissed"
        assert outcome == "confirmed"

    def test_a_fresh_entry_is_not_treated_as_stale(self, monkeypatch):
        entry = _stage()  # created just now
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None, _record(entry["number"])]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created")),
        )
        outcome, _ = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome != "stale_dismissed"


# ── Exactly-once claim ───────────────────────────────────────────────────────


class TestExactlyOnceClaim:
    def test_a_second_tap_after_resolution_is_already_handled(self, monkeypatch):
        entry = _stage()
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None, _record(entry["number"])]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created")),
        )
        first, _ = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert first == "confirmed"
        second, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert second == "already_handled"

    def test_dismiss_after_confirm_is_already_handled(self, monkeypatch):
        entry = _stage()
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None, _record(entry["number"])]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created")),
        )
        handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        outcome, _ = handler.process_dismiss_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "already_handled"


# ── Live drift at tap ─────────────────────────────────────────────────────────


class TestLiveDriftReCheck:
    def test_an_item_that_vanished_since_staging_re_stages_never_pushes(self, monkeypatch):
        entry = _stage()
        push_client = FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created"))
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(missing_items={"PURE-Original"}),
            push_client=push_client,
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "drifted_restaged"
        assert push_client.calls == [], "drift must prevent the push entirely"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_STAGED

    def test_the_order_now_existing_since_staging_re_stages(self, monkeypatch):
        entry = _stage()
        push_client = FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created"))
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[_record(entry["number"])]),
            push_client=push_client,
        )
        outcome, _ = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "drifted_restaged"
        assert push_client.calls == []


# ── Outcome classification ────────────────────────────────────────────────────


class TestOutcomeClassification:
    def test_201_plus_clean_readback_is_confirmed(self, monkeypatch):
        entry = _stage()
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None, _record(entry["number"])]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created")),
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "confirmed"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_CONFIRMED
        rows = pending._read_ledger()
        assert rows[-1]["event"] == pending.STATE_CONFIRMED and rows[-1]["clean"] is True

    def test_4xx_is_failed(self, monkeypatch):
        entry = _stage()
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=400, text="missingItems")),
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "failed"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_FAILED
        assert pending._read_ledger()[-1]["clean"] is False

    def test_blank_200_exhaustion_is_unknown_and_locks(self, monkeypatch):
        entry = _stage()
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=200, text="",
                                                          blank_after_retries=True)),
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "unknown"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_UNKNOWN
        assert "never auto-retry" in msg

    def test_network_exhaustion_is_unknown(self, monkeypatch):
        entry = _stage()
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=None,
                                                          network_exhausted=True)),
        )
        outcome, _ = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "unknown"

    def test_201_but_readback_never_finds_it_is_unknown(self, monkeypatch):
        entry = _stage()
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None, None, None, None]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created")),
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "unknown"
        assert "cannot find it" in msg

    def test_200_on_create_is_anomaly_updated(self, monkeypatch):
        entry = _stage()
        push_client = FakePushClient(dpush.PushOutcome(env="prod", status=200, text='{"status":"Updated"}'))
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None]),
            push_client=push_client,
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "anomaly_updated"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_ANOMALY_UPDATED
        assert "Call Nimbl now" in msg

    def test_a_409_conflict_body_is_failed_not_a_false_confirmed(self, monkeypatch):
        """LIVE FINDING 2026-09-23: a genuine duplicate returns 409 Conflict
        in the multistatus body (outer status 207), not a silent 200 update.
        Classified FAILED -- safe, nothing was created or changed."""
        entry = _stage()
        conflict_body = (
            '<ns2:multistatus><response><status>HTTP/1.1 409 Conflict</status>'
            "<description>Duplicate entry</description></response></ns2:multistatus>"
        )
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=207, text=conflict_body)),
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "failed"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_FAILED

    def test_201_but_lines_differ_is_mismatch(self, monkeypatch):
        entry = _stage()
        wrong_record = dc.OrderHeaderRecord(
            number=entry["number"], customer_order_number="4471", ship_to_postal_code="11101",
            lines=[dc.OrderHeaderLine(item_number="PURE-Original", order_pack_quantity=999)],
        )
        _patch_clients(
            monkeypatch, reference_hit=False,
            read_client=FakeReadClient(find_order_detail_sequence=[None, wrong_record]),
            push_client=FakePushClient(dpush.PushOutcome(env="prod", status=201, text="201 Created")),
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "mismatch"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_MISMATCH
        assert "Call Nimbl now" in msg


# ── Integration: the REAL DeposcoPushClient's prod gate, reached through handler ──


def _patch_real_client_read_methods(monkeypatch, *, find_order_detail_sequence):
    """Patches METHODS on the real `dc.DeposcoClient` class -- not the class
    itself -- so `DeposcoPushClient.__init__`'s own internal
    `dc.DeposcoClient(...)` construction (it shares the exact same module
    reference `handler.dc` does) stays REAL and unaffected. Replacing the
    whole class here would ALSO replace what deposco_push.py builds its
    `_reader` from, defeating the point of this test."""
    seq = list(find_order_detail_sequence)
    state = {"n": 0}

    def fake_find_order_detail(self, order_type, number):
        idx = min(state["n"], len(seq) - 1) if seq else 0
        state["n"] += 1
        return seq[idx] if seq else None

    monkeypatch.setattr(dc.DeposcoClient, "find_order_detail", fake_find_order_detail)
    monkeypatch.setattr(
        dc.DeposcoClient, "search_orders",
        lambda self, order_type, **kw: dc.DeposcoResponse("prod", "/search/Order", 200, "<orders/>"),
    )
    monkeypatch.setattr(dc.DeposcoClient, "item_exists", lambda self, item_number: True)
    monkeypatch.setattr(
        dc.DeposcoClient, "get_enterprise_availability",
        lambda self, item_numbers=None, **kw: dc.AvailabilityResult(
            env="prod",
            rows=[dc.EnterpriseInventoryRow(item_number="PURE-Original", measures={"atpQty": 500})],
        ),
    )


class TestRealPushClientProdGateReachedThroughHandler:
    """Every other test in this file mocks dpush.DeposcoPushClient entirely --
    which proves the orchestration logic, but never proves the REAL prod
    double-gate (deposco_push.py's own class) is actually invoked correctly
    from process_push_tap. This uses the real class end to end -- only the
    READ methods and the final _send (the actual httpx call) are stubbed."""

    def test_prod_push_with_no_channel_allowlist_is_safely_refused_not_crashed(
        self, monkeypatch,
    ):
        """This is the ACTUAL live state of this build: CORA_DEPOSCO_PUSH_CHANNELS
        is never set (guardrail). A tap on a prod-staged card must be refused
        by the real gate, reported as a clean 'error' outcome -- never a crash,
        and never a false push."""
        monkeypatch.delenv("CORA_DEPOSCO_PUSH_CHANNELS", raising=False)
        monkeypatch.setenv("DEPOSCO_PROD_USER", "prod-user")
        monkeypatch.setenv("DEPOSCO_PROD_PASS", "prod-pass")
        entry = _stage()  # env="prod" by default in _stage's payload build
        _patch_real_client_read_methods(monkeypatch, find_order_detail_sequence=[None])
        monkeypatch.setattr(
            dpush.DeposcoPushClient, "_send",
            lambda self, *a, **k: (_ for _ in ()).throw(
                AssertionError("no network call should ever be attempted")),
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "error"
        assert "refused" in msg.lower()
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_STAGED, (
            "a refused push must release the claim back to STAGED, not strand it CLAIMED"
        )

    def test_prod_push_with_the_channel_allowlisted_reaches_the_real_send(self, monkeypatch):
        """The mirror case: WITH the allowlist set, the real gate lets the
        call through to _send (proving the gate is not simply always-refuse)."""
        monkeypatch.setenv("CORA_DEPOSCO_PUSH_CHANNELS", "wholesale")
        monkeypatch.setenv("DEPOSCO_PROD_USER", "prod-user")
        monkeypatch.setenv("DEPOSCO_PROD_PASS", "prod-pass")
        entry = _stage()
        _patch_real_client_read_methods(
            monkeypatch, find_order_detail_sequence=[None, _record(entry["number"])],
        )
        sent = []

        class _FakeResponse:
            status_code = 207
            text = "201 Created"

        monkeypatch.setattr(
            dpush.DeposcoPushClient, "_send",
            lambda self, url, headers, body: (sent.append(url), _FakeResponse())[-1],
        )
        outcome, msg = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
        assert sent, "the real gate should have let this call reach _send"
        assert outcome == "confirmed"


# ── Dismiss ───────────────────────────────────────────────────────────────────


class TestDismissTap:
    def test_harrison_can_dismiss(self):
        entry = _stage()
        outcome, msg = handler.process_dismiss_tap(entry["id"], handler.HARRISON_ID)
        assert outcome == "dismissed"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_DISMISSED

    def test_a_non_approver_cannot_dismiss(self):
        entry = _stage()
        outcome, _ = handler.process_dismiss_tap(entry["id"], JUSTIN_ID)
        assert outcome == "not_authorized"
        assert pending.get_entry(entry["id"])["state"] == pending.STATE_STAGED
