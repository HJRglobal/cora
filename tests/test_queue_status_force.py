"""R14-9(a) (cq-323c8974fa02): a queue/card-STATUS question in Harrison's DM forces
the ledger-read tool before the model speaks.

THE INCIDENT (2026-09-21 08:44-08:47 AZ, DM D0B4CTD3B09; verbatim below). Harrison
asked three times whether his Monday-menu card presses had registered. All three
answers ran at tool_use=0 and denied the capability ("I don't have direct read access
to the card ledger", "outside my current context scope", "that needs a direct check
of the ledger"). VERIFY-FIRST overturned the kickoff premise that the read tool
existed: the bot had NO reader of the code-queue ledger (only the MCP surface the bot
model is never offered), so the denials were TRUE as built. This slice BUILDS the
read (code_queue.render_card_status via the new cora_queue_status tool) and forces it
on the intent (code_queue.is_queue_status_question), after every command / write
force and before the self-inventory force.

The truthful answer that morning was on disk: 20 of 21 carded rows carried a decision
event after the menu went out; the 21st was an APPROVED row whose Stage press the
evidence floor refused (outcome=no_evidence writes no event).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from cora import code_queue as cq

HARRISON = "U0B2RM2JYJ1"
ALEX = "U0B3VGWJTMJ"

# verbatim, Slack DM D0B4CTD3B09 (the log cut Q1 at 80 chars; Slack holds the whole text)
Q1 = "Have all cards been responded to(Keep, park, dismiss or Stage/Queue decisions)? Or have I missed any?"
Q2 = "I'm wanting you to confirm if any have not been responded to by me."
Q3 = ("I have pressed them all, but they still show as unresponded to. Does it just take a while? "
      "Should I press multiple times?")

MUST_FORCE = [
    "have my cards registered?",
    "which cards are still unresponded?",
    "did the stage press land?",
    "did my stage press on cq-f880ce946bb6 land?",
    "what is still waiting on me in the code queue?",
    "have all my code-session cards been responded to?",
    "which of the staged prompts are still waiting on a decision?",
    "is cq-2d26f131091e still pending a decision?",
    "Cora, which cards are still awaiting a decision?",
    "check whether my Monday menu presses registered",
    "how many cards are left to decide?",
]

# Real founder-DM texts (bot logs, 6/11-9/21; 80-char log cut where it applies) that
# must NOT force the read -- commands, verbs, other questions, card-adjacent prose.
REAL_FOUNDER_NEGATIVES = [
    "Queue all your recommendations into a staged Cowork session",
    "Yes, queue this as a bug report and code session",
    "Were you supposed to provide a preview card/button for me to click?",
    "I don't see any card anymore. Surface it again",
    "did you complete this?",
    "flag other open items for my review",
    "Stage all of them",
    "Checking your heartbeat",
    "Can you access HubSpot?",
    "Give me each of these decisions to reply to individually for you to record each ",
    "were you able to draft emails for any of these follow up needs?",
    "mark cq-6fef5505fb3e as SHIPPED",
    "queue this threads issues for code session / staged prompt",
    "something is broke. You can't save to drafts. Queue this as a code session item ",
    "Do not file that new item, the id already exists in the queue. Call cora_queue_c",
    "stage a code-session prompt for cq-f52c6b691127",
    "what's on my plate?",
    "which of my tasks are overdue?",
    "What company is the most cash crunched this week?",
    "what are my open Asana tasks?",
    "what were my action items from the July 8 finance weekly?",
    "Where does the capital program stand?",
    "as long as the \"stage\" process was accomplished now to the same result they woul",
    "this decision was closed. Money is being repaid by Jordyn early October",
    "What are the most important things this week for me to focus on?",
    "Approved. Ready to move on",
    "Resolved. Closed, completed. Announcements made. Nothing further needed.",
]

SYNTHETIC_NEGATIVES = [
    "what decisions are still open?",
    "has Tommy responded to the Sprouts email?",
    "did the Kroger PO land?",
    "has the Amex card payment been registered?",
    "did the gift cards land at the Tucson store?",
    "did the press release land in the Business Journal?",
    "which tasks are still open?",
    "is the Deposco sync still stuck?",
    "Did the wire go through?",
    "has the credit card statement been recorded?",
    "which business cards are still waiting on me?",
    "did the press land in the Phoenix paper?",          # Tier-B-only words, standalone
    "did the clicks land on the landing page?",
    "Queue this as a code session: cards don't refresh -- still show as unresponded?",
    "show me the card payment totals?",
]


class TestPredicate:
    def test_live_q1_forces_standalone(self):
        assert cq.is_queue_status_question(Q1) is True

    def test_live_q2_q3_are_follow_ups_that_force_only_after_q1(self):
        assert cq.is_queue_status_question(Q2) is False
        assert cq.is_queue_status_question(Q2, prior_user_texts=[Q1]) is True
        assert cq.is_queue_status_question(Q3) is False
        assert cq.is_queue_status_question(Q3, prior_user_texts=[Q1, Q2]) is True

    @pytest.mark.parametrize("text", MUST_FORCE)
    def test_must_force(self, text):
        assert cq.is_queue_status_question(text) is True

    @pytest.mark.parametrize("text", REAL_FOUNDER_NEGATIVES + SYNTHETIC_NEGATIVES)
    def test_must_not_force(self, text):
        assert cq.is_queue_status_question(text) is False

    def test_follow_up_needs_a_card_question_among_the_last_three_turns(self):
        assert cq.is_queue_status_question("did they go through?", prior_user_texts=[Q1]) is True
        assert cq.is_queue_status_question(
            "did they go through?", prior_user_texts=["what's our cash position?"]) is False
        # only the last THREE user turns count
        assert cq.is_queue_status_question(
            "did they go through?", prior_user_texts=[Q1, "a?", "b?", "c?"]) is False

    def test_slack_entities_are_decoded_first(self):
        assert cq.is_queue_status_question("have my cards registered&#63;") is True

    def test_over_500_chars_never_forces(self):
        assert cq.is_queue_status_question("have my cards registered? " + "x" * 500) is False

    @pytest.mark.parametrize("shape", [
        " " * 480 + "cards?", "cards " * 80 + "?", "responded to " * 36 + "?",
        "?" + "them all " * 55, ("card still show " * 31)[:499] + "?",
    ], ids=["spaces", "cards", "responded", "pronouns", "mixed"])
    def test_predicate_is_fast_on_degenerate_input_at_the_gate(self, shape):
        t0 = time.perf_counter()
        cq.is_queue_status_question(shape, prior_user_texts=[shape, shape, shape])
        assert time.perf_counter() - t0 < 0.1

    @pytest.mark.parametrize("shape", [
        " " * 40_000 + "x", "card " * 8_000, "responded to " * 3_000, "!" * 40_000,
        "still " * 8_000 + "x", "cq-" * 13_000,
    ], ids=["spaces", "card", "responded", "bang", "still", "cq"])
    def test_raw_regexes_are_linear_past_the_gate(self, shape):
        """D-171: the 500-char gate runs first, but each compiled pattern must stand
        on its own at Slack's 40k cap too."""
        t0 = time.perf_counter()
        for rx in (cq._QS_IMPERATIVE_RE, cq._QS_REQUEST_RE, cq._QS_OBJECT_RE,
                   cq._QS_STATUS_RE, cq._QS_PRONOUN_RE, cq._QS_CARD_BEFORE_RE):
            list(rx.finditer(shape))
        assert time.perf_counter() - t0 < 0.2


