"""Actuals script CLI (13WCF M2/S3).

Every test is offline: QBO and drive_io are stubbed. Nothing here may reach the
network, the real store, or the G: mount.

The pins that earn their keep: the realm allowlist (an exact-string exclusion of
personal books was defeated by casing in M1), the exit codes an operator acts on,
and the invariant that neither the render nor the mirror ever carries raw failure
text or human-typed memo fields into a shared folder.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    """Import the script by path -- scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "run_cashflow_actuals", _REPO_ROOT / "scripts" / "run_cashflow_actuals.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


script = _load_script()

from cora import cashflow_actuals as ca  # noqa: E402
from cora import cashflow_ledger as cl  # noqa: E402
from cora import cashflow_maps as cm  # noqa: E402

THURSDAY = datetime.date(2026, 8, 6)
W1 = datetime.date(2026, 7, 31)
W2 = datetime.date(2026, 7, 24)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    store = tmp_path / "cashflow-ledger"
    monkeypatch.setattr(cl, "STORE_DIR", store)
    monkeypatch.setattr(cl, "FORECAST_SNAPSHOT_DIR", store / "forecast-snapshots")
    monkeypatch.setattr(ca, "ACTUALS_DIR", store / "actuals")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
    # Any Drive touch that is not explicitly under test is a bug -- make it loud.
    monkeypatch.setattr(script.drive_io, "exists",
                        lambda *a, **k: pytest.fail("drive_io.exists called"))
    monkeypatch.setattr(script.qbs, "load_snapshot", lambda *a, **k: None)
    return store


def _snapshot(weekday="Friday"):
    cl.FORECAST_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (cl.FORECAST_SNAPSHOT_DIR / "2026-08-05_forecast.json").write_text(
        json.dumps({"snapshot_date": "2026-08-05",
                    "week_ending_weekday": weekday}), encoding="utf-8")


def _stub_qbo(monkeypatch, *, net=-100.0, fail: set[str] | None = None,
              tie_out_net=None):
    """Patch the qbo_client functions the script hands to build_window."""
    from cora.tools import qbo_client as qc
    fail = fail or set()

    def query_accounts(realm):
        if realm in fail:
            raise RuntimeError("HTTP 503 boom")
        return [{"id": "9", "type": "Bank"}]

    monkeypatch.setattr(qc, "query_accounts", query_accounts)
    monkeypatch.setattr(qc, "general_ledger_bank_rows", lambda *a, **k: {
        "rows": [{"txn_id": "1", "amount": net, "split_account_id": None}],
        "row_count": 1, "opening_balance": 1000.0,
        "identity": {"checked": 1, "worst_residual": 0.0, "failed": []},
        "duplicate_row_keys": 0, "sections": {}})
    monkeypatch.setattr(qc, "bank_side_flow", lambda *a, **k: {
        "net": net if tie_out_net is None else tie_out_net,
        "per_type": {"Purchase": net}, "counts": {"Purchase": 1},
        "types_expected": 9, "internal_transfers": 0.0, "credit_rows": 0,
        "empty_types": [], "unexpected_keys": {}, "errors": {}, "capped_types": []})
    monkeypatch.setattr(qc, "newest_bank_side_txn_date",
                        lambda *a, **k: {"date": "2026-08-04"})


def _stub_maps(monkeypatch, pairs=None, excluded=None):
    emap = cm.EntityMap(
        pairs={k: cm.RealmPairing(realm=k, **v) for k, v in (pairs or {
            "F3E": {"tab": "CF_F3", "confirmed": True},
            "BDM": {"tab": "CF_BigDM", "confirmed": False},
        }).items()},
        excluded_realms=excluded or cm.HARD_EXCLUDED_REALMS,
        manual_entry_tabs=["CF_UFL"], derived_tabs=["CF_SUMMARY"])
    monkeypatch.setattr(script.cm, "load_entity_map", lambda *a, **k: emap)
    monkeypatch.setattr(script.cm, "load_category_map", lambda *a, **k: cm.CategoryMap(
        categories={"receipts": ["Services"]}))
    return emap


def _stub_provisioned(monkeypatch, realms):
    import cora.connectors.qbo_oauth as oauth
    monkeypatch.setattr(oauth, "list_provisioned_entities", lambda: list(realms))


