"""Snapshot script CLI (13WCF M1/S3).

Every test is offline: the Sheets service and drive_io are stubbed. Nothing here
may reach the network, the real store, or the G: mount.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    """Import the script by path -- scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "run_cashflow_forecast_snapshot",
        _REPO_ROOT / "scripts" / "run_cashflow_forecast_snapshot.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


script = _load_script()

from cora import cashflow_ledger as cl  # noqa: E402
from cora.connectors import gsheets_financials as gf  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    store = tmp_path / "cashflow-ledger"
    monkeypatch.setattr(cl, "STORE_DIR", store)
    monkeypatch.setattr(cl, "FORECAST_SNAPSHOT_DIR", store / "forecast-snapshots")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
    monkeypatch.setattr(gf, "build_sheets_service", lambda: object())
    # Any Drive touch in a test is a bug -- make it loud.
    monkeypatch.setattr(script.drive_io, "exists",
                        lambda *a, **k: pytest.fail("drive_io.exists called"))
    return store


def _vector(tab: str, *, last_actual="2026-07-31") -> gf.ForecastVector:
    return gf.ForecastVector(
        tab=tab,
        status="ok",
        week_ending_weekday="Friday",
        series={"ending_cash": [
            gf.WeekPoint("7-31", "2026-07-31", 100.0, 100.0, 0.0, gf.BASIS_POST_CLOSE),
            gf.WeekPoint("8-7", "2026-08-07", 4321.0, None, None, gf.BASIS_FORECAST),
        ]},
        last_actual_week_ending=last_actual,
        forward_week_endings=["2026-08-07"],
        triplet_checked=42,
    )


def _stub_reads(monkeypatch, fn=None):
    monkeypatch.setattr(
        gf, "get_forecast_vector",
        fn or (lambda tab, **kw: _vector(tab)),
    )


# ── rendering ───────────────────────────────────────────────────────────────

class TestRender:
    def _snap(self, **kw):
        return cl.build_snapshot(
            ["CF_LLC"], read_vector=lambda t: _vector(t),
            today=datetime.date(2026, 8, 10), **kw,
        )

    def test_dry_run_is_ascii_only(self):
        """D-016/D-119: this text is read in a cp1252 console."""
        text = script.render_dry_run(self._snap())
        assert text.isascii(), [c for c in text if not c.isascii()]

    def test_names_coverage_basis_and_derived_weekday(self):
        text = script.render_dry_run(self._snap())
        assert "1 of 1 tabs" in text
        assert "Friday (derived from the sheet)" in text
        assert cl.SNAPSHOT_BASIS in text

    def test_states_the_d121_trap(self):
        text = script.render_dry_run(self._snap())
        assert "post_close_column_value" in text
        assert "UNKNOWN is never zero" in text

    def test_unreadable_tabs_are_surfaced(self):
        def read(tab):
            if tab == "CF_UFL":
                raise RuntimeError("HTTP 403")
            return _vector(tab)

        snap = cl.build_snapshot(["CF_LLC", "CF_UFL"], read_vector=read,
                                 today=datetime.date(2026, 8, 10))
        text = script.render_dry_run(snap)
        assert "UNREADABLE (1)" in text and "HTTP 403" in text
        assert "not counted as covered" in text

    def test_post_refresh_note_only_when_suspect(self):
        clean = script.render_dry_run(self._snap())
        assert "All tabs read PRE-REFRESH" in clean

        suspect = cl.build_snapshot(
            ["CF_LLC"], read_vector=lambda t: _vector(t, last_actual="2026-08-07"),
            today=datetime.date(2026, 8, 10),
        )
        text = script.render_dry_run(suspect)
        assert "POST-REFRESH SUSPECT" in text
        assert "excluded from forecast-accuracy math" in text

    def test_no_prior_snapshot_is_not_rendered_as_a_refresh_signal(self):
        """'no_prior_snapshot' is absence of evidence -- it must not appear in
        the roll-state reason column as though the sheet had been refreshed."""
        snap = self._snap()
        assert snap["tabs"]["CF_LLC"]["roll_signals"] == ["no_prior_snapshot"]
        assert "no_prior_snapshot" not in script.render_dry_run(snap)

    def test_money_formatting(self):
        assert script._fmt(None) == "UNKNOWN"
        assert script._fmt(0.0) == "$0"
        assert script._fmt(-0.4) == "$0"          # never "$-0"
        assert script._fmt(-1756.0) == "-$1,756"
        assert script._fmt(1765138.0) == "$1,765,138"

    def test_week0_forecast_extraction(self):
        block = self._snap()["tabs"]["CF_LLC"]
        assert script._week0_forecast(block) == 4321.0

    def test_week0_forecast_unknown_when_no_forward_weeks(self):
        assert script._week0_forecast({"forward_week_endings": []}) is None


# ── CLI behaviour ───────────────────────────────────────────────────────────

