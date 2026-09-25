"""Code #15 S2 (cq-22b84598aee8) -- gap-task dedup: normalizer, two-tier matchers,
the propose-once/created ledger, and the pass-5 proposal gate.

THE SHAPE UNDER TEST IS TWO TIERS, NOT "EVERY FAMILY COLLAPSES". Measured on the
live 430-row pass-5 corpus (2026-09-24): a safe normalizer merges the figure/date
drift (tier A, auto-suppressed); the LLM paraphrases are only merged by rules loose
enough to also merge DISTINCT gaps, so they are tier B -- listed, never suppressed.
These tests pin the suppress/list TABLE, and pin that the negative probes stay
apart. Tenant/person names are anonymized; figures, dates, parentheticals and
invoice ids are kept exactly as they occurred, because those are what the
normalizer acts on.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from _timing import best_of_3
from cora import gap_task_dedup as gtd


def _tier(a: str, b: str) -> str:
    return gtd.match_tier(a, b)


# ── the six named families: the suppress / list table ────────────────────────

CASH = [
    "Track cash position week-over-week and monitor for refresh",
    "Monitor cash position day-over-day — cash dropped $23,186 to $35,337",
    "Monitor cash position day-over-day (currently $35,337)",
    "Monitor cash position and assess inflow stabilization",
    "Monitor cash position and assess inflow stabilization ($33,487 as of 2026-08-27)",
]
WHEY = [
    "Implement Whey Protein 20 lb pricing adjustment ($240 to $295)",
    "Implement Whey Protein 20 lb pricing adjustment ($240 to $295) effective 8/28/26",
    "Whey Protein price increase implementation ($240 → $295/unit, effective 8/28/26)",
    "Implement Whey Protein price increase ($240 → $295/unit, effective 8/28/26)",
    "Implement whey protein wholesale price increase ($240 → $295/unit) effective 8/28/26",
]
DLC = [
    "Collect $10,196.12 from Acme Wellness Counseling (inv. #2077, due 2026-08-15)",
    "Collect $10,196.12 from Acme Wellness Counseling",
    "Collect $10,196.12 from Acme Wellness Counseling (inv. #2077, overdue since 2026-08-15)",
]
THIELE = [
    "Collect $1,500 remainder from Pat Doe (inv. #2056)",
    "Collect $1,500 remainder from Pat Doe (inv. #2056) - Sam Roe to provide edited invoice",
]
BUZZELLI = [
    "Process applicant Zorvan for Team Member role at Gilbert Val Vista/Pecos",
    "Process employment application for Zorvan - Team Member role at Gilbert Val Vista/Pecos",
]
FEEDSPOT = [
    "Complete FeedSpot Business tier signup and confirm CEO Podcasts placement",
    "Complete FeedSpot Business tier signup ($19/mo) for CEO Podcasts list",
]


def test_cash_family_figure_drift_is_tier_A_and_the_rest_are_listed():
    # the two tier-A pairs the recon measured (created 9/1=9/3 and 9/9=9/15)
    assert _tier(CASH[2], CASH[1]) == "A"
    assert _tier(CASH[4], CASH[3]) == "A"
    # every other member is LISTED against the family, never suppressed
    assert _tier(CASH[1], CASH[0]) == "B"
    assert _tier(CASH[3], CASH[0]) == "B"
    assert _tier(CASH[3], CASH[1]) == "B"
    assert _tier(CASH[4], CASH[2]) == "B"


def test_whey_family_one_exact_and_one_token_set_pair_are_A_rest_listed():
    assert _tier(WHEY[1], WHEY[0]) == "A"          # 'effective 8/28/26' drift
    assert _tier(WHEY[3], WHEY[2]) == "A"          # implement == implementation
    assert _tier(WHEY[4], WHEY[3]) == "B"          # 'wholesale' -- a paraphrase
    assert _tier(WHEY[2], WHEY[0]) == "B"


def test_dlc_family_all_three_are_A():
    assert _tier(DLC[1], DLC[0]) == "A"
    assert _tier(DLC[2], DLC[0]) == "A"
    assert _tier(DLC[2], DLC[1]) == "A"


def test_thiele_prefix_is_A():
    assert _tier(THIELE[1], THIELE[0]) == "A"


def test_buzzelli_and_feedspot_paraphrases_are_listed_not_suppressed():
    assert _tier(BUZZELLI[1], BUZZELLI[0]) == "B"
    assert _tier(FEEDSPOT[1], FEEDSPOT[0]) == "B"


def test_every_family_member_is_suppressed_or_listed():
    for fam in (CASH, WHEY, DLC, THIELE, BUZZELLI, FEEDSPOT):
        for i, cand in enumerate(fam[1:], 1):
            assert any(_tier(cand, earlier) for earlier in fam[:i]), cand


# ── negative probes: distinct gaps must stay apart ───────────────────────────

def test_distinct_invoice_sets_never_match():
    q2 = ("Process quarterly security monitoring invoices from Brightline Electric "
          "(invoices 6098, 6102, 6103, 6104)")
    q3 = "Process Brightline Electric invoices (6155, 6156, 6157) - Quarterly Security Monitoring"
    assert _tier(q2, q3) == ""


def test_different_invoice_ids_never_match():
    a = "Collect $10,196.12 from Acme Wellness Counseling (inv. #2077, due 2026-08-15)"
    b = "Collect $10,196.12 from Acme Wellness Counseling (inv. #2101, due 2026-09-15)"
    assert _tier(a, b) == ""


def test_two_dated_runs_of_a_recurring_task_never_match():
    """A date that survives the drift removal is identity: two payroll runs are
    two tasks. Without this guard the second week's run would be suppressed at
    proposal time for the whole 120-day window (D-051 self-review)."""
    assert _tier("Process payroll for 9/2/26 with OT hours",
                 "Process payroll for 9/16/26 with OT hours") == ""
    assert _tier("Send the rent notice by Sept 1", "Send the rent notice by Oct 1") == ""
    # ...but the SAME date, or a date only in drift positions, still matches
    assert _tier("Process payroll for 9/2/26 with OT hours",
                 "Process payroll for 9/2 with OT hours") == "A"
    assert _tier(CASH[4], "Monitor cash position and assess inflow stabilization "
                          "($35,337 as of 2026-08-20)") == "A"


def test_january_vs_february_is_listed_only():
    jan = "Track and complete F3 Instagram sponsorship posts for January fighters"
    feb = "Follow up on incomplete sponsorship posts for February fighters"
    assert _tier(jan, feb) == "B"


def test_dash_tail_sub_tasks_are_listed_only():
    """The one false merge the recon found before the dash-tail refinement."""
    a = "REP Fitness Cooper DeJean content shoot — coordinate treadmill & VPR Air Bike featuring"
    b = ("REP Fitness Cooper DeJean content shoot — finalize filming location and "
         "timing for week of 7/30")
    assert _tier(a, b) == "B"


def test_same_address_different_job_never_matches():
    awning = "Awning installation at 1337 S. Gilbert Rd #122"
    window = "Pass-through window modification at 1337 S. Gilbert Rd #122 - licensed glazier approval"
    assert _tier(awning, window) == ""
    # ...while the same awning job with a detail tail IS the same task
    assert _tier("Awning installation at 1337 S. Gilbert Rd #122 - black-and-white striped",
                 awning) == "A"


def test_cross_entity_never_matches_in_ledger_queries():
    rows = [{"kind": "created", "ref": "u1", "gid": "1", "entity": "HJRPROD",
             "subject": CASH[3], "ts": datetime.now(timezone.utc).isoformat()}]
    assert gtd.find_tier_a("HJRP", CASH[4], rows) is None
    assert gtd.near_duplicates("HJRP", CASH[4], rows) == []
    assert gtd.find_tier_a("HJRPROD", CASH[4], rows) is not None


def test_a_short_prefix_is_not_enough_for_tier_A():
    """3 content tokens is below the prefix floor -- listed, never suppressed."""
    assert _tier("Collect $1,500 remainder from Pat", THIELE[0]) == "B"


# ── normalizer details ───────────────────────────────────────────────────────

def test_normalize_strips_the_legacy_prefix_and_figures():
    assert gtd.normalize("[HJRP] Drive doc suggests missing task: " + CASH[4]) == \
        gtd.normalize(CASH[3]) == "monitor cash position and assess inflow stabilization"
    assert gtd.normalize("[HJRP] " + CASH[3]) == gtd.normalize(CASH[3])


def test_ids_come_from_the_raw_text_including_parentheticals():
    assert gtd.extract_ids(DLC[0]) == frozenset({"2077"})
    assert gtd.extract_ids("File docs (lot BCB26161 — 5,588 cases)") == frozenset({"26161", "5588"})
    assert gtd.extract_ids(CASH[4]) == frozenset()   # money + ISO date removed first


@pytest.mark.parametrize("tok", ["implementation", "implement", "processing",
                                 "settings", "set", "ring", "installation", "a", ""])
def test_stem_is_idempotent(tok):
    assert gtd.stem(gtd.stem(tok)) == gtd.stem(tok)


def test_stem_joins_implement_and_implementation():
    assert gtd.stem("implement") == gtd.stem("implementation") == gtd.stem("implementing")


_SHAPES = {
    "open-paren": "(" * 10000, "close-paren": ")" * 10000, "money": "$" + "1," * 5000,
    "dashes": "— " * 5000, "digits": "9" * 10000, "slashes": "a/" * 5000,
    "as-of": "as of " * 2000, "months": "jan " * 2500, "brackets": "[" * 10000,
    "md-dates": "1/1/" * 2500,
}


@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_linear_cost_on_degenerate_input(name):
    shape = _SHAPES[name]
    """D-171: every regex bounded; the input cap applies before any of them."""
    assert best_of_3(gtd.normalize, shape) < 0.2
    assert best_of_3(gtd.extract_ids, shape) < 0.2
    assert best_of_3(gtd.match_tier, shape, shape[::-1]) < 0.5
    # the raw patterns themselves, uncapped
    for rx in (gtd._MONEY_RE, gtd._PAREN_RE, gtd._MONTH_DATE_RE, gtd._PREFIX_RE,
               gtd._CLAUSE_RE, gtd._NUMRUN_RE, gtd._DASH_SPLIT_RE):
        assert best_of_3(lambda: list(rx.finditer(shape))) < 0.2


# ── plan text helpers ────────────────────────────────────────────────────────

def test_subject_of_prefers_payload_then_suggested_then_description():
    assert gtd.subject_of({"payload": {"subject": "S"}, "description": "[X] D"}) == "S"
    assert gtd.subject_of({"payload": {"suggested_task_name": "[F3E] T"}}) == "T"
    assert gtd.subject_of({"payload": {}, "description":
                           "[OSN] Drive doc suggests missing task: Legacy one"}) == "Legacy one"


# ── ledger ───────────────────────────────────────────────────────────────────

def _ledger(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "gap-ledger.jsonl"
    monkeypatch.setenv("GAP_TASK_FP_PATH", str(p))
    return p


def _now_iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _seed_proposed_updates(monkeypatch, tmp_path, live_rows, archive_rows=()):
    from cora import knowledge_review as kr
    live = tmp_path / "proposed.jsonl"
    arch = tmp_path / "proposed.archive.jsonl"
    live.write_text("".join(json.dumps(r) + "\n" for r in live_rows), encoding="utf-8")
    arch.write_text("".join(json.dumps(r) + "\n" for r in archive_rows), encoding="utf-8")
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", live)
    monkeypatch.setattr(kr, "_ARCHIVE_PATH", arch)
    return live, arch


def _p5row(uid, ent, subj, state="PENDING", days_ago=1.0):
    return {"update_id": f"pass5:drive:{uid}", "update_type": "asana_task",
            "description": f"[{ent}] Drive doc suggests missing task: {subj}",
            "payload": {}, "state": state, "proposed_at": _now_iso(days_ago)}


def test_bootstrap_seeds_from_BOTH_files_any_state_pass5_only(tmp_path, monkeypatch):
    led = _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [
        _p5row("aaaa0001", "HJRP", CASH[3], "APPROVED"),
        {"update_id": "missing_asana_task:x:1", "update_type": "asana_task",
         "description": "pass1 row", "payload": {"suggested_task_name": "p1"},
         "state": "PENDING", "proposed_at": _now_iso()},
        _p5row("aaaa0009", "LEX-LLC", "never seeded", "PENDING"),
    ], archive_rows=[_p5row("aaaa0002", "OSN", WHEY[0], "DISMISSED")])
    n = gtd.ensure_bootstrapped()
    assert n == 2
    rows = [json.loads(ln) for ln in led.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["kind"] == "bootstrap" and rows[0]["rows"] == 2
    refs = {r["ref"]: r for r in rows[1:]}
    assert set(refs) == {"pass5:drive:aaaa0001", "pass5:drive:aaaa0002"}
    assert refs["pass5:drive:aaaa0002"]["state"] == "DISMISSED"
    # a second call never re-seeds
    assert gtd.ensure_bootstrapped() == 0


def test_a_read_only_caller_bootstraps_in_memory_and_writes_nothing(tmp_path, monkeypatch):
    """--dry-run builds the seed in memory only (Q9)."""
    led = _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [_p5row("bbbb0001", "HJRP", CASH[3])])
    rows = gtd.ledger_rows(persist=False)
    assert [r["ref"] for r in rows] == ["pass5:drive:bbbb0001"]
    assert not led.exists()
    # and a write path persists it before appending (never pre-empts the seed)
    assert gtd.record_proposal(gap_id="pass5:drive:bbbb0002", entity="HJRP", subject=WHEY[0])
    refs = [json.loads(ln).get("ref") for ln in led.read_text(encoding="utf-8").splitlines()]
    assert "pass5:drive:bbbb0001" in refs and "pass5:drive:bbbb0002" in refs


def test_the_window_drops_old_rows(tmp_path, monkeypatch):
    led = _ledger(tmp_path, monkeypatch)
    led.write_text(
        json.dumps({"kind": "bootstrap", "ts": _now_iso(), "rows": 0}) + "\n"
        + json.dumps({"kind": "created", "ts": _now_iso(200), "ref": "old", "gid": "1",
                      "entity": "HJRP", "subject": CASH[3]}) + "\n"
        + json.dumps({"kind": "created", "ts": _now_iso(1), "ref": "new", "gid": "2",
                      "entity": "HJRP", "subject": CASH[3]}) + "\n", encoding="utf-8")
    assert [r["ref"] for r in gtd.ledger_rows(persist=False)] == ["new"]


def test_a_corrupt_or_unreadable_ledger_fails_OPEN(tmp_path, monkeypatch):
    led = _ledger(tmp_path, monkeypatch)
    led.write_text("not json\n[1,2]\n\n{\"kind\": \"created\"}\n", encoding="utf-8")
    assert gtd.ledger_rows(persist=True) == []          # nothing suppressed
    monkeypatch.setenv("GAP_TASK_FP_PATH", str(tmp_path))  # a DIRECTORY
    assert gtd.ledger_rows(persist=True) == []


def test_a_write_failure_is_fail_soft(tmp_path, monkeypatch):
    monkeypatch.setenv("GAP_TASK_FP_PATH", str(tmp_path))  # a DIRECTORY: cannot append
    assert gtd.record_created(update_id="u", entity="HJRP", subject="s", gid="1") is False


def test_record_proposal_is_idempotent_per_ref_and_never_records_lex(tmp_path, monkeypatch):
    led = _ledger(tmp_path, monkeypatch)
    assert gtd.record_proposal(gap_id="pass5:drive:c1", entity="OSN", subject=WHEY[0])
    assert not gtd.record_proposal(gap_id="pass5:drive:c1", entity="OSN", subject=WHEY[0])
    assert not gtd.record_proposal(gap_id="pass5:drive:c2", entity="LEX-LLC", subject="x")
    rows = [json.loads(ln) for ln in led.read_text(encoding="utf-8").splitlines()]
    assert [r.get("ref") for r in rows if r["kind"] == "proposed"] == ["pass5:drive:c1"]


def test_ledger_rows_carry_ids_and_the_key_never_the_source_doc(tmp_path, monkeypatch):
    led = _ledger(tmp_path, monkeypatch)
    gtd.record_created(update_id="pass5:drive:d1", entity="HJRP", subject=CASH[3],
                       gid="1218344765000648", url="https://app.asana.com/x",
                       project_gid="1216928758714643", assignee_gid="1209060959783860")
    row = [json.loads(ln) for ln in led.read_text(encoding="utf-8").splitlines()][-1]
    assert row["kind"] == "created" and row["gid"] == "1218344765000648"
    assert row["key"] == gtd.normalize(CASH[3])
    assert "source" not in row and "source_filename" not in row


def test_execution_time_proposals_must_be_EARLIER_but_created_rows_always_count():
    """Two legacy PENDING duplicates must not refuse each other forever: at
    execution only an EARLIER proposal counts; a created task counts always."""
    rows = [
        {"kind": "proposed", "ref": "later", "entity": "HJRP", "subject": CASH[3],
         "ts": _now_iso(1)},
        {"kind": "proposed", "ref": "earlier", "entity": "HJRP", "subject": CASH[3],
         "ts": _now_iso(10)},
    ]
    me = _now_iso(5)
    hit = gtd.find_tier_a("HJRP", CASH[4], rows, exclude_ref="me", before_ts=me)
    assert hit is not None and hit["ref"] == "earlier"
    assert gtd.find_tier_a("HJRP", CASH[4], rows[:1], exclude_ref="me", before_ts=me) is None
    created = [{"kind": "created", "ref": "x", "gid": "9", "entity": "HJRP",
                "subject": CASH[3], "ts": _now_iso(0)}]
    assert gtd.find_tier_a("HJRP", CASH[4], created, before_ts=me) is not None


# ── the fixture pair, synthetic (1218344765000648 is COMPLETED live now) ──────

def test_fixture_pair_the_second_cash_task_is_a_tier_A_repeat(tmp_path, monkeypatch):
    """9/15: 1218516997344812 was created for the '($33,487 as of 2026-08-27)'
    variant while 1218344765000648 -- the same task minus the figure -- was still
    open. The ledger now holds the first create; the second is a tier-A repeat."""
    _ledger(tmp_path, monkeypatch)
    gtd.record_created(update_id="pass5:drive:9d1b78bb", entity="HJRP",
                       subject="Monitor cash position and assess inflow stabilization",
                       gid="1218344765000648")
    hit = gtd.find_tier_a(
        "HJRP", "Monitor cash position and assess inflow stabilization ($33,487 as of 2026-08-27)",
        gtd.ledger_rows(persist=True), exclude_ref="pass5:drive:16975563",
        before_ts=_now_iso(0))
    assert hit is not None and hit["gid"] == "1218344765000648"


# ── Code #15 D-051 r1 (s2#0 / s2#2) ──────────────────────────────────────────

# Each pair differs ONLY in words the tier-A key throws away: a digit-free
# parenthetical, or a short figure-bearing dash tail. Distinct tasks -- listed
# (B), never suppressed (A) for the 120-day window.
_DISTINCT_BY_DROPPED_WORDS = [
    ("Update Shopify product page (Mango flavor)", "Update Shopify product page (Berry flavor)"),
    ("Schedule content shoot (REP Fitness)", "Schedule content shoot (Gymshark)"),
    ("Hire Team Member for Gilbert store (morning shift)",
     "Hire Team Member for Gilbert store (closing shift)"),
    ("Pay Deposco invoice - $4,800 setup fee", "Pay Deposco invoice - $2,100 monthly storage"),
    ("Collect payment from Acme Gym - $5,000 deposit",
     "Collect payment from Acme Gym - $7,500 final balance"),
    ("[F3E] Drive doc suggests missing task: Update Shopify product page (Mango flavor)",
     "Update Shopify product page (Berry flavor)"),
]


@pytest.mark.parametrize("a,b", _DISTINCT_BY_DROPPED_WORDS)
def test_words_the_key_drops_still_tell_two_tasks_apart(a, b):
    """D-051 r1 s2#2: `normalize` deleted every parenthetical and every short
    figure dash-tail, so these came out tier A -- suppressed at proposal and
    refused at execution. Now two non-empty, unequal dropped-word sets = no tier
    A; the pair is still LISTED (tier B)."""
    assert _tier(a, b) == "B"
    assert _tier(b, a) == "B"


def test_the_drift_the_key_was_measured_on_still_collapses():
    """The guard only reads DIGIT-FREE parentheticals and tails `normalize`
    really drops -- the figure/date drift stays tier A, as does one side with no
    parenthetical at all, or the same words re-inflected."""
    assert _tier(CASH[2], CASH[1]) == "A"      # (currently $35,337) vs '— cash dropped ...'
    assert _tier(CASH[4], CASH[3]) == "A"      # ($33,487 as of 2026-08-27)
    assert _tier(WHEY[1], WHEY[0]) == "A"
    assert _tier("Update Shopify product page (Mango flavor)",
                 "Update Shopify product page") == "A"
    assert _tier("Update Shopify product page (Mango flavor)",
                 "Update Shopify product page (Mango flavors)") == "A"
    assert _tier("Pay Deposco invoice - $4,800 setup fee",
                 "Pay Deposco invoice - $4,900 setup fee") == "A"


# D-051 r2 s2#r2-2: the THIRD word-drop site -- `_CLAUSE_RE` deletes everything from
# "as of|currently|pending since|overdue since" to the end, and drift_identity
# guarded only the parenthetical and dash-tail sites. Each pair differs ONLY after
# the clause keyword (the last one: a MID-string keyword, whose key was "update").
_DISTINCT_BY_DROPPED_CLAUSE = [
    ("Resolve Shopify checkout bug currently affecting Apple Pay",
     "Resolve Shopify checkout bug currently affecting discount codes"),
    ("Follow up with Brightwell on sleeves currently blocked by artwork",
     "Follow up with Brightwell on sleeves currently blocked by freight"),
    ("Chase the Acme Gym invoice overdue since the March promo",
     "Chase the Acme Gym invoice overdue since the April rebrand"),
    ("Reconcile the vendor ledger pending since the audit",
     "Reconcile the vendor ledger pending since the migration"),
    ("Update the currently active price list for Target",
     "Update the currently active wholesale agreement with Costco"),
    ("[F3E] Drive doc suggests missing task: Resolve Shopify checkout bug currently "
     "affecting Apple Pay", "Resolve Shopify checkout bug currently affecting discount codes"),
]


@pytest.mark.parametrize("a,b", _DISTINCT_BY_DROPPED_CLAUSE)
def test_words_the_clause_drop_removes_still_tell_two_tasks_apart(a, b):
    assert gtd.normalize(a) == gtd.normalize(b)       # the key alone cannot tell them apart
    assert _tier(a, b) == "B"
    assert _tier(b, a) == "B"
    ca, cb = gtd.drift_identity(a)[2], gtd.drift_identity(b)[2]
    assert ca and cb and ca != cb                     # the guard's third set sees it


def test_the_clause_drift_still_collapses():
    """Only the clause's CONTENT words count: a figure-only clause, one side with no
    clause at all, or the same words re-inflected stay tier A (the measured drift)."""
    assert _tier("Reconcile the vendor ledger as of 2026-08-27",
                 "Reconcile the vendor ledger as of 2026-09-03") == "A"
    assert _tier("Monitor the ad spend currently $4,100",
                 "Monitor the ad spend currently $5,250") == "A"
    assert _tier("Resolve Shopify checkout bug currently affecting Apple Pay",
                 "Resolve Shopify checkout bug") == "A"
    assert _tier("Resolve Shopify checkout bug currently affecting Apple Pay",
                 "Resolve Shopify checkout bug currently affecting Apple Pay payments") == "A"
    assert _tier(CASH[2], CASH[1]) == "A"      # "(currently $35,337)": inside a paren
    assert _tier(DLC[2], DLC[0]) == "A"        # "(..., overdue since 2026-08-15)"


# D-051 r3 s1s2#r3-0: a clause-bearing subject vs its OWN truncated copies. The
# clause starts before the cut and runs past it, so each copy carries a CUT clause.
_CLAUSE_SUBJ_191 = (
    "Follow up with Brightwell Packaging on the retail sleeve reprint for the Costco "
    "roadshow, currently blocked by artwork approval from the design agency and the "
    "freight booking with the carrier")
_CLAUSE_SUBJ_300 = (
    "Follow up with Brightwell Packaging on the retail sleeve reprint order for the "
    "Costco spring roadshow in Phoenix and the Scottsdale pop-up event series, "
    "currently blocked by final artwork approval from the outside design agency, "
    "the freight booking with the carrier and the updated pallet configuration")[:gtd._MAX_INPUT]


def test_a_subject_is_tier_A_against_its_own_truncated_copies():
    """The executor's own Asana task name is f"[{ent}] {subj}"[:150] (plan_create)
    and a ledger row stores subject[:240]; the clause guard read the copy's cut
    clause as a different task, so the project-scan net (the only one left after a
    create that timed out post-commit) no longer recognised the task it made."""
    assert len(_CLAUSE_SUBJ_191) > 150 and len(_CLAUSE_SUBJ_300) > gtd._MAX_SUBJECT_STORED
    for subj, copy in (
            (_CLAUSE_SUBJ_191, f"[F3E] {_CLAUSE_SUBJ_191}"[:150].strip()),  # plan_create name
            (_CLAUSE_SUBJ_300, _CLAUSE_SUBJ_300[:gtd._MAX_SUBJECT_STORED])):  # ledger subject
        cut, full = gtd.drift_identity(copy)[2], gtd.drift_identity(subj)[2]
        assert cut and full and cut < full                    # a CUT clause: strict subset
        assert gtd.tier_a(subj, copy) and gtd.tier_a(copy, subj)
        assert _tier(subj, copy) == "A"
    # every CAP-length cut point of the 191-char subject past its clause keyword, word
    # boundary or not (real copies exist only at the 150 / 240 caps -- D-051 r4)
    kw = _CLAUSE_SUBJ_191.index("currently")
    for n in range(max(kw + len("currently b"), gtd._TRUNCATION_MIN_CHARS), len(_CLAUSE_SUBJ_191)):
        assert gtd.tier_a(_CLAUSE_SUBJ_191, _CLAUSE_SUBJ_191[:n]), n


@pytest.mark.parametrize("short,longer", [
    # D-051 r4: a CHARACTER prefix in the dropped region is a different task, not a copy
    ("Order F3 Pure cans - 12 Lemon", "Order F3 Pure cans - 12 Lemonade"),
    ("Reprint F3 Pure labels - 4 Mango", "Reprint F3 Pure labels - 4 Mangosteen"),
    ("Ship the pallet to the depot, currently blocked by Dana",
     "Ship the pallet to the depot, currently blocked by Danaher"),
    # a word-boundary extension of a SHORT subject (< 4 content tokens) is not a copy
    ("Hire currently open roles", "Hire currently open roles for the Phoenix warehouse"),
    ("Fix currently broken POS", "Fix currently broken POS at the Gilbert store"),
])
def test_a_short_or_mid_word_prefix_is_not_a_truncated_copy(short, longer):
    assert not gtd._is_truncated_copy(short, longer)
    assert not gtd.tier_a(short, longer) and not gtd.tier_a(longer, short)


def test_a_truncated_copy_is_found_by_the_ledger_but_a_different_clause_is_not(tmp_path,
                                                                            monkeypatch):
    _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [])
    assert gtd.record_created(update_id="u-trunc-1", entity="F3E", subject=_CLAUSE_SUBJ_300,
                              gid="1200000000000001")
    rows = gtd.ledger_rows(persist=True)
    assert rows[-1]["subject"] == _CLAUSE_SUBJ_300[:gtd._MAX_SUBJECT_STORED]
    hit = gtd.find_tier_a("F3E", _CLAUSE_SUBJ_300, rows)
    assert hit is not None and hit["ref"] == "u-trunc-1"
    # not a copy: the same key with a DIFFERENT clause still falls to tier B
    other = _CLAUSE_SUBJ_300.split("currently")[0] + "currently blocked by the landlord"
    assert gtd.find_tier_a("F3E", other, rows) is None


def test_a_dropped_clause_conflict_is_listed_by_the_ledger_never_suppressed(tmp_path,
                                                                            monkeypatch):
    _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [])
    assert gtd.record_proposal(gap_id="pass5:drive:cls00001", entity="F3E",
                               subject="Resolve Shopify checkout bug currently affecting Apple Pay")
    rows = gtd.ledger_rows(persist=True)
    other = "Resolve Shopify checkout bug currently affecting discount codes"
    assert gtd.find_tier_a("F3E", other, rows) is None                       # not suppressed
    assert [n["ref"] for n in gtd.near_duplicates("F3E", other, rows)] == \
        ["pass5:drive:cls00001"]                                              # ... listed


def test_a_dropped_word_conflict_is_listed_by_the_ledger_never_suppressed(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [])
    assert gtd.record_proposal(gap_id="pass5:drive:fla00001", entity="F3E",
                               subject="Update Shopify product page (Mango flavor)")
    rows = gtd.ledger_rows(persist=True)
    berry = "Update Shopify product page (Berry flavor)"
    assert gtd.find_tier_a("F3E", berry, rows) is None             # not suppressed
    assert [n["ref"] for n in gtd.near_duplicates("F3E", berry, rows)] == \
        ["pass5:drive:fla00001"]                                    # ... listed


def _flaky_seed_replace(monkeypatch, fails: int):
    """Path.replace of the seed's .tmp raises `fails` times (a Windows AV scan /
    sharing violation on the rename), then works. The APPEND opens a different
    file, so it is not blocked -- the asymmetry the review found."""
    real = Path.replace
    left = {"n": fails}

    def _replace(self, target):
        if str(self).endswith(".tmp") and left["n"] > 0:
            left["n"] -= 1
            raise PermissionError("sharing violation")
        return real(self, target)
    monkeypatch.setattr(Path, "replace", _replace)
    return left


def test_a_failed_seed_write_never_lets_an_append_create_the_ledger(tmp_path, monkeypatch):
    """D-051 r1 s2#0 (a): two failed seed writes (the executor's ledger read,
    then the append's own attempt) used to be followed by `open("a")`, which
    CREATED the ledger holding only the new row -- and since the path then
    existed, the one-time seed never ran again: the propose-once net was gone."""
    led = _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [
        _p5row(f"sd{i:06d}", "OSN", f"Awning installation at store {i} front", "APPROVED",
               days_ago=10) for i in range(5)])
    gtd._BOOT_CACHE.clear()
    _flaky_seed_replace(monkeypatch, fails=2)
    assert len(gtd.ledger_rows(persist=True)) == 5        # seed write 1 fails: in memory
    assert gtd.record_created(update_id="pass5:drive:new00001", entity="OSN",
                              subject="Something else entirely new", gid="1") is False
    assert not led.exists()                               # never created without the seed
    # the rename works again: the next write seeds FIRST, then appends
    assert gtd.record_created(update_id="pass5:drive:new00002", entity="OSN",
                              subject="Another new thing", gid="2") is True
    rows = gtd.ledger_rows(persist=True)
    assert sum(1 for r in rows if r["kind"] == "proposed") == 5
    assert gtd.find_tier_a("OSN", "Awning installation at store 1 front", rows,
                           before_ts=_now_iso(0)) is not None


def test_a_raising_seed_read_never_escapes_record_created(tmp_path, monkeypatch, caplog):
    """D-051 r1 s2#0 (b): `bootstrap_rows` raising (a bad byte in the 18 MB
    archive) escaped `ensure_bootstrapped` -> `_append` -> `record_created` --
    AFTER the executor's Asana create. A ledger WRITE failure is fail-SOFT."""
    led = _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [])
    gtd._BOOT_CACHE.clear()

    def _boom():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")
    monkeypatch.setattr(gtd, "bootstrap_rows", _boom)
    caplog.set_level(logging.WARNING)
    assert gtd.ensure_bootstrapped() == 0
    assert gtd.record_created(update_id="pass5:drive:x1", entity="OSN", subject="s",
                              gid="1") is False
    assert gtd.record_proposal(gap_id="pass5:drive:x2", entity="OSN", subject="t") is False
    assert not led.exists()
    assert gtd.ledger_rows(persist=True) == []            # the read side: fail OPEN


