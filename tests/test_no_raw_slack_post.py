"""CI guard (Phase 3 B1): every module that POSTs raw JSON to Slack's
chat.postMessage MUST route the message body through slack_egress.sanitize_text.

Those raw httpx/requests POSTs bypass the class-level slack_sdk.WebClient egress
patch (installed from cora/__init__.py), so without an explicit sanitize they
egress mojibake / bare URLs / naked IDs / named sources unredacted. This test
fails if a NEW raw sender is added (or an existing one's sanitize is removed) --
keyed on the in-file presence of `sanitize_text`, so it survives file renames.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCAN_DIRS = (_REPO / "src" / "cora", _REPO / "scripts")
_RAW_MARKER = "slack.com/api/chat"
_SANITIZE = "sanitize_text"


def test_every_raw_slack_post_sanitizes():
    offenders = []
    for base in _SCAN_DIRS:
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if _RAW_MARKER in text and _SANITIZE not in text:
                offenders.append(str(path.relative_to(_REPO)))
    assert not offenders, (
        "Raw Slack chat.postMessage sender(s) missing slack_egress.sanitize_text -- "
        "they bypass the egress boundary; wrap the text body with sanitize_text(): "
        + ", ".join(sorted(offenders))
    )
