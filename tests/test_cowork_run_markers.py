"""Code #13 slice 9c (cq-06045f418bd2): the Cowork-estate run-marker CONTRACT.

Pins the parts of the contract that live outside the mirror's own plan: the footer
document (deployment/cowork-run-marker-footer.md) that Harrison's pin-script -Apply
injects, the static-walk belt that keeps a stray .md under _runs/ out of the KB, the
DECLARED cadence map's seeds, the evaluator's date arithmetic, and -- on this host --
the paste block itself: the FIND anchors must match the live pin script exactly, and
the ASSEMBLED script must parse and, run against fixture SKILL.md files, append the
footer body-only, idempotently, leaving the frontmatter byte-identical.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import incremental_sync_static as inc  # noqa: E402
import mirror_claude_workspace as m  # noqa: E402

FOOTER_DOC = REPO / "deployment" / "cowork-run-marker-footer.md"
CADENCE_YAML = REPO / "data" / "maps" / "cowork-run-cadence.yaml"
PIN_SCRIPT = Path(r"C:\Users\Harri\code\pin-scheduled-task-models.ps1")

_EDIT_RE = re.compile(
    r"FIND:\r?\n```powershell\r?\n(.*?)\r?\n```\r?\nREPLACE:\r?\n```powershell\r?\n(.*?)\r?\n```",
    re.DOTALL,
)


def _edits() -> list[tuple[str, str]]:
    text = FOOTER_DOC.read_text(encoding="utf-8")
    pairs = _EDIT_RE.findall(text)
    assert pairs, "no FIND/REPLACE pairs parsed from the footer doc -- format drifted"
    return pairs


# ── the footer document ───────────────────────────────────────────────────────
def test_footer_doc_is_ascii():
    """D-016: the paste block lands in a PowerShell 5.1 script that reads UTF-8 as
    Windows-1252 -- one curly quote corrupts the here-string."""
    raw = FOOTER_DOC.read_bytes()
    bad = [i for i, b in enumerate(raw) if b > 127]
    assert not bad, f"non-ASCII byte(s) at offsets {bad[:5]}"


def test_footer_doc_carries_the_marker_and_the_four_fields():
    """The injector keys idempotence on the marker comment and the reader requires the
    four fields; the document is the contract of record for both."""
    text = FOOTER_DOC.read_text(encoding="utf-8")
    assert m.RUN_MARKER_FOOTER_MARKER in text
    for field in m.RUN_MARKER_FIELDS:
        assert re.search(rf"\b{field}\b", text), field
    assert "a run that writes nothing is a run that did not happen" in text
    assert "-InjectRunMarkerFooter" in text
    assert "restart the claude app" in text.lower()
    assert "_runs" in text and "<folder-id>" in text.lower()
    assert "FOLDER id" in text  # the key is the folder, stated in the footer text itself


def test_footer_doc_never_touches_the_root_default_line():
    """tests/scripts/test_mirror_claude_workspace.py pins the pin script's $Root default
    against the mirror yaml; the paste block must not carry a replacement for it."""
    for find, replace in _edits():
        assert "$Root" not in find and "$Root" not in replace


def test_footer_template_in_the_doc_matches_the_paste_block():
    """The footer shown in section 3 and the here-string in EDIT 3 must be the same
    text -- two copies that drift would inject one contract and document another."""
    text = FOOTER_DOC.read_text(encoding="utf-8")
    shown = re.search(r"```text\r?\n(.*?)\r?\n```", text, re.DOTALL)
    assert shown, "section-3 footer block not found"
    injected = None
    for _find, replace in _edits():
        mt = re.search(r"\$footerTemplate = @'\r?\n(.*?)\r?\n'@", replace, re.DOTALL)
        if mt:
            injected = mt.group(1)
    assert injected is not None, "EDIT 3 here-string not found"
    assert shown.group(1).strip() == injected.strip()
    assert injected.lstrip().startswith(m.RUN_MARKER_FOOTER_MARKER)


# ── the KB belt ───────────────────────────────────────────────────────────────
def test_static_walk_excludes_anything_under_a_runs_segment(tmp_path):
    """ZONE-K is the KB-ingested zone; a .md a task drops under _runs/ by mistake must
    never ingest. Segment match: a file merely NAMED test_runs.md stays ingestible."""
    zk = tmp_path / "HJR-Founder-OS" / "_shared" / "claude-workspace-mirror"
    assert inc.is_static_excluded(zk / "_runs" / "some-task" / "2026-09-19.md")
    assert inc.is_static_excluded(zk / "_runs" / "some-task" / "2026-09-19.json")
    assert not inc.is_static_excluded(zk / "skills" / "wrap-it.SKILL.md")
    assert not inc.is_static_excluded(tmp_path / "HJR-Founder-OS" / "02-F3-Energy" / "test_runs.md")


# ── the declared cadence map ──────────────────────────────────────────────────
def test_cadence_yaml_seeds_the_four_self_logging_tasks():
    """Ruling: seed the four tasks that already self-log, at plausible cadences taken
    from their SKILL.md prose, marked transitional. A row without a positive
    cadence_hours would silently read 'unknown cadence' instead of 'did not run'."""
    data = yaml.safe_load(CADENCE_YAML.read_text(encoding="utf-8"))
    assert set(data) >= {"connector-health-heartbeat", "cora-knowledge-review",
                         "cowork-cora-redundancy-audit", "hygiene-asana"}
    for tid, ent in data.items():
        assert float(ent["cadence_hours"]) > 0, tid
        assert isinstance(ent["expects_output"], bool), tid
        assert "transitional" in str(ent.get("note", "")), tid
    assert data["connector-health-heartbeat"]["cadence_hours"] == 24
    assert data["cora-knowledge-review"]["cadence_hours"] == 24
    assert data["cowork-cora-redundancy-audit"]["cadence_hours"] == 720
    assert data["hygiene-asana"]["cadence_hours"] == 168


def test_load_cadence_reads_the_live_map_and_is_fail_soft(tmp_path):
    live, err = m.load_cadence()
    assert err is None and "connector-health-heartbeat" in live
    missing, err2 = m.load_cadence(tmp_path / "nope.yaml")
    assert missing == {} and err2
    notmap = tmp_path / "list.yaml"
    notmap.write_text("- a\n- b\n", encoding="utf-8")
    assert m.load_cadence(notmap) == ({}, "cadence map is not a mapping")


# ── the evaluator (pure, pinned clock) ────────────────────────────────────────
TODAY = date(2026, 9, 19)


def test_evaluate_marker_missing_with_cadence_is_did_not_run():
    row = m.evaluate_run_marker("t", None, {"cadence_hours": 168, "expects_output": True}, today=TODAY)
    assert row["status"] == m.RM_DID_NOT_RUN and "MISSED FIRE" in row["detail"]


def test_evaluate_weekly_window_is_two_cadences():
    """A weekly task's marker 13 days old is inside 2x168h; 15 days is not."""
    ent = {"cadence_hours": 168, "expects_output": True}
    inside = {"date": "2026-09-06", "unreadable": False, "ok": True, "outputs": 1}
    outside = {"date": "2026-09-04", "unreadable": False, "ok": True, "outputs": 1}
    assert m.evaluate_run_marker("t", inside, ent, today=TODAY)["status"] == m.RM_OK
    assert m.evaluate_run_marker("t", outside, ent, today=TODAY)["status"] == m.RM_DID_NOT_RUN


