"""Backlog refill: turn measured AI-visibility gaps into PROPOSED backlog rows.

The refill answers "what should we write next" with a MEASUREMENT rather than a
model's opinion: the prompts where the latest completed weekly scan found zero
unaided mentions of the target brand are exactly the discovery questions F3 does
not currently answer anywhere an answer engine can see.

Rows land as PROPOSED, which `operating_files.next_queued` never selects, so a
refill can suggest twelve topics and authorise none of them. Harrison flips one
to QUEUED when he wants it written.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Below this many QUEUED rows, the weekly run proposes more.
REFILL_THRESHOLD = 4
REFILL_COUNT = 4

# The Learn lane covers the F3 product brands. `hjr` is Harrison's personal brand
# instrument and is measured for parity, not for f3energy.com content.
_LEARN_BRANDS = ("energy", "pure", "mood")


def db_path() -> Path:
    return Path(os.environ.get(
        "CORA_AI_VISIBILITY_DB", str(_REPO_ROOT / "data" / "ai_visibility.db")))


def zero_mention_prompt_ids(*, limit: int = 40) -> list[tuple[str, str, str]]:
    """[(prompt_id, brand, intent)] with zero unaided mentions in the latest
    COMPLETED scan. Empty list on any failure (the refill is a nicety, never a
    reason to fail a staging run).

    Scoped to `aided = 0`: an aided prompt names F3 in the question, so a
    non-mention there measures something else entirely.
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path().as_posix(), uri=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("f3e_blog refill: cannot open visibility db: %s", exc)
        return []
    try:
        row = con.execute(
            "SELECT id FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return []
        scan_id = row[0]
        marks = ",".join("?" * len(_LEARN_BRANDS))
        sql = (
            "SELECT prompt_id, brand, intent FROM answers "
            "WHERE scan_id = ? AND aided = 0 AND (error IS NULL OR error = '') "
            "AND brand IN (%s) "
            "GROUP BY prompt_id, brand, intent "
            "HAVING SUM(mentioned) = 0 "
            "ORDER BY COUNT(*) DESC LIMIT ?" % marks
        )
        return [tuple(r) for r in con.execute(
            sql, (scan_id, *_LEARN_BRANDS, int(limit))).fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.warning("f3e_blog refill: visibility query failed: %s", exc)
        return []
    finally:
        con.close()


def _prompt_texts() -> dict[str, str]:
    try:
        from ..ai_visibility.prompts import load_basket
        basket = load_basket()
    except Exception as exc:  # noqa: BLE001
        log.warning("f3e_blog refill: cannot load prompt basket: %s", exc)
        return {}
    out: dict[str, str] = {}
    for brand in basket.brands.values() if isinstance(basket.brands, dict) else basket.brands:
        for p in getattr(brand, "prompts", []) or []:
            out[p.id] = p.text
    return out


_PILLAR_BY_INTENT = {
    "discovery": "Category education",
    "problem": "Use-case",
    "comparison": "Category education",
    "branded": "Community/brand",
}
_BRAND_LABEL = {"energy": "Energy", "pure": "Pure", "mood": "Mood"}


def build_proposals(*, count: int = REFILL_COUNT,
                    exclude_titles: set[str] | None = None) -> list[dict]:
    """PROPOSED-row dicts for the lowest-hanging measured gaps.

    Titles already in the backlog are skipped so a refill cannot re-propose a
    topic that is already queued, drafted, or published.
    """
    gaps = zero_mention_prompt_ids()
    if not gaps:
        return []
    texts = _prompt_texts()
    skip = {t.strip().lower() for t in (exclude_titles or set())}
    out: list[dict] = []
    for prompt_id, brand, intent in gaps:
        text = texts.get(prompt_id, "")
        if not text:
            continue
        if text.strip().lower() in skip:
            continue
        pillar = _PILLAR_BY_INTENT.get(intent, "Category education")
        label = _BRAND_LABEL.get(brand, brand)
        out.append({
            # The prompt text IS the reader's question, which is the whole point
            # of the answer-first format. A human retitles it when they queue it.
            "title": text,
            "lane_pillar": "%s (%s)" % (pillar, label),
            "target_prompt": "%s 0-mention (%s)" % (prompt_id, intent),
            "notes": ("Proposed from the latest AI-visibility scan: zero unaided "
                      "mentions across every engine. Flip to QUEUED to have it "
                      "drafted."),
        })
        if len(out) >= count:
            break
    return out
