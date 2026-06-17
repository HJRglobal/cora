"""Tests for the knowledge-gap autofill engine (gap_autofill.py, 2026-06-07).

Layer A: source string assertions against app.py / run_knowledge_review.py /
deployment script / owners map.
Layer B: unit tests against gap_autofill with env-overridden paths.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import gap_autofill as ga


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _gap(ts=None, entity="F3E", question="What is the F3 Pure launch date?",
         gap="Pure launch date not in KB", channel="f3e-leadership"):
    return {"ts": ts or _iso(), "entity": entity, "question": question,
            "gap": gap, "channel": channel}


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    """Redirect every gap_autofill path to tmp."""
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(tmp_path / "resolved.jsonl"))
    monkeypatch.setenv("GAP_AUTOFILL_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("GAP_ASK_PENDING_PATH", str(tmp_path / "asks.json"))
    monkeypatch.setenv("GAP_DOMAIN_OWNERS_PATH", str(tmp_path / "owners.yaml"))
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path / "known-answers"))
    return tmp_path


def _write_gaps(tmp_path, gaps):
    (tmp_path / "gaps.jsonl").write_text(
        "\n".join(json.dumps(g) for g in gaps) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Layer B -- gap loading
# ---------------------------------------------------------------------------

class TestLoadOpenGaps:
    def test_missing_log_returns_empty(self, paths):
        assert ga.load_open_gaps() == []

    def test_loads_unresolved_gaps(self, paths):
        _write_gaps(paths, [_gap(ts="2026-06-01T00:00:00+00:00")])
        gaps = ga.load_open_gaps()
        assert len(gaps) == 1
        assert gaps[0]["entity"] == "F3E"

    def test_skips_resolved(self, paths):
        ts = "2026-06-01T00:00:00+00:00"
        _write_gaps(paths, [_gap(ts=ts)])
        (paths / "resolved.jsonl").write_text(
            json.dumps({"id": ts, "action": "answer"}) + "\n", encoding="utf-8")
        assert ga.load_open_gaps() == []

    def test_skips_state_handled(self, paths):
        ts = "2026-06-01T00:00:00+00:00"
        _write_gaps(paths, [_gap(ts=ts)])
        ga.save_state({ts: {"state": "proposed"}})
        assert ga.load_open_gaps() == []

    def test_skips_malformed_and_empty(self, paths):
        (paths / "gaps.jsonl").write_text(
            "not json\n" + json.dumps({"ts": _iso(), "gap": "", "question": "x"})
            + "\n" + json.dumps(_gap()) + "\n", encoding="utf-8")
        assert len(ga.load_open_gaps()) == 1


class TestGapAge:
    def test_age_hours(self):
        assert 71 < ga.gap_age_hours(_gap(ts=_iso(hours_ago=72))) < 73

    def test_bad_ts_is_zero(self):
        assert ga.gap_age_hours({"ts": "garbage"}) == 0.0


# ---------------------------------------------------------------------------
# Layer B -- entity scoping + evidence filtering
# ---------------------------------------------------------------------------

class TestEntityScope:
    def test_plain_entity(self):
        assert ga._entity_scope("F3E") == ("F3E", None)

    def test_lex_sub_entity(self):
        assert ga._entity_scope("LEX-LLC") == ("LEX", "LEX-LLC")

    def test_empty_defaults_fndr(self):
        assert ga._entity_scope("") == ("FNDR", None)


class _FakeKB:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self._results


def _chunk(source="slack", distance=0.5, content="Tommy said launch is 6/15",
           title="#f3e-leadership", date_modified=1780000000):
    return SimpleNamespace(source=source, distance=distance, content=content,
                           title=title, date_modified=date_modified)


class TestSearchSlackEvidence:
    def test_filters_non_slack_sources(self, paths):
        kb = _FakeKB([_chunk(source="fireflies"), _chunk(source="slack")])
        out = ga.search_slack_evidence(kb, _gap())
        assert len(out) == 1
        assert out[0].source == "slack"

    def test_filters_distance(self, paths):
        kb = _FakeKB([_chunk(distance=2.0), _chunk(distance=0.9)])
        out = ga.search_slack_evidence(kb, _gap())
        assert len(out) == 1

    def test_filters_phi_content(self, paths):
        kb = _FakeKB([_chunk(content="client name John Doe care plan update"),
                      _chunk(content="launch date confirmed")])
        out = ga.search_slack_evidence(kb, _gap())
        assert len(out) == 1

    def test_lex_sub_entity_passed_through(self, paths):
        kb = _FakeKB([])
        ga.search_slack_evidence(kb, _gap(entity="LEX-LLC"))
        assert kb.calls[0]["entity"] == "LEX"
        assert kb.calls[0]["sub_entity"] == "LEX-LLC"

    def test_kb_error_returns_empty(self, paths):
        class _Boom:
            def search(self, **kw):
                raise RuntimeError("db locked")
        assert ga.search_slack_evidence(_Boom(), _gap()) == []

    def test_sources_env_override(self, paths, monkeypatch):
        monkeypatch.setenv("GAP_AUTOFILL_SOURCES", "slack,fireflies")
        kb = _FakeKB([_chunk(source="fireflies")])
        assert len(ga.search_slack_evidence(kb, _gap())) == 1


# ---------------------------------------------------------------------------
# Layer B -- draft_answer (fail-closed)
# ---------------------------------------------------------------------------

class TestDraftAnswer:
    def test_insufficient_evidence_returns_none(self, paths):
        assert ga.draft_answer(_gap(), [_chunk()]) is None

    def test_no_api_key_returns_none(self, paths, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert ga.draft_answer(_gap(), [_chunk(), _chunk()]) is None

    def _fake_anthropic(self, monkeypatch, response_text):
        fake = types.ModuleType("anthropic")

        class _Msg:
            content = [SimpleNamespace(text=response_text)]

        class _Messages:
            def create(self, **kw):
                return _Msg()

        class _Client:
            def __init__(self, api_key=""):
                self.messages = _Messages()

        fake.Anthropic = _Client
        monkeypatch.setitem(sys.modules, "anthropic", fake)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def test_answerable_verdict(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, json.dumps({
            "answerable": True, "answer": "Pure launches 6/15.",
            "confidence": "HIGH", "citation": "excerpt 1"}))
        out = ga.draft_answer(_gap(), [_chunk(), _chunk()])
        assert out == {"answer": "Pure launches 6/15.", "confidence": "HIGH",
                       "citation": "excerpt 1"}

    def test_not_answerable_returns_none(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, json.dumps({
            "answerable": False, "answer": "", "confidence": "LOW", "citation": ""}))
        assert ga.draft_answer(_gap(), [_chunk(), _chunk()]) is None

    def test_garbage_json_returns_none(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, "I think the answer might be...")
        assert ga.draft_answer(_gap(), [_chunk(), _chunk()]) is None

    def test_phi_answer_rejected(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, json.dumps({
            "answerable": True,
            "answer": "The client name is John, diagnosis on his care plan is X.",
            "confidence": "HIGH", "citation": "excerpt 1"}))
        assert ga.draft_answer(_gap(), [_chunk(), _chunk()]) is None

    def test_bad_confidence_normalized(self, paths, monkeypatch):
        self._fake_anthropic(monkeypatch, json.dumps({
            "answerable": True, "answer": "Yes.", "confidence": "VERY",
            "citation": ""}))
        out = ga.draft_answer(_gap(), [_chunk(), _chunk()])
        assert out["confidence"] == "MED"


# ---------------------------------------------------------------------------
# Layer B -- escalation eligibility + owner resolution
# ---------------------------------------------------------------------------

class TestShouldEscalate:
    def test_old_f3e_gap_escalates(self, paths):
        assert ga.should_escalate(_gap(ts=_iso(hours_ago=100))) is True

    def test_young_gap_does_not(self, paths):
        assert ga.should_escalate(_gap(ts=_iso(hours_ago=1))) is False

    def test_lex_never_escalates(self, paths):
        assert ga.should_escalate(_gap(ts=_iso(hours_ago=100), entity="LEX")) is False
        assert ga.should_escalate(_gap(ts=_iso(hours_ago=100), entity="LEX-LLC")) is False

    def test_phi_question_never_escalates(self, paths):
        g = _gap(ts=_iso(hours_ago=100),
                 question="what is the care plan for this client name?")
        assert ga.should_escalate(g) is False


class TestResolveOwner:
    def test_missing_map_returns_none(self, paths):
        assert ga.resolve_owner("F3E") is None

    def test_resolves_entity_and_default(self, paths):
        (paths / "owners.yaml").write_text(
            "owners:\n  F3E: U111\ndefault: U999\n", encoding="utf-8")
        assert ga.resolve_owner("F3E") == "U111"
        assert ga.resolve_owner("UNKNOWN") == "U999"

    def test_repo_map_has_required_entities(self):
        import yaml
        data = yaml.safe_load(
            (_REPO_ROOT / "data" / "maps" / "gap-domain-owners.yaml")
            .read_text(encoding="utf-8"))
        owners = data["owners"]
        for ent in ("F3E", "OSN", "BDM", "HJRP", "FNDR", "HJRG"):
            assert ent in owners, f"missing owner for {ent}"
        assert data["default"]  # Harrison fallback present


# ---------------------------------------------------------------------------
# Layer B -- escalation DM + pending-ask lifecycle
# ---------------------------------------------------------------------------

class _FakeSlack:
    def __init__(self):
        self.posts = []

    def conversations_open(self, users):
        return {"channel": {"id": f"D{users[0]}"}}

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": "1717000000.000100"}


class TestEscalateAndCapture:
    def _setup_owner(self, paths):
        (paths / "owners.yaml").write_text(
            "owners:\n  F3E: U111\ndefault: U999\n", encoding="utf-8")

    def test_escalate_records_pending_ask(self, paths):
        self._setup_owner(paths)
        slack = _FakeSlack()
        ask = ga.escalate_gap(_gap(), slack)
        assert ask is not None
        assert ask["target_user_id"] == "U111"
        assert ask["ask_message_ts"] == "1717000000.000100"
        assert len(slack.posts) == 1
        live = ga.load_pending_asks()
        assert ask["ask_id"] in live

    def test_thread_reply_matches(self, paths):
        self._setup_owner(paths)
        ask = ga.escalate_gap(_gap(), _FakeSlack())
        m = ga.match_pending_ask("U111", "1717000000.000100")
        assert m and m["ask_id"] == ask["ask_id"]

    def test_wrong_thread_does_not_match(self, paths):
        self._setup_owner(paths)
        ga.escalate_gap(_gap(), _FakeSlack())
        assert ga.match_pending_ask("U111", "9999999999.000001") is None

    def test_toplevel_single_ask_matches(self, paths):
        self._setup_owner(paths)
        ga.escalate_gap(_gap(), _FakeSlack())
        assert ga.match_pending_ask("U111", None) is not None

    def test_toplevel_blocked_when_disallowed(self, paths):
        self._setup_owner(paths)
        ga.escalate_gap(_gap(), _FakeSlack())
        assert ga.match_pending_ask("U111", None, allow_toplevel=False) is None

    def test_other_user_does_not_match(self, paths):
        self._setup_owner(paths)
        ga.escalate_gap(_gap(), _FakeSlack())
        assert ga.match_pending_ask("U222", None) is None

    def test_expired_ask_does_not_match(self, paths):
        self._setup_owner(paths)
        ask = ga.escalate_gap(_gap(), _FakeSlack())
        asks = ga.load_pending_asks()
        asks[ask["ask_id"]]["asked_at"] = _iso(hours_ago=ga.ASK_TTL_HOURS + 1)
        ga.save_pending_asks(asks)
        assert ga.match_pending_ask("U111", None) is None

    def test_record_answer_proposes_and_resolves(self, paths, monkeypatch):
        self._setup_owner(paths)
        ask = ga.escalate_gap(_gap(ts="2026-06-01T00:00:00+00:00"), _FakeSlack())
        proposed = []
        monkeypatch.setattr(
            "cora.knowledge_review.propose_update",
            lambda **kw: proposed.append(kw))
        ack = ga.record_ask_answer(ask, "Launch is June 15, confirmed with BCB.")
        assert "routed your answer to Harrison" in ack
        assert len(proposed) == 1
        assert proposed[0]["update_type"] == "known_answer"
        assert proposed[0]["payload"]["answer_source"] == "teammate_dm"
        asks = ga.load_pending_asks()
        assert asks[ask["ask_id"]]["state"] == "ANSWERED"
        state = ga.load_state()
        assert state["2026-06-01T00:00:00+00:00"]["state"] == "proposed"

    def test_decline_keeps_gap_open(self, paths, monkeypatch):
        self._setup_owner(paths)
        ask = ga.escalate_gap(_gap(ts="2026-06-01T00:00:00+00:00"), _FakeSlack())
        proposed = []
        monkeypatch.setattr(
            "cora.knowledge_review.propose_update",
            lambda **kw: proposed.append(kw))
        ack = ga.record_ask_answer(ask, "no idea, sorry")
        assert proposed == []
        assert "thanks for letting me know" in ack.lower()
        assert ga.load_pending_asks()[ask["ask_id"]]["state"] == "DECLINED"
        assert "2026-06-01T00:00:00+00:00" not in ga.load_state()

    def test_phi_reply_rejected(self, paths, monkeypatch):
        self._setup_owner(paths)
        ask = ga.escalate_gap(_gap(), _FakeSlack())
        proposed = []
        monkeypatch.setattr(
            "cora.knowledge_review.propose_update",
            lambda **kw: proposed.append(kw))
        ack = ga.record_ask_answer(
            ask, "the client name is John Doe and his care plan says...")
        assert proposed == []
        assert "protected" in ack.lower()


class TestShiftKeywordGuard:
    def test_shift_commands_detected(self):
        assert ga.is_shift_keyword("my schedule") is True
        assert ga.is_shift_keyword("when do I work next week?") is True
        assert ga.is_shift_keyword("help") is True

    def test_long_answers_not_flagged(self):
        assert ga.is_shift_keyword(
            "The launch got pushed to June 15 because BCB needed the new "
            "carton artwork before the production run could stop") is False


# ---------------------------------------------------------------------------
# Layer B -- executor (apply_known_answer)
# ---------------------------------------------------------------------------

class TestApplyKnownAnswer:
    def _payload(self, **over):
        p = {"gap_ts": "2026-06-01T00:00:00+00:00", "entity": "F3E",
             "question": "When does Pure launch?", "gap": "Pure launch date",
             "answer": "June 15, 2026.", "answer_source": "slack_kb",
             "citation": "excerpt 1"}
        p.update(over)
        return p

    def test_writes_known_facts_and_resolved(self, paths):
        ka_dir = paths / "known-answers"
        ka_dir.mkdir()
        (ka_dir / "f3e.md").write_text(
            "# F3E\n\n## Routing rules\n\n## Known facts\n\n## Other\n",
            encoding="utf-8")
        ok, msg = ga.apply_known_answer(self._payload())
        assert ok, msg
        content = (ka_dir / "f3e.md").read_text(encoding="utf-8")
        facts = content.split("## Known facts")[1].split("## Other")[0]
        assert "Q: When does Pure launch?" in facts
        assert "A: June 15, 2026." in facts
        resolved = [json.loads(l) for l in
                    (paths / "resolved.jsonl").read_text(encoding="utf-8").splitlines()]
        assert resolved[0]["id"] == "2026-06-01T00:00:00+00:00"
        assert resolved[0]["source"] == "gap_autofill"

    def test_creates_missing_file(self, paths):
        ok, _ = ga.apply_known_answer(self._payload(entity="OSN"))
        assert ok
        assert (paths / "known-answers" / "osn.md").exists()

    def test_lex_sub_entity_routes_to_lex_file(self, paths):
        ok, _ = ga.apply_known_answer(self._payload(entity="LEX-LLC"))
        assert ok
        assert (paths / "known-answers" / "lex.md").exists()

    def test_unknown_entity_routes_to_fndr(self, paths):
        ok, _ = ga.apply_known_answer(self._payload(entity="WAT"))
        assert ok
        assert (paths / "known-answers" / "fndr.md").exists()

    def test_empty_answer_fails_soft(self, paths):
        ok, msg = ga.apply_known_answer(self._payload(answer=""))
        assert not ok
        assert "no answer" in msg

    def test_resolved_gap_no_longer_open(self, paths):
        ts = "2026-06-01T00:00:00+00:00"
        _write_gaps(paths, [_gap(ts=ts)])
        assert len(ga.load_open_gaps()) == 1
        ok, _ = ga.apply_known_answer(self._payload(gap_ts=ts))
        assert ok
        assert ga.load_open_gaps() == []

    # --- idempotency (B6) ----------------------------------------------------
    def _seed_f3e(self, paths):
        ka_dir = paths / "known-answers"
        ka_dir.mkdir(exist_ok=True)
        (ka_dir / "f3e.md").write_text(
            "# F3E\n\n## Routing rules\n\n## Known facts\n\n## Other\n",
            encoding="utf-8")
        return ka_dir

    def test_idempotent_on_resolved_gap(self, paths):
        """Window A: a full apply completed but the update stayed PENDING (crash
        before resolve_update). The re-run must not duplicate the fact block or
        the resolved line."""
        ka_dir = self._seed_f3e(paths)
        ok1, _ = ga.apply_known_answer(self._payload())
        assert ok1
        ok2, msg2 = ga.apply_known_answer(self._payload())  # crash-recovery re-run
        assert ok2
        assert "already resolved" in msg2
        content = (ka_dir / "f3e.md").read_text(encoding="utf-8")
        assert content.count("A: June 15, 2026.") == 1
        ids = [json.loads(l)["id"] for l in
               (paths / "resolved.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert ids.count("2026-06-01T00:00:00+00:00") == 1

    def test_idempotent_crash_between_append_and_resolved(self, paths):
        """Window B: the .md append succeeded but the resolved-ledger write did
        not (crash between the two). The re-run must skip the duplicate append and
        still complete the resolved write."""
        ka_dir = self._seed_f3e(paths)
        ga.apply_known_answer(self._payload())
        (paths / "resolved.jsonl").write_text("", encoding="utf-8")  # simulate the lost write
        ok2, _ = ga.apply_known_answer(self._payload())
        assert ok2
        content = (ka_dir / "f3e.md").read_text(encoding="utf-8")
        assert content.count("A: June 15, 2026.") == 1
        resolved = [l for l in
                    (paths / "resolved.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(resolved) == 1

    def test_idempotent_content_dedup_blank_gap_ts(self, paths):
        """Blank gap_ts has no ledger key; the content-dedup guard alone must
        prevent a duplicate fact block on a re-run."""
        ka_dir = self._seed_f3e(paths)
        ga.apply_known_answer(self._payload(gap_ts=""))
        ga.apply_known_answer(self._payload(gap_ts=""))
        content = (ka_dir / "f3e.md").read_text(encoding="utf-8")
        assert content.count("A: June 15, 2026.") == 1


# ---------------------------------------------------------------------------
# Layer A -- wiring assertions
# ---------------------------------------------------------------------------

class TestWiring:
    def test_app_imports_gap_autofill(self):
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "from . import gap_autofill" in src

    def test_app_dm_path_captures_before_shift_handler(self):
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "gap_autofill.match_pending_ask" in src
        assert "gap_autofill.record_ask_answer" in src
        # capture must come before the shift-handler call in the DM block
        assert (src.index("gap_autofill.match_pending_ask")
                < src.index("osn_shift_handler.handle_dm(text=text"))

    def test_knowledge_review_executor_has_known_answer_branch(self):
        src = (_REPO_ROOT / "scripts" / "run_knowledge_review.py").read_text(
            encoding="utf-8")
        assert 'update_type == "known_answer"' in src
        assert "apply_known_answer" in src

    def test_run_script_exists_with_dry_run(self):
        src = (_REPO_ROOT / "scripts" / "run_gap_autofill.py").read_text(
            encoding="utf-8")
        assert "--dry-run" in src
        assert "load_open_gaps" in src
        assert "should_escalate" in src

    def test_ps1_is_ascii_and_uses_venv_python(self):
        raw = (_REPO_ROOT / "deployment" / "setup-gap-autofill-task.ps1").read_bytes()
        assert all(b <= 127 for b in raw), "PS1 must be ASCII-only (D-016)"
        text = raw.decode("ascii")
        assert r".venv\Scripts\python.exe" in text, "must use venv python (D-005)"
        assert "uv run" not in text

    def test_fail_closed_doctrine_documented(self):
        src = (_REPO_ROOT / "src" / "cora" / "gap_autofill.py").read_text(
            encoding="utf-8")
        assert "fail-closed" in src.lower()
        assert "D-011" in src