# ── realm allowlist ──────────────────────────────────────────────────────────

class TestResolveRealms:
    def test_default_is_every_provisioned_non_excluded_realm(self, monkeypatch):
        emap = _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E", "BDM", "HRLLC", "OSN"])
        realms, scope = script._resolve_realms("", emap)
        assert realms == ["BDM", "F3E"]
        assert scope == ["BDM", "F3E"]

    def test_case_insensitive_allowlist(self, monkeypatch):
        emap = _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E", "BDM"])
        realms, _ = script._resolve_realms("f3e", emap)
        assert realms == ["F3E"]

    def test_excluded_realm_can_never_be_requested(self, monkeypatch):
        """Personal books stay out however the name is cased -- an ALLOWLIST
        against known scope, never a denylist against someone else's
        normalisation (D-127f)."""
        emap = _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E", "HRLLC"])
        with pytest.raises(SystemExit) as excinfo:
            script._resolve_realms("hrllc", emap)
        assert excinfo.value.code == 2

    def test_unknown_realm_refuses_rather_than_silently_reading_nothing(self, monkeypatch):
        emap = _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E"])
        with pytest.raises(SystemExit):
            script._resolve_realms("NOPE", emap)

    def test_duplicates_collapse(self, monkeypatch):
        emap = _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E"])
        assert script._resolve_realms("F3E,f3e", emap)[0] == ["F3E"]


# ── the dry-run render ───────────────────────────────────────────────────────

def _payload(**overrides):
    block = {
        "status": "ok", "map_confirmed": False, "net_flow": -100.0,
        "receipts": 0.0, "disbursements": -100.0, "closing_bank_balance": 900.0,
        "posted_through": "2026-08-04", "categories": {},
        "uncategorized": {"amount": -100.0, "rows": 1,
                          "reasons": {"multi_line_split": 1}},
        "tie_out": {"status": "ok", "residual": 0.0, "empty_types": [],
                    "unexpected_keys": {}, "capped_types": []},
    }
    block.update(overrides)
    return {
        "window_kind": ca.WINDOW_PRELIMINARY, "week_start": "2026-07-25",
        "week_ending": "2026-07-31", "week_ending_weekday": "Friday",
        "week_source": "forecast snapshot 2026-08-05", "basis": ca.FLOW_BASIS,
        "covered": 1, "expected": 2, "map_confirmed_pairs": 0, "supersedes": None,
        "realms": {"F3E": block}, "excluded_realms": ["HRLLC", "OSN"],
        "manual_entry_tabs": ["CF_UFL"], "derived_tabs": ["CF_SUMMARY"],
        "notes": ["a note"],
    }


