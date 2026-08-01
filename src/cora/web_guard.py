"""Web-search egress gate + usage governor for the Anthropic server-side web tools.

Cora answers live-web questions via the Messages API server tools
(web_search_20260209 / web_fetch_20260209). Attaching those tools means the
model may compose search queries that LEAVE the machine, so enablement is
deterministic and fail-closed (kickoff 2026-07-31, gate lifted by Harrison):

  1. LEX scope (entity LEX or any LEX-* sub-entity) NEVER gets web tools in v1.
  2. Tools attach only on (a) explicit web intent ("search the web",
     "current price of ...") or (b) a time-sensitive question whose KB
     retrieval came back empty/weak (kb_best_distance past the relevance gate).
     Never on plate / confirm / forced-tool / cheap paths (app.py enforces the
     force_tool exclusion; the intent gate handles the rest).
  3. The user query is screened before any attach: phi_guard.is_any_phi
     (the shared 3-predicate union -- the doctrine home for every egress
     checkpoint), Visibility-CPA names, email addresses, and internal
     $-figures. Any trip or ANY exception -> tools not attached. A block here
     is a soft degradation (the reply falls back to KB-only behavior), never
     a user-facing refusal, so the screen is deliberately recall-biased.
  4. A daily search cap (CORA_WEB_SEARCH_DAILY_CAP) bounds spend; per-call
     max_uses rides on the tool definitions in claude_client.

The usage ledger records decisions and per-call search counts. Raw query text
is NEVER persisted (D-082 posture) -- only the decision reason and scope.

Residual (documented, accepted for v1): once tools are attached, the MODEL
composes the actual search strings from its full context; that layer is
governed by the WEB_MODE_CONTEXT prompt rule below plus the blocked-domains
list, not by this deterministic gate. LEX-off + the query screen + the
internal-domain blocklist are the deterministic belts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import phi_guard

log = logging.getLogger("cora.web_guard")

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Module constant so tests can redirect it (registered in tests/conftest.py
# _LEDGER_CONSTS); never hand-edit the live file.
_USAGE_LEDGER = _REPO_ROOT / "data" / "state" / "web-search-usage.jsonl"

# ---------------------------------------------------------------------------
# Env knobs (read lazily so a .env edit + restart flips them; no import cache)
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    return os.environ.get("CORA_WEB_TOOLS", "on").strip().lower() not in (
        "off",
        "0",
        "false",
        "no",
    )


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def search_max_uses() -> int:
    return _int_env("CORA_WEB_SEARCH_MAX_USES", 3)


def fetch_max_uses() -> int:
    return _int_env("CORA_WEB_FETCH_MAX_USES", 2)


def daily_cap() -> int:
    return _int_env("CORA_WEB_SEARCH_DAILY_CAP", 40)


def _kb_miss_distance() -> float:
    try:
        return float(os.environ.get("CORA_WEB_KB_MISS_DISTANCE", "") or 1.30)
    except (TypeError, ValueError):
        return 1.30


# ---------------------------------------------------------------------------
# Intent classification (pure regex -- no network, no model call)
# ---------------------------------------------------------------------------

# Explicit ask for live-web information. Deliberately narrow: internal
# phrasings ("what's the latest on the OSN recon") must NOT trip this --
# bare "latest"/"today" are excluded; the market-price leg requires a
# now-anchor ("right now" / "currently" / "these days" / "today").
_WEB_INTENT_RE = re.compile(
    r"(?:"
    r"\bsearch(?:\s+\w+){0,2}\s+(?:the\s+)?(?:web|internet|online)\b"
    r"|\b(?:look|looked|looking)\s*(?:it\s+)?up\s+(?:online|on\s+the\s+(?:web|internet))\b"
    r"|\bgoogle\s+(?:it|for|this|that|\w+)\b"
    r"|\bweb\s+search\b"
    r"|\b(?:current|latest|live|today's)\s+(?:price|prices|pricing|cost|news|headlines|rate|rates)\b"
    r"|\b(?:price|prices|cost|costs|going\s+for|selling\s+for|retail(?:ing)?\s+(?:at|for))\b"
    r".{0,50}?\b(?:right\s+now|currently|these\s+days|at\s+the\s+moment|today)\b"
    r"|\bin\s+the\s+news\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)

# Time-sensitivity signal -- weaker than explicit intent; only attaches when
# KB retrieval also came back empty/weak for the question.
_TIME_SENSITIVE_RE = re.compile(
    r"(?:"
    r"\bright\s+now\b|\bcurrently\b|\bas\s+of\s+(?:now|today)\b"
    r"|\b(?:latest|current|live|breaking)\s+\w+"
    r"|\bup[- ]to[- ]date\b"
    r"|\b(?:stock|market|share)\s+price\b|\bexchange\s+rate\b"
    r"|\bnews\b|\bheadlines?\b"
    r")",
    re.IGNORECASE,
)


def is_web_intent(text: str) -> bool:
    """Explicit live-web intent in the user's own words."""
    return bool(text and _WEB_INTENT_RE.search(text))


