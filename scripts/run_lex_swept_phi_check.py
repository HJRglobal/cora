"""Daily PHI re-scan net over Drive `_brain/swept/` (lex-swept-phi-check).

Defense-in-depth SECOND pass behind the materializer's inline `_phi_wall`. The wall
drops LEX/LBHS/clinical/named-billing content BEFORE writing
`_brain/swept/{ENTITY}/YYYY-MM-DD.md`; this script re-reads what was actually WRITTEN
and alerts + quarantines if any PHI slipped through (a future wall regression, a
mis-tagged chunk, or the audit the North Star promised). Same lesson as the WS17-B/C
independent pre-merge passes: an independent check finds what the producer's own
check doesn't.

NO DRIFT: the detectors are IMPORTED from `phi_guard` + `drive_materializer` — the same
functions/regexes the wall uses (`is_clinical_phi`, `is_lex_billing_status_phi`,
`_LBHS_SIGNAL_RE`, `scrub_lex_phi`/`redact_cue_adjacent_names`, `_LEX_CONTEXT_RE`,
`_lex_staff_names`). `scan_body` mirrors `_phi_wall`'s ENTITY-AWARE structure (LEX branch
vs non-LEX backstop) so it (a) never false-positives on non-LEX vendor possessives /
commercial "client billing", and (b) is a strict SUPERSET of what the wall drops — it
ALSO flags a `scrub_lex_phi` diff on a LEX file, which catches the regression case
(raw PHI written without the wall's scrub) that re-running the wall would silently
re-scrub-and-pass.

ON A HIT: quarantine + alert (entity / date / which-detector — NEVER the offending text)
+ audit log. Quarantine = rename in place to `{date}.QUARANTINED.md` inside the same
`_brain/swept/{ENTITY}/` dir. This is DELIBERATE, not `_brain/_quarantine/`: the KB-ingest
exclusion (`kb_exclusions.is_swept_path` / drive_connector) keys on a path having BOTH a
`_brain` AND a `swept` segment, so a file renamed within swept/ stays KB-EXCLUDED (the PHI
is never re-ingested), while `_brain/_quarantine/` has `_brain` but NOT `swept` and WOULD
be ingested. The `.QUARANTINED.md` suffix drops it from the `{date}.md` live-digest pattern.

CLEAN RUN: a heartbeat audit line ("N files scanned, 0 PHI"). FAIL-SOFT: a read error logs
+ is surfaced in the summary/alert (an unreadable file is "unverified", NEVER silently
passed). Script-side — no bot restart.

Usage:
  python scripts/run_lex_swept_phi_check.py [--dry-run] [--all] [--since-hours N]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Standalone script: load .env itself (D-058). The bot's load happens in app.py.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # noqa: BLE001 -- env may already be set
    pass

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# IMPORT the exact detectors the materializer wall uses -> one source of truth, no drift.
from src.cora import drive_materializer as dm  # noqa: E402
from src.cora import kb_exclusions, phi_guard  # noqa: E402

log = logging.getLogger("lex_swept_phi_check")

_DEFAULT_SINCE_HOURS = 26          # daily 07:06 run picks up the 05:45 materializer's writes
_QUARANTINE_SUFFIX = ".QUARANTINED.md"
_HARRISON_SLACK_ID = "U0B2RM2JYJ1"


# ── header strip ────────────────────────────────────────────────────────────────
# The materializer's own header lines are PHI-free boilerplate — but the LEX header
# line literally contains "LBHS (42 CFR Part 2) excluded", which the LBHS regex would
# false-match. Strip the known header lines before scanning so we scan the distilled
# body only (what the wall actually checked). If the header format ever drifts, the
# fail-safe direction is over-scan -> a benign false alarm, never a missed leak.
import re  # noqa: E402

_HEADER_LINE_RES = (
    re.compile(r"^#\s+.*swept-knowledge digest.*$", re.IGNORECASE),
    re.compile(r"^_Auto-distilled by Cora\b.*_\s*$", re.IGNORECASE),
    re.compile(r"^_LEX:.*LBHS.*excluded.*_\s*$", re.IGNORECASE),
)


def distilled_body(content: str) -> str:
    """Drop the materializer's PHI-free header boilerplate; return the distilled body."""
    return "\n".join(
        ln for ln in content.splitlines()
        if not any(rx.match(ln) for rx in _HEADER_LINE_RES)
    )


# ── detection (entity-aware; mirrors drive_materializer._phi_wall) ────────────────

@dataclass
class ScanResult:
    is_hit: bool
    detectors: list[str] = field(default_factory=list)


