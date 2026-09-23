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
    # D-051 forcing-seams-6 (recall): the queue's own outcome words next to an id ...
    "has cq-f880ce946bb6 been staged yet?",
    "is cq-f880ce946bb6 approved?",
    "was cq-f880ce946bb6 shipped?",
    "what's the status of cq-f880ce946bb6?",
    "where does cq-f880ce946bb6 stand?",
    # ... and `show me` / `give me` are read requests, not commands
    "show me which cards are still unresponded?",
    "Show me whether my card presses registered",
    "Cora, show me which cards are still waiting on me",
    "give me the cards still awaiting a decision?",
    # the code-queue objects the narrowed Tier A still names
    "did the monday menu cards register?",
    "is the code backlog still stuck?",
    "have my presses registered?",
    "did my button presses land?",
]

# D-051 forcing-seams-2: Tier A must name the CODE-QUEUE object. Every row forced
# the ledger read before the fix (reproduced) and got the Monday-menu tally back.
OTHER_CARD_AND_BACKLOG_SURFACES = [
    "is the AP backlog still stuck?",
    "what's the backlog of outstanding AR at OSN?",
    "is the invoice backlog still outstanding?",
    "how big is the backlog of outstanding orders at Deposco?",
    "is the ticket backlog still stuck at Lexington?",
    "have my decision cards been responded to?",
    "have all my knowledge review cards been responded to?",
    "did my confirm cards land?",
    "did my blog publish cards register?",
    "did the catch-up cards land?",
    "did the meeting ask cards land?",
    "are the menu cards still showing the old prices?",
    "have the rack cards landed at Sprouts?",
]

# D-051 forcing-seams-3: a queue verb NEXT TO an id is a command (or a compound
# command + question); the forced read would blind S2's zero-tool screen to a
# narrated "Staged cq-..." on exactly this shape.
COMPOUND_VERB_AND_STATUS = [
    "can you stage cq-621dfad586aa? and did cq-9c4e2d8f5f1a land?",
    "Could you stage cq-621dfad586aa too -- have my other cards registered?",
    "please dismiss `cq-621dfad586aa` -- did my other presses land?",
    "did the stage press land? also approve cq-621dfad586aa",
]

