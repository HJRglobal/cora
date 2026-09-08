"""Banking-identifier redaction at KB-chunk EGRESS (ingest-integrity bundle I1,
cq-c89cfab00b1f, 2026-09-08; D-051 lens-C remediation the same day).

THE HOLE. On 2026-09-04 ``cora_kb_search("Wire Instructions for HJR Global bank
routing account")`` returned a ``drive_sweep`` chunk of a wire-instructions PDF
with the bank's ABA/routing number, SWIFT code and beneficiary account number in
clear text. The kickoff assumed a "D-034 financial-intent belt" already covered
the Slack path and only the plugin path was open. VERIFY-FIRST (2026-09-08)
found NO such belt anywhere in ``src/cora``: the intent classifier routes every
banking-shaped question as ``complex`` (KB retrieval ON), and a read-only scan
counted ~6,700 chunks carrying routing / SWIFT / IBAN / account-number shapes
across gmail + drive_sweep in every entity partition. So the redaction sits at
every place chunk text is rendered for a model or a person -- the shared
renderer ``context_loader._format_kb_chunks`` (Slack main retrieval, the
cross-entity fallback, the MCP ``text``), the MCP structured ``results[]``, the
Tier-2 renderers (``historical_access.format_owned_chunks``,
``finance_receipts.format_finance_chunks``), the daily briefing's chunk
snippets and the nightly Drive digest. One implementation, several call sites;
never a second copy (the Code #5 H5 lesson).

WHAT IT REDACTS -- the value, never the label. ``ABA/Routing Number:
[redacted: banking identifier]`` still tells the reader WHICH document to open,
and the chunk's deep link is untouched (the human opens the PDF).

  * ABA / RTN / routing numbers: exactly 9 digits within 40 characters of a
    ``routing`` / ``ABA`` / ``RTN`` cue -- across a line break, a ``(9 digits)``
    aside or a table cell (the D-051 measurement found 79 live chunks whose
    routing value sat on the NEXT line after the label; PDF extraction joins
    pages with newlines).
  * SWIFT / BIC codes: the 8- or 11-char code after a ``SWIFT`` / ``BIC`` cue,
    when a label word / separator sits between them OR the code carries a digit
    -- so ``SWIFT TRANSFER FEES`` (all-caps prose) is not a code. A bounded
    parenthetical aside between label and code is crossed -- ``SWIFT Code (For
    International Wires): X`` is the EXACT shape of the 9/4 chunk (the D-051
    lens-F review found the first cut redacted 2 of its 3 identifiers).
  * IBANs: the country-code+check-digit shape after an ``IBAN`` cue.
  * Every label/value gap admits a line break (PDF tables extract the value on
    the line AFTER its label).
  * TITLES too: the renderers pass the chunk title (an email subject, a file
    name) through the same function -- "Wire info - routing 021000021" as a
    subject line would otherwise ride into the header, the link label and the
    WARN line unredacted.
  * Account numbers: 6+ digits (dashes/spaces allowed) after an ``account`` /
    ``acct`` / ``a/c`` label, optionally ``number`` / ``no.`` / ``nbr`` / ``id`` /
    ``#``, then ``:`` / ``#`` / ``-`` / ``=`` / ``is`` -- and ONLY when the chunk
    carries a banking cue somewhere (routing / swift / wire / beneficiary / bank
    / ach / checking / savings / iban). "account balance 1234567" is NOT redacted
    (a balance is not a label), "account ending in 4321" is NOT (last-4 is below
    the 6-char floor), a HubSpot "account 246351746" in a chunk with no banking
    cue is NOT, and a date / year range / US phone after the label is NOT.
  * Continuations: a second value that follows a redacted one on the same line
    -- ``Routing: 123456789 (wire) 123456780 (ACH)``, ``Routing/Account:
    123456789 / 9876543210`` -- is redacted too.

REGEX DISCIPLINE (seven ReDoS incidents in this repo's history): every quantified
class is bounded, no unbounded ``.*``, no nested unbounded quantifiers, every
alternation anchored on a whole-word cue. Measured in the test file over a
200 KB adversarial chunk (tens of ms).

FAIL-CLOSED: if the redactor itself raises (pure regex; it should not), the
chunk body is replaced with a withheld marker rather than passed through.

ACCEPTED RESIDUALS (documented, measured): a value with NO label anywhere in
reach ("wire to 021000021 / 12345678" with neither word) is not caught; a
LEX "ABA (Applied Behavior Analysis) ... 9-digit" collision redacts a
non-banking number (2 live chunks, safe direction); vendor / utility account
numbers in banking-shaped chunks are redacted too (~700 live, safe direction --
the deep link carries the human to the document).
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

# 1. ABA / RTN / routing -- exactly nine digits within a BOUNDED window after the
#    cue. The window admits newlines, parentheses and digits ("(9 digits)") so a
#    label on one line and its value on the next is one match; the value must be
#    a standalone 9-digit run.
_ROUTING_RE = re.compile(
    r"(?i:\b(?:aba|rtn|routing)\b[\s\S]{0,40}?)(?<!\d)(\d{9})(?!\d)"
)

# 2. SWIFT / BIC -- 8 or 11 upper-case alphanumerics (first six letters). A code
#    counts when a label word / separator sits between cue and code, OR the code
#    itself carries a digit; a bare all-caps English word after the cue ("SWIFT
#    TRANSFER FEES APPLY") is prose, not a code.
_SWIFT_RE = re.compile(
    r"(?i:\b(?:swift|bic)\b(?:[\s/]*(?:code|id|number|no\.?|bic))?"
    r"(?:\s*\([^()\n]{0,40}\))?[^A-Za-z0-9]{0,12})"
    r"(?:(?<=[:#=\-]\s)|(?<=[:#=\-])|(?<=\bcode\s)|(?<=\bid\s)|(?<=\bnumber\s)|(?<=\bno\.\s)|(?<=\bno\s)|(?=[A-Z]{6}[A-Z0-9]*\d))"
    r"([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)(?![A-Za-z0-9])"
)

# 3. IBAN -- two letters, two check digits, 11-30 more alphanumerics (single
#    internal spaces allowed only when followed by another alphanumeric).
_IBAN_RE = re.compile(
    r"(?i:\biban\b(?:\s*(?:no\.?|number|code))?[^A-Za-z0-9]{0,12})"
    r"([A-Z]{2}\d{2}(?:[A-Z0-9]|[ ](?=[A-Z0-9])){11,30})(?![A-Za-z0-9])"
)

# 4. Account number -- label + optional id-word + optional connector + 6+ chars
#    of digits/dashes/spaces starting and ending on a digit. The connector admits
#    "is" ("account number is 12345678" -- 87 live chunks) and up to four
#    punctuation chars ("Acct#:" -- 16 live), never an arbitrary word.
_ACCOUNT_RE = re.compile(
    r"(?i:\b(?:acct|account|a/c)\b\.?"
    r"(?:\s+(?:number|no\.?|num\.?|nbr\.?|id|#))?"
    r"(?:\s*(?:is|=)\s*|[\s:#\-=|]{0,4}))"
    # not a DATE ("account 2024-01-15"), a YEAR RANGE ("account 2019-2025") or a
    # US PHONE ("account 480-555-1234") -- those are the value shapes that share
    # the digit/dash alphabet with an account number (D-051 corpus, 2026-09-08)
    r"(?!\d{4}-\d{2}-\d{2}(?![\d-])|\d{4}-\d{4}(?![\d-])|\d{3}-\d{3}-\d{4}(?![\d-]))"
    r"(?<!\d)(\d[\d\- ]{4,22}\d)(?!\d)"
)

# 5. Continuations -- a further value on the SAME line right after a redacted
#    one: "(wire) 123456780 (ACH)", "/ 9876543210", ", 021000021".
_MARKER_ESC = re.escape(MARKER)
_CONTINUATION_RE = re.compile(
    _MARKER_ESC
    + r"(?:[ \t]*\([^()\n]{0,20}\))?[ \t]*(?:[/,;|]|\band\b|\bor\b)?[ \t]*(?:\([^()\n]{0,20}\)[ \t]*)?"
    + r"(?<!\d)(\d[\d\- ]{4,22}\d)(?!\d)",
    re.IGNORECASE,
)


def _sub_group1(pattern: re.Pattern, text: str) -> tuple[str, int]:
    """Replace group 1 of every match with MARKER, keeping the cue/label text."""
    count = 0
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        s, e = m.span(1)
        if s < 0 or e <= s or s < pos:
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
        # Continuations: bounded fixpoint (each pass redacts at least one value
        # or stops; a line holds only so many values).
        if total:
            for _ in range(8):
                out, n = _sub_group1(_CONTINUATION_RE, out)
                if not n:
                    break
                total += n
        return out, total
    except Exception as exc:  # noqa: BLE001 -- fail-CLOSED: withhold, never pass through
        log.warning("banking-identifier redaction failed (%s) -- content withheld", exc)
        return WITHHELD, 1


def redact_title(title: str | None) -> str:
    """The title-shaped convenience: redacted text only (n is implied by MARKER)."""
    out, _n = redact_banking_identifiers(title or "")
    return out


def has_banking_identifier(text: str) -> bool:
    """True if the redactor would change *text* (read-only probe; used by audits)."""
    _out, n = redact_banking_identifiers(text)
    return n > 0
