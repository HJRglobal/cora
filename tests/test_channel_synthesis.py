"""Tests for the daily channel synthesis (channel_synthesis.py).

Slice 0: shared primitives -- entity-prefix task filter, TIER_1 allowlist +
deliver_to_channel (egress + fail-soft + fail-closed tier gate), and the
standalone-script (D-047) + source-post-site guards.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import asana_filters as af
from cora import channel_synthesis as cs
from cora.reply_formatter import normalize_slack_bold
from cora.slack_egress import sanitize_text


# ---------------------------------------------------------------------------
# Entity-prefix task filtering (asana_filters.task_belongs_to_entity)
# ---------------------------------------------------------------------------

class TestEntityPrefixFilter:
    def _task(self, *project_names, memberships=None):
        t = {"projects": [{"name": n} for n in project_names]}
        if memberships is not None:
            t["memberships"] = [{"project": {"name": n}} for n in memberships]
        return t

    def test_f3e_matches_bracket_f3e(self):
        assert af.task_belongs_to_entity(self._task("[F3E] Sales Pipeline"), "F3E")

    def test_f3e_matches_brand_line(self):
        assert af.task_belongs_to_entity(self._task("[F3 Pure] Launch"), "F3E")

    def test_f3c_separated_from_f3e(self):
        """F3C is a SEPARATE entity here; an [F3C] task must NOT match F3E and an
        [F3E] task must NOT match F3C (no cross-entity bleed between the two)."""
        f3c_task = self._task("[F3C] Nonprofit gala")
        assert af.task_belongs_to_entity(f3c_task, "F3C")
        assert not af.task_belongs_to_entity(f3c_task, "F3E")
        f3e_task = self._task("[F3E] Retail")
        assert not af.task_belongs_to_entity(f3e_task, "F3C")

    def test_f3_community_prefix_is_f3c_not_f3e(self):
        t = self._task("[F3 Community] Education foundation")
        assert af.task_belongs_to_entity(t, "F3C")
        assert not af.task_belongs_to_entity(t, "F3E")

    def test_lex_union_prefixes(self):
        for name in ("[LEX] Ops", "[LEX-LLC] DDD", "[LTS] Thing",
                     "[LBHS] COPA", "[LLA] X", "[LLC] Admin"):
            assert af.task_belongs_to_entity(self._task(name), "LEX"), name

    def test_case_insensitive(self):
        assert af.task_belongs_to_entity(self._task("[osn] gilbert"), "OSN")
        assert af.task_belongs_to_entity(self._task("[OsN] Gilbert"), "OSN")

    def test_reads_memberships_project_names(self):
        t = {"projects": [], "memberships": [{"project": {"name": "[HJRP] Leases"}}]}
        assert af.task_belongs_to_entity(t, "HJRP")

    def test_hjrprod_subcodes(self):
        for name in ("[HJRPROD] X", "[POD] Episode", "[FF] Falling Forward"):
            assert af.task_belongs_to_entity(self._task(name), "HJRPROD"), name

    def test_unknown_entity_is_false(self):
        assert not af.task_belongs_to_entity(self._task("[F3E] X"), "NOPE")

    def test_no_projects_is_false(self):
        assert not af.task_belongs_to_entity({"projects": []}, "F3E")


# ---------------------------------------------------------------------------
# TIER_1 allowlist + deliver_to_channel
# ---------------------------------------------------------------------------

class _FakeClient:
    """Records chat_postMessage calls; never opens a DM."""
    last: dict = {}

    def __init__(self, token):
        _FakeClient.last = {"token": token}

    def chat_postMessage(self, channel, text):
        _FakeClient.last["channel"] = channel
        _FakeClient.last["text"] = text
        return {"ok": True}


class _BoomClient:
    def __init__(self, token):
        pass

    def chat_postMessage(self, channel, text):
        raise RuntimeError("slack down")


class TestTierAllowlist:
    def test_all_scope_channels_are_tier1(self):
        for scope, cid in cs.SCOPE_CHANNELS.items():
            assert cs._assert_tier1(cid), scope

    def test_smoke_channel_is_tier1(self):
        assert cs._assert_tier1(cs.SMOKE_CHANNEL)

    def test_founder_operations_is_allowlisted(self):
        # D1: the name classifier mis-classifies #founder-operations as TIER_3;
        # the id allowlist must still accept it (the portfolio post's target).
        assert cs._assert_tier1("C0BCUBUDHAR")

    def test_random_channel_refused(self):
        assert not cs._assert_tier1("C0DEADBEEF")
        assert not cs._assert_tier1("")


class TestDeliverToChannel:
    def test_refuses_non_tier1_and_posts_nothing(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        ok = cs.deliver_to_channel("C0NOTALLOWED", "portfolio cash $1,000,000")
        assert ok is False
        assert "channel" not in _FakeClient.last  # never attempted a post

    def test_posts_to_allowlisted_channel(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        ok = cs.deliver_to_channel(cs.SCOPE_CHANNELS["portfolio"], "hello team")
        assert ok is True
        assert _FakeClient.last["channel"] == cs.SCOPE_CHANNELS["portfolio"]
        assert "hello team" in _FakeClient.last["text"]

    def test_normalizes_bold_and_sanitizes(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        body = ("**Cash** update: see "
                "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view")
        ok = cs.deliver_to_channel(cs.SCOPE_CHANNELS["f3e"], body)
        assert ok is True
        expected = sanitize_text(normalize_slack_bold(body))[:cs._MAX_SLACK_CHARS]
        assert _FakeClient.last["text"] == expected
        # bold was normalized (** -> *) and the raw drive URL did not survive verbatim
        assert "**Cash**" not in _FakeClient.last["text"]
        assert "*Cash*" in _FakeClient.last["text"]

    def test_no_token_fails_soft(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        assert cs.deliver_to_channel(cs.SCOPE_CHANNELS["osn"], "x") is False

    def test_empty_body_fails_soft(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        assert cs.deliver_to_channel(cs.SCOPE_CHANNELS["osn"], "") is False

    def test_post_exception_fails_soft(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _BoomClient)
        assert cs.deliver_to_channel(cs.SCOPE_CHANNELS["bdm"], "body") is False


# ---------------------------------------------------------------------------
# Standalone-script (D-047) + source-post-site guards
# ---------------------------------------------------------------------------

class TestNoBotProcessImport:
    def test_import_does_not_pull_bot_modules(self):
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "import cora.channel_synthesis; "
            "bad = [m for m in ('cora.app', 'cora.tool_dispatch', 'cora.claude_client')"
            " if m in sys.modules]; "
            "assert not bad, f'bot-process modules imported: {bad}'"
        ) % str(_REPO_ROOT / "src")
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr


class TestSourcePostSites:
    def test_channel_module_posts_to_channels_never_opens_dm(self):
        """channel_synthesis posts to channels (chat_postMessage) but NEVER opens
        a DM (conversations_open) -- that path belongs to the Harrison-only memo."""
        src = (_REPO_ROOT / "src" / "cora" / "channel_synthesis.py").read_text(
            encoding="utf-8")
        assert src.count("conversations_open") == 0
        assert src.count("chat_postMessage") == 1

    def test_strategy_memo_harrison_only_invariant_unchanged(self):
        """The weekly memo's Harrison-only guarantee must remain provably intact:
        exactly one channel-post + one DM-open site, still hard-coded to Harrison."""
        src = (_REPO_ROOT / "src" / "cora" / "strategy_memo.py").read_text(
            encoding="utf-8")
        assert src.count("chat_postMessage") == 1
        assert src.count("conversations_open") == 1
        assert 'HARRISON_SLACK_ID = "U0B2RM2JYJ1"' in src
