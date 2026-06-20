"""Drive structured fact extractor — Builds 2 & 3 of the Drive Intelligence layer.

Build 2: run_extraction()
    Reads recently-ingested drive_sweep KB chunks, sends each file's content to
    Claude Haiku for structured extraction, and stores facts in drive_extracted_facts.

Build 3: run_proposal_loop()
    Reads high-confidence facts from drive_extracted_facts and converts them into
    knowledge_review.propose_update() calls gated by Harrison's 👍/👎.

Fact types extracted:
    person    — named individual mentioned in document context
    company   — external company, vendor, or counterparty
    deal      — deal, contract, agreement, or transaction reference
    project   — project, initiative, or workstream reference
    decision  — explicit decision recorded in the document
    amount    — dollar amount, financial figure, or metric

PHI guardrail: LEX chunks excluded entirely.
Visibility CPA exclusion: Hayden Greber / Andrew Stubbs / Sarah Bertoglio /
Emily Stubbs / Michael DiBenedetto / Andrew Lee never in facts or proposals.
Harrison sole-authority doctrine: this module NEVER writes to decisions.md,
Asana, or HubSpot. All output is proposals via knowledge_review.propose_update().
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """You are a structured data extractor for a portfolio of businesses.

Extract key facts from the document excerpt below. Return ONLY a JSON object with one key:
  "facts": list of fact objects

Each fact object has exactly these fields:
  "fact_type":  one of "person" | "company" | "deal" | "project" | "decision" | "amount"
  "subject":    the name/label of the fact (person name, company name, deal name, etc.)
  "detail":     one-sentence description of what the document says about the subject
  "confidence": "HIGH" | "MED" | "LOW"

Rules:
- Extract only facts EXPLICITLY stated in the document. No inferences.
- Exclude anything involving client PHI, diagnoses, care plans, or health data.
- Exclude these individuals: Hayden Greber, Andrew Stubbs, Sarah Bertoglio, Emily Stubbs,
  Michael DiBenedetto, Andrew Lee (Visibility CPA team).
- Maximum 5 facts per document. Prefer HIGH-confidence, specific facts.
- "amount" facts must include the dollar figure or metric in the detail field.
- If the document has no extractable facts, return {"facts": []}.

Return ONLY the JSON object. No preamble. No explanation.
"""

# Watermark keys
_WATERMARK_EXTRACT = "drive_extractor"
_WATERMARK_PROPOSE = "drive_extractor_proposals"

# Proposal lookback: facts extracted in the last N seconds
_PROPOSAL_LOOKBACK_SECONDS = 7 * 86400  # 7 days

# Per-run proposal cap (WS17-B item 2). A single backfill once extracted ~17k
# facts and proposed every one in a single run, flooding the review ledger. Cap
# the number of NEW proposals per run; when the cap bites we do NOT advance the
# proposal watermark, so the remainder is picked up (deduped) on the next run
# rather than silently dropped. Env-overridable for backfills.
_MAX_PROPOSALS_PER_RUN = int(os.environ.get("DRIVE_EXTRACTOR_MAX_PROPOSALS_PER_RUN", "50"))

# Minimum content length to bother extracting
_MIN_CONTENT_CHARS = 50

# Max content chars fed to Haiku per file
_MAX_CONTENT_CHARS = 2000

# Max chunks per source_id (same file can have multiple chunks)
_MAX_CHUNKS_PER_FILE = 3

# Fact types that map to knowledge_review update types
_FACT_TYPE_TO_UPDATE_TYPE = {
    "decision": "UPDATE_TYPE_DECISION",
    "project":  "UPDATE_TYPE_ASANA_TASK",
    "deal":     "UPDATE_TYPE_HUBSPOT_NOTE",
    "company":  "UPDATE_TYPE_HUBSPOT_NOTE",
    "person":   "UPDATE_TYPE_GENERIC",
    "amount":   None,  # amounts are skipped in the proposal loop
}

# Visibility CPA exclusion — centralized in phi_guard
from cora.phi_guard import VISIBILITY_CPA_NAMES as _VIS_CPA_NAMES_LOWER, is_visibility_cpa_mention as _is_vis_cpa

# ── DB helpers ─────────────────────────────────────────────────────────────────

def _default_db_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "cora_kb.db"


def ensure_facts_table(db_path: Path | str) -> None:
    """Idempotently create the drive_extracted_facts table and indexes."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drive_extracted_facts (
            fact_id        TEXT PRIMARY KEY,
            source_id      TEXT NOT NULL,
            entity         TEXT NOT NULL,
            sub_entity     TEXT,
            fact_type      TEXT NOT NULL,
            subject        TEXT NOT NULL,
            detail         TEXT NOT NULL,
            confidence     TEXT NOT NULL,
            extracted_at   INTEGER NOT NULL,
            metadata       TEXT DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_def_entity ON drive_extracted_facts(entity)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_def_extracted_at ON drive_extracted_facts(extracted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_def_fact_type ON drive_extracted_facts(fact_type)"
    )
    conn.commit()
    conn.close()


