"""Code #14 D-051 ROUND 2 (F1 group): regressions the round-1 remediation introduced in
the two honesty rails, found by the focused re-review and confirmed by a refuter.

  F1-R1 / forcing-seams-5   a user-typed unknown id is exempt only as a PURE ECHO
                            (round 1 exempted every typed id, so a fabricated STATE
                            claim about it -- "Yes -- cq-X is staged." -- wrote no
                            counted line at any tool count); prior turns count too.

Every sentence here is a probe reproduced against the round-1 tip (bca189c) before the
fix; the "base" column of the re-review is the pre-remediation branch (b04d3f6).
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from cora import capability_set as cs
from cora import slack_egress as se

HARRISON = "U0B2RM2JYJ1"
KNOWN_CQ = frozenset({"cq-a24f9d2210fc", "cq-0123456789ab"})


@pytest.fixture(autouse=True)
def observe(monkeypatch, tmp_path):
    monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", tmp_path / "no-registry.yaml")
    monkeypatch.setattr(se, "_ID_LEDGERS", (
        ("cq", re.compile(r"\bcq-[0-9a-f]{12}\b", re.IGNORECASE), lambda: KNOWN_CQ),
        ("dw", re.compile(r"\bdw-[0-9a-f]{12}\b", re.IGNORECASE), lambda: frozenset()),
    ))
    se._MODE_WARNED.clear()


def _msgs(caplog, key, kind):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith(f"{key} kind={kind}")]


def _write(text, *, user_text="", prior=(), count=0):
    return se.screen_phantom_write_claims(text, tool_use_count=count, channel_name="dm",
                                          user_id=HARRISON, user_text=user_text,
                                          prior_user_texts=list(prior))


def _best_of_3(fn) -> float:
    runs = []
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        runs.append(time.perf_counter() - t0)
    return min(runs)


# ── F1-R1 / forcing-seams-5: the typed-id exemption is a PURE ECHO only ────────
ASK = "did cq-000000000002 land?"
RELAY = "- `cq-000000000002` -- not in the queue ledger"


class TestTypedIdPureEcho:
    @pytest.mark.parametrize("count", [0, 1])
    @pytest.mark.parametrize("reply", [
        "Yes -- cq-000000000002 is staged.",
        "Yes -- cq-000000000002 is staged and on Monday's menu.",
        "cq-000000000002 has been staged.",
        "`cq-000000000002` is staged.",
        "Yes, cq-000000000002's been queued.",
        "Staged cq-000000000002 for Monday.",
    ])
    def test_a_state_claim_about_a_typed_id_is_counted(self, caplog, reply, count):
        """The refuter's residual: a free-form ask about a typo'd id answered with a
        completion claim. Round 1 logged it at INFO only; the base counted it."""
        caplog.set_level(logging.INFO, logger=se.__name__)
        _write(reply, user_text=ASK, count=count)
        fab = _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")
        assert len(fab) == 1 and "cq-000000000002" in fab[0], (reply, fab)

    def test_both_pasted_fabricated_ids_count_when_the_reply_claims_them(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write("Staged both: cq-e7f2a4c91b2e and cq-9c4e2d8f5f1a -- they land Monday.",
               user_text="stage cq-e7f2a4c91b2e and cq-9c4e2d8f5f1a please", count=1)
        fab = _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")
        assert len(fab) == 2

    @pytest.mark.parametrize("reply", [
        RELAY,
        "`cq-000000000002` isn't in the queue ledger -- did you mean another id?",
        "I couldn't find cq-000000000002 in the ledger.",
        "cq-000000000002 has not been staged.",
        "No record of cq-000000000002 -- it was never captured.",
    ])
    def test_an_honest_relay_of_a_typed_id_is_still_not_counted(self, caplog, reply):
        caplog.set_level(logging.INFO, logger=se.__name__)
        assert _write(reply, user_text=ASK, count=1) == reply
        assert _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id") == []
        assert any("fabricated-id echo" in r.getMessage() and r.levelno == logging.INFO
                   for r in caplog.records)

    def test_a_forced_follow_up_naming_no_id_relays_a_prior_turn_id(self, caplog):
        """forcing-seams-5 residual: the queue-status force covers follow-ups within
        three turns, and the id came from the PRIOR user turn."""
        caplog.set_level(logging.INFO, logger=se.__name__)
        _write(RELAY, user_text="and is it there now?", prior=[ASK], count=1)
        assert _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id") == []
        _write("Yes -- cq-000000000002 is staged now.", user_text="and is it there now?",
               prior=[ASK], count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    def test_an_id_older_than_six_user_turns_is_not_typed(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(RELAY, user_text="and now?", prior=[ASK, "a", "b", "c", "d", "e", "f"], count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    def test_enforce_redacts_a_claimed_typed_id_and_keeps_an_echoed_one(self, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        assert _write("Yes -- cq-000000000002 is staged.", user_text=ASK, count=1) == (
            "Yes -- [unknown id] is staged.")
        assert _write(RELAY, user_text=ASK, count=1) == RELAY

    def test_every_occurrence_must_be_an_echo(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(RELAY + "\nAlso: cq-000000000002 is queued for Monday.", user_text=ASK, count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    def test_a_failing_echo_rule_counts_the_id(self, caplog, monkeypatch):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        monkeypatch.setattr(se, "_id_sentence", lambda *a: (_ for _ in ()).throw(ValueError()))
        _write(RELAY, user_text=ASK, count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    @pytest.mark.parametrize("shape", [
        "cq-000000000002 " + "is " * 13000 + "staged",
        "cq-000000000002 is" + " " * 40000 + "staged",
        "cq-000000000002 has" + " been" * 8000 + " staged",
        ("cq-000000000002 " * 2600)[:40000],
        "cq-000000000002" + "." * 40000,
        "- `cq-000000000002` -- not in the queue ledger\n" * 800,
    ], ids=["is_run", "space_run", "been_run", "id_run", "dots", "relay_lines"])
    def test_the_echo_rule_is_linear_at_40k(self, shape):
        """D-171: the state regex, the sentence cut and the per-occurrence cap."""
        assert _best_of_3(lambda: list(se._ID_STATE_CLAIM_RE.finditer(shape))) < 0.2
        assert _best_of_3(lambda: se._id_is_pure_echo(shape, "cq-000000000002")) < 0.5
        assert _best_of_3(lambda: _write(shape, user_text=ASK, count=1)) < 1.0


# ── F1-R2 + honesty-rails-8: 'initial' precision (by-agent / habitual / labels) ──
MUST_NOT_FIRE_R2 = [
    # F1-R2: a by-agent or habitual word inside the short for/with/from object
    "Queued for review by Justin.", "Filed with the IRS by Justin.", "Queued for review by the bookkeeper.",
    "Queued for Monday by Harrison.", "Updated from the bank feed nightly.", "Updated with actuals every Monday.",
    "Filed with the IRS every April.", "Queued for review weekly.", "Yes, filed with the IRS by Justin.",
    "Per the KB: the sales tax return. Filed with the state by Justin.",
    "Looking at the sheet notes. Updated from the bank feed nightly.", "Updated for each store.",
    # honesty-rails-8 remainder: a participle + ':' / '(' metadata LABEL
    "Updated: 9/12 (per the doc footer)", "Created: March 2025 (per the doc properties)",
    "*Filed:* 4/15/2025", "Doc properties\n- Created: 2024-03-01\n- Updated: 2026-09-12",
    "Filed (per the county record): 4/15/2025", "Updated (last): 9/12",
    "Updated (2026-08-30): the lease addendum", "Created:\n- 2024-03-01", "**Updated:** Sept 1st",
    "Updated: (per the doc footer) 9/12", "* Created: 2024-03-01",
]
MUST_FIRE_R2 = [
    # a dated FILENAME is not a date label (the 9/15 prompt path shape)
    ("Staged: 2026-09-16_fndr_cora-code-prompt.md", "initial"),
    ("Staged: cq-a24f9d2210fc", "initial"), ("Updated: the deal is now Closed Won.", "initial"),
    ("Staged (draft): G:\\x.md", "initial"), ("Staged (per your ask): the kickoff prompt", "initial"),
    ("Created: Tue 2pm with Justin", "initial"), ("Updated:\nThe deal is Closed Won.", "initial"),
    ("- Created: Pay the invoice", "initial"),
    # the round-1 for/with/from recall stays
    ("Queued for your review.", "initial"), ("Staged for Monday's menu.", "initial"),
    ("Updated with the new totals.", "initial"), ("Queued for the next code session.", "initial"),
    ("Filed to the Receipts & Invoices inbox.", "initial"),
    # receipts are untouched by the label rule
    ("- Task created: Pay the invoice", "receipt"), ("Kickoff prompt staged (draft): G:\\x.md", "receipt"),
]


class TestInitialPrecisionR2:
    @pytest.mark.parametrize("text", MUST_NOT_FIRE_R2)
    def test_third_party_habitual_and_label_shapes_never_fire(self, text):
        assert se._find_write_claim(text) is None, (text, se._find_write_claim(text))

    @pytest.mark.parametrize("text,form", MUST_FIRE_R2)
    def test_claims_next_to_those_shapes_still_fire(self, text, form):
        hit = se._find_write_claim(text)
        assert hit is not None and hit[1] == form, (text, hit)

    def test_a_label_reply_writes_no_counted_line(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write("Per the doc properties:\n- Created: 2024-03-01\n- Updated: 2026-09-12\n"
               "Filed (per the county record): 4/15/2025")
        assert _msgs(caplog, se.PHANTOM_LOG_KEY, "lexicon") == []

    @pytest.mark.parametrize("shape", [
        "Queued for" + " a" * 20000 + " by x.", "Updated from " + "the " * 10000 + "nightly.",
        "Updated:" + " " * 40000 + "9/12", "Updated:" + "*" * 40000, "Updated:\n" + " " * 40000 + "- 9/12",
        "Filed (per" + " x" * 20000 + "): 4/15", "Filed (" + "per " * 10000 + ")", "Updated: " + "9/" * 20000,
        "Updated: " + "March " * 6000 + "1", "Updated (" + "2026-" * 8000, ("Updated: 9/12\n" * 3000),
        "- Created: 2024-03-01\n" * 1800, "Staged: " + "2026-09-16_" * 3600,
    ], ids=["for_by", "from_nightly", "colon_sp", "colon_stars", "colon_nl", "paren_per", "paren_per_rep",
            "slashes", "months", "paren_dates", "label_lines", "bullets", "filenames"])
    def test_the_new_label_and_habitual_patterns_are_linear_at_40k(self, shape):
        """D-171: _WC_OBJ_NOT_HAB / _WC_LABEL_COLON / _WC_LABEL_PAREN / _WC_DATE at 40k."""
        rx = dict(se._WRITE_CLAIM_FORMS)["initial"]
        assert _best_of_3(lambda: list(rx.finditer(shape))) < 0.2
        assert _best_of_3(lambda: list(se._WC_RECEIPT_RE.finditer(shape))) < 0.2
        assert _best_of_3(lambda: se._find_write_claim(shape)) < 0.5


# ── F1-R3: a title-case receipt is skipped only for a TEAMMATE subject ─────────
TITLE_CASE_RECEIPTS = [
    "**Calendar Invite Created:** Tue 2pm with Justin", "**Meeting Invite Created:** Thursday 10am",
    "Slack DM queued (draft): to Tessa", "Slack DM staged (draft): to Tessa", "Draft DM queued: to Justin",
    "Inventory Adjustment staged: Pure Original +12 cases", "Kroger PO updated: 40 cases",
    "F3E SKU updated: Pure 12pk", "Payment Link created: https://pay.example/x",
    "**Shopify Discount Created:** 10% off", "Price Change queued (draft): Pure 12pk", "QBO Bill created: Cox",
    "HubSpot Deal updated: Kroger", "Kickoff Prompt staged (draft):",
]
TEAMMATE_SUBJECTS = [
    "Tasks Justin created:", "Deals Tommy updated (last 7 days):", "Invoices Jerry filed:",
    "Hannah Grant updated:", "Justin created: the deck", "Harrison staged: three prompts.", "Justin created.",
]


class TestReceiptSubjectR2:
    @pytest.fixture(autouse=True)
    def names(self, monkeypatch):
        monkeypatch.setattr(se, "_person_name_tokens",
                            lambda: frozenset({"justin", "tommy", "jerry", "hannah", "grant", "tessa"}),
                            raising=False)

    @pytest.mark.parametrize("text", TITLE_CASE_RECEIPTS)
    def test_title_case_receipts_fire(self, text):
        assert se._find_write_claim(text) is not None and se._find_write_claim(text)[1] == "receipt", text

    @pytest.mark.parametrize("text", TEAMMATE_SUBJECTS)
    def test_teammate_subjects_never_fire(self, text):
        assert se._find_write_claim(text) is None, (text, se._find_write_claim(text))

    def test_a_zero_tool_mimicked_receipt_writes_a_counted_line(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write("**Calendar Invite Created:** Tue 2pm with Justin\nLink: (sent to both of you)")
        hits = _msgs(caplog, se.PHANTOM_LOG_KEY, "lexicon")
        assert len(hits) == 1 and "form=receipt" in hits[0]

    @pytest.mark.parametrize("shape", [
        "Deals Tommy updated:\n" * 2000, "Invoices Jerry filed (x):\n" * 1600,
        ("Aaaa Bbbb Cccc Dddd staged:\n" * 1400), "Tasks " * 8000 + "Justin created:",
    ], ids=["plural_name", "plural_paren", "title4", "plural_run"])
    def test_the_subject_rule_is_linear_at_40k(self, shape):
        assert _best_of_3(lambda: list(se._WC_RECEIPT_RE.finditer(shape))) < 0.2
        assert _best_of_3(lambda: se._find_write_claim(shape)) < 0.5


class TestPersonNameTokens:
    def test_the_real_registry_supplies_first_and_last_names(self, monkeypatch):
        monkeypatch.setitem(se._PERSON_NAME_CACHE, "at", None)
        names = se._person_name_tokens()
        assert {"justin", "hannah", "tommy"} <= names
        assert not (names & se._WC_RECEIPT_NOUNS)

    def test_an_unreadable_registry_falls_back_to_shape_alone(self, monkeypatch):
        from cora import org_roles
        monkeypatch.setitem(se._PERSON_NAME_CACHE, "at", None)
        monkeypatch.setattr(org_roles, "all_roles", lambda: (_ for _ in ()).throw(OSError("locked")))
        assert se._person_name_tokens() == frozenset()
        assert se._find_write_claim("Tasks Justin created:") is None          # the plural shape still skips
        assert se._find_write_claim("Inventory Adjustment staged: +12") is not None
        monkeypatch.setitem(se._PERSON_NAME_CACHE, "at", None)


# ── F1-R5: a deflection reads its own object, then the sentence BEFORE it ──────
def _cap(text, *, channel="dm", entity="FNDR", founder=True, user=HARRISON):
    return se.screen_capability_claims(text, tool_use_count=0, channel_name=channel, user_id=user,
                                       entity=entity, cross_entity=founder, founder=founder)


FRONT_NAMED_DEFLECTIONS = [
    ("On the HubSpot side, that needs a live look at the deal record.", "hubspot"),
    ("For Asana, that would need a direct check of the task history.", "asana"),
    ("Whether the invoice posted in QuickBooks -- that needs a direct look at the register.", "quickbooks"),
    ("For the code queue, that needs a direct check of the backlog, not another tap.", "code queue"),
]


class TestDeflectionFrontNamedR2:
    @pytest.mark.parametrize("channel,entity,founder,user", [
        ("dm", "FNDR", True, HARRISON), ("f3e-sales", "F3E", False, "U0B3VGWJTMJ")], ids=["founder_dm", "f3e"])
    @pytest.mark.parametrize("text,term", FRONT_NAMED_DEFLECTIONS)
    def test_a_family_named_before_the_deflection_trips(self, caplog, text, term, channel, entity,
                                                         founder, user):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        assert _cap(text, channel=channel, entity=entity, founder=founder, user=user) == text
        hits = _msgs(caplog, se.CAPABILITY_LOG_KEY, "denial")
        assert len(hits) == 1 and f"term='{term}'" in hits[0], hits

    @pytest.mark.parametrize("text", [
        "That needs a direct check of the ledger at the bank, not QBO.",
        "That needs a direct check of the bank statement.",
        "That would need a look at the contract.",
        "I pulled QuickBooks already. That needs a direct check of the bank statement.",
    ])
    def test_the_tail_and_earlier_sentences_are_never_read(self, caplog, text):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _cap(text)
        assert _msgs(caplog, se.CAPABILITY_LOG_KEY, "denial") == []

    @pytest.mark.parametrize("shape", [
        "For " + "HubSpot " * 5000 + "that needs a direct check of x.",
        ("x" * 150 + " that needs a direct check of the backlog. ") * 200,
        "that needs a direct check of " * 1400,
    ], ids=["long_head", "many_deflections", "deflect_rep"])
    def test_the_head_search_is_linear_at_40k(self, shape):
        assert _best_of_3(lambda: _cap(shape)) < 0.5


# ── honesty-rails-10 / redos-slack-surfaces-6: the row records the first NON-echo hit ─
CTX_OPEN = {"channel_id": "C0TEST", "entity": "F3E", "snippet_withheld": None, "founder_belt": False}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "phantom-write-claims.jsonl"
    monkeypatch.setattr(se, "PHANTOM_CLAIMS_LEDGER", path)
    monkeypatch.setattr(se, "_RAIL_LEDGER_ARMED", True)
    return path


def _rows(path):
    import json
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if path.exists() else []


def _pw(text, user, prior=()):
    return se.screen_phantom_write_claims(text, tool_use_count=0, channel_name="dm", user_id=HARRISON,
                                          user_text=user, prior_user_texts=list(prior),
                                          rail_context=CTX_OPEN)


# the two findings' OWN repros (the user text is unquoted in the hr-10 one)
HR10_USER = "Quick check: the vendor portal says Task created: Pay the invoice -- is that ours?"
HR10_REPLY = ("Task created: Pay the invoice -- that line is the vendor portal notification you pasted, "
              "not mine, and nothing on my side confirms it yet today. Separately, I updated the Kroger "
              "reorder sheet with the new case counts.")
RS6_USER = "I filed the Cox invoice with the bookkeeper on Tuesday afternoon"
RS6_REPLY = ('You said "I filed the Cox invoice with the bookkeeper on Tuesday afternoon", so that one is '
             "yours and already handled on your side as far as I can tell. The rest: all three queued: the "
             "Mesa reorder, the Kroger PO and the Tucson invoice.")


class TestRowRecordsTheRealPhantomR2:
    def test_hr10_the_unquoted_echo_does_not_hide_the_later_phantom(self, ledger, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        assert _pw(HR10_REPLY, HR10_USER) == HR10_REPLY
        rows = _rows(ledger)
        assert len(rows) == 1                                           # still ONE row per reply
        assert rows[0]["phrase"] == "updated" and rows[0]["form"] == "first_person"
        assert "I updated the Kroger reorder sheet" in rows[0]["snippet"]
        hits = _msgs(caplog, se.PHANTOM_LOG_KEY, "lexicon")
        assert len(hits) == 1 and "phrase='updated'" in hits[0] and "form=first_person" in hits[0]

    def test_rs6_a_quoted_first_person_echo_does_not_hide_the_later_phantom(self, ledger):
        _pw(RS6_REPLY, RS6_USER)
        row = _rows(ledger)[0]
        assert row["phrase"] == "queued" and row["form"] == "quantifier"
        assert "all three queued" in row["snippet"]

    def test_the_same_verb_and_form_twice_centres_on_the_non_echo_one(self, ledger):
        user = "title it Task created: Pay the invoice"
        reply = ("Task created: Pay the invoice -- that is the portal line you pasted, and it came from the "
                 "vendor, not from me at any point today.\nTask created: Reorder Kroger cases")
        _pw(reply, user)
        row = _rows(ledger)[0]
        assert row["form"] == "receipt" and "Reorder Kroger cases" in row["snippet"]

    def test_an_all_echo_reply_still_fires_and_records_the_first_hit(self, ledger, caplog):
        """The FIRE decision is unchanged: unquoted echoes count by design."""
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _pw("Task created: Pay the invoice", "Task created: Pay the invoice")
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "lexicon")) == 1
        assert _rows(ledger)[0]["snippet"].startswith("Task created: Pay the invoice")

    def test_a_reply_with_no_user_text_records_its_first_hit(self, ledger):
        _pw("Deleted. And later: I updated the sheet.", "")
        assert _rows(ledger)[0]["phrase"] == "deleted"

    def test_a_preference_failure_records_the_first_hit(self, ledger, monkeypatch):
        monkeypatch.setattr(se, "_preferred_write_claim_span",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError()), raising=False)
        _pw(HR10_REPLY, HR10_USER)
        assert _rows(ledger)[0]["phrase"] == "created"

    @pytest.mark.parametrize("shape", [
        ("Task created: Pay the invoice\n" * 1300), ("I staged it. " * 3000),
        'You said "I filed the Cox invoice with the bookkeeper" -- ' * 700,
    ], ids=["echo_receipts", "first_person_run", "quoted_echo_run"])
    def test_the_preference_is_bounded_at_40k(self, ledger, shape):
        user = "Task created: Pay the invoice. I staged it. I filed the Cox invoice with the bookkeeper"
        assert _best_of_3(lambda: se._preferred_write_claim_span(shape, [user])) < 0.5
        assert _best_of_3(lambda: _pw(shape, user)) < 1.0


# ── forcing-seams-5 (orphan half): the card-status read agrees with known_ids() ─
class TestCardStatusOrphanR2:
    @pytest.fixture
    def qledger(self, tmp_path, monkeypatch):
        from cora import code_queue as cq
        monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "code-session-queue.jsonl")
        monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "fp.jsonl")
        monkeypatch.setattr(cq, "_SIGNALS_LEDGER", tmp_path / "sig.jsonl")
        cq._KNOWN_IDS_CACHE.update({"key": None, "ids": frozenset()})
        cq._append_event({"event": "captured", "id": "cq-0123456789ab", "ts": cq._now_iso(),
                          "status": "APPROVED", "title": "mirror", "kind": "capability_ask",
                          "severity": "HIGH", "entity": "FNDR", "signal": "explicit"})
        cq._append_event({"event": "recurrence", "id": "cq-8f7d6da0112a", "ts": cq._now_iso()})
        return cq

    def test_an_orphan_id_is_not_reported_absent(self, qledger):
        cq = qledger
        assert "cq-8f7d6da0112a" in cq.known_ids()                  # the rail calls it KNOWN
        out = cq.render_card_status(["cq-8f7d6da0112a", "cq-000000000002"])
        orphan = next(l for l in out.splitlines() if "cq-8f7d6da0112a" in l)
        absent = next(l for l in out.splitlines() if "cq-000000000002" in l)
        assert "not in the queue ledger" not in orphan and "events only" in orphan
        assert absent.endswith("not in the queue ledger")

    def test_relaying_the_orphan_line_trips_no_rail(self, qledger, caplog, monkeypatch):
        cq = qledger
        monkeypatch.setattr(se, "_ID_LEDGERS", (
            ("cq", re.compile(r"\bcq-[0-9a-f]{12}\b", re.IGNORECASE), cq.known_ids),))
        caplog.set_level(logging.WARNING, logger=se.__name__)
        line = next(l for l in cq.render_card_status(["cq-8f7d6da0112a"]).splitlines()
                    if "cq-8f7d6da0112a" in l)
        _write(line, user_text="did cq-8f7d6da0112a land?", count=0)
        assert not [r for r in caplog.records if r.getMessage().startswith(se.PHANTOM_LOG_KEY + " kind=")]


# ── honesty-rails-11: every owner-private tool withholds the snippet ──────────
class TestOwnerPrivateToolsR2:
    @pytest.fixture
    def app(self):
        import cora.app as app
        return app

    @pytest.mark.parametrize("tool", [
        "cora_person_dossier", "gmail_create_draft", "personal_oneamerica_portfolio",
        "personal_capital_program_state", "personal_travel_points",
    ])
    def test_the_re_review_gaps_now_withhold(self, app, tool):
        ctx = app._rail_context_for_reply(dict(CTX_OPEN), {}, {"tool_names": ["asana_get_my_tasks", tool]})
        assert ctx["snippet_withheld"] == se.RAIL_SNIPPET_WITHHELD_PERSONAL

    def test_every_verbatim_personal_tool_is_owner_private(self, app):
        from cora.tools import tool_dispatch as td
        personal = {n for n in td.VERBATIM_TABLE_TOOLS if n.startswith("personal_")}
        assert personal == {"personal_oneamerica_portfolio", "personal_capital_program_state",
                            "personal_travel_points"}                       # the registry marker, pinned
        assert personal <= app._OWNER_PRIVATE_TOOLS

    def test_every_registered_personal_tool_is_owner_private(self, app):
        """A future personal_* reader that skips VERBATIM_TABLE_TOOLS still fails here."""
        from cora.tools import tool_dispatch as td
        registered = {n for n in td._TOOL_FUNCTIONS if n.startswith("personal_")}
        assert registered and registered <= app._OWNER_PRIVATE_TOOLS

    def test_the_named_owner_private_tools_are_registered(self, app):
        from cora.tools import tool_dispatch as td
        assert app._OWNER_PRIVATE_NAMED_TOOLS <= set(td._TOOL_FUNCTIONS)

    def test_a_dossier_turn_writes_the_marker_not_the_content(self, app, ledger):
        import json
        ctx = app._rail_context_for_reply(dict(CTX_OPEN), {}, {"tool_names": ["cora_person_dossier"]})
        se.screen_phantom_write_claims("Tessa's week: 14 emails with the Kroger buyer. I updated her notes.",
                                       tool_use_count=0, channel_name="dm", user_id=HARRISON,
                                       rail_context=ctx)
        rows = _rows(ledger)
        assert rows and all(r["snippet"] == se.RAIL_SNIPPET_WITHHELD_PERSONAL for r in rows)
        assert "Kroger" not in json.dumps(rows)

    def test_an_ordinary_read_tool_keeps_its_scope(self, app):
        base = dict(CTX_OPEN)
        assert app._rail_context_for_reply(base, {}, {"tool_names": ["calendar_get_my_events",
                                                                     "qbo_get_profit_loss"]}) is base
