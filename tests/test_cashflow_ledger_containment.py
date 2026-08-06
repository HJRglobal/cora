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
    KB_EXCLUDED_FOLDER_IDS,
    folder_ids_excluded,
    is_excluded_folder,
    is_finance_worksheet_path,
    is_finance_worksheet_title,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 01-HJR-Global/accounting/cashflow-ledger -- the 13WCF shadow-ledger mirror.
#: Verified live 2026-08-05 to resolve to "cashflow-ledger" under
#: accounting <- 01-HJR-Global <- HJR-Founder-OS.
CASHFLOW_LEDGER_FOLDER = "1aDnmz3oY7QZxsH7mv7_ZDu7cUyDWLhy7"


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

    def test_the_names_the_code_ACTUALLY_generates_are_caught(self):
        """Pinned on the MECHANISM, not on a hand-copied string.

        The M2 build named its files `<week>_prelim.json` / `<week>_final.json`,
        which matched NEITHER the `prelim-w\\d` shape this rule was written for nor
        the `actuals` keyword -- so the belt silently did not cover the new files,
        which is the "guard simply never fires" class. Deriving the names from
        cashflow_actuals means a future rename breaks this test instead of the
        boundary. Loosening the rule to a bare "final" was the wrong fix: it would
        over-match real business documents.
        """
        import datetime

        from cora import cashflow_actuals as ca

        week = datetime.date(2026, 8, 7)
        for kind in (ca.WINDOW_PRELIMINARY, ca.WINDOW_FINALIZED):
            name = ca.actuals_filename(week, kind)
            assert is_finance_worksheet_title(name), name
            assert is_finance_worksheet_path(
                f"01-HJR-Global/accounting/cashflow-ledger/actuals/{name}"), name

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


class TestLedgerFolderIsPinnedOnTheDriveSweepPath:
    """The `carried finding` from the M1 cascade report, now closed.

    Until the first mirror run created the folder there was no id to pin, so the
    drive_sweep door rested entirely on `is_finance_worksheet_title` -- a
    FILENAME heuristic carrying the whole boundary. These pin the id AND the two
    mechanisms that make it load-bearing, so the coverage cannot silently revert
    to title-matching.
    """

    def test_the_folder_id_is_pinned(self):
        assert CASHFLOW_LEDGER_FOLDER in KB_EXCLUDED_FOLDER_IDS
        assert is_excluded_folder(CASHFLOW_LEDGER_FOLDER)

    def test_a_file_parented_directly_in_it_is_excluded(self):
        assert folder_ids_excluded([CASHFLOW_LEDGER_FOLDER])
        assert folder_ids_excluded(["someOtherFolder", CASHFLOW_LEDGER_FOLDER])
        assert not folder_ids_excluded(["someOtherFolder"])
        assert not folder_ids_excluded(None)

    def test_the_founders_os_walk_prunes_the_whole_subtree(self):
        """The snapshots live in a CHILD folder (forecast-snapshots/), so the
        parent id only covers them because the BFS skips a folder AND never
        enqueues its subfolders. M2-M4 add actuals/, worksheets/, candidates/
        and outlook-entities/ -- all covered by the same prune, which is why one
        parent id is enough and no child ids need pinning."""
        source = (_REPO_ROOT / "src" / "cora" / "connectors"
                  / "drive_sweep.py").read_text(encoding="utf-8")
        assert "skip_folder_ids=KB_EXCLUDED_FOLDER_IDS" in source
        assert "current_id in skip_folder_ids" in source
        assert 'subfolder["id"] not in skip_folder_ids' in source

    def test_the_flat_sweep_expansion_reaches_nested_subfolders(self):
        """The per-user sweep has no tree context, so it relies on the roots
        being expanded to their descendants. Fake tree: the ledger folder has a
        forecast-snapshots child, exactly like the live one."""
        from cora.connectors import drive_sweep

        tree = {CASHFLOW_LEDGER_FOLDER: [{"id": "forecast-snapshots-child"}]}

        class _Req:
            def __init__(self, fid):
                self._fid = fid

            def execute(self):
                return {"files": tree.get(self._fid, [])}

        class _Files:
            def list(self, *, q, **k):
                return _Req(q.split("'")[1])

        class _Service:
            def files(self):
                return _Files()

        expanded, complete = drive_sweep._expanded_excluded_folder_ids(_Service())
        assert complete is True
        assert CASHFLOW_LEDGER_FOLDER in expanded
        assert "forecast-snapshots-child" in expanded
        assert folder_ids_excluded(["forecast-snapshots-child"], expanded)

    def test_the_title_rule_is_now_a_belt_not_the_boundary(self):
        """Both doors must hold independently -- if pinning the folder ever gets
        reverted, the title rule alone should still catch the mirror files."""
        assert is_finance_worksheet_title("2026-08-10_forecast.json")
        assert is_finance_worksheet_path(
            "01-HJR-Global/accounting/cashflow-ledger/forecast-snapshots/x.json")


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


