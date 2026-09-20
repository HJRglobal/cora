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
