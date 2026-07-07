"""SQLite store for the AI-visibility engine (data/ai_visibility.db).

Five tables, schema ensured idempotently on every connect (same pattern as
influencer_client.py):

  scans     -- one row per scan run (metadata, cost, status)
  answers   -- one row per prompt x model x run for the 4 DIRECT engines
  mentions  -- brand + competitor presence per answer (+ aggregate AIO rows)
  citations -- verified cited URL + source type (per answer, or aggregate AIO)
  scores    -- per brand per scan: 4 components + composite + AIO + WoW delta

The Google-AI-Overviews (Otterly) slice does not create per-run answer rows
(it has no per-prompt granularity); its citations/mentions are stored with
model='aio_otterly' and answer_id NULL, and its metrics land on the scores row.

PHI guard OFF: marketing/visibility data only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .classifier import Classification
from .scorer import BrandScore

log = logging.getLogger(__name__)

# src/cora/ai_visibility/store.py -> parents[3] = repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB_PATH = _REPO_ROOT / "data" / "ai_visibility.db"

# Overridable so tests can point at a tmp DB.
_db_path_override: Path | None = None


def set_db_path(path: str | Path | None) -> None:
    global _db_path_override
    _db_path_override = Path(path) if path else None


def _db_path() -> Path:
    return _db_path_override or _DEFAULT_DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_conn() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")  # D-039: two writers wait, don't crash
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scans (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at       TEXT NOT NULL,
            finished_at      TEXT,
            basket_version   INTEGER,
            models_json      TEXT,
            runs_per_prompt  INTEGER,
            brands_json      TEXT,
            status           TEXT NOT NULL DEFAULT 'running',
            total_calls      INTEGER DEFAULT 0,
            total_cost_usd   REAL DEFAULT 0.0,
            aio_included     INTEGER DEFAULT 0,
            notes            TEXT
        );

        CREATE TABLE IF NOT EXISTS answers (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id          INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            brand            TEXT NOT NULL,
            prompt_id        TEXT NOT NULL,
            intent           TEXT,
            aided            INTEGER,
            model            TEXT NOT NULL,
            run_index        INTEGER NOT NULL,
            raw_text         TEXT,
            classifier_json  TEXT,
            mentioned        INTEGER DEFAULT 0,
            is_correct_brand INTEGER DEFAULT 0,
            position         INTEGER,
            sentiment        TEXT,
            num_competitors  INTEGER DEFAULT 0,
            cost_usd         REAL DEFAULT 0.0,
            error            TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_av_answers_scan  ON answers(scan_id, brand, model);
        CREATE INDEX IF NOT EXISTS idx_av_answers_prompt ON answers(scan_id, prompt_id);

        CREATE TABLE IF NOT EXISTS mentions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id       INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            answer_id     INTEGER REFERENCES answers(id) ON DELETE CASCADE,
            brand         TEXT NOT NULL,
            model         TEXT NOT NULL,
            name          TEXT NOT NULL,
            is_target     INTEGER NOT NULL DEFAULT 0,
            position      INTEGER,
            sentiment     TEXT,
            mentions_count INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_av_mentions_scan ON mentions(scan_id, brand, model);

        CREATE TABLE IF NOT EXISTS citations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id      INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            answer_id    INTEGER REFERENCES answers(id) ON DELETE CASCADE,
            brand        TEXT NOT NULL,
            model        TEXT NOT NULL,
            url          TEXT NOT NULL,
            domain       TEXT,
            resolved     INTEGER DEFAULT 1,
            source_type  TEXT,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_av_citations_scan ON citations(scan_id, brand);
        CREATE INDEX IF NOT EXISTS idx_av_citations_type ON citations(source_type);

        CREATE TABLE IF NOT EXISTS scores (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id               INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            brand                 TEXT NOT NULL,
            presence              REAL,
            share_of_voice        REAL,
            position              REAL,
            sentiment             REAL,
            composite             REAL,
            composite_direct_only REAL,
            unaided_presence      REAL,
            per_intent_json       TEXT,
            engines_json          TEXT,
            aio_presence          REAL,
            aio_share_of_voice    REAL,
            aio_position          REAL,
            aio_sentiment         REAL,
            aio_composite         REAL,
            per_engine_json       TEXT,
            prev_composite        REAL,
            wow_delta             REAL,
            computed_at           TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_av_scores_scan  ON scores(scan_id, brand);
        CREATE INDEX IF NOT EXISTS idx_av_scores_brand ON scores(brand);
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# scans
# ---------------------------------------------------------------------------
def create_scan(*, basket_version: int, models: list[str], runs_per_prompt: int,
                brands: list[str], status: str = "running", notes: str = "") -> int:
    conn = _get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO scans (started_at, basket_version, models_json, runs_per_prompt,
                                  brands_json, status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (_now(), basket_version, json.dumps(models), runs_per_prompt,
             json.dumps(brands), status, notes),
        )
        conn.commit()
        scan_id = int(cur.lastrowid)
    finally:
        conn.close()
    log.info("ai_visibility scan %d created (models=%s brands=%s)", scan_id, models, brands)
    return scan_id


def finish_scan(scan_id: int, *, status: str, total_calls: int, total_cost_usd: float,
                aio_included: bool, notes: str | None = None) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE scans SET finished_at=?, status=?, total_calls=?, total_cost_usd=?,
                                aio_included=?, notes=COALESCE(?, notes) WHERE id=?""",
            (_now(), status, total_calls, round(total_cost_usd, 4),
             1 if aio_included else 0, notes, scan_id),
        )
        conn.commit()
    finally:
        conn.close()
    log.info("ai_visibility scan %d finished status=%s calls=%d cost=$%.4f",
             scan_id, status, total_calls, total_cost_usd)