def _fact_id(source_id: str, fact_type: str, subject: str) -> str:
    """Content-addressed MD5 ID — same fact from same source always gets same ID."""
    key = f"{source_id}|{fact_type}|{subject.lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


def _store_facts(
    facts: list[dict],
    source_id: str,
    entity: str,
    sub_entity: str | None,
    db_path: Path | str,
) -> int:
    """Upsert facts into drive_extracted_facts. Returns number of new rows stored."""
    if not facts:
        return 0

    now_ts = int(time.time())
    rows = []
    for f in facts:
        fact_type = f.get("fact_type", "")
        subject = (f.get("subject") or "").strip()
        detail = (f.get("detail") or "").strip()
        confidence = f.get("confidence", "MED")

        if not fact_type or not subject or not detail:
            continue
        if confidence not in ("HIGH", "MED", "LOW"):
            confidence = "MED"

        fid = _fact_id(source_id, fact_type, subject)
        rows.append((fid, source_id, entity, sub_entity, fact_type, subject, detail, confidence, now_ts, "{}"))

    if not rows:
        return 0

    conn = sqlite3.connect(str(db_path))
    result = conn.executemany(
        """
        INSERT INTO drive_extracted_facts
            (fact_id, source_id, entity, sub_entity, fact_type, subject, detail, confidence, extracted_at, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fact_id) DO UPDATE SET
            detail       = excluded.detail,
            confidence   = excluded.confidence,
            extracted_at = excluded.extracted_at
        """,
        rows,
    )
    stored = result.rowcount
    conn.commit()
    conn.close()
    return stored if stored is not None else len(rows)


# ── Build 2: Extraction ────────────────────────────────────────────────────────

def extract_facts_for_file(
    anthropic_client: Any,
    source_id: str,
    filename: str,
    entity: str,
    sub_entity: str | None,
    content: str,
) -> list[dict]:
    """Send one file's content to Haiku and return extracted fact dicts.

    Returns empty list if content is too short, entity is LEX, or Haiku fails.
    """
    # PHI guardrail — skip LEX entities entirely
    if entity.startswith("LEX"):
        return []

    content = content.strip()
    if len(content) < _MIN_CONTENT_CHARS:
        return []

    # Truncate to Haiku budget
    excerpt = content[:_MAX_CONTENT_CHARS]

    # Check for Visibility CPA names
    if _is_vis_cpa(excerpt):
        log.debug("drive_extractor: skipping %s -- Visibility CPA name detected", filename)
        return []

    user_msg = f"FILENAME: {filename}\nENTITY: {entity}\n\n{excerpt}"

    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=_EXTRACTION_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = (resp.content[0].text or "").strip()
    except Exception as exc:
        log.error("drive_extractor: Haiku call failed for %s: %s", filename, exc)
        return []

    # Strip markdown code fences if the model wrapped its JSON output
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(raw)
        facts = parsed.get("facts") or []
    except (ValueError, TypeError):
        log.warning("drive_extractor: non-JSON from Haiku for %s: %.80s", filename, raw)
        return []

    # Filter out any Visibility CPA facts that slipped through
    clean: list[dict] = []
    for f in facts:
        if _is_vis_cpa(f.get("subject") or ""):
            continue
        clean.append(f)

    return clean