def test_evaluate_unknown_cadence_beats_missing_marker_but_not_unreadable():
    """Order of precedence: unreadable is reported even without a cadence row; a
    missing marker without a row is 'unknown cadence', not 'did not run'."""
    assert m.evaluate_run_marker("t", None, None, today=TODAY)["status"] == m.RM_UNKNOWN_CADENCE
    bad = {"date": "2026-09-19", "unreadable": True, "error": "JSONDecodeError"}
    assert m.evaluate_run_marker("t", bad, None, today=TODAY)["status"] == m.RM_UNREADABLE


def test_evaluate_zero_cadence_row_is_unknown_cadence():
    row = m.evaluate_run_marker("t", None, {"cadence_hours": 0, "expects_output": True}, today=TODAY)
    assert row["status"] == m.RM_UNKNOWN_CADENCE


# ── R14-6: the `registered:` grace (ONE cadence, ruling 9.11(ii)) ──────────────
def _reg(cadence_h, registered, marker=None):
    ent = {"cadence_hours": cadence_h, "expects_output": True, "registered": registered}
    return m.evaluate_run_marker("t", marker, ent, today=TODAY)


def test_registered_grace_is_one_cadence_daily():
    """Registered today, no marker -> awaiting. A bare date is the END of that AZ day,
    so registered yesterday is 0h old at the start of today -> still awaiting (the
    -Apply may have run after yesterday's fire; D-051 dry-run-writes-1). Two days back
    is 24h past that origin, NOT < 24h: did not run. 1x, not run_marker's 2x."""
    row = _reg(24, "2026-09-19")
    assert row["status"] == m.RM_AWAITING_FIRST
    assert "registered 2026-09-19" in row["detail"] and "1x cadence" in row["detail"]
    assert _reg(24, "2026-09-18")["status"] == m.RM_AWAITING_FIRST
    late = _reg(24, "2026-09-17")
    assert late["status"] == m.RM_DID_NOT_RUN and "grace has elapsed" in late["detail"]


