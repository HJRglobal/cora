"""Loader + validator for the frozen AI-visibility prompt basket.

The canonical instrument lives at ``data/maps/ai-visibility-prompts.yaml`` (a
byte-identical copy of the frozen Drive v1). This module parses it into typed,
immutable objects and enforces the frozen-instrument invariants so a corrupted
or accidentally-reworded basket fails loudly at load time rather than silently
skewing a weekly scan.

Invariants enforced:
  * ``version`` is a positive int; ``sampling.runs_per_prompt`` >= 1.
  * Every model in ``sampling.models`` is one of the four known grounded surfaces.
  * Every prompt id is globally unique across all brands.
  * Every prompt has non-empty id + text, a known intent, and a bool ``aided``.
  * Every brand has aliases + a competitor_set + at least one prompt.

PHI guard OFF: marketing data only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

# src/cora/ai_visibility/prompts.py -> parents[3] = repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASKET_PATH = _REPO_ROOT / "data" / "maps" / "ai-visibility-prompts.yaml"

# The four grounded model surfaces this engine queries directly. AIO (Otterly)
# is merged separately as a 5th engine and is deliberately NOT a basket model.
KNOWN_MODELS: frozenset[str] = frozenset(
    {"perplexity_sonar", "openai_web_search", "gemini_grounding", "claude_web"}
)
KNOWN_INTENTS: frozenset[str] = frozenset(
    {"discovery", "problem", "comparison", "branded"}
)

# The Otterly-sourced Google AI Overviews engine tag (a "model" in the DB, but
# never a basket-queried model).
AIO_MODEL = "aio_otterly"


class PromptBasketError(ValueError):
    """Raised when the prompt basket is missing, malformed, or violates an invariant."""


@dataclass(frozen=True)
class Prompt:
    """One frozen prompt row."""

    id: str
    text: str
    intent: str
    aided: bool
    brand: str  # brand key this prompt belongs to (energy|pure|mood)


@dataclass(frozen=True)
class Brand:
    """One brand's config + its prompt set."""

    key: str
    brand_name: str
    aliases: tuple[str, ...]
    disambiguation: str
    positioning: str
    competitor_set: tuple[str, ...]
    prompts: tuple[Prompt, ...]

    def prompt_ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.prompts)


@dataclass(frozen=True)
class Basket:
    """The whole frozen instrument."""

    version: int
    created: str
    owner: str
    runs_per_prompt: int
    models: tuple[str, ...]
    cadence: str
    brands: dict[str, Brand]

    def brand(self, key: str) -> Brand:
        try:
            return self.brands[key]
        except KeyError as exc:
            raise PromptBasketError(
                f"Unknown brand key {key!r}; known brands: {sorted(self.brands)}"
            ) from exc

    def brand_keys(self) -> tuple[str, ...]:
        return tuple(self.brands.keys())

    def all_prompts(self) -> tuple[Prompt, ...]:
        return tuple(p for b in self.brands.values() for p in b.prompts)

    def all_prompt_ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.all_prompts())

    def total_prompts(self) -> int:
        return len(self.all_prompts())


# ---------------------------------------------------------------------------
# Loading (cached; keyed by resolved path)
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache: dict[str, Basket] = {}


def _coerce_str_list(value, field: str, ctx: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PromptBasketError(f"{ctx}: {field} must be a list, got {type(value).__name__}")
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s)
    return tuple(out)


def _parse_prompt(raw: dict, brand_key: str, seen_ids: set[str]) -> Prompt:
    if not isinstance(raw, dict):
        raise PromptBasketError(
            f"brand {brand_key!r}: each prompt must be a mapping, got {type(raw).__name__}"
        )
    pid = str(raw.get("id", "")).strip()
    if not pid:
        raise PromptBasketError(f"brand {brand_key!r}: a prompt is missing its id")
    if pid in seen_ids:
        raise PromptBasketError(
            f"duplicate prompt id {pid!r} -- ids MUST be globally unique (frozen instrument)"
        )
    seen_ids.add(pid)

    text = str(raw.get("text", "")).strip()
    if not text:
        raise PromptBasketError(f"prompt {pid!r}: empty text")

    intent = str(raw.get("intent", "")).strip().lower()
    if intent not in KNOWN_INTENTS:
        raise PromptBasketError(
            f"prompt {pid!r}: unknown intent {intent!r}; expected one of {sorted(KNOWN_INTENTS)}"
        )

    aided_raw = raw.get("aided")
    if not isinstance(aided_raw, bool):
        raise PromptBasketError(
            f"prompt {pid!r}: 'aided' must be a bool, got {type(aided_raw).__name__}"
        )

    return Prompt(id=pid, text=text, intent=intent, aided=aided_raw, brand=brand_key)


