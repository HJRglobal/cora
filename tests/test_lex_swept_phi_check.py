"""Tests for scripts/run_lex_swept_phi_check.py -- the daily PHI re-scan net over
_brain/swept/.

Gate (spec section 15):
  - Planted clinical-PHI swept file -> detected + quarantined + alert (NO PHI text).
  - Planted named-billing-in-Lexington-context file -> detected.
  - Clean digest -> passes, no quarantine, heartbeat (no alert).
  - Detector parity with drive_materializer._phi_wall (same imported functions; the
    scanner is a strict SUPERSET of what the wall drops).
Plus: the LEX-header false-positive guard, read-error-never-silently-passed,
already-quarantined skip, and quarantine-stays-KB-excluded.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "src"))

import scripts.run_lex_swept_phi_check as mod  # noqa: E402
from src.cora import drive_materializer as dm  # noqa: E402
from src.cora import kb_exclusions, phi_guard  # noqa: E402

_NOW = datetime(2026, 6, 30, 7, 6, 0)


def _swept_root(tmp_path: Path) -> Path:
    """A tmp swept root mirroring production (`.../_brain/swept/`) so the KB-exclusion
    guard (kb_exclusions.is_swept_path, keyed on _brain+swept segments) behaves live."""
    r = tmp_path / "_brain" / "swept"
    r.mkdir(parents=True, exist_ok=True)
    return r


def _write(root: Path, entity: str, date: str, body: str, *, lex_header: bool = False) -> Path:
    """Write a materializer-style swept file (header + body)."""
    d = root / entity
    d.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {entity} — swept-knowledge digest — {date}\n\n"
        f"_Auto-distilled by Cora from the day's swept activity (gmail:3). "
        f"Distilled signal only; see the source systems for detail._\n"
    )
    if lex_header:
        header += "_LEX: GM-level / aggregate / PHI-scrubbed. LBHS (42 CFR Part 2) excluded._\n"
    p = d / f"{date}.md"
    p.write_text(header + "\n" + body.strip() + "\n", encoding="utf-8")
    return p


class _Collector:
    def __init__(self):
        self.alerts: list[str] = []

    def __call__(self, text: str) -> None:
        self.alerts.append(text)


def test_planted_clinical_phi_detected_quarantined_alert_clean(tmp_path):
    root = _swept_root(tmp_path)
    p = _write(root, "LEX", "2026-06-30",
               "## Key facts & updates\n- A client was diagnosed with autism and is now prescribed risperidone.\n",
               lex_header=True)
    col = _Collector()
    stats = mod.run(swept_root=root, all_files=True, now=_NOW, alert_fn=col)

    assert len(stats["hits"]) == 1
    assert stats["hits"][0].entity == "LEX"
    assert not p.exists()                                      # original quarantined
    q = root / "LEX" / "2026-06-30.QUARANTINED.md"
    assert q.exists()
    assert kb_exclusions.is_swept_path(q) is True              # stays KB-excluded
    assert len(col.alerts) == 1
    alert = col.alerts[0].lower()
    assert "lex" in alert and "2026-06-30" in alert
    for phi in ("risperidone", "autism", "diagnosed"):
        assert phi not in alert                                # NEVER the PHI text


def test_planted_named_billing_in_lex_context_detected(tmp_path):
    root = _swept_root(tmp_path)
    _write(root, "HJRG", "2026-06-30",
           "## Notable communications\n- AHCCCS reconciliation: client Bob Smith's billing authorization is pending.\n")
    col = _Collector()
    stats = mod.run(swept_root=root, all_files=True, now=_NOW, alert_fn=col)
    assert len(stats["hits"]) == 1
    assert "named_billing_status_phi_lex_context" in stats["hits"][0].detectors
    assert "Bob Smith" not in col.alerts[0]


def test_clean_digest_passes_no_quarantine_no_alert(tmp_path, caplog):
    root = _swept_root(tmp_path)
    _write(root, "F3E", "2026-06-30",
           "## Decisions\n- Locked the Q3 retail deck with Tommy and Larry.\n"
           "## Action items / follow-ups\n- Walmart's PO ships Friday; Sprouts' sample due.\n")
    col = _Collector()
    with caplog.at_level(logging.INFO, logger="lex_swept_phi_check"):
        stats = mod.run(swept_root=root, all_files=True, now=_NOW, alert_fn=col)
    assert stats["hits"] == [] and stats["errors"] == []
    assert stats["scanned"] == 1
    assert col.alerts == []
    assert (root / "F3E" / "2026-06-30.md").exists()           # not quarantined
    assert any("0 PHI" in r.message for r in caplog.records)   # heartbeat


def test_non_lex_vendor_possessives_not_false_positive(tmp_path):
    root = _swept_root(tmp_path)
    _write(root, "OSN", "2026-06-30",
           "## Key facts & updates\n- Walmart's reorder, Matt's recon, client billing portal updated.\n")
    stats = mod.run(swept_root=root, all_files=True, now=_NOW)
    assert stats["hits"] == []


def test_lex_header_line_not_a_false_positive(tmp_path):
    # Clean LEX body whose materializer header literally says "LBHS (42 CFR Part 2)
    # excluded" -> must be CLEAN (header stripped before scan; scrub_lex_phi-only diff
    # does not rewrite the "## Key facts" header words near the DTA cue).
    root = _swept_root(tmp_path)
    p = _write(root, "LEX", "2026-06-30",
               "## Key facts & updates\n- DTA staffing and van logistics on track; one program audit closed.\n",
               lex_header=True)
    stats = mod.run(swept_root=root, all_files=True, now=_NOW)
    assert stats["hits"] == []
    assert p.exists()


def test_distilled_body_strips_lbhs_header():
    content = (
        "# LEX — swept-knowledge digest — 2026-06-30\n\n"
        "_Auto-distilled by Cora from the day's swept activity (gmail:2)._\n"
        "_LEX: GM-level / aggregate / PHI-scrubbed. LBHS (42 CFR Part 2) excluded._\n\n"
        "## Decisions\n- Clean body line.\n"
    )
    stripped = mod.distilled_body(content)
    assert "LBHS" not in stripped and "42 CFR" not in stripped
    assert "Clean body line." in stripped


def test_scanner_superset_of_phi_wall_drops():
    cases = [
        ("LEX", "Program update mentions BHRF intake coordination."),          # LBHS signal survives scrub
        ("HJRG", "A member was prescribed risperidone this week."),            # clinical, non-LEX
        ("FNDR", "AHCCCS: client Bob's billing authorization is pending."),    # billing + LEX context, non-LEX
    ]
    for entity, body in cases:
        assert dm._phi_wall(entity, body) is None, f"precondition: wall should drop {entity}:{body!r}"
        assert mod.scan_body(entity, body).is_hit, f"scanner missed a wall-drop for {entity}:{body!r}"


def test_drift_guard_same_detector_objects():
    assert mod.phi_guard is phi_guard
    assert mod.dm is dm
    assert mod.dm._LBHS_SIGNAL_RE is dm._LBHS_SIGNAL_RE
    assert mod.dm._LEX_CONTEXT_RE is dm._LEX_CONTEXT_RE


def test_read_error_surfaced_not_silently_passed(tmp_path):
    root = _swept_root(tmp_path)
    _write(root, "F3E", "2026-06-30", "## Decisions\n- clean.\n")
    bad = root / "F3E" / "2026-06-29.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")      # read_text(utf-8) raises
    col = _Collector()
    stats = mod.run(swept_root=root, all_files=True, now=_NOW, alert_fn=col)
    assert len(stats["errors"]) == 1
    assert stats["errors"][0][0] == bad
    assert len(col.alerts) == 1                                # unreadable -> alert, never silent
    assert "read error" in col.alerts[0].lower()


def test_already_quarantined_skipped(tmp_path):
    root = _swept_root(tmp_path)
    d = root / "LEX"
    d.mkdir(parents=True)
    (d / "2026-06-28.QUARANTINED.md").write_text(
        "## x\n- A client diagnosed with autism.\n", encoding="utf-8")
    stats = mod.run(swept_root=root, all_files=True, now=_NOW)
    assert stats["scanned"] == 0 and stats["hits"] == []


def test_dry_run_no_quarantine_no_alert(tmp_path):
    root = _swept_root(tmp_path)
    p = _write(root, "LEX", "2026-06-30",
               "## x\n- A client diagnosed with autism, on risperidone.\n", lex_header=True)
    col = _Collector()
    stats = mod.run(swept_root=root, all_files=True, now=_NOW, dry_run=True, alert_fn=col)
    assert len(stats["hits"]) == 1                             # still detected
    assert p.exists()                                          # NOT quarantined
    assert col.alerts == []                                    # NOT alerted
    assert stats["hits"][0].quarantined_to is None
