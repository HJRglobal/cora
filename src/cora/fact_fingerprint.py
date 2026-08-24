"""Same-fact fingerprinting for propose-once lanes (C6 / cq-0d40bb50bdb1).

WHY THIS IS A SHARED MODULE AND NOT AN IMPORT
`friction_mining` already owns this pattern (exact fingerprint + fuzzy >= 0.85 +
a JSONL ledger written at PROPOSAL time, so a finding never re-proposes
regardless of outcome -- D-030). But friction_mining does
`from .reconciliation_engine import _cosine_sim, _extract_sentences` at module
level, so reconciliation_engine cannot import friction_mining back. Extracting
the rule into a leaf module with no cora imports is the same shape the repo
already used for `lex_sub_entity.py`.

WHY 0.85 SequenceMatcher IS NOT ENOUGH HERE, measured
The live pass-5 corpus (187 rows, 2026-06-13..08-24) contains one CBS Northstar
POS quote proposed on EIGHT consecutive nights, six of which Harrison filed --
one vendor decision in the decisions inbox six times. Haiku re-reads the same
Drive digest each night and paraphrases the same sentence: "$200 professional
services charge" / "...allocation" / "...fee" / "...cost". Replaying the
candidate rules over that corpus:

    SequenceMatcher >= 0.85 (the friction rule)   CBS 8 -> 4
    SequenceMatcher >= 0.80                       CBS 8 -> 3
    Jaccard on content words >= 0.60              CBS 8 -> 3
    containment (overlap coefficient) >= 0.80     CBS 8 -> 2   <- best

SequenceMatcher is weak against exactly the single-word substitution an LLM
produces; containment is not, because the substituted word is one token out of
twenty. So `same_fact` ORs the two: SequenceMatcher catches reorderings,
containment catches substitutions.

FINGERPRINT THE SUMMARY, NEVER THE DESCRIPTION. Every pass-5 decision
description carries the constant 38-char prefix "[OSN] Uncaptured decision in
Drive: ", which inflates the ratio between UNRELATED decisions -- replayed, that
suppressed MORE of the corpus (187 -> 150) than fingerprinting the summary did,
and the extra suppression was prefix noise, not real matches.

BOUNDED BY CONSTRUCTION. An O(n^2) fuzzy scan of the 18 MB / 19,842-row
proposed-updates archive did not finish in 100 seconds when measured. This
ledger is its own small file with a time window, exactly like the friction and
code-queue ledgers -- never a scan of a general ledger.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger(__name__)

# Two comparators, OR'd. See the module docstring for the measurement.
FUZZY_RATIO = 0.85
CONTAINMENT_RATIO = 0.80
# How far back the ledger is consulted. Long enough to cover a recurrence that
# fires nightly for weeks; short enough that the scan stays trivial.
DEFAULT_WINDOW_DAYS = 120
_MAX_NORMALIZED = 160

# Tokens that carry no discriminating signal for containment. Deliberately tiny:
# an over-eager stop list makes two different facts look identical, which is the
# failure mode that matters here (a suppressed decision is never seen again).
_STOPWORDS = frozenset({
    "a", "an", "and", "at", "by", "for", "from", "in", "is", "of", "on", "or",
    "the", "to", "with",
})


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, cap length.

    Byte-identical to friction_mining's rule so a fingerprint computed here and
    one computed there agree.
    """
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()[:_MAX_NORMALIZED]


def compute_fingerprint(kind: str, representative: str) -> str:
    """Stable 12-hex fingerprint of one fact.

    hashlib, NOT the builtin `hash()`. `hash()` over a str is siphash-randomized
    per interpreter and PYTHONHASHSEED is not pinned anywhere in this repo, so
    ids built with it differ ACROSS RUNS -- which is the root cause of the six
    duplicate filings: two nights produced byte-identical decision text and
    still got different ids, so every downstream exact-id dedup was a no-op.
    """
    return hashlib.md5(
        f"{kind}|{normalize(representative)}".encode()
    ).hexdigest()[:12]


def _content_tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if t and t not in _STOPWORDS}


def containment(a: str, b: str) -> float:
    """Overlap coefficient over content words: |A n B| / min(|A|, |B|).

    Unlike a ratio over the whole string, one substituted word out of twenty
    barely moves this -- which is precisely the LLM-paraphrase case.
    """
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def same_fact(a: str, b: str) -> bool:
    """Do these two summaries state the same fact?"""
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return False
    na, nb = normalize(a), normalize(b)
    if na and na == nb:
        return True
    if SequenceMatcher(None, na, nb).ratio() >= FUZZY_RATIO:
        return True
    return containment(a, b) >= CONTAINMENT_RATIO


# ── ledger ──────────────────────────────────────────────────────────────────

def _iter_ledger(path: Path, window_days: int):
    if not path.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        if str(rec.get("ts") or "") < cutoff:
            continue
        yield rec


def already_proposed(path: Path, kind: str, representative: str, *,
                     scope: str = "", window_days: int = DEFAULT_WINDOW_DAYS) -> str:
    """The fingerprint of a prior proposal of this same fact, or "".

    `scope` (an entity) gates the fuzzy comparison: two entities can legitimately
    make near-identical decisions and suppressing the second would lose it.

    FAIL-OPEN. A ledger read error must let the item through -- proposing a
    duplicate is recoverable, silently dropping a real decision is not.
    """
    try:
        fp = compute_fingerprint(kind, representative)
        for rec in _iter_ledger(Path(path), window_days):
            if rec.get("kind") and rec.get("kind") != kind:
                continue
            if rec.get("fingerprint") == fp:
                return str(rec["fingerprint"])
            if scope and str(rec.get("scope") or "") != scope:
                continue
            if same_fact(representative, str(rec.get("representative") or "")):
                return str(rec.get("fingerprint") or fp)
    except Exception:  # noqa: BLE001
        log.warning("fact_fingerprint: ledger read failed (allowing)", exc_info=True)
    return ""


def record_proposal(path: Path, kind: str, representative: str, *,
                    scope: str = "", ref: str = "") -> str:
    """Append one fingerprint row at PROPOSAL time and return the fingerprint.

    At proposal time, not at approval time (D-030): a finding must never
    re-propose regardless of what the human decided about it. Fail-soft -- a
    write error must not block the proposal it was recording.
    """
    fp = compute_fingerprint(kind, representative)
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "fingerprint": fp,
                "scope": scope,
                "ref": ref,
                "representative": str(representative or "")[:400],
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.warning("fact_fingerprint: ledger write failed", exc_info=True)
    return fp