def get_scan(scan_id: int) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def latest_completed_scan_id(before_scan_id: int | None = None) -> int | None:
    conn = _get_conn()
    try:
        if before_scan_id is not None:
            row = conn.execute(
                "SELECT id FROM scans WHERE status='completed' AND id<? ORDER BY id DESC LIMIT 1",
                (before_scan_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM scans WHERE status='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# answers / mentions / citations
# ---------------------------------------------------------------------------
def insert_answer(*, scan_id: int, brand: str, prompt_id: str, intent: str, aided: bool,
                  model: str, run_index: int, raw_text: str,
                  classification: Classification, cost_usd: float,
                  error: str | None = None) -> int:
    conn = _get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO answers (scan_id, brand, prompt_id, intent, aided, model, run_index,
                                    raw_text, classifier_json, mentioned, is_correct_brand,
                                    position, sentiment, num_competitors, cost_usd, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scan_id, brand, prompt_id, intent, 1 if aided else 0, model, run_index,
             (raw_text or "")[:20000], json.dumps(asdict(classification)),
             1 if classification.mentioned else 0,
             1 if classification.is_correct_brand else 0,
             classification.position, classification.sentiment,
             len(classification.competitors_mentioned), round(cost_usd, 6),
             error, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_mention(conn: sqlite3.Connection, *, scan_id: int, answer_id: int | None,
                    brand: str, model: str, name: str, is_target: bool,
                    position: int | None, sentiment: str | None, mentions_count: int) -> None:
    conn.execute(
        """INSERT INTO mentions (scan_id, answer_id, brand, model, name, is_target,
                                 position, sentiment, mentions_count, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (scan_id, answer_id, brand, model, name, 1 if is_target else 0,
         position, sentiment, mentions_count, _now()),
    )


def record_answer_mentions(*, scan_id: int, answer_id: int, brand: str, brand_name: str,
                           model: str, classification: Classification) -> None:
    """One target-brand mention row (when a hit) + one row per competitor mentioned."""
    conn = _get_conn()
    try:
        if classification.is_hit:
            _insert_mention(conn, scan_id=scan_id, answer_id=answer_id, brand=brand,
                            model=model, name=brand_name, is_target=True,
                            position=classification.position,
                            sentiment=classification.sentiment, mentions_count=1)
        for comp in classification.competitors_mentioned:
            _insert_mention(conn, scan_id=scan_id, answer_id=answer_id, brand=brand,
                            model=model, name=comp, is_target=False,
                            position=None, sentiment=None, mentions_count=1)
        conn.commit()
    finally:
        conn.close()


def insert_citations(*, scan_id: int, answer_id: int | None, brand: str, model: str,
                     citations: list) -> int:
    """citations: list of objects with .url/.domain/.resolved/.source_type."""
    if not citations:
        return 0
    conn = _get_conn()
    try:
        n = 0
        for c in citations:
            conn.execute(
                """INSERT INTO citations (scan_id, answer_id, brand, model, url, domain,
                                          resolved, source_type, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (scan_id, answer_id, brand, model, c.url, c.domain,
                 1 if c.resolved else 0, c.source_type, _now()),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def record_aio_slice(*, scan_id: int, brand: str, brand_name: str, model: str,
                     competitor_mentions: dict[str, int], citations: list) -> None:
    """Persist the AIO slice's competitor mentions + citations (answer_id NULL).
    The AIO metrics themselves land on the scores row via save_score."""
    conn = _get_conn()
    try:
        for name, count in (competitor_mentions or {}).items():
            _insert_mention(conn, scan_id=scan_id, answer_id=None, brand=brand, model=model,
                            name=name, is_target=False, position=None, sentiment=None,
                            mentions_count=int(count or 0))
        conn.commit()
    finally:
        conn.close()
    insert_citations(scan_id=scan_id, answer_id=None, brand=brand, model=model,
                     citations=citations or [])


# ---------------------------------------------------------------------------
# scoring reads + writes
# ---------------------------------------------------------------------------
def answers_for_scan(scan_id: int, brand: str) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM answers WHERE scan_id=? AND brand=? AND error IS NULL",
            (scan_id, brand),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def competitor_counts_by_answer(scan_id: int, brand: str) -> dict[int, int]:
    """answer_id -> number of competitor mentions (direct engines only)."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT answer_id, COUNT(*) AS n FROM mentions
               WHERE scan_id=? AND brand=? AND is_target=0 AND answer_id IS NOT NULL
               GROUP BY answer_id""",
            (scan_id, brand),
        ).fetchall()
        return {int(r["answer_id"]): int(r["n"]) for r in rows}
    finally:
        conn.close()


def save_score(scan_id: int, score: BrandScore) -> int:
    """Persist a BrandScore, computing WoW delta vs the previous completed scan."""
    prev = previous_composite(score.brand, scan_id)
    wow = round(score.composite - prev, 2) if prev is not None else None
    conn = _get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO scores (scan_id, brand, presence, share_of_voice, position, sentiment,
                                   composite, composite_direct_only, unaided_presence,
                                   per_intent_json, engines_json, aio_presence, aio_share_of_voice,
                                   aio_position, aio_sentiment, aio_composite, per_engine_json,
                                   prev_composite, wow_delta, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scan_id, score.brand, score.presence, score.share_of_voice, score.position,
             score.sentiment, score.composite, score.composite_direct_only,
             score.unaided_presence, json.dumps(score.per_intent),
             json.dumps(score.engines), score.aio_presence, score.aio_share_of_voice,
             score.aio_position, score.aio_sentiment, score.aio_composite,
             json.dumps(score.per_engine), prev, wow, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def previous_composite(brand: str, before_scan_id: int) -> float | None:
    """Composite from the most recent completed scan's score for this brand,
    before the given scan."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT s.composite AS composite FROM scores s
               JOIN scans sc ON sc.id = s.scan_id
               WHERE s.brand=? AND s.scan_id < ? AND sc.status='completed'
               ORDER BY s.scan_id DESC LIMIT 1""",
            (brand, before_scan_id),
        ).fetchone()
        return float(row["composite"]) if row and row["composite"] is not None else None
    finally:
        conn.close()


def latest_scores() -> dict[str, dict]:
    """Latest completed scan's scores per brand (for the tool + card)."""
    conn = _get_conn()
    try:
        scan = conn.execute(
            "SELECT * FROM scans WHERE status='completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not scan:
            return {}
        rows = conn.execute("SELECT * FROM scores WHERE scan_id=?", (scan["id"],)).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            d["scan"] = dict(scan)
            out[d["brand"]] = d
        return out
    finally:
        conn.close()


def top_competitor_gaps(scan_id: int, brand: str, limit: int = 3) -> list[dict]:
    """Prompts where a competitor is named but the brand is NOT a hit -- the
    'competitor beats us' gaps for the card."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT a.prompt_id, a.model,
                      (SELECT COUNT(*) FROM mentions m
                        WHERE m.answer_id=a.id AND m.is_target=0) AS comp_count
               FROM answers a
               WHERE a.scan_id=? AND a.brand=? AND a.error IS NULL
                     AND NOT (a.mentioned=1 AND a.is_correct_brand=1)
               """,
            (scan_id, brand),
        ).fetchall()
        # aggregate competitor pressure per prompt across models
        agg: dict[str, int] = {}
        for r in rows:
            if int(r["comp_count"] or 0) > 0:
                agg[r["prompt_id"]] = agg.get(r["prompt_id"], 0) + int(r["comp_count"])
        ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [{"prompt_id": pid, "competitor_pressure": n} for pid, n in ranked]
    finally:
        conn.close()
