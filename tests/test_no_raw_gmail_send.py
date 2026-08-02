"""CI guard (D-051 lens 1: send-path bypass hunting).

The ONLY module allowed to call the Gmail send API is src/cora/revops/sender.py
(the ladder chokepoint). Any other call site -- messages().send, drafts().send,
or an SMTP client -- is a send-path bypass and fails this test.

Pattern follows tests/test_no_raw_slack_post.py: scan src/cora + scripts on
in-file markers so the guard survives renames.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = (_REPO_ROOT / "src" / "cora", _REPO_ROOT / "scripts")

# The single sanctioned send call site.
_ALLOWED = {(_REPO_ROOT / "src" / "cora" / "revops" / "sender.py").resolve()}

_GMAIL_SEND_RE = re.compile(
    r"\.messages\(\)\s*\.send\(|\.drafts\(\)\s*\.send\(|users\.messages\.send|users\.drafts\.send"
)
_SMTP_RE = re.compile(r"^\s*(?:import|from)\s+(?:smtplib|sendgrid|mailgun|postmarker)\b", re.M)


def _py_files():
    for base in _SCAN_DIRS:
        if base.exists():
            yield from base.rglob("*.py")


def test_gmail_send_only_from_the_sanctioned_chokepoint():
    offenders = []
    for path in _py_files():
        if path.resolve() in _ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _GMAIL_SEND_RE.search(text):
            offenders.append(str(path))
    assert offenders == [], (
        "Gmail send API called outside src/cora/revops/sender.py (send-path "
        f"bypass): {offenders}. Every send must go through the send-trust gate."
    )


def test_no_smtp_or_esp_clients_anywhere():
    offenders = []
    for path in _py_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SMTP_RE.search(text):
            offenders.append(str(path))
    assert offenders == [], (
        f"SMTP/ESP client import found (send-path bypass): {offenders}"
    )


def test_sanctioned_sender_still_exists_and_is_gated():
    """If sender.py is renamed/moved, the allowlist above must move with it."""
    sender_path = next(iter(_ALLOWED))
    assert sender_path.exists(), "revops/sender.py moved -- update this guard"
    text = sender_path.read_text(encoding="utf-8")
    assert "_gmail_send_raw" in text
    assert "send_live_mode" in text  # kill switch consulted in this module
    assert "claim_for_send" in text  # single-shot claim in this module
