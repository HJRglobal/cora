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
        """A bare "forecast" token would swallow real business documents."""
        assert not is_finance_worksheet_title("2026-forecast-model.xlsx")
        assert not is_finance_worksheet_title("F3E revenue forecast.md")
        assert not is_finance_worksheet_title("2026-08-10_close-pack.md")
        assert not is_finance_worksheet_title("")

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

    def _bank(self, *dates: str):
        cl.FORECAST_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        for d in dates:
            (cl.FORECAST_SNAPSHOT_DIR / f"{d}_forecast.json").write_text("{}")

    def test_never_run_warns(self):
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "warn"
        assert "lost permanently" in r.detail

    def test_this_weeks_snapshot_present(self):
        self._bank("2026-08-10")
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "ok"

    def test_missed_monday_warns_from_tuesday(self):
        self._bank("2026-08-03")
        r = health.check_cashflow_forecast_snapshot(today=self.TUE)
        assert r.status == "warn"
        assert "2026-08-10" in r.detail and "2026-08-03" in r.detail

    def test_monday_itself_is_silent(self):
        """08:45 health check vs a 06:10 job -- a same-day WARN would fire on
        any week the task merely runs late."""
        self._bank("2026-08-03")
        r = health.check_cashflow_forecast_snapshot(today=self.MON)
        assert r.status == "ok"
        assert "due 06:10" in r.detail

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