def test_a_failed_seed_write_keeps_the_in_memory_seed(tmp_path, monkeypatch):
    """The cache used to be popped BEFORE the write, so a failed write lost the
    in-memory seed too. It is dropped only once a write has succeeded."""
    led = _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [_p5row("kc000001", "HJRP", CASH[3])])
    gtd._BOOT_CACHE.clear()
    assert len(gtd.ledger_rows(persist=False)) == 1       # builds the cache, writes nothing
    _flaky_seed_replace(monkeypatch, fails=1)
    assert gtd.ensure_bootstrapped() == 0
    assert str(led) in gtd._BOOT_CACHE
    assert gtd.ensure_bootstrapped() == 1 and led.exists()
    assert str(led) not in gtd._BOOT_CACHE


# ── pass 5: the proposal gate ────────────────────────────────────────────────

def _drive_db(tmp_path, entity="HJRP") -> Path:
    db = tmp_path / "kb.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY, source TEXT,
        source_id TEXT, entity TEXT, sub_entity TEXT, content TEXT, deep_link TEXT,
        title TEXT, metadata TEXT DEFAULT '{}', ingested_at INTEGER,
        date_created INTEGER, date_modified INTEGER)""")
    conn.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("c1", "drive_sweep", "digest-2026-08-27", entity, None,
                  "Cash position summary for the property portfolio this week.",
                  "", "", "{}", int(time.time()), None, None))
    conn.commit()
    conn.close()
    return db


def _haiku(missing: list[dict]) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(
        text=json.dumps({"missing_tasks": missing, "decisions": [], "completed_tasks": []}))])
    return client


def _run_pass5(db, client, open_tasks=()):
    from cora import reconciliation_engine as re_
    return [g for g in re_.reconcile(open_tasks=list(open_tasks), active_deals=[],
                                     db_path=db, passes=[5], anthropic_client=client)
            if g.gap_type == "missing_asana_task"]


def test_pass5_payload_carries_subject_source_key_and_NO_entity(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch)
    gaps = _run_pass5(_drive_db(tmp_path), _haiku([
        {"subject": CASH[4], "source_filename": "digest-2026-08-27", "entity": "HJRP",
         "confidence": "HIGH"}]))
    assert len(gaps) == 1
    p = gaps[0].payload
    assert p["subject"] == CASH[4]
    assert p["source_filename"] == "digest-2026-08-27"
    assert p["dedup_key"] == gtd.normalize(CASH[4])
    assert p["suggested_task_name"] == f"[HJRP] {CASH[4]}"[:150]
    assert "entity" not in p  # Q4: no delegation widening


def test_pass5_suppresses_a_tier_A_repeat_on_the_second_run(tmp_path, monkeypatch, caplog):
    from cora import reconciliation_engine as re_
    _ledger(tmp_path, monkeypatch)
    db = _drive_db(tmp_path)
    first = _run_pass5(db, _haiku([{"subject": CASH[3], "source_filename": "d1",
                                    "entity": "HJRP", "confidence": "HIGH"}]))
    assert len(first) == 1
    assert re_.record_task_proposals(first) == 1   # the runner, after propose
    caplog.set_level(logging.INFO, logger="cora.reconciliation_engine")
    second = _run_pass5(db, _haiku([{"subject": CASH[4], "source_filename": "d2",
                                     "entity": "HJRP", "confidence": "HIGH"}]))
    assert second == []
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "suppressed" in msgs
    assert "inflow stabilization" not in msgs  # ids + counts, never the subject


def test_pass5_never_suppresses_a_paraphrase(tmp_path, monkeypatch):
    from cora import reconciliation_engine as re_
    _ledger(tmp_path, monkeypatch)
    db = _drive_db(tmp_path, entity="OSN")
    first = _run_pass5(db, _haiku([{"subject": BUZZELLI[0], "source_filename": "d1",
                                    "entity": "OSN", "confidence": "HIGH"}]))
    re_.record_task_proposals(first)
    again = _run_pass5(db, _haiku([{"subject": BUZZELLI[1], "source_filename": "d2",
                                    "entity": "OSN", "confidence": "HIGH"}]))
    assert len(again) == 1   # tier B is listed on the card, not suppressed here


def test_pass5_in_pass_duplicate_pair_emits_one_gap(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch)
    gaps = _run_pass5(_drive_db(tmp_path), _haiku([
        {"subject": CASH[3], "source_filename": "d1", "entity": "HJRP", "confidence": "HIGH"},
        {"subject": CASH[4], "source_filename": "d2", "entity": "HJRP", "confidence": "MED"},
    ]))
    assert len(gaps) == 1


def test_pass5_suppresses_a_repeat_of_an_open_entity_task(tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch)
    gaps = _run_pass5(_drive_db(tmp_path), _haiku([
        {"subject": CASH[4], "source_filename": "d", "entity": "HJRP", "confidence": "HIGH"}]),
        open_tasks=[{"gid": "1", "name": f"[HJRP] {CASH[3]}"}])
    assert gaps == []


def test_pass5_building_gaps_records_nothing(tmp_path, monkeypatch):
    """A dry run builds every gap and proposes nothing: building must not write
    the ledger (the fact_fingerprint D-051 rule)."""
    led = _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [_p5row("eeee0001", "OSN", WHEY[0])])
    _run_pass5(_drive_db(tmp_path), _haiku([
        {"subject": CASH[3], "source_filename": "d", "entity": "HJRP", "confidence": "HIGH"}]))
    assert not led.exists()


# ── the runner: record after propose, and --dry-run writes nothing ───────────

def _import_runner(monkeypatch, tmp_path):
    """Import scripts/run_reconciliation without its import-time log FILE (it
    would land in the checkout's logs/)."""
    if "scripts.run_reconciliation" not in sys.modules:
        monkeypatch.setattr(logging, "FileHandler", lambda *a, **k: logging.NullHandler())
    import importlib
    rr = importlib.import_module("scripts.run_reconciliation")
    monkeypatch.setattr(rr, "GAPS_DIR", tmp_path / "gaps")
    monkeypatch.setattr(rr, "_fetch_open_tasks", lambda: [])
    monkeypatch.setattr(rr, "_fetch_active_deals", lambda: [])
    return rr


def _gap(subject=CASH[3]):
    from cora.reconciliation_engine import ReconciliationGap
    return ReconciliationGap(
        gap_id="pass5:drive:f00dbeef", gap_type="missing_asana_task",
        description=f"[HJRP] Drive doc suggests missing task: {subject}",
        source_evidence="Source: d", source="drive_sweep", source_id="d", entity="HJRP",
        confidence="HIGH", proposed_action="x",
        payload={"subject": subject, "source_filename": "d",
                 "dedup_key": gtd.normalize(subject),
                 "suggested_task_name": f"[HJRP] {subject}"},
        title=subject)


def test_runner_dry_run_proposes_nothing_and_writes_no_ledger(tmp_path, monkeypatch):
    led = _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [_p5row("ffff0001", "OSN", WHEY[0])])
    rr = _import_runner(monkeypatch, tmp_path)
    propose = MagicMock(side_effect=AssertionError("a dry run proposed"))
    monkeypatch.setattr(rr, "propose_update", propose)
    monkeypatch.setattr(rr, "reconcile", lambda *a, **k: [_gap()])
    monkeypatch.setattr("sys.argv", ["run_reconciliation.py", "--dry-run", "--passes", "5"])
    rr.main()
    propose.assert_not_called()
    assert not led.exists()


def test_runner_live_records_after_propose_and_only_then(tmp_path, monkeypatch):
    led = _ledger(tmp_path, monkeypatch)
    _seed_proposed_updates(monkeypatch, tmp_path, [_p5row("ffff0002", "OSN", WHEY[0])])
    rr = _import_runner(monkeypatch, tmp_path)
    monkeypatch.setattr(rr, "reconcile", lambda *a, **k: [_gap()])
    # a FAILED propose records nothing ...
    monkeypatch.setattr(rr, "propose_update", MagicMock(side_effect=RuntimeError("io")))
    monkeypatch.setattr("sys.argv", ["run_reconciliation.py", "--passes", "5"])
    rr.main()
    refs = [json.loads(ln).get("ref") for ln in led.read_text(encoding="utf-8").splitlines()]
    assert "pass5:drive:ffff0002" in refs            # the live run seeded the ledger
    assert "pass5:drive:f00dbeef" not in refs
    # ... a successful one records the proposal
    monkeypatch.setattr(rr, "propose_update", MagicMock(return_value=True))
    rr.main()
    refs = [json.loads(ln).get("ref") for ln in led.read_text(encoding="utf-8").splitlines()]
    assert refs.count("pass5:drive:f00dbeef") == 1
