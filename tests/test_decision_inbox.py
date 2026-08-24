"""Decisions lane (Fork 4, 2026-08-01) -- decision_capture -> Harrison one-tap
NON-canon inbox.

Locked invariants under test:
  * NEVER-EXPIRING: propose_update stamps no TTL; both drain expiry helpers
    skip the type (even legacy rows that still carry expires_at).
  * NEVER-AUTOWRITE-BY-TYPE: apply_knowledge_update refuses it, apply_autowrite
    refuses it at its own chokepoint, and the drain's decision lane never
    enters the autowrite scan.
  * LEX/PHI HARD-EXCLUDED FAIL-CLOSED: screen_decision at card-render AND at
    the durable write; a screening error counts as excluded.
  * NON-CANON: accepted decisions land in data/decisions-inbox.md + ledger
    only; nothing touches decisions.md / decisions-pending.md; promotion stays
    the Cowork cascade (D-011).
  * TOCTOU: taps run under _ONE_TAP_LOCK; the second concurrent tap no-ops.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

try:
    from cora import decision_inbox as di
    from cora import knowledge_review as kr
    import scripts.run_knowledge_review as rkr
    _IMPORT_OK = True
except Exception:  # noqa: BLE001
    _IMPORT_OK = False

pytestmark = pytest.mark.skipif(not _IMPORT_OK,
                                reason="cora imports unavailable on this mount")


def _decision(uid="dec-1", desc="[F3E] Decision: lock the Pure launch for 9/15.",
              payload=None, evidence="Tommy: we're locking Pure for 9/15.",
              confidence="HIGH", state="PENDING", **over):
    d = {
        "update_id": uid,
        "update_type": "decision_capture",
        "description": desc,
        "payload": {} if payload is None else payload,
        "source_evidence": evidence,
        "confidence": confidence,
        "state": state,
        "proposed_at": "2026-07-20T00:00:00+00:00",
        "resolved_at": None,
        "dm_message_ts": "",
        "dm_channel_id": "",
    }
    d.update(over)
    return d


@pytest.fixture
def inbox_env(tmp_path, monkeypatch):
    inbox = tmp_path / "decisions-inbox.md"
    ledger = tmp_path / "state" / "inbox-ledger.jsonl"
    monkeypatch.setenv("CORA_DECISIONS_INBOX_PATH", str(inbox))
    monkeypatch.setenv("CORA_DECISIONS_INBOX_LEDGER", str(ledger))
    return inbox, ledger


# ── screen_decision: LEX/PHI hard-exclusion, fail-closed ─────────────────────

class TestScreenDecision:
    def test_clean_fndr_decision_passes(self):
        excluded, reason = di.screen_decision(_decision())
        assert excluded is False and reason == ""

    def test_lex_entity_in_payload_excluded(self):
        u = _decision(payload={"entity": "LEX-LLC", "decision_text": "x y z"})
        assert di.screen_decision(u) == (True, "lex_entity")

    def test_lex_entity_lowercase_excluded(self):
        u = _decision(payload={"entity": "lex", "decision_text": "x"})
        assert di.screen_decision(u) == (True, "lex_entity")

    def test_lex_prefix_in_description_excluded(self):
        # The pass5 payload shape is empty -- entity rides the description prefix.
        u = _decision(desc="[LEX-LTS] Decision: adjust the schedule.", payload={})
        excluded, reason = di.screen_decision(u)
        assert excluded is True and reason in ("lex_entity", "lex_token")

    def test_lexington_token_in_text_excluded(self):
        u = _decision(desc="Decision: Lexington will move billing in-house.")
        assert di.screen_decision(u) == (True, "lex_token")

    def test_lex_token_in_payload_serialization_excluded(self):
        u = _decision(payload={"chunk_title": "lex-llc weekly sync"})
        assert di.screen_decision(u)[0] is True

    def test_complex_and_flex_do_not_false_positive(self):
        u = _decision(desc="Decision: the complex flex-schedule rollout is approved.")
        assert di.screen_decision(u) == (False, "")

    # D-051 remediation (lex-subentity-token-blind): bare sub-entity codes and
    # underscore/concatenated LEX forms are excluded too.
    def test_bare_subentity_codes_excluded(self):
        for txt in ("We decided to move the client into the LBHS Mesa house.",
                    "Decision: LTS scheduling moves to Thursdays.",
                    "Decision: the LLA program audit is approved."):
            excluded, reason = di.screen_decision(_decision(desc=txt))
            assert excluded is True and reason == "lex_token", txt

    def test_underscore_and_concatenated_lex_forms_excluded(self):
        for txt in ("Decision recorded in LEX_LLC_Contract.pdf",
                    "Decision: LexingtonServices billing moves in-house."):
            assert di.screen_decision(_decision(desc=txt))[0] is True, txt

    def test_plain_llc_alone_not_excluded(self):
        # Every HJR entity is an LLC -- the bare word must not false-positive.
        u = _decision(desc="Decision: the F3E LLC operating agreement is signed.")
        assert di.screen_decision(u) == (False, "")

    def test_phi_text_excluded(self):
        u = _decision(desc="Decision: update the care plan for the patient intake flow.")
        assert di.screen_decision(u) == (True, "phi")

    def test_screen_error_fails_closed(self, monkeypatch):
        import cora.phi_guard as pg
        def _boom(_text):
            raise RuntimeError("phi guard exploded")
        monkeypatch.setattr(pg, "is_any_phi", _boom)
        assert di.screen_decision(_decision()) == (True, "screen_error")

    def test_entity_of_prefers_payload_then_prefix(self):
        assert di.entity_of(_decision(payload={"entity": "HJRP"})) == "HJRP"
        assert di.entity_of(_decision(desc="[OSN] Decision: x", payload={})) == "OSN"
        assert di.entity_of(_decision(desc="no prefix here", payload={})) == ""


# ── apply_decision_accept: durable NON-canon write, idempotent ───────────────

class TestApplyDecisionAccept:
    def test_happy_path_writes_inbox_and_ledger(self, inbox_env):
        inbox, ledger = inbox_env
        ok, summary = di.apply_decision_accept(_decision())
        assert ok is True and "filed" in summary
        text = inbox.read_text(encoding="utf-8")
        assert "NON-CANON" in text                      # header states the contract
        assert "lock the Pure launch" in text
        assert "decision-inbox-id: dec-1" in text
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["update_id"] == "dec-1"

    def test_idempotent_second_call_no_duplicate(self, inbox_env):
        inbox, ledger = inbox_env
        assert di.apply_decision_accept(_decision())[0] is True
        ok, summary = di.apply_decision_accept(_decision())
        assert ok is True and "already filed" in summary
        assert inbox.read_text(encoding="utf-8").count("decision-inbox-id: dec-1") == 1
        assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1

    def test_crash_recovery_md_written_ledger_missing_converges(self, inbox_env):
        inbox, ledger = inbox_env
        # Simulate: md append landed, crash before the ledger append.
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            f"header\n## x\n- {di._uid_marker('dec-1')}\n", encoding="utf-8")
        ok, _ = di.apply_decision_accept(_decision())
        assert ok is True
        assert inbox.read_text(encoding="utf-8").count("decision-inbox-id: dec-1") == 1
        assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1

    def test_lex_refused_at_write(self, inbox_env):
        inbox, ledger = inbox_env
        ok, summary = di.apply_decision_accept(
            _decision(payload={"entity": "LEX-LLC"}))
        assert ok is False and summary.startswith("excluded:")
        assert not inbox.exists() and not ledger.exists()

    def test_phi_refused_at_write(self, inbox_env):
        inbox, _ = inbox_env
        ok, summary = di.apply_decision_accept(
            _decision(desc="Decision: change the patient diagnosis workflow."))
        assert ok is False and summary.startswith("excluded:")
        assert not inbox.exists()

    def test_empty_text_refused(self, inbox_env):
        ok, summary = di.apply_decision_accept(
            _decision(desc="", payload={}, evidence=""))
        assert ok is False and "empty" in summary

    def test_missing_update_id_refused(self, inbox_env):
        ok, _ = di.apply_decision_accept(_decision(uid=""))
        assert ok is False

    def test_only_expected_files_created(self, inbox_env, tmp_path):
        # Canon-boundary pin: the ONLY writes are the inbox md + the ledger.
        di.apply_decision_accept(_decision())
        created = sorted(str(p.relative_to(tmp_path))
                         for p in tmp_path.rglob("*") if p.is_file())
        assert created == ["decisions-inbox.md", str(Path("state") / "inbox-ledger.jsonl")]

    def test_default_paths_are_noncanon(self):
        # Defaults live under data/ (never KB-swept; md gitignored) -- and are
        # not the repo/founder decisions logs.
        assert di._DEFAULT_INBOX_PATH.name == "decisions-inbox.md"
        assert di._DEFAULT_INBOX_PATH.parent.name == "data"
        assert "decisions.md" != di._DEFAULT_INBOX_PATH.name
        assert "pending" not in di._DEFAULT_INBOX_PATH.name

    def test_inbox_stats(self, inbox_env):
        assert di.inbox_stats() == {"total": 0, "recent": 0}
        di.apply_decision_accept(_decision(uid="s1"))
        di.apply_decision_accept(_decision(uid="s2", desc="[OSN] Decision: two."))
        stats = di.inbox_stats()
        assert stats["total"] == 2 and stats["recent"] == 2


# ── never-expire: TTL at creation + both drain expiry helpers ────────────────

class TestNeverExpire:
    def test_propose_update_stamps_no_ttl(self, tmp_path):
        with patch.object(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "u.jsonl"):
            kr._SEEN_IDS_CACHE = None
            kr._ARCHIVE_IDS_CACHE = None
            kr.propose_update(
                update_id="ttl-dec", update_type=kr.UPDATE_TYPE_DECISION,
                description="d", payload={"entity": "FNDR"})
            e = json.loads((tmp_path / "u.jsonl").read_text(encoding="utf-8"))
        assert e["expires_at"] is None

    def test_decision_still_not_knowledge(self):
        # Its own lane: never enters Harrison's knowledge queue / autowrite scan.
        assert kr.is_knowledge_update("decision_capture", {}) is False
        assert rkr._is_knowledge_item({"update_type": "decision_capture"}) is False

    def test_auto_expire_unrouted_skips_decisions(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=14)
        entries = [
            # legacy decision row WITH a stamped, already-past expires_at
            _decision(uid="d-legacy",
                      proposed_at=(now - timedelta(days=40)).isoformat(),
                      expires_at=(now - timedelta(days=30)).isoformat()),
            # decision row with no expires_at, ancient
            _decision(uid="d-old",
                      proposed_at=(now - timedelta(days=400)).isoformat()),
            # a genuinely operational row for contrast. `generic`, not
            # hubspot_note: the mechanical three left this pass on 2026-08-20
            # (cq-6b014816819c / D-206) and now escalate instead. What is under
            # test here is that DECISION rows are skipped, so the contrast row
            # only has to be something this pass still claims.
            {"update_id": "op-old", "update_type": "generic",
             "state": "PENDING", "dm_message_ts": "", "payload": {},
             "proposed_at": (now - timedelta(days=40)).isoformat()},
        ]
        n = rkr._auto_expire_unrouted_operational(entries, cutoff, now)
        assert n == 1
        assert entries[0]["state"] == "PENDING"
        assert entries[1]["state"] == "PENDING"
        assert entries[2]["state"] == "DISMISSED"

    def test_auto_dismiss_stale_pending_skips_dmd_decisions(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=14)
        entries = [
            _decision(uid="d-dmd", dm_message_ts="111.222",
                      proposed_at=(now - timedelta(days=100)).isoformat()),
            {"update_id": "k-dmd", "update_type": "known_answer",
             "state": "PENDING", "dm_message_ts": "333.444", "payload": {},
             "proposed_at": (now - timedelta(days=100)).isoformat()},
        ]
        n = rkr._auto_dismiss_stale_pending(entries, cutoff, now)
        assert n == 1
        assert entries[0]["state"] == "PENDING"      # decision card never expires
        assert entries[1]["state"] == "DISMISSED"    # knowledge 14d rule unchanged

    def test_decision_not_in_operational_types(self):
        assert "decision_capture" not in rkr._OPERATIONAL_TYPES


# ── never-autowrite-by-type ──────────────────────────────────────────────────

class TestNeverAutowrite:
    def test_apply_knowledge_update_refuses_decisions(self):
        ok, summary = kr.apply_knowledge_update(_decision())
        assert ok is False and "not one-tap-approvable" in summary

    def test_apply_autowrite_refuses_decisions_by_type(self, tmp_path, inbox_env):
        with patch.object(kr, "_AUTOWRITE_AUDIT_PATH", tmp_path / "audit.jsonl"):
            ok, summary = kr.apply_autowrite(_decision(), tier=0, reason="auto_tier0")
            assert ok is False
            assert "never autowrite-eligible" in summary
            assert not (tmp_path / "audit.jsonl").exists()  # no audit row = no write

    def test_drain_autowrite_scan_never_sees_decisions(self, tmp_path, monkeypatch):
        """E2E: with autowrite fully on, a PENDING decision row still becomes a
        CARD (send_individual_dms w/ decision builder) -- never an auto-write."""
        (tmp_path / "proposed.jsonl").write_text(
            json.dumps(_decision(uid="d-e2e")) + "\n", encoding="utf-8")
        (tmp_path / "reply.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
        monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
        monkeypatch.setattr(kr, "_ARCHIVE_PATH", tmp_path / "archive.jsonl")
        kr._SEEN_IDS_CACHE = None
        kr._ARCHIVE_IDS_CACHE = None
        monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
        monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "all")  # fully on

        autowrite = MagicMock()
        monkeypatch.setattr(rkr, "apply_autowrite", autowrite)
        sent = MagicMock(return_value={"d-e2e": "222.333"})
        monkeypatch.setattr(rkr, "send_individual_dms", sent)
        monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")
        monkeypatch.setattr(rkr, "_route_operational_to_owners", lambda *a, **k: 0)
        monkeypatch.setattr(rkr, "correlate_reactions_to_updates", lambda: [])

        monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
        rkr.main()

        autowrite.assert_not_called()
        assert sent.call_count == 1
        assert sent.call_args.kwargs.get("block_builder") is kr.build_decision_blocks


# ── card blocks + drain lane ─────────────────────────────────────────────────

class TestDecisionCards:
    def test_blocks_carry_decision_action_ids(self):
        text, blocks = kr.build_decision_blocks(_decision())
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        ids = [el["action_id"] for el in actions["elements"]]
        assert ids == [kr.ACTION_DECISION_ACCEPT, kr.ACTION_DECISION_DISMISS]
        assert all(el["value"] == "dec-1" for el in actions["elements"])
        assert actions["block_id"].startswith("dc_actions_")

    def test_action_ids_distinct_from_knowledge(self):
        assert kr.ACTION_DECISION_ACCEPT != kr.ACTION_APPROVE
        assert kr.ACTION_DECISION_DISMISS != kr.ACTION_DISMISS

    def test_card_text_states_noncanon_and_never_expires(self):
        text = kr.format_decision_dm(_decision())
        assert "NON-canon" in text or "non-canon" in text.lower()
        assert "never expires" in text.lower()
        assert "decisions.md" in text  # names where promotion actually happens

    def test_send_individual_dms_default_builder_unchanged(self):
        client = MagicMock()
        client.conversations_open.return_value = {"channel": {"id": "D1"}}
        client.chat_postMessage.return_value = {"ts": "1.2"}
        u = {"update_id": "k1", "update_type": "known_answer",
             "description": "d", "payload": {}, "dm_message_ts": ""}
        res = kr.send_individual_dms([u], "xoxb-test", _client_factory=lambda: client)
        assert res == {"k1": "1.2"}
        blocks = client.chat_postMessage.call_args.kwargs["blocks"]
        actions = [b for b in blocks if b.get("type") == "actions"][0]
        assert actions["elements"][0]["action_id"] == kr.ACTION_APPROVE

    def test_screen_and_send_caps_and_excludes(self, tmp_path, monkeypatch):
        import logging
        resolved = MagicMock(return_value=True)
        monkeypatch.setattr(rkr, "resolve_update", resolved)
        sent = MagicMock(return_value={})
        monkeypatch.setattr(rkr, "send_individual_dms", sent)
        monkeypatch.setattr(rkr, "send_dm_to_harrison", MagicMock(return_value="h"))
        monkeypatch.setattr(rkr, "_patch_dm_ts", MagicMock())

        items = ([_decision(uid=f"ok{i}") for i in range(7)]
                 + [_decision(uid="lex1", payload={"entity": "LEX-LLC"})])
        n_sent, n_excluded = rkr._screen_and_send_decision_cards(
            items, "xoxb-test", logging.getLogger("t"))

        assert n_excluded == 1
        resolved.assert_called_once()
        assert resolved.call_args.args[0] == "lex1"
        assert resolved.call_args.args[1] == "DISMISSED"
        assert resolved.call_args.kwargs["reason"].startswith("lex_phi_excluded")
        # capped at 5 of the 7 renderable
        batch = sent.call_args.args[0]
        assert len(batch) == 5
        assert sent.call_args.kwargs.get("block_builder") is kr.build_decision_blocks

    def test_screen_and_send_failed_dm_stays_pending(self, tmp_path, monkeypatch):
        import logging
        monkeypatch.setattr(rkr, "resolve_update", MagicMock(return_value=True))
        monkeypatch.setattr(rkr, "send_individual_dms", MagicMock(return_value={}))
        monkeypatch.setattr(rkr, "send_dm_to_harrison", MagicMock(return_value=None))
        patch_ts = MagicMock()
        monkeypatch.setattr(rkr, "_patch_dm_ts", patch_ts)
        n_sent, _ = rkr._screen_and_send_decision_cards(
            [_decision()], "xoxb-test", logging.getLogger("t"))
        assert n_sent == 0
        patch_ts.assert_not_called()  # no ts -> stays unsent, retries next run

    def test_main_sends_decisions_when_no_knowledge(self, tmp_path, monkeypatch):
        """Regression pin: the knowledge-empty early-return must NOT skip the
        decision lane (cards send before it)."""
        (tmp_path / "proposed.jsonl").write_text(
            json.dumps(_decision(uid="only-dec")) + "\n", encoding="utf-8")
        (tmp_path / "reply.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
        monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
        monkeypatch.setattr(kr, "_ARCHIVE_PATH", tmp_path / "archive.jsonl")
        kr._SEEN_IDS_CACHE = None
        kr._ARCHIVE_IDS_CACHE = None
        monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
        monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "off")
        sent = MagicMock(return_value={"only-dec": "5.5"})
        monkeypatch.setattr(rkr, "send_individual_dms", sent)
        monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")
        monkeypatch.setattr(rkr, "_route_operational_to_owners", lambda *a, **k: 0)
        monkeypatch.setattr(rkr, "correlate_reactions_to_updates", lambda: [])

        monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
        rkr.main()

        assert sent.call_count == 1  # the decision card went out
        # and the ledger row got its dm_message_ts patched
        row = json.loads((tmp_path / "proposed.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
        assert row["dm_message_ts"] == "5.5"

    def test_dry_run_sends_and_resolves_nothing(self, tmp_path, monkeypatch):
        (tmp_path / "proposed.jsonl").write_text(
            json.dumps(_decision(uid="dr1")) + "\n", encoding="utf-8")
        (tmp_path / "reply.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
        monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
        kr._SEEN_IDS_CACHE = None
        kr._ARCHIVE_IDS_CACHE = None
        monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
        monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
        before = (tmp_path / "proposed.jsonl").read_bytes()
        sent = MagicMock()
        monkeypatch.setattr(rkr, "send_individual_dms", sent)
        monkeypatch.setattr(rkr, "correlate_reactions_to_updates", lambda: [])
        monkeypatch.setattr("sys.argv", ["run_knowledge_review.py", "--dry-run"])
        rkr.main()
        sent.assert_not_called()
        assert (tmp_path / "proposed.jsonl").read_bytes() == before


# ── one-tap processor: gate, TOCTOU, apply-first-then-resolve ────────────────

class TestProcessDecisionTap:
    def _seed(self, tmp_path, monkeypatch, *entries):
        p = tmp_path / "u.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in entries),
                     encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", p)
        kr._SEEN_IDS_CACHE = None
        kr._ARCHIVE_IDS_CACHE = None
        return p

    def test_non_harrison_refused(self, tmp_path, monkeypatch, inbox_env):
        self._seed(tmp_path, monkeypatch, _decision())
        outcome, msg = kr.process_decision_tap("dec-1", "U_SOMEONE", approve=True)
        assert outcome == "not_authorized"

    def test_not_found(self, tmp_path, monkeypatch, inbox_env):
        self._seed(tmp_path, monkeypatch)
        outcome, _ = kr.process_decision_tap(
            "ghost", kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "not_found"

    def test_dismiss(self, tmp_path, monkeypatch, inbox_env):
        p = self._seed(tmp_path, monkeypatch, _decision())
        outcome, _ = kr.process_decision_tap(
            "dec-1", kr.HARRISON_SLACK_USER_ID, approve=False)
        assert outcome == "dismissed"
        row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert row["state"] == "DISMISSED"

    def test_accept_files_and_resolves(self, tmp_path, monkeypatch, inbox_env):
        inbox, ledger = inbox_env
        p = self._seed(tmp_path, monkeypatch, _decision())
        outcome, msg = kr.process_decision_tap(
            "dec-1", kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "accepted"
        assert "non-canon" in msg.lower()
        assert inbox.exists() and "dec-1" in inbox.read_text(encoding="utf-8")
        row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert row["state"] == "APPROVED"
        assert row["resolved_reason"] == "one_tap_button"

    def test_accept_excluded_dismisses(self, tmp_path, monkeypatch, inbox_env):
        inbox, _ = inbox_env
        # A LEX row that somehow reached a card: the apply re-screen catches it.
        p = self._seed(tmp_path, monkeypatch,
                       _decision(payload={"entity": "LEX-LLC"}))
        outcome, msg = kr.process_decision_tap(
            "dec-1", kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "excluded"
        assert not inbox.exists()
        row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert row["state"] == "DISMISSED"
        assert row["resolved_reason"] == "lex_phi_excluded"

    def test_apply_io_failure_leaves_pending(self, tmp_path, monkeypatch, inbox_env):
        p = self._seed(tmp_path, monkeypatch, _decision())
        import cora.decision_inbox as dimod
        monkeypatch.setattr(dimod, "apply_decision_accept",
                            lambda u, via="": (False, "inbox write failed: disk"))
        outcome, _ = kr.process_decision_tap(
            "dec-1", kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "apply_failed"
        row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert row["state"] == "PENDING"  # retryable; card never expires

    def test_second_tap_already_resolved(self, tmp_path, monkeypatch, inbox_env):
        self._seed(tmp_path, monkeypatch, _decision())
        kr.process_decision_tap("dec-1", kr.HARRISON_SLACK_USER_ID, approve=True)
        outcome, _ = kr.process_decision_tap(
            "dec-1", kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "already_resolved"

    def test_concurrent_taps_exactly_one_wins(self, tmp_path, monkeypatch, inbox_env):
        inbox, ledger = inbox_env
        self._seed(tmp_path, monkeypatch, _decision())
        n_threads = 6
        barrier = threading.Barrier(n_threads)
        outcomes = []
        lock = threading.Lock()

        def tap():
            barrier.wait()
            out, _ = kr.process_decision_tap(
                "dec-1", kr.HARRISON_SLACK_USER_ID, approve=True)
            with lock:
                outcomes.append(out)

        threads = [threading.Thread(target=tap) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count("accepted") == 1
        assert outcomes.count("already_resolved") == n_threads - 1
        assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


# ── emoji-fallback executor + ack ────────────────────────────────────────────

class TestEmojiFallback:
    def test_executor_files_to_inbox(self, tmp_path, monkeypatch, inbox_env):
        import logging
        inbox, _ = inbox_env
        posts = []
        monkeypatch.setattr(rkr, "_post_to_slack",
                            lambda tok, ch, msg: posts.append((ch, msg)))
        ok = rkr._execute_approved_update(_decision(), "xoxb-test",
                                          logging.getLogger("t"))
        assert ok is True
        assert inbox.exists()
        assert posts and "non-canon inbox" in posts[0][1]
        assert "memory/decisions.md" not in posts[0][1]  # old advisory retired

    def test_executor_failure_is_truthful(self, tmp_path, monkeypatch, inbox_env):
        import logging
        monkeypatch.setattr(rkr, "_post_to_slack", lambda *a: None)
        ok = rkr._execute_approved_update(
            _decision(payload={"entity": "LEX-LLC"}), "xoxb-test",
            logging.getLogger("t"))
        assert ok is False  # LEX re-screen refused -> D2 ack must not say "filed"

    def test_ack_text_decision_branch(self):
        msg = rkr._ack_reaction_text("APPROVED", "decision_capture", success=True)
        assert "inbox" in msg.lower() and "non-canon" in msg.lower()
        fail = rkr._ack_reaction_text("APPROVED", "decision_capture", success=False)
        assert "inbox" not in fail.lower()


# ── weekly digest line (S2) ──────────────────────────────────────────────────

class TestDigestLine:
    def test_empty_inbox_no_line(self, inbox_env):
        import scripts.run_autowrite_digest as rad
        assert rad._decisions_inbox_line() == ""

    def test_line_carries_counts_and_filename(self, inbox_env):
        import scripts.run_autowrite_digest as rad
        di.apply_decision_accept(_decision(uid="dg1"))
        line = rad._decisions_inbox_line()
        # D-051 (digest-awaiting-count-monotonic): LIFETIME wording -- the line
        # must NOT claim an "awaiting promotion" backlog it cannot track.
        assert "1 all-time" in line and "(1 in the last 7d)" in line
        assert "decisions-inbox.md" in line
        assert "awaiting" not in line.lower()
        # C3 (cq-a46ebe458d92): and it must name the ACTOR and the LANE. These
        # rows are Harrison's own taps on decision cards (70/70 live rows are
        # via=one_tap_button), but the line rides a DM headlined "Cora
        # auto-learned this week" where a bare "accepted" read as machine output
        # -- and it was the only non-zero number in that DM, masking the 0/0/0.
        assert "Decision cards YOU filed" in line
        assert "not auto-writes" in line

    def test_line_fail_soft(self, inbox_env, monkeypatch):
        import scripts.run_autowrite_digest as rad
        monkeypatch.setattr(di, "inbox_stats",
                            lambda days=7: (_ for _ in ()).throw(RuntimeError("x")))
        assert rad._decisions_inbox_line() == ""

    def test_recent_inbox_activity_triggers_quiet_week_send(
            self, inbox_env, monkeypatch):
        import scripts.run_autowrite_digest as rad
        di.apply_decision_accept(_decision(uid="dg2"))
        monkeypatch.setattr(rad, "build_digest", lambda now, days=7: (
            {"this_week": 0, "prev_week": 0, "reverts_this_week": 0,
             "level": "off"}, []))
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(rad, "deliver", sent)
        monkeypatch.setattr("sys.argv", ["run_autowrite_digest.py"])
        assert rad.main() == 0
        sent.assert_called_once()

    def test_truly_quiet_week_still_skips(self, inbox_env, monkeypatch):
        import scripts.run_autowrite_digest as rad
        monkeypatch.setattr(rad, "build_digest", lambda now, days=7: (
            {"this_week": 0, "prev_week": 0, "reverts_this_week": 0,
             "level": "off"}, []))
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(rad, "deliver", sent)
        monkeypatch.setattr("sys.argv", ["run_autowrite_digest.py"])
        assert rad.main() == 0
        sent.assert_not_called()


# ── D-051 remediation (2026-08-01 review) ────────────────────────────────────

class TestScreenErrorNotTerminal:
    """D-051 screen-error-terminal-mass-dismiss: a transient screening failure
    must never DISMISS the pool -- rows stay PENDING and retry."""

    def test_drain_skips_pool_on_screen_error(self, monkeypatch):
        import logging
        monkeypatch.setattr(di, "screen_decision",
                            lambda u: (True, "screen_error"))
        resolved = MagicMock()
        monkeypatch.setattr(rkr, "resolve_update", resolved)
        sent = MagicMock(return_value={})
        monkeypatch.setattr(rkr, "send_individual_dms", sent)
        monkeypatch.setattr(rkr, "send_dm_to_harrison", MagicMock())
        n_sent, n_excluded = rkr._screen_and_send_decision_cards(
            [_decision(uid=f"d{i}") for i in range(4)], "xoxb-test",
            logging.getLogger("t"))
        resolved.assert_not_called()   # nothing dismissed
        sent.assert_not_called()       # nothing rendered either (fail closed)
        assert n_sent == 0 and n_excluded == 0

    def test_tap_screen_error_leaves_pending(self, tmp_path, monkeypatch, inbox_env):
        p = tmp_path / "u.jsonl"
        p.write_text(json.dumps(_decision()) + "\n", encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", p)
        kr._SEEN_IDS_CACHE = None
        kr._ARCHIVE_IDS_CACHE = None
        monkeypatch.setattr(di, "screen_decision", lambda u: (True, "screen_error"))
        outcome, _ = kr.process_decision_tap(
            "dec-1", kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "apply_failed"  # NOT excluded/dismissed
        row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert row["state"] == "PENDING"

    def test_deterministic_exclusion_still_dismisses(self, tmp_path, monkeypatch,
                                                     inbox_env):
        p = tmp_path / "u.jsonl"
        p.write_text(json.dumps(_decision(payload={"entity": "LEX-LLC"})) + "\n",
                     encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", p)
        kr._SEEN_IDS_CACHE = None
        kr._ARCHIVE_IDS_CACHE = None
        outcome, _ = kr.process_decision_tap(
            "dec-1", kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "excluded"
        row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert row["state"] == "DISMISSED"


class TestEmojiApplyFirstThenResolve:
    """D-051 emoji-resolve-before-apply (HIGH): the scheduled path must not
    strand an APPROVED-but-never-filed decision."""

    def _executor(self, update, monkeypatch, inbox_env):
        import logging
        monkeypatch.setattr(rkr, "_post_to_slack", lambda *a: None)
        resolved = MagicMock(return_value=True)
        monkeypatch.setattr(rkr, "resolve_update", resolved)
        ok = rkr._execute_approved_update(update, "xoxb-test",
                                          logging.getLogger("t"))
        return ok, resolved

    def test_executor_resolves_approved_only_on_success(self, monkeypatch, inbox_env):
        ok, resolved = self._executor(_decision(), monkeypatch, inbox_env)
        assert ok is True
        resolved.assert_called_once()
        assert resolved.call_args.args[1] == "APPROVED"
        assert resolved.call_args.kwargs["reason"] == "emoji_reaction"

    def test_executor_transient_failure_leaves_pending(self, monkeypatch, inbox_env):
        monkeypatch.setattr(di, "apply_decision_accept",
                            lambda u, via="": (False, "inbox write failed: disk"))
        ok, resolved = self._executor(_decision(), monkeypatch, inbox_env)
        assert ok is False
        resolved.assert_not_called()  # left PENDING -> correlate retries

    def test_executor_excluded_dismisses(self, monkeypatch, inbox_env):
        ok, resolved = self._executor(
            _decision(payload={"entity": "LEX-LLC"}), monkeypatch, inbox_env)
        assert ok is False
        resolved.assert_called_once()
        assert resolved.call_args.args[1] == "DISMISSED"
        assert resolved.call_args.kwargs["reason"] == "lex_phi_excluded"

    def test_main_defers_resolve_for_approved_decisions(self, tmp_path, monkeypatch,
                                                        inbox_env):
        """E2E: emoji-APPROVED decision -> row APPROVED only AFTER a successful
        filing; on filing failure the row stays PENDING."""
        inbox, _ = inbox_env
        row = _decision(uid="em1", dm_message_ts="111.222")
        (tmp_path / "proposed.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
        (tmp_path / "reply.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
        monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
        monkeypatch.setattr(kr, "_ARCHIVE_PATH", tmp_path / "archive.jsonl")
        kr._SEEN_IDS_CACHE = None
        kr._ARCHIVE_IDS_CACHE = None
        monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
        monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "off")
        reaction = {"action": "APPROVED", "channel_id": "D1", "message_ts": "111.222"}
        monkeypatch.setattr(rkr, "correlate_reactions_to_updates",
                            lambda: [(row, reaction)])
        monkeypatch.setattr(rkr, "_post_to_slack", lambda *a: None)
        monkeypatch.setattr(rkr, "_ack_correlated_reaction", MagicMock())
        monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")
        monkeypatch.setattr(rkr, "send_individual_dms", lambda *a, **k: {})
        monkeypatch.setattr(rkr, "_route_operational_to_owners", lambda *a, **k: 0)

        monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
        rkr.main()

        after = json.loads((tmp_path / "proposed.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
        assert after["state"] == "APPROVED"
        assert after["resolved_reason"] == "emoji_reaction"
        assert inbox.exists() and "em1" in inbox.read_text(encoding="utf-8")

    def test_main_failed_filing_stays_pending(self, tmp_path, monkeypatch, inbox_env):
        row = _decision(uid="em2", dm_message_ts="111.333")
        (tmp_path / "proposed.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
        (tmp_path / "reply.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
        monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
        monkeypatch.setattr(kr, "_ARCHIVE_PATH", tmp_path / "archive.jsonl")
        kr._SEEN_IDS_CACHE = None
        kr._ARCHIVE_IDS_CACHE = None
        monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
        monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "off")
        reaction = {"action": "APPROVED", "channel_id": "D1", "message_ts": "111.333"}
        monkeypatch.setattr(rkr, "correlate_reactions_to_updates",
                            lambda: [(row, reaction)])
        monkeypatch.setattr(di, "apply_decision_accept",
                            lambda u, via="": (False, "inbox write failed: disk"))
        monkeypatch.setattr(rkr, "_post_to_slack", lambda *a: None)
        monkeypatch.setattr(rkr, "_ack_correlated_reaction", MagicMock())
        monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")
        monkeypatch.setattr(rkr, "send_individual_dms", lambda *a, **k: {})
        monkeypatch.setattr(rkr, "_route_operational_to_owners", lambda *a, **k: 0)

        monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
        rkr.main()

        after = json.loads((tmp_path / "proposed.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
        assert after["state"] == "PENDING"  # retried at the next run


class TestSelfHealAndRecard:
    """D-051 step0-rmw-zombie: filed-but-unresolved rows re-resolve APPROVED;
    stale DM'd PENDING cards re-card instead of rotting invisibly."""

    def test_self_heal_and_recard(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        old_ts = f"{(now - timedelta(days=20)).timestamp():.6f}"
        fresh_ts = f"{(now - timedelta(days=5)).timestamp():.6f}"
        entries = [
            _decision(uid="filed", dm_message_ts=old_ts),          # -> healed
            _decision(uid="stale-card", dm_message_ts=old_ts),     # -> re-carded
            _decision(uid="fresh-card", dm_message_ts=fresh_ts),   # untouched
            _decision(uid="no-dm"),                                # untouched
            _decision(uid="bad-ts", dm_message_ts="not-a-ts"),     # untouched
            {"update_id": "kn", "update_type": "known_answer",
             "state": "PENDING", "dm_message_ts": old_ts},         # not our lane
        ]
        healed, recarded = rkr._self_heal_decisions(entries, {"filed"}, now)
        assert healed == 1 and recarded == 1
        assert entries[0]["state"] == "APPROVED"
        assert entries[0]["resolved_reason"] == "self_heal_inbox_filed"
        assert entries[1]["state"] == "PENDING"
        assert entries[1]["dm_message_ts"] == ""     # rejoins the unsent pool
        assert entries[2]["dm_message_ts"] == fresh_ts
        assert entries[3]["state"] == "PENDING"
        assert entries[4]["dm_message_ts"] == "not-a-ts"
        assert entries[5]["dm_message_ts"] == old_ts  # knowledge lane untouched

    def test_filed_update_ids_never_raises(self, inbox_env):
        assert di.filed_update_ids() == set()
        di.apply_decision_accept(_decision(uid="fid"))
        assert "fid" in di.filed_update_ids()


class TestCrossProcessLockAndNeverRaises:
    """D-051 cross-process-duplicate-inbox-filing + apply-accept-raises."""

    def test_busy_lock_returns_retryable(self, inbox_env, monkeypatch):
        monkeypatch.setattr(di, "_XPROC_LOCK_TIMEOUT_S", 0.2)
        lock = di._xproc_lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("held", encoding="utf-8")
        try:
            ok, summary = di.apply_decision_accept(_decision(uid="locked"))
        finally:
            lock.unlink()
        assert ok is False and "busy" in summary
        assert "locked" not in di.filed_update_ids()

    def test_stale_lock_cleared_and_apply_proceeds(self, inbox_env, monkeypatch):
        import os as _os
        lock = di._xproc_lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("stale", encoding="utf-8")
        old = time.time() - 120
        _os.utime(lock, (old, old))
        ok, _ = di.apply_decision_accept(_decision(uid="after-stale"))
        assert ok is True
        assert not lock.exists()  # released after the filing

    def test_non_utf8_inbox_never_raises(self, inbox_env):
        inbox, _ = inbox_env
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_bytes(b"# header\x93smart quote\x94\n")  # cp1252 bytes
        ok, summary = di.apply_decision_accept(_decision(uid="utf8"))
        assert ok is False
        assert "failed" in summary  # truthful, retryable -- no exception


# ── source-level canon-boundary + wiring pins ────────────────────────────────

class TestCanonBoundaryAndWiring:
    def test_decision_inbox_never_references_pending_canon(self):
        # No path to any canon decision file may appear ANYWHERE in the module
        # source (the docstring deliberately avoids the literals too).
        src = (Path(di.__file__)).read_text(encoding="utf-8")
        assert "decisions-pending" not in src
        assert "memory/decisions.md" not in src
        assert "STRATEGY_DECISIONS_PATH" not in src

    def test_app_registers_decision_actions(self):
        src = (_REPO / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "knowledge_review.ACTION_DECISION_ACCEPT" in src
        assert "knowledge_review.ACTION_DECISION_DISMISS" in src
        assert "process_decision_tap" in src

    def test_owner_labels_no_longer_carry_decisions(self):
        assert "decision_capture" not in rkr._OWNER_ITEM_LABELS

    def test_expire_script_protects_decisions(self):
        import scripts.expire_stale_operational_updates as esu
        assert "decision_capture" not in esu._DEFAULT_EXPIRE_TYPES
        assert "decision_capture" in esu._PROTECTED_TYPES

    def test_triage_script_protects_decisions(self):
        import scripts.triage_proposed_updates as tpu
        assert "decision_capture" not in tpu._DEFAULT_DISMISS_TYPES
        assert "decision_capture" in tpu._PROTECTED_TYPES
