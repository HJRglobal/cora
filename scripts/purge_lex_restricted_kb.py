#!/usr/bin/env python3
"""Purge already-ingested restricted-LEX (LBHS/LTS) PHI-content KB chunks (W6-01, Fix-A / D-073).

Pairs with the ingest-time drop (store.upsert_documents Step 0a + lex_sub_entity.
restricted_lex_phi_content_drop): the drop stops FUTURE gmail/drive_sweep LBHS/LTS PHI
ingestion; this removes the PHI already in the KB.

W6-01 Fix-A (2026-07-06, Harrison): NARROWED from the D-072 broad by-tag purge to PHI-CONTENT
only. LBHS/LTS BUSINESS chunks (payroll / fees / PTO / aggregate "client billing" -- NOT
patient records, NOT 42-CFR-Part-2) are KEPT + retrievable; only chunks whose title+content
trip the W2-01 live PHI predicate (phi_guard.non_lex_phi_backstop_trips_live) are deleted.
Verified against the live corpus: of ~1,800 tagged chunks only ~42 carry PHI content.

SCOPE:
  DEFAULT  = sub_entity IN (LEX-LBHS, LEX-LTS) AND source IN (gmail, drive_sweep) AND PHI-content
             -- the deny-list gap (42 CFR Part 2 / PT-15 PHI that slipped in via the non-Slack
             sweeps). Business chunks in scope are KEPT.
  NON-default-source rows (static_md session-captures, #lex/#shaun-leadership slack threads,
  the drive_asset "LBHS unpaid management fees" business file) are REPORTED (flagged PHI vs
  business) but not deleted by default. Widen only on Harrison's explicit decision:
      --include-source slack --include-source static_md --include-source drive_asset
      --all-sources          # every PHI-content LBHS/LTS chunk regardless of source
  A widened scope still deletes ONLY the PHI-content chunks; business is always kept.

Reversibility: --apply first writes a full row-backup (chunk_id + all columns) to
data/purge-lex-restricted-<UTC>.bak.jsonl BEFORE deleting, so the deleted rows can be
audited / re-inserted (vectors restore by re-ingest from source). Belt: copy cora_kb.db
before --apply.

Usage (--dry-run is read-only + safe anytime; STOP Cora before --apply):
    .venv\\Scripts\\python.exe scripts\\purge_lex_restricted_kb.py                 # dry-run
    .venv\\Scripts\\python.exe scripts\\purge_lex_restricted_kb.py --apply         # delete (Cora stopped)
    .venv\\Scripts\\python.exe scripts\\purge_lex_restricted_kb.py --all-sources   # widen scope
After --apply, reclaim disk: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py

Exit codes: 0 ok, 1 fatal, 3 Cora appears to be running (--apply refused; pass --force).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora import phi_guard  # noqa: E402
from cora.knowledge_base import schema  # noqa: E402
from cora.knowledge_base.lex_sub_entity import (  # noqa: E402
    RESTRICTED_INGEST_SOURCES,
    RESTRICTED_INGEST_SUB_ENTITIES,
)

KB_DB_PATH = _REPO / "data" / "cora_kb.db"
HEARTBEAT_PATH = _REPO / "data" / "health" / "heartbeat.txt"
_BATCH = 500


def _heartbeat_is_fresh(max_age_s: int = 180) -> bool:
    """True if the live bot's heartbeat was written < max_age_s ago (service running).
    Mirrors prune_kb_retention.py / migrate_kb_binary_index.py so the destructive-KB
    scripts share one running-bot guard."""
    try:
        return (time.time() - HEARTBEAT_PATH.stat().st_mtime) < max_age_s
    except OSError:
        return False


def _lex_staff_names() -> set[str]:
    """org-roles staff roster to PRESERVE in the PHI-content decision (so a staff possessive
    isn't read as a care recipient). Fail-soft to empty (err toward purging)."""
    try:
        from cora import org_roles
        return {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
    except Exception:  # noqa: BLE001
        return set()


def _is_phi(title, content, staff) -> bool:
    """The SAME PHI-content decision the ingest drop uses (W6-01 Fix-A / D-073): the TAG-SCOPED
    individual-gated predicate over title+content. Business chunks return False (kept). Must
    match store.restricted_lex_phi_content_drop's predicate so ingest-drop and purge agree."""
    return phi_guard.non_lex_phi_backstop_trips_individual(
        (title or "") + " " + (content or ""), allowed_names=staff
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("purge-lex-restricted-kb")


def _tag_placeholders() -> str:
    return ",".join("?" * len(RESTRICTED_INGEST_SUB_ENTITIES))


# PER-CHUNK evaluation (D-051 re-gate finding 3, resolved 2026-07-06): the purge mirrors the
# PER-CHUNK ingest drop (store.upsert_documents Step 1a) -- a chunk is purged only when THAT
# chunk's own title+content carries PHI. Verify-first rejected whole-doc grouping: on the live
# corpus it over-purged large mixed BUSINESS docs (a 131-chunk "Weekly Cash Flow Standing
# ACTUAL", P&L, tracking spreadsheets) whose joined 500KB trips the billing leg on
# billing-words + "Lexington" + some name in unrelated rows -- the exact business over-drop
# Harrison's narrowing forbids. Per-chunk keeps a business chunk unless a client name +
# billing/dx co-occur LOCALLY; the non-identifying boilerplate chunks of a clinical doc that
# survive are guarded at retrieval by W2-01 + the strict Drive-egress predicate.

def phi_breakdown(conn, staff) -> dict:
    """Read-only. Per (sub_entity, source): (total, phi_count). Business = total - phi.
    W6-01 Fix-A: the purge deletes only PHI-content chunks (per-chunk); business is KEPT."""
    from collections import Counter
    tag_ph = _tag_placeholders()
    rows = conn.execute(
        f"SELECT sub_entity, source, title, content FROM knowledge_chunks "
        f"WHERE sub_entity IN ({tag_ph})",
        RESTRICTED_INGEST_SUB_ENTITIES,
    )
    tot: Counter = Counter()
    phi: Counter = Counter()
    for sub, src, title, content in rows:
        tot[(sub, src)] += 1
        if _is_phi(title, content, staff):
            phi[(sub, src)] += 1
    return {"total": tot, "phi": phi}


def target_chunk_ids(conn, sources: tuple[str, ...] | None, staff) -> set[str]:
    """Read-only. chunk_ids for LBHS/LTS chunks in *sources* (None = all sources) whose OWN
    title+content is PHI (W6-01 Fix-A / D-073, per-chunk -- mirrors the ingest drop). Business
    chunks are NOT targeted (kept)."""
    tag_ph = _tag_placeholders()
    if sources is None:
        q = (f"SELECT chunk_id, title, content FROM knowledge_chunks "
             f"WHERE sub_entity IN ({tag_ph})")
        params: tuple = tuple(RESTRICTED_INGEST_SUB_ENTITIES)
    else:
        src_ph = ",".join("?" * len(sources))
        q = (f"SELECT chunk_id, title, content FROM knowledge_chunks "
             f"WHERE sub_entity IN ({tag_ph}) AND source IN ({src_ph})")
        params = (*RESTRICTED_INGEST_SUB_ENTITIES, *sources)
    return {cid for cid, title, content in conn.execute(q, params)
            if _is_phi(title, content, staff)}


def non_default_rows(conn, staff) -> list[tuple]:
    """Read-only. LBHS/LTS chunks OUTSIDE the default (gmail/drive_sweep) source scope, each
    flagged PHI vs business (per-chunk) -- the ones a widened purge would ADDITIONALLY
    consider (a widened purge still deletes only the PHI chunks). For Harrison review."""
    tag_ph = _tag_placeholders()
    src_ph = ",".join("?" * len(RESTRICTED_INGEST_SOURCES))
    rows = conn.execute(
        f"SELECT sub_entity, source, title, source_id, content FROM knowledge_chunks "
        f"WHERE sub_entity IN ({tag_ph}) AND source NOT IN ({src_ph}) "
        f"ORDER BY source, sub_entity",
        (*RESTRICTED_INGEST_SUB_ENTITIES, *RESTRICTED_INGEST_SOURCES),
    ).fetchall()
    return [(sub, src, title, sid, _is_phi(title, content, staff))
            for sub, src, title, sid, content in rows]


def backup_rows(conn, chunk_ids: set[str], out_path: Path) -> int:
    """Write full deleted-row content to a JSONL backup BEFORE deletion (reversible-audit)."""
    ids = list(chunk_ids)
    written = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for i in range(0, len(ids), _BATCH):
            batch = ids[i:i + _BATCH]
            ph = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT chunk_id, source, source_id, entity, sub_entity, date_created, "
                f"date_modified, author, title, content, deep_link, metadata, ingested_at "
                f"FROM knowledge_chunks WHERE chunk_id IN ({ph})",
                batch,
            ).fetchall()
            cols = ["chunk_id", "source", "source_id", "entity", "sub_entity",
                    "date_created", "date_modified", "author", "title", "content",
                    "deep_link", "metadata", "ingested_at"]
            for row in rows:
                fh.write(json.dumps(dict(zip(cols, row)), ensure_ascii=False) + "\n")
                written += 1
    return written


def delete_chunks(conn, chunk_ids: set[str]) -> dict:
    """Batched delete from all 3 tables. Returns rows deleted per table."""
    totals = {"knowledge_vec_bin": 0, "knowledge_vec_f32": 0, "knowledge_chunks": 0}
    ids = list(chunk_ids)
    for i in range(0, len(ids), _BATCH):
        batch = ids[i:i + _BATCH]
        ph = ",".join("?" * len(batch))
        for tbl in ("knowledge_vec_bin", "knowledge_vec_f32", "knowledge_chunks"):
            cur = conn.execute(f"DELETE FROM {tbl} WHERE chunk_id IN ({ph})", batch)
            totals[tbl] += cur.rowcount
    conn.commit()
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge restricted-LEX (LBHS/LTS) KB chunks (W6-01).")
    ap.add_argument("--apply", action="store_true", help="Delete (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (this is the default).")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--include-source", action="append", default=[],
                    help="Add a source beyond the default gmail/drive_sweep (repeatable): "
                         "slack | static_md | drive_asset.")
    ap.add_argument("--all-sources", action="store_true",
                    help="Purge EVERY LBHS/LTS chunk regardless of source (true 0-remaining).")
    ap.add_argument("--force", action="store_true",
                    help="Skip the heartbeat (Cora-running) safety guard.")
    args = ap.parse_args()
    apply_changes = args.apply and not args.dry_run

    # Running-bot guard: never delete rows out from under the live service. delete_chunks
    # holds the WAL write lock across the batched loop; a concurrent bot writer would block
    # (busy_timeout) then raise. Refuse --apply while the heartbeat is fresh (D-051 finding 6).
    if apply_changes and _heartbeat_is_fresh() and not args.force:
        log.error("Cora heartbeat is fresh (<180s) -- the service appears to be RUNNING. "
                  "Stop the cowork-cora-service task first, or pass --force.")
        return 3

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1

    if args.all_sources:
        sources: tuple[str, ...] | None = None
    else:
        sources = tuple(RESTRICTED_INGEST_SOURCES) + tuple(
            s.strip() for s in args.include_source if s.strip()
        )

    staff = _lex_staff_names()
    conn = schema.connect(db_path)
    try:
        log.info("=== Restricted-LEX (LBHS/LTS) PHI-content purge (W6-01 Fix-A / D-073) ===")
        log.info("  staff roster loaded: %d name(s)", len(staff))
        if not staff:
            log.warning("  staff roster is EMPTY -- staff-attributed business (e.g. "
                        "'<staff>'s billing') may be MIS-FLAGGED as PHI and over-counted. "
                        "Confirm org-roles.yaml loads before trusting the manifest / --apply.")
        log.info("  LBHS/LTS population by (sub_entity, source) -- total / PHI(purge) / business(KEEP):")
        bd = phi_breakdown(conn, staff)
        for key in sorted(bd["total"]):
            sub, src = key
            tot = bd["total"][key]; phi = bd["phi"][key]
            log.info("      %-10s %-12s  total=%-5d PHI=%-4d business=%d", sub, src, tot, phi, tot - phi)

        ids = target_chunk_ids(conn, sources, staff)
        scope_label = "ALL sources" if sources is None else ", ".join(sources)
        log.info("  Purge scope = sources[%s], PHI-content only -> %d chunk(s) targeted "
                 "(business chunks in scope are KEPT)", scope_label, len(ids))

        # Always list the NON-default rows — the widest scope (--all-sources) is the MOST
        # destructive and must be the MOST transparent, not silent (D-051 finding 7). Each is
        # flagged PHI vs business; a widened purge still deletes only the PHI ones.
        nd = non_default_rows(conn, staff)
        if nd:
            in_scope_extra = {s.strip() for s in args.include_source if s.strip()}
            log.info("  --- NON-default-source LBHS/LTS rows (REVIEW) ---")
            for sub, src, title, sid, is_phi in nd:
                widened = sources is None or src in in_scope_extra
                mark = "WILL PURGE" if (widened and is_phi) else ("kept-business" if not is_phi else "kept-out-of-scope")
                log.info("      [%s] %-10s %-11s | %s", mark, sub, src, (title or "(untitled)")[:66])

        if not apply_changes:
            log.info("Dry-run -- nothing deleted. Re-run with --apply (Cora STOPPED). "
                     "Harrison-gated: review the PHI/business breakdown + NON-default rows above first.")
            return 0
        if not ids:
            log.info("Nothing to delete for this scope (no PHI-content chunks).")
            return 0

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = _REPO / "data" / f"purge-lex-restricted-{ts}.bak.jsonl"
        n_bak = backup_rows(conn, ids, bak)
        log.info("Backed up %d row(s) to %s (reversible-audit) before delete.", n_bak, bak)

        log.info("Deleting %d PHI-content chunks from knowledge_chunks + both vec tables...", len(ids))
        totals = delete_chunks(conn, ids)
        log.info("Deleted: %s", totals)

        remaining = len(target_chunk_ids(conn, None, staff))
        log.info("PHI-content LBHS/LTS chunks remaining (all sources): %d (business KEPT)", remaining)
        log.info("Reclaim disk with: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py")
    except Exception as exc:  # noqa: BLE001
        log.error("purge failed: %s", exc, exc_info=True)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
