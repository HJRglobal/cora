"""S6a (Code #14, cq-a5b3e6a2e844): run_drive_sweep --with-slack summary target.

#cora-drive-sweep never existed -- chat.postMessage returned channel_not_found on
19 of 19 task logs (2026-09-03..09-22), so every summary was dropped. The target is
now a pinned channel ID (#cora-health), the env override is honoured only when it is
an ID, the Slack error code logs on ONE line, and a target-is-wrong code falls back
ONCE to Harrison's DM. Drives the real functions against a fake WebClient -- no
network, no Slack.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "run_drive_sweep_s6a", _REPO_ROOT / "scripts" / "run_drive_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeClient:
    """Records every call. ``fail`` maps a channel to the Slack error code its
    chat_postMessage raises."""

    instances: list = []

    def __init__(self, token=None, fail=None):
        self.token = token
        self.fail = dict(fail or {})
        self.posts: list[tuple[str, str]] = []
        self.opens: list[list[str]] = []
        _FakeClient.instances.append(self)

    def chat_postMessage(self, channel, text, **_kw):
        if channel in self.fail:
            raise SlackApiError("The request to the Slack API failed.",
                                {"ok": False, "error": self.fail[channel]})
        self.posts.append((channel, text))
        return {"ok": True}

    def conversations_open(self, users):
        self.opens.append(list(users))
        return {"ok": True, "channel": {"id": "D0HARRISONDM"}}


@pytest.fixture
def fake_slack(monkeypatch):
    """Patch slack_sdk.WebClient with a factory whose fail-map the test sets."""
    import slack_sdk
    _FakeClient.instances = []
    state = {"fail": {}}

    def factory(token=None, **_kw):
        return _FakeClient(token=token, fail=state["fail"])

    monkeypatch.setattr(slack_sdk, "WebClient", factory)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("DRIVE_SWEEP_NOTIFY_CHANNEL", raising=False)
    return state


_STATS = {"accounts_swept": 1, "files_enumerated": 2}


def test_summary_posts_to_pinned_channel_id(fake_slack):
    mod = _load()
    mod._notify_after_run(_STATS, with_slack=True, dry_run=False)
    (client,) = _FakeClient.instances
    assert [c for c, _ in client.posts] == ["C0B7CADQ98S"]
    assert mod.DRIVE_SWEEP_NOTIFY_CHANNEL_ID == "C0B7CADQ98S"
    assert re.fullmatch(r"C[A-Z0-9]+", client.posts[0][0])
    assert client.posts[0][0] != "cora-drive-sweep"


def test_env_bare_name_rejected_falls_back_to_constant(fake_slack, monkeypatch, caplog):
    mod = _load()
    monkeypatch.setenv("DRIVE_SWEEP_NOTIFY_CHANNEL", "cora-drive-sweep")
    with caplog.at_level(logging.WARNING, logger="run_drive_sweep"):
        assert mod._resolve_notify_channel() == "C0B7CADQ98S"
    assert any("not a Slack channel id" in r.getMessage() for r in caplog.records)


def test_env_channel_id_is_honoured(fake_slack, monkeypatch):
    mod = _load()
    monkeypatch.setenv("DRIVE_SWEEP_NOTIFY_CHANNEL", "C0B4B0URRQS")
    assert mod._resolve_notify_channel() == "C0B4B0URRQS"
    monkeypatch.setenv("DRIVE_SWEEP_NOTIFY_CHANNEL", "C0B4B0URRQS\n")   # stripped, then fullmatch
    assert mod._resolve_notify_channel() == "C0B4B0URRQS"
    monkeypatch.setenv("DRIVE_SWEEP_NOTIFY_CHANNEL", "#C0B4B0URRQS")
    assert mod._resolve_notify_channel() == "C0B7CADQ98S"


def test_slack_error_code_logged_single_line(fake_slack, caplog):
    mod = _load()
    fake_slack["fail"] = {"C0BRATELIMIT": "ratelimited"}
    with caplog.at_level(logging.WARNING, logger="run_drive_sweep"):
        mod._post_slack_summary(_STATS, dry_run=False, channel="C0BRATELIMIT")
    hits = [r.getMessage() for r in caplog.records if "error=ratelimited" in r.getMessage()]
    assert len(hits) == 1 and "\n" not in hits[0]
    assert "channel=C0BRATELIMIT" in hits[0]


@pytest.mark.parametrize("code", ["channel_not_found", "is_archived", "not_in_channel"])
def test_target_wrong_falls_back_to_harrison_dm_once(fake_slack, caplog, code):
    from cora.repeat_signal import HARRISON_SLACK_ID
    mod = _load()
    fake_slack["fail"] = {"C0BGONEGONE": code}
    with caplog.at_level(logging.WARNING, logger="run_drive_sweep"):
        mod._post_slack_summary(_STATS, dry_run=False, channel="C0BGONEGONE")
    (client,) = _FakeClient.instances
    assert client.opens == [[HARRISON_SLACK_ID]]
    assert [c for c, _ in client.posts] == ["D0HARRISONDM"]            # exactly one DM post
    warn = [r.getMessage() for r in caplog.records if "falling back ONCE" in r.getMessage()]
    assert len(warn) == 1 and "C0BGONEGONE" in warn[0] and f"error={code}" in warn[0]


def test_other_error_no_dm(fake_slack):
    mod = _load()
    fake_slack["fail"] = {"C0BRATELIMIT": "ratelimited"}
    mod._post_slack_summary(_STATS, dry_run=False, channel="C0BRATELIMIT")
    (client,) = _FakeClient.instances
    assert client.opens == [] and client.posts == []


def test_dm_fallback_failure_is_logged_not_raised(fake_slack, caplog):
    mod = _load()
    fake_slack["fail"] = {"C0BGONEGONE": "channel_not_found", "D0HARRISONDM": "cannot_dm_bot"}
    with caplog.at_level(logging.WARNING, logger="run_drive_sweep"):
        mod._post_slack_summary(_STATS, dry_run=False, channel="C0BGONEGONE")
    assert any("DM fallback failed: error=cannot_dm_bot" in r.getMessage() for r in caplog.records)


def test_dry_run_never_posts(fake_slack):
    mod = _load()
    mod._notify_after_run(_STATS, with_slack=True, dry_run=True)
    mod._notify_after_run(_STATS, with_slack=False, dry_run=False)
    assert _FakeClient.instances == []


def test_main_dry_run_with_slack_never_constructs_a_client(fake_slack, monkeypatch, tmp_path):
    """D-290: drive the REAL main() with --dry-run --with-slack; the only Slack write
    site must be unreachable."""
    mod = _load()
    sa = tmp_path / "sa.json"
    sa.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(sa))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(mod, "_setup_logging", lambda: None)
    import anthropic
    import cora.knowledge_base as kbmod
    import cora.connectors.drive_sweep as ds
    monkeypatch.setattr(kbmod, "KnowledgeBase", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(anthropic, "Anthropic", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(ds, "run_sweep", lambda **_k: dict(_STATS))
    monkeypatch.setattr(sys, "argv", ["run_drive_sweep.py", "--dry-run", "--with-slack"])
    assert mod.main() == 0
    assert _FakeClient.instances == []
    # ...and the same run WITHOUT --dry-run does post (the guard, not a broken path).
    monkeypatch.setattr(sys, "argv", ["run_drive_sweep.py", "--with-slack"])
    assert mod.main() == 0
    assert [c for c, _ in _FakeClient.instances[0].posts] == ["C0B7CADQ98S"]


@pytest.mark.parametrize("ch", [" ", "-", "C", "0", "A"])
def test_channel_id_regex_linear_on_degenerate_input(ch):
    """D-171 growth shape: 40k of each character the pattern could chew must stay fast.
    Best of 3 (D-051 integration-tests-6): one sample flakes under host load."""
    from _timing import best_of_3
    mod = _load()
    rx = mod._SLACK_CHANNEL_ID_RE
    for n in (10_000, 40_000):
        s = ch * n

        def run():
            rx.fullmatch(s)
            rx.fullmatch(s + "!")
            rx.fullmatch("C" + s)
        assert best_of_3(run) < 0.2
