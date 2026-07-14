"""On-demand tool summary + (slice 8) weekly Slack score card.

Reads the latest completed scan from the store and renders it for consumers:
  * get_tool_summary()  -- compact text the f3e_ai_visibility tool hands to the LLM
  * (slice 8) build_scorecard / post_scorecards -- the weekly Slack card

Source-opaque: never names the DB, the vendor (Otterly), or any tool -- only the
public surface name "Google AI Overviews" where relevant. PHI guard OFF.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from . import store
from .prompts import PromptBasketError, load_basket

log = logging.getLogger(__name__)

_BRAND_ORDER = ["energy", "pure", "mood", "hjr"]
_BRAND_LABEL = {"energy": "F3 Energy", "pure": "F3 Pure", "mood": "F3 Mood",
                "hjr": "Harrison Rogers"}
# Short label used in the "competitor named but <X> isn't" gap line. The F3
# beverage brands share "F3" (output byte-identical to pre-hjr); the founder
# personal brand uses its own name. Unknown keys fall back to "F3".
_BRAND_SHORT = {"energy": "F3", "pure": "F3", "mood": "F3", "hjr": "Harrison Rogers"}

# Score card Slack channel: config value, never hardcoded at the call site.
# Default = #f3-ai-visibility (C0BFXEJ1UJU). Override with AI_VISIBILITY_CHANNEL
# or --channel. Only the emoji allowlist below is used on the card.
_DEFAULT_CHANNEL = "C0BFXEJ1UJU"
# Composite -> status emoji (allowlist: green/yellow/red circles only).
_GREEN, _YELLOW, _RED = "\U0001F7E2", "\U0001F7E1", "\U0001F534"

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
            short = _BRAND_SHORT.get(bkey, "F3")
            lines.append(f"    where a competitor is named but {short} isn't: "
                         + "; ".join(f'"{g}"' for g in gaps))
    if not aio_any:
        lines.append("(Google AI Overviews coverage was not available for this scan.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weekly Slack score card
# ---------------------------------------------------------------------------
def _status_emoji(composite: float) -> str:
    if composite >= 60:
        return _GREEN
    if composite >= 35:
        return _YELLOW
    return _RED


def build_scorecard(scores: dict[str, dict]) -> str:
    """Build the weekly Slack mrkdwn score card (routed through sanitize_text by
    the poster; NEVER through reply_formatter, which would flatten it)."""
    if not scores:
        return "*F3 AI Visibility* - no completed scan to report yet."
    any_meta = next(iter(scores.values()))["scan"]
    date_str = _scan_date(any_meta)
    pmap = _prompt_text_map()

    lines = [f"*F3 AI Visibility - weekly scan ({date_str})*", ""]
    aio_any = False
    for bkey in _BRAND_ORDER:
        s = scores.get(bkey)
        if not s:
            continue
        comp = s.get("composite") or 0.0
        emoji = _status_emoji(comp)
        lines.append(f"{emoji} *{_BRAND_LABEL[bkey]}* - {comp:.0f}/100  ({_fmt_delta(s.get('wow_delta'))})")
        sov = s.get("share_of_voice") or 0.0
        rivals = store.top_competitors(s["scan"]["id"], bkey, limit=3)
        rivals_txt = ", ".join(name for name, _n in rivals) if rivals else "none detected"
        lines.append(f"    • Unaided presence {(s.get('unaided_presence') or 0):.0f}% | "
                     f"Share of voice {sov:.0f}% (top rivals: {rivals_txt})")
        if s.get("aio_composite") is not None:
            aio_any = True
            lines.append(f"    • Google AI Overviews: {s['aio_composite']:.0f}/100")
        gaps = top_gaps_text(s["scan"]["id"], bkey, pmap)
        if gaps:
            lines.append("    • Competitors beat us on: "
                         + "; ".join(f'"{g}"' for g in gaps))
        lines.append("")

    footer = "_4 engines queried directly"
    footer += " + Google AI Overviews" if aio_any else "; Google AI Overviews unavailable this week"
    footer += ". Scores refresh weekly._"
    lines.append(footer)
    return "\n".join(lines).strip()


def _resolve_channel(channel: str | None) -> str:
    return channel or os.environ.get("AI_VISIBILITY_CHANNEL", "") or _DEFAULT_CHANNEL


def post_scorecards(scan_id: int, *, channel: str | None = None) -> bool:
    """Post the weekly score card to Slack. Returns True on success; never raises
    (a posting failure must not fail the scan). Routes text through the egress
    boundary (sanitize_text) -- NOT reply_formatter."""
    scores = store.scores_for_scan(scan_id)
    if not scores:
        log.warning("ai_visibility: no scores for scan %d; not posting", scan_id)
        return False
    text = build_scorecard(scores)
    target = _resolve_channel(channel)
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("ai_visibility: SLACK_BOT_TOKEN not set; cannot post score card")
        return False
    try:
        import requests  # noqa: PLC0415
        from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: explicit sanitize
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"channel": target, "text": sanitize_text(text), "mrkdwn": True,
                  "unfurl_links": False, "unfurl_media": False},
            timeout=15,
        )
        data = resp.json() if resp.ok else {}
        if not data.get("ok"):
            log.warning("ai_visibility: score-card post failed channel=%s error=%s",
                        target, data.get("error", resp.status_code))
            return False
        log.info("ai_visibility: score card posted to %s (scan %d)", target, scan_id)
        return True
    except Exception as exc:  # noqa: BLE001 -- posting must never fail the scan
        log.warning("ai_visibility: score-card post raised: %s", exc)
        return False
