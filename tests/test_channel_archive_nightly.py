"""Code #16 C1 (A25) -- nightly_health_check.check_channel_archive: ONE CheckResult,
failing-capable (the demoting fixture WARNs and writes the demotion), honours
--dry-run at its write sites, BLIND without a client (the default refuses under
pytest), and registered in main() with the dry_run kwarg.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import nightly_health_check as hc  # noqa: E402
from _chanarch_fakes import BOT_UID, PERSON  # noqa: E402
from cora.channel_archive import policy  # noqa: E402
from cora.channel_archive import store as st  # noqa: E402
from test_channel_archive_monitor import MonSlack  # noqa: E402


from cora.channel_archive import monitor as mon  # noqa: E402

#: a clock safely inside the lane's lifetime whatever the host clock says
NOWX = max(time.time(), mon.LANE_EPOCH + 5 * 86400)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(mon, "PACE_S", 0.0)


def _demoting():
    return MonSlack(archived={"C0ARCHIVE01": {}},
                    events={"C0ARCHIVE01": [{"ts": f"{NOWX - 3600:.6f}", "subtype": "channel_archive",
                                             "user": BOT_UID}]})


def test_a_clean_ledger_reads_ok_with_coverage():
    fake = MonSlack(archived={"C0HUMAN001": {}},
                    events={"C0HUMAN001": [{"ts": f"{NOWX - 3600:.6f}", "subtype": "channel_archive",
                                            "user": PERSON}]})
    r = hc.check_channel_archive(client_factory=lambda: fake, now=NOWX)
    assert isinstance(r, hc.CheckResult) and r.status == "ok", r.detail
    assert r.name == "Dead-channel archive lane" and "acting T0" in r.detail
    assert "histories read" in r.detail


def test_an_unattributed_archive_warns_and_demotes():
    r = hc.check_channel_archive(client_factory=_demoting, now=NOWX)
    assert r.status == "warn" and "UNATTRIBUTED" in r.detail and "demotion WRITTEN" in r.detail
    assert policy.is_demoted()


def test_dry_run_warns_but_writes_nothing():
    r = hc.check_channel_archive(dry_run=True, client_factory=_demoting, now=NOWX)
    assert r.status == "warn" and "UNATTRIBUTED" in r.detail and "WRITTEN" not in r.detail
    assert not policy.is_demoted() and not st.ledger_path().exists()


def test_dry_run_never_claims_the_lane_is_demoted():
    """D-051 r1 c1-monitor#5: under --dry-run nothing is written, so the line must not
    say DEMOTED -- it says what a real run would do and that nothing was written."""
    r = hc.check_channel_archive(dry_run=True, client_factory=_demoting, now=NOWX)
    assert "is DEMOTED" not in r.detail and "would demote" in r.detail
    assert "demotion NOT written (dry run)" in r.detail


def test_a_failed_demotion_write_is_named_in_the_tail(monkeypatch, tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_DEMOTION_PATH", str(blocker / "demotion.json"))
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")
    r = hc.check_channel_archive(client_factory=_demoting, now=NOWX)
    assert r.status == "warn" and r.detail.startswith("acting T1")
    assert "DEMOTION WRITE FAILED" in r.detail and "demotion WRITTEN" not in r.detail


def test_no_client_is_blind_never_ok():
    r = hc.check_channel_archive()          # default factory refuses under pytest
    assert r.status == "warn" and r.detail.startswith("BLIND: no Slack client")


def test_a_crashing_client_is_blind():
    def _boom():
        raise RuntimeError("no network")
    r = hc.check_channel_archive(client_factory=_boom)
    assert r.status == "warn" and "BLIND" in r.detail


def test_main_calls_it_with_the_dry_run_kwarg():
    import ast
    tree = ast.parse((REPO / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "check_channel_archive"]
    assert len(calls) == 1
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw == {"dry_run": "args.dry_run"}
