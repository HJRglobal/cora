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
