"""Deterministic silence-nudge template renderer (R3).

No LLM anywhere in the nudge path: the body a card shows (and the gate sends)
is a template file with string substitution, nothing else. Templates live in
design/playbooks/revops/ and merge through Harrison like any canon-adjacent
config.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cora.revops.nudge_templates")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE_DIR = _REPO_ROOT / "design" / "playbooks" / "revops"

_WORKSTREAM_SLUGS = {
    "Retail": "retail",
    "Press": "press",
    "Suppliers": "suppliers",
}

_FIRST_NAME_RE = re.compile(r"[A-Za-z][A-Za-z.'-]*")


def first_name_from_counterparty(counterparty_name: Optional[str]) -> str:
    """'Josh A. (Wham Foods)' -> 'Josh'; fall back to 'there'."""
    if not counterparty_name:
        return "there"
    m = _FIRST_NAME_RE.search(counterparty_name)
    if not m:
        return "there"
    token = m.group(0).rstrip(".")
    # Single-letter initials ('T. Mannan' -> prefer the surname? No: too clever.
    # A one-letter first token reads wrong in a greeting; use 'there'.)
    return token if len(token) > 1 else "there"


def template_path_for(workstream: Optional[str]) -> Path:
    slug = _WORKSTREAM_SLUGS.get((workstream or "").strip(), "default")
    candidate = _TEMPLATE_DIR / f"silence-nudge-{slug}.md"
    if candidate.exists():
        return candidate
    return _TEMPLATE_DIR / "silence-nudge-default.md"


def render_nudge(
    *,
    workstream: Optional[str],
    counterparty_name: Optional[str],
    days_silent: int,
) -> Optional[str]:
    """Render the nudge body, or None when the template is unreadable
    (callers treat None as 'do not stage anything' -- fail-closed)."""
    try:
        raw = template_path_for(workstream).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - fail closed: no template, no nudge
        logger.exception("nudge template unreadable for workstream %r", workstream)
        return None
    body = raw.replace("{first_name}", first_name_from_counterparty(counterparty_name))
    body = body.replace("{days_silent}", str(int(days_silent)))
    return body.strip() + "\n"