def is_time_sensitive(text: str) -> bool:
    """The question is anchored to the present (weaker signal than intent)."""
    return bool(text and _TIME_SENSITIVE_RE.search(text))


def _kb_missed(kb_meta: dict | None) -> bool:
    """True when KB retrieval gave the question no relevant grounding.

    kb_relevant_hits==0 is empirically unreachable at ~560K chunks, so the
    real signal is kb_best_distance past the relevance gate (context_loader
    _KB_MAX_DISTANCE=1.30). No search ran / no distance -> treated as a miss
    (a KB outage should not blind a time-sensitive question).
    """
    if not kb_meta or not kb_meta.get("kb_search_ran"):
        return True
    best = kb_meta.get("kb_best_distance")
    if best is None:
        return True
    try:
        return float(best) >= _kb_miss_distance()
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Query egress screen (fail-closed)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Internal $-figure screen: a dollar amount is blocked only when internal
# finance vocabulary or an internal entity token rides in the same query --
# "search the web for laptops under $1000" stays legitimate.
_CURRENCY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?\b")
_FINANCE_VOCAB_RE = re.compile(
    r"\b(?:revenue|cash|balance|payroll|p&l|profit|ebitda|margin|invoice"
    r"|receivables?|payables?|runway|deposit|wire|close\s+pack|burn)\b",
    re.IGNORECASE,
)
_ENTITY_TOKEN_RE = re.compile(
    r"\b(?:F3E|F3\s+Energy|OSN|Old\s+School\s+Nutrition|Lexington|LBHS"
    r"|HJR|HJRG|HJRP|BDM|Big\s+D\s+Media|UFL)\b",
    re.IGNORECASE,
)


def _screen_query(text: str) -> str | None:
    """Return a block-reason slug, or None when the query may leave the machine."""
    if phi_guard.is_any_phi(text):
        return "phi"
    if phi_guard.is_visibility_cpa_mention(text):
        return "cpa_name"
    if _EMAIL_RE.search(text):
        return "email"
    if _CURRENCY_RE.search(text) and (
        _FINANCE_VOCAB_RE.search(text) or _ENTITY_TOKEN_RE.search(text)
    ):
        return "internal_figure"
    return None


def _is_lex_scope(entity: str) -> bool:
    ent = (entity or "").upper()
    return ent == "LEX" or ent.startswith("LEX-")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebDecision:
    attach: bool
    reason: str


def evaluate(
    query: str,
    entity: str,
    kb_meta: dict | None = None,
    channel_name: str = "",
) -> WebDecision:
    """Decide whether this request may carry the server-side web tools.

    Fail-closed: any exception anywhere in the pipeline returns attach=False.
    """
    try:
        if not _enabled():
            return WebDecision(False, "disabled")
        if _is_lex_scope(entity):
            return WebDecision(False, "lex_scope")
        query = query or ""
        explicit = is_web_intent(query)
        fallback = not explicit and is_time_sensitive(query) and _kb_missed(kb_meta)
        if not explicit and not fallback:
            return WebDecision(False, "no_intent")
        blocked = _screen_query(query)
        if blocked:
            return WebDecision(False, f"blocked:{blocked}")
        if searches_today() >= daily_cap():
            return WebDecision(False, "daily_cap")
        return WebDecision(True, "explicit_intent" if explicit else "time_sensitive_kb_miss")
    except Exception:  # noqa: BLE001 -- fail-closed by doctrine
        log.exception("web_guard evaluate failed -- web tools withheld")
        return WebDecision(False, "error")


# ---------------------------------------------------------------------------
# Usage ledger + daily cap accounting (raw query text never persisted)
# ---------------------------------------------------------------------------


