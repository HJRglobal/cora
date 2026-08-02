"""Deterministic email egress guard (R2) -- the outbound-email twin of
channel_content_guard, per design section 7 (locked 2026-08-01).

Runs on EVERY outbound email surface, all tiers: at stage time AND again at
send time (config can change in between), and on Tier-0 drafts (PHI hard-block;
other classes annotate the notification).

BLOCK classes 1-7, WARN class 8:
  1 em_dash          U+2014 anywhere (hard rule, locked 2026-07-31); U+2013 = WARN
  2 health_claims    claims lexicon on F3E threads
  3 nsf_context      "NSF" outside an Energy-product context (Energy only)
  4 press_figures    raise/valuation/stake dollar figures on Press threads
  5 founded_2022     canon: founded 2023
  6 phi              phi_guard.is_any_phi, FAIL-CLOSED (guard error = block)
  7 internal_refs    internal paths/links (reuse reply_formatter patterns, but
                     BLOCK rather than silently rewrite on sends)
  8 retail_price     WARN: $ figure on a Retail thread outside the canonical set

Quoted reply lines ('>'-prefixed) are the counterparty's words, not ours:
classes 2/3/4/5/8 skip them (lens: claims words inside quoted customer text
must not block a legitimate reply). Classes 1/6/7 scan the FULL text: an
em-dash, PHI, or an internal path must never leave the building even quoted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..reply_formatter import _BARE_DOC_URL_RE, _DRIVE_PATH_RE
from .. import phi_guard

logger = logging.getLogger("cora.revops.email_egress_guard")

# --- class 2: health/disease claims (F3E threads) --------------------------

_HEALTH_CLAIMS_RE = re.compile(
    r"\b(cure[sd]?|curing|treat(?:s|ed|ing|ment)?|prevent(?:s|ed|ion)?|"
    r"disease[s]?|anxiety|depression|insomnia|adhd|diabetes|cancer|"
    r"heal(?:s|ing)?|remedy|sleep\s+aid|anxiety\s+relief|"
    r"clinically\s+proven|fda[\s-]+approved)\b",
    re.IGNORECASE,
)

# --- class 3: NSF context ---------------------------------------------------

_NSF_RE = re.compile(r"\bNSF\b")

# --- class 4: press finance figures ----------------------------------------

_DOLLAR_FIGURE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:[kKmMbB]\b|million|billion)?")
_PRESS_FINANCE_WORDS_RE = re.compile(
    r"\b(rais(?:e[sd]?|ing)|valuation|stake|funding|round|invest(?:ment|or|ing)?s?)\b",
    re.IGNORECASE,
)

# --- class 5: founded 2022 ---------------------------------------------------

_FOUNDED_2022_RE = re.compile(r"\bfounded\s+(?:in\s+)?2022\b", re.IGNORECASE)

# --- class 7: internal paths/links ------------------------------------------

_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s<>|]+")
_INTERNAL_SCHEME_RE = re.compile(r"\b(?:computer|slack)://\S+", re.IGNORECASE)

# --- class 8: canonical retail price set -------------------------------------

CANONICAL_RETAIL_PRICES = frozenset(
    {"25.15", "22.19", "18.50", "22.43", "19.79", "16.50", "36.99", "32.99", "39.99"}
)
_PRICE_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\n])\s+")


@dataclass
class GuardResult:
    blocks: list[dict[str, str]] = field(default_factory=list)
    warns: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocks

    def block(self, class_id: int, class_name: str, reason: str) -> None:
        self.blocks.append(
            {"class_id": str(class_id), "class": class_name, "reason": reason}
        )

    def warn(self, class_id: int, class_name: str, reason: str) -> None:
        self.warns.append(
            {"class_id": str(class_id), "class": class_name, "reason": reason}
        )

    def to_dict(self) -> dict[str, Any]:
        return {"blocks": self.blocks, "warns": self.warns}

    def summary(self) -> str:
        if self.ok and not self.warns:
            return "clean"
        parts = []
        if self.blocks:
            parts.append("BLOCK: " + "; ".join(b["reason"] for b in self.blocks))
        if self.warns:
            parts.append("WARN: " + "; ".join(w["reason"] for w in self.warns))
        return " | ".join(parts)


def _strip_quoted_lines(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
    )


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def check_email(
    text: Optional[str],
    *,
    workstream: Optional[str] = None,
    entity: Optional[str] = None,
) -> GuardResult:
    """Run all guard classes over an outbound email body (or note text).

    FAIL-CLOSED: any unexpected error inside the guard yields a BLOCK
    (guard_error) rather than a silent pass.
    """
    result = GuardResult()
    try:
        _check_email_inner(result, text or "", workstream=workstream, entity=entity)
    except Exception:  # noqa: BLE001 - fail closed by contract
        logger.exception("email egress guard crashed; failing closed (BLOCK)")
        result.block(0, "guard_error", "guard crashed; failing closed")
    return result


def _check_email_inner(
    result: GuardResult, text: str, *, workstream: Optional[str], entity: Optional[str]
) -> None:
    ws = (workstream or "").strip()
    ent = (entity or "").strip().upper()
    unquoted = _strip_quoted_lines(text)

    # 1. em-dash anywhere (full text, including quoted lines)
    if "—" in text:
        result.block(1, "em_dash", "em-dash (U+2014) present; hard rule, rewrite without it")
    if "–" in text:
        result.warn(1, "en_dash", "en-dash (U+2013) present; prefer a plain hyphen or rewrite")

    # 2. health/disease claims on F3E threads (unquoted text only)
    if ent == "F3E":
        m = _HEALTH_CLAIMS_RE.search(unquoted)
        if m:
            result.block(
                2, "health_claims", f"health/disease claim language: {m.group(0)!r}"
            )

    # 3. NSF outside an Energy-product context (unquoted; sentence-scoped)
    for sentence in _sentences(unquoted):
        if _NSF_RE.search(sentence):
            low = sentence.lower()
            if "pure" in low or "energy" not in low:
                result.block(
                    3,
                    "nsf_context",
                    "NSF referenced outside an Energy-product context "
                    "(NSF Certified for Sport = Energy only)",
                )
                break

    # 4. raise/valuation/stake dollar figures on Press threads (unquoted)
    if ws == "Press":
        for sentence in _sentences(unquoted):
            if _DOLLAR_FIGURE_RE.search(sentence) and _PRESS_FINANCE_WORDS_RE.search(
                sentence
            ):
                result.block(
                    4,
                    "press_figures",
                    "raise/valuation/stake dollar figure on a Press thread "
                    "(embargo doctrine: no figures before the formal announcement)",
                )
                break

    # 5. founded 2022 (canon: 2023) (unquoted)
    if _FOUNDED_2022_RE.search(unquoted):
        result.block(5, "founded_2022", "'founded 2022' contradicts canon (2023)")

    # 6. PHI (fail-closed; full text)
    try:
        phi_hit = phi_guard.is_any_phi(text)
    except Exception:  # noqa: BLE001 - fail closed
        logger.exception("phi_guard failed inside email egress guard; blocking")
        phi_hit = True
    if phi_hit:
        result.block(6, "phi", "PHI/LEX-sensitive content detected; thread ejected for review")

    # 7. internal paths/links (full text; BLOCK, never silently rewrite a send)
    for pattern, label in (
        (_BARE_DOC_URL_RE, "internal document link"),
        (_DRIVE_PATH_RE, "internal Drive path"),
        (_WINDOWS_PATH_RE, "local file path"),
        (_INTERNAL_SCHEME_RE, "internal link scheme"),
    ):
        m = pattern.search(text)
        if m:
            result.block(7, "internal_refs", f"{label}: {m.group(0)[:60]!r}")
            break

    # 8. WARN: non-canonical dollar figure on a Retail thread (unquoted)
    if ws == "Retail":
        for m in _PRICE_RE.finditer(unquoted):
            figure = m.group(1).replace(",", "")
            normalized = f"{float(figure):.2f}" if "." in figure else figure
            if normalized not in CANONICAL_RETAIL_PRICES:
                result.warn(
                    8,
                    "retail_price",
                    f"${figure} is not in the canonical price set; double-check "
                    "before sending (freight/misc figures are fine)",
                )
                break
