"""Asana identity resolver -- the ONE seam that picks which PAT Cora uses.

Code #13 RIDER 1 / S-B (bundle code-identity-v1). Before this module every
Asana caller read ``ASANA_PAT`` straight out of the environment per call
(three ``_pat()`` seams in asana_client / asana_connector / lex_client plus
four scripts), so every Cora write was attributed to Harrison and every
Cora read ran with Harrison's full visibility. This module lets Harrison
flip the bot + scripts onto a dedicated ``cora@hjrglobal.com`` seat's token
with ONE env key, and makes the two identities mutually exclusive at the
token layer:

    CORA_ASANA_IDENTITY  unset / "harrison"  -> ASANA_PAT       (today's behaviour)
    CORA_ASANA_IDENTITY  "cora"              -> ASANA_PAT_CORA  (least privilege)
    anything else                            -> AsanaIdentityError

HARD RULES (locked in the kickoff, pinned by tests/test_asana_identity.py):

  * ``cora`` with ``ASANA_PAT_CORA`` empty/missing is a HARD RAISE -- never a
    silent fallback to Harrison's token. A least-privilege flip that quietly
    degraded to the privileged token would be the exact failure the flip
    exists to prevent.
  * Error messages name the KEY NAME only. No token value, no prefix of one,
    and not even the raw flag value (a mis-pasted secret in the flag would
    otherwise echo into a log line).
  * With the flag unset the returned token is byte-identical to
    ``os.environ["ASANA_PAT"]`` -- the default path IS today's behaviour.

Bot-loaded (imported by asana_client / asana_connector / lex_client, which the
bot process imports), so flipping the identity for the BOT needs one restart
(load_dotenv populates os.environ once at process start). Scripts are a fresh
process per fire and pick the flag up at their next run.
"""
from __future__ import annotations

import os

IDENTITY_ENV = "CORA_ASANA_IDENTITY"
HARRISON_PAT_ENV = "ASANA_PAT"
CORA_PAT_ENV = "ASANA_PAT_CORA"

IDENTITY_HARRISON = "harrison"
IDENTITY_CORA = "cora"

_PAT_KEY_BY_IDENTITY: dict[str, str] = {
    IDENTITY_HARRISON: HARRISON_PAT_ENV,
    IDENTITY_CORA: CORA_PAT_ENV,
}
VALID_IDENTITIES: tuple[str, ...] = tuple(_PAT_KEY_BY_IDENTITY)

#: The Asana seat the ``cora`` identity's token MUST belong to. A users/me probe
#: under ``ASANA_PAT_CORA`` that answers with any other email means a privileged
#: token was pasted into the least-privilege key (D-051 finding B-asana-identity-3:
#: every surface would say "cora" while every write stayed attributed to Harrison).
CORA_SEAT_EMAIL = "cora@hjrglobal.com"

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_CRITICAL = "critical"


def classify_users_me(identity: str, email: str | None) -> tuple[str, str]:
    """Classify a users/me answer against the identity the token was resolved for.

    Returns ``(status, reason)`` with status one of ``ok`` / ``warn`` / ``critical``:

      * no email in the answer          -> ``warn``  "identity unverifiable" (never ok)
      * identity cora, email != cora@   -> ``critical`` (the privileged token is still
                                           in use under the least-privilege flag)
      * identity harrison, email == cora@ -> ``critical`` (the keys are swapped)
      * otherwise                       -> ``ok``

    The email is compared case-insensitively and whitespace-trimmed. The reason
    carries key NAMES and the seat email only -- never a token value.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return STATUS_WARN, (
            "identity unverifiable -- users/me returned no email, so the active token "
            f"could not be matched against {IDENTITY_ENV}={identity}"
        )
    is_cora_seat = normalized == CORA_SEAT_EMAIL
    if identity == IDENTITY_CORA and not is_cora_seat:
        return STATUS_CRITICAL, (
            f"IDENTITY MISMATCH -- {IDENTITY_ENV}={identity} but the token in {CORA_PAT_ENV} "
            f"answers users/me as {normalized}, not {CORA_SEAT_EMAIL}; a privileged token is "
            "still in use under the least-privilege flag (re-paste the cora@ seat's PAT)"
        )
    if identity == IDENTITY_HARRISON and is_cora_seat:
        return STATUS_CRITICAL, (
            f"IDENTITY MISMATCH -- {IDENTITY_ENV}={identity} but the token in {HARRISON_PAT_ENV} "
            f"answers users/me as {CORA_SEAT_EMAIL}; the two PAT keys appear swapped"
        )
    return STATUS_OK, f"users/me = {normalized}"


class AsanaIdentityError(Exception):
    """Raised when the identity flag is unrecognised or the ACTIVE identity's
    PAT key is missing. Message text carries key NAMES only, never a value."""


def active_identity() -> str:
    """Return ``"harrison"`` (default) or ``"cora"``; raise on any other value.

    Reads the flag per call (like the seams it replaces) so a script picks the
    flip up at its next fire without a code change.
    """
    raw = (os.environ.get(IDENTITY_ENV) or "").strip().lower()
    if not raw:
        return IDENTITY_HARRISON
    if raw not in _PAT_KEY_BY_IDENTITY:
        # Deliberately do NOT echo the raw value: a secret pasted into the
        # wrong .env line must not surface through this error.
        raise AsanaIdentityError(
            f"{IDENTITY_ENV} has an unrecognised value -- expected one of "
            f"{'|'.join(VALID_IDENTITIES)}; Asana disabled until it is fixed"
        )
    return raw


def pat_key_for(identity: str) -> str:
    """The env KEY NAME that holds *identity*'s token (never the value)."""
    try:
        return _PAT_KEY_BY_IDENTITY[identity]
    except KeyError as exc:
        raise AsanaIdentityError(
            f"unknown Asana identity -- expected one of {'|'.join(VALID_IDENTITIES)}"
        ) from exc


def active_pat_key() -> str:
    """The env KEY NAME the ACTIVE identity reads (health-check consumer)."""
    return pat_key_for(active_identity())


def resolve_pat() -> tuple[str, str]:
    """Return ``(token, identity)`` for the active identity.

    Raises AsanaIdentityError when the flag is invalid or the active key is
    empty/missing. There is NO fallback between identities by construction:
    the key looked up is a pure function of the identity.
    """
    identity = active_identity()
    key = _PAT_KEY_BY_IDENTITY[identity]
    val = os.environ.get(key, "")
    if not val.strip():
        raise AsanaIdentityError(
            f"{key} not set in environment ({IDENTITY_ENV}={identity}) -- Asana "
            f"tool-use disabled; no fallback to another identity's token"
        )
    return val, identity


def resolve_pat_for(identity: str) -> str:
    """Return the token for an EXPLICIT identity (the visibility diff runs both
    views side by side). Same hard-raise contract as resolve_pat()."""
    key = pat_key_for(identity)
    val = os.environ.get(key, "")
    if not val.strip():
        raise AsanaIdentityError(
            f"{key} not set in environment -- cannot build the '{identity}' view"
        )
    return val
