"""C1 (cq-da2d6772f0ec): a KB eval that stays red must not stay unowned.

`f3e-pure-wholesale-ladder` failed on 8/10, 8/17 and 8/24 with no seed and no
owner, on buyer-facing wholesale pricing -- the exact class that produced the
cq-4d73879917fa fabrication in July.

THE CAUSE WAS NEITHER SUSPECT IN THE SEED. Not the contradictory 8/7 pricing
pair, and not the 2024 Infinity agreement (which does not appear in the top 8
chunks for this question at all -- that claim traced back to canon's own prose
about itself, i.e. the audit repeated a warning label as a measurement). The eval
asserted the literal "Tier 1 = 32% off", and the 2026-08-04 Pure MSRP raise
rewrote the ladder from percentages to fixed dollars ("Quote DOLLARS, not
percentages"), deleting that exact phrase. The FACT was intact and correct the
whole time -- $25.15 / $22.19 / $18.50 -- only the anchor was stale. Re-anchored
on "$25.15", which appears exactly once in the live file and, unlike a bare
"25.15", cannot be satisfied by Cora's own stale 8/1 reply chunk.

Two mechanism notes that shape the rest of this file:

  A >=2-CONSECUTIVE RULE NEEDS NO NEW STATE. `failing_ids & prev_failing` IS the
  two-in-a-row set and the runner already had both. Only a streak COUNT and
  once-per-incident seeding need persistence, and both are additive keys.

  THE SEED DEDUP IS A TRAP. code_queue.find_fingerprint is sha1(signal +
  normalized title) with NO time component and never checks item status -- so a
  signal keyed only on the eval id would seed exactly ONCE in the lifetime of the
  repo, and a regression long after that item shipped would be swallowed
  silently. The streak identity therefore rides in the signal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import run_kb_evals as ev  # noqa: E402


CASES = {"f3e-x": {"id": "f3e-x", "entity": "F3E", "question": "what is X?"}}


# ── the stale anchor ────────────────────────────────────────────────────────

def test_the_wholesale_case_is_anchored_on_the_dollar_figure():
    """A percentage framing is a presentation choice canon has already changed
    once; the tier dollars survived that rewrite."""
    import yaml
    path = Path(__file__).resolve().parents[1] / "data" / "evals" / "golden-set.yaml"
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    case = next(c for c in (cases.get("cases") or cases)
                if c.get("id") == "f3e-pure-wholesale-ladder")
    assert case["expect_substring"] == "$25.15"
    assert "32% off" not in case["expect_substring"]


# ── streaks ─────────────────────────────────────────────────────────────────

def test_a_streak_counts_consecutive_runs():
    st = {}
    for expected in (1, 2, 3):
        st = ev._bump_streaks({"a"}, st)
        assert st == {"a": expected}


def test_a_pass_resets_the_streak_entirely():
    """A later regression must start a FRESH incident, not resume an old one --
    otherwise it would seed on run one and the threshold would mean nothing."""
    st = ev._bump_streaks({"a"}, {})
    st = ev._bump_streaks({"a"}, st)
    assert st["a"] == 2
    st = ev._bump_streaks(set(), st)      # the case passed
    assert st == {}
    st = ev._bump_streaks({"a"}, st)      # and regressed later
    assert st == {"a": 1}


# ── the auto-seed ───────────────────────────────────────────────────────────

def test_one_red_week_never_seeds():
    with patch("cora.code_queue.seed_item") as seed:
        out = ev._autoseed_persistent_failures({"f3e-x": 1}, {}, CASES)
    seed.assert_not_called()
    assert out == {}


def test_two_consecutive_red_weeks_seed_once():
    with patch("cora.code_queue.seed_item", return_value="cq-abc") as seed:
        out = ev._autoseed_persistent_failures({"f3e-x": 2}, {}, CASES)
    seed.assert_called_once()
    kw = seed.call_args.kwargs
    assert kw["status"] == "PROPOSED", "an automated seeder must not create owed taps"
    assert kw["entity"] == "F3E"
    assert "f3e-x" in kw["title"] and "2 consecutive" in kw["title"]
    assert "f3e-x" in kw["signal"]
    # D-051: the marker keys on the INCIDENT (the date the streak started),
    # not on a constant. A constant made the SIGNAL identical for every
    # recurrence and walked straight into the fingerprint trap the docstring
    # says it avoids -- find_fingerprint has no time component.
    assert list(out) == ["f3e-x"]
    assert out["f3e-x"].startswith("since20")


def test_a_third_red_week_does_not_seed_again():
    """Once per incident, not once per run."""
    with patch("cora.code_queue.seed_item", return_value="cq-abc") as seed:
        out = ev._autoseed_persistent_failures({"f3e-x": 3}, {"f3e-x": "since2026-08-10"}, CASES)
    seed.assert_not_called()
    assert out == {"f3e-x": "since2026-08-10"}


def test_a_regression_after_a_fix_seeds_a_NEW_item():
    """THE DEDUP TRAP. find_fingerprint has no time component and ignores item
    status, so a signal keyed only on the eval id would seed once ever."""
    seeded = {"f3e-x": "since2026-08-10"}
    # the case passed -> the marker is dropped
    cleared = ev._autoseed_persistent_failures({}, seeded, CASES)
    assert cleared == {}
    # ...and a later regression seeds again, under a different signal
    with patch("cora.code_queue.seed_item", return_value="cq-def") as seed:
        ev._autoseed_persistent_failures({"f3e-x": 2}, cleared, CASES)
    seed.assert_called_once()


def test_the_summary_tells_the_reader_to_check_canon_too():
    """The three-week miss happened because everyone assumed a red eval means
    broken retrieval. It meant a stale literal."""
    with patch("cora.code_queue.seed_item", return_value="cq-abc") as seed:
        ev._autoseed_persistent_failures({"f3e-x": 2}, {}, CASES)
    summary = seed.call_args.kwargs["summary"]
    assert "canon" in summary.lower()
    assert "BOTH" in summary


def test_a_seed_failure_never_takes_the_run_down():
    with patch("cora.code_queue.seed_item", side_effect=RuntimeError("boom")):
        assert ev._autoseed_persistent_failures({"f3e-x": 2}, {}, CASES) == {}


def test_a_refused_seed_is_not_recorded_as_seeded():
    """seed_item returns None on a PHI refusal. Recording that as seeded would
    silence the case forever."""
    with patch("cora.code_queue.seed_item", return_value=None):
        assert ev._autoseed_persistent_failures({"f3e-x": 2}, {}, CASES) == {}


# ── state file ──────────────────────────────────────────────────────────────

def test_the_old_single_key_state_file_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_STATE_PATH", tmp_path / "s.json")
    (tmp_path / "s.json").write_text(json.dumps(
        {"failing_ids": ["a"], "ts": "2026-08-24T00:00:00Z"}), encoding="utf-8")
    failing, streaks, seeded = ev._load_state()
    assert failing == {"a"} and streaks == {} and seeded == {}


def test_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_STATE_PATH", tmp_path / "s.json")
    ev._save_last_failing({"a"}, {"a": 3}, {"a": "since2026-08-10"})
    assert ev._load_state() == ({"a"}, {"a": 3}, {"a": "since2026-08-10"})


def test_a_missing_state_file_is_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_STATE_PATH", tmp_path / "absent.json")
    assert ev._load_state() == (set(), {}, {})


# ── the render bug ──────────────────────────────────────────────────────────

def test_still_failing_is_listed_even_in_a_week_with_a_new_failure():
    """Was gated `if failed and not newly_failing`, so a week with BOTH dropped
    the standing failures out of the post entirely -- the digest reported the new
    break and silently stopped mentioning the old one."""
    msg = ev.format_slack_summary({
        "passed": 40, "evaluated": 42, "failed": 2, "pass_rate_pct": 95.2,
        "newly_failing": ["new-case"], "fixed_since_last": [],
        "failing_ids": ["new-case", "old-case"], "still_failing_unevaluated": [],
        "skipped_l2_only": 0, "load_errors": [],
        "streaks": {"old-case": 3, "new-case": 1},
    })
    assert "*Newly failing:* new-case" in msg
    assert "Still failing: old-case (3w)" in msg


def test_a_first_week_repeat_carries_no_misleading_week_count():
    msg = ev.format_slack_summary({
        "passed": 41, "evaluated": 42, "failed": 1, "pass_rate_pct": 97.6,
        "newly_failing": [], "fixed_since_last": [], "failing_ids": ["c"],
        "still_failing_unevaluated": [], "skipped_l2_only": 0, "load_errors": [],
        "streaks": {"c": 1},
    })
    assert "Still failing: c" in msg
    assert "(1w)" not in msg


def test_an_l2_only_carry_is_not_double_reported():
    msg = ev.format_slack_summary({
        "passed": 41, "evaluated": 41, "failed": 0, "pass_rate_pct": 100.0,
        "newly_failing": [], "fixed_since_last": [], "failing_ids": ["l2c"],
        "still_failing_unevaluated": ["l2c"], "skipped_l2_only": 1,
        "load_errors": [], "streaks": {},
    })
    assert msg.count("l2c") == 1
    assert "needs --answers" in msg
