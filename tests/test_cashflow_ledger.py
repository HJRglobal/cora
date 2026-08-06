"""Shadow-ledger snapshot store (13WCF M1/S2)."""

from __future__ import annotations

import datetime
import json

import pytest

from cora import cashflow_ledger as cl
from cora.connectors import gsheets_financials as gf


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Never touch the real store or the Drive mount from a test."""
    store = tmp_path / "cashflow-ledger"
    monkeypatch.setattr(cl, "STORE_DIR", store)
    monkeypatch.setattr(cl, "FORECAST_SNAPSHOT_DIR", store / "forecast-snapshots")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
    return store


def _vector(
    tab="CF_TEST",
    *,
    last_actual="2026-07-31",
    forward=("2026-08-07", "2026-08-14"),
    weekday="Friday",
    status="ok",
) -> gf.ForecastVector:
    if status != "ok":
        return gf.ForecastVector(tab=tab, status=status, unknown_reason="bad grid")
    points = [
        gf.WeekPoint("7-31", "2026-07-31", 100.0, 100.0, 0.0, gf.BASIS_POST_CLOSE),
    ] + [
        gf.WeekPoint(w[5:].lstrip("0").replace("-0", "-"), w, 200.0, None, None,
                     gf.BASIS_FORECAST)
        for w in forward
    ]
    return gf.ForecastVector(
        tab=tab,
        status="ok",
        week_ending_weekday=weekday,
        series={"ending_cash": points},
        last_actual_week_ending=last_actual,
        forward_week_endings=list(forward),
        triplet_checked=1,
    )


# ── week-ending arithmetic ──────────────────────────────────────────────────

class TestLastCompletedWeekEnding:
    def test_midweek(self):
        # Wed 2026-08-05 -> the most recent completed Friday is 7-31.
        assert cl.last_completed_week_ending(
            "Friday", datetime.date(2026, 8, 5)
        ) == datetime.date(2026, 7, 31)

    def test_on_the_week_ending_day_itself(self):
        """Friday is not COMPLETE until it is over -- go back a full week."""
        assert cl.last_completed_week_ending(
            "Friday", datetime.date(2026, 8, 7)
        ) == datetime.date(2026, 7, 31)

    def test_the_monday_after(self):
        assert cl.last_completed_week_ending(
            "Friday", datetime.date(2026, 8, 10)
        ) == datetime.date(2026, 8, 7)

    def test_derives_from_the_given_weekday_not_friday(self):
        assert cl.last_completed_week_ending(
            "Thursday", datetime.date(2026, 8, 5)
        ) == datetime.date(2026, 7, 30)

    def test_garbage_weekday(self):
        assert cl.last_completed_week_ending("Blursday", datetime.date(2026, 8, 5)) is None


# ── roll state ──────────────────────────────────────────────────────────────

class TestClassifyRollState:
    MON = datetime.date(2026, 8, 10)          # Monday after the 8-7 week closed

    def test_clean_pre_refresh_monday(self):
        """Boundary still at 7-31 on Monday 8/10 -> the refresh has not run."""
        r = cl.classify_roll_state(
            _vector(last_actual="2026-07-31"),
            {"last_actual_week_ending": "2026-07-31",
             "forward_week_endings": ["2026-08-07", "2026-08-14"]},
            today=self.MON,
        )
        assert r["post_refresh_suspect"] is False
        assert r["roll_signals"] == []
        assert r["expected_pre_refresh_boundary"] == "2026-08-07"

    def test_absolute_signal_fires_with_no_history(self):
        """The first snapshot is taken mid-week at merge and IS post-refresh.
        A relative-only rule would leave it unstamped and wrongly trusted."""
        r = cl.classify_roll_state(
            _vector(last_actual="2026-07-31"), None, today=datetime.date(2026, 8, 5)
        )
        assert r["post_refresh_suspect"] is True
        assert "actuals_for_last_completed_week_present" in r["roll_signals"]
        assert "no_prior_snapshot" in r["roll_signals"]

    def test_no_prior_alone_is_not_suspect(self):
        """Absence of evidence is not evidence of a refresh."""
        r = cl.classify_roll_state(
            _vector(last_actual="2026-07-31"), None, today=self.MON
        )
        assert r["roll_signals"] == ["no_prior_snapshot"]
        assert r["post_refresh_suspect"] is False

    def test_actual_boundary_advanced_since_prior(self):
        r = cl.classify_roll_state(
            _vector(last_actual="2026-08-07"),
            {"last_actual_week_ending": "2026-07-31",
             "forward_week_endings": ["2026-08-07", "2026-08-14"]},
            today=self.MON,
        )
        assert r["post_refresh_suspect"] is True
        assert "last_actual_advanced_since_prior" in r["roll_signals"]

    def test_window_rolled_since_prior(self):
        r = cl.classify_roll_state(
            _vector(last_actual="2026-07-31", forward=("2026-08-07", "2026-08-21")),
            {"last_actual_week_ending": "2026-07-31",
             "forward_week_endings": ["2026-08-07", "2026-08-14"]},
            today=self.MON,
        )
        assert r["post_refresh_suspect"] is True
        assert "week_grid_rolled_since_prior" in r["roll_signals"]


# ── snapshot build ──────────────────────────────────────────────────────────

class TestBuildSnapshot:
    TODAY = datetime.date(2026, 8, 10)

    def test_happy_path_coverage_and_shape(self):
        snap = cl.build_snapshot(
            ["CF_LLC", "CF_UFL"],
            read_vector=lambda t: _vector(t),
            today=self.TODAY,
        )
        assert snap["covered"] == 2 and snap["expected"] == 2
        assert snap["week_ending_weekday"] == "Friday"
        assert snap["schema_version"] == cl.SCHEMA_VERSION
        assert set(snap["tabs"]) == {"CF_LLC", "CF_UFL"}
        assert snap["tabs"]["CF_LLC"]["entity_codes"] == ["LEX-LLC"]
        json.dumps(snap)  # must be serialisable

    def test_unknown_tab_is_not_counted_as_covered(self):
        """D-117: a tab we could not read must never read as a flat one."""
        def read(tab):
            return _vector(tab, status="unknown") if tab == "CF_UFL" else _vector(tab)

        snap = cl.build_snapshot(
            ["CF_LLC", "CF_UFL"], read_vector=read, today=self.TODAY
        )
        assert snap["covered"] == 1 and snap["expected"] == 2
        assert "CF_UFL" in snap["unreadable_tabs"]
        assert "CF_UFL" not in snap["tabs"]

    def test_one_raising_tab_does_not_lose_the_week(self):
        def read(tab):
            if tab == "CF_UFL":
                raise RuntimeError("HTTP 500")
            return _vector(tab)

        snap = cl.build_snapshot(
            ["CF_LLC", "CF_UFL"], read_vector=read, today=self.TODAY
        )
        assert snap["covered"] == 1
        assert "HTTP 500" in snap["unreadable_tabs"]["CF_UFL"]

    def test_excluded_tab_is_never_collected(self):
        """CF_HR LLC is personal books and the mirror is a shared folder."""
        seen: list[str] = []

        def read(tab):
            seen.append(tab)
            return _vector(tab)

        snap = cl.build_snapshot(
            ["CF_LLC", "CF_HR LLC"], read_vector=read, today=self.TODAY
        )
        assert seen == ["CF_LLC"]
        assert "CF_HR LLC" not in snap["tabs"]
        assert snap["expected"] == 1

    def test_disagreeing_weekdays_refuse(self):
        def read(tab):
            return _vector(tab, weekday="Thursday" if tab == "CF_UFL" else "Friday")

        with pytest.raises(cl.LedgerError, match="week-ending weekday"):
            cl.build_snapshot(["CF_LLC", "CF_UFL"], read_vector=read, today=self.TODAY)

    def test_roll_state_is_stamped_per_tab(self):
        prior = {
            "snapshot_date": "2026-08-03",
            "tabs": {
                "CF_LLC": {"last_actual_week_ending": "2026-07-31",
                           "forward_week_endings": ["2026-08-07", "2026-08-14"]},
                "CF_UFL": {"last_actual_week_ending": "2026-07-31",
                           "forward_week_endings": ["2026-08-07", "2026-08-14"]},
            },
        }

        def read(tab):
            return _vector(tab, last_actual="2026-08-07" if tab == "CF_UFL" else "2026-07-31")

        snap = cl.build_snapshot(
            ["CF_LLC", "CF_UFL"], read_vector=read, today=self.TODAY, prior=prior
        )
        assert snap["tabs"]["CF_LLC"]["post_refresh_suspect"] is False
        assert snap["tabs"]["CF_UFL"]["post_refresh_suspect"] is True
        assert snap["prior_snapshot_date"] == "2026-08-03"

    def test_narrowed_run_is_marked_partial(self):
        snap = cl.build_snapshot(
            ["CF_LLC"],
            read_vector=lambda t: _vector(t),
            today=self.TODAY,
            full_scope=["CF_LLC", "CF_UFL"],
        )
        assert snap["partial_sweep"] is True

    def test_basis_note_names_the_d121_trap(self):
        snap = cl.build_snapshot(
            ["CF_LLC"], read_vector=lambda t: _vector(t), today=self.TODAY
        )
        assert snap["basis"] == cl.SNAPSHOT_BASIS
        assert any("post_close_column_value" in n for n in snap["notes"])


# ── store round-trip ────────────────────────────────────────────────────────

class TestStore:
    def test_write_then_load(self):
        snap = cl.build_snapshot(
            ["CF_LLC"],
            read_vector=lambda t: _vector(t),
            today=datetime.date(2026, 8, 10),
        )
        path = cl.write_snapshot(snap)
        assert path.name == "2026-08-10_forecast.json"
        assert cl.load_snapshot(datetime.date(2026, 8, 10))["covered"] == 1
        assert not list(path.parent.glob("*.tmp"))

    def test_prior_snapshot_lookup_is_strictly_older(self):
        for d in ("2026-08-03", "2026-08-10"):
            cl.write_snapshot(cl.build_snapshot(
                ["CF_LLC"], read_vector=lambda t: _vector(t),
                today=datetime.date.fromisoformat(d),
            ))
        prior = cl.load_prior_snapshot(datetime.date(2026, 8, 10))
        assert prior["snapshot_date"] == "2026-08-03"
        assert cl.latest_snapshot_date() == datetime.date(2026, 8, 10)

    def test_no_snapshots_yet(self):
        assert cl.list_snapshot_dates() == []
        assert cl.latest_snapshot_date() is None
        assert cl.load_prior_snapshot(datetime.date(2026, 8, 10)) is None

    def test_unparseable_filename_ignored(self):
        cl.FORECAST_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (cl.FORECAST_SNAPSHOT_DIR / "notadate_forecast.json").write_text("{}")
        assert cl.list_snapshot_dates() == []

    def test_corrupt_snapshot_reads_as_absent_not_a_crash(self):
        cl.FORECAST_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (cl.FORECAST_SNAPSHOT_DIR / "2026-08-10_forecast.json").write_text("{oops")
        assert cl.load_snapshot(datetime.date(2026, 8, 10)) is None

    def test_change_gate_ignores_only_the_timestamp(self):
        a = json.dumps({"generated_at_utc": "T1", "covered": 3})
        b = json.dumps({"generated_at_utc": "T2", "covered": 3})
        c = json.dumps({"generated_at_utc": "T2", "covered": 4})
        assert cl.same_ignoring_stamps(a, b) is True
        assert cl.same_ignoring_stamps(a, c) is False


# ── scope ───────────────────────────────────────────────────────────────────

class TestScope:
    def test_sweepable_tabs_excludes_personal_books(self):
        tabs = cl.sweepable_tabs()
        assert "CF_HR LLC" not in tabs
        assert set(tabs) & cl.EXCLUDED_TABS == set()

    def test_sweepable_tabs_covers_the_live_workbook(self):
        tabs = cl.sweepable_tabs()
        for expected in ("CF_LLC", "CF_LEXCORP", "OSN Consolidated",
                         "CF_SUMMARY", "CF_OSN Core4"):
            assert expected in tabs

    def test_entity_codes_for_tab(self):
        assert "LEX-LLA" in cl.entity_codes_for_tab("CF_LLA_MV")
        assert cl.entity_codes_for_tab("nope") == []

    def test_mirror_path_lands_in_the_accounting_tree(self, tmp_path):
        p = cl.mirror_path(datetime.date(2026, 8, 10))
        assert p.name == "2026-08-10_forecast.json"
        assert "cashflow-ledger" in p.parts and "accounting" in p.parts
