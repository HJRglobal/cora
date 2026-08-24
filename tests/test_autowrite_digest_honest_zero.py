"""C3 (cq-a46ebe458d92): a zero in the weekly auto-learned digest must be legible.

The digest read 0 this week / 0 last week / 0 reverts for three straight Mondays
at level=`all` and the reader could not tell an accurate zero from a broken pipe.

VERIFY-FIRST settled the fork the seed posed (dead lane vs dead ledger read): the
READ is correct and reads the same constant the WRITE appends to. The lane is
DEAD -- `_autowrite_eligible` has never returned True in production, because
machine-mined items carry no contributor id and so tier to 2 by construction.
The zero is true. The defect is that nothing said so.

Two things were also silently wrong around it:

  THE LANE NEVER REPORTED ITSELF. `_autowrite_eligible` returns a refusal reason
  for every item it declines and the caller discarded it, and the success line
  was guarded on `if auto_done:` -- so a lane refusing 100% of its input logged
  nothing. 37 consecutive knowledge-review logs contain the string "autowrite"
  zero times.

  THE ONE NON-ZERO NUMBER MEASURED THE HUMAN. "Decisions inbox: 70 accepted"
  counts Harrison's own taps on DECISION cards -- a different lane, ledger and
  actor -- inside a message headlined "Cora auto-learned this week". (The seed
  claimed it counted auto-filings; it does not. All 70 live rows are
  via=one_tap_button. The label was the defect, not the count.)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cora import graduated_trust_shadow as gts

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import run_autowrite_digest as rad  # noqa: E402


@pytest.fixture()
def shadow_dir(tmp_path, monkeypatch):
    d = tmp_path / "shadow"
    d.mkdir()
    monkeypatch.setenv("CORA_GRADUATED_SHADOW_DIR", str(d))
    return d


def _scan_row(d: Path, *, level="all", scanned=3, applied=0, refusals=None,
              day="2026-08-24"):
    rec = {"type": "autowrite_scan", "ts": f"{day}T14:00:00+00:00",
           "level": level, "scanned": scanned, "applied": applied,
           "refusals": refusals or {}}
    with (d / f"graduated-trust-shadow-{day}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _tier_row(d: Path, uid: str, tier: int, day="2026-08-24"):
    rec = {"type": "shadow_decision", "ts": f"{day}T14:00:00+00:00",
           "update_id": uid, "update_type": "efficiency", "shadow_tier": tier,
           "shadow_decision": "harrison" if tier == 2 else "would-auto-approve"}
    with (d / f"graduated-trust-shadow-{day}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


# ── the why-zero line ───────────────────────────────────────────────────────

def test_a_nonzero_week_gets_no_explanation():
    """The line exists to explain a zero. On a real week the numbers speak."""
    assert rad._why_zero_line({"this_week": 3, "level": "all"}) == ""


def test_lane_off_says_so_plainly(shadow_dir):
    line = rad._why_zero_line({"this_week": 0, "level": "off"})
    assert "OFF" in line
    assert "the setting, not a fault" in line


def test_scan_records_are_authoritative_and_name_the_refusals(shadow_dir):
    _scan_row(shadow_dir, scanned=9, applied=0,
              refusals={"tier_2": 7, "info_for_cora_never_autowrites": 2})
    line = rad._why_zero_line({"this_week": 0, "level": "all"})
    assert "9 item(s) scanned" in line
    assert "`tier_2` x7" in line
    assert "`info_for_cora_never_autowrites` x2" in line
    assert "the lane ran, nothing qualified" in line


def test_a_lane_that_ran_but_saw_nothing_is_called_a_starved_pipe(shadow_dir):
    """scanned=0 is the one shape that IS a fault: the run happened and no
    knowledge item reached it."""
    _scan_row(shadow_dir, scanned=0, applied=0, refusals={})
    line = rad._why_zero_line({"this_week": 0, "level": "all"})
    assert "starved pipe" in line
    assert "not a quiet week" in line


def test_refusals_aggregate_across_runs_in_the_window(shadow_dir):
    _scan_row(shadow_dir, scanned=4, refusals={"tier_2": 4}, day="2026-08-22")
    _scan_row(shadow_dir, scanned=5, refusals={"tier_2": 5}, day="2026-08-24")
    line = rad._why_zero_line({"this_week": 0, "level": "all"}, days=3650)
    assert "9 item(s) scanned over 2 run(s)" in line
    assert "`tier_2` x9" in line


def test_fallback_to_shadow_tiers_when_no_scan_rows_exist_yet(shadow_dir):
    """Scan rows only start accruing at the next review run. Until then the
    shadow log can say how items TIERED but not what declined them -- and the
    line must not pretend otherwise."""
    for i in range(9):
        _tier_row(shadow_dir, f"u{i}", 2)
    line = rad._why_zero_line({"this_week": 0, "level": "all"})
    assert "nothing was ELIGIBLE" in line
    assert "T2=9" in line
    assert "the supply is Tier-2" in line


def test_fallback_flags_tier01_items_as_declined_downstream(shadow_dir):
    for i in range(2):
        _tier_row(shadow_dir, f"t0-{i}", 0)
    _tier_row(shadow_dir, "t2-1", 2)
    line = rad._why_zero_line({"this_week": 0, "level": "all"})
    assert "2 tiered to 0/1" in line
    assert "downstream rule declined them" in line
    # it must NOT assert a cause it cannot see
    assert "Worth a look" not in line


def test_no_shadow_data_at_all_yields_the_starved_wording(shadow_dir):
    line = rad._why_zero_line({"this_week": 0, "level": "all"})
    assert "0 knowledge items classified" in line
    assert "starved or broken pipe" in line


def test_the_line_is_fail_soft(shadow_dir):
    """A digest must never fail because of a diagnostic."""
    with patch.object(gts, "read_autowrite_scans", side_effect=RuntimeError("boom")):
        assert rad._why_zero_line({"this_week": 0, "level": "all"}) == ""


# ── the mislabelled counter ─────────────────────────────────────────────────

def test_the_inbox_line_names_the_actor_and_the_lane():
    with patch("cora.decision_inbox.inbox_stats",
               return_value={"total": 70, "recent": 25}), \
         patch("cora.decision_inbox._inbox_path",
               return_value=Path("decisions-inbox.md")):
        line = rad._decisions_inbox_line(days=7)
    assert "Decision cards YOU filed" in line
    assert "a separate lane, not auto-writes" in line
    assert "70 all-time" in line and "25 in the last 7d" in line
    # the bare word that made a human tally read as machine output
    assert "accepted all-time" not in line


# ── the scan record itself ──────────────────────────────────────────────────

def test_scan_record_round_trips(shadow_dir):
    assert gts.record_autowrite_scan(
        level="all", scanned=5, applied=1, refusals={"tier_2": 4}) is True
    rows = gts.read_autowrite_scans()
    assert len(rows) == 1
    assert rows[0]["scanned"] == 5 and rows[0]["applied"] == 1
    assert rows[0]["refusals"] == {"tier_2": 4}


def test_scan_rows_are_inert_in_the_shadow_report(shadow_dir):
    """It shares the shadow file, so it must not perturb the flip gauge that
    file exists to serve. It carries no update_id, so build_report skips it
    before the type check."""
    _tier_row(shadow_dir, "real-1", 2)
    gts.record_autowrite_scan(level="all", scanned=1, applied=0,
                              refusals={"tier_2": 1})
    rep = gts.build_report()
    assert rep["total_decisions"] == 1
    assert rep["by_tier"] == {"2": 1}


def test_read_scans_ignores_other_row_types_and_junk(shadow_dir):
    _tier_row(shadow_dir, "real-1", 2)
    _scan_row(shadow_dir, scanned=2)
    with (shadow_dir / "graduated-trust-shadow-2026-08-24.jsonl").open(
            "a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write("[1,2,3]\n")
    rows = gts.read_autowrite_scans()
    assert len(rows) == 1 and rows[0]["scanned"] == 2


def test_record_is_fail_soft_on_an_unwritable_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CORA_GRADUATED_SHADOW_DIR", str(tmp_path / "s"))
    with patch.object(gts, "_shadow_log_path", side_effect=OSError("nope")):
        assert gts.record_autowrite_scan(level="all", scanned=1, applied=0,
                                         refusals={}) is False


# ── the review run reports itself ───────────────────────────────────────────

def test_the_review_run_logs_and_records_even_when_it_writes_nothing(
        tmp_path, monkeypatch, caplog):
    """The `if auto_done:` guard is what made a fully-refusing lane look
    identical to one that never ran."""
    import importlib
    import run_knowledge_review as rkr
    kr = importlib.import_module("cora.knowledge_review")

    proposed = tmp_path / "proposed.jsonl"
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", proposed)
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
    kr._SEEN_IDS_CACHE = None
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "all")
    monkeypatch.setenv("CORA_GRADUATED_SHADOW_DIR", str(tmp_path / "shadow"))
    monkeypatch.setattr(rkr, "send_individual_dms", MagicMock(return_value={}))
    monkeypatch.setattr(rkr, "send_dm_to_harrison", MagicMock(return_value="hdr"))
    monkeypatch.setattr(rkr, "_send_dm_to_user", MagicMock(return_value="ts"))
    # every item refused, which is the live production shape
    monkeypatch.setattr(rkr, "_autowrite_eligible",
                        lambda u, level: (False, 2, "tier_2"))
    # the established idiom in this suite -- without it main() makes a live
    # OpenAI embeddings call and a live Anthropic call per run
    monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)

    kr.propose_update(update_id="ka-1", update_type="known_answer",
                      description="a fact", payload={"entity": "FNDR"},
                      confidence="MED")

    monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
    with caplog.at_level("INFO"):
        rkr.main()

    assert any("autowrite(all): scanned=1 written=0 -> Harrison=1 "
               "refusals=tier_2=1" in r.getMessage() for r in caplog.records),         "the lane still does not report itself"
    rows = gts.read_autowrite_scans()
    assert len(rows) == 1
    assert rows[0]["scanned"] == 1 and rows[0]["applied"] == 0
    assert rows[0]["refusals"] == {"tier_2": 1}
