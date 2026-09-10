"""C2 -- knowledge-backlog drain (Code #12; Q5 = (d) ruled 2026-09-01; classes + 14d
ruled 2026-09-08).

Contract under test:
  * EXACTLY task_close / asana_task / hubspot_note, entity FNDR or F3E, >= 14 days
    pending (from the later of creation and first surfacing), no reviewer action
    -> DISMISSED with the NAMED reason `expired_low_risk`;
  * a 15-day-old known_answer proposal does NOT expire; a LEX-entity mechanical
    row does NOT; an OSN/HJRG row does NOT; a row whose entity cannot be resolved
    does NOT (counted as unresolved); a row a reviewer reacted to does NOT;
  * the entity resolver reads payload.entity, then the description's bracketed /
    parenthesized KNOWN code, then the Asana project gid in task_url;
  * the weekly batch card renders counts by type x entity, the oldest rows
    content-screened (LEX counted, never rendered), the named expiry tally and a
    NEEDS HARRISON block; it sends once per digest day and never on other days;
  * flywheel_metrics counts expired_low_risk_7d and the digest line carries it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.run_knowledge_review as rkr
from cora import review_lanes as rl


def _now():
    return datetime.now(timezone.utc)


def _row(utype="task_close", entity="F3E", days=15, state="PENDING", dm_ts="", **extra):
    r = {
        "update_id": f"{utype}-{entity}-{days}-{extra.get('tag', '')}",
        "update_type": utype, "state": state,
        "description": extra.pop("description", f"[{entity}] something"),
        "payload": extra.pop("payload", {"entity": entity} if entity else {}),
        "proposed_at": (_now() - timedelta(days=days)).isoformat(),
        "dm_message_ts": dm_ts, "resolved_at": None,
    }
    r.update(extra)
    return r


class TestResolveEntity:
    def test_payload_entity_wins(self):
        assert rl.resolve_entity({"payload": {"entity": "f3e"}, "description": "[OSN] x"}) == "F3E"

    def test_bracketed_and_parenthesized_description_codes(self):
        assert rl.resolve_entity({"payload": {}, "description": "[BDM] Drive doc suggests missing task: x"}) == "BDM"
        assert rl.resolve_entity({"payload": {}, "description": 'Deal "Jesse" mentioned in slack (UFL) but no HubSpot activity in 7d'}) == "UFL"

    def test_non_entity_parentheticals_do_not_resolve(self):
        assert rl.resolve_entity({"payload": {}, "description": 'Possible task completion: "x" (assigned to Micah Kessler)'}) == ""
        assert rl.resolve_entity({"payload": {}, "description": "flagged (HIGH) by the sweep"}) == ""

    def test_task_url_project_gid_resolves_via_the_asana_map(self, monkeypatch):
        monkeypatch.setattr(rl, "_asana_gid_entity_map",
                            lambda: {"1215470928454227": "F3E", "1215470834914137": ""})
        u = {"payload": {"task_url": "https://app.asana.com/1/682743441507584/project/1215470928454227/task/1"},
             "description": "Possible task completion"}
        assert rl.resolve_entity(u) == "F3E"
        # the shared HJRG/FNDR catch-all is ambiguous -> unresolved, never a guess
        u2 = {"payload": {"task_url": "https://app.asana.com/1/682743441507584/project/1215470834914137/task/1"},
              "description": "Possible task completion"}
        assert rl.resolve_entity(u2) == ""

    def test_code_inside_a_quoted_task_name_resolves(self):
        """D-051 lens E F8: the task_close producer puts the code INSIDE the quoted
        task name; the `^`-anchored regex missed 8 live rows."""
        assert rl.resolve_entity({"payload": {}, "description":
                                  'Possible task completion: "[HJRG] Reinstate Reclaim or Calendly" -- slack says: "x"'}) == "HJRG"
        assert rl.resolve_entity({"payload": {}, "description":
                                  'Possible task completion: "[BDM] podcast episode 12 b-roll" -- gmail says: "y"'}) == "BDM"

    def test_two_different_known_codes_are_ambiguous(self):
        assert rl.resolve_entity({"payload": {}, "description":
                                  'Deal "Acme (F3E) Holdings" mentioned in slack (LEX) but no HubSpot note exists'}) == ""
        # the same code twice is not ambiguous
        assert rl.resolve_entity({"payload": {}, "description": "[OSN] x (OSN)"}) == "OSN"

    def test_live_asana_map_gives_f3e_catch_all_and_flags_the_shared_one(self):
        m = rl._asana_gid_entity_map()
        assert m.get("1215470928454227") == "F3E"           # [F3E] Operations -- General
        # the [HJRG] Operations -- General catch-all is shared by FNDR + HJRG in the map
        # exactly '' (D-051 lens D MED #6): 'FNDR' would let the drain auto-dismiss
        # rows on the shared FNDR/HJRG catch-all by name
        assert m.get("1215470834914137", "__missing__") == ""


class TestExpiry:
    def test_15d_task_close_f3e_expires_by_name(self):
        now = _now()
        e = _row("task_close", "F3E", 15)
        assert rkr._expire_low_risk_mechanical([e], now, {}) == (1, 0, 0, 0, 1)  # never carded (no dm_ts)
        assert e["state"] == "DISMISSED" and e["resolved_reason"] == "expired_low_risk"
        assert e["surfaced"] is False
        assert e["resolved_at"]

    @pytest.mark.parametrize("utype", ["task_close", "asana_task", "hubspot_note"])
    def test_all_three_classes_fndr(self, utype):
        e = _row(utype, "FNDR", 20)
        assert rkr._expire_low_risk_mechanical([e], _now(), {})[0] == 1

    def test_13d_is_too_young(self):
        e = _row("task_close", "F3E", 13)
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0, 0, 0)
        assert e["state"] == "PENDING"

    def test_surfacing_restarts_the_clock(self):
        """Created 20d ago but first shown 5d ago -> the reviewer has had 5 days."""
        shown = (_now() - timedelta(days=5)).timestamp()
        e = _row("task_close", "F3E", 20, dm_ts=f"{shown:.6f}")
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0, 0, 0)

    def test_15d_known_answer_never_expires(self):
        e = _row("known_answer", "F3E", 15)
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0, 0, 0)
        assert e["state"] == "PENDING"

    def test_decision_capture_never_expires(self):
        e = _row("decision_capture", "F3E", 40)
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0, 0, 0)

    def test_lex_mechanical_never_expires(self):
        e = _row("task_close", "LEX", 40)
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 1, 0, 0)
        assert e["state"] == "PENDING"

    def test_other_entities_never_expire(self):
        rows = [_row("asana_task", ent, 30, tag=ent) for ent in ("OSN", "HJRG", "UFL", "BDM")]
        assert rkr._expire_low_risk_mechanical(rows, _now(), {}) == (0, 0, 4, 0, 0)
        assert all(r["state"] == "PENDING" for r in rows)

    def test_unresolvable_entity_is_a_refusal_and_counted(self):
        e = _row("task_close", "", 30, description='Possible task completion: "x" (assigned to Y)',
                 payload={"task_url": "https://app.asana.com/1/1/task/9"})
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 1, 0, 0, 0)
        assert e["state"] == "PENDING"

    def test_reviewer_reaction_blocks_expiry(self, monkeypatch):
        e = _row("task_close", "F3E", 30, dm_ts="1700.5")
        monkeypatch.setattr(rkr, "_answered_by_an_approver", lambda entry, actors: True)
        assert rkr._expire_low_risk_mechanical([e], _now(), {"1700.5": {"U0B2RM2JYJ1"}}) == (0, 0, 0, 0, 0)

    def test_content_screened_row_never_expires(self, monkeypatch):
        """D-051 lens E F3: the decision lane's content screen is a gate here too."""
        e = _row("task_close", "F3E", 30)
        monkeypatch.setattr(rl, "content_screen_excludes", lambda u: (True, "lex_token"))
        assert rl.is_low_risk_expirable(e, _now()) == (False, "content_screened")
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0, 1, 0)
        assert e["state"] == "PENDING"

    def test_live_shape_lex_token_under_an_f3e_entity_is_screened(self):
        """The live row lens E named: payload entity F3E, description carrying a LEX
        token -- 1 of the 53 the first run would have dismissed. Real screen."""
        e = _row("task_close", "F3E", 30, description=
                 'Possible task completion: "Confirm receipts" -- fireflies says: "mentioned in fireflies (LEX) client intake"')
        ok, why = rl.is_low_risk_expirable(e, _now())
        assert ok is False and why == "content_screened"

    def test_expired_rows_record_whether_a_reviewer_ever_saw_them(self):
        """D-051 lens E F4: 53 of 53 live-eligible rows had never been carded."""
        shown = (_now() - timedelta(days=20)).timestamp()
        carded = _row("task_close", "F3E", 40, dm_ts=f"{shown:.6f}", tag="carded")
        never = _row("task_close", "F3E", 40, tag="never")
        assert rkr._expire_low_risk_mechanical([carded, never], _now(), {}) == (2, 0, 0, 0, 1)
        assert carded["surfaced"] is True and never["surfaced"] is False

    def test_malformed_timestamp_is_never_expired(self):
        e = _row("task_close", "F3E", 30)
        e["proposed_at"] = "not-a-date"
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0, 0, 0)

    def test_pass_runs_before_escalation_in_step0(self):
        import inspect
        src = inspect.getsource(rkr.main)
        assert src.index("_expire_low_risk_mechanical(entries, now, answered_ts)") < \
            src.index("_escalate_stale_mechanical(entries, now, answered_ts)")
        assert 'if low_risk_expired or low_risk_unresolved or low_risk_other or low_risk_screened:' in src