def test_registered_grace_weekly_boundary():
    """Sat 9/12 registered (after that day's 06:30 fire) -> the next fire is Sat 9/19
    06:30, so the 9/19 mirror still awaits it; one day earlier the grace is spent."""
    assert _reg(168, "2026-09-13")["status"] == m.RM_AWAITING_FIRST   # 120h < 168h
    assert _reg(168, "2026-09-12")["status"] == m.RM_AWAITING_FIRST   # 144h < 168h
    assert _reg(168, "2026-09-11")["status"] == m.RM_DID_NOT_RUN      # 168h, not < 168h


# ── D-051 dry-run-writes-1: the grace ends at the FIRST FIRE, not at midnight ──
from datetime import datetime as _dt, timedelta as _tdelta, timezone as _tz  # noqa: E402

_AZ = _tz(_tdelta(hours=-7))


def _at(d: str, hh: int, mm: int) -> "_dt":
    y, mo, dd = (int(x) for x in d.split("-"))
    return _dt(y, mo, dd, hh, mm, tzinfo=_AZ)


def _reg_at(cadence_h, registered, now):
    ent = {"cadence_hours": cadence_h, "expects_output": True, "registered": registered}
    return m.evaluate_run_marker("t", None, ent, today=now.date(), now=now)["status"]


def test_registered_mid_morning_daily_is_still_awaiting_at_the_next_0345_mirror():
    """The finding's pin: -Apply at 10:00 AZ on 9/25 (after the 06:40 fire), daily
    cadence. The 9/26 03:45 mirror (the one the 08:45 health check reads) must say
    awaiting -- the first fire is 06:40. By the 12:15 run that fire is overdue."""
    for reg in (_at("2026-09-25", 10, 0), "2026-09-25T10:00", "2026-09-25T17:00:00+00:00"):
        assert _reg_at(24, reg, _at("2026-09-25", 12, 15)) == m.RM_AWAITING_FIRST, reg
        assert _reg_at(24, reg, _at("2026-09-26", 3, 45)) == m.RM_AWAITING_FIRST, reg
        assert _reg_at(24, reg, _at("2026-09-26", 12, 15)) == m.RM_DID_NOT_RUN, reg


def test_a_bare_date_counts_from_the_end_of_that_az_day():
    """A date-only stamp may have been written at 23:59, so its first fire can be a
    whole cadence after that midnight: awaiting through D+1, did not run on D+2."""
    for now in (_at("2026-09-25", 12, 15), _at("2026-09-26", 3, 45), _at("2026-09-26", 12, 15)):
        assert _reg_at(24, "2026-09-25", now) == m.RM_AWAITING_FIRST, now
    assert _reg_at(24, "2026-09-25", _at("2026-09-27", 3, 45)) == m.RM_DID_NOT_RUN