# ── the renderer (read-only) ─────────────────────────────────────────────────

@pytest.fixture
def qledger(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "code-session-queue.jsonl")
    monkeypatch.setattr(cq, "_MENU_RUNS_LEDGER", tmp_path / "code-queue-menu-runs.jsonl")
    return tmp_path


def _w(path: Path, rows):
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _cap(cid, *, status="PROPOSED", title="Queue fixture item", entity="FNDR", signal="explicit",
         summary="an explicit ask", ts="2026-09-10T12:00:00+00:00", evidence=None):
    return {"event": "captured", "id": cid, "ts": ts, "status": status, "title": title,
            "summary": summary, "entity": entity, "signal": signal, "kind": "bug",
            "severity": "P2", "evidence": evidence or []}


MENU_TS = "2026-09-21T14:00:54.478710+00:00"


def _seed_menu(qdir: Path):
    decided = [f"cq-{i:012x}" for i in range(1, 21)]
    floor_held = "cq-f880ce946bb6"
    lex = "cq-00000000aaaa"
    rows = [_cap(c) for c in decided]
    # the evidence-floor row: APPROVED, passive signal, no permalink -> has_evidence False
    rows.append(_cap(floor_held, status="APPROVED", signal="passive", summary="", title="Folder links"))
    rows.append(_cap(lex, status="APPROVED", entity="LEX-LLC", title="[LEX build ask -- details withheld]"))
    for c in decided:
        rows.append({"event": "staged", "id": c, "ts": "2026-09-21T15:20:00+00:00", "via": "button"})
    # a decision BEFORE the menu ts must not count
    rows.append({"event": "approved", "id": floor_held, "ts": "2026-09-14T10:00:00+00:00"})
    _w(qdir / "code-session-queue.jsonl", rows)
    _w(qdir / "code-queue-menu-runs.jsonl", [{
        "ts": MENU_TS, "sent": True, "message_ts": "1789999254.143349",
        "approved": decided[:6] + [floor_held], "stale_actionable": decided[6:14],
        "proposed_actionable": decided[14:] + [lex], "stale_listed": ["cq-999999999999"],
    }])
    return decided, floor_held, lex


