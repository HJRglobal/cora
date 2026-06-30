"""Nightly Drive materialization (2026-06-29) — distill the day's NEW swept KB chunks
into Drive _brain/swept/{ENTITY}/YYYY-MM-DD.md so a Drive-reading frontend (Tag) answers
from swept knowledge, not just the curated known-answers/reference layer.

What this is NOT:
- It does NOT rebuild the vector KB, run a vector search, or re-fetch any connector.
  It reads chunks ALREADY embedded in the local cora_kb.db (metadata SELECT only).
- The 6.2 GB cora_kb.db stays LOCAL and is never copied to Drive — only the distilled
  markdown is written to Drive.

Design invariants:
- Per-(entity, source) watermark: a fail-closed per-entity skip never advances another
  source's cursor, so a skipped/failed entity never silently drops a day.
- Fail-closed distill: any LLM error → skip that entity tonight (no file, no watermark
  advance → it retries next run). Never write a partial/garbage digest.
- LEX is PHI-walled: LEX-LBHS (42 CFR Part 2) is HARD-EXCLUDED at the query; LLC/LTS/LLA
  are materialized GM-level + run through scrub_lex_phi; a file is DROPPED (not written,
  watermark not advanced) if clinical or named-billing PHI survives the scrub. The distill
  prompt is explicitly forbidden from emitting client names / clinical detail.
- The output folder _brain/swept/ is excluded from BOTH KB ingest walks (static_md +
  drive_connector) so the digests never feed back into the KB (no loop / no bloat).
- Transport: the local G: mount (atomic temp-file + rename), matching session_capture /
  strategy_memo. Drive-for-Desktop syncs it to the cloud for Tag.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import phi_guard

log = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5"
_REPO_ROOT = Path(__file__).parent.parent.parent

# Canonical top-level entity codes to materialize (mirrors session_capture.ENTITY_FOLDERS
# keys). LEX is the only one carrying the PHI wall below.
ENTITY_CODES: tuple[str, ...] = (
    "HJRG", "F3E", "F3C", "UFL", "HJRPROD", "HJRP", "BDM", "OSN", "FNDR", "LEX",
)

# Swept content sources ONLY. Deliberately excludes: static_md (curated Founder-OS
# markdown, not swept — and including it would re-distill _brain itself), drive_asset
# (image/asset metadata, not narrative), user_note (blast-radius-1, never materialized).
SWEPT_SOURCES: tuple[str, ...] = (
    "gmail", "drive_sweep", "fireflies", "asana", "slack", "notion",
)

# LEX-LBHS = 42 CFR Part 2; never materialized to Drive under any circumstance.
_LEX_EXCLUDE_SUB: tuple[str, ...] = ("LEX-LBHS",)

_DEFAULT_LOOKBACK_HOURS = 26        # first run / missing-watermark seed (daily + overlap)
_MAX_CHUNKS_PER_SOURCE = 2000       # bound a catch-up run after downtime
_MAX_CHARS_PER_SOURCE = 8000        # cap the distill input per source
_MIN_BODY_CHARS = 40                # below this, treat a distill as empty -> skip


# ── paths (all env-overridable for tests) ──────────────────────────────────────

def _kb_db_path() -> Path:
    return Path(os.environ.get("MATERIALIZER_KB_DB_PATH")
                or _REPO_ROOT / "data" / "cora_kb.db")


def _swept_root() -> Path:
    return Path(os.environ.get("SWEPT_DIR")
                or r"G:\My Drive\HJR-Founder-OS\_brain\swept")


def _watermark_path() -> Path:
    return Path(os.environ.get("MATERIALIZATION_WATERMARK_PATH")
                or _REPO_ROOT / "data" / "state" / "materialization-watermark.json")


def _wm_key(entity: str, source: str) -> str:
    return f"{entity}|{source}"


def _load_watermarks() -> dict[str, int]:
    try:
        data = json.loads(_watermark_path().read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001 — missing/corrupt -> empty (first-run seed kicks in)
        return {}


def _save_watermarks(wm: dict[str, int]) -> None:
    p = _watermark_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wm, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ── LLM distillation (fail-closed) ──────────────────────────────────────────────

def _get_client(client: Any = None) -> Any:
    if client is not None:
        return client
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("drive_materializer: ANTHROPIC_API_KEY not set — skipping run")
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except Exception as exc:  # noqa: BLE001 — fail-closed
        log.warning("drive_materializer: anthropic client init failed: %s", exc)
        return None


_DISTILL_PROMPT = """You are distilling one day of an organization's internal activity for the business unit "{entity}" into a concise operational digest. The input below was swept from email, meeting transcripts, Slack, Asana, Drive docs, and Notion for this unit.

