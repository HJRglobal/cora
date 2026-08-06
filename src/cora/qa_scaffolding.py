"""Canonical [QA] smoke-traffic marker (D-104) -- ONE definition, every intake.

Harrison prefixes verification-week smoke messages with a literal ``[QA]``. Those
messages are test scaffolding, not organizational signal, and must never become a
durable item on ANY intake surface. Before this module the marker was honoured in
exactly one place -- ``gap_autofill``'s known-answer MINING eligibility screen --
which runs long AFTER a gap has already been logged. So a ``[QA]`` smoke message
still minted knowledge gaps, code-queue capture cards, and decision-inbox items.

Zero cora imports on purpose: every intake chokepoint (``knowledge_gaps.log_gap``,
``code_queue.capture_message_signal``, ``decision_inbox.screen_decision``,
``info_intake.ingest``) can import this without any cycle risk.

Two predicates, deliberately different in strictness:

``is_qa_message``   PREFIX-anchored, for RAW user message text. A mid-sentence
                    mention of QA inside a genuine message ("our [QA] process
                    changed") must not silently swallow real signal.
``contains_qa_marker`` the literal token ANYWHERE, for DERIVED or concatenated
                    text (a summary, a serialized payload) where the original
                    prefix position is long gone and a false negative -- smoke
                    traffic filed as canon -- is the worse error.
"""

from __future__ import annotations

import re

# Leading Slack mention tokens: "<@UCORA> [QA] ..." must still read as [QA].
_LEADING_MENTIONS_RE = re.compile(r"^(?:\s*<[@#!][^>]*>)+\s*")

# Optional leading Slack formatting characters, then the literal marker.
_QA_PREFIX_RE = re.compile(r"^\s*(?:[*_~`>\s]+)?\[qa\]", re.IGNORECASE)

_QA_ANYWHERE_RE = re.compile(r"\[qa\]", re.IGNORECASE)


def is_qa_message(text: str) -> bool:
    """True when raw message `text` carries the literal [QA] prefix."""
    return bool(_QA_PREFIX_RE.match(_LEADING_MENTIONS_RE.sub("", text or "")))


def contains_qa_marker(text: str) -> bool:
    """True when the literal [QA] token appears anywhere in `text`. For derived or
    concatenated text only -- see the module docstring."""
    return bool(_QA_ANYWHERE_RE.search(text or ""))