class TestRenderer:
    def test_the_0921_shape_reads_20_decided_and_names_the_floor_held_card(self, qledger):
        decided, floor_held, lex = _seed_menu(qledger)
        out = cq.render_card_status()
        assert "22 card(s) with decision buttons -- 20 decided since it went out, 2 with no decision recorded" in out
        assert floor_held in out and "refused by the evidence floor" in out
        assert f"`stage {floor_held}`" in out
        assert "cq-999999999999" not in out          # a listed-only row carries no buttons
        assert cq.QUEUE_STATUS_FOOTER in out
        assert "STAGED" in out and "via button" in out

    def test_a_lex_row_renders_by_id_with_the_placeholder_only(self, qledger):
        _decided, _f, lex = _seed_menu(qledger)
        out = cq.render_card_status()
        assert lex in out
        line = next(l for l in out.splitlines() if lex in l)
        # the load_items LEX-safe view: the fixed placeholder, never a raw title
        assert "[LEX build ask -- details withheld]" in line
        assert "no decision recorded" in line

    def test_per_id_lookup(self, qledger):
        decided, floor_held, _lex = _seed_menu(qledger)
        out = cq.render_card_status([decided[0], floor_held, "cq-abcabcabcabc"])
        assert f"`{decided[0]}`" in out and "STAGED" in out
        assert "not in the queue ledger" in out

    def test_capture_cards_still_undecided_are_listed(self, qledger):
        _w(qledger / "code-session-queue.jsonl", [
            _cap("cq-111111111111", ts="2026-09-20T10:00:00+00:00"),
            {"event": "dm_sent", "id": "cq-111111111111", "ts": "2026-09-20T10:00:05+00:00"},
            _cap("cq-222222222222", ts="2026-09-20T10:00:00+00:00"),
            {"event": "dm_sent", "id": "cq-222222222222", "ts": "2026-09-20T10:00:05+00:00"},
            {"event": "dismissed", "id": "cq-222222222222", "ts": "2026-09-20T11:00:00+00:00"},
        ])
        from datetime import datetime, timezone
        out = cq.render_card_status(now=datetime(2026, 9, 22, tzinfo=timezone.utc))
        assert "cq-111111111111" in out and "cq-222222222222" not in out
        assert "No Monday menu has been sent yet" in out

    def test_the_footer_says_only_what_the_code_does(self):
        """Lesson 40 (measure, do not reason): a repeat Keep / Later DOES write
        another event, so 'pressing again is a no-op' would be false."""
        assert "repeat Keep or Later records one more" in cq.QUEUE_STATUS_FOOTER
        assert cq.KEEP_CAP >= 1

    def test_the_read_writes_nothing(self, qledger, monkeypatch):
        _seed_menu(qledger)
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in qledger.iterdir()}
        monkeypatch.setattr(cq, "_append_event", lambda *a, **k: (_ for _ in ()).throw(AssertionError("write")))
        monkeypatch.setattr(cq, "_append_jsonl", lambda *a, **k: (_ for _ in ()).throw(AssertionError("write")))
        cq.render_card_status()
        cq.render_card_status(["cq-000000000001"])
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in qledger.iterdir()}
        assert before == after


# ── the tool ─────────────────────────────────────────────────────────────────