# D-051 forcing-seams-1: follow-up turns that must NOT force even with a card
# question (Q1) in the priors -- a generic pronoun / determiner, a confirm turn, or
# a follow-up that names its own non-queue object. Every row forced before the fix.
TIER_B_NEGATIVES_WITH_A_CARD_PRIOR = [
    "any outstanding invoices for F3E?",
    "were all the payroll runs recorded?",
    "any open decisions still waiting on me?",
    "which of them are still pending my approval in Asana?",
    "did it land in the bank?",
    "what sources do you cover? are they all recorded?",
    "yes -- confirm that it is recorded",
    "confirm, and let me know if it landed",
    "yes go ahead, did it land?",
    "did those payments go through?",
    "did those invoices land?",
    "did they go through?",   # 'they' is not press-referring (accepted recall cost)
    # D-051 F2-R1: the status MAIN verb after the pronoun counts only when it closes
    # its clause, so a determiner before a participle adjective / homograph noun
    # stays out; a bare base form after `any` also needs an auxiliary before `any`
    "did we buy any land?",
    "is there any land still available?",
    "are these landed costs final?",
    "any registered users on the new portal?",
    "are those registered trademarks still pending?",
    "were those recorded calls uploaded?",
    "did those two people land?",
    "any stuck orders?",
    "any take on the Sprouts deal?",
    "did any land in the bank?",
]
TIER_B_POSITIVES_WITH_A_CARD_PRIOR = [
    Q2, Q3,
    "did those go through?",
    "which of them are still unresponded?",
    "did the rest go through?",
    "are these still showing as unresponded?",
    "have I missed any?",
    # D-051 F2-R1: do-support follow-ups -- the auxiliary BEFORE the pronoun, the
    # status main verb after it. The round-1 narrowing dropped every one of these.
    "did those land?",
    "have those registered?",
    "did these register?",
    "did any land?",
    "have any registered?",
    "have these landed?",
    "did those register yet?",
    "are those recorded?",
    "did those stick?",
    "did those take effect?",
    "so did those actually register?",
    "and these -- landed?",
    "those landed?",
    "did those two land?",
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

    @pytest.mark.parametrize("text", OTHER_CARD_AND_BACKLOG_SURFACES)
    def test_other_card_surfaces_and_business_backlogs_never_force(self, text):
        assert cq.is_queue_status_question(text) is False
        # and a card question in the priors does not rescue them
        assert cq.is_queue_status_question(text, prior_user_texts=[Q1]) is False

    @pytest.mark.parametrize("text", COMPOUND_VERB_AND_STATUS)
    def test_a_queue_verb_next_to_an_id_never_forces(self, text):
        assert cq.is_queue_status_question(text) is False
        assert cq.is_queue_status_question(text, prior_user_texts=[Q1]) is False

    def test_a_verb_mid_sentence_that_is_not_next_to_an_id_still_forces(self):
        # the adjacency bound: "stage press on cq-X" is a press noun, not a command
        assert cq.is_queue_status_question("did my stage press on cq-f880ce946bb6 land?") is True
        # a past participle next to the id is a status word, not a command
        assert cq.is_queue_status_question("I staged cq-f880ce946bb6 -- did it land?") is True

    @pytest.mark.parametrize("text", TIER_B_NEGATIVES_WITH_A_CARD_PRIOR)
    def test_a_generic_follow_up_never_forces_even_after_a_card_question(self, text):
        assert cq.is_queue_status_question(text, prior_user_texts=[Q1]) is False
        assert cq.is_queue_status_question(
            text, prior_user_texts=["have my cards registered?"]) is False

    @pytest.mark.parametrize("text", TIER_B_POSITIVES_WITH_A_CARD_PRIOR)
    def test_a_press_referring_follow_up_forces_only_after_a_card_question(self, text):
        assert cq.is_queue_status_question(text, prior_user_texts=[Q1]) is True
        assert cq.is_queue_status_question(
            text, prior_user_texts=["what's our cash position?"]) is False

    def test_follow_up_needs_a_card_question_among_the_last_three_turns(self):
        assert cq.is_queue_status_question("did those go through?", prior_user_texts=[Q1]) is True
        assert cq.is_queue_status_question(
            "did those go through?", prior_user_texts=["what's our cash position?"]) is False
        # only the last THREE user turns count
        assert cq.is_queue_status_question(
            "did those go through?", prior_user_texts=[Q1, "a?", "b?", "c?"]) is False

    def test_slack_entities_are_decoded_first(self):
        assert cq.is_queue_status_question("have my cards registered&#63;") is True

    def test_over_500_chars_never_forces(self):
        assert cq.is_queue_status_question("have my cards registered? " + "x" * 500) is False

    @pytest.mark.parametrize("shape", [
        " " * 480 + "cards?", "cards " * 80 + "?", "responded to " * 36 + "?",
        "?" + "them all " * 55, ("card still show " * 31)[:499] + "?",
        ("cq-0123456789ab staged " * 22)[:499] + "?", ("them still pending " * 27)[:499] + "?",
        ("those have landed " * 28)[:499] + "?", "any " * 124 + "?",
        ("did those land? " * 32)[:499], ("did any register " * 30)[:499] + "?",
    ], ids=["spaces", "cards", "responded", "pronouns", "mixed", "id-status", "them-status",
            "those-landed", "any", "did-those-land", "did-any-register"])
    def test_predicate_is_fast_on_degenerate_input_at_the_gate(self, shape):
        t0 = time.perf_counter()
        cq.is_queue_status_question(shape, prior_user_texts=[shape, shape, shape])
        assert time.perf_counter() - t0 < 0.1

    @pytest.mark.parametrize("shape", [
        " " * 40_000 + "x", "card " * 8_000, "responded to " * 3_000, "!" * 40_000,
        "still " * 8_000 + "x", "cq-" * 13_000,
        # D-051 remediation shapes: each edited / new pattern's own tokens
        "any " * 10_000, "those   " * 5_000, "any \t\t" * 6_000 + "x",
        "stage " * 6_000 + "cq-", "stage" + " " * 40_000 + "cq-", "re-stage `" * 4_000,
        "the rest " * 4_000, "monday menu " * 3_000 + "cards", "monday" + "\t" * 40_000,
        "catch-up " * 4_000 + "cards", "knowledge" + " " * 40_000 + "base",
        "show me " * 5_000, "give" + " " * 40_000 + "me", "code backlog " * 3_000,
        "decision " * 4_000 + "cards", "button " * 6_000 + "presses", "staged status " * 3_000,
        # D-051 round-2 shapes (F2-R1 pronoun + main verb)
        "those landed " * 3_000, "did any land " * 3_000, "any" + " " * 40_000 + "land",
        "those actually " * 3_000 + "register", "those take" + " " * 40_000 + "effect",
        "did any " * 5_000 + "x", "these -- " * 4_000,
    ], ids=["spaces", "card", "responded", "bang", "still", "cq",
            "any", "those", "any-tabs", "stage-cq", "stage-spaces-cq", "restage-tick",
            "the-rest", "monday-menu", "monday-tabs", "catch-up", "knowledge-spaces",
            "show-me", "give-spaces", "code-backlog", "decision-cards", "button-presses",
            "id-status",
            "those-landed", "did-any-land", "any-spaces-land", "those-actually",
            "those-take-spaces", "did-any", "these-dash"])
    def test_raw_regexes_are_linear_past_the_gate(self, shape):
        """D-171: the 500-char gate runs first, but each compiled pattern must stand
        on its own at Slack's 40k cap too. Best of 3: measured ~14ms worst shape on
        an idle host (the bound is ~14x that); a single run flaked once under five
        concurrent full suites -- the minimum is what the pattern costs."""
        rxs = (cq._QS_IMPERATIVE_RE, cq._QS_REQUEST_RE, cq._QS_OBJECT_RE,
               cq._QS_STATUS_RE, cq._QS_PRONOUN_RE, cq._QS_CARD_BEFORE_RE,
               cq._QS_ID_STATUS_RE, cq._QS_VERB_ID_RE, cq._QS_FOREIGN_RE, cq._QS_CQ_ID_RE)
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            for rx in rxs:
                list(rx.finditer(shape))
            best = min(best, time.perf_counter() - t0)
        assert best < 0.2


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
# D-051 integration-tests-3: the LEX row's RAW title must be distinctive. The first
# cut seeded it with the redaction placeholder itself, so a renderer that printed
# the raw fold title would have printed the same string and passed.
LEX_RAW_TITLE = "Quibblewick website footer rebuild"


def _seed_menu(qdir: Path):
    decided = [f"cq-{i:012x}" for i in range(1, 21)]
    floor_held = "cq-f880ce946bb6"
    lex = "cq-00000000aaaa"
    rows = [_cap(c) for c in decided]
    # the evidence-floor row: APPROVED, passive signal, no permalink -> has_evidence False
    rows.append(_cap(floor_held, status="APPROVED", signal="passive", summary="", title="Folder links"))
    rows.append(_cap(lex, status="APPROVED", entity="LEX-LLC", title=LEX_RAW_TITLE,
                     summary="Quibblewick summary text"))
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
        # the fold really holds the raw title -- or this test proves nothing
        assert cq._fold_items()[lex]["title"] == LEX_RAW_TITLE
        for out in (cq.render_card_status(), cq.render_card_status([lex])):
            assert lex in out
            line = next(l for l in out.splitlines() if lex in l)
            # the load_items LEX-safe view: the fixed placeholder, never a raw title
            assert cq._LEX_REDACTED_TITLE in line
            assert "Quibblewick" not in out           # D-145: not on ANY line of the render
            assert "no decision recorded" in line

    def test_the_read_states_its_scope(self, qledger):
        """D-051 forcing-seams-2: a turn mis-forced by a question about another card
        surface must not relay the Monday-menu tally as that surface's truth."""
        _seed_menu(qledger)
        for out in (cq.render_card_status(), cq.render_card_status(["cq-000000000001"])):
            assert cq.QUEUE_STATUS_SCOPE in out
            assert "Decision-inbox" in cq.QUEUE_STATUS_SCOPE
            assert "knowledge-review" in cq.QUEUE_STATUS_SCOPE

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

# D-051 forcing-seams-1: the displacement rows the review reproduced. Each is owned
# by an EXISTING force (or by the model, for None); the first cut's Tier B forced
# cora_queue_status on every one of them once a card question sat in the priors.
_DISPLACEMENT_ROWS_WITH_A_CARD_PRIOR = [
    ("what sources do you cover? are they all recorded?", "cora_self_inventory"),
    ("any open decisions still waiting on me?", None),
    ("which of them are still pending my approval in Asana?", None),
    ("yes go ahead, did it land?", None),
]

_TIER_A_PRIORS = [[], [Q1], ["have my cards registered?"], [Q1, Q2, Q3]]


def _full_force_for(message: str, prior: list[str]) -> str | None:
    """_dispatch_qa's force chain, in its precedence order, WITH the queue-status
    branch (after every command / write force, before self-inventory)."""
    import cora.app as capp
    if capp._code_queue_capture_intent(message):
        return "cora_queue_code_session"
    if capp._delegate_work_intent(message):
        return "cora_delegate_work"
    staged = capp._staged_write_force_tool(message)
    if staged:
        return staged
    asana = capp._asana_destructive_intent(message)
    if asana:
        return asana
    if cq.is_queue_status_question(message, prior_user_texts=prior):
        return "cora_queue_status"
    return capp._self_inventory_force(message)


@pytest.mark.parametrize("prior", _TIER_A_PRIORS, ids=["no-prior", "q1", "short-tier-a", "q1-q3"])
def test_no_existing_forced_row_now_reads_as_a_status_question(prior, monkeypatch):
    """Displacement guard (D-158): every row the phantom-preview + inventory tables
    pin to an EXISTING force must stay non-status -- with NO priors (47 rows at
    build time, 0 flips) and, since Tier B reads the priors, WITH a Tier-A card
    question in them (D-051 forcing-seams-1: the first cut only ever ran the tables
    with no priors, so Tier B was never tested against them)."""
    import test_phantom_preview_force as ppf
    monkeypatch.setenv("CORA_LEXICON", "resolve")
    rows = []
    for name in ("_MUST_FORCE", "_MUST_STAY_WITH_EXISTING", "_MUST_FORCE_INVENTORY",
                 "_MUST_STAY_WITH_EXISTING_INVENTORY"):
        for row in getattr(ppf, name):
            rows.append(row[0] if isinstance(row, (list, tuple)) else row)
    assert len(rows) >= 30
    assert [r for r in rows if cq.is_queue_status_question(r, prior_user_texts=prior)] == []
    for row in ppf._MUST_FORCE + ppf._MUST_STAY_WITH_EXISTING + ppf._MUST_FORCE_INVENTORY \
            + ppf._MUST_STAY_WITH_EXISTING_INVENTORY:
        assert _full_force_for(row[0], prior) == row[1], row[0]
    for msg in ppf._MUST_NOT_FORCE:
        assert _full_force_for(msg, prior) is None, msg


@pytest.mark.parametrize("message,expected", _DISPLACEMENT_ROWS_WITH_A_CARD_PRIOR)
def test_the_reproduced_displacements_stay_with_their_owner(message, expected, monkeypatch):
    monkeypatch.setenv("CORA_LEXICON", "resolve")
    for prior in _TIER_A_PRIORS:
        assert _full_force_for(message, prior) == expected, (message, prior)


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

    @pytest.mark.parametrize("text", [Q1, Q2, "have my cards registered?",
                                      "yes go ahead -- have my cards registered?"])
    def test_a_pending_staged_write_is_never_pre_empted(self, text, monkeypatch):
        """D-051 forcing-seams-1: a deferred confirm reaches the model with no forced
        read on iteration 0 -- the read's tool_use would switch off the S2' zero-tool
        screen for the very turn that confirms a write."""
        import cora.app as app
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        prior = [{"role": "user", "content": Q1}, {"role": "assistant", "content": "..."}]
        assert app._queue_status_turn(HARRISON, "dm", None, text, prior) is True
        assert app._queue_status_turn(HARRISON, "dm", None, text, prior, pending=True) is False

    @pytest.mark.parametrize("text", [
        "stage cq-621dfad586aa", "• `stage cq-621dfad586aa`", "stage cq-<id>?",
        "<@U0B44MDGC5R> `stage cq-621dfad586aa` -- did my other cards register?",
        "approve cq-621dfad586aa -- did my other cards register?",
    ])
    def test_a_verb_attempt_is_never_forced(self, text, monkeypatch):
        import cora.app as app
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        prior = [{"role": "user", "content": Q1}]
        assert app._queue_status_turn(HARRISON, "dm", None, text, prior) is False

    def test_the_verb_attempt_gate_is_its_own_belt(self, monkeypatch):
        """The seam refuses a verb attempt even if the predicate would not (the
        predicate's own verb-next-to-id screen is a second, independent rail)."""
        import cora.app as app
        monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
        monkeypatch.setattr(cq, "is_queue_status_question", lambda *a, **k: True)
        assert app._queue_status_turn(HARRISON, "dm", None, "stage cq-<the id>", []) is False
        assert app._queue_status_turn(HARRISON, "dm", None, "have my cards registered?", []) is True

    def test_the_call_site_passes_the_pending_note(self):
        import inspect
        import cora.app as app
        body = inspect.getsource(app._dispatch_qa)
        i_note = body.index("pending_note = _tool_dispatch.describe_live_pendings(")
        i_calc = body.index("queue_status_turn = _queue_status_turn(")
        assert i_note < i_calc
        assert "pending=bool(pending_note))" in body[i_calc:i_calc + 300]

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


# ── the seam, driven through _dispatch_qa (D-051 forcing-seams-8) ────────────
# test_source_order_and_cache_bypass pins substring ORDER only; an edit that kept
# the literals but overwrote force_tool (or re-read the cache) would stay green.
# This drives the real pipeline with the model, the context load and the cache
# stubbed (no network, no ledger writes) and observes what reached the model.

_PENDING_NOTE = ("STAGED WRITES AWAITING CONFIRMATION: this person currently has a "
                 "personal note staged and unconfirmed (newest first: a personal note).")


def _drive_dispatch_qa(monkeypatch, text, *, prior=None, pending_note=""):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    import cora.app as app_mod

    monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
    seen: dict = {}

    def fake_generate(*_a, meta=None, **kw):
        seen.update(kw)
        if meta is not None:
            meta["used_tools"] = True
            meta["used_verbatim_tool"] = False
        return "Relayed from the ledger."

    posts = {"n": 0}

    def fake_say(**_kw):
        posts["n"] += 1
        if posts["n"] == 1:
            raise RuntimeError("no placeholder")   # -> the non-streaming path
        return {"ok": True}

    cache = MagicMock()
    cache.lookup.return_value = None
    hints = SimpleNamespace(bypass_cache=False, skip_kb=True, kb_k_override=None, cache_ttl=300)
    with patch.object(app_mod, "generate_response", side_effect=fake_generate), \
         patch.object(app_mod.ic, "classify", return_value="qa"), \
         patch.object(app_mod.ic, "routing_hints", return_value=hints), \
         patch.object(app_mod.sc, "get_cache", return_value=cache), \
         patch.object(app_mod.kb_embeddings, "embed_query", return_value=[0.0] * 8), \
         patch.object(app_mod, "load_context_parts", return_value=("static", "kb")), \
         patch.object(app_mod, "load_prompt", return_value="sys"), \
         patch.object(app_mod.model_router, "choose_model", return_value="model-x"), \
         patch.object(app_mod.model_router, "short_label", return_value="x"), \
         patch.object(app_mod.user_identity, "display_name", return_value="Harrison"), \
         patch.object(app_mod.user_identity, "get_user", return_value=None), \
         patch.object(app_mod.lex_phi_access, "phi_allowed", return_value=False), \
         patch.object(app_mod.knowledge_check, "recall_ask_note", return_value=""), \
         patch.object(app_mod._tool_dispatch, "describe_live_pendings", return_value=pending_note), \
         patch.object(app_mod.active_thread_store, "register"):
        app_mod._dispatch_qa(
            channel_id="D0TESTDM", channel_name="dm", user_id=HARRISON,
            user_message=text, reply_thread_ts="1789999254.000100", entity="FNDR",
            client=MagicMock(), say=fake_say, prior_messages=list(prior or []),
        )
    return seen, cache


class TestDispatchQaBehaviour:
    def test_a_card_question_forces_the_read_and_never_touches_the_cache(self, monkeypatch):
        seen, cache = _drive_dispatch_qa(monkeypatch, Q1)
        assert seen["force_tool"] == "cora_queue_status"
        cache.lookup.assert_not_called()
        cache.store.assert_not_called()

    def test_a_follow_up_forces_the_read_through_the_real_dm_priors(self, monkeypatch):
        prior = [{"role": "user", "content": Q1}, {"role": "assistant", "content": "Checking."}]
        seen, cache = _drive_dispatch_qa(monkeypatch, Q2, prior=prior)
        assert seen["force_tool"] == "cora_queue_status"
        cache.lookup.assert_not_called()

    def test_the_harness_can_see_a_cache_read_and_store(self, monkeypatch):
        """Control: an ordinary DM question reads AND stores, so the two
        assert_not_called above are observations, not blind spots."""
        seen, cache = _drive_dispatch_qa(monkeypatch, "what's our cash position this week?")
        assert seen["force_tool"] is None
        cache.lookup.assert_called_once()
        cache.store.assert_called_once()

    def test_a_pending_staged_write_is_not_pre_empted_end_to_end(self, monkeypatch):
        prior = [{"role": "user", "content": Q1}, {"role": "assistant", "content": "Checking."}]
        seen, _cache = _drive_dispatch_qa(monkeypatch, "yes go ahead -- have my cards registered?",
                                          prior=prior, pending_note=_PENDING_NOTE)
        assert seen["force_tool"] != "cora_queue_status"

    def test_an_earlier_force_still_wins_on_the_same_text(self, monkeypatch):
        t = "queue a code session: cards don't refresh after a press -- they still show as unresponded"
        seen, _cache = _drive_dispatch_qa(monkeypatch, t, prior=[{"role": "user", "content": Q1}])
        assert seen["force_tool"] == "cora_queue_code_session"
