"""v2 S4: every staged-write preview must tell the truth about how to confirm.

Before this, only Shopify's preview said something that actually works in a
channel. Every other kind said "reply to confirm", which is FALSE there: channel
`message` events are not subscribed, so a bare in-thread reply never reaches the
app at all (cq-8063c3cee70f -- a DW confirm typed in a channel thread was
silently ignored, having been told that would work).

_confirm_how is the single source of that sentence and adapts on both axes that
change what is actually true: the surface (a DM DOES deliver a bare reply) and
CORA_CONFIRM_BUTTONS (with buttons off there is no Confirm to tap).
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

from cora.tools import tool_dispatch as td  # noqa: E402


@pytest.fixture
def buttons_on(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")


@pytest.fixture
def buttons_off(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")


class TestConfirmHow:
    def test_channel_with_buttons_offers_tap_and_mention(self, buttons_on):
        out = td._confirm_how("f3e-leadership")
        assert "tap *Confirm* below" in out
        assert '@mention me with "confirm"' in out

    def test_dm_with_buttons_offers_tap_and_plain_reply(self, buttons_on):
        out = td._confirm_how("dm")
        assert "tap *Confirm* below" in out
        assert 'reply "confirm"' in out
        assert "@mention" not in out, "a DM needs no mention"

    def test_channel_without_buttons_never_promises_a_button(self, buttons_off):
        out = td._confirm_how("f3e-leadership")
        assert "Confirm* below" not in out
        assert out == '@mention me with "confirm"'

    def test_dm_without_buttons_says_plain_reply(self, buttons_off):
        assert td._confirm_how("dm") == 'reply "confirm"'

    def test_channel_copy_never_claims_a_bare_reply_works(self, buttons_on, buttons_off):
        """The whole point: in a channel, "reply to confirm" is a lie."""
        for flag in ("on", "off"):
            os.environ["CORA_CONFIRM_BUTTONS"] = flag
            out = td._confirm_how("f3e-leadership").lower()
            assert not re.search(r"\breply\b", out), f"channel copy still says reply ({flag})"

    def test_capitalize_only_touches_the_first_character(self, buttons_on):
        plain = td._confirm_how("dm")
        caps = td._confirm_how("dm", capitalize=True)
        assert caps[0] == plain[0].upper()
        assert caps[1:] == plain[1:]

    def test_empty_channel_is_treated_as_a_channel_not_a_dm(self, buttons_off):
        """Fail toward the STRICTER instruction: telling someone to @mention in
        a DM is merely redundant; telling them to bare-reply in a channel is
        wrong and loses the confirm."""
        assert "@mention" in td._confirm_how("")

    def test_dm_match_is_case_and_space_insensitive(self, buttons_off):
        for variant in ("dm", "DM", " dm "):
            assert td._confirm_how(variant) == 'reply "confirm"'


# ── every preview surface actually uses it ─────────────────────────────────

_SRC = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(encoding="utf-8")


class TestNoPreviewStillSaysReplyToConfirm:
    def test_no_user_facing_preview_hardcodes_reply_to_confirm(self):
        """Drift guard. The only surviving "reply to confirm" phrasings are
        MODEL-facing contract text (_write_blocked_contract / the Shopify twin),
        which instruct the model how to call the tool again -- not user copy --
        plus the relative-due-date warning, which asks the user to confirm a
        DATE reading, not a staged write.

        Scanned via the AST rather than by line, so that comments and docstrings
        (including the one on _confirm_how that EXPLAINS this very phrase) are
        not mistaken for shipped copy."""
        tree = ast.parse(_SRC)
        docstring_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                if (ast.get_docstring(node, clean=False) is not None
                        and node.body and isinstance(node.body[0], ast.Expr)):
                    docstring_nodes.add(id(node.body[0].value))

        allowed_fragments = (
            "the user must reply to confirm and",       # model-facing contract x2
            "Reply to confirm it as-is, or give me",    # due-date sanity warning
        )
        unexpected = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstring_nodes
                    and "reply to confirm" in node.value.lower()
                    and not any(f in node.value for f in allowed_fragments)):
                unexpected.append((node.lineno, node.value.strip()[:80]))
        assert not unexpected, f"user-facing previews still say 'reply to confirm': {unexpected}"

    @pytest.mark.parametrize("marker", [
        "_asana_create_preview_text",      # asana create
        "_delegated_preview_text",         # delegated work
        "_shopify_preview_text",           # shopify single
        "_shopify_bulk_preview_text",      # shopify batch
    ])
    def test_shared_preview_builders_take_a_channel(self, marker):
        """A preview builder that cannot see the channel cannot tell the truth."""
        sig = re.search(rf"def {marker}\((.*?)\) -> str:", _SRC, re.S)
        assert sig, f"{marker} not found"
        assert "channel" in sig.group(1), f"{marker} has no channel parameter"

    def test_confirm_how_is_used_by_every_stash_kind(self):
        """One call per kind at minimum: asana (create/complete/delete/update/
        comment/subtask), calendar (create/delete x2), forget_note, code_queue,
        delegated, shopify (single/lexicon/bulk)."""
        assert _SRC.count("_confirm_how(") >= 16, (
            "a staged-write preview is missing the shared confirm instruction")
