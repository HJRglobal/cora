"""CI guard (Phase 3 B1): every Slack sender must route its body through the
egress boundary.

Two bypass classes the boundary (class-level slack_sdk.WebClient patch, installed
from cora/__init__.py) does NOT cover on its own:

  (1) RAW httpx/requests POSTs to slack.com/api/chat.* -- never touch WebClient,
      so the class patch is irrelevant. Must call slack_egress.sanitize_text.

  (2) WebClient chat_* senders in a SCRIPT that imports no cora module -- the
      class patch is installed by cora/__init__.py, which never runs in that
      process, so the WebClient is unpatched there. Must EITHER reference
      sanitize_text OR import cora (which installs the patch in-process).

Each test fails if a NEW sender of that class appears without the required
protection -- keyed on in-file markers, so it survives file renames. The
adversarial review (2026-06-17) found 5 WebClient-class-(2) bypasses the original
raw-only guard could not catch; this is the strengthened version.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCAN_DIRS = (_REPO / "src" / "cora", _REPO / "scripts")
_RAW_MARKER = "slack.com/api/chat"
_SANITIZE = "sanitize_text"
_WEBCLIENT_SEND_RE = re.compile(
    r"\.chat_(?:postMessage|postEphemeral|update|scheduleMessage)\(")
_CORA_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+cora\b", re.MULTILINE)


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


def test_webclient_senders_in_scripts_are_covered():
    # A scripts/ WebClient sender runs as its own process; if it imports no cora
    # module the class patch never installs, so it must sanitize explicitly.
    offenders = []
    for path in (_REPO / "scripts").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _WEBCLIENT_SEND_RE.search(text):
            if _SANITIZE not in text and not _CORA_IMPORT_RE.search(text):
                offenders.append(str(path.relative_to(_REPO)))
    assert not offenders, (
        "scripts/ WebClient chat_* sender(s) that neither import cora (to install "
        "the egress patch in-process) nor call sanitize_text -- they egress "
        "unsanitized: " + ", ".join(sorted(offenders))
    )
