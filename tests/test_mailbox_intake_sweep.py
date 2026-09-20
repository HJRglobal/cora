"""Code #13 Rider 1 S-A -- cora@ into the DWD roster + a knowledge-review INTAKE
route (cq-8d16f1a557e5; finishes the 8/18 PROVISION ruling).

VERIFY-FIRST premises this file pins (both overturned the kickoff prose):
  * the gmail sweep NEVER proposes (its one write is kb.upsert_documents), so the
    intake route is a NEW consumer script over the shared info_intake.ingest
    chokepoint, and the roster row must set every chunk-ingest flag EXPLICITLY
    false (the defaults are true);
  * the "existing skip rules" were lines of the attachment filer's Claude PROMPT,
    so the Fireflies / Calendar / noreply skips here are CODE rules with their own
    growth-shape tests.

Fixtures use SYNTHETIC identities and synthetic LEX tokens only (D-145) -- no
client text, no real mail.
"""
from __future__ import annotations

import ast
import base64
import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from cora import info_intake as ii  # noqa: E402
from cora import knowledge_review as kr  # noqa: E402
from cora import gap_autofill as ga  # noqa: E402

CORA_MAILBOX = "cora@hjrglobal.com"
REAL_ROSTER = _REPO / "data" / "maps" / "monitored-email-accounts.yaml"


def _load():
    """Import scripts/run_mailbox_intake_sweep.py (the test_gmail_threaded_sweep
    convention). Importing it must write nothing (logging is lazy)."""
    sys.path.insert(0, str(_REPO / "scripts"))
    import run_mailbox_intake_sweep as m
    return m


# Synthetic roster: one flagged human (sender), one alias-only sender, one shared
# inbox, one disabled human, and the intake mailbox. Ids are fake.
_FIXTURE_ROSTER = """
accounts:
  - email: quill@example-hjr.test
    name: Quill Marsh
    enabled: true
    dwd_eligible: true
    thread_sweep: true
    attachment_filer: true
    drive_sweep: true
    entity_default: FNDR
    slack_user_id: U_QUILL
    fireflies_seat: true
  - email: rook@example-hjr.test
    name: Rook Vale (F3E)
    enabled: true
    dwd_eligible: true
    thread_sweep: true
    attachment_filer: true
    drive_sweep: true
    entity_default: F3E
    known_aliases:
      - rook@example-f3.test
    slack_user_id: U_ROOK
  - email: payables@example-hjr.test
    name: Payables Inbox
    enabled: true
    dwd_eligible: true
    thread_sweep: true
    attachment_filer: true
    drive_sweep: false
    entity_default: FNDR
  - email: gone@example-hjr.test
    name: Gone Person
    enabled: false
    dwd_eligible: true
    thread_sweep: true
    attachment_filer: true
    drive_sweep: true
    entity_default: FNDR
    slack_user_id: U_GONE
  - email: cora@hjrglobal.com
    name: Cora (system mailbox)
    enabled: true
    dwd_eligible: true
    thread_sweep: false
    attachment_filer: false
    drive_sweep: false
    entity_default: FNDR
    fireflies_seat: false
    intake_route: knowledge_review
"""


@pytest.fixture()
def roster(tmp_path) -> Path:
    p = tmp_path / "monitored-email-accounts.yaml"
    p.write_text(_FIXTURE_ROSTER, encoding="utf-8")
    return p


@pytest.fixture()
def sweep(monkeypatch, roster, tmp_path):
    """The script module pointed at the fixture roster + a tmp log dir. The
    watermark path is already redirected by conftest (MAILBOX_INTAKE_WATERMARK_PATH)."""
    m = _load()
    monkeypatch.setattr(m, "ACCOUNTS_PATH", roster)
    monkeypatch.setattr(m, "_LOG_DIR", tmp_path / "logs")
    return m


# ── fake Gmail service ──────────────────────────────────────────────────────
def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _gmail_msg(mid: str, *, sender: str, subject: str, body: str, internal_ms: int) -> dict:
    return {
        "id": mid, "threadId": f"t-{mid}", "internalDate": str(internal_ms),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": CORA_MAILBOX},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": _b64(body)},
        },
    }


class _FakeService:
    """users().messages().list/get(...).execute(num_retries=...) over a fixed set.
    Records every method name touched so a test can prove the surface is GET-only."""

    def __init__(self, messages: list[dict]):
        self._by_id = {m["id"]: m for m in messages}
        self.calls: list[str] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        self.calls.append("list")
        ids = [{"id": i} for i in self._by_id]
        return _Exec({"messages": ids[: kw.get("maxResults", 100)]})

    def get(self, **kw):
        self.calls.append("get")
        return _Exec(self._by_id[kw["id"]])


class _Exec:
    def __init__(self, payload):
        self._p = payload

    def execute(self, num_retries=0):
        return self._p


