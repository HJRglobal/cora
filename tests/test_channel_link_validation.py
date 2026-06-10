"""Tests for app._validate_channel_links — hallucinated <#Cxxx|name> token cleanup.

The LLM occasionally fabricates Slack channel IDs in redirect copy (observed
2026-06-10). Valid IDs are preserved exactly; IDs that fail conversations_info
with channel_not_found degrade to plain '#name' text; transient API errors keep
the token (fail-open) and are not cached.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import cora.app as app_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    app_mod._channel_link_cache.clear()
    yield
    app_mod._channel_link_cache.clear()


def _client(valid_ids=(), error_ids=()):
    """Mock WebClient: valid ids resolve, error_ids raise a transient error,
    everything else raises channel_not_found."""
    client = MagicMock()

    def _info(channel):
        if channel in valid_ids:
            return {"ok": True, "channel": {"id": channel}}
        if channel in error_ids:
            raise RuntimeError("ratelimited")
        raise RuntimeError("channel_not_found")

    client.conversations_info.side_effect = _info
    return client


def test_no_channel_tokens_short_circuits():
    client = _client()
    text = "Plain reply with no links."
    assert app_mod._validate_channel_links(text, client) == text
    client.conversations_info.assert_not_called()


def test_valid_channel_link_preserved():
    client = _client(valid_ids={"C0B3A3U7WS3"})
    text = "Ask in <#C0B3A3U7WS3|lex-leadership> instead."
    assert app_mod._validate_channel_links(text, client) == text


def test_hallucinated_channel_link_degrades_to_plain_name():
    client = _client()
    text = "Ask in <#C0B5LPJS41G|lex-llc> or <#C0B5M4R7ZQX|lex-leadership>."
    out = app_mod._validate_channel_links(text, client)
    assert out == "Ask in #lex-llc or #lex-leadership."


def test_hallucinated_link_without_label():
    client = _client()
    out = app_mod._validate_channel_links("See <#C0FAKE12345>.", client)
    assert out == "See the relevant channel."


def test_transient_error_keeps_token_and_is_not_cached():
    client = _client(error_ids={"C0B3A3U7WS3"})
    text = "Ask in <#C0B3A3U7WS3|lex-leadership>."
    assert app_mod._validate_channel_links(text, client) == text
    assert "C0B3A3U7WS3" not in app_mod._channel_link_cache


def test_verdicts_are_cached():
    client = _client(valid_ids={"C0REAL"})
    text = "<#C0REAL|a> <#C0REAL|a> <#C0FAKE|b> <#C0FAKE|b>"
    out = app_mod._validate_channel_links(text, client)
    assert out == "<#C0REAL|a> <#C0REAL|a> #b #b"
    # one lookup per distinct id
    assert client.conversations_info.call_count == 2


def test_mixed_valid_and_invalid():
    client = _client(valid_ids={"C0B4KRQT3LY"})
    text = "Real: <#C0B4KRQT3LY|f3e-leadership>, fake: <#C0DEADBEEF|ghost>."
    out = app_mod._validate_channel_links(text, client)
    assert out == "Real: <#C0B4KRQT3LY|f3e-leadership>, fake: #ghost."


# ---------------------------------------------------------------------------
# _fix_lex_channel_names — nonexistent #lex-<subentity> plain-text rewrite
# ---------------------------------------------------------------------------

def test_lex_alias_rewrites_all_four_subentities():
    text = "Ask in #lex-llc, #lex-lts, #lex-lbhs, or #lex-lla."
    out = app_mod._fix_lex_channel_names(text)
    assert out == "Ask in #llc, #lts, #lbhs, or #lla."


def test_lex_alias_rewrites_suffixed_variants():
    out = app_mod._fix_lex_channel_names("Try #lex-llc-leadership for that.")
    assert out == "Try #llc-leadership for that."


def test_lex_alias_leaves_real_lex_channels_alone():
    text = "Financial questions go to #lex-finance or #lex-leadership."
    assert app_mod._fix_lex_channel_names(text) == text


def test_lex_alias_no_lex_mentions_short_circuits():
    text = "Nothing about those channels here."
    assert app_mod._fix_lex_channel_names(text) is text
