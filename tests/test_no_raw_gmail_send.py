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

# Broad by design (D-051 lens 1): the two-line form
#   msgs = service.users().messages()
#   msgs.send(userId=..., body=...)
# must not evade the guard, so ANY `.send(` carrying a Gmail-shaped argument
# counts, as does any mention of the collection accessors near a send.
_GMAIL_SEND_RE = re.compile(
    r"\.messages\(\)\s*\.send\("
    r"|\.drafts\(\)\s*\.send\("
    r"|users\.(?:messages|drafts)\.send"
    r"|\.send\(\s*userId\s*="
    r"|\.send\(\s*\*\*"
)
# Any mailer/ESP client, static or dynamic. Matched in IMPORT/CLIENT position
# only -- a bare word list also hits ordinary prose ("please resend the note")
# and domain blocklists ("sendgrid.net"), which are not send paths.
_MAILER_LIBS = (
    r"smtplib|aiosmtplib|yagmail|sendgrid|mailgun|postmarker|resend|mailjet|sparkpost"
)
_SMTP_RE = re.compile(
    rf"^\s*(?:import|from)\s+(?:{_MAILER_LIBS})\b"
    rf"|import_module\(\s*[\"'](?:{_MAILER_LIBS})"
    r"|boto3\.client\(\s*[\"']ses",
    re.M,
)
_ESP_HTTP_RE = re.compile(
    r"https?://[^\s\"']*(?:api\.sendgrid\.com|api\.mailgun\.net|api\.resend\.com|"
    r"api\.postmarkapp\.com)[^\s\"']*",
    re.IGNORECASE,
)


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
        if path.resolve() == Path(__file__).resolve():
            continue  # this guard names the libraries it forbids
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SMTP_RE.search(text) or _ESP_HTTP_RE.search(text):
            offenders.append(str(path))
    assert offenders == [], (
        f"SMTP/ESP client or endpoint found (send-path bypass): {offenders}"
    )


def test_sanctioned_sender_still_exists_and_is_gated():
    """If sender.py is renamed/moved, the allowlist above must move with it."""
    sender_path = next(iter(_ALLOWED))
    assert sender_path.exists(), "revops/sender.py moved -- update this guard"
    text = sender_path.read_text(encoding="utf-8")
    assert "_gmail_send_raw" in text
    assert "send_live_mode" in text  # kill switch consulted in this module
    assert "claim_for_send" in text  # single-shot claim in this module