class TestBatchCard:
    def _pending(self):
        return [
            _row("task_close", "F3E", 41, tag="a", description="Possible task completion: \"Confirm receipts\" -- gmail says: \"x\""),
            _row("hubspot_note", "UFL", 30, tag="b", description='Deal "Jesse" mentioned in slack (UFL) but no HubSpot activity'),
            _row("asana_task", "LEX", 33, tag="c", description="[LEX] client census review"),
            _row("known_answer", "F3E", 9, tag="d", description="F3E Anaheim address known-answer"),
            _row("decision_capture", "FNDR", 3, tag="e", description="decision x"),
        ]

    def test_card_counts_screens_and_names_needs_harrison(self, monkeypatch):
        monkeypatch.setattr(rl, "content_screen_excludes", lambda u: (False, ""))
        text = rkr._build_mechanical_batch_card(self._pending(), _now(), expired_this_run=2, expired_7d=5)
        assert "3 bookkeeping item(s) PENDING" in text
        assert "task_close 1" in text and "hubspot_note 1" in text and "asana_task 1" in text
        assert "LEX 1 (count only)" in text
        assert "client census" not in text            # LEX row counted, never rendered
        assert "Confirm receipts" in text              # oldest non-LEX row rendered
        assert "expired_low_risk" in text and "2 this run" in text and "5 in 7d" in text
        assert "*Needs Harrison*" in text and "1 knowledge item(s) + 1 decision card(s)" in text
        assert "Anaheim" in text
        assert "carries no buttons" in text

    def test_withheld_counts_cover_the_whole_pool_not_just_the_cap(self, monkeypatch):
        """D-051 lens E F13: the loop used to break at the cap, so '4 withheld' was
        a count of the rows scanned, not of the pool."""
        rows = [_row("task_close", "F3E", 50 - i, tag=f"r{i}", description=f"row {i}") for i in range(12)]
        monkeypatch.setattr(rl, "content_screen_excludes",
                            lambda u: (True, "phi") if u["description"] in ("row 9", "row 10", "row 11", "row 8")
                            else (False, ""))
        text = rkr._build_mechanical_batch_card(rows, _now(), expired_this_run=0, expired_7d=0)
        assert "4 withheld by the content screen" in text and "whole pending pool" in text
        assert "  8. " in text and "  9. " not in text  # still capped at 8 rendered

    def test_never_carded_and_unavailable_7d_are_named(self, monkeypatch):
        monkeypatch.setattr(rl, "content_screen_excludes", lambda u: (False, ""))
        text = rkr._build_mechanical_batch_card(self._pending(), _now(), expired_this_run=3,
                                                expired_7d=None, never_carded_this_run=2)
        assert "3 this run (2 of them never carded to a reviewer)" in text
        assert "unavailable (ledger unreadable)" in text

    def test_7d_count_dedups_live_and_archive_and_is_none_when_unreadable(self, tmp_path, monkeypatch):
        """D-051 lens E F6: a rotation crash window leaves one row in BOTH files;
        flywheel_metrics dedups -- the card must agree. Blind is None, never 0."""
        from cora import knowledge_review as kr
        row = {"update_id": "dup-1", "state": "DISMISSED", "resolved_reason": "expired_low_risk",
               "resolved_at": (_now() - timedelta(days=1)).isoformat()}
        arch = tmp_path / "archive.jsonl"
        arch.write_text(json.dumps(row) + "\n" + "torn{\n", encoding="utf-8")
        monkeypatch.setattr(kr, "load_proposed_updates", lambda: [dict(row)])
        monkeypatch.setattr(kr, "_ARCHIVE_PATH", arch)
        assert rkr._count_expired_low_risk_7d(_now()) == 1
        monkeypatch.setattr(kr, "load_proposed_updates", lambda: (_ for _ in ()).throw(OSError("disk")))
        assert rkr._count_expired_low_risk_7d(_now()) is None

    def test_dry_run_previews_on_an_off_day(self, tmp_path, monkeypatch, caplog):
        """D-051 lens E F12: the preview used to be reachable only on the day it also
        sends for real."""
        monkeypatch.setattr(rkr, "_MECHANICAL_BATCH_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(rl, "content_screen_excludes", lambda u: (False, ""))
        monkeypatch.setattr(rkr, "_is_digest_day", lambda: False)
        monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: pytest.fail("must not send"))
        log = __import__("logging").getLogger("t")
        caplog.set_level("INFO", logger="t")
        assert rkr._maybe_send_mechanical_batch_card(self._pending(), log, dry_run=True, expired_this_run=0) is False
        msgs = [r.getMessage() for r in caplog.records]
        assert any("would read" in m and "Mondays only" in m for m in msgs)
        assert not (tmp_path / "state.json").exists()

    def test_content_screen_withholds_a_row(self, monkeypatch):
        monkeypatch.setattr(rl, "content_screen_excludes",
                            lambda u: (True, "phi") if "Jesse" in str(u.get("description")) else (False, ""))
        text = rkr._build_mechanical_batch_card(self._pending(), _now(), expired_this_run=0, expired_7d=0)
        assert "Jesse" not in text and "1 withheld by the content screen" in text

    def test_sends_once_per_digest_day_and_never_off_day(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rkr, "_MECHANICAL_BATCH_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(rl, "content_screen_excludes", lambda u: (False, ""))
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        sent = []
        monkeypatch.setattr(rkr, "send_dm_to_harrison",
                            lambda text, token, _client_factory=None: sent.append(text) or "171.1")
        log = __import__("logging").getLogger("t")
        monkeypatch.setattr(rkr, "_is_digest_day", lambda: False)
        assert rkr._maybe_send_mechanical_batch_card(self._pending(), log, dry_run=False, expired_this_run=0) is False
        assert sent == []
        monkeypatch.setattr(rkr, "_is_digest_day", lambda: True)
        assert rkr._maybe_send_mechanical_batch_card(self._pending(), log, dry_run=False, expired_this_run=1) is True
        assert len(sent) == 1
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["message_ts"] == "171.1"
        # second run the same day: no second card
        assert rkr._maybe_send_mechanical_batch_card(self._pending(), log, dry_run=False, expired_this_run=0) is False
        assert len(sent) == 1

    def test_dry_run_logs_never_sends(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(rkr, "_MECHANICAL_BATCH_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(rl, "content_screen_excludes", lambda u: (False, ""))
        monkeypatch.setattr(rkr, "_is_digest_day", lambda: True)
        monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: pytest.fail("must not send"))
        log = __import__("logging").getLogger("t")
        caplog.set_level("INFO", logger="t")
        assert rkr._maybe_send_mechanical_batch_card(self._pending(), log, dry_run=True, expired_this_run=0) is False
        assert any("mechanical batch card would read" in r.getMessage() for r in caplog.records)
        assert not (tmp_path / "state.json").exists()

    def test_failed_send_retries_next_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rkr, "_MECHANICAL_BATCH_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(rl, "content_screen_excludes", lambda u: (False, ""))
        monkeypatch.setattr(rkr, "_is_digest_day", lambda: True)
        monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: None)
        log = __import__("logging").getLogger("t")
        assert rkr._maybe_send_mechanical_batch_card(self._pending(), log, dry_run=False, expired_this_run=0) is False
        assert not (tmp_path / "state.json").exists()

    def test_hooked_into_main_after_pending_is_read(self):
        import inspect
        src = inspect.getsource(rkr.main)
        assert src.index("pending = get_pending_updates()") < src.index("_maybe_send_mechanical_batch_card(")


class TestDigestCount:
    def test_flywheel_counts_expired_low_risk_7d(self, tmp_path, monkeypatch):
        from cora import flywheel_metrics as fm
        live = tmp_path / "live.jsonl"
        rows = [
            {"update_id": "1", "update_type": "task_close", "state": "DISMISSED",
             "proposed_at": (_now() - timedelta(days=20)).isoformat(),
             "resolved_at": (_now() - timedelta(days=1)).isoformat(),
             "resolved_reason": "expired_low_risk"},
            {"update_id": "2", "update_type": "task_close", "state": "DISMISSED",
             "proposed_at": (_now() - timedelta(days=20)).isoformat(),
             "resolved_at": (_now() - timedelta(days=1)).isoformat(),
             "resolved_reason": "expired_unrouted"},
            {"update_id": "3", "update_type": "task_close", "state": "DISMISSED",
             "proposed_at": (_now() - timedelta(days=30)).isoformat(),
             "resolved_at": (_now() - timedelta(days=9)).isoformat(),
             "resolved_reason": "expired_low_risk"},
        ]
        live.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        paths = fm._paths(None)
        monkeypatch.setattr(fm, "_paths", lambda repo_root=None: {**paths, "ledger_live": live,
                                                                 "ledger_archive": tmp_path / "none.jsonl"})
        m = fm.collect(update_baseline=False)
        assert m["expired_low_risk_7d"] == 1 and m["expired_unrouted_7d"] == 1
        assert any("expired_low_risk=1" in ln for ln in fm.format_lines(m))