def test_weekly_registered_after_the_saturday_fire_awaits_the_next_saturday():
    """hygiene-asana: cron Sat 06:30. Registered Sat 9/26 -> first fire Sat 10/3 06:30;
    the 10/3 03:45 mirror awaits it, the 10/4 03:45 mirror reports the miss."""
    assert _reg_at(168, "2026-09-26", _at("2026-10-03", 3, 45)) == m.RM_AWAITING_FIRST
    assert _reg_at(168, "2026-09-26", _at("2026-10-04", 3, 45)) == m.RM_DID_NOT_RUN


def test_monthly_grace_is_the_longest_calendar_month_not_720h():
    """cowork-cora-redundancy-audit: cron 1st 10:00. Registered Oct 1 (after the fire)
    -> first fire Nov 1 10:00, 31 days on: the Nov 1 03:45 mirror must await it (720h
    would read did not run that morning). Nov 2 reports the miss."""
    for reg in ("2026-10-01", "2026-10-01T11:00"):
        assert _reg_at(720, reg, _at("2026-11-01", 3, 45)) == m.RM_AWAITING_FIRST, reg
        assert _reg_at(720, reg, _at("2026-11-02", 3, 45)) == m.RM_DID_NOT_RUN, reg
    assert m._first_fire_grace_h(720) == 744 and m._first_fire_grace_h(168) == 168
    assert m._first_fire_grace_h(24) == 24


def test_a_future_registered_value_is_still_absent_with_a_run_instant():
    """Keep the fail-closed rule: a registered DAY after the run's day is a typo, never
    an excuse -- including a timestamp that lands on a later AZ day."""
    now = _at("2026-09-25", 3, 45)
    for reg in ("2026-09-26", "2026-09-26T01:00", "2027-09-25"):
        ent = {"cadence_hours": 24, "expects_output": True, "registered": reg}
        row = m.evaluate_run_marker("t", None, ent, today=now.date(), now=now)
        assert row["status"] == m.RM_DID_NOT_RUN and "in the future -- ignored" in row["detail"], reg


def test_the_mirror_passes_its_run_instant_only_when_it_matches_today(monkeypatch, tmp_path):
    """plan_cowork_tasks hands the evaluator the REAL run instant (a daily row
    registered 9/25 10:00 reads awaiting at 9/26 03:45). A pinned `today` that does
    not match the instant falls back to the start of that day -- never a mixed clock."""
    task = tmp_path / "tasks" / "amber-wren-digest"
    task.mkdir(parents=True)
    (task / "SKILL.md").write_text("---\nname: amber-wren-digest\n---\nbody\n", encoding="utf-8")
    monkeypatch.setenv("COWORK_TASKS_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr(m, "read_run_marker", lambda tid: None)
    monkeypatch.setattr(m, "load_cadence", lambda: ({"amber-wren-digest": {
        "cadence_hours": 24, "expects_output": True, "registered": "2026-09-25T10:00"}}, None))
    monkeypatch.setattr(m, "_now_az", lambda: _at("2026-09-26", 3, 45))
    monkeypatch.setattr(m, "_today_az", lambda: date(2026, 9, 26))
    plan = m.Plan()
    m.plan_cowork_tasks(plan, m.load_config())
    assert plan.run_markers["amber-wren-digest"]["status"] == m.RM_AWAITING_FIRST
    # today pinned to 9/27 while the instant says 9/26: start of 9/27 is 38h on
    monkeypatch.setattr(m, "_today_az", lambda: date(2026, 9, 27))
    plan2 = m.Plan()
    m.plan_cowork_tasks(plan2, m.load_config())
    assert plan2.run_markers["amber-wren-digest"]["status"] == m.RM_DID_NOT_RUN


def test_registered_accepts_date_datetime_and_iso():
    from datetime import datetime, timezone  # noqa: PLC0415
    for val in (date(2026, 9, 19),
                datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc),
                "2026-09-19T00:00:00+00:00",
                "2026-09-19"):
        assert _reg(24, val)["status"] == m.RM_AWAITING_FIRST, repr(val)


