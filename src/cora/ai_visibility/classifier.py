"""LLM-as-judge classifier for AI-visibility answers.

A grounded model's answer to a buyer-style prompt is judged by Claude Haiku
(NOT string-matched -- "F3" alone would false-positive on F3 Nation fitness /
Formula 3). The judge returns strict JSON:

    {
      "mentioned": bool,               # the target brand appears at all
      "is_correct_brand": bool,        # ... and it is OUR beverage brand (disambiguation)
      "position": int | null,          # 1 = named first / most prominent, null = absent
      "sentiment": "positive|neutral|negative|mixed",
      "competitors_mentioned": [str],  # names from the brand's competitor_set
      "cited_sources": [url]
    }

A run counts toward presence only when ``mentioned AND is_correct_brand`` --
so a namesake (F3 Nation) is detected (mentioned may be true) but excluded
(is_correct_brand false).

Fail-CLOSED: any missing key / API error / parse failure returns a
Classification with mentioned=False and ``error`` set -- it never raises, so one
bad judge call drops a single run rather than aborting the scan.

PHI guard OFF: marketing/visibility data only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from .prompts import Brand, Prompt

log = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"  # pinned snapshot (deterministic judge)
_VALID_SENTIMENT = frozenset({"positive", "neutral", "negative", "mixed"})

_SYSTEM = (
    "You are a precise mention classifier for AI answer engines. You judge whether "
    "a specific target (a brand OR a person) was recommended or referenced in an "
    "AI-generated answer. You never confuse the target with same-named entities "
    "(namesakes) -- including a different person who happens to share the name. You "
    "reply with ONLY a single JSON object, no prose, no code fence."
)

_PROMPT_TEMPLATE = """Judge whether the TARGET (a brand or a person) is recommended in the AI ANSWER below.

TARGET: {brand_name}
Known aliases (how it may be written): {aliases}
Disambiguation (what/who it IS and is NOT): {disambiguation}
Its competitor set (only these count as competitors): {competitors}

THE QUESTION THAT WAS ASKED:
{question}

THE AI ANSWER TO JUDGE:
\"\"\"
{answer}
\"\"\"

Return ONLY this JSON object (no code fence, no commentary):
{{
  "mentioned": <true if the target -- by any alias, or unambiguously the same
    brand/person -- is referenced at all in the answer; false otherwise. A bare "F3"
    (or a bare first/last name) that clearly refers to something/someone else is NOT
    a mention>,
  "is_correct_brand": <true ONLY if the reference is the specific target described in
    the Disambiguation above (the target brand or person). If it refers to a namesake
    or a different same-named entity (e.g. F3 Nation fitness, Formula 3 racing, a
    generic 'pure' product, a mood-tracking app, or a different person who shares the
    name), set false>,
  "position": <1 if the target brand is the first / most prominently recommended item,
    2 if second, 3 if third, a higher integer if lower, or null if not mentioned>,
  "sentiment": "<positive|neutral|negative|mixed toward the target brand; neutral if merely listed; neutral if not mentioned>",
  "competitors_mentioned": [<names taken ONLY from the competitor set above that are recommended or mentioned in the answer>],
  "cited_sources": [<any source URLs the answer cites, as plain strings; [] if none>]
}}"""


@dataclass
class Classification:
    mentioned: bool = False
    is_correct_brand: bool = False
    position: int | None = None
    sentiment: str = "neutral"
    competitors_mentioned: list[str] = field(default_factory=list)
    cited_sources: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def is_hit(self) -> bool:
        """A run counts toward presence only when the CORRECT brand is mentioned."""
        return bool(self.mentioned and self.is_correct_brand)


def is_hit(c: Classification) -> bool:
    return c.is_hit


# ---------------------------------------------------------------------------
# Haiku seam (monkeypatched in tests)
# ---------------------------------------------------------------------------
def _judge_raw(user_prompt: str) -> str:
    """Call Haiku once and return the raw text. Raises on any API problem
    (classify_answer catches -> fail-closed). Bounded timeout + no long retry so
    a slow judge never stalls a scan."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    import anthropic  # noqa: PLC0415

    client = anthropic.Anthropic(api_key=api_key, timeout=20.0, max_retries=1)
    resp = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=400,
        temperature=0,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            log.info("ai_visibility_judge usage input=%d output=%d",
                     int(getattr(usage, "input_tokens", 0) or 0),
                     int(getattr(usage, "output_tokens", 0) or 0))
    except (TypeError, ValueError, AttributeError):
        pass
    return resp.content[0].text