def scan_body(entity: str, body: str) -> ScanResult:
    """Re-scan a WRITTEN swept body for PHI using the SAME imported detectors as the
    wall. Entity-aware (LEX vs non-LEX) so it matches the wall's behavior and never
    false-positives on non-LEX vendor possessives / commercial billing. A strict
    SUPERSET of `_phi_wall` drops (adds the LEX scrub-diff regression net + flags any
    LBHS signal in any entity)."""
    e = (entity or "").strip().upper()
    detectors: list[str] = []

    if e == "LEX":
        staff = dm._lex_staff_names()
        try:
            scrubbed = phi_guard.scrub_lex_phi(body, allowed_names=staff)
            # The doubly-scrubbed text (matches _phi_wall) is used ONLY for the
            # LBHS/clinical/billing checks below -- NOT for the diff.
            double = phi_guard.redact_cue_adjacent_names(scrubbed, allowed_names=staff)
        except Exception:  # noqa: BLE001 -- fail-CLOSED, exactly like the wall
            return ScanResult(True, ["scrub_error_fail_closed"])
        # Diff on scrub_lex_phi ALONE (the spec's named detector): it redacts only
        # diagnoses/meds/DOB/client-names/possessives, so a diff means RAW PHI is in
        # the written artifact that the write-time scrub should have removed (a wall
        # regression). It is idempotent on a properly-materialized (post-scrub) file,
        # so no false positive. (redact_cue_adjacent_names is deliberately EXCLUDED
        # from the diff -- it's recall-biased and rewrites ordinary Title-case words
        # near a cue, e.g. "Key" in a "## Key facts" header, which would false-fire.)
        if scrubbed != body:
            detectors.append("scrub_lex_phi_diff")
        if dm._LBHS_SIGNAL_RE.search(double):
            detectors.append("lbhs_42cfr_part2")
        if phi_guard.is_clinical_phi(double):
            detectors.append("clinical_phi")
        if phi_guard.is_lex_billing_status_phi(double):
            detectors.append("named_billing_status_phi")
    else:
        # Non-LEX backstop (a holdco/founder digest can span Lexington via a mis-tagged
        # chunk). No scrub-diff here: scrubbing a non-LEX digest would false-positive on
        # ordinary vendor/company possessives ("Walmart's order").
        if phi_guard.is_clinical_phi(body):
            detectors.append("clinical_phi")
        if dm._LBHS_SIGNAL_RE.search(body):
            detectors.append("lbhs_42cfr_part2")
        if phi_guard.is_lex_billing_status_phi(body) and dm._LEX_CONTEXT_RE.search(body):
            detectors.append("named_billing_status_phi_lex_context")

    return ScanResult(bool(detectors), detectors)


# ── file model ────────────────────────────────────────────────────────────────

@dataclass
class FileFinding:
    path: Path
    entity: str
    date: str
    detectors: list[str]
    quarantined_to: Path | None = None


def _entity_of(path: Path) -> str:
    """`_brain/swept/{ENTITY}/{date}.md` -> ENTITY (the parent dir name)."""
    return path.parent.name


def _date_of(path: Path) -> str:
    return path.stem  # "2026-06-30.md" -> "2026-06-30"


def quarantine_file(path: Path, *, now: datetime | None = None) -> Path:
    """Rename in place to `{date}.QUARANTINED.md` (stays under the _brain/swept
    KB-exclusion, so the PHI is never re-ingested). On a same-day re-hit, append a
    timestamp so a prior quarantine is never clobbered."""
    now = now or datetime.now()
    dest = path.with_name(path.stem + _QUARANTINE_SUFFIX)
    if dest.exists():
        dest = path.with_name(f"{path.stem}.QUARANTINED.{int(now.timestamp())}.md")
    path.rename(dest)
    # Belt: the quarantined file MUST remain KB-excluded (still under _brain/swept).
    if not kb_exclusions.is_swept_path(dest):
        log.error(
            "lex-swept-phi-check: quarantined file %s is NOT KB-excluded — manual action needed",
            dest,
        )
    return dest


# ── alert (entity / date / detectors ONLY — never the PHI text) ───────────────────

def build_alert(hits: list[FileFinding], errors: list[tuple[Path, str]]) -> str:
    lines = [f":rotating_light: *LEX swept PHI check* — {len(hits)} file(s) flagged"]
    for h in hits:
        q = f"  ->  quarantined `{h.quarantined_to.name}`" if h.quarantined_to else "  (dry-run, not quarantined)"
        lines.append(f"- *{h.entity}* {h.date}: detectors `{', '.join(h.detectors)}`{q}")
    if errors:
        lines.append(f":warning: {len(errors)} file(s) could not be read (UNVERIFIED — not passed):")
        for p, _exc in errors:
            lines.append(f"- `{_entity_of(p)}/{p.name}` read error")
    lines.append("_PHI text is never included here. See logs/lex-swept-phi-check-<date>.log on the host._")
    return "\n".join(lines)


# ── run ────────────────────────────────────────────────────────────────────────

def _iter_swept_files(root: Path, *, since_hours: int, all_files: bool, now: datetime) -> list[Path]:
    out: list[Path] = []
    cutoff = now.timestamp() - since_hours * 3600
    for f in sorted(root.glob("**/*.md")):
        if f.name.endswith(_QUARANTINE_SUFFIX) or ".QUARANTINED." in f.name:
            continue  # already handled
        if not f.is_file():
            continue
        if not all_files:
            try:
                if f.stat().st_mtime < cutoff:
                    continue
            except OSError:
                pass  # can't stat -> include it (fail-safe: scan rather than skip)
        out.append(f)
    return out