def test_registered_parsed_from_real_yaml_forms(tmp_path):
    """The loader hands evaluate_run_marker whatever yaml.safe_load produced: a bare
    date and a bare timestamp both arrive as date/datetime objects, not strings."""
    p = tmp_path / "c.yaml"
    p.write_text("a:\n  cadence_hours: 24\n  expects_output: true\n  registered: 2026-09-19\n"
                 "b:\n  cadence_hours: 24\n  expects_output: true\n"
                 "  registered: 2026-09-19T00:00:00+00:00\n", encoding="utf-8")
    cad, err = m.load_cadence(p)
    assert err is None
    for tid in ("a", "b"):
        assert m.evaluate_run_marker(tid, None, cad[tid], today=TODAY)["status"] == m.RM_AWAITING_FIRST


def test_unparseable_or_future_registered_is_no_excuse():
    """A typo must never become an excuse: 'soon', '', a bool, an int, and a date AFTER
    today all read as absent -> did not run (fail-closed)."""
    for val in ("soon", "", True, 20260919, None, "2027-09-19"):
        assert _reg(24, val)["status"] == m.RM_DID_NOT_RUN, repr(val)
    assert "in the future -- ignored" in _reg(24, "2026-09-20")["detail"]


def test_no_registered_key_keeps_todays_behaviour():
    row = m.evaluate_run_marker("t", None, {"cadence_hours": 24, "expects_output": True}, today=TODAY)
    assert row["status"] == m.RM_DID_NOT_RUN and "registered" not in row["detail"]


def test_grace_never_masks_a_stale_or_empty_marker():
    """The grace applies ONLY while no marker exists: registered today with a 20-day-old
    marker on a weekly task is still did not run; a fresh empty marker still reads
    fired but wrote nothing."""
    stale = {"date": "2026-08-30", "unreadable": False, "ok": True, "outputs": 1}
    assert _reg(168, "2026-09-19", stale)["status"] == m.RM_DID_NOT_RUN
    empty = {"date": "2026-09-19", "unreadable": False, "ok": True, "outputs": 0}
    assert _reg(24, "2026-09-19", empty)["status"] == m.RM_WROTE_NOTHING


def test_awaiting_first_is_neither_alarm_nor_ok():
    assert m.RM_AWAITING_FIRST not in m.RUN_MARKER_ALARM_STATUSES
    assert m.RM_AWAITING_FIRST != m.RM_OK
    order = list(m._RM_RENDER_ORDER)
    assert order.index(m.RM_AWAITING_FIRST) < order.index(m.RM_OK)


def test_seed_rows_carry_no_guessed_registered_date():
    """The footer is NOT applied (0 SKILL.md carry it on 2026-09-23): a `registered:`
    on a seed row would silence a real 'did not run'. Harrison stamps the real
    -Apply date; until then the seeds must read did not run."""
    data = yaml.safe_load(CADENCE_YAML.read_text(encoding="utf-8"))
    for tid in ("connector-health-heartbeat", "cora-knowledge-review",
                "cowork-cora-redundancy-audit", "hygiene-asana"):
        assert "registered" not in data[tid], tid


# ── the cadence map against the LIVE Cowork registry (this host only) ─────────
def _cron_hours(expr: str) -> float | None:
    """Map the three cron shapes the cadence map uses to hours; None for anything else.
    str.split only -- no regex (D-171 n/a)."""
    f = str(expr).split()
    if len(f) != 5:
        return None
    mi, hr, dom, mon, dow = f
    if not (mi.isdigit() and hr.isdigit()) or mon != "*":   # one fixed fire time only
        return None
    if dom == "*" and dow == "*":
        return 24.0
    if dom == "*" and dow.isdigit():
        return 168.0
    if dom.isdigit() and dow == "*":
        return 720.0
    return None


