"""Deterministic, precision-biased classifier for financial documents.

Tags gmail / drive_sweep KB content as `financial_document` (receipt, invoice,
order confirmation, payment, statement) for the Tier 2-Finance retrieval path
(see historical_access.py + finance_receipts.py). Runs at the
store.upsert_documents choke point so EVERY connector — including the 18-month
gmail backfill — tags at ingest; scripts/backfill_financial_document_tags.py
is the one-time catch-up for chunks ingested before this shipped.

Design rule from the spec: ERR TOWARD PRECISION. Only high-confidence
financial documents get the tag, so a misclassified personal email can never
surface into #founder-finance. Scoring requires two independent signals (or one
very strong combination):

  +2  financial-document term in the SUBJECT/TITLE (invoice, receipt, ...)
  +2  financial sender (billing@/invoices@/... local part, or a known
      finance-platform domain: intuit, bill.com, stripe, ...)
  +2  attachment filename that looks like an invoice/receipt/statement
  +1  currency amount in the content ($1,234.56)
  +1  strong billing phrase in the body (amount due, payment received, ...)

Tag when score >= 3. A lone subject keyword ("re: that receipt you asked
about") or a lone dollar amount in a personal email never qualifies.

Pure module: re + stdlib only (store.py imports it — keep it dependency-free).
"""

from __future__ import annotations

import re

# Subject/title terms that name a financial document type.
_SUBJECT_RE = re.compile(
    r"\b(?:invoice|receipt|order\s+confirmation|purchase\s+order|"
    r"billing\s+statement|account\s+statement|statement\s+of\s+account|"
    r"payment\s+(?:received|receipt|confirmation|due|reminder)|"
    r"amount\s+due|past\s+due|remittance|sales\s+order|estimate\s+#|"
    r"quote\s+#|po\s*#\s*\d|your\s+order|order\s+#)\b",
    re.IGNORECASE,
)

# Sender local-parts + platform domains that exist to send financial documents.
_SENDER_RE = re.compile(
    r"(?:\b(?:billing|invoice|invoices|receipts?|payments?|accounting|"
    r"accountspayable|accountsreceivable|ar|ap|noreply\+billing|statements?)@|"
    r"@(?:[\w.-]*\b(?:intuit|quickbooks|bill|stripe|paypal|squareup|square|"
    r"gusto|adp|freshbooks|xero|waveapps|melio|ramp|brex|expensify|"
    r"billtrust|aviationinvoice)\b)[\w.-]*\.(?:com|net|io))",
    re.IGNORECASE,
)

# Attachment filenames that look like financial docs. Matched against the
# gmail header "Attachments: ..." line content or raw filename strings.
_ATTACHMENT_RE = re.compile(
    r"\b[\w \-#]*(?:invoice|receipt|statement|remittance|purchase[ _-]?order|"
    r"\bpo[#_-]?\d+|estimate|bill)[\w \-#]*\.(?:pdf|csv|xlsx?|docx?)\b",
    re.IGNORECASE,
)

# Currency amount with cents, or a clearly-money integer amount ($1,500).
_CURRENCY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?\b")

# Strong billing phrases in the body.
_BODY_RE = re.compile(
    r"\b(?:amount\s+due|total\s+due|balance\s+due|payment\s+(?:received|"
    r"processed|confirmation)|paid\s+in\s+full|due\s+(?:date|upon\s+receipt)|"
    r"remit\s+to|invoice\s+number|invoice\s*#|net\s*(?:15|30|45|60)\b|"
    r"thank\s+you\s+for\s+your\s+(?:order|payment|purchase))\b",
    re.IGNORECASE,
)

_TAG_THRESHOLD = 3


def financial_document_score(
    title: str,
    content: str = "",
    author: str = "",
    attachment_names: tuple[str, ...] = (),
) -> int:
    """Return the additive signal score (see module docstring)."""
    title = title or ""
    content = content or ""
    author = author or ""
    score = 0

    if _SUBJECT_RE.search(title):
        score += 2
    if author and _SENDER_RE.search(author):
        score += 2

    attachment_text = " ".join(attachment_names)
    if not attachment_text:
        # gmail chunks carry an "Attachments: a.pdf, b.pdf" header line.
        m = re.search(r"^Attachments:\s*(.+)$", content, re.IGNORECASE | re.MULTILINE)
        if m:
            attachment_text = m.group(1)
        elif _SUBJECT_RE.search(title) is None and title:
            # drive_sweep: the title IS the filename.
            attachment_text = title
    if attachment_text and _ATTACHMENT_RE.search(attachment_text):
        score += 2

    if _CURRENCY_RE.search(content):
        score += 1
    if _BODY_RE.search(content):
        score += 1

    return score


def is_financial_document(
    title: str,
    content: str = "",
    author: str = "",
    attachment_names: tuple[str, ...] = (),
) -> bool:
    """Precision-biased: True only when score >= 3 (two independent signals)."""
    return financial_document_score(title, content, author, attachment_names) >= _TAG_THRESHOLD