def _extract_json(raw: str) -> dict | None:
    """Fence-strip -> brace-slice -> json.loads. None on any failure."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _normalize_competitors(raw_list, competitor_set: tuple[str, ...]) -> list[str]:
    """Map judge output to the canonical competitor_set (case/space-insensitive).
    Unknown names are dropped so share-of-voice stays clean."""
    if not isinstance(raw_list, list):
        return []
    canon = {c.strip().lower(): c for c in competitor_set}
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_list:
        key = str(item or "").strip().lower()
        if key in canon and canon[key] not in seen:
            seen.add(canon[key])
            out.append(canon[key])
    return out


def _clean_urls(raw_list) -> list[str]:
    if not isinstance(raw_list, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for u in raw_list:
        s = str(u or "").strip()
        if s.lower().startswith(("http://", "https://")) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _coerce(obj: dict, brand: Brand, fallback_citations: list[str]) -> Classification:
    mentioned = bool(obj.get("mentioned"))
    is_correct = bool(obj.get("is_correct_brand")) if mentioned else False

    pos = obj.get("position")
    position: int | None = None
    if isinstance(pos, bool):
        position = None  # guard: JSON true/false is not a position
    elif isinstance(pos, (int, float)):
        position = int(pos) if int(pos) >= 1 else None
    elif isinstance(pos, str) and pos.strip().isdigit():
        position = int(pos.strip()) or None
    # A non-hit has no meaningful position.
    if not (mentioned and is_correct):
        position = None

    sentiment = str(obj.get("sentiment") or "neutral").strip().lower()
    if sentiment not in _VALID_SENTIMENT:
        sentiment = "neutral"
    if not (mentioned and is_correct):
        sentiment = "neutral"

    citations = _clean_urls(obj.get("cited_sources")) or list(fallback_citations)

    return Classification(
        mentioned=mentioned,
        is_correct_brand=is_correct,
        position=position,
        sentiment=sentiment,
        competitors_mentioned=_normalize_competitors(
            obj.get("competitors_mentioned"), brand.competitor_set
        ),
        cited_sources=citations,
    )


def build_prompt(brand: Brand, prompt: Prompt, answer_text: str) -> str:
    return _PROMPT_TEMPLATE.format(
        brand_name=brand.brand_name,
        aliases=", ".join(brand.aliases),
        disambiguation=brand.disambiguation or "(none)",
        competitors=", ".join(brand.competitor_set),
        question=prompt.text[:1000],
        answer=(answer_text or "").strip()[:6000],
    )


def classify_answer(brand: Brand, prompt: Prompt, answer_text: str,
                    answer_citations: list[str] | None = None) -> Classification:
    """Judge one answer. Fail-CLOSED: on any error return a not-mentioned verdict
    with ``error`` set (never raises)."""
    fallback = _clean_urls(answer_citations or [])
    if not (answer_text or "").strip():
        # Empty answer -> definitively not mentioned; no judge call needed.
        return Classification(cited_sources=fallback)
    try:
        raw = _judge_raw(build_prompt(brand, prompt, answer_text))
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.warning("ai_visibility classifier: Haiku call failed for %s/%s: %s",
                    brand.key, prompt.id, exc)
        return Classification(cited_sources=fallback, error=str(exc))
    obj = _extract_json(raw)
    if obj is None:
        log.warning("ai_visibility classifier: unparseable judge output for %s/%s",
                    brand.key, prompt.id)
        return Classification(cited_sources=fallback, error="unparseable judge output")
    return _coerce(obj, brand, fallback)
