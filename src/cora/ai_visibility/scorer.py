"""0-100 AI-visibility composite scorer (pure math; no I/O).

Per brand, over the brand's own prompts, aggregated across the models present:

  * Presence  40%  = share of runs where the CORRECT brand is mentioned
  * Share of AI voice 25% = brand mentions / (brand + competitor mentions)
  * Position  20%  = normalized prominence (1st=100, 2nd=66, 3rd=33, >=4th=15,
                     present-but-unranked=33, absent=0), averaged over runs
  * Sentiment 15%  = positive=100 / neutral=60 / mixed=40 / negative=0, averaged
                     over the runs where the brand is a hit
  * Composite = weighted sum -> 0-100 (components stored for audit, unlike BeFound)

Engine model: each metric is computed PER ENGINE, then averaged across the
engines present (equal weight per engine). For the 4 direct surfaces with equal
run counts this equals pooling all runs; when an engine has fewer successful
runs, per-engine-mean keeps each engine's weight equal (more correct than
pooling). Google AI Overviews (via Otterly) is merged in as a 5th engine at
equal weight -- our direct scans stay authoritative for the 4 they query
directly, and the AIO slice contributes 1/(N+1) of the composite. Both the
merged ``composite`` and ``composite_direct_only`` are stored.

PHI guard OFF: marketing/visibility data only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WEIGHTS = {"presence": 0.40, "share_of_voice": 0.25, "position": 0.20, "sentiment": 0.15}

_SENTIMENT_VALUE = {"positive": 100.0, "neutral": 60.0, "mixed": 40.0, "negative": 0.0}
# Present-but-unranked prominence: a hit whose position the judge could not rank.
_UNRANKED_POSITION = 33.0


@dataclass
class RunVerdict:
    """One classified run, flattened for scoring."""

    model: str
    intent: str
    aided: bool
    is_hit: bool
    position: int | None
    sentiment: str
    competitor_count: int


@dataclass
class EngineComponents:
    presence: float = 0.0
    share_of_voice: float = 0.0
    position: float = 0.0
    sentiment: float = 0.0
    composite: float = 0.0
    n_runs: int = 0
    n_hits: int = 0


@dataclass
class AioMetrics:
    presence: float = 0.0
    share_of_voice: float = 0.0
    position: float = 0.0
    sentiment: float = 0.0
    composite: float = 0.0


@dataclass
class BrandScore:
    brand: str
    presence: float = 0.0
    share_of_voice: float = 0.0
    position: float = 0.0
    sentiment: float = 0.0
    composite: float = 0.0
    composite_direct_only: float = 0.0
    unaided_presence: float = 0.0
    per_intent: dict[str, float] = field(default_factory=dict)
    engines: list[str] = field(default_factory=list)
    aio_presence: float | None = None
    aio_share_of_voice: float | None = None
    aio_position: float | None = None
    aio_sentiment: float | None = None
    aio_composite: float | None = None
    per_engine: dict[str, dict] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def _ladder(pos: int) -> float:
    return {1: 100.0, 2: 66.0, 3: 33.0}.get(pos, 15.0 if pos and pos >= 4 else 0.0)


def position_value(is_hit: bool, position: int | None) -> float:
    if not is_hit:
        return 0.0
    if position is None:
        return _UNRANKED_POSITION
    return _ladder(int(position))


def sentiment_value(label: str) -> float:
    return _SENTIMENT_VALUE.get((label or "").strip().lower(), 60.0)


def composite_of(presence: float, sov: float, position: float, sentiment: float) -> float:
    return round(
        WEIGHTS["presence"] * presence
        + WEIGHTS["share_of_voice"] * sov
        + WEIGHTS["position"] * position
        + WEIGHTS["sentiment"] * sentiment,
        2,
    )


def _mean(values: list[float], default: float = 0.0) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else default


def _engine_components(runs: list[RunVerdict]) -> EngineComponents:
    if not runs:
        return EngineComponents()
    total = len(runs)
    hits = [r for r in runs if r.is_hit]
    brand_count = len(hits)
    comp_total = sum(max(0, r.competitor_count) for r in runs)

    presence = 100.0 * brand_count / total
    sov = 100.0 * brand_count / (brand_count + comp_total) if (brand_count + comp_total) else 0.0
    position = _mean([position_value(r.is_hit, r.position) for r in runs])
    sentiment = _mean([sentiment_value(r.sentiment) for r in hits]) if hits else 0.0
    return EngineComponents(
        presence=round(presence, 2),
        share_of_voice=round(sov, 2),
        position=round(position, 2),
        sentiment=round(sentiment, 2),
        composite=composite_of(presence, sov, position, sentiment),
        n_runs=total,
        n_hits=brand_count,
    )


def _presence_over(verdicts: list[RunVerdict], engines: list[str], filt) -> float:
    """Per-engine-mean presence over the filtered subset (engines with no
    matching runs are dropped from the mean)."""
    per_engine: list[float] = []
    for m in engines:
        subset = [v for v in verdicts if v.model == m and filt(v)]
        if subset:
            per_engine.append(100.0 * sum(1 for v in subset if v.is_hit) / len(subset))
    return round(_mean(per_engine), 2)


# ---------------------------------------------------------------------------
# AIO metric derivation (from an Otterly slice's primitive fields)
# ---------------------------------------------------------------------------
def _nss_to_100(nss: float) -> float:
    if -1.0 <= nss <= 1.0:
        return round((nss + 1.0) / 2.0 * 100.0, 2)
    v = max(-100.0, min(100.0, float(nss)))
    return round((v + 100.0) / 2.0, 2)


def _aio_sentiment_score(sentiment) -> float | None:
    if isinstance(sentiment, dict):
        pos = sentiment.get("positive")
        neu = sentiment.get("neutral")
        neg = sentiment.get("negative")
        nums = [x for x in (pos, neu, neg) if isinstance(x, (int, float))]
        total = sum(nums)
        if nums and total > 0:
            p = pos if isinstance(pos, (int, float)) else 0
            n = neu if isinstance(neu, (int, float)) else 0
            g = neg if isinstance(neg, (int, float)) else 0
            return round((p * 100.0 + n * 60.0 + g * 0.0) / total, 2)
        nss = sentiment.get("nss")
        if isinstance(nss, (int, float)):
            return _nss_to_100(nss)
    elif isinstance(sentiment, str) and sentiment.strip():
        return sentiment_value(sentiment)
    return None


def aio_metrics(*, presence, share_of_voice=None, average_rank=None,
                sentiment=None) -> AioMetrics | None:
    """Build AioMetrics from an Otterly slice's primitives. None if no presence
    signal (i.e. AIO unavailable for this brand)."""
    if presence is None:
        return None
    pres = float(presence)
    sov = float(share_of_voice) if share_of_voice is not None else 0.0
    # average_rank is prominence GIVEN the brand appeared; the direct engines'
    # position is averaged over ALL runs (misses -> 0), i.e. presence-diluted.
    # Scale the AIO prominence by presence so the merged 5th-engine position is
    # on the same basis as the 4 direct engines.
    if average_rank is not None:
        pos = _ladder(int(round(float(average_rank)))) * (pres / 100.0)
    else:
        pos = _UNRANKED_POSITION * (pres / 100.0) if pres > 0 else 0.0
    pos = round(pos, 2)
    sent = _aio_sentiment_score(sentiment)
    if sent is None:
        sent = 60.0 if pres > 0 else 0.0  # neutral default when the brand is present
    return AioMetrics(
        presence=round(pres, 2),
        share_of_voice=round(sov, 2),
        position=round(pos, 2),
        sentiment=round(sent, 2),
        composite=composite_of(pres, sov, pos, sent),
    )


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------
def score_brand(verdicts: list[RunVerdict], *, aio: AioMetrics | None = None) -> BrandScore:
    """Compute the full BrandScore. Direct components = mean over present
    engines; ``composite`` merges AIO as a 5th engine at equal weight."""
    engines = sorted({v.model for v in verdicts})
    per_engine: dict[str, EngineComponents] = {m: _engine_components(
        [v for v in verdicts if v.model == m]) for m in engines}

    presence = round(_mean([per_engine[m].presence for m in engines]), 2)
    sov = round(_mean([per_engine[m].share_of_voice for m in engines]), 2)
    position = round(_mean([per_engine[m].position for m in engines]), 2)
    # Sentiment is a hits-only signal (per the module contract). Average it ONLY
    # across engines that actually had >=1 hit -- a no-hit engine's 0.0 sentinel
    # must not vote (absence is already penalized by presence; counting it here
    # would double-penalize and understate a positively-mentioned brand).
    sentiment = round(_mean([per_engine[m].sentiment for m in engines
                             if per_engine[m].n_hits > 0]), 2)
    direct_composite = composite_of(presence, sov, position, sentiment)

    present_intents = sorted({v.intent for v in verdicts})
    per_intent = {
        intent: _presence_over(verdicts, engines, lambda v, i=intent: v.intent == i)
        for intent in present_intents
    }
    unaided = _presence_over(verdicts, engines, lambda v: not v.aided)

    composite = direct_composite
    aio_fields: tuple = (None, None, None, None, None)
    if aio is not None:
        n = len(engines)
        composite = round((n * direct_composite + aio.composite) / (n + 1), 2) if n else aio.composite
        aio_fields = (aio.presence, aio.share_of_voice, aio.position, aio.sentiment, aio.composite)

    return BrandScore(
        brand="",
        presence=presence,
        share_of_voice=sov,
        position=position,
        sentiment=sentiment,
        composite=composite,
        composite_direct_only=direct_composite,
        unaided_presence=unaided,
        per_intent=per_intent,
        engines=engines,
        aio_presence=aio_fields[0],
        aio_share_of_voice=aio_fields[1],
        aio_position=aio_fields[2],
        aio_sentiment=aio_fields[3],
        aio_composite=aio_fields[4],
        per_engine={m: {
            "presence": per_engine[m].presence,
            "share_of_voice": per_engine[m].share_of_voice,
            "position": per_engine[m].position,
            "sentiment": per_engine[m].sentiment,
            "composite": per_engine[m].composite,
            "n_runs": per_engine[m].n_runs,
        } for m in engines},
    )