Produce GitHub-flavored markdown with EXACTLY these five section headers, in this order (keep every header even if a section has no bullets):

## Decisions
## Action items / follow-ups
## Key facts & updates
## Notable communications
## Who-owns-what changes

Rules:
- Distill hard — short bullets, signal only. NO raw email bodies, NO long verbatim quotes.
- "Notable communications": one line each, "who -> who: outcome" (the gist + the result), never a transcript.
- Be concrete (staff/vendor names, amounts, dates) where it is ordinary business activity.
- If the input is thin, produce a short digest. Do NOT invent facts that are not in the input.
{lex_rule}
Input:
---
{body}
"""

_LEX_RULE = """
LEXINGTON IS A CARE PROVIDER — HARD PHI RULES, NON-NEGOTIABLE (this digest is stored in a shared location):
- NEVER include any client / patient / member / individual NAME, diagnosis, medication, date of birth, or any individual's billing / authorization / eligibility / coverage / claims / placement detail.
- Refer to people ONLY by staff role or in the aggregate ("a client", "several members", "the DTA team").
- Keep everything GM-level and aggregate: counts, program / operational status, staffing, logistics. When in doubt, leave it out.
"""


def _build_source_block(chunks: list[dict[str, Any]]) -> str:
    """Concatenate chunk title+content for one source, capped at _MAX_CHARS_PER_SOURCE."""
    parts: list[str] = []
    used = 0
    for c in chunks:
        title = (c.get("title") or "").strip()
        body = (c.get("content") or "").strip()
        seg = (f"{title}\n{body}" if title else body).strip()
        if not seg:
            continue
        if used + len(seg) > _MAX_CHARS_PER_SOURCE:
            seg = seg[: max(0, _MAX_CHARS_PER_SOURCE - used)]
        if seg:
            parts.append(seg)
            used += len(seg)
        if used >= _MAX_CHARS_PER_SOURCE:
            break
    return "\n\n".join(parts)


def _distill_entity(entity: str, source_chunks: dict[str, list], client: Any) -> str | None:
    """Distill an entity's day across all its sources into one markdown body, or None."""
    blocks: list[str] = []
    for source in SWEPT_SOURCES:
        chunks = source_chunks.get(source) or []
        if not chunks:
            continue
        block = _build_source_block(chunks)
        if block:
            blocks.append(f"### Source: {source}\n{block}")
    if not blocks:
        return None
    prompt = _DISTILL_PROMPT.format(
        entity=entity,
        body="\n\n".join(blocks),
        lex_rule=_LEX_RULE if entity == "LEX" else "",
    )
    try:
        resp = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001 — fail-closed
        log.warning("drive_materializer: distill failed for %s: %s", entity, exc)
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    return raw or None


def _phi_wall(entity: str, body: str) -> str | None:
    """Return the safe body to write, or None to DROP the entity's file this run.

    LEX: scrub with scrub_lex_phi (fail-closed on scrub error), then DROP if clinical
    PHI or named-billing PHI still trips after scrubbing. Redaction placeholders like
    "[diagnosis redacted]" deliberately do NOT trip is_clinical_phi / is_lex_billing_status_phi.
    Non-LEX: a defense-in-depth clinical-PHI backstop — should never trip; if it does,
    something is mis-tagged, so drop rather than write.
    """
    if entity == "LEX":
        try:
            scrubbed = phi_guard.scrub_lex_phi(body, allowed_names=None)
        except Exception as exc:  # noqa: BLE001 — fail-closed
            log.warning("drive_materializer: LEX scrub error — dropping LEX file: %s", exc)
            return None
        if phi_guard.is_clinical_phi(scrubbed) or phi_guard.is_lex_billing_status_phi(scrubbed):
            log.warning("drive_materializer: LEX digest still trips PHI after scrub — DROPPED")
            return None
        return scrubbed
    if phi_guard.is_clinical_phi(body):
        log.warning("drive_materializer: %s digest unexpectedly contains clinical PHI — DROPPED", entity)
        return None
    return body


