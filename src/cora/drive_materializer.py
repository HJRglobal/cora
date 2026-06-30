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

from . import phi_guard, org_roles

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

# Belt for NULL-tagged Part-2 content: the sub_entity tagger is precision-first (a
# behavioral-health chunk that never literally says "LBHS" stays NULL and would slip past
# the _LEX_EXCLUDE_SUB query filter), so we ALSO drop the whole LEX digest if any explicit
# LBHS / 42-CFR-Part-2 signal appears in the distilled body. Mirrors the LBHS keyword set
# in knowledge_base/lex_sub_entity.py (kept in sync intentionally).
_LBHS_SIGNAL_RE = re.compile(
    r"\b(LBHS|BHRF|COPA|Behavioral Health Services|Jared Harker)\b", re.IGNORECASE
)

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


# ── Change 3: flywheel-ledger mirror (DR / portability) ─────────────────────
# The flywheel's working ledgers stay LOCAL: they are high-frequency, lock-protected
# appends that the live knowledge-review + gap pipeline reads AND writes. We MIRROR them
# to Drive _brain/_flywheel/ once per run (a snapshot COPY) rather than live-appending to
# the G: mount — Drive-for-Desktop conflicts on rapid concurrent appends. This is DR +
# future-portability insurance; the canonical ledgers remain local by design (readers are
# deliberately NOT repointed at Drive — that would reintroduce the append-conflict).
_FLYWHEEL_LEDGERS: tuple[tuple[str, str], ...] = (
    ("data", "cora-proposed-memory-updates.jsonl"),
    ("data", "cora-proposed-memory-updates.archive.jsonl"),
    ("data", "cora-reply-log.jsonl"),
    ("logs", "knowledge-gaps.jsonl"),
    ("design/known-answers", ".resolved-gaps.jsonl"),
)


def _flywheel_dir() -> Path:
    return Path(os.environ.get("FLYWHEEL_MIRROR_DIR")
                or r"G:\My Drive\HJR-Founder-OS\_brain\_flywheel")


def mirror_flywheel_ledgers(repo_root: Path | None = None) -> list[str]:
    """Snapshot-copy the flywheel ledgers to Drive _brain/_flywheel/. Never raises.

    Returns the basenames mirrored; skips ledgers that don't exist yet. Atomic per file
    (temp + replace). A one-way COPY only — the canonical ledgers stay local.
    """
    root = repo_root or _REPO_ROOT
    dest = _flywheel_dir()
    mirrored: list[str] = []
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 — DR mirror must never break the run
        log.warning("drive_materializer: flywheel mirror dir unavailable: %s", exc)
        return mirrored
    for sub, name in _FLYWHEEL_LEDGERS:
        src = root / sub / name
        try:
            if not src.exists():
                continue
            tmp = dest / (name + ".tmp")
            tmp.write_bytes(src.read_bytes())
            tmp.replace(dest / name)
            mirrored.append(name)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the mirror
            log.warning("drive_materializer: flywheel mirror failed for %s: %s", name, exc)
    return mirrored


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
- PHI rule (EVERY unit, since holdco/founder digests can span Lexington, a care provider): NEVER include any individual care-recipient's name (a client / patient / member) or their billing / authorization / eligibility / coverage status. Staff and vendor names are fine.
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