def run_extraction(
    anthropic_client: Any,
    *,
    db_path: Path | str | None = None,
    lookback_days: int = 7,
    backfill: bool = False,
    dry_run: bool = False,
) -> dict:
    """Main Build 2 loop: extract structured facts from recent Drive KB chunks.

    Returns dict with: files_processed, facts_extracted, facts_stored, errors.
    """
    db = Path(db_path) if db_path else _default_db_path()
    stats = {"files_processed": 0, "facts_extracted": 0, "facts_stored": 0, "errors": 0}

    # Ensure facts table exists
    try:
        ensure_facts_table(db)
    except Exception as exc:
        log.error("drive_extractor: could not create facts table: %s", exc)
        stats["errors"] += 1
        return stats

    # Determine watermark
    lookback_cutoff = int(time.time()) - lookback_days * 86400
    last_run_ts: int = 0

    if not backfill:
        try:
            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT last_sync_at FROM sync_state WHERE source = ?",
                (_WATERMARK_EXTRACT,),
            ).fetchone()
            conn.close()
            if row and isinstance(row[0], int) and row[0] > 0:
                last_run_ts = row[0]
        except Exception:
            pass

    cutoff_ts = max(lookback_cutoff, last_run_ts)
    log.info(
        "drive_extractor: extracting chunks ingested since %s (backfill=%s, dry_run=%s)",
        cutoff_ts, backfill, dry_run,
    )

    # Fetch recent drive_sweep chunks, excluding LEX
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT source_id, entity, sub_entity, content, metadata
            FROM knowledge_chunks
            WHERE source = 'drive_sweep'
              AND ingested_at >= ?
            ORDER BY source_id, ingested_at DESC
            """,
            (cutoff_ts,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        log.error("drive_extractor: DB query failed: %s", exc)
        stats["errors"] += 1
        return stats

    if not rows:
        log.info("drive_extractor: no recent drive_sweep chunks found")
        return stats

    # Group by source_id (file), taking first N chunks per file
    files: dict[str, dict] = {}
    for row in rows:
        source_id = row["source_id"]
        entity = row["entity"] or "FNDR"
        if entity.startswith("LEX"):
            continue  # PHI guardrail
        if source_id not in files:
            try:
                meta = json.loads(row["metadata"] or "{}")
            except (ValueError, TypeError):
                meta = {}
            files[source_id] = {
                "entity": entity,
                "sub_entity": row["sub_entity"],
                "filename": meta.get("filename") or source_id,
                "chunks": [],
            }
        if len(files[source_id]["chunks"]) < _MAX_CHUNKS_PER_FILE:
            files[source_id]["chunks"].append(row["content"] or "")

    log.info("drive_extractor: %d unique files to process", len(files))

    run_start = int(time.time())

    for source_id, file_data in files.items():
        content = " ".join(c for c in file_data["chunks"] if c).strip()
        if not content:
            continue

        stats["files_processed"] += 1
        entity = file_data["entity"]
        sub_entity = file_data["sub_entity"]
        filename = file_data["filename"]

        facts = extract_facts_for_file(
            anthropic_client, source_id, filename, entity, sub_entity, content
        )
        stats["facts_extracted"] += len(facts)

        if facts and not dry_run:
            stored = _store_facts(facts, source_id, entity, sub_entity, db)
            stats["facts_stored"] += stored
        elif facts and dry_run:
            log.info("drive_extractor [DRY RUN]: would store %d facts for %s", len(facts), filename)
            stats["facts_stored"] += len(facts)  # log as-would-store

    # Advance watermark
    if not dry_run:
        try:
            conn = sqlite3.connect(str(db))
            conn.execute(
                """
                INSERT INTO sync_state (source, last_sync_at, last_source_modified)
                VALUES (?, ?, NULL)
                ON CONFLICT(source) DO UPDATE SET last_sync_at = excluded.last_sync_at
                """,
                (_WATERMARK_EXTRACT, run_start),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("drive_extractor: could not advance watermark: %s", exc)

    log.info(
        "drive_extractor done: files=%d facts_extracted=%d facts_stored=%d errors=%d",
        stats["files_processed"], stats["facts_extracted"],
        stats["facts_stored"], stats["errors"],
    )
    return stats


# ── Build 3: Proposal loop ─────────────────────────────────────────────────────

def _build_proposed_action(
    fact_type: str, subject: str, detail: str, entity: str, source_id: str
) -> str:
    if fact_type == "decision":
        return f"Capture decision in decisions.md: [{entity}] {subject} — {detail[:120]}"
    if fact_type == "project":
        return f"Create or update Asana task: [{entity}] {subject}"
    if fact_type in ("deal", "company"):
        return f"Review HubSpot for deal/company: {subject} — {detail[:120]}"
    if fact_type == "person":
        return f"Confirm contact record for: {subject} ({entity}) — {detail[:80]}"
    return f"Review fact: {subject} — {detail[:120]}"


def run_proposal_loop(
    *,
    db_path: Path | str | None = None,
    dry_run: bool = False,
) -> dict:
    """Build 3: Convert high-confidence facts into Harrison-gated knowledge proposals.

    Reads facts from drive_extracted_facts that were extracted since the last
    proposal watermark, filters to HIGH/MED non-LEX non-amount facts, and
    calls knowledge_review.propose_update() for each.

    Returns dict with: proposed, skipped, errors.
    """
    from cora.knowledge_review import propose_update  # local import for testability

    db = Path(db_path) if db_path else _default_db_path()
    stats = {"proposed": 0, "skipped": 0, "errors": 0}

    # Determine proposal watermark
    cutoff_ts = int(time.time()) - _PROPOSAL_LOOKBACK_SECONDS
    last_propose_ts: int = 0

    try:
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT last_sync_at FROM sync_state WHERE source = ?",
            (_WATERMARK_PROPOSE,),
        ).fetchone()
        conn.close()
        if row and isinstance(row[0], int) and row[0] > 0:
            last_propose_ts = row[0]
    except Exception:
        pass

    effective_cutoff = max(cutoff_ts, last_propose_ts)
    log.info("drive_extractor proposals: reading facts since %s", effective_cutoff)

    # Query pending facts. The freshness window (extracted_at >= cutoff) is an
    # INTENTIONAL product bound: Drive-extracted facts are surfaced only while
    # recent (a 3-week-old "X is a photographer" fact has little value). Ordering
    # is HIGH-confidence first, then OLDEST-first within a tier, so when the
    # per-run cap bites it defers the FRESHEST facts (which have the most window
    # time left to be drained on a later run) rather than the ones about to age
    # out. A large historical backfill is NOT drained by this daily proposer --
    # that is the triage tool's job (or a one-time DRIVE_EXTRACTOR_MAX_PROPOSALS_
    # PER_RUN bump); the per-run cap exists to stop a single run flooding review.
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        facts = conn.execute(
            """
            SELECT fact_id, source_id, entity, sub_entity, fact_type,
                   subject, detail, confidence, extracted_at, metadata
            FROM drive_extracted_facts
            WHERE extracted_at >= ?
              AND confidence IN ('HIGH', 'MED')
            ORDER BY confidence DESC, extracted_at ASC
            """,
            (effective_cutoff,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        log.error("drive_extractor proposals: DB query failed: %s", exc)
        stats["errors"] += 1
        return stats

    log.info("drive_extractor proposals: %d candidate facts", len(facts))

    run_start = int(time.time())
    newly_proposed = 0   # appended this run (excludes idempotency-skipped dups)
    capped = False       # True if the per-run cap stopped us before the last fact
    examined = 0         # facts looked at this run (for the deferred-count log)

    for fact in facts:
        examined += 1
        entity = fact["entity"] or "FNDR"
        fact_type = fact["fact_type"]
        subject = fact["subject"]
        detail = fact["detail"]
        source_id = fact["source_id"]
        fact_id = fact["fact_id"]
        confidence = fact["confidence"]

        # PHI guardrail
        if entity.startswith("LEX"):
            stats["skipped"] += 1
            continue

        # Skip amount type — numeric data without action mapping
        update_type_key = _FACT_TYPE_TO_UPDATE_TYPE.get(fact_type)
        if update_type_key is None:
            stats["skipped"] += 1
            continue

        # Resolve update type constant
        try:
            from cora.knowledge_review import (
                UPDATE_TYPE_DECISION,
                UPDATE_TYPE_ASANA_TASK,
                UPDATE_TYPE_HUBSPOT_NOTE,
                UPDATE_TYPE_GENERIC,
            )
            _UPDATE_TYPE_MAP = {
                "UPDATE_TYPE_DECISION":     UPDATE_TYPE_DECISION,
                "UPDATE_TYPE_ASANA_TASK":   UPDATE_TYPE_ASANA_TASK,
                "UPDATE_TYPE_HUBSPOT_NOTE": UPDATE_TYPE_HUBSPOT_NOTE,
                "UPDATE_TYPE_GENERIC":      UPDATE_TYPE_GENERIC,
            }
            update_type = _UPDATE_TYPE_MAP.get(update_type_key, UPDATE_TYPE_GENERIC)
        except ImportError:
            update_type = "generic"

        proposed_action = _build_proposed_action(fact_type, subject, detail, entity, source_id)
        description = f"[Drive] [{entity}] {fact_type.capitalize()}: {subject} — {detail[:150]}"

        if dry_run:
            log.info(
                "drive_extractor [DRY RUN] would propose: fact_id=%s type=%s confidence=%s",
                fact_id[:12], fact_type, confidence,
            )
            stats["proposed"] += 1
            continue

        try:
            appended = propose_update(
                update_id=f"drive_fact:{fact_id}",
                update_type=update_type,
                description=description,
                payload={
                    "fact_id": fact_id,
                    "fact_type": fact_type,
                    "subject": subject,
                    "detail": detail,
                    "entity": entity,
                    "source_id": source_id,
                },
                source_evidence=f"Drive file: {source_id}",
                confidence=confidence,
            )
            # propose_update returns False when the id already exists (re-run /
            # backfill) — those don't count as proposed or toward the per-run cap.
            if appended is False:
                stats["skipped"] += 1
            else:
                stats["proposed"] += 1
                newly_proposed += 1
                if newly_proposed >= _MAX_PROPOSALS_PER_RUN:
                    capped = True
                    deferred = len(facts) - examined
                    # Observability (no silent caps): surface the in-window backlog
                    # the cap deferred. These re-run next pass while still inside the
                    # freshness window; any that age past the window are intentionally
                    # not surfaced (stale). A persistently large number here means a
                    # backfill is in progress -> use the triage tool or bump the cap.
                    log.warning(
                        "drive_extractor: per-run proposal cap (%d) reached — "
                        "%d in-window candidate(s) deferred to a later run (watermark held). "
                        "If this stays high, a backfill is draining slowly; bump "
                        "DRIVE_EXTRACTOR_MAX_PROPOSALS_PER_RUN or run the triage tool.",
                        _MAX_PROPOSALS_PER_RUN, deferred,
                    )
                    break
        except Exception as exc:
            log.warning("drive_extractor: propose_update failed for %s: %s", fact_id[:12], exc)
            stats["errors"] += 1

    stats["capped"] = capped

    # Advance proposal watermark.
    # When the per-run cap stopped us early we must NOT advance the watermark,
    # or the deferred facts would be skipped on the next run (silent data loss).
    # Holding the watermark lets the next run re-query them; already-proposed
    # facts are then idempotency-skipped, so the run makes forward progress.
    if not dry_run and not capped:
        try:
            conn = sqlite3.connect(str(db))
            conn.execute(
                """
                INSERT INTO sync_state (source, last_sync_at, last_source_modified)
                VALUES (?, ?, NULL)
                ON CONFLICT(source) DO UPDATE SET last_sync_at = excluded.last_sync_at
                """,
                (_WATERMARK_PROPOSE, run_start),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("drive_extractor: could not advance proposal watermark: %s", exc)

    log.info(
        "drive_extractor proposals done: proposed=%d skipped=%d errors=%d",
        stats["proposed"], stats["skipped"], stats["errors"],
    )
    return stats
