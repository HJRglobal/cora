"""Step 7.5 reconcile for Code #14: every transition is gated on the fix being
present on this tree (behavioural checks); dry-run writes nothing; every SHIPPED
transition passes the C7 bundle reference; terminal rows are never flipped; the
probe proves the gate on a throwaway ledger; the LEX read-back never prints the
kickoff path; an APPROVED read-back prints the bare verb line for Harrison and never
stages it."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    # the script's import-time load_dotenv(override=True) writes straight into
    # os.environ (monkeypatch cannot see it) and un-pins the live flags conftest set.
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "reconcile_code14",
            _REPO_ROOT / "scripts" / "reconcile_code_queue_code14_2026-09-23.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


EXPECTED_SHIPPED = {"cq-0f04ad6543a8", "cq-439d89a84de4", "cq-c7dbaef87633", "cq-a5b3e6a2e844",
                    "cq-17fe76f5ab91", "cq-2f6a6cee0169", "cq-323c8974fa02", "cq-85b35413b020"}
READBACK = "cq-a24f9d2210fc"
FAKE_PROMPT_PATH = r"G:\My Drive\SECRET-LEX-FOLDER\kickoff-probe.md"


def _rec(status, severity="MEDIUM"):
    return lambda cq_id: {"id": cq_id, "status": status, "severity": severity}


def _fold(status, prompt_path=FAKE_PROMPT_PATH):
    return lambda: {READBACK: {"id": READBACK, "status": status, "entity": "LEX",
                               "prompt_path": prompt_path}}


def _boom(*a, **k):
    raise AssertionError("dry-run wrote!")


def _no_writes(mod, monkeypatch):
    for name in ("process_queue_action", "dismiss_with_evidence", "supersede_item",
                 "set_severity", "_append_event", "ensure_kickoff_staged"):
        monkeypatch.setattr(mod.code_queue, name, _boom)


def _exists(value):
    from cora import drive_io
    return lambda path, **kw: value


def test_ids_match_the_kickoff_and_every_precondition_passes_on_this_tree():
    mod = _load()
    assert set(mod.SHIPPED) == EXPECTED_SHIPPED
    assert set(mod.PRECONDITIONS) == EXPECTED_SHIPPED
    # S1 rode Code #13; S4 was DISMISSED -- neither is ever transitioned here
    assert "cq-70d7b203f7ad" not in mod.SHIPPED and "cq-70d7b203f7ad" in mod.ALREADY_SHIPPED
    assert "cq-1a8611487e26" not in mod.SHIPPED and "cq-1a8611487e26" in mod.DISMISSED_NOT_TOUCHED
    assert READBACK not in mod.SHIPPED and mod.READBACK_ID == READBACK
    for cq_id, fn in mod.PRECONDITIONS.items():
        assert fn() is None, (cq_id, fn())
    assert mod.BUNDLE_ID == "code-14" and mod.BRANCH == "claude/code-14-bug-bundle"
    from cora import code_queue
    assert mod.HARRISON_ID == code_queue.HARRISON_ID


def test_dry_run_reports_everything_and_writes_nothing(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item",
                        lambda cq: {"id": cq, "status": "SHIPPED" if cq == "cq-70d7b203f7ad" else "APPROVED",
                                    "bundle_id": "code-13"})
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(True))
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    for cq in EXPECTED_SHIPPED:
        assert f"would mark SHIPPED  {cq}" in out
    assert "ALREADY SHIPPED  cq-70d7b203f7ad" in out
    assert "NOT TOUCHED  cq-1a8611487e26" in out
    assert f"READ-BACK  {READBACK}  (STAGED) -> kickoff on disk (path withheld, LEX row)" in out
    assert "SECRET-LEX-FOLDER" not in out and "kickoff-probe" not in out
    assert "BLOCKED" not in out


def test_apply_passes_the_bundle_reference_and_labels_follow_outcomes(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item",
                        lambda cq: {"id": cq, "status": "SHIPPED" if cq == "cq-70d7b203f7ad" else "PROPOSED"})
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(True))
    monkeypatch.setattr(mod, "_head_commit", lambda: "abc1234")
    ship_calls = []

    def fake_action(action_id, cq_id, actor_id, **kw):
        ship_calls.append((cq_id, kw))
        assert action_id == mod.code_queue.ACTION_MARK_SHIPPED and actor_id == mod.HARRISON_ID
        return ("shipped", "\U0001F6A2 Marked shipped.") if cq_id != "cq-2f6a6cee0169" else ("refused", "no bundle")
    monkeypatch.setattr(mod.code_queue, "process_queue_action", fake_action)
    monkeypatch.setattr(mod.code_queue, "_append_event", _boom)
    monkeypatch.setattr(mod.code_queue, "ensure_kickoff_staged", _boom)
    assert mod.main(["--apply"]) == 1  # the one refused ship makes it non-zero
    out = capsys.readouterr().out
    assert "SHIPPED  cq-0f04ad6543a8" in out and "SHIPPED  cq-85b35413b020" in out
    assert "NOT SHIPPED  cq-2f6a6cee0169" in out and "refused: no bundle" in out
    assert ship_calls and all(kw == {"bundle_id": "code-14", "branch": mod.BRANCH, "commit": "abc1234"}
                              for _cq, kw in ship_calls)
    assert {cq for cq, _ in ship_calls} == EXPECTED_SHIPPED
    # neither printed-only row was ever passed to the writer
    assert not {"cq-70d7b203f7ad", "cq-1a8611487e26", READBACK} & {cq for cq, _ in ship_calls}


def test_terminal_rows_are_never_flipped(capsys, monkeypatch):
    mod = _load()
    for terminal in ("DISMISSED", "SUPERSEDED"):
        monkeypatch.setattr(mod.code_queue, "get_item", _rec(terminal))
        monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
        from cora import drive_io
        monkeypatch.setattr(drive_io, "exists", _exists(True))
        _no_writes(mod, monkeypatch)
        assert mod.main(["--apply"]) == 1
        out = capsys.readouterr().out
        assert out.count("refusing to flip a terminal row") == len(EXPECTED_SHIPPED)


def test_an_approved_readback_prints_the_bare_verb_and_never_stages(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item",
                        lambda cq: {"id": cq, "status": "SHIPPED"})
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("APPROVED", prompt_path=""))
    _no_writes(mod, monkeypatch)
    assert mod.main(["--apply"]) == 0
    out = capsys.readouterr().out
    # the bare line, on its own line, for Harrison to paste
    assert f"\nstage {READBACK}\n" in out


def test_a_missing_or_unknowable_kickoff_is_a_nonzero_readback(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", lambda cq: {"id": cq, "status": "SHIPPED"})
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(False))
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "kickoff file MISSING (path withheld, LEX row)" in out and "SECRET-LEX-FOLDER" not in out

    def _gone(path, **kw):
        raise drive_io.DriveUnavailable("mount gone")
    monkeypatch.setattr(drive_io, "exists", _gone)
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "kickoff existence UNKNOWN" in out and "SECRET-LEX-FOLDER" not in out


def test_s1_not_yet_shipped_by_code13_is_loud(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(True))
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 1
    assert "S1 comes back FIRST" in capsys.readouterr().out


def test_a_mismatched_harrison_id_refuses_before_any_transition(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "HARRISON_ID", "U0SOMEONEELSE")
    _no_writes(mod, monkeypatch)
    assert mod.main(["--apply"]) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_probe_gate_refuses_without_bundle_and_never_touches_the_real_ledger(capsys):
    mod = _load()
    real = mod.code_queue._DEFAULT_EVENT_LEDGER
    redirected = mod.code_queue._EVENT_LEDGER
    before = real.read_text(encoding="utf-8") if real.exists() else None
    assert mod.main(["--probe-gate"]) == 0
    out = capsys.readouterr().out
    assert "PROBE OK" in out and "refused" in out
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert before == after
    assert mod.code_queue._EVENT_LEDGER == redirected  # restored


def test_env_pins_survive_loading_the_script(monkeypatch):
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")
    monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "observe")
    _load()
    assert os.environ["CORA_CODE_QUEUE"] == "off" and os.environ["CORA_SENTINEL_ENFORCE"] == "observe"


def test_a_raising_precondition_blocks_instead_of_aborting(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(True))
    monkeypatch.setitem(mod.PRECONDITIONS, "cq-0f04ad6543a8", lambda: 1 / 0)
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED  cq-0f04ad6543a8" in out and "precondition raised ZeroDivisionError" in out
    assert "would mark SHIPPED  cq-439d89a84de4" in out  # the loop continued


def test_the_s3_precondition_never_writes_the_real_rail_ledger_and_restores_state():
    mod = _load()
    from cora import slack_egress as se
    armed, path = se._RAIL_LEDGER_ARMED, se.PHANTOM_CLAIMS_LEDGER
    before = Path(path).read_bytes() if Path(path).exists() else None
    assert mod._s3_present() is None
    assert (se._RAIL_LEDGER_ARMED, se.PHANTOM_CLAIMS_LEDGER) == (armed, path)
    assert (Path(path).read_bytes() if Path(path).exists() else None) == before


def test_the_honesty_rail_precondition_is_behavioural_not_a_grep(monkeypatch):
    """A tree where the capability screen exists but never WARNs must be BLOCKED."""
    mod = _load()
    from cora import slack_egress
    monkeypatch.setattr(slack_egress, "screen_capability_claims", lambda text, **kw: text)
    blocker = mod._r149_present()
    assert blocker and "did not WARN" in blocker


def test_the_s7_precondition_blocks_a_question_mark_badge(monkeypatch):
    mod = _load()
    from cora import knowledge_review as kr
    monkeypatch.setattr(kr, "format_mechanical_dm", lambda u: "*[Asana task]* `?`\nClose a probe task")
    blocker = mod._s7_present()
    assert blocker and "'?'" in blocker


# -- D-051 integration-tests-4: each precondition BLOCKS a stubbed tree ----------
# One stub per SHIPPED id whose old check a stub could satisfy: the substance of
# the fix is gone while every name, and every string a grep looked for, is kept.
import pytest  # noqa: E402


def _nhc(mod):
    import nightly_health_check as nhc   # the same module object the script imports
    return nhc


@pytest.mark.parametrize("stub", ["own_log_detector", "report_markers", "truncating_flatten",
                                  "main_skips_flatten"])
def test_s2_blocks_a_tree_without_the_own_log_skip_or_the_full_detail_line(monkeypatch, stub):
    mod = _load()
    nhc = _nhc(mod)
    if stub == "own_log_detector":
        monkeypatch.setattr(nhc, "_is_own_log", lambda path: False)
        want = "re-raises its own quoted report line"
    elif stub == "report_markers":
        monkeypatch.setattr(nhc, "_OWN_REPORT_MARKERS", ())
        want = "re-raises its own quoted report line"
    elif stub == "truncating_flatten":
        monkeypatch.setattr(nhc, "_flatten_detail", lambda detail: (detail or "")[:100])
        want = "truncates or keeps newlines"
    else:
        def main():  # a main() that only NAMES the helper: _flatten_detail(r.detail)
            return 0
        monkeypatch.setattr(nhc, "main", main)
        want = "does not log the result detail through _flatten_detail"
    blocker = mod._s2_present()
    assert blocker and want in blocker, blocker
    # the probe never leaves the module pointed at its throwaway log dir
    assert nhc._LOG_DIR.name == "logs"


def test_s2_blocks_a_tree_that_hides_a_real_writer_failure_in_its_own_log(monkeypatch):
    """The narrow exclusion must stay narrow: a check that skips its whole own log
    would hide REPEAT_SIGNAL_WRITE_FAILING raised inside the health-check process."""
    mod = _load()
    nhc = _nhc(mod)
    real = nhc.check_logs_24h

    def skip_whole_own_log():
        saved = nhc._CRITICAL_RE
        try:
            nhc._CRITICAL_RE = __import__("re").compile(r"\A(?!x)x")   # matches nothing
            return real()
        finally:
            nhc._CRITICAL_RE = saved
    monkeypatch.setattr(nhc, "check_logs_24h", skip_whole_own_log)
    blocker = mod._s2_present()
    assert blocker and "no longer reads critical" in blocker


@pytest.mark.parametrize("stub", ["never_withholds", "wiring_only_in_comments", "no_ledger_arm"])
def test_s3_blocks_a_tree_whose_rail_context_never_withholds_or_is_unwired(monkeypatch, stub):
    mod = _load()
    from cora import app
    if stub == "never_withholds":
        monkeypatch.setattr(app, "_rail_context", lambda *a, **k: {
            "channel_id": "C0", "entity": "LEX-LLC", "snippet_withheld": None, "founder_belt": False})
        want = "does not withhold the snippet in LEX scope"
    elif stub == "wiring_only_in_comments":
        def _dispatch_qa(*a, **k):
            # exactly six in COMMENTS, which the old source-count grep accepted:
            # rail_context=rail_ctx rail_context=rail_ctx rail_context=rail_ctx
            # rail_context=rail_ctx rail_context=rail_ctx rail_context=rail_ctx
            return None
        monkeypatch.setattr(app, "_dispatch_qa", _dispatch_qa)
        want = "not passed at all six screen sites"
    else:
        import importlib
        main_mod = importlib.import_module("cora.main")
        monkeypatch.setattr(mod.inspect, "getsource",
                            lambda obj, _real=mod.inspect.getsource:
                            "# slack_egress.arm_rail_ledger() is armed elsewhere\nx = 'arm_rail_ledger()'\n"
                            if obj is main_mod else _real(obj))
        want = "never arms the rail ledger"
    blocker = mod._s3_present()
    assert blocker and want in blocker, blocker


@pytest.mark.parametrize("stub", ["comment_only", "legacy_behind_getattr", "result_discarded",
                                  "legacy_named", "referenced_never_called"])
def test_r143_blocks_a_run_preflight_that_only_mentions_the_attribution_rail(monkeypatch, stub):
    """Every stub keeps the helper names; the first three also satisfy the old
    substring test ('rail2_attribution_hit(' present, 'rail2_legacy_hit(' absent)."""
    mod = _load()
    from cora.f3e_blog import preflight as pf
    if stub == "comment_only":
        def run_preflight(*, title, summary, body_html, lane="learn"):
            # rail2_attribution_hit( is wired below
            return pf.PreflightResult(passed=True)
        want = "not wired to the attribution rail"
    elif stub == "legacy_behind_getattr":
        def run_preflight(*, title, summary, body_html, lane="learn"):
            text = pf.unescaped(body_html)
            legacy = getattr(pf, "rail2_legacy_" + "hit")      # no Call node named legacy
            hit = pf.rail2_attribution_hit(text) or legacy(text)
            trips = [pf.Trip("R2", "clean/natural", "body", text)] if hit else []
            return pf.PreflightResult(passed=not trips, trips=trips)
        want = "still trips rail 2 on the ruled green-tea phrase"
    elif stub == "result_discarded":
        def run_preflight(*, title, summary, body_html, lane="learn"):
            pf.rail2_attribution_hit(body_html)              # called, result thrown away
            return pf.PreflightResult(passed=True)
        want = "does not trip rail 2 on a clean claim"
    elif stub == "legacy_named":
        def run_preflight(*, title, summary, body_html, lane="learn"):
            text = pf.unescaped(body_html)
            hit = pf.rail2_legacy_hit(text)   # the frozen same-sentence scan
            trips = [pf.Trip("R2", "clean/natural", "body", text)] if hit else []
            return pf.PreflightResult(passed=not trips, trips=trips)
        want = "not wired to the attribution rail"
    else:
        def run_preflight(*, title, summary, body_html, lane="learn"):
            _ = pf.rail2_attribution_hit  # referenced, never called
            return pf.PreflightResult(passed=True)
        want = "not wired to the attribution rail"
    monkeypatch.setattr(pf, "run_preflight", run_preflight)
    blocker = mod._r143_present()
    assert blocker and want in blocker, blocker


@pytest.mark.parametrize("stub", ["seam_none", "seam_never_fires", "seam_fires_for_anyone",
                                  "never_forced"])
def test_r149a_blocks_a_tree_without_a_working_forcing_seam(monkeypatch, stub):
    mod = _load()
    from cora import app
    if stub == "seam_none":
        monkeypatch.setattr(app, "_queue_status_turn", None)
        want = "seam _queue_status_turn is missing"
    elif stub == "seam_never_fires":
        monkeypatch.setattr(app, "_queue_status_turn", lambda *a, **k: False)
        want = "does not fire for Harrison"
    elif stub == "seam_fires_for_anyone":
        monkeypatch.setattr(app, "_queue_status_turn", lambda *a, **k: True)
        want = "fires outside Harrison's DM"
    else:
        def _dispatch_qa(*a, **k):
            queue_status_turn = app._queue_status_turn(*a)
            # force_tool = "cora_queue_status"
            return queue_status_turn
        monkeypatch.setattr(app, "_dispatch_qa", _dispatch_qa)
        want = "never forces cora_queue_status"
    blocker = mod._r149_present()
    assert blocker and want in blocker, blocker


def test_the_s2_probe_restores_the_log_dir_and_writes_no_real_log():
    mod = _load()
    nhc = _nhc(mod)
    before = nhc._LOG_DIR
    listing = sorted(p.name for p in before.glob("health-check-*.log")) if before.exists() else []
    assert mod._s2_present() is None
    assert nhc._LOG_DIR == before
    after = sorted(p.name for p in before.glob("health-check-*.log")) if before.exists() else []
    assert after == listing
