"""S3 (cq-439d89a84de4): a phantom-rail hit is adjudicable from disk.

The 9/17 hits (phrase='filed' in a teammate DM, 'updated' in #lex-website,
'created' in #hjr-travel-booking) could not be split into real phantoms and noise:
the WARN named only the phrase and the reply text existed nowhere on disk. Every
firing line of the two seam screens now appends ONE row to
data/state/phantom-write-claims.jsonl (a scrubbed +/-60-char snippet or a WITHHELD
marker, response_chars, tool_use_count, a ref) and the WARN names the ref.

DEVIATION FROM THE KICKOFF (reported): the snippet lives in the LEDGER only, not in
the WARN line -- bot logs are backed up to Drive and sessions quote WARN lines
verbatim into captures the KB ingests, so reply text in the log line is a
propagation path (D-145). The 7d counter stays the LOG SCAN (a ledger that starts
empty at deploy would read a false "clean"); egress_rails now anchors the firing
key to the start of the message (parity verified on the live logs, 1/7/30 d).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from cora import slack_egress as se

HARRISON = "U0B2RM2JYJ1"
CTX_OPEN = {"channel_id": "C0TEST", "entity": "F3E", "snippet_withheld": None, "founder_belt": False}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "phantom-write-claims.jsonl"
    monkeypatch.setattr(se, "PHANTOM_CLAIMS_LEDGER", path)
    monkeypatch.setattr(se, "_RAIL_LEDGER_ARMED", True)
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
    monkeypatch.setattr(se, "_known_cq_ids", lambda: frozenset())
    monkeypatch.setattr(se, "_known_dw_ids", lambda: frozenset())
    se._MODE_WARNED.clear()
    return path


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _warns(caplog, key):
    return [r.getMessage() for r in caplog.records if key in r.getMessage() and " kind=" in r.getMessage()]


def _pw(text, ctx=CTX_OPEN, count=0, channel="f3e-sales"):
    return se.screen_phantom_write_claims(text, tool_use_count=count, channel_name=channel,
                                          user_id="U0B3KH5UZJ7", rail_context=ctx)


class TestLexiconRow:
    def test_one_row_with_the_snippet_and_the_warn_names_only_the_ref(self, ledger, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        text = "Sure thing. I staged the kickoff for you and it is ready to review."
        assert _pw(text) == text                                   # observe: byte-identical
        rows = _rows(ledger)
        assert len(rows) == 1
        row = rows[0]
        assert row["rail"] == se.PHANTOM_LOG_KEY and row["kind"] == "lexicon"
        assert row["phrase"] == "staged" and row["form"] == "first_person"
        assert "I staged the kickoff" in row["snippet"]
        assert row["response_chars"] == len(text) and row["tool_use_count"] == 0
        assert row["mode"] == "observe" and row["channel_id"] == "C0TEST" and row["entity"] == "F3E"
        warn = _warns(caplog, se.PHANTOM_LOG_KEY)[0]
        assert f"ref={row['ref']}" in warn and f"response_chars={len(text)}" in warn and "tool_use=0" in warn
        assert "kickoff" not in warn                             # the reply text never rides the log line

    def test_the_key_still_leads_the_warn_so_the_counter_is_unchanged(self, ledger, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _pw("I filed it.")
        assert _warns(caplog, se.PHANTOM_LOG_KEY)[0].startswith("phantom-write-claim kind=lexicon ")


class TestScrub:
    def test_banking_is_redacted_on_the_FULL_text_before_windowing(self, ledger):
        text = ("Wire Routing Number: 021000021" + " filler" * 6 + " -- and I updated the template.")
        _pw(text)
        snippet = _rows(ledger)[0]["snippet"]
        assert "021000021" not in snippet and "000021" not in snippet
        # proof the ORDER matters: windowing the raw text first strands a partial number
        i = text.index("updated")
        assert "000021" in text[max(0, i - 60):i + 60]

    def test_bare_drive_url_and_long_ids_are_redacted(self, ledger):
        text = ("I created the doc https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit "
                "and task 1216478597972490.")
        _pw(text)
        snippet = _rows(ledger)[0]["snippet"]
        assert "docs.google.com" not in snippet and "1216478597972490" not in snippet

    def test_a_sentinel_token_is_deleted_silently_never_a_second_rail_line(self, ledger, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _pw("WRITE_CONFIRMED: I updated the row.")
        assert "WRITE_CONFIRMED" not in _rows(ledger)[0]["snippet"]
        assert not [r for r in caplog.records if "sentinel-egress-leak" in r.getMessage()]


class TestWithhold:
    @pytest.mark.parametrize("marker", [se.RAIL_SNIPPET_WITHHELD_LEX, se.RAIL_SNIPPET_WITHHELD_GRANT])
    def test_a_withheld_scope_writes_the_marker_only(self, ledger, caplog, marker):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        text = "I updated the Kestrelwood intake packet for the Tuesday review."
        _pw(text, ctx={**CTX_OPEN, "entity": "LEX-LLC", "snippet_withheld": marker})
        row = _rows(ledger)[0]
        assert row["snippet"] == marker
        joined = json.dumps(row) + " ".join(_warns(caplog, se.PHANTOM_LOG_KEY))
        assert "Kestrelwood" not in joined and "intake packet" not in joined

    def test_no_scope_context_withholds(self, ledger):
        se.screen_phantom_write_claims("I filed it.", tool_use_count=0, channel_name="x", user_id="U")
        assert _rows(ledger)[0]["snippet"] == se.RAIL_SNIPPET_NO_CONTEXT

    def test_founder_dm_belt_withholds_a_phi_shaped_snippet_and_keeps_a_clean_one(self, ledger):
        from cora import phi_guard
        phi = "I updated the care plan for the client's seizure medication."
        clean = "I updated the invoice for Kroger."
        assert phi_guard.is_any_phi(phi) and not phi_guard.is_any_phi(clean)   # measured, not assumed
        ctx = {**CTX_OPEN, "entity": "FNDR", "founder_belt": True}
        _pw(phi, ctx=ctx, channel="dm")
        _pw(clean, ctx=ctx, channel="dm")
        rows = _rows(ledger)
        assert rows[0]["snippet"] == se.RAIL_SNIPPET_WITHHELD_PHI
        assert "Kroger" in rows[1]["snippet"]


class TestAppScopePredicate:
    @pytest.fixture
    def app(self):
        import cora.app as app
        return app

    def test_lex_channel_withholds(self, app):
        ctx = app._rail_context("C1", "U0B3KH5UZJ7", "LEX-LLC", None, False, False)
        assert ctx["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_LEX

    def test_grant_turn_withholds(self, app):
        ctx = app._rail_context("D1", HARRISON, "FNDR", object(), True, True)
        assert ctx["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_GRANT

    def test_non_lex_non_custodian_channel_keeps_a_snippet(self, app):
        ctx = app._rail_context("C2", "U0B3KH5UZJ7", "FNDR", None, False, False)
        assert ctx["snippet_withheld"] is None and ctx["founder_belt"] is False

    def test_founder_dm_rides_the_content_belt(self, app, monkeypatch):
        monkeypatch.setattr(app.lex_phi_access, "phi_allowed", lambda *a, **k: True)
        ctx = app._rail_context("D0B4CTD3B09", HARRISON, "FNDR", None, True, True)
        assert ctx["snippet_withheld"] is None and ctx["founder_belt"] is True

    def test_a_non_founder_custodian_context_withholds(self, app, monkeypatch):
        monkeypatch.setattr(app.lex_phi_access, "phi_allowed", lambda *a, **k: True)
        ctx = app._rail_context("D9", "U0B3AEQS0NB", "LEX-LLC", None, True, False)
        assert ctx["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_LEX
        ctx2 = app._rail_context("D9", "U0B3AEQS0NB", "FNDR", None, True, False)
        assert ctx2["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_LEX

    def test_an_error_withholds(self, app, monkeypatch):
        monkeypatch.setattr(app.web_guard, "is_lex_scope", lambda *a: (_ for _ in ()).throw(ValueError()))
        ctx = app._rail_context("C1", "U", "F3E", None, False, False)
        assert ctx["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_LEX

    def test_all_six_screen_calls_carry_the_context_computed_before_the_cache(self, app):
        import inspect
        src = inspect.getsource(app._dispatch_qa)
        assert src.count("rail_context=rail_ctx,") == 6
        assert src.index("rail_ctx = _rail_context(") < src.index("sc.get_cache().lookup(")
        assert src.count("slack_egress.screen_phantom_write_claims(") == 3
        assert src.count("slack_egress.screen_capability_claims(") == 3


class TestOtherKinds:
    def test_fabricated_ids_one_row_per_id(self, ledger, caplog, monkeypatch):
        # _ID_LEDGERS captured the reader functions at import; patch the table itself
        monkeypatch.setattr(se, "_ID_LEDGERS", tuple(
            (label, rx, (lambda: frozenset())) for label, rx, _r in se._ID_LEDGERS))
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _pw("Queued cq-e7f2a4c91b2e and cq-9c4e2d8f5f1a.", count=1)
        rows = _rows(ledger)
        assert sorted(r["phrase"] for r in rows) == ["cq-9c4e2d8f5f1a", "cq-e7f2a4c91b2e"]
        assert all(r["kind"] == "fabricated-id" and r["phrase"] in r["snippet"] for r in rows)
        assert len(_warns(caplog, se.PHANTOM_LOG_KEY)) == len(rows)   # 1:1 with counted lines

    def test_capability_denial_and_toolname_rows(self, ledger, monkeypatch):
        from cora import capability_set as cs
        monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", Path("no-registry.yaml"))
        se.screen_capability_claims(
            "I don't have direct read access to the card ledger. Try cora_self_inventory.",
            tool_use_count=0, channel_name="dm", user_id=HARRISON, entity="FNDR",
            cross_entity=True, founder=True, rail_context={**CTX_OPEN, "entity": "FNDR"})
        kinds = sorted(r["kind"] for r in _rows(ledger))
        assert kinds == ["denial", "toolname"]
        assert all(r["rail"] == se.CAPABILITY_LOG_KEY for r in _rows(ledger))


class TestGates:
    def test_eval_mode_writes_no_row_but_still_warns(self, ledger, caplog, monkeypatch):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        _pw("I filed it.")
        assert _rows(ledger) == [] and len(_warns(caplog, se.PHANTOM_LOG_KEY)) == 1
        assert "ref=-" in _warns(caplog, se.PHANTOM_LOG_KEY)[0]

    def test_an_unarmed_process_writes_no_row(self, ledger, monkeypatch):
        monkeypatch.setattr(se, "_RAIL_LEDGER_ARMED", False)
        _pw("I filed it.")
        assert _rows(ledger) == []

    def test_main_arms_the_ledger(self):
        import inspect
        import cora.main as main
        assert "slack_egress.arm_rail_ledger()" in inspect.getsource(main)

    def test_a_write_failure_never_blocks_the_reply(self, ledger, monkeypatch, tmp_path):
        monkeypatch.setattr(se, "PHANTOM_CLAIMS_LEDGER", tmp_path)        # a directory
        assert _pw("I filed it.") == "I filed it."

    def test_enforce_row_snippet_is_from_the_original_reply(self, ledger, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        out = _pw("I filed it with the others.")
        assert out.startswith(se.PHANTOM_HONEST_TEMPLATE)
        row = _rows(ledger)[0]
        assert row["mode"] == "enforce" and se.PHANTOM_HONEST_TEMPLATE not in row["snippet"]


class TestCounterAnchor:
    def test_a_key_quoted_later_in_a_line_never_counts_for_a_second_rail(self, tmp_path):
        from datetime import datetime
        from cora import egress_rails as er
        now = datetime(2026, 9, 22, 10, 0, 0)
        lf = tmp_path / "cora-2026-09-22.log"
        lf.write_text(
            "2026-09-22T09:59:00 WARNING [T] cora.slack_egress: phantom-write-claim kind=lexicon "
            "phrase='filed' form=initial mode=observe channel=#dm user=U response_chars=40 tool_use=0 "
            "ref=pr-1 -- quoting phantom-capability-claim kind=denial and sentinel-egress-leak mode=x\n",
            encoding="utf-8")
        import os
        os.utime(lf, (now.timestamp(), now.timestamp()))
        counts = er.scan_rail_hits(datetime(2026, 9, 22, 9, 0, 0), log_dir=tmp_path)["counts"]
        assert counts == {er.RAIL_SENTINEL: 0, er.RAIL_PHANTOM: 1, er.RAIL_CAPABILITY: 0}


def _best_of_3(fn) -> float:
    runs = []
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        runs.append(time.perf_counter() - t0)
    return min(runs)


FOUNDER_CTX = {**CTX_OPEN, "entity": "FNDR", "founder_belt": True}


class TestCode14FounderBeltFullText:
    """honesty-rails-3: the founder-DM belt reads the FULL scrubbed reply, not the
    +/-60 window -- a LEX program cue far from the hit must still withhold."""

    @pytest.mark.parametrize("text", [
        ("Re the Lexington DDD member intake (see the thread for the full notes). Quick recap below, "
         "nothing else pending on your side. I updated the notes for Maria Lopez and moved her "
         "follow-up to Thursday."),
        ("The AHCCCS renewal packet went out yesterday, see the thread above for the details and "
         "the owner. Separately -- I created the follow-up task for Daniel Reyes and his mother."),
    ], ids=["lexington-ddd", "ahcccs"])
    def test_a_cue_outside_the_window_withholds(self, ledger, text):
        from cora import phi_guard
        i = text.index(" I ") if " I " in text else text.index("I created")
        window = text[max(0, i - 60):i + 60]
        assert not phi_guard.is_lex_program_context(window)      # the window alone would pass
        assert phi_guard.is_lex_program_context(text)            # the reply does not
        _pw(text, ctx=FOUNDER_CTX, channel="dm")
        row = _rows(ledger)[0]
        assert row["snippet"] == se.RAIL_SNIPPET_WITHHELD_PHI
        assert "Lopez" not in json.dumps(row) and "Reyes" not in json.dumps(row)

    def test_a_clean_founder_reply_keeps_its_snippet(self, ledger):
        _pw("Quick recap: the Kroger reorder went out. I updated the sheet with the new case counts.",
            ctx=FOUNDER_CTX, channel="dm")
        assert "I updated the sheet" in _rows(ledger)[0]["snippet"]


class TestCode14LocateTheFiredClaim:
    """honesty-rails-10 / redos-slack-surfaces-6: the snippet centres on the claim the
    screen FIRED on, found with the same echo rule -- not the first claim of the text."""

    def test_the_snippet_holds_the_fired_claim_not_the_user_echo(self, ledger):
        user = 'Cowork told me "all three locked in and ready for Monday" -- true?'
        text = ('You quoted "all three locked in and ready for Monday" from the Cowork thread, which '
                'I did not write myself, and nothing on my side confirms it yet today. Separately, I '
                'updated the Kroger reorder sheet with the new case counts.')
        se.screen_phantom_write_claims(text, tool_use_count=0, channel_name="dm", user_id=HARRISON,
                                       user_text=user, rail_context=CTX_OPEN)
        row = _rows(ledger)[0]
        assert row["phrase"] == "updated" and row["form"] == "first_person"
        assert "I updated the Kroger" in row["snippet"]

    def test_locate_prefers_the_fired_verb_and_form(self):
        clean = "Deleted. And later: I updated the sheet."
        assert se._locate_write_claim(clean, (), ("updated", "first_person")) == (
            clean.index("I updated"), clean.index("I updated") + len("I updated"))
        assert se._locate_write_claim(clean, (), ("nope", "first_person"))[0] == 0   # falls back to the first


class TestCode14ScrubOncePerCall:
    """redos-slack-surfaces-2 / honesty-rails-12: one scrub per screen call, however
    many rows it writes."""

    def _ids_reply(self, n: int) -> str:
        import random
        rnd = random.Random(7)
        return " ".join("cq-%012x" % rnd.getrandbits(48) for _ in range(n))

    def test_one_scrub_for_many_fabricated_ids(self, ledger, monkeypatch):
        monkeypatch.setattr(se, "_ID_LEDGERS", tuple(
            (label, rx, (lambda: frozenset())) for label, rx, _r in se._ID_LEDGERS))
        calls = []
        real = se._scrub_for_snippet
        monkeypatch.setattr(se, "_scrub_for_snippet", lambda t: calls.append(1) or real(t))
        _pw(self._ids_reply(50), count=1)
        assert len(_rows(ledger)) == 50 and len(calls) == 1

    def test_one_scrub_and_one_full_belt_for_the_founder_dm(self, ledger, monkeypatch):
        monkeypatch.setattr(se, "_ID_LEDGERS", tuple(
            (label, rx, (lambda: frozenset())) for label, rx, _r in se._ID_LEDGERS))
        calls = []
        real = se._founder_belt_passes
        monkeypatch.setattr(se, "_founder_belt_passes", lambda s: calls.append(len(s)) or real(s))
        text = self._ids_reply(20)
        _pw(text, ctx=FOUNDER_CTX, count=1, channel="dm")
        full = [n for n in calls if n > 250]
        assert len(full) == 1                                     # the full-text belt ran ONCE
        assert len(_rows(ledger)) == 20

    def test_500_unknown_ids_are_linear(self, ledger, monkeypatch):
        monkeypatch.setattr(se, "_ID_LEDGERS", tuple(
            (label, rx, (lambda: frozenset())) for label, rx, _r in se._ID_LEDGERS))
        text = self._ids_reply(500)
        path = ledger

        def run():
            path.unlink(missing_ok=True)
            _pw(text, count=1)
        assert _best_of_3(run) < 1.5                              # was ~1.5-2.5 s of pure re-scrubbing

    def test_the_capability_screen_shares_one_scrub_too(self, ledger, monkeypatch):
        from cora import capability_set as cs
        monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", Path("no-registry.yaml"))
        calls = []
        real = se._scrub_for_snippet
        monkeypatch.setattr(se, "_scrub_for_snippet", lambda t: calls.append(1) or real(t))
        se.screen_capability_claims(
            "I don't have direct read access to the card ledger. Try cora_self_inventory or cora_my_notes.",
            tool_use_count=0, channel_name="dm", user_id=HARRISON, entity="FNDR",
            cross_entity=True, founder=True, rail_context={**CTX_OPEN, "entity": "FNDR"})
        assert len(_rows(ledger)) == 3 and len(calls) == 1


class TestCode14EmptyMdLinkLinear:
    """redos-slack-surfaces-3: `[label]()` cleanup admitted '[' in the label class --
    O(n^2) on a '['-heavy reply (8.9 s at 40k), run on EVERY reply and per snippet."""

    @pytest.mark.parametrize("shape", ["[" * 40000, "[a" * 20000, "[" + "a" * 40000, "[a](" + " " * 40000,
                                       "[]( " * 10000, "[x]() " * 6600],
                             ids=["brackets", "bracket_a", "open", "open_paren", "shells", "real_shells"])
    def test_linear_at_40k(self, shape):
        from cora import reply_formatter as rf
        assert _best_of_3(lambda: rf.redact_links_and_ids(shape)) < 0.5
        assert _best_of_3(lambda: se._scrub_for_snippet(shape)) < 0.5
        assert _best_of_3(lambda: list(rf._EMPTY_MD_LINK_RE.finditer(shape))) < 0.2

    def test_the_same_shells_still_clean(self):
        from cora import reply_formatter as rf
        assert rf.redact_links_and_ids("see [the doc]() now") == "see the doc now"
        assert rf.redact_links_and_ids("[a [b]()") == "[a b"
        assert rf.format_reply("Open [the tracker](https://docs.google.com/spreadsheets/d/"
                               "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit).") == "Open the tracker."

    def test_a_bracket_heavy_reply_screens_fast_with_the_ledger_armed(self, ledger):
        assert _best_of_3(lambda: _pw("[" * 16000 + " I staged it.")) < 0.5


class TestCode14CannotCheckLinesAreLedgered:
    """integration-tests-8: the ABSENT / UNAVAILABLE lines are COUNTED phantom lines
    (`phantom-write-claim kind=`), so each now carries a ledger row and its ref."""

    def test_absent_gets_a_row_and_a_ref(self, ledger, caplog, monkeypatch):
        monkeypatch.setattr(se, "_ID_LEDGERS", (("dw", se._ID_LEDGERS[1][1], lambda: None),))
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _pw("Your job dw-0123456789ab is running.", count=1)
        rows = _rows(ledger)
        warns = _warns(caplog, se.PHANTOM_LOG_KEY)
        assert len(rows) == 1 and len(warns) == 1
        assert rows[0]["kind"] == "fabricated-id" and rows[0]["phrase"] == "ledger=dw ABSENT"
        assert "dw-0123456789ab" in rows[0]["snippet"] and f"ref={rows[0]['ref']}" in warns[0]
        assert warns[0].startswith("phantom-write-claim kind=fabricated-id ledger=dw ABSENT")

    def test_unavailable_gets_a_row_and_a_ref(self, ledger, caplog, monkeypatch):
        def _boom():
            raise PermissionError("locked")
        monkeypatch.setattr(se, "_ID_LEDGERS", (("cq", se._ID_LEDGERS[0][1], _boom),))
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _pw("see cq-e7f2a4c91b2e", count=1)
        rows = _rows(ledger)
        warns = _warns(caplog, se.PHANTOM_LOG_KEY)
        assert len(rows) == 1 and rows[0]["phrase"] == "ledger=cq UNAVAILABLE"
        assert len(warns) == 1 and f"ref={rows[0]['ref']}" in warns[0]


class TestCode14OwnerPrivateTurns:
    """honesty-rails-11: a reply built on owner-private unstripped content withholds
    its snippet like the Tier-2 grant turn of the same class."""

    @pytest.fixture
    def app(self):
        import cora.app as app
        return app

    def test_unstripped_personal_withholds(self, app):
        ctx = app._rail_context_for_reply(dict(CTX_OPEN), {"unstripped_personal": True}, {})
        assert ctx["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_PERSONAL

    @pytest.mark.parametrize("tool", ["cora_my_notes", "cora_remember", "cora_forget_note", "gmail_inbox"])
    def test_an_owner_private_tool_withholds(self, app, tool):
        ctx = app._rail_context_for_reply(dict(CTX_OPEN), {}, {"tool_names": ["asana_get_my_tasks", tool]})
        assert ctx["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_PERSONAL

    def test_an_ordinary_turn_keeps_its_scope(self, app):
        base = dict(CTX_OPEN)
        assert app._rail_context_for_reply(base, {}, {"tool_names": ["asana_get_my_tasks"]}) is base

    def test_an_existing_lex_withhold_is_kept(self, app):
        ctx = {**CTX_OPEN, "snippet_withheld": se.RAIL_SNIPPET_WITHHELD_LEX}
        out = app._rail_context_for_reply(ctx, {"unstripped_personal": True}, {})
        assert out["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_LEX

    def test_an_error_withholds(self, app):
        out = app._rail_context_for_reply(dict(CTX_OPEN), {"unstripped_personal": True}, object())
        assert out["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_PERSONAL

    def test_the_row_carries_the_marker_not_the_note(self, ledger):
        import cora.app as app
        ctx = app._rail_context_for_reply(dict(CTX_OPEN), {"unstripped_personal": True}, {})
        _pw("Your note says the Tucson vendor is Apex, and I updated it this morning.", ctx=ctx, count=0)
        rows = _rows(ledger)
        assert rows and all(r["snippet"] == se.RAIL_SNIPPET_WITHHELD_PERSONAL for r in rows)
        assert "Apex" not in json.dumps(rows)

    def test_both_final_reply_sites_refresh_the_scope_before_screening(self, app):
        import inspect
        src = inspect.getsource(app._dispatch_qa)
        assert src.count("rail_ctx = _rail_context_for_reply(rail_ctx, kb_meta, gen_meta)") == 2
        for site in [i for i in range(len(src)) if src.startswith("rail_ctx = _rail_context_for_reply(", i)]:
            nxt = src.index("slack_egress.screen_phantom_write_claims(", site)
            assert src.index("slack_egress.screen_capability_claims(", site) > nxt
            assert nxt - site < 300


def test_snippet_path_is_linear_on_a_200kb_adversarial_reply(ledger):
    text = ("I updated " + "Routing Number: 021000021 " * 2000 + "https://x.com/" + "a" * 40000 + " ") * 2
    t0 = time.perf_counter()
    _pw(text[:200_000])
    assert time.perf_counter() - t0 < 2.0
