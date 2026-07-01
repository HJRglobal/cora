"""Tests for run_false_deflection_watch.py -- the R6 over-deflection watch.

The watch parses the bot's own `user_access: blocked` log lines over a window,
classifies each block by TOPIC (financials/legal/hr/cap_table/phi/entity_auth),
and flags a spike of the over-deflection-prone buckets (financials + legal) on a
commercial role (Alex, Tommy, Elena). PHI and correct firewall/HR/cap_table
refusals are NEVER counted toward the flag.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_false_deflection_watch as w  # noqa: E402

ALEX = "U0B3VGWJTMJ"
TOMMY = "U0B3RU5Q55G"
SHAUN = "U0B3PS82G30"  # non-commercial

# The exact redirect strings from user_access.check_access.
R_FIN = "Company financials (P&L, cash, payroll) go in a finance channel or to Harrison."
R_LEGAL = "That's a legal matter. Reach Emily Stubbs."
R_HR = "HR matters go to Hannah Grant or Harrison."
R_CAP = "Ownership details need Harrison."
R_PHI = "Client-specific health info stays in the EHR. Ask the clinical lead."
R_ENTITY = ("That's outside what I can help with in this channel. Ask me in the "
            "channel for the team that owns it and I'll answer there.")


def _line(ts: str, tag: str, user: str, reason: str) -> str:
    return f"{ts} INFO [MainThread] cora.app: {tag} user={user} entity=F3E reason={reason}"


class TestClassifyReason:
    def test_each_redirect_maps_to_its_bucket(self):
        assert w.classify_reason(R_FIN) == "financials"
        assert w.classify_reason(R_LEGAL) == "legal"
        assert w.classify_reason(R_HR) == "hr"
        assert w.classify_reason(R_CAP) == "cap_table"
        assert w.classify_reason(R_PHI) == "phi"
        assert w.classify_reason(R_ENTITY) == "entity_auth"
        assert w.classify_reason("") == "other"

    def test_phi_robust_to_copy_change(self):
        # R4 may reword PHI copy; a token-set (not the single 'EHR' literal) still catches it.
        assert w.classify_reason("Client health info stays in the electronic health record.") == "phi"
        assert w.classify_reason("Ask the clinical lead about that.") == "phi"


class TestParseBlockLine:
    def test_handle_mention_format(self):
        ev = w.parse_block_line(_line("2026-06-30T14:41:07", "user_access: blocked", ALEX, R_FIN))
        assert ev is not None and ev["user"] == ALEX and ev["bucket"] == "financials"
        assert ev["ts"] == datetime(2026, 6, 30, 14, 41, 7)

    def test_cora_ask_and_dm_formats(self):
        assert w.parse_block_line(_line("2026-06-29T09:00:00", "cora_ask: user_access blocked", TOMMY, R_FIN))["bucket"] == "financials"
        assert w.parse_block_line(_line("2026-06-29T09:00:00", "dm_qa: user_access blocked", ALEX, R_LEGAL))["bucket"] == "legal"

    def test_reason_optional(self):
        line = "2026-06-30T10:00:00 INFO [MainThread] cora.app: dm_qa: user_access blocked user=%s entity=F3E" % ALEX
        ev = w.parse_block_line(line)
        assert ev is not None and ev["reason"] == "" and ev["bucket"] == "other"

    def test_non_block_line_ignored(self):
        assert w.parse_block_line("2026-06-30T10:00:00 INFO x: app_mention routed channel=#f3e") is None
        assert w.parse_block_line("random text") is None


class TestCollectEvents:
    def test_window_filter_drops_old(self):
        cutoff = datetime(2026, 6, 24)
        old = _line("2026-06-20T10:00:00", "user_access: blocked", ALEX, R_FIN)
        new = _line("2026-06-28T10:00:00", "user_access: blocked", ALEX, R_FIN)
        events = w.collect_events([old, new], cutoff)
        assert len(events) == 1 and events[0]["ts"] == datetime(2026, 6, 28, 10, 0, 0)

    def test_missing_ts_kept_fail_open(self):
        no_ts = "cora.app: user_access: blocked user=%s entity=F3E reason=%s" % (ALEX, R_FIN)
        assert len(w.collect_events([no_ts], datetime(2026, 6, 24))) == 1


class TestSummarizeAndFlag:
    def _events(self, specs):  # specs: list of (user, reason)
        return [{"ts": None, "user": u, "entity": "F3E", "reason": r,
                 "bucket": w.classify_reason(r)} for u, r in specs]

    def test_financials_spike_flagged(self):
        s = w.summarize(self._events([(ALEX, R_FIN), (ALEX, R_LEGAL), (ALEX, R_FIN)]))
        assert len(s["flagged"]) == 1
        assert s["flagged"][0]["user"] == ALEX and s["flagged"][0]["over_deflection"] == 3

    def test_below_threshold_not_flagged(self):
        assert w.summarize(self._events([(TOMMY, R_FIN), (TOMMY, R_FIN)]))["flagged"] == []

    def test_phi_never_flagged(self):
        assert w.summarize(self._events([(ALEX, R_PHI)] * 5))["flagged"] == []

    def test_correct_refusals_not_flagged(self):
        # prompt-and-watch-3: hr/cap_table/entity_auth are CORRECT refusals, not
        # over-deflection — a spike of them must NOT flag the role.
        s = w.summarize(self._events([(ALEX, R_HR), (ALEX, R_CAP), (ALEX, R_ENTITY),
                                      (ALEX, R_HR), (ALEX, R_CAP)]))
        assert s["flagged"] == []
        assert s["commercial_over"][ALEX] == 0  # none are financials/legal

    def test_mixed_only_financials_legal_count(self):
        # 2 financials + 1 legal (=3 over) alongside 3 correct refusals -> flagged on 3.
        s = w.summarize(self._events([(ALEX, R_FIN), (ALEX, R_FIN), (ALEX, R_LEGAL),
                                      (ALEX, R_HR), (ALEX, R_CAP), (ALEX, R_ENTITY)]))
        assert len(s["flagged"]) == 1 and s["flagged"][0]["over_deflection"] == 3

    def test_non_commercial_user_never_flagged(self):
        assert w.summarize(self._events([(SHAUN, R_FIN)] * 5))["flagged"] == []


class TestBuildReport:
    def test_clean_report_when_no_spike(self):
        assert "No over-deflection spike" in w.build_report(w.summarize([]), 7)

    def test_alert_report_when_flagged(self):
        events = [{"ts": None, "user": ALEX, "entity": "F3E", "reason": R_FIN,
                   "bucket": "financials"}] * 3
        report = w.build_report(w.summarize(events), 7)
        assert "Alex Cordova" in report and "over-deflection" in report.lower()