def _lex_staff_names() -> set[str]:
    """Staff roster to PRESERVE during LEX scrubbing (so staff names aren't redacted but
    client names are). Mirrors context_loader._apply_lex_phi_scrub. Fail-soft to empty
    (empty = over-redact, the safe direction)."""
    try:
        return {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
    except Exception:  # noqa: BLE001
        return set()


def _phi_wall(entity: str, body: str) -> str | None:
    """Return the safe body to write, or None to DROP the entity's file this run.

    LEX: mirror the live non-custodian retrieval scrub stack (context_loader.
    _apply_lex_phi_scrub) — scrub_lex_phi THEN redact_cue_adjacent_names with the staff
    roster, so a bare client name near a PHI cue ("the client, Madison" / "incident
    involving Jalen") is caught, not just cue-adjacent possessives. Fail-CLOSED on any
    scrub error. Then DROP the whole file if (a) an explicit LBHS / 42-CFR-Part-2 signal
    appears (the belt for NULL-tagged mis-classified Part-2 content the sub_entity filter
    misses), or (b) clinical PHI or named-billing/status PHI survives the scrub. Redaction
    placeholders ("[diagnosis redacted]", "[client]'s", "[name redacted]") deliberately do
    NOT trip is_clinical_phi / is_lex_billing_status_phi, so a properly-scrubbed digest is
    written, not over-dropped.

    Non-LEX: a holdco/founder digest can legitimately span Lexington (a finance meeting
    that names a Lexington client's authorization status), so apply the SAME client-level
    drop — clinical OR named-billing/status — as a backstop against a mis-tagged LEX chunk
    reaching an HJRG/FNDR digest on the org-wide-readable Drive store.
    """
    if entity == "LEX":
        try:
            staff = _lex_staff_names()
            scrubbed = phi_guard.scrub_lex_phi(body, allowed_names=staff)
            scrubbed = phi_guard.redact_cue_adjacent_names(scrubbed, allowed_names=staff)
        except Exception as exc:  # noqa: BLE001 — fail-closed
            log.warning("drive_materializer: LEX scrub error — dropping LEX file: %s", exc)
            return None
        if _LBHS_SIGNAL_RE.search(scrubbed):
            log.warning("drive_materializer: LEX digest carries an LBHS/42-CFR-Part-2 signal — DROPPED")
            return None
        if phi_guard.is_clinical_phi(scrubbed) or phi_guard.is_lex_billing_status_phi(scrubbed):
            log.warning("drive_materializer: LEX digest still trips PHI after scrub — DROPPED")
            return None
        return scrubbed
    if phi_guard.is_clinical_phi(body) or phi_guard.is_lex_billing_status_phi(body):
        log.warning("drive_materializer: %s digest contains client-level PHI (mis-tagged LEX?) — DROPPED", entity)
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
    # The YYYY-MM-DD filename is box-local (AZ) PRESENTATION only; chunk selection +
    # watermarking are epoch-based (TZ-correct). The 05:45 AZ slot stays away from local
    # midnight, so one run can't straddle two AZ dates — keep it away from midnight.
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
            source_adv: dict[str, int] = {}
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
                    ats = [int(c["ingested_at"]) for c in chunks]
                    mx = max(ats)
                    full_page = len(chunks) >= _MAX_CHUNKS_PER_SOURCE
                    # Boundary-safe watermark: an upsert batch stamps MANY chunks with one
                    # ingested_at second, so a full page may have SPLIT that second.
                    # Advancing to mx then querying `> mx` would silently drop the rest of
                    # that second. On a full page advance to mx-1 so the whole boundary
                    # second is re-fetched next run (the daily file is overwritten
                    # idempotently — re-processing is harmless), converging over a few runs.
                    if full_page and min(ats) == mx:
                        # Pathological: the ENTIRE full page is ONE second — mx-1 would
                        # re-fetch the same second forever. Advance past it and log LOUDLY
                        # (the tail of this one second beyond the page limit is not
                        # materialized — visible, never silent, never an infinite loop).
                        log.warning(
                            "drive_materializer: %s/%s has >=%d chunks at one ingested_at=%d; "
                            "advancing past it (tail beyond the page limit not materialized)",
                            entity, source, _MAX_CHUNKS_PER_SOURCE, mx,
                        )
                        source_adv[source] = mx
                    else:
                        source_adv[source] = (mx - 1) if full_page else mx

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
                for source, adv in source_adv.items():
                    wm[_wm_key(entity, source)] = adv
                # Persist incrementally so a mid-loop crash never replays a completed
                # entity's distill (LLM cost) — the day's progress survives.
                _save_watermarks(wm)
            stats["entities_written"] += 1

        if not dry_run:
            # watermarks already persisted incrementally per written entity above.
            # Change 3: end-of-run DR mirror of the flywheel ledgers (never fails the run).
            try:
                stats["flywheel_mirrored"] = mirror_flywheel_ledgers()
            except Exception as exc:  # noqa: BLE001
                log.warning("drive_materializer: flywheel mirror error: %s", exc)
                stats["flywheel_mirrored"] = []
    finally:
        if own_kb:
            try:
                kb.close()
            except Exception:  # noqa: BLE001
                pass

    return stats
