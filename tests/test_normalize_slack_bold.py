"""WS-5: reply_formatter.normalize_slack_bold + proactive-sender wiring.

The 2026-07-01 format-standard ship fixed conversational bold via format_reply
but deferred the proactive senders (the daily briefing was emitting literal
**bold** into DMs). normalize_slack_bold converts **/__ bold to Slack *bold*
and does NOTHING else -- code fences, inline code, and sanctioned <...> tokens
are protected wholesale.
"""

from unittest.mock import MagicMock, patch

from cora.reply_formatter import normalize_slack_bold


class TestNormalizeSlackBold:
    def test_double_star_converts(self):
        assert normalize_slack_bold("**Top item** for today") == "*Top item* for today"

    def test_double_underscore_converts(self):
        assert normalize_slack_bold("__urgent__ item") == "*urgent* item"

    def test_already_slack_bold_is_untouched(self):
        text = "*already bold* and _italic_ stay"
        assert normalize_slack_bold(text) == text

    def test_idempotent(self):
        once = normalize_slack_bold("**x** and **y**")
        assert normalize_slack_bold(once) == once

    def test_fenced_block_protected(self):
        text = "Summary:\n```\ncol_a    **raw**   12.00\n```\n**after** fence"
        out = normalize_slack_bold(text)
        assert "col_a    **raw**   12.00" in out   # fence content untouched
        assert "*after* fence" in out

    def test_inline_code_protected(self):
        text = "run `pip install **pkg**` then **read** the docs"
        out = normalize_slack_bold(text)
        assert "`pip install **pkg**`" in out
        assert "*read* the docs" in out

    def test_slack_tokens_protected(self):
        text = "<https://example.com|**not a bold**> and **real bold**"
        out = normalize_slack_bold(text)
        assert "<https://example.com|**not a bold**>" in out
        assert "*real bold*" in out

    def test_multiline_bold_not_matched(self):
        text = "**spans\nlines** stays"
        assert normalize_slack_bold(text) == text

    def test_no_other_transformation(self):
        # Emoji, dashes, headers, bullets, URLs -- all untouched.
        text = "# Header\n- bullet — dash 🎉 https://example.com **b**"
        out = normalize_slack_bold(text)
        assert out == "# Header\n- bullet — dash 🎉 https://example.com *b*"

    def test_falsy_and_non_str_passthrough(self):
        assert normalize_slack_bold("") == ""
        assert normalize_slack_bold(None) is None

    def test_unterminated_fence_left_alone(self):
        text = "```\n**inside unterminated**"
        out = normalize_slack_bold(text)
        # No closing fence -> no fence match -> bold inside converts (the
        # conservative reading: an unterminated fence is not a real block).
        assert "*inside unterminated*" in out


class TestWiring:
    def test_briefing_synthesize_normalizes(self, monkeypatch):
        import scripts.run_daily_briefing as rdb

        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="**Top of plate**: close the PO")]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_resp
        with patch("anthropic.Anthropic", return_value=fake_client):
            rec = MagicMock()
            rec.name = "Tommy Anderson"
            out = rdb._synthesize(api_key="sk-test", rec=rec,
                                  sections_text="s", chunks=[], today_str="t")
        assert out == "*Top of plate*: close the PO"

    def test_strategy_memo_slack_copy_normalized(self, monkeypatch):
        from cora import strategy_memo as sm

        sent = {}

        class _FakeClient:
            def __init__(self, token=""):
                pass

            def conversations_open(self, users):
                return {"channel": {"id": "D1"}}

            def chat_postMessage(self, channel, text):
                sent["text"] = text
                return {"ok": True}

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        with patch("slack_sdk.WebClient", _FakeClient):
            ok = sm.deliver_to_harrison("**Cash concentration** worsened")
        assert ok is True
        assert "*Cash concentration* worsened" in sent["text"]
        assert "**" not in sent["text"]

    def test_coras_read_line_normalized(self):
        from cora.knowledge_review import format_single_item_dm
        dm = format_single_item_dm({
            "update_type": "known_answer", "confidence": "HIGH",
            "description": "desc",
            "_coras_read": "Cora's read: **corroborated** by two sources",
        })
        assert "*corroborated*" in dm
        assert "**corroborated**" not in dm
