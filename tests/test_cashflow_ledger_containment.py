"""KB containment + missed-run detection for the 13WCF shadow ledger (M1/S4).

The ledger writes cash figures -- eventually including the LexCorp war chest and
a portfolio roll-up -- into the Founder-OS accounting tree. None of it may ever
become a retrievable KB chunk.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

from cora import cashflow_ledger as cl
from cora.kb_exclusions import (
    is_finance_worksheet_path,
    is_finance_worksheet_title,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ── path / title exclusion ──────────────────────────────────────────────────

class TestLedgerIsKbExcluded:
    @pytest.mark.parametrize("path", [
        "01-HJR-Global/accounting/cashflow-ledger/forecast-snapshots/2026-08-10_forecast.json",
        "01-HJR-Global/accounting/cashflow-ledger/worksheets/2026-08-10_fndr_cashflow-worksheet.md",
        "01-HJR-Global/accounting/cashflow-ledger/candidates/2026-08-10.md",
        "01-HJR-Global/accounting/cashflow-ledger/outlook-founder.json",
        r"01-HJR-Global\accounting\cashflow-ledger\actuals\2026-08-10_final-W2.json",
    ])
    def test_every_ledger_family_is_excluded(self, path):
        assert is_finance_worksheet_path(path)

    def test_the_a5_worksheet_lane_is_still_excluded(self):
        """Don't regress the family this rule was built for."""
        assert is_finance_worksheet_path(
            "01-HJR-Global/accounting/forecast-assist/2026-08-05_fndr_forecast-assist.md")

    def test_sibling_accounting_content_still_ingests(self):
        """Close packs ARE knowledge -- the rule must stay narrow."""
        assert not is_finance_worksheet_path("01-HJR-Global/accounting/close-packs/x.md")
        assert not is_finance_worksheet_path("01-HJR-Global/accounting/live-snapshots/x.json")
        assert not is_finance_worksheet_path("02-F3-Energy/projects/cashflow.md")
        assert not is_finance_worksheet_path("")

    def test_title_predicate_covers_the_drive_sweep_door(self):
        """drive_sweep stores a bare file id and NO path, and sweep_founders_os
        walks 01-HJR-Global -- so the segment rule alone cannot see these."""
        assert is_finance_worksheet_title("2026-08-10_forecast.json")
        assert is_finance_worksheet_title("2026-08-10_fndr_cashflow-worksheet.md")
        assert is_finance_worksheet_title("2026-08-05_fndr_forecast-assist.md")

    def test_title_predicate_does_not_over_match_business_docs(self):
        """A bare keyword substring would silently and PERMANENTLY block real
        documents from the KB, and the store logs only a count -- undiagnosable."""
        assert not is_finance_worksheet_title("2026-forecast-model.xlsx")
        assert not is_finance_worksheet_title("F3E revenue forecast.md")
        assert not is_finance_worksheet_title("LLC-cashflow-worksheet-v3.xlsx")
        assert not is_finance_worksheet_title("OSN cashflow worksheet.xlsx")
        assert not is_finance_worksheet_title("2026-08-10_close-pack.md")
        assert not is_finance_worksheet_title("")

    def test_title_predicate_survives_drive_side_decoration(self):
        """Drive-for-Desktop conflict copies and 'Copy of' prefixes must not
        walk a generated file past the rule."""
        assert is_finance_worksheet_title("2026-08-10_forecast (1).json")
        assert is_finance_worksheet_title("Copy of 2026-08-10_forecast.json")
        assert is_finance_worksheet_title("2026-08-10_forecast-2.json")
        assert is_finance_worksheet_title("2026-08-10_final-W2.json")
        assert is_finance_worksheet_title("2026-08-10_prelim-W1.json")

    def test_title_predicate_matches_full_title_not_only_basename(self):
        """A Drive display name may itself contain '/' (a date like 8/11);
        path-splitting it would drop the token we are looking for."""
        assert is_finance_worksheet_title("2026-08-10_fndr_cashflow-worksheet 8/11.md")

    def test_non_dated_ledger_files_are_caught(self):
        assert is_finance_worksheet_title("outlook-founder.json")
        assert is_finance_worksheet_title("ledger.json")

    def test_predicates_are_wired_at_the_store_chokepoint(self):
        """One chokepoint covers every connector; assert the wiring survives."""
        source = (_REPO_ROOT / "src" / "cora" / "knowledge_base"
                  / "store.py").read_text(encoding="utf-8")
        assert "is_finance_worksheet_path(doc.source_id)" in source
        assert "is_finance_worksheet_path(meta_path)" in source
        assert "is_finance_worksheet_title(doc.title)" in source