def _kr(pending=None):
    k = MagicMock()
    k.load_proposed_updates.return_value = pending or []
    k.UPDATE_TYPE_GENERIC = "generic"
    return k


_NOW_MS = 1_790_000_000_000


# ═════════════════════════════════════════════════════════════════════════════
# (a) the roster row + every loader's posture toward it
# ═════════════════════════════════════════════════════════════════════════════
class TestRealRosterRow:
    def _cora_row(self) -> dict:
        data = yaml.safe_load(REAL_ROSTER.read_text(encoding="utf-8"))
        rows = [a for a in data["accounts"] if a.get("email") == CORA_MAILBOX]
        assert len(rows) == 1, "exactly one cora@ row"
        return rows[0]

    def test_row_has_the_locked_posture(self):
        r = self._cora_row()
        assert r["name"] == "Cora (system mailbox)"
        assert r["enabled"] is True and r["dwd_eligible"] is True
        # EXPLICITLY false -- absent would default true and chunk-ingest the mailbox
        assert r["thread_sweep"] is False
        assert r["attachment_filer"] is False
        assert r["drive_sweep"] is False
        assert r["entity_default"] == "FNDR"
        assert r["fireflies_seat"] is False
        assert r["intake_route"] == "knowledge_review"
        assert "slack_user_id" not in r and "asana_gid" not in r

    def test_header_documents_the_new_key(self):
        head = REAL_ROSTER.read_text(encoding="utf-8").split("\naccounts:")[0]
        assert "intake_route: knowledge_review" in head
        assert "run_mailbox_intake_sweep.py" in head
        block = head[head.index("# intake_route:"):]
        assert block.isascii()

    def test_gmail_threaded_sweep_excludes_the_row(self):
        sys.path.insert(0, str(_REPO / "scripts"))
        import gmail_threaded_sweep as g
        emails = {a.get("email") for a in g._load_accounts()}
        assert CORA_MAILBOX not in emails

    def test_attachment_filer_excludes_the_row(self):
        from cora.connectors import attachment_filer as af
        emails = {a.get("email") for a in af.load_monitored_accounts()}
        assert CORA_MAILBOX not in emails

    def test_drive_sweep_filter_excludes_the_row(self):
        # run_drive_sweep's row filter is an inline comprehension inside main();
        # apply the SAME predicate and pin its source shape.
        src = (_REPO / "scripts" / "run_drive_sweep.py").read_text(encoding="utf-8")
        assert 'a.get("enabled") and a.get("dwd_eligible") and a.get("drive_sweep")' in src
        data = yaml.safe_load(REAL_ROSTER.read_text(encoding="utf-8"))
        swept = {a.get("email") for a in data["accounts"]
                 if a.get("enabled") and a.get("dwd_eligible") and a.get("drive_sweep")}
        assert CORA_MAILBOX not in swept

    def test_fireflies_coverage_pin_unchanged(self):
        from cora.connectors.fireflies_coverage import load_dwd_humans
        humans = load_dwd_humans()
        assert len(humans) == 10  # the 2026-07-01 right-size pin (test_fireflies_coverage)
        assert "Cora (system mailbox)" not in {h.name for h in humans}
        assert not any(CORA_MAILBOX in h.all_emails for h in humans)

    def test_fireflies_coverage_fixture_with_cora_row(self, roster):
        from cora.connectors.fireflies_coverage import load_dwd_humans
        humans = load_dwd_humans(roster)
        assert {h.name for h in humans} == {"Quill Marsh"}  # the only flagged human

    def test_historical_access_tolerates_the_row(self):
        from cora import historical_access as ha
        idx = ha._build_identity_index()  # must not raise
        # No slack id -> never a slack-owned mailbox; unknown owner chunks are
        # header-stripped fail-closed, and the row is never chunk-ingested anyway.
        assert not any(CORA_MAILBOX in emails for emails in idx.by_slack.values())

    def test_finance_receipts_digest_includes_the_row(self):
        # DOCUMENTED CONSEQUENCE (ruled dwd_eligible: true): the finance-receipt
        # digest sweeps 'all enabled DWD mailboxes', so cora@ is one of them. A
        # receipt emailed to cora@ would be classified/filed like any inbox's.
        # Flagged in the report as a design consequence, not an accident.
        from cora import finance_receipts as fr
        assert CORA_MAILBOX in fr._digest_accounts()

    def test_person_identity_tolerates_the_row(self):
        from cora import person_identity as pid
        pid.invalidate_cache()
        try:
            mm = pid._mailbox_map()  # must not raise
            # keyed on slack_user_id -> the row attaches to NO person
            assert not any(r.get("email") == CORA_MAILBOX for rows in mm.values() for r in rows)
        finally:
            pid.invalidate_cache()