def test_cron_hours_mapping():
    assert _cron_hours("40 6 * * *") == 24
    assert _cron_hours("30 6 * * 6") == 168
    assert _cron_hours("0 10 1 * *") == 720
    assert _cron_hours("0 7 * * 1-5") is None and _cron_hours("*/15 * * * *") is None
    assert _cron_hours("bogus") is None


def test_cadence_rows_match_live_cowork_cron():
    """R14-6: every cadence_hours is pinned to the task's LIVE cronExpression. Reads
    the Cowork registry READ-ONLY by glob (never a hard-coded account/org path);
    skips when the registry is not on this host."""
    import glob  # noqa: PLC0415
    import json  # noqa: PLC0415
    appdata = os.environ.get("APPDATA") or ""
    files = glob.glob(os.path.join(appdata, "Claude", "local-agent-mode-sessions", "*", "*",
                                   "scheduled-tasks.json")) if appdata else []
    live: dict[str, str] = {}
    for fp in files:
        try:
            data = json.loads(Path(fp).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for t in data.get("scheduledTasks") or []:
            if isinstance(t, dict) and t.get("id") and t.get("cronExpression"):
                live[str(t["id"])] = str(t["cronExpression"])
    if not live:
        pytest.skip("Cowork scheduled-task registry not on this host")
    rows = yaml.safe_load(CADENCE_YAML.read_text(encoding="utf-8"))
    for tid, ent in rows.items():
        assert tid in live, f"{tid}: in the cadence map but not in the live Cowork registry"
        hours = _cron_hours(live[tid])
        assert hours is not None, f"{tid}: unsupported cron shape {live[tid]!r}"
        assert float(ent["cadence_hours"]) == hours, (tid, live[tid], ent["cadence_hours"])
        assert f'"{live[tid]}"' in CADENCE_YAML.read_text(encoding="utf-8"), \
            f"{tid}: the yaml comment does not cite the live cron {live[tid]!r}"


def test_alarm_statuses_are_exactly_the_health_lane_keys():
    """The mirror's alarm vocabulary and the nightly check's key list must agree, or a
    status the mirror emits is one the health lane silently ignores."""
    import nightly_health_check as nhc  # noqa: PLC0415
    assert [label for _k, label in nhc._RUN_MARKER_ALARM_KEYS] == [
        "did not run", "fired but wrote nothing", "unreadable marker", "reported an error"]
    assert set(m.RUN_MARKER_ALARM_STATUSES) == {m.RM_DID_NOT_RUN, m.RM_WROTE_NOTHING,
                                                 m.RM_UNREADABLE, m.RM_REPORTED_ERROR}


# ── the paste block against the LIVE pin script (this host only) ──────────────
def _assemble_patched_script() -> str:
    """Apply the doc's FIND/REPLACE pairs to the live script text. Each FIND must occur
    exactly once -- the drift guard for the paste block."""
    raw = PIN_SCRIPT.read_bytes().decode("utf-8")   # bytes -> CRLF preserved (read_text would translate)
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw
    for find, replace in _edits():
        f = find.replace("\r\n", "\n").replace("\n", nl)
        r = replace.replace("\r\n", "\n").replace("\n", nl)
        assert text.count(f) == 1, f"FIND anchor not unique in the live pin script:\n{find[:120]}"
        text = text.replace(f, r, 1)
    return text


@pytest.fixture()
def live_pin_script():
    if not PIN_SCRIPT.exists():
        pytest.skip("pin script not on this host")
    return PIN_SCRIPT


def test_paste_block_anchors_match_the_live_pin_script(live_pin_script):
    patched = _assemble_patched_script()
    assert "[switch]$InjectRunMarkerFooter" in patched
    assert "$footerTemplate = @'" in patched
    assert all(ord(c) < 128 for c in patched), "assembled script must stay ASCII (D-016)"
    # the $Root default line survived byte-for-byte
    orig_root = re.search(r'\$Root\s*=\s*"[^"]+"', live_pin_script.read_text(encoding="utf-8")).group(0)
    assert orig_root in patched


def _skill(root: Path, tid: str, model: str = "sonnet", body: str = "do the work\n") -> Path:
    d = root / tid
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {tid}\n" + (f"model: {model}\n" if model else "") + f"description: runs daily\n---\n\n{body}"
    p = d / "SKILL.md"
    p.write_bytes(fm.encode("utf-8"))
    return p


def _ps(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        capture_output=True, text=True, timeout=180,
    )


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None, reason="PowerShell host")
def test_assembled_script_parses(live_pin_script, tmp_path):
    patched = tmp_path / "pin-patched.ps1"
    patched.write_text(_assemble_patched_script(), encoding="ascii", newline="")
    ps = (f"$null = [System.Management.Automation.PSParser]::Tokenize("
         f"(Get-Content -Raw -LiteralPath '{patched}'), [ref]$null)")
    proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None, reason="PowerShell host")