class TestJsonMimeInvariantCoversTheLedger:
    """The mirror is .json. It is excluded twice over: by the segment/title rules
    above and by drive_sweep never requesting application/json. This pins the
    second belt for THIS store, so adding json to the allow-list fails here and
    names the ledger, not only the A5 bank snapshot."""

    def test_json_is_not_in_the_text_mime_allowlist(self):
        from cora.connectors import drive_sweep
        assert "application/json" not in drive_sweep._TEXT_MIME_TYPES
        assert not "application/json".startswith("text/")

    def test_json_extracts_to_empty(self, monkeypatch):
        from cora.connectors import drive_sweep

        class _Media:
            def get_media(self, fileId):  # noqa: N803 - google api kwarg
                return object()

        class _Service:
            def files(self):
                return _Media()

        monkeypatch.setattr(drive_sweep, "_retry_execute",
                            lambda req: b'{"tabs": {"CF_LEXCORP": {}}}')
        text = drive_sweep._download_and_extract(
            _Service(), {"id": "f1", "mimeType": "application/json"})
        assert text == "", (
            "application/json must extract to empty -- a war-chest / portfolio "
            "cash figure must never become a KB chunk"
        )

    def test_the_local_store_is_outside_the_static_md_tree(self):
        """data/state/ is Cora's working state, not a KB-ingested tree."""
        assert "state" in cl.FORECAST_SNAPSHOT_DIR.parts
        assert "_brain" not in cl.FORECAST_SNAPSHOT_DIR.parts


# ── missed-run detection ────────────────────────────────────────────────────

def _load_health():
    spec = importlib.util.spec_from_file_location(
        "nightly_health_check", _REPO_ROOT / "scripts" / "nightly_health_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


health = _load_health()


class TestSnapshotFreshnessCheck:
    MON = datetime.date(2026, 8, 10)
    TUE = datetime.date(2026, 8, 11)
    FRI = datetime.date(2026, 8, 14)

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cl, "FORECAST_SNAPSHOT_DIR",
                            tmp_path / "forecast-snapshots")

    def _bank(self, *dates: str, covered: int = 19, expected: int = 19):
        import json
        cl.FORECAST_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        for d in dates:
            (cl.FORECAST_SNAPSHOT_DIR / f"{d}_forecast.json").write_text(
                json.dumps({"snapshot_date": d, "covered": covered,
                            "expected": expected}))

    def test_never_run_warns(self):
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "warn"
        assert "lost permanently" in r.detail

    def test_this_weeks_snapshot_present(self):
        self._bank("2026-08-10")
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "ok"
        assert "19/19" in r.detail

    def test_missed_monday_warns(self):
        self._bank("2026-08-03")
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "warn"
        assert "2026-08-10" in r.detail and "2026-08-03" in r.detail

    def test_monday_warns_while_recovery_is_still_possible(self):
        """The check runs ONCE daily at 08:45, against a job that fired 06:15 --
        the Monday outcome is already final and the sheet refresh lands later
        that day. Staying silent until Tuesday means every miss is reported only
        once it is permanently unrecoverable."""
        self._bank("2026-08-03")
        r = health.check_cashflow_forecast_snapshot(today=self.MON)
        assert r.status == "warn"
        assert "still time to run it by hand" in r.detail

    def test_hollow_snapshot_is_not_green(self):
        """A dated FILE is not evidence of a banked week."""
        cl.FORECAST_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (cl.FORECAST_SNAPSHOT_DIR / "2026-08-10_forecast.json").write_text("{}")
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "warn"
        assert "coverage could not be read" in r.detail

    def test_partial_coverage_warns(self):
        self._bank("2026-08-10", covered=3, expected=19)
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "warn"
        assert "3 of 19" in r.detail

    def test_a_stray_future_file_does_not_blind_the_check(self):
        """One typo'd --date used to mask a dead job for months."""
        self._bank("2026-12-28")
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "warn"

    def test_still_warns_later_in_the_week(self):
        self._bank("2026-08-03")
        assert health.check_cashflow_forecast_snapshot(today=self.FRI).status == "warn"

    def test_store_read_failure_is_a_warn_not_a_crash(self, monkeypatch):
        def boom():
            raise OSError("disk gone")

        monkeypatch.setattr(cl, "list_snapshot_dates", boom)
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "warn"

    def test_check_is_registered_in_the_run(self):
        source = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(
            encoding="utf-8")
        assert "all_results.append(check_cashflow_forecast_snapshot())" in source
