"""API-token-shape redaction at KB-chunk EGRESS and in the session-capture harvest
(Code #15 S1, cq-d9d0c92cc797, 2026-09-24).

THE HOLE. On 2026-09-11 an Asana personal access token was pasted into a Cowork
session (a PowerShell ``Read-Host`` prompt). The nightly session-capture harvest
flattens that transcript and sends it to Haiku; the paste sat past the 24,000-char
distill cap, so by LUCK it never reached the prompt (a strict-PHI transcript gets a
60,000-char cap, and the prompt tells Haiku to prefer specifics "such as IDs"). A
read-only scan of the KB (2026-09-24) found ONE live-shaped token chunk at rest
(a drive_sweep row carrying a Slack bot token and an Anthropic key) that every
chunk renderer would have served verbatim. The banking belt
(``banking_identifiers``) has the right seams but knows nothing about token shapes.

WHAT IT REDACTS -- exactly four shapes, case-sensitive (token prefixes are), every
quantifier bounded (seven ReDoS incidents in this repo's history):

  * ``asana-pat``      -- v1 ``1/<gid>:<32 alnum>`` and v2 ``2/<gid>/<gid>:<32
    alnum>`` (the 9/11 paste is v2, segment lengths 1/16/16/32). Left edge is
    "not a digit" ONLY, so a JSON-escaped newline or a word glued in front
    ("...PAT2/...") still matches. The repo hook's prefix screen and the pre-S1
    ``secrets_scan`` shape are NOT reused: the first false-positives on 69 live
    gmail chunks (Asana task-URL paths), the second missed v2 entirely.
  * ``slack-token``    -- ``xox`` + one of a/b/p/r/s, a dash, a 1-15 digit segment,
    then a dash-joined tail that must carry a 16-alnum run (kills
    "two-factor"-style prose and the ``your-token-here`` placeholders).
  * ``sk-key``         -- the ``sk-`` family: the prefixed kinds (proj / svcacct /
    admin / None / Anthropic ``ant-<kind><nn>``) allow ``_`` and ``-`` in a
    20-300 char tail that carries a digit; a BARE ``sk-`` needs 32-300 alnum with
    no hyphen, a digit AND a letter (kills kebab-case words and ``task-`` /
    ``desk-`` joins; the left boundary rejects a preceding word char or hyphen).
  * ``google-api-key`` -- ``AIza`` + exactly 35 of ``[0-9A-Za-z_-]``.

OUT OF SCOPE (recorded residuals, seeded): Slack app-level / rotating-refresh
token prefixes, Google OAuth access/refresh tokens, the legacy Asana ``0/<32 hex>``
form (collides with URL path hashes), and live ``.env`` values that carry no token
shape (a vendor password, an API key without a prefix). A prefixed token glued to
a preceding LETTER (a literal ``\\n`` escape in JSON text, "tokenxox...") is not
matched by the three prefix shapes -- only the Asana leg tolerates that.

API (the ``banking_identifiers`` contract, so every seam reads the same):
  * ``redact_secret_tokens(text) -> (text, n)`` never raises; fails CLOSED to
    ``WITHHELD``; ``n == 0`` returns the input byte for byte; idempotent (the
    ``MARKER`` carries no shape).
  * ``redact_secret_tokens_strict(text)`` -- same, but RAISES instead of
    withholding: for AT-REST rewrites (the KB purge), where a withheld marker must
    never be persisted over a chunk.
  * ``count_by_shape(text) -> {shape: n}`` -- counts only, never text (manifests).
  * ``redact_chunk_egress(text) -> (text, n_banking, n_tokens)`` -- the composed
    helper every chunk renderer calls: tokens FIRST, then banking. Order matters:
    a ``routing`` cue within 40 chars of a legacy 9-digit Slack segment lets the
    banking routing leg put ITS marker inside the token; the token regex then no
    longer matches and the secret tail would survive.
"""
from __future__ import annotations

import logging
import re

from . import banking_identifiers

log = logging.getLogger("cora.secret_tokens")

MARKER = "[redacted: api token]"
WITHHELD = "[content withheld -- api-token redaction error]"