class TestRenderDryRun:
    def test_unconfirmed_pairs_are_labelled(self):
        assert "UNCONFIRMED" in script.render_dry_run(_payload())

    def test_confirmed_pair_is_not_labelled_unconfirmed(self):
        out = script.render_dry_run(_payload(map_confirmed=True))
        assert "UNCONFIRMED" not in out

    def test_unknown_is_never_rendered_as_zero(self):
        out = script.render_dry_run(_payload(
            status="error", reason_code="api_server_error", net_flow=None,
            receipts=None, disbursements=None, closing_bank_balance=None))
        assert "UNKNOWN" in out
        assert "api_server_error" in out

    def test_tie_out_failure_is_shouted(self):
        out = script.render_dry_run(_payload(tie_out={
            "status": "failed", "residual": -12.0, "empty_types": [],
            "unexpected_keys": {}, "capped_types": []}))
        assert "TIE-OUT FAILED" in out
        assert "usable for comparison" in out

    def test_unexpected_query_key_is_shouted(self):
        """The $11,950.34 class: a changed response key must not pass quietly."""
        out = script.render_dry_run(_payload(tie_out={
            "status": "ok", "residual": 0.0, "empty_types": [],
            "unexpected_keys": {"Deposit": "DepositV2"}, "capped_types": []}))
        assert "UNEXPECTED QUERY KEY" in out

    def test_page_cap_is_shouted(self):
        out = script.render_dry_run(_payload(tie_out={
            "status": "ok", "residual": 0.0, "empty_types": [],
            "unexpected_keys": {}, "capped_types": ["Purchase"]}))
        assert "PAGE CAP HIT" in out

    def test_refused_realm_names_its_candidate_tabs(self):
        payload = _payload()
        payload["realms"]["LEX"] = {
            "status": "refused", "reason_code": "realm_scope_undeclared",
            "candidate_tabs": ["CF_LLC", "CF_LBHS"]}
        out = script.render_dry_run(payload)
        assert "REFUSED" in out
        assert "scope_attested or filters" in out

    def test_manual_entry_tabs_are_not_presented_as_gaps(self):
        out = script.render_dry_run(_payload())
        assert "no QBO source, not a gap" in out

    def test_internal_transfer_exclusion_is_disclosed(self):
        out = script.render_dry_run(_payload(internal_transfers_excluded=3000.0))
        assert "internal bank-to-bank" in out

    def test_perimeter_and_notes_are_rendered(self):
        payload = _payload()
        payload["notes"] = [ca.PERIMETER_NOTE]
        assert "not a cash event" in script.render_dry_run(payload)

    def test_preliminary_and_finalized_are_distinguishable(self):
        assert "PRELIMINARY" in script.render_dry_run(_payload())
        final = _payload()
        final["window_kind"] = ca.WINDOW_FINALIZED
        final["supersedes"] = "2026-07-31_prelim-actuals.json"
        out = script.render_dry_run(final)
        assert "FINALIZED" in out and "supersedes" in out

    def test_render_survives_a_missing_block(self):
        """The dry run is the only pre-flight gate; it must not be the thing that
        breaks on an unexpected payload shape."""
        payload = _payload()
        payload["realms"]["ODD"] = {"status": "ok"}
        assert "ODD" in script.render_dry_run(payload)


# ── exit codes ───────────────────────────────────────────────────────────────

class TestMain:
    def test_clean_dry_run(self, monkeypatch, capsys):
        _snapshot()
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E", "BDM"])
        _stub_qbo(monkeypatch)
        assert script.main(["--dry-run", "--date", "2026-08-06"]) == 0
        out = capsys.readouterr().out
        assert "PRELIMINARY" in out and "FINALIZED" in out

    def test_dry_run_writes_nothing(self, monkeypatch):
        _snapshot()
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E"])
        _stub_qbo(monkeypatch)
        script.main(["--dry-run", "--date", "2026-08-06"])
        assert not ca.ACTUALS_DIR.exists() or not list(ca.ACTUALS_DIR.glob("*.json"))

    def test_writes_both_windows(self, monkeypatch):
        _snapshot()
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E"])
        _stub_qbo(monkeypatch)
        monkeypatch.setattr(script, "_mirror", lambda payload: None)
        assert script.main(["--date", "2026-08-06"]) == 0
        assert ca.load_window(W1, ca.WINDOW_PRELIMINARY) is not None
        assert ca.load_window(W2, ca.WINDOW_FINALIZED) is not None

    def test_window_filter(self, monkeypatch):
        _snapshot()
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E"])
        _stub_qbo(monkeypatch)
        monkeypatch.setattr(script, "_mirror", lambda payload: None)
        script.main(["--window", "final", "--date", "2026-08-06"])
        assert ca.load_window(W1, ca.WINDOW_PRELIMINARY) is None
        assert ca.load_window(W2, ca.WINDOW_FINALIZED) is not None

    def test_no_snapshot_refuses_rather_than_assuming_friday(self, monkeypatch):
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E"])
        assert script.main(["--dry-run", "--date", "2026-08-06"]) == 2

    def test_unusable_map_refuses(self, monkeypatch):
        _snapshot()
        monkeypatch.setattr(script.cm, "load_entity_map",
                            lambda *a, **k: (_ for _ in ()).throw(
                                cm.MapError("excluded_realms is missing")))
        assert script.main(["--dry-run", "--date", "2026-08-06"]) == 2

    def test_bad_date_refuses(self, monkeypatch):
        assert script.main(["--dry-run", "--date", "not-a-date"]) == 2

    def test_no_sweepable_realms_refuses(self, monkeypatch):
        _snapshot()
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["HRLLC"])
        assert script.main(["--dry-run", "--date", "2026-08-06"]) == 2

    def test_a_failed_realm_degrades_but_still_banks(self, monkeypatch):
        _snapshot()
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E", "BDM"])
        _stub_qbo(monkeypatch, fail={"BDM"})
        monkeypatch.setattr(script, "_mirror", lambda payload: None)
        assert script.main(["--date", "2026-08-06"]) == 1
        payload = ca.load_window(W1, ca.WINDOW_PRELIMINARY)
        assert payload["realms"]["BDM"]["net_flow"] is None
        assert payload["realms"]["F3E"]["net_flow"] == -100.0

    def test_tie_out_failure_degrades_the_exit_code(self, monkeypatch):
        """The figure looks fine, so nothing else in the estate would tell anybody
        it disagrees with the ledger."""
        _snapshot()
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E"])
        _stub_qbo(monkeypatch, tie_out_net=-88.0)
        monkeypatch.setattr(script, "_mirror", lambda payload: None)
        assert script.main(["--date", "2026-08-06"]) == 1

    def test_all_realms_dead_refuses_to_write(self, monkeypatch):
        """D-127c: no monitor may call a total failure green."""
        _snapshot()
        _stub_maps(monkeypatch)
        _stub_provisioned(monkeypatch, ["F3E"])
        _stub_qbo(monkeypatch, fail={"F3E"})
        assert script.main(["--date", "2026-08-06"]) == 2
        assert ca.load_window(W1, ca.WINDOW_PRELIMINARY) is None
        assert ca.load_window(W2, ca.WINDOW_FINALIZED) is None


