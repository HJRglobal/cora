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

    def test_live_asana_map_gives_f3e_catch_all_and_flags_the_shared_one(self):
        m = rl._asana_gid_entity_map()
        assert m.get("1215470928454227") == "F3E"           # [F3E] Operations -- General
        # the [HJRG] Operations -- General catch-all is shared by FNDR + HJRG in the map
        assert m.get("1215470834914137", "") in ("", "HJRG", "FNDR")


class TestExpiry:
    def test_15d_task_close_f3e_expires_by_name(self):
        now = _now()
        e = _row("task_close", "F3E", 15)
        assert rkr._expire_low_risk_mechanical([e], now, {}) == (1, 0, 0)
        assert e["state"] == "DISMISSED" and e["resolved_reason"] == "expired_low_risk"
        assert e["resolved_at"]

    @pytest.mark.parametrize("utype", ["task_close", "asana_task", "hubspot_note"])
    def test_all_three_classes_fndr(self, utype):
        e = _row(utype, "FNDR", 20)
        assert rkr._expire_low_risk_mechanical([e], _now(), {})[0] == 1

    def test_13d_is_too_young(self):
        e = _row("task_close", "F3E", 13)
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0)
        assert e["state"] == "PENDING"

    def test_surfacing_restarts_the_clock(self):
        """Created 20d ago but first shown 5d ago -> the reviewer has had 5 days."""
        shown = (_now() - timedelta(days=5)).timestamp()
        e = _row("task_close", "F3E", 20, dm_ts=f"{shown:.6f}")
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0)

    def test_15d_known_answer_never_expires(self):
        e = _row("known_answer", "F3E", 15)
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0)
        assert e["state"] == "PENDING"

    def test_decision_capture_never_expires(self):
        e = _row("decision_capture", "F3E", 40)
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0)

    def test_lex_mechanical_never_expires(self):
        e = _row("task_close", "LEX", 40)
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 1)
        assert e["state"] == "PENDING"

    def test_other_entities_never_expire(self):
        rows = [_row("asana_task", ent, 30, tag=ent) for ent in ("OSN", "HJRG", "UFL", "BDM")]
        assert rkr._expire_low_risk_mechanical(rows, _now(), {}) == (0, 0, 4)
        assert all(r["state"] == "PENDING" for r in rows)

    def test_unresolvable_entity_is_a_refusal_and_counted(self):
        e = _row("task_close", "", 30, description='Possible task completion: "x" (assigned to Y)',
                 payload={"task_url": "https://app.asana.com/1/1/task/9"})
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 1, 0)
        assert e["state"] == "PENDING"

    def test_reviewer_reaction_blocks_expiry(self, monkeypatch):
        e = _row("task_close", "F3E", 30, dm_ts="1700.5")
        monkeypatch.setattr(rkr, "_answered_by_an_approver", lambda entry, actors: True)
        assert rkr._expire_low_risk_mechanical([e], _now(), {"1700.5": {"U0B2RM2JYJ1"}}) == (0, 0, 0)

    def test_malformed_timestamp_is_never_expired(self):
        e = _row("task_close", "F3E", 30)
        e["proposed_at"] = "not-a-date"
        assert rkr._expire_low_risk_mechanical([e], _now(), {}) == (0, 0, 0)

    def test_pass_runs_before_escalation_in_step0(self):
        import inspect
        src = inspect.getsource(rkr.main)
        assert src.index("_expire_low_risk_mechanical(entries, now, answered_ts)") < \
            src.index("_escalate_stale_mechanical(entries, now, answered_ts)")
        assert 'if low_risk_expired or low_risk_unresolved or low_risk_other:' in src


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