def _append_ledger(row: dict) -> None:
    try:
        _USAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(_USAGE_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")
    except OSError:
        log.warning("web_guard ledger write failed", exc_info=True)


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def record_decision(decision: WebDecision, entity: str, channel_name: str = "") -> None:
    """Ledger attaches and blocks (skip the high-volume no-intent rows)."""
    if decision.reason in ("no_intent", "disabled"):
        return
    _append_ledger(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "date": _today(),
            "event": "attach" if decision.attach else "block",
            "reason": decision.reason,
            "entity": entity,
            "channel": channel_name,
        }
    )


def record_usage(searches: int, fetches: int, entity: str) -> None:
    """Record actual server-tool consumption after a web-enabled call."""
    if not searches and not fetches:
        return
    _append_ledger(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "date": _today(),
            "event": "usage",
            "searches": int(searches or 0),
            "fetches": int(fetches or 0),
            "entity": entity,
        }
    )


def searches_today() -> int:
    """Sum of recorded searches for today (approximate cap -- checked pre-call)."""
    total = 0
    today = _today()
    try:
        if not _USAGE_LEDGER.exists():
            return 0
        with open(_USAGE_LEDGER, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if row.get("event") == "usage" and row.get("date") == today:
                    total += int(row.get("searches") or 0)
    except OSError:
        log.warning("web_guard ledger read failed", exc_info=True)
    return total


# ---------------------------------------------------------------------------
# Provenance context + citation rendering
# ---------------------------------------------------------------------------

# Injected into the uncached runtime context when web tools are attached.
# Known-Answers-win is the D-095 grounding rule extended to web results.
WEB_MODE_CONTEXT = (
    "## Web search mode\n"
    "The web_search and web_fetch tools are available for THIS question. "
    "Use them for live/current facts the internal context cannot answer. Rules:\n"
    "1. NEVER include personal names, client or health details, teammate emails, "
    "or internal financial figures in a search query -- search with generic terms.\n"
    "2. Name the source of every web-sourced claim inline (e.g. 'per Newegg'). "
    "Source links are appended automatically -- do not fabricate links.\n"
    "3. The Known Answers section and internal context ALWAYS win conflicts with "
    "web results: web results supplement internal canon, never override it.\n"
)

# Never render citations pointing at internal doc surfaces, even if a page
# somehow cites one (mirrors the reply_formatter source-opacity domains).
_INTERNAL_CITE_RE = re.compile(
    r"^(?:[\w-]+\.)*(?:docs\.google\.com|drive\.google\.com|app\.asana\.com"
    r"|notion\.so|intuit\.com|slack\.com)$",
    re.IGNORECASE,
)

# Domains the server-side tools must not search or fetch (interior surfaces;
# public marketing sites deliberately stay reachable).
BLOCKED_DOMAINS = [
    "docs.google.com",
    "drive.google.com",
    "app.asana.com",
    "notion.so",
    "intuit.com",
    "slack.com",
]


def _clean_label(label: str) -> str:
    """Make a citation title safe inside a Slack <url|label> token."""
    label = (label or "").strip()
    label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    label = label.replace("|", "/").replace("\n", " ")
    if len(label) > 60:
        label = label[:57].rstrip() + "..."
    return label


def format_sources_line(citations: list[dict] | None, max_sources: int = 4) -> str:
    """Compose a 'Sources:' line of sanctioned Slack link tokens.

    <url|label> tokens are protected end-to-end by reply_formatter Pass 1 and
    the slack_egress boundary, so they survive every downstream redaction pass.
    """
    if not citations:
        return ""
    seen: set[str] = set()
    tokens: list[str] = []
    for cite in citations:
        if not isinstance(cite, dict):
            continue
        url = (cite.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if any(ch in url for ch in "<>|") or any(ch.isspace() for ch in url):
            continue
        try:
            host = (urlparse(url).hostname or "").strip(".")
        except ValueError:
            continue
        if not host or _INTERNAL_CITE_RE.match(host):
            continue
        if url in seen:
            continue
        seen.add(url)
        label = _clean_label(cite.get("title") or host)
        tokens.append(f"<{url}|{label}>")
        if len(tokens) >= max_sources:
            break
    if not tokens:
        return ""
    return "Sources: " + " · ".join(tokens)
