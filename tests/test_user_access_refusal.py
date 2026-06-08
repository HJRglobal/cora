"""Regression tests for the access-gate refusal copy (Fix 2) + Alex's F3E
authorization (Fix 3), from the 2026-06-08 comms review.

Fix 2: check_access refusal must never emit an internal entity code.
Fix 3: the 6/1 #f3-events refusal of Alex was a data gap, now closed -- lock it.
"""

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.user_access import check_access, is_authorized  # noqa: E402

_ENTITY_CODE_TOKENS = ["FNDR", "HJRG", "F3E", "OSN", "LEX", "UFL", "BDM", "HJRP", "HJRPROD", "F3C"]


class TestRefusalCopyNoEntityLeak:
    def test_refusal_emits_no_entity_code(self):
        # An unknown user asking about a non-FNDR/HJRG entity is refused.
        msg = check_access("U_UNKNOWN_USER_XYZ", "F3E", "how are sales going")
        assert msg is not None, "expected a refusal for unknown user on F3E"
        for token in _ENTITY_CODE_TOKENS:
            assert token not in msg, f"refusal copy leaked entity code: {token!r} in {msg!r}"

    def test_refusal_is_redirecting_and_channel_relative(self):
        msg = check_access("U_UNKNOWN_USER_XYZ", "OSN", "what's the revenue")
        assert msg is not None
        assert "channel" in msg.lower()

    def test_known_user_blocked_entity_no_leak(self):
        # Alex is F3E/UFL/HJRG; asking about OSN is refused -- copy must not leak "OSN"/"F3E".
        msg = check_access("U0B3VGWJTMJ", "OSN", "how is the store doing")
        assert msg is not None
        for token in _ENTITY_CODE_TOKENS:
            assert token not in msg


class TestAlexAuthorizationFix3:
    def test_alex_authorized_f3e(self):
        # The 6/1 #f3-events refusal must not recur: Alex IS authorized for F3E.
        assert is_authorized("U0B3VGWJTMJ", "F3E") is True

    def test_alex_authorized_ufl_and_hjrg(self):
        assert is_authorized("U0B3VGWJTMJ", "UFL") is True
        assert is_authorized("U0B3VGWJTMJ", "HJRG") is True

    def test_alex_not_authorized_osn(self):
        assert is_authorized("U0B3VGWJTMJ", "OSN") is False

    def test_alex_f3e_access_passes_check(self):
        # Full check_access path: a non-sensitive F3E question from Alex passes (None).
        assert check_access("U0B3VGWJTMJ", "F3E", "what events are coming up") is None