class TestTool:
    def _call(self, user, channel, **inp):
        from cora.tools import tool_dispatch as td
        return td._tool_cora_queue_status(user, "FNDR", {"_channel_name": channel, **inp})

    def test_refuses_non_founder_and_non_dm_without_any_queue_content(self, qledger, monkeypatch):
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        _seed_menu(qledger)
        for user, ch in [(ALEX, "dm"), (HARRISON, "fndr-leadership"), (HARRISON, "")]:
            out = self._call(user, ch)
            assert "Nothing was read" in out and "cq-" not in out

    def test_founder_dm_returns_the_render(self, qledger, monkeypatch):
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        _seed_menu(qledger)
        assert "decision buttons" in self._call(HARRISON, "dm")

    def test_ids_are_extracted_from_the_user_message_when_no_arg(self, qledger, monkeypatch):
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        decided, _f, _l = _seed_menu(qledger)
        out = self._call(HARRISON, "dm", _user_message=f"did {decided[3]} land?")
        assert "Code-queue status (from the ledger)" in out and decided[3] in out

    def test_a_read_failure_says_so_and_never_raises(self, qledger, monkeypatch):
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        monkeypatch.setattr(cq, "render_card_status", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        out = self._call(HARRISON, "dm")
        assert "UNAVAILABLE" in out and "NOT evidence that nothing registered" in out

    def test_wiring(self):
        from cora.tools import tool_dispatch as td
        names = [t["name"] for t in td.TOOL_DEFINITIONS]
        assert names.count("cora_queue_status") == 1
        assert td._TOOL_FUNCTIONS["cora_queue_status"] is td._tool_cora_queue_status
        assert td._TOOL_TIMEOUTS["cora_queue_status"] == 8
        assert "cora_queue_status" not in td._GLOBAL_CORE_TOOLS
        fndr = {t["name"] for t in td.tools_for_entity("FNDR")}
        founder_elsewhere = {t["name"] for t in td.tools_for_entity("F3E", cross_entity=True)}
        member_f3e = {t["name"] for t in td.tools_for_entity("F3E")}
        member_lex = {t["name"] for t in td.tools_for_entity("LEX-LLC")}
        assert "cora_queue_status" in fndr and "cora_queue_status" in founder_elsewhere
        assert "cora_queue_status" not in member_f3e and "cora_queue_status" not in member_lex


# ── the app seam ─────────────────────────────────────────────────────────────

def test_no_existing_forced_row_now_reads_as_a_status_question():
    """Displacement guard (D-158): every row the phantom-preview + inventory tables
    pin to an EXISTING force must stay non-status (47 rows at build time, 0 flips)."""
    import test_phantom_preview_force as ppf
    rows = []
    for name in ("_MUST_FORCE", "_MUST_STAY_WITH_EXISTING", "_MUST_FORCE_INVENTORY",
                 "_MUST_STAY_WITH_EXISTING_INVENTORY"):
        for row in getattr(ppf, name):
            rows.append(row[0] if isinstance(row, (list, tuple)) else row)
    assert len(rows) >= 30
    assert [r for r in rows if cq.is_queue_status_question(r)] == []


class TestAppSeam:
    def test_founder_dm_only(self, monkeypatch):
        import cora.app as app
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        assert app._queue_status_turn(HARRISON, "dm", None, Q1, []) is True
        assert app._queue_status_turn(ALEX, "dm", None, Q1, []) is False
        assert app._queue_status_turn(HARRISON, "fndr-leadership", None, Q1, []) is False
        assert app._queue_status_turn(HARRISON, "dm", object(), Q1, []) is False   # grant turn
        assert app._queue_status_turn(None, "dm", None, Q1, []) is False

    def test_prior_user_turns_feed_the_follow_up(self, monkeypatch):
        import cora.app as app
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        prior = [{"role": "user", "content": Q1}, {"role": "assistant", "content": "..."}]
        assert app._queue_status_turn(HARRISON, "dm", None, Q2, prior) is True
        assert app._queue_status_turn(HARRISON, "dm", None, Q2, []) is False
        # assistant text never counts as a prior user turn
        assert app._queue_status_turn(HARRISON, "dm", None, Q2,
                                      [{"role": "assistant", "content": Q1}]) is False

    def test_a_detector_error_never_steals_the_turn(self, monkeypatch):
        import cora.app as app
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        monkeypatch.setattr(cq, "is_queue_status_question",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        assert app._queue_status_turn(HARRISON, "dm", None, Q1, []) is False

    def test_source_order_and_cache_bypass(self):
        import inspect
        import cora.app as app
        body = inspect.getsource(app._dispatch_qa)
        i_calc = body.index("queue_status_turn = _queue_status_turn(")
        i_cache = body.index("and not inventory_turn and not queue_status_turn):")
        i_asana = body.index("force_tool = _asana_destructive_intent(user_message)")
        i_qs = body.index('force_tool = "cora_queue_status"')
        i_inv = body.index('force_tool = "cora_self_inventory"')
        assert i_calc < i_cache < i_asana < i_qs < i_inv

    def test_a_command_about_cards_goes_to_capture_not_the_read(self):
        import cora.app as app
        t = "queue a code session: cards don't refresh after a press -- they still show as unresponded"
        assert app._code_queue_capture_intent(t) is True
        assert cq.is_queue_status_question(t) is False