# ═════════════════════════════════════════════════════════════════════════════
# (b) the consumer's roster loaders
# ═════════════════════════════════════════════════════════════════════════════
class TestLoaders:
    def test_intake_mailboxes_is_exactly_the_routed_row(self, sweep):
        rows = sweep.load_intake_mailboxes()
        assert [r["email"] for r in rows] == [CORA_MAILBOX]

    def test_other_route_value_is_refused(self, sweep, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text(_FIXTURE_ROSTER.replace("intake_route: knowledge_review",
                                             "intake_route: knowlege_review"), encoding="utf-8")
        assert sweep.load_intake_mailboxes(p) == []

    def test_disabled_routed_row_is_skipped(self, sweep, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text(_FIXTURE_ROSTER.replace(
            "    fireflies_seat: false\n    intake_route",
            "    fireflies_seat: false\n    enabled: false\n    intake_route"), encoding="utf-8")
        assert sweep.load_intake_mailboxes(p) == []

    def test_routed_row_with_a_sweep_flag_left_true_is_refused(self, sweep, tmp_path):
        # Posture guard: an intake mailbox that is ALSO chunk-ingested is refused.
        p = tmp_path / "r.yaml"
        p.write_text(_FIXTURE_ROSTER.replace(
            "    thread_sweep: false\n    attachment_filer: false\n    drive_sweep: false\n    entity_default: FNDR\n    fireflies_seat: false",
            "    attachment_filer: false\n    drive_sweep: false\n    entity_default: FNDR\n    fireflies_seat: false"),
            encoding="utf-8")
        assert sweep.load_intake_mailboxes(p) == []

    def test_sender_index_maps_email_and_alias_to_slack_id(self, sweep):
        idx = sweep.build_sender_index()
        assert idx["quill@example-hjr.test"] == ("U_QUILL", "Quill Marsh")
        assert idx["rook@example-hjr.test"] == ("U_ROOK", "Rook Vale")
        assert idx["rook@example-f3.test"] == ("U_ROOK", "Rook Vale")  # known_aliases
        assert "payables@example-hjr.test" not in idx  # no slack id -> never a sender
        assert "gone@example-hjr.test" not in idx      # disabled
        assert CORA_MAILBOX not in idx                 # the mailbox is not a sender


# ═════════════════════════════════════════════════════════════════════════════
# CODE skip rules (+ growth shape for every new regex)
# ═════════════════════════════════════════════════════════════════════════════
class TestSkipRules:
    @pytest.mark.parametrize("addr", [
        "noreply@fireflies.ai", "fred@app.fireflies.ai",
        "calendar-notification@google.com",
        "noreply@somewhere.test", "no-reply@somewhere.test", "no_reply@x.test",
        "notifications@github.test", "notification+abc@x.test",
        "mailer-daemon@googlemail.test", "postmaster@x.test",
        "do-not-reply@x.test", "donotreply@x.test", "alerts@x.test",
    ])
    def test_automated_senders(self, sweep, addr):
        assert sweep.is_automated_sender(addr) is True

    @pytest.mark.parametrize("addr", [
        "quill@example-hjr.test", "mynotifications@x.test", "notifications-team@x.test",
        "replyguy@x.test", "not-a-mailbox",
    ])
    def test_human_senders_not_automated(self, sweep, addr):
        assert sweep.is_automated_sender(addr) is False

    @pytest.mark.parametrize("subject", [
        "Invitation: Weekly sync @ Mon Sep 21 (cora@hjrglobal.com)",
        "Re: Invitation: Weekly sync", "Fwd: Updated invitation: Weekly sync",
        "Accepted: Weekly sync @ Mon", "Declined: Weekly sync", "Tentatively Accepted: X",
        "Canceled event: Weekly sync", "Your meeting recap: Weekly sync",
        "Meeting notes: Weekly sync", "Meeting summary - Weekly sync",
        "Fireflies has joined your meeting",
    ])
    def test_automated_subjects(self, sweep, subject):
        assert sweep.is_automated_subject(subject) is True

    @pytest.mark.parametrize("subject", [
        "Vendor change for the Tucson stoves", "Re: pricing note",
        "The invitation list for the launch party is final",  # not subject-leading
        "(no subject)", "",
    ])
    def test_human_subjects_pass(self, sweep, subject):
        assert sweep.is_automated_subject(subject) is False

    def test_skip_reason_order_and_fail_closed(self, sweep):
        idx = sweep.build_sender_index()
        k = dict(mailbox=CORA_MAILBOX, sender_index=idx)
        assert sweep.skip_reason(sender="Fireflies <noreply@fireflies.ai>", subject="Recap",
                                 body="x", **k) == sweep.SKIP_AUTOMATED_SENDER
        assert sweep.skip_reason(sender="Quill Marsh <quill@example-hjr.test>",
                                 subject="Accepted: sync", body="", **k) == sweep.SKIP_AUTOMATED_SUBJECT
        assert sweep.skip_reason(sender=CORA_MAILBOX, subject="note", body="x",
                                 **k) == sweep.SKIP_SELF
        assert sweep.skip_reason(sender="Stranger <stranger@outside.test>", subject="hi",
                                 body="x", **k) == sweep.SKIP_NON_ROSTER
        assert sweep.skip_reason(sender="quill@example-hjr.test", subject="", body="  ",
                                 **k) == sweep.SKIP_EMPTY
        assert sweep.skip_reason(sender="Quill Marsh <quill@example-hjr.test>",
                                 subject="one-line note", body="", **k) == ""

    def test_alias_sender_resolves(self, sweep):
        idx = sweep.build_sender_index()
        assert sweep.skip_reason(sender="rook@example-f3.test", subject="n", body="b",
                                 mailbox=CORA_MAILBOX, sender_index=idx) == ""

    # Growth-shape: every new regex stays linear on adversarial input (ReDoS class).
    def test_localpart_regex_growth_shape(self, sweep):
        for n in (1_000, 20_000):
            local = "no" + "-" * n + "reply"
            t0 = time.perf_counter()
            sweep.is_automated_sender(f"{local}@x.test")
            assert time.perf_counter() - t0 < 0.5
        t0 = time.perf_counter()
        sweep.is_automated_sender("noreply+" + "a." * 20_000 + "@x.test")
        assert time.perf_counter() - t0 < 0.5

    def test_subject_regex_growth_shape(self, sweep):
        for n in (1_000, 5_000):
            t0 = time.perf_counter()
            sweep.is_automated_subject("Re: " * n + "x" * 10_000)
            sweep.is_automated_subject("Re:" + " " * n + ":" * n)
            sweep.is_automated_subject("meeting " * n)
            assert time.perf_counter() - t0 < 0.5

    def test_intake_text_and_ts(self, sweep):
        assert sweep.intake_text("Subject line", "body") == "Subject line\nbody"
        assert sweep.intake_text("(no subject)", "just a body") == "just a body"
        assert sweep.intake_text("only a subject", "") == "only a subject"  # no size floor
        assert sweep.intake_ts(1_790_000_000_123, "m1") == "1790000000123"
        assert sweep.intake_ts("1790000000123", "m1") == "1790000000123"
        assert sweep.intake_ts(0, "m1") == "m1"
        assert sweep.intake_ts(None, "m1") == "m1"


# ═════════════════════════════════════════════════════════════════════════════
# (e) intake fixtures through the REAL ingest chokepoint
# ═════════════════════════════════════════════════════════════════════════════
class TestIntake:
    def _run(self, sweep, messages, *, apply, known_answers, pending=None):
        svc = _FakeService(messages)
        k = _kr(pending)
        row = sweep.load_intake_mailboxes()[0]
        with patch.object(ii, "knowledge_review", k):
            # ingest reads canon from KNOWN_ANSWERS_DIR (conftest tmp) -- point it explicitly
            with patch.object(ii, "_default_known_answers_dir", lambda: known_answers):
                s = sweep.sweep_mailbox(
                    row, sender_index=sweep.build_sender_index(), watermarks={},
                    apply=apply, max_messages=100, bootstrap_days=7, service=svc)
        return s, k, svc

    def test_roster_human_note_becomes_exactly_one_pending_proposal(self, sweep, tmp_path):
        msgs = [_gmail_msg("m1", sender="Quill Marsh <quill@example-hjr.test>",
                           subject="Tucson stove vendor",
                           body="The Tucson stove vendor is Apex Appliance as of this week.",
                           internal_ms=_NOW_MS)]
        s, k, svc = self._run(sweep, msgs, apply=True, known_answers=tmp_path / "ka")
        assert s["outcomes"] == {ii.QUEUED: 1}
        assert k.propose_update.call_count == 1
        kw = k.propose_update.call_args.kwargs
        assert kw["update_id"] == f"infocora-{_NOW_MS}"
        assert kw["update_type"] == "generic"
        assert kw["confidence"] == "MED" and kw["source_evidence"] == ""
        p = kw["payload"]
        assert p["source"] == "info-for-cora"       # R3 autowrite SOURCE exclusion applies
        assert p["intake_route"] == "mailbox"
        assert p["author_id"] == "U_QUILL" and p["author_name"] == "Quill Marsh"
        assert p["channel"] == "cora@ mailbox" and p["channel_id"] == ""
        assert p["permalink"] == ""                 # no bogus Slack archive URL
        assert p["message_ts"] == str(_NOW_MS)
        assert "Apex Appliance" in p["text"]
        # PENDING, never applied: the only knowledge_review touches are the pending
        # load + the proposal. No resolve, no apply, no autowrite.
        touched = {c[0] for c in k.method_calls}
        assert touched == {"load_proposed_updates", "propose_update"}
        assert set(svc.calls) == {"list", "get"}     # GET-only Gmail surface
        assert s["new_watermark"] > 0

    def test_fireflies_notification_is_skipped_by_the_code_rule(self, sweep, tmp_path):
        msgs = [_gmail_msg("f1", sender="Fireflies.ai <noreply@fireflies.ai>",
                           subject="Your meeting recap: Weekly sync",
                           body="Here is the recap of your meeting with action items.",
                           internal_ms=_NOW_MS)]
        s, k, _ = self._run(sweep, msgs, apply=True, known_answers=tmp_path / "ka")
        assert s["skips"] == {sweep.SKIP_AUTOMATED_SENDER: 1}
        assert s["outcomes"] == {}
        k.propose_update.assert_not_called()
        # and a Calendar invite from a ROSTER human is caught by the subject rule
        msgs = [_gmail_msg("c1", sender="quill@example-hjr.test",
                           subject="Invitation: Weekly sync @ Mon Sep 21",
                           body="You have been invited.", internal_ms=_NOW_MS)]
        s, k, _ = self._run(sweep, msgs, apply=True, known_answers=tmp_path / "ka")
        assert s["skips"] == {sweep.SKIP_AUTOMATED_SUBJECT: 1}
        k.propose_update.assert_not_called()

    def test_synthetic_lex_token_email_is_lex_refused_at_ingest(self, sweep, tmp_path):
        # D-145: synthetic token, never client text. The blanket LEX-content
        # refusal lives in ingest() and is inherited, not re-implemented.
        msgs = [_gmail_msg("l1", sender="quill@example-hjr.test",
                           subject="Kiosk vendor",
                           body="The LEX-ZQX99 site kiosk vendor changed to Northwind this month.",
                           internal_ms=_NOW_MS)]
        s, k, _ = self._run(sweep, msgs, apply=True, known_answers=tmp_path / "ka")
        assert s["outcomes"] == {ii.LEX_REFUSED: 1}
        k.propose_update.assert_not_called()

    def test_non_roster_sender_is_skipped_never_proposed(self, sweep, tmp_path):
        msgs = [_gmail_msg("n1", sender="Someone Outside <stranger@outside.test>",
                           subject="A fact for you",
                           body="The Tucson stove vendor is Apex Appliance as of this week.",
                           internal_ms=_NOW_MS)]
        s, k, _ = self._run(sweep, msgs, apply=True, known_answers=tmp_path / "ka")
        assert s["skips"] == {sweep.SKIP_NON_ROSTER: 1}
        k.propose_update.assert_not_called()

    def test_self_sent_is_skipped(self, sweep, tmp_path):
        msgs = [_gmail_msg("s1", sender=f"Cora <{CORA_MAILBOX}>", subject="x",
                           body="a note to myself", internal_ms=_NOW_MS)]
        s, k, _ = self._run(sweep, msgs, apply=True, known_answers=tmp_path / "ka")
        assert s["skips"] == {sweep.SKIP_SELF: 1}
        k.propose_update.assert_not_called()

    def test_dry_run_proposes_nothing(self, sweep, tmp_path):
        msgs = [_gmail_msg("d1", sender="quill@example-hjr.test", subject="Tucson stove vendor",
                           body="The Tucson stove vendor is Apex Appliance as of this week.",
                           internal_ms=_NOW_MS)]
        s, k, _ = self._run(sweep, msgs, apply=False, known_answers=tmp_path / "ka")
        assert s["outcomes"] == {ii.QUEUED: 1}          # classified as it WOULD queue
        k.propose_update.assert_not_called()             # ...but nothing written

    def test_ingest_error_freezes_the_watermark(self, sweep, tmp_path):
        msgs = [_gmail_msg("e1", sender="quill@example-hjr.test", subject="n",
                           body="The Tucson stove vendor is Apex Appliance as of this week.",
                           internal_ms=_NOW_MS)]
        svc = _FakeService(msgs)
        row = sweep.load_intake_mailboxes()[0]
        with patch.object(sweep.info_intake, "ingest",
                          return_value=ii.IntakeResult(ii.ERROR, detail="x")):
            s = sweep.sweep_mailbox(row, sender_index=sweep.build_sender_index(),
                                    watermarks={}, apply=True, max_messages=100,
                                    bootstrap_days=7, service=svc)
        assert s["new_watermark"] == 0

    def test_list_failure_skips_mailbox_without_crash(self, sweep):
        svc = MagicMock()
        svc.users.return_value.messages.return_value.list.side_effect = RuntimeError("boom")
        row = sweep.load_intake_mailboxes()[0]
        s = sweep.sweep_mailbox(row, sender_index={}, watermarks={}, apply=True,
                                max_messages=100, bootstrap_days=7, service=svc)
        assert s["error"] and s["new_watermark"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# main(): --dry-run default is a claim about EVERY write site (D-290)
# ═════════════════════════════════════════════════════════════════════════════
class TestMainWriteSites:
    def _main(self, sweep, monkeypatch, tmp_path, argv, messages):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(tmp_path / "sa.json"))
        marker = tmp_path / "task-runs.jsonl"
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(marker))
        svc = _FakeService(messages)
        monkeypatch.setattr(sweep, "_service", lambda mailbox: svc)
        k = _kr()
        with patch.object(ii, "knowledge_review", k), \
                patch.object(ii, "_default_known_answers_dir", lambda: tmp_path / "ka"):
            rc = sweep.main(argv)
        return rc, k, marker

    def test_default_is_dry_run_zero_writes(self, sweep, monkeypatch, tmp_path):
        wm = sweep.watermark_path()
        assert str(tmp_path) in str(wm) or "mailbox-intake-watermark" in wm.name
        assert not wm.exists()
        msgs = [_gmail_msg("d1", sender="quill@example-hjr.test", subject="Tucson stove vendor",
                           body="The Tucson stove vendor is Apex Appliance as of this week.",
                           internal_ms=_NOW_MS)]
        rc, k, marker = self._main(sweep, monkeypatch, tmp_path, [], msgs)
        assert rc == 0
        k.propose_update.assert_not_called()   # write site 1: the review ledger
        assert not wm.exists()                 # write site 2: the watermark
        assert not marker.exists()             # write site 3: the run marker
        # (the REAL data/state path is redirected by conftest + guarded in
        # _GUARDED_LEDGERS, so this test can never reach it by construction)

    def test_apply_writes_proposal_watermark_and_marker(self, sweep, monkeypatch, tmp_path):
        wm = sweep.watermark_path()
        msgs = [_gmail_msg("a1", sender="quill@example-hjr.test", subject="Tucson stove vendor",
                           body="The Tucson stove vendor is Apex Appliance as of this week.",
                           internal_ms=_NOW_MS)]
        rc, k, marker = self._main(sweep, monkeypatch, tmp_path, ["--apply"], msgs)
        assert rc == 0
        assert k.propose_update.call_count == 1
        marks = json.loads(wm.read_text(encoding="utf-8"))
        assert marks == {CORA_MAILBOX: pytest.approx(int(time.time()), abs=30)}
        assert marker.exists()
        row = json.loads(marker.read_text(encoding="utf-8").splitlines()[-1])
        assert row["task"] == "Cora - Mailbox Intake Sweep" and row["outputs"] == 1

    def test_missing_sa_key_exits_2_without_touching_anything(self, sweep, monkeypatch, tmp_path):
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
        monkeypatch.setattr(sweep, "_service", lambda mailbox: pytest.fail("must not build"))
        assert sweep.main(["--apply"]) == 2
        assert not sweep.watermark_path().exists()

    def test_mailbox_filter_unknown_is_a_noop(self, sweep, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(tmp_path / "sa.json"))
        monkeypatch.setattr(sweep, "_service", lambda mailbox: pytest.fail("must not build"))
        assert sweep.main(["--apply", "--mailbox", "nobody@x.test"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
# watermark: cap-aware + atomic + redirected + guarded
# ═════════════════════════════════════════════════════════════════════════════
class TestWatermark:
    def test_cap_aware(self, sweep):
        assert sweep.next_watermark(5, 100, 50, 1000) == 1000      # drained -> sync_start
        assert sweep.next_watermark(100, 100, 50, 1000) == 50      # capped -> newest processed
        assert sweep.next_watermark(100, 100, 0, 1000) == 0        # capped, nothing -> unchanged

    def test_roundtrip_atomic_and_env_redirected(self, sweep, tmp_path, monkeypatch):
        p = tmp_path / "wm.json"
        monkeypatch.setenv("MAILBOX_INTAKE_WATERMARK_PATH", str(p))
        assert sweep.read_watermarks() == {}
        sweep.write_watermarks({CORA_MAILBOX: 123})
        assert sweep.read_watermarks() == {CORA_MAILBOX: 123}
        assert not p.with_suffix(".json.tmp").exists()   # tmp replaced, not left behind
        p.write_text("{not json", encoding="utf-8")
        assert sweep.read_watermarks() == {}              # corrupt -> bootstrap, no raise

    def test_conftest_redirect_and_guard_row_present(self):
        src = (_REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
        assert 'monkeypatch.setenv("MAILBOX_INTAKE_WATERMARK_PATH"' in src
        assert '"data/state/mailbox-intake-watermark.json",' in src
        env = (_REPO / ".env.example").read_text(encoding="utf-8")
        assert "MAILBOX_INTAKE_WATERMARK_PATH=" in env
        assert "cora@hjrglobal.com" in env

    def test_default_path_is_the_guarded_one(self, sweep, monkeypatch):
        monkeypatch.delenv("MAILBOX_INTAKE_WATERMARK_PATH", raising=False)
        assert sweep.watermark_path() == _REPO / "data" / "state" / "mailbox-intake-watermark.json"

    def test_task_registry_row_and_marker_writer(self):
        data = yaml.safe_load((_REPO / "data" / "maps" / "scheduled-task-state.yaml")
                              .read_text(encoding="utf-8"))
        names = {e["name"] for e in data["run_markers"]}
        assert "Cora - Mailbox Intake Sweep" in names
        src = (_REPO / "scripts" / "run_mailbox_intake_sweep.py").read_text(encoding="utf-8")
        assert "run_marker.write(" in src


# ═════════════════════════════════════════════════════════════════════════════
# (d) the staged READ-ONLY probe + the registration PS1
# ═════════════════════════════════════════════════════════════════════════════
_WRITE_VERBS = frozenset({
    "insert", "patch", "update", "delete", "send", "modify", "create", "batchModify",
    "batchDelete", "trash", "untrash", "import_", "quickAdd", "move", "clear", "watch",
    "stop", "setLabels", "modifyLabels", "instances", "post", "put", "remove",
})


class TestProbeAndPs1:
    def test_probe_imports_without_network_and_issues_no_write_verbs(self):
        # Importing must not touch google libs or the network (lazy imports).
        sys.path.insert(0, str(_REPO / "scripts"))
        import probe_cora_mailbox_dwd as probe
        assert probe.DEFAULT_MAILBOX == CORA_MAILBOX
        src = (_REPO / "scripts" / "probe_cora_mailbox_dwd.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and not ast.unparse(n.func).startswith("sys.path.")}  # the src insert
        assert not (called & _WRITE_VERBS), called & _WRITE_VERBS
        assert {"list", "execute"} <= called
        # the calendar list rides the calendar.events SCOPE (write=True), still a read
        assert "write=True" in src
        # module-level imports are stdlib/dotenv only (no google at import time)
        top: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        assert not ({"google", "googleapiclient", "cora"} & top), top

    def test_probe_reports_class_never_payload(self, monkeypatch):
        sys.path.insert(0, str(_REPO / "scripts"))
        import probe_cora_mailbox_dwd as probe

        class _Boom(Exception):
            resp = type("R", (), {"status": 403})()

        def _bad(*a, **k):
            raise _Boom("secret payload text")

        import cora.connectors.gmail_reader as gr
        import cora.tools.calendar_client as cc
        monkeypatch.setattr(gr, "_build_service", _bad)
        monkeypatch.setattr(cc, "_build_service", _bad)
        ok, why = probe.probe_gmail(CORA_MAILBOX)
        assert ok is False and why == "_Boom HTTP 403" and "secret" not in why
        ok, why = probe.probe_calendar(CORA_MAILBOX)
        assert ok is False and "secret" not in why

    def test_ps1_is_ascii_windowless_and_apply(self):
        p = _REPO / "deployment" / "setup-mailbox-intake-task.ps1"
        src = p.read_text(encoding="utf-8")
        assert src.isascii(), "D-016: PS1 must be ASCII-only"
        assert '. "$PSScriptRoot\\_task-action.ps1"' in src          # run_hidden wrapper
        assert "New-WrappedTaskAction" in src
        assert "run_mailbox_intake_sweep.py" in src and "--apply" in src
        assert '$FireAt     = "06:10"' in src
        assert "-RunLevel Limited" in src
        assert "pin-scheduled-task-models.ps1 -Apply" in src

    def test_sweep_script_is_ascii(self):
        for name in ("run_mailbox_intake_sweep.py", "probe_cora_mailbox_dwd.py"):
            assert (_REPO / "scripts" / name).read_text(encoding="utf-8").isascii(), name


# ═════════════════════════════════════════════════════════════════════════════
# (c) "again at apply" (D-145): the info-for-cora generic's apply-time re-check
# ═════════════════════════════════════════════════════════════════════════════
class TestApplyTimeRecheck:
    """VERIFY-FIRST: the re-check EXISTED in gap_autofill.apply_contributed_note
    (PHI by content; LEX by ENTITY tag only) but a refusal left the row PENDING.
    This slice adds the CONTENT-keyed LEX check (same detector as ingest) and the
    'excluded:' -> DISMISSED lex_phi_excluded contract in both executors."""

    HARRISON = "U0B2RM2JYJ1"

    def _seed(self, tmp_path, monkeypatch, *, text, entity="FNDR", uid="ic-1"):
        ledger = tmp_path / "updates.jsonl"
        payload = {"text": text, "author_id": "U_QUILL", "author_name": "Quill Marsh",
                   "entity": entity, "channel": "cora@ mailbox", "channel_id": "",
                   "source": "info-for-cora", "message_ts": "1790000000000",
                   "permalink": "", "intake_route": "mailbox"}
        entry = {"update_id": uid, "update_type": "generic", "description": "d",
                 "payload": payload, "source_evidence": "", "confidence": "MED",
                 "state": "PENDING", "proposed_at": "2026-09-19T00:00:00+00:00",
                 "resolved_at": None, "dm_message_ts": "1790000000.0001",
                 "dm_channel_id": "D1"}
        ledger.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", ledger)
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path / "known-answers"))
        return ledger

    def _row(self, ledger, uid="ic-1"):
        for l in ledger.read_text(encoding="utf-8").splitlines():
            rec = json.loads(l)
            if rec.get("update_id") == uid:
                return rec
        return {}

    def test_apply_refuses_lex_content_under_a_non_lex_tag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        ok, summary = ga.apply_contributed_note(
            {"entity": "FNDR", "text": "The LEX-ZQX99 site kiosk vendor changed this month."})
        assert ok is False
        assert summary.startswith("excluded:") and "LEX" in summary
        assert not (tmp_path / "fndr.md").exists()

    def test_apply_refusals_keep_the_excluded_prefix_and_words(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        ok, s = ga.apply_contributed_note({"entity": "LEX-LLC", "text": "Staff use the new kiosk."})
        assert ok is False and s.startswith("excluded:") and "LEX" in s
        ok, s = ga.apply_contributed_note(
            {"entity": "OSN", "text": "Bob Smith's AHCCCS authorization is pending."})
        assert ok is False and s.startswith("excluded:") and "PHI" in s
        # a clean fact still writes (the re-check must not over-refuse)
        ok, _ = ga.apply_contributed_note(
            {"entity": "F3E", "text": "The Anaheim warehouse is at 123 Main St."})
        assert ok is True

    def test_lex_check_failure_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        with patch.object(ii, "is_lex_content", side_effect=RuntimeError("detector down")):
            ok, s = ga.apply_contributed_note({"entity": "F3E", "text": "A perfectly fine fact."})
        assert ok is False and s.startswith("excluded:")

    def test_one_tap_approve_dismisses_excluded_with_reason(self, tmp_path, monkeypatch):
        ledger = self._seed(tmp_path, monkeypatch,
                            text="The LEX-ZQX99 site kiosk vendor changed this month.")
        outcome, msg = kr.process_one_tap_action("ic-1", self.HARRISON, approve=True)
        assert outcome == "excluded" and "Dismissed" in msg
        row = self._row(ledger)
        assert row["state"] == "DISMISSED"
        assert row["resolved_reason"] == "lex_phi_excluded"
        assert not (tmp_path / "known-answers").exists()

    def test_one_tap_approve_of_a_clean_note_still_writes(self, tmp_path, monkeypatch):
        ledger = self._seed(tmp_path, monkeypatch,
                            text="The Anaheim warehouse is at 123 Main St.")
        outcome, _ = kr.process_one_tap_action("ic-1", self.HARRISON, approve=True)
        assert outcome == "approved"
        assert self._row(ledger)["state"] == "APPROVED"

    def test_one_tap_transient_failure_still_leaves_pending(self, tmp_path, monkeypatch):
        ledger = self._seed(tmp_path, monkeypatch, text="A fine durable fact about vendors.")
        with patch.object(kr, "apply_knowledge_update",
                          return_value=(False, "apply failed: disk")):
            outcome, _ = kr.process_one_tap_action("ic-1", self.HARRISON, approve=True)
        assert outcome == "apply_failed"
        assert self._row(ledger)["state"] == "PENDING"

    def test_scheduled_executor_dismisses_excluded(self, tmp_path, monkeypatch):
        import scripts.run_knowledge_review as rkr
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path / "ka"))
        monkeypatch.setattr(rkr, "_post_to_slack", lambda *a: None)
        resolved = MagicMock(return_value=True)
        monkeypatch.setattr(rkr, "resolve_update", resolved)
        update = {"update_id": "ic-2", "update_type": "generic", "description": "d",
                  "payload": {"entity": "FNDR", "source": "info-for-cora",
                              "text": "The LEX-ZQX99 site kiosk vendor changed this month."}}
        ok = rkr._execute_approved_update(update, "fake-slack-token-not-a-secret", logging.getLogger("t"))
        assert ok is False
        resolved.assert_called_once()
        assert resolved.call_args.args[1] == "DISMISSED"
        assert resolved.call_args.kwargs["reason"] == "lex_phi_excluded"
        assert not (tmp_path / "ka").exists()

    def test_scheduled_executor_transient_failure_leaves_pending(self, tmp_path, monkeypatch):
        import scripts.run_knowledge_review as rkr
        monkeypatch.setattr(rkr, "_post_to_slack", lambda *a: None)
        resolved = MagicMock(return_value=True)
        monkeypatch.setattr(rkr, "resolve_update", resolved)
        monkeypatch.setattr(ga, "apply_contributed_note",
                            lambda p: (False, "apply failed: disk"))
        update = {"update_id": "ic-3", "update_type": "generic", "description": "d",
                  "payload": {"entity": "FNDR", "source": "info-for-cora",
                              "text": "A fine durable fact about vendors."}}
        ok = rkr._execute_approved_update(update, "fake-slack-token-not-a-secret", logging.getLogger("t"))
        assert ok is False
        resolved.assert_not_called()