def run(
    *,
    swept_root: Path | None = None,
    since_hours: int = _DEFAULT_SINCE_HOURS,
    all_files: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    alert_fn=None,
) -> dict:
    """Scan, quarantine on hit, alert. Returns a stats dict. Never raises on a single
    file (fail-soft). `alert_fn(text)` is injected (tests pass a collector); main()
    wires the real Slack send."""
    root = swept_root or dm._swept_root()
    now = now or datetime.now()
    hits: list[FileFinding] = []
    errors: list[tuple[Path, str]] = []
    scanned = 0

    if not root.exists():
        log.warning("lex-swept-phi-check: swept root %s does not exist — nothing to scan", root)
        return {"scanned": 0, "hits": [], "errors": [], "root_missing": True}

    for f in _iter_swept_files(root, since_hours=since_hours, all_files=all_files, now=now):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 -- fail-soft; NEVER silently pass
            log.error("lex-swept-phi-check: READ ERROR %s: %s (UNVERIFIED)", f, exc)
            errors.append((f, str(exc)))
            continue
        scanned += 1
        entity, date = _entity_of(f), _date_of(f)
        res = scan_body(entity, distilled_body(content))
        if not res.is_hit:
            log.info("lex-swept-phi-check: clean entity=%s file=%s", entity, f.name)
            continue
        quarantined = None
        if not dry_run:
            try:
                quarantined = quarantine_file(f, now=now)
            except Exception as exc:  # noqa: BLE001 -- a quarantine failure must still alert
                log.error("lex-swept-phi-check: quarantine FAILED for %s: %s", f, exc)
        # Audit: entity/date/detectors + quarantine target ONLY — never the body.
        log.warning(
            "lex-swept-phi-check: PHI HIT entity=%s date=%s detectors=%s quarantined=%s",
            entity, date, res.detectors, (quarantined.name if quarantined else "(dry-run/failed)"),
        )
        hits.append(FileFinding(f, entity, date, res.detectors, quarantined))

    if not hits and not errors:
        log.info("lex-swept-phi-check: %d files scanned, 0 PHI", scanned)  # heartbeat
    else:
        log.warning(
            "lex-swept-phi-check: %d scanned, %d PHI hit(s), %d read error(s)",
            scanned, len(hits), len(errors),
        )
        if (hits or errors) and not dry_run and alert_fn is not None:
            try:
                alert_fn(build_alert(hits, errors))
            except Exception as exc:  # noqa: BLE001 -- alert failure must not crash the run
                log.error("lex-swept-phi-check: alert send failed: %s", exc)

    return {"scanned": scanned, "hits": hits, "errors": errors}


# ── Slack send (standalone; raw POST + egress sanitize, the script pattern) ───────

def _post_slack(token: str, channel: str, text: str) -> None:
    import httpx
    from src.cora.slack_egress import sanitize_text  # noqa: PLC0415 -- raw POST bypasses the WebClient patch
    try:
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"channel": channel, "text": sanitize_text(text),
                  "unfurl_links": False, "unfurl_media": False},
            timeout=15,
        )
        ok = resp.json().get("ok", False)
        if not ok:
            log.error("lex-swept-phi-check: Slack post to %s failed: %s", channel, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.error("lex-swept-phi-check: Slack post to %s errored: %s", channel, exc)


def _make_alert_fn(token: str, channel: str):
    def _send(text: str) -> None:
        # DM Harrison (reliable) + the security/health channel.
        _post_slack(token, _HARRISON_SLACK_ID, text)
        if channel:
            _post_slack(token, channel, text)
    return _send


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily PHI re-scan over _brain/swept/.")
    ap.add_argument("--dry-run", action="store_true", help="Scan + report only; no quarantine, no alert.")
    ap.add_argument("--all", action="store_true", help="Scan ALL swept files (not just the last ~26h).")
    ap.add_argument("--since-hours", type=int, default=_DEFAULT_SINCE_HOURS, help="Modified-within window.")
    args = ap.parse_args()

    log_dir = _REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"lex-swept-phi-check-{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(
                open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
            ),
        ],
    )

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = os.environ.get("PHI_CHECK_ALERT_CHANNEL", "cora-health")
    alert_fn = None
    if args.dry_run:
        log.info("lex-swept-phi-check: DRY RUN — no quarantine, no alert.")
    elif token:
        alert_fn = _make_alert_fn(token, channel)
    else:
        log.warning("lex-swept-phi-check: SLACK_BOT_TOKEN not set — alerts will not send (still quarantining + logging).")

    stats = run(
        since_hours=args.since_hours,
        all_files=args.all,
        dry_run=args.dry_run,
        alert_fn=alert_fn,
    )
    # Exit nonzero when something needs a human (PHI hit or an unverified read error) so
    # a wrapping monitor can react; a clean run exits 0.
    return 1 if (stats.get("hits") or stats.get("errors")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