#: (shape, compiled regex). Order is the redaction order; the shapes are disjoint
#: (no shape's alphabet admits another's prefix at a valid left boundary).
SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("asana-pat", re.compile(
        r"(?<![0-9])[12]/[0-9]{10,20}(?:/[0-9]{10,20})?:[0-9A-Za-z]{32}(?![0-9A-Za-z])")),
    ("slack-token", re.compile(
        r"(?<![0-9A-Za-z])xox[abprs]-[0-9]{1,15}-"
        r"(?=[0-9A-Za-z-]{0,200}[0-9A-Za-z]{16})[0-9A-Za-z-]{10,250}(?![0-9A-Za-z-])")),
    ("sk-key", re.compile(
        r"(?<![0-9A-Za-z_-])sk-(?:"
        r"(?:proj|svcacct|admin|None|ant-[a-z]{2,8}[0-9]{2})-(?=[0-9A-Za-z_-]{0,300}[0-9])[0-9A-Za-z_-]{20,300}"
        r"|(?=[0-9A-Za-z]{0,300}[0-9])(?=[0-9A-Za-z]{0,300}[A-Za-z])[0-9A-Za-z]{32,300}"
        r")(?![0-9A-Za-z_-])")),
    ("google-api-key", re.compile(
        r"(?<![0-9A-Za-z_-])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
)

SHAPE_NAMES: tuple[str, ...] = tuple(name for name, _rx in SHAPES)


def _redact_counts(text: str) -> tuple[str, dict[str, int]]:
    """The one redaction loop (raises on an engine error -- callers decide)."""
    counts: dict[str, int] = {}
    work = text
    for kind, rx in SHAPES:
        work, n = rx.subn(MARKER, work)
        if n:
            counts[kind] = n
    return work, counts


def redact_secret_tokens_strict(text: str | None) -> tuple[str, int]:
    """``(redacted, n)``; RAISES on an internal error (never returns WITHHELD).

    For at-rest rewrites only. ``n == 0`` returns the input byte for byte."""
    if not text:
        return text or "", 0
    out, counts = _redact_counts(text)
    total = sum(counts.values())
    if not total:
        return text, 0
    return out, total


def redact_secret_tokens(text: str | None) -> tuple[str, int]:
    """Return ``(redacted_text, n_redactions)``. Never raises.

    ``n_redactions == 0`` guarantees ``redacted_text`` is the input, byte for byte.
    An internal error returns ``(WITHHELD, 1)`` -- fail CLOSED, never pass through.
    """
    try:
        return redact_secret_tokens_strict(text)
    except Exception as exc:  # noqa: BLE001 -- fail-CLOSED: withhold, never pass through
        log.warning("api-token redaction failed (%s) -- content withheld", type(exc).__name__)
        return WITHHELD, 1


def redact_title(title: str | None) -> str:
    """The title-shaped convenience: redacted text only."""
    out, _n = redact_secret_tokens(title or "")
    return out


def has_secret_token(text: str | None) -> bool:
    """True if the redactor would change *text* (read-only probe)."""
    _out, n = redact_secret_tokens(text or "")
    return n > 0


def count_by_shape(text: str | None) -> dict[str, int]:
    """``{shape: occurrences}`` for the shapes present (empty when clean). Counts
    only -- the matched text is never returned. Uses the SAME sequential loop as
    the redactor, so ``sum(count_by_shape(t).values())`` is the ``n`` that
    ``redact_secret_tokens(t)`` reports. An engine error counts as
    ``{"unscannable": 1}`` (fail closed: the caller sees a non-clean text)."""
    if not text:
        return {}
    try:
        _out, counts = _redact_counts(text)
        return counts
    except Exception as exc:  # noqa: BLE001
        log.warning("api-token count failed (%s)", type(exc).__name__)
        return {"unscannable": 1}


def redact_chunk_egress(text: str | None) -> tuple[str, int, int]:
    """The composed chunk-egress belt: ``(text, n_banking, n_tokens)``.

    Tokens FIRST, then ``banking_identifiers.redact_banking_identifiers`` (that
    module is unchanged). ``n_banking + n_tokens == 0`` returns the input byte for
    byte. Never raises; either leg's failure withholds the whole text."""
    if not text:
        return text or "", 0, 0
    out, n_tok = redact_secret_tokens(text)
    if n_tok and out == WITHHELD:
        return WITHHELD, 0, n_tok
    out, n_bank = banking_identifiers.redact_banking_identifiers(out)
    if not (n_tok or n_bank):
        return text, 0, 0
    return out, n_bank, n_tok