# ── mirror ───────────────────────────────────────────────────────────────────

class TestMirror:
    def test_change_gated(self, monkeypatch):
        payload = _payload()
        body = json.dumps(payload, indent=2, sort_keys=True)
        writes: list = []
        monkeypatch.setattr(script.drive_io, "exists", lambda *a: True)
        monkeypatch.setattr(script.drive_io, "read_text", lambda *a: body)
        monkeypatch.setattr(script.drive_io, "write_text_atomic",
                            lambda *a: writes.append(a))
        script._mirror(payload)
        assert writes == []

    def test_writes_when_changed(self, monkeypatch):
        payload = _payload()
        writes: list = []
        monkeypatch.setattr(script.drive_io, "exists", lambda *a: True)
        monkeypatch.setattr(script.drive_io, "read_text", lambda *a: '{"x": 1}')
        monkeypatch.setattr(script.drive_io, "write_text_atomic",
                            lambda *a: writes.append(a))
        script._mirror(payload)
        assert len(writes) == 1

    def test_drive_unavailable_is_fail_soft(self, monkeypatch):
        monkeypatch.setattr(script.drive_io, "exists", lambda *a: (
            (_ for _ in ()).throw(script.drive_io.DriveUnavailable("mount gone"))))
        script._mirror(_payload())      # must not raise

    def test_os_error_is_fail_soft(self, monkeypatch):
        monkeypatch.setattr(script.drive_io, "exists", lambda *a: False)
        monkeypatch.setattr(script.drive_io, "write_text_atomic",
                            lambda *a: (_ for _ in ()).throw(OSError("disk")))
        script._mirror(_payload())      # must not raise

    def test_mirror_target_is_the_actuals_subfolder(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(script.drive_io, "exists", lambda *a: False)
        monkeypatch.setattr(script.drive_io, "write_text_atomic",
                            lambda target, body: seen.append(target))
        script._mirror(_payload())
        assert seen[0].parts[-2:] == ("actuals", "2026-07-31_prelim-actuals.json")

    def test_mirrored_body_carries_no_human_typed_text(self, monkeypatch):
        """D-124/D-127g: memo and name fields never reach the payload, so they
        cannot reach a folder Justin and Hayden work in."""
        seen: list = []
        monkeypatch.setattr(script.drive_io, "exists", lambda *a: False)
        monkeypatch.setattr(script.drive_io, "write_text_atomic",
                            lambda target, body: seen.append(body))
        script._mirror(_payload())
        for field in ("memo", "Memo", "Description", "quickbooks.api.intuit.com"):
            assert field not in seen[0]
