"""Banking-identifier redaction at KB-chunk EGRESS (ingest-integrity bundle I1,
cq-c89cfab00b1f, 2026-09-08).

THE HOLE. On 2026-09-04 ``cora_kb_search("Wire Instructions for HJR Global bank
routing account")`` returned a ``drive_sweep`` chunk of a wire-instructions PDF
with the bank's ABA/routing number, SWIFT code and beneficiary account number in
clear text. The kickoff assumed a "D-034 financial-intent belt" already covered
the Slack path and only the plugin path was open. VERIFY-FIRST (2026-09-08)
found NO such belt anywhere in ``src/cora``: the intent classifier routes every
banking-shaped question as ``complex`` (KB retrieval ON), and a read-only scan
counted ~1,000 chunks carrying routing / SWIFT / account-number shapes across
gmail + drive_sweep in every entity partition. So the redaction sits at the ONE
place every retrieval consumer renders chunk text -- ``context_loader
._format_kb_chunks`` (Slack main retrieval, the cross-entity fallback, and the
MCP ``text`` rendering) -- plus the MCP server's structured ``results[].content``.
One implementation, two call sites; never a second copy (the Code #5 H5 lesson).

WHAT IT REDACTS -- the value, never the label. ``ABA/Routing Number:
[redacted: banking identifier]`` still tells the reader WHICH document to open,
and the chunk's deep link is untouched (the human opens the PDF).

  * ABA / RTN / routing numbers: exactly 9 digits within 24 non-digit chars of a
    ``routing`` / ``ABA`` / ``RTN`` cue.
  * SWIFT / BIC codes: the 8- or 11-char code right after a ``SWIFT`` / ``BIC`` cue.
  * IBANs: the country-code+check-digit shape right after an ``IBAN`` cue.
  * Account numbers: 6+ digits (dashes/spaces allowed) DIRECTLY after an
    ``account`` / ``acct`` / ``a/c`` label (optionally ``number`` / ``no.`` /
    ``#`` / ``id``, optionally ``:`` / ``#`` / ``-``). This leg is the weak-cue,
    loose-shape one, so it fires ONLY when the chunk carries a banking cue
    somewhere (routing / swift / wire / beneficiary / bank / ach / checking /
    savings / iban). "account balance 1234567" is NOT redacted (the gap admits
    labels, not words); "account ending in 4321" is NOT redacted (last-4 is below
    the 6-char floor); a HubSpot "account 246351746" in a chunk with no banking
    cue is NOT redacted.

REGEX DISCIPLINE (three ReDoS incidents in this repo's history): every quantified
class is bounded, no ``.*``, no nested unbounded quantifiers, and every alternation
is anchored on a whole-word cue. ``redact_banking_identifiers`` is measured in the
test file over a 200 KB adversarial chunk.

FAIL-CLOSED: if the redactor itself raises (it should not -- pure regex), the
chunk body is replaced with a withheld marker rather than passed through
unredacted, mirroring ``context_loader._apply_lex_phi_scrub``.

ACCEPTED RESIDUALS (documented, not hidden): an account number written as prose
("the account is 12345678") or a routing number with no cue word in the same
line are not caught; ``historical_access.format_owned_chunks`` (the Tier-2
explicit OWN-mailbox / finance-tier pull) is deliberately NOT wired here -- that
is a finance-workflow decision for Harrison, flagged in the bundle report.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("cora.banking_identifiers")

MARKER = "[redacted: banking identifier]"
WITHHELD = "[content withheld -- banking-identifier redaction error]"

# A chunk is "banking-shaped" when any of these whole words appears. Gates the
# weak-cue ACCOUNT leg only; the routing/SWIFT/IBAN legs carry their own cue.
_BANKING_CUE_RE = re.compile(
    r"\b(?:routing|aba|rtn|swift|bic|iban|wire|wires|beneficiary|bank|ach|checking|savings)\b",
    re.IGNORECASE,
)

# 1. ABA / RTN / routing -- exactly nine digits. The gap between the cue and the
#    digits is a BOUNDED class of non-digits (labels, punctuation, "(ACH)").
_ROUTING_RE = re.compile(
    r"(?i:\b(?:aba|rtn|routing)\b[^\d\n]{0,24})(\d{9})(?!\d)"
)

# 2. SWIFT / BIC -- 8 or 11 uppercase alphanumerics (first six letters). The cue
#    group is case-insensitive; the CODE group is not (a SWIFT code is upper-case
#    by definition, so "swift reply: hello" can never match).
_SWIFT_RE = re.compile(
    r"(?i:\b(?:swift|bic)\b[^A-Za-z0-9\n]{0,12}(?:code[^A-Za-z0-9\n]{0,6})?)"
    r"([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)(?![A-Za-z0-9])"
)

# 3. IBAN -- two letters, two check digits, 11-30 more alphanumerics (single
#    internal spaces allowed only when followed by another alphanumeric, so a
#    trailing space is never swallowed into the redaction).
_IBAN_RE = re.compile(
    r"(?i:\biban\b[^A-Za-z0-9\n]{0,12})"
    r"([A-Z]{2}\d{2}(?:[A-Z0-9]|[ ](?=[A-Z0-9])){11,30})(?![A-Za-z0-9])"
)

# 4. Account number -- STRICT label gap (label words + punctuation only), 6+
#    chars of digits/dashes/spaces starting and ending on a digit.
_ACCOUNT_RE = re.compile(
    r"(?i:\b(?:acct|account|a/c)\b\.?"
    r"(?:\s+(?:number|no\.?|num\.?|id|#))?"
    r"\s*[:#\-]?\s*)"
    r"(\d[\d\- ]{4,22}\d)(?!\d)"
)


def _sub_group1(pattern: re.Pattern, text: str) -> tuple[str, int]:
    """Replace group 1 of every match with MARKER, keeping the cue/label text."""
    count = 0
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        s, e = m.span(1)
        if s < 0 or e <= s:
            continue
        out.append(text[pos:s])
        out.append(MARKER)
        pos = e
        count += 1
    if not count:
        return text, 0
    out.append(text[pos:])
    return "".join(out), count


def redact_banking_identifiers(text: str) -> tuple[str, int]:
    """Return ``(redacted_text, n_redactions)``. Never raises.

    ``n_redactions == 0`` guarantees ``redacted_text`` is the input, byte for byte.
    """
    if not text:
        return text or "", 0
    try:
        total = 0
        out = text
        for pat in (_ROUTING_RE, _SWIFT_RE, _IBAN_RE):
            out, n = _sub_group1(pat, out)
            total += n
        # The weak-cue ACCOUNT leg fires only on a banking-shaped chunk. The gate
        # reads the ORIGINAL text so an earlier redaction cannot un-gate it.
        if _BANKING_CUE_RE.search(text):
            out, n = _sub_group1(_ACCOUNT_RE, out)
            total += n
        return out, total
    except Exception as exc:  # noqa: BLE001 -- fail-CLOSED: withhold, never pass through
        log.warning("banking-identifier redaction failed (%s) -- content withheld", exc)
        return WITHHELD, 1


def has_banking_identifier(text: str) -> bool:
    """True if the redactor would change *text* (read-only probe; used by audits)."""
    _out, n = redact_banking_identifiers(text)
    return n > 0