def _parse_brand(brand_key: str, raw: dict, seen_ids: set[str]) -> Brand:
    if not isinstance(raw, dict):
        raise PromptBasketError(f"brand {brand_key!r}: must be a mapping")
    brand_name = str(raw.get("brand_name", "")).strip()
    if not brand_name:
        raise PromptBasketError(f"brand {brand_key!r}: missing brand_name")

    aliases = _coerce_str_list(raw.get("aliases"), "aliases", f"brand {brand_key!r}")
    if not aliases:
        raise PromptBasketError(f"brand {brand_key!r}: must declare at least one alias")

    competitor_set = _coerce_str_list(
        raw.get("competitor_set"), "competitor_set", f"brand {brand_key!r}"
    )
    if not competitor_set:
        raise PromptBasketError(f"brand {brand_key!r}: must declare a competitor_set")

    prompts_raw = raw.get("prompts")
    if not isinstance(prompts_raw, list) or not prompts_raw:
        raise PromptBasketError(f"brand {brand_key!r}: must declare at least one prompt")
    prompts = tuple(_parse_prompt(p, brand_key, seen_ids) for p in prompts_raw)

    return Brand(
        key=brand_key,
        brand_name=brand_name,
        aliases=aliases,
        disambiguation=str(raw.get("disambiguation", "")).strip(),
        positioning=str(raw.get("positioning", "")).strip(),
        competitor_set=competitor_set,
        prompts=prompts,
    )


def _parse_basket(data: dict) -> Basket:
    if not isinstance(data, dict):
        raise PromptBasketError("basket root must be a mapping")

    try:
        version = int(data.get("version"))
    except (TypeError, ValueError) as exc:
        raise PromptBasketError("basket 'version' must be an int") from exc
    if version < 1:
        raise PromptBasketError(f"basket 'version' must be >= 1, got {version}")

    sampling = data.get("sampling") or {}
    if not isinstance(sampling, dict):
        raise PromptBasketError("'sampling' must be a mapping")
    try:
        runs_per_prompt = int(sampling.get("runs_per_prompt"))
    except (TypeError, ValueError) as exc:
        raise PromptBasketError("sampling.runs_per_prompt must be an int") from exc
    if runs_per_prompt < 1:
        raise PromptBasketError(
            f"sampling.runs_per_prompt must be >= 1, got {runs_per_prompt}"
        )

    models = _coerce_str_list(sampling.get("models"), "models", "sampling")
    if not models:
        raise PromptBasketError("sampling.models must list at least one model")
    unknown = [m for m in models if m not in KNOWN_MODELS]
    if unknown:
        raise PromptBasketError(
            f"sampling.models has unknown model(s) {unknown}; known: {sorted(KNOWN_MODELS)}"
        )

    brands_raw = data.get("brands")
    if not isinstance(brands_raw, dict) or not brands_raw:
        raise PromptBasketError("'brands' must be a non-empty mapping")

    seen_ids: set[str] = set()
    brands: dict[str, Brand] = {}
    for brand_key, braw in brands_raw.items():
        brands[str(brand_key)] = _parse_brand(str(brand_key), braw, seen_ids)

    return Basket(
        version=version,
        created=str(data.get("created", "")).strip(),
        owner=str(data.get("owner", "")).strip(),
        runs_per_prompt=runs_per_prompt,
        models=models,
        cadence=str(sampling.get("cadence", "")).strip(),
        brands=brands,
    )


def load_basket(path: str | Path | None = None, *, use_cache: bool = True) -> Basket:
    """Load + validate the prompt basket. Raises PromptBasketError on any problem.

    Cached per resolved path (tests that write a temp basket pass use_cache=False).
    """
    resolved = Path(path) if path else DEFAULT_BASKET_PATH
    resolved = resolved.resolve()
    key = str(resolved)

    if use_cache:
        with _cache_lock:
            cached = _cache.get(key)
        if cached is not None:
            return cached

    if not resolved.exists():
        raise PromptBasketError(f"prompt basket not found at {resolved}")
    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptBasketError(f"cannot read prompt basket at {resolved}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise PromptBasketError(f"prompt basket at {resolved} is not valid YAML: {exc}") from exc

    basket = _parse_basket(data)

    if use_cache:
        with _cache_lock:
            _cache[key] = basket
    return basket


def clear_cache() -> None:
    """Drop the in-memory basket cache (tests + a future live-reload)."""
    with _cache_lock:
        _cache.clear()