class TestMain:
    def test_dry_run_writes_nothing(self, monkeypatch, capsys):
        _stub_reads(monkeypatch)
        assert script.main(["--dry-run", "--date", "2026-08-10"]) == 0
        assert "nothing written" in capsys.readouterr().out
        assert cl.list_snapshot_dates() == []

    def test_full_run_writes_the_snapshot(self, monkeypatch):
        _stub_reads(monkeypatch)
        assert script.main(["--no-mirror", "--date", "2026-08-10"]) == 0
        assert cl.latest_snapshot_date() == datetime.date(2026, 8, 10)
        snap = cl.load_snapshot(datetime.date(2026, 8, 10))
        assert snap["covered"] == snap["expected"] == len(cl.sweepable_tabs())

    def test_narrowed_run_refuses_to_overwrite(self, monkeypatch):
        """The tabs it never read would look unreadable to every consumer."""
        _stub_reads(monkeypatch)
        assert script.main(["--tabs", "CF_LLC", "--no-mirror",
                            "--date", "2026-08-10"]) == 2
        assert cl.list_snapshot_dates() == []

    def test_narrowed_dry_run_is_allowed(self, monkeypatch, capsys):
        _stub_reads(monkeypatch)
        assert script.main(["--tabs", "CF_LLC", "--dry-run",
                            "--date", "2026-08-10"]) == 0
        assert "1 of " in capsys.readouterr().out

    def test_excluded_tab_is_refused_even_when_asked_for(self, monkeypatch):
        seen: list[str] = []

        def read(tab, **kw):
            seen.append(tab)
            return _vector(tab)

        _stub_reads(monkeypatch, read)
        script.main(["--tabs", "CF_LLC,CF_HR LLC", "--dry-run", "--date", "2026-08-10"])
        assert "CF_HR LLC" not in seen

    def test_only_excluded_tabs_requested_exits_2(self, monkeypatch):
        _stub_reads(monkeypatch)
        assert script.main(["--tabs", "CF_HR LLC", "--date", "2026-08-10"]) == 2

    def test_unreadable_tab_still_banks_the_week_exit_1(self, monkeypatch):
        """A Monday that goes unsnapshotted is history lost forever -- one bad
        tab must never cost the whole week."""
        def read(tab, **kw):
            if tab == "CF_UFL":
                raise RuntimeError("boom")
            return _vector(tab)

        _stub_reads(monkeypatch, read)
        assert script.main(["--no-mirror", "--date", "2026-08-10"]) == 1
        snap = cl.load_snapshot(datetime.date(2026, 8, 10))
        assert "CF_UFL" in snap["unreadable_tabs"]
        assert snap["covered"] == len(cl.sweepable_tabs()) - 1

    def test_auth_failure_leaves_the_previous_snapshot_alone(self, monkeypatch):
        def boom():
            raise RuntimeError("unauthorized_client")

        monkeypatch.setattr(gf, "build_sheets_service", boom)
        assert script.main(["--date", "2026-08-10"]) == 2
        assert cl.list_snapshot_dates() == []

    def test_structural_failure_leaves_the_previous_snapshot_alone(self, monkeypatch):
        def read(tab, **kw):
            wd = "Thursday" if tab == "CF_UFL" else "Friday"
            v = _vector(tab)
            v.week_ending_weekday = wd
            return v

        _stub_reads(monkeypatch, read)
        cl.write_snapshot(cl.build_snapshot(
            ["CF_LLC"], read_vector=lambda t: _vector(t),
            today=datetime.date(2026, 8, 3),
        ))
        assert script.main(["--date", "2026-08-10"]) == 2
        assert cl.list_snapshot_dates() == [datetime.date(2026, 8, 3)]

    def test_bad_date_arg(self, monkeypatch):
        _stub_reads(monkeypatch)
        assert script.main(["--date", "not-a-date"]) == 2

    def test_prior_snapshot_feeds_roll_detection(self, monkeypatch):
        _stub_reads(monkeypatch, lambda tab, **kw: _vector(tab, last_actual="2026-07-31"))
        script.main(["--no-mirror", "--date", "2026-08-03"])
        _stub_reads(monkeypatch, lambda tab, **kw: _vector(tab, last_actual="2026-08-07"))
        script.main(["--no-mirror", "--date", "2026-08-10"])
        snap = cl.load_snapshot(datetime.date(2026, 8, 10))
        assert snap["prior_snapshot_date"] == "2026-08-03"
        block = snap["tabs"]["CF_LLC"]
        assert block["post_refresh_suspect"] is True
        assert "last_actual_advanced_since_prior" in block["roll_signals"]


# ── Drive mirror ────────────────────────────────────────────────────────────

class TestMirror:
    def test_mount_outage_does_not_kill_the_local_write(self, monkeypatch):
        _stub_reads(monkeypatch)
        monkeypatch.setattr(script.drive_io, "exists", lambda *a, **k: False)

        def boom(*a, **k):
            raise script.drive_io.DriveUnavailable("G: gone")

        monkeypatch.setattr(script.drive_io, "write_text_atomic", boom)
        assert script.main(["--date", "2026-08-10"]) == 0
        assert cl.latest_snapshot_date() == datetime.date(2026, 8, 10)

    def test_unchanged_payload_skips_the_write(self, monkeypatch):
        _stub_reads(monkeypatch)
        writes: list = []
        snap = cl.build_snapshot(["CF_LLC"], read_vector=lambda t: _vector(t),
                                 today=datetime.date(2026, 8, 10))
        import json
        existing = json.dumps({**snap, "generated_at_utc": "earlier"},
                              indent=2, sort_keys=True)
        monkeypatch.setattr(script.drive_io, "exists", lambda *a, **k: True)
        monkeypatch.setattr(script.drive_io, "read_text", lambda *a, **k: existing)
        monkeypatch.setattr(script.drive_io, "write_text_atomic",
                            lambda *a, **k: writes.append(a))
        script._mirror(snap)
        assert writes == []

    def test_changed_payload_writes(self, monkeypatch):
        writes: list = []
        snap = cl.build_snapshot(["CF_LLC"], read_vector=lambda t: _vector(t),
                                 today=datetime.date(2026, 8, 10))
        monkeypatch.setattr(script.drive_io, "exists", lambda *a, **k: True)
        monkeypatch.setattr(script.drive_io, "read_text", lambda *a, **k: '{"covered": 99}')
        monkeypatch.setattr(script.drive_io, "write_text_atomic",
                            lambda *a, **k: writes.append(a))
        script._mirror(snap)
        assert len(writes) == 1
        assert "2026-08-10_forecast.json" in str(writes[0][0])
