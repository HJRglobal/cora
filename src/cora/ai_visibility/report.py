"""On-demand tool summary + (slice 8) weekly Slack score card.

Reads the latest completed scan from the store and renders it for consumers:
  * get_tool_summary()  -- compact text the f3e_ai_visibility tool hands to the LLM
  * (slice 8) build_scorecard / post_scorecards -- the weekly Slack card

Source-opaque: never names the DB, the vendor (Otterly), or any tool -- only the
public surface name "Google AI Overviews" where relevant. PHI guard OFF.
"""

from __future__ import annotations

import logging
from datetime import datetime

from . import store
from .prompts import PromptBasketError, load_basket

log = logging.getLogger(__name__)

_BRAND_ORDER = ["energy", "pure", "mood"]
_BRAND_LABEL = {"energy": "F3 Energy", "pure": "F3 Pure", "mood": "F3 Mood"}

_NO_SCAN = (
    "No AI visibility scan has completed yet. The first weekly scan runs Monday "
    "10:15 AZ; ask again after it lands."
)


def _fmt_delta(wow) -> str:
    if wow is None:
        return "first run - no baseline"
    sign = "+" if wow >= 0 else ""
    return f"{sign}{wow:.1f} WoW"


def _scan_date(scan_meta: dict) -> str:
    ts = (scan_meta or {}).get("finished_at") or (scan_meta or {}).get("started_at") or ""
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ts[:10] if ts else "unknown date"


def _prompt_text_map() -> dict[str, str]:
    try:
        basket = load_basket()
    except PromptBasketError:
        return {}
    return {p.id: p.text for p in basket.all_prompts()}


def top_gaps_text(scan_id: int, brand_key: str, pmap: dict[str, str], limit: int = 3) -> list[str]:
    """Prompt texts where a competitor is named but F3 is not."""
    gaps = store.top_competitor_gaps(scan_id, brand_key, limit=limit)
    return [pmap.get(g["prompt_id"], g["prompt_id"]) for g in gaps]


def get_tool_summary() -> str:
    """Latest per-brand scores + deltas + top gaps, as plain text for the LLM."""
    scores = store.latest_scores()
    if not scores:
        return _NO_SCAN
    any_meta = next(iter(scores.values()))["scan"]
    date_str = _scan_date(any_meta)
    pmap = _prompt_text_map()

    lines = [f"F3 AI Visibility - latest weekly scan ({date_str}):"]
    aio_any = False
    for bkey in _BRAND_ORDER:
        s = scores.get(bkey)
        if not s:
            continue
        comp = s.get("composite") or 0.0
        line = (f"- {_BRAND_LABEL[bkey]}: {comp:.0f}/100 ({_fmt_delta(s.get('wow_delta'))}); "
                f"unaided presence {(s.get('unaided_presence') or 0):.0f}%, "
                f"share-of-voice {(s.get('share_of_voice') or 0):.0f}%")
        if s.get("aio_composite") is not None:
            aio_any = True
            line += f"; Google AI Overviews {s['aio_composite']:.0f}/100"
        lines.append(line)
        gaps = top_gaps_text(s["scan"]["id"], bkey, pmap)
        if gaps:
            lines.append("    where a competitor is named but F3 isn't: "
                         + "; ".join(f'"{g}"' for g in gaps))
    if not aio_any:
        lines.append("(Google AI Overviews coverage was not available for this scan.)")
    return "\n".join(lines)