class TestActualsFreshnessCheck:
    """M2's sibling monitor. Same D-127c mechanics -- payload coverage, not
    filenames -- but deliberately calmer: these windows ARE recoverable."""

    MON = datetime.date(2026, 8, 10)
    TUE = datetime.date(2026, 8, 11)

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        from cora import cashflow_actuals as ca
        monkeypatch.setattr(ca, "ACTUALS_DIR", tmp_path / "actuals")

    def _bank(self, *weeks: str, covered: int = 8, expected: int = 8,
              awaiting: list | None = None):
        import json

        from cora import cashflow_actuals as ca
        ca.ACTUALS_DIR.mkdir(parents=True, exist_ok=True)
        for week in weeks:
            (ca.ACTUALS_DIR / f"{week}_final-actuals.json").write_text(
                json.dumps({"week_ending": week, "window_kind": "finalized",
                            "covered": covered, "expected": expected,
                            "awaiting_map_confirmation": awaiting or ["LEX"]}))

    def test_never_run_warns_but_says_it_is_recoverable(self):
        r = health.check_cashflow_actuals(today=self.TUE)
        assert r.status == "warn"
        assert "re-readable" in r.detail

    def test_current_finalized_window_is_ok(self):
        """The finalized window trails by two weeks BY DESIGN, so a two-week-old
        week-ending is the healthy state, not a miss."""
        self._bank("2026-07-31")
        r = health.check_cashflow_actuals(today=self.TUE)
        assert r.status == "ok"
        assert "8/8" in r.detail
        # The expected gap is visible as itself, not as missing coverage.
        assert "1 awaiting map confirmation" in r.detail

    def test_more_than_two_weeks_behind_warns(self):
        self._bank("2026-07-10")
        r = health.check_cashflow_actuals(today=self.TUE)
        assert r.status == "warn"
        assert "--date" in r.detail

    def test_hollow_window_is_not_green(self):
        from cora import cashflow_actuals as ca
        ca.ACTUALS_DIR.mkdir(parents=True, exist_ok=True)
        (ca.ACTUALS_DIR / "2026-07-31_final-actuals.json").write_text("{}")
        r = health.check_cashflow_actuals(today=self.TUE)
        assert r.status == "warn"
        assert "coverage could not be read" in r.detail

    def test_partial_coverage_warns_and_names_the_expected_gap(self):
        self._bank("2026-07-31", covered=3, expected=8)
        r = health.check_cashflow_actuals(today=self.TUE)
        assert r.status == "warn"
        assert "3 of 8" in r.detail

    def test_a_stray_future_window_does_not_blind_the_check(self):
        self._bank("2026-12-25")
        assert health.check_cashflow_actuals(today=self.TUE).status == "warn"

    def test_a_preliminary_window_does_not_satisfy_the_check(self):
        """Accuracy binds to matured weeks; a preliminary file is not evidence
        that the finalized re-pull ever happened."""
        import json

        from cora import cashflow_actuals as ca
        ca.ACTUALS_DIR.mkdir(parents=True, exist_ok=True)
        (ca.ACTUALS_DIR / "2026-07-31_prelim-actuals.json").write_text(
            json.dumps({"week_ending": "2026-07-31", "covered": 8, "expected": 9}))
        assert health.check_cashflow_actuals(today=self.TUE).status == "warn"

    def test_store_read_failure_is_a_warn_not_a_crash(self, monkeypatch):
        from cora import cashflow_actuals as ca

        def boom():
            raise OSError("disk gone")
        monkeypatch.setattr(ca, "list_finalized_weeks", boom)
        assert health.check_cashflow_actuals(today=self.TUE).status == "warn"

    def test_check_is_registered_in_the_run(self):
        source = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(
            encoding="utf-8")
        assert "all_results.append(check_cashflow_actuals())" in source

    def test_urgency_is_lower_than_the_snapshot_check(self):
        """Warning at the same pitch as an unrecoverable loss is how a reader
        learns to skip both. The snapshot check says 'lost permanently'; this one
        must not."""
        self._bank("2026-07-10")
        detail = health.check_cashflow_actuals(today=self.TUE).detail
        assert "lost permanently" not in detail
        assert "permanently" not in detail