def _render_file(entity: str, today_str: str, body: str, source_chunks: dict[str, list]) -> str:
    counts = ", ".join(f"{s}:{len(c)}" for s, c in source_chunks.items() if c)
    header = (
        f"# {entity} — swept-knowledge digest — {today_str}\n\n"
        f"_Auto-distilled by Cora from the day's swept activity ({counts}). "
        f"Distilled signal only; see the source systems for detail._\n"
    )
    if entity == "LEX":
        header += "_LEX: GM-level / aggregate / PHI-scrubbed. LBHS (42 CFR Part 2) excluded._\n"
    return header + "\n" + body.strip() + "\n"


def _write_swept_file(entity: str, today_str: str, doc: str) -> Path:
    out_dir = _swept_root() / entity
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{today_str}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(doc, encoding="utf-8")
    tmp.replace(path)
    return path


def run(
    *,
    today: Any = None,
    client: Any = None,
    dry_run: bool = False,
    lookback_hours: int | None = None,
    kb: Any = None,
) -> dict[str, Any]:
    """Materialize one day of swept knowledge per entity. Returns a stats dict.

    today: date override (tests). client: injected LLM client (tests). kb: injected
    KnowledgeBase (tests) — otherwise opened from _kb_db_path(). dry_run: distill but
    write nothing and don't advance watermarks.
    """
    now = int(time.time())
    lookback = (lookback_hours if lookback_hours is not None else _DEFAULT_LOOKBACK_HOURS) * 3600
    today_str = (today or datetime.now().date()).isoformat()
    stats: dict[str, Any] = {
        "entities_written": 0, "entities_skipped": 0, "lex_dropped": 0,
        "entities_no_new": 0, "files": [],
    }

    client = _get_client(client)
    if client is None:
        return {**stats, "aborted": "no_llm_client"}

    own_kb = kb is None
    if own_kb:
        from .knowledge_base import KnowledgeBase
        kb = KnowledgeBase(_kb_db_path())

    try:
        wm = _load_watermarks()
        for entity in ENTITY_CODES:
            exclude = _LEX_EXCLUDE_SUB if entity == "LEX" else None
            source_chunks: dict[str, list] = {}
            source_maxts: dict[str, int] = {}
            for source in SWEPT_SOURCES:
                since = wm.get(_wm_key(entity, source)) or (now - lookback)
                try:
                    chunks = kb.get_chunks_since(
                        source=source, entity=entity, since_ts=since,
                        exclude_sub_entities=exclude, limit=_MAX_CHUNKS_PER_SOURCE,
                    )
                except Exception as exc:  # noqa: BLE001 — one bad source must not abort the entity
                    log.warning("drive_materializer: query failed %s/%s: %s", entity, source, exc)
                    chunks = []
                if chunks:
                    source_chunks[source] = chunks
                    source_maxts[source] = max(int(c["ingested_at"]) for c in chunks)

            if not source_chunks:
                stats["entities_no_new"] += 1
                continue

            body = _distill_entity(entity, source_chunks, client)
            if not body or len(body) < _MIN_BODY_CHARS:
                log.info("drive_materializer: %s distill empty/failed — not advancing watermark", entity)
                stats["entities_skipped"] += 1
                continue

            safe = _phi_wall(entity, body)
            if safe is None:
                # PHI drop (LEX) or backstop trip — DO NOT advance the watermark, so it
                # retries next run and stays visible in logs (self-healing if scrub improves).
                stats["lex_dropped" if entity == "LEX" else "entities_skipped"] += 1
                continue

            doc = _render_file(entity, today_str, safe, source_chunks)
            if not dry_run:
                path = _write_swept_file(entity, today_str, doc)
                stats["files"].append(str(path))
                for source, mx in source_maxts.items():
                    wm[_wm_key(entity, source)] = mx
            stats["entities_written"] += 1

        if not dry_run:
            _save_watermarks(wm)
    finally:
        if own_kb:
            try:
                kb.close()
            except Exception:  # noqa: BLE001
                pass

    return stats