def test_assembled_script_appends_footer_body_only_idempotently(live_pin_script, tmp_path):
    """Against fixture SKILL.md files (never the live estate): dry run writes nothing;
    -Apply appends the footer with the FOLDER id substituted, after the body, leaving
    the first frontmatter byte-identical; a second -Apply is a no-op (OK); a run
    WITHOUT the switch never injects (opt-in)."""
    patched = tmp_path / "pin-patched.ps1"
    patched.write_text(_assemble_patched_script(), encoding="ascii", newline="")
    root = tmp_path / "Scheduled"
    pinned = _skill(root, "quartz-ledger-brief", model="haiku", body="step one\nstep two\n")
    unpinned = _skill(root, "pebble-harbor-digest", model="")
    before_pinned = pinned.read_bytes()

    # 1. dry run (default) -- nothing written, table names the FOOTER action
    proc = _ps(patched, "-Root", str(root), "-InjectRunMarkerFooter")
    assert proc.returncode == 0, proc.stderr
    assert pinned.read_bytes() == before_pinned and "FOOTER" in proc.stdout

    # 2. apply
    proc = _ps(patched, "-Root", str(root), "-InjectRunMarkerFooter", "-Apply", "-NoBackup")
    assert proc.returncode == 0, proc.stderr
    after = pinned.read_text(encoding="utf-8")
    fm_re = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
    assert fm_re.match(after).group(0) == fm_re.match(before_pinned.decode("utf-8")).group(0)
    assert after.count(m.RUN_MARKER_FOOTER_MARKER) == 1
    assert "step two" in after and after.index("step two") < after.index(m.RUN_MARKER_FOOTER_MARKER)
    assert r"_runs\quartz-ledger-brief\<YYYY-MM-DD>.json" in after      # folder id substituted
    assert "<FOLDER-ID>" not in after
    for field in m.RUN_MARKER_FIELDS:
        assert field in after
    # the unpinned task got its model pin AND the footer in the same pass
    up = unpinned.read_text(encoding="utf-8")
    assert re.search(r"(?m)^model: sonnet$", up) and up.count(m.RUN_MARKER_FOOTER_MARKER) == 1
    assert r"_runs\pebble-harbor-digest\<YYYY-MM-DD>.json" in up

    # 3. idempotent: a second apply leaves both files byte-identical and reports OK
    snap = (pinned.read_bytes(), unpinned.read_bytes())
    proc = _ps(patched, "-Root", str(root), "-InjectRunMarkerFooter", "-Apply", "-NoBackup")
    assert proc.returncode == 0, proc.stderr
    assert (pinned.read_bytes(), unpinned.read_bytes()) == snap
    assert "FOOTER" not in proc.stdout.replace("run-marker footers", "")

    # 4. opt-in: without the switch a fresh file is never given the footer
    fresh = _skill(root, "velvet-orbit-sync", model="haiku")
    proc = _ps(patched, "-Root", str(root), "-Apply", "-NoBackup")
    assert proc.returncode == 0, proc.stderr
    assert m.RUN_MARKER_FOOTER_MARKER not in fresh.read_text(encoding="utf-8")
