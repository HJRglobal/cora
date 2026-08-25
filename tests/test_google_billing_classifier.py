"""C13 (cq-015b3bc779e9): Google billing mail must reach the finance receipt lane.

THE MEASURED DEFECT. `payments-noreply@google.com` is a real, high-volume vendor
sender in the live corpus (195 gmail chunks -- Workspace invoices, Cloud
auto-reload notices, and the Google Ads billing mail this slice is about) and it
matched NO sender pattern: the alternation requires the local part to end at `@`,
so `payments-noreply@` never satisfied `payments?@`, and `google.com` is not a
finance-platform domain.

The consequence, measured on the live KB rather than reasoned about: of 40 gmail
chunks whose title names a Google invoice, only 11 carried the
`financial_document` tag. The tag landed on whichever chunk happened to hold a
`$` amount (+1) on top of the subject term (+2); every OTHER chunk of the same
invoice email scored 2 and stayed invisible to the Tier-2-Finance retrieval lane.
So a #founder-finance pull for a Google invoice returned a fragment. A sender is
a per-DOCUMENT property, so scoring it fixes every chunk of the email at once,
which a per-chunk money amount structurally cannot.

BOTH DIRECTIONS ARE PINNED HERE, because that is the D-217/D-218 lesson from #6:
a filter has to be measured against the exact artifact it exists to catch AND
against the nearest legitimate input it must not catch. Whole-population
measurement on the live corpus at the time of the fix: +68 newly tagged on
`payments-noreply@`, and ZERO newly tagged across 2,880 chunks of non-billing
Google mail (Ads performance nags, Forms receipts, Docs comments, Calendar).
"""

from __future__ import annotations

import pytest

from cora.finance_doc_classifier import (
    _TAG_THRESHOLD,
    financial_document_score,
    is_financial_document,
)

_WORKSPACE_BODY = (
    "Google Workspace Your Google Workspace monthly invoice is available. "
    "Please find the PDF document attached at the bottom of this email."
)


def test_a_google_invoice_whose_pdf_is_named_by_bare_number_is_tagged():
    """THE artifact this fix exists for. Google names the attachment by invoice
    number, so the attachment pattern does not fire; without the sender signal
    this scored 2 against a threshold of 3."""
    score = financial_document_score(
        "Google Workspace: Your invoice is available for hjrglobal.com",
        _WORKSPACE_BODY,
        "Google Payments <payments-noreply@google.com>",
        ("5158741234.pdf",),
    )
    assert score >= _TAG_THRESHOLD, f"scored {score}, below the {_TAG_THRESHOLD} threshold"


def test_a_google_ads_invoice_is_tagged():
    assert is_financial_document(
        "Your Google Ads invoice is available",
        "Your Google Ads invoice for August 2026 is now available. "
        "Account: 352-797-6311.",
        "Google Payments <payments-noreply@google.com>",
        ("7712345678.pdf",),
    )


def test_every_chunk_of_one_invoice_email_is_tagged_not_just_the_money_chunk():
    """The precise defect: the subject rides on every chunk (+2) but only the
    chunk carrying the `$` amount reached 3. The sender is per-document, so all
    chunks now clear the bar together."""
    subject = "Google Workspace: Your invoice is available for hjrglobal.com"
    sender = "payments-noreply@google.com"
    money_chunk = "Total in USD $412.55 will be charged automatically."
    prose_chunk = "Your invoice is available. Please find the PDF attached."
    assert is_financial_document(subject, money_chunk, sender, ())
    assert is_financial_document(subject, prose_chunk, sender, ()), \
        "the non-money chunk of the same invoice must also be retrievable"


@pytest.mark.parametrize("label,subject,body,sender", [
    # 213 live chunks. Highest-risk neighbour: same domain, same "ads" topic,
    # zero financial content.
    ("Ads performance nag", "F3 Energy, you received 77 clicks from your ads last week",
     "Use Ask Advisor to explain your top performance trends.",
     "Google Ads <ads-noreply@google.com>"),
    ("Ads account invite", "Accept your invitation to access a Google Ads account",
     "f3energymarketing@gmail.com has invited you to access the account.",
     "Google Ads <ads-account-noreply@google.com>"),
    # 596 live chunks, and the trap worth naming: the local part CONTAINS the word
    # "receipts" but this is a Google Forms submission notice.
    ("Forms receipt", "Your form response", "Thanks for filling out the form.",
     "Google Forms <forms-receipts-noreply@google.com>"),
    # 1,863 live chunks, and they routinely quote dollar figures from a sheet.
    ("Sheets comment", "Justin commented on Standing ACTUALS",
     "Justin Moran left a comment: can you check the $1,200 line",
     "comments-noreply@docs.google.com"),
    ("Calendar notification", "Invitation: F3 Weekly",
     "You have been invited to F3 Weekly.", "calendar-notification@google.com"),
])
def test_non_billing_google_mail_stays_untagged(label, subject, body, sender):
    assert not is_financial_document(subject, body, sender, ()), \
        f"{label} must not be tagged as a financial document"


def test_the_billing_sender_alone_is_still_not_enough():
    """The two-independent-signals rule is the whole precision design. A billing
    sender with no financial subject term and no money must score 2 and stop."""
    score = financial_document_score(
        "Update to your Google account",
        "We are making changes to your account settings.",
        "payments-noreply@google.com",
        (),
    )
    assert score == 2
    assert score < _TAG_THRESHOLD


def test_the_pre_existing_finance_platform_senders_still_score():
    """Guard against a regression in the alternation while editing it."""
    for sender in ("billing@stripe.com", "invoices@bill.com",
                   "noreply@intuit.com", "ap@melio.com"):
        assert financial_document_score("Invoice 1234", "Amount due $500.00",
                                        sender, ()) >= _TAG_THRESHOLD, sender


def test_a_personal_email_mentioning_a_receipt_is_still_not_a_document():
    """The precision case the module's own docstring promises."""
    assert not is_financial_document(
        "re: that receipt you asked about",
        "I'll dig it up when I get back to my desk.",
        "hannah@hjrglobal.com", (),
    )
