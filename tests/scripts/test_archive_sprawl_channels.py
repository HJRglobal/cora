"""Code #14 R14-7c: scripts/archive_sprawl_channels.py hard-fails without
SLACK_USER_TOKEN -- in BOTH modes, before any Slack client exists.

Before: `token = user_token or os.environ.get("SLACK_BOT_TOKEN")` -- the owner-token
script silently ran as the BOT (a dry-run still reads every channel; an --apply
archives). Ruled 2026-09-19 (4b.4): no fallback. With the key absent from the live
.env (and the runbook saying not to add it), the ruled outcome is that the script
cannot run at all until Harrison deliberately provisions the key.

The module is loaded with load_dotenv neutralised (the real .env never reaches this
process) and WebClient replaced by a recorder that RAISES if built on the refusal
path -- so "no Slack call" is proven by construction, not by a mock of main().
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "archive_sprawl_channels.py"

_BOT = "".join(("xox", "b-test-bot-token"))    # assembled: no literal token shape in source
_USER = "".join(("xox", "p-test-user-token"))


def _load(monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    spec = importlib.util.spec_from_file_location("_archive_sprawl_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    pass


def _run(mod, monkeypatch, argv):
    """main() parses sys.argv (the script's real CLI surface) -- drive it there."""
    monkeypatch.setattr(sys, "argv", ["archive_sprawl_channels.py", *argv])
    return mod.main()


@pytest.fixture()
def sprawl(monkeypatch):
    m = _load(monkeypatch)
    built: list[str] = []

    def _refuse_client(*a, **k):
        built.append(k.get("token") or (a[0] if a else ""))
        raise AssertionError("WebClient constructed on the refusal path")

    monkeypatch.setattr(m, "WebClient", _refuse_client)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", _BOT)
    m._built = built
    return m


@pytest.mark.parametrize("argv", [[], ["--apply"], ["--apply", "--also", "C0TEST00001"]])
def test_no_user_token_exits_in_every_mode_before_any_client(sprawl, monkeypatch, argv):
    with pytest.raises(SystemExit) as exc:
        _run(sprawl, monkeypatch, argv)
    msg = str(exc.value.code)
    assert "SLACK_USER_TOKEN not set" in msg
    assert "never falls back to SLACK_BOT_TOKEN" in msg
    assert "Nothing was read or archived" in msg
    assert sprawl._built == [], "a Slack client was built without the owner token"


def test_blank_user_token_is_absent(sprawl, monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "   ")
    with pytest.raises(SystemExit):
        _run(sprawl, monkeypatch, [])
    assert sprawl._built == []


def test_with_the_user_token_construction_proceeds_on_that_token(sprawl, monkeypatch):
    """The refusal is the ONLY change: with the key present the script builds its
    client on the USER token (never the bot token) and carries on to indexing."""
    built: list[str] = []

    class _Client:
        def __init__(self, token="", **_kw):
            built.append(token)

    def _stop_index(client):
        raise _Stop()

    monkeypatch.setattr(sprawl, "WebClient", _Client)
    monkeypatch.setattr(sprawl, "build_channel_index", _stop_index)
    monkeypatch.setenv("SLACK_USER_TOKEN", _USER)
    with pytest.raises(_Stop):
        _run(sprawl, monkeypatch, [])
    assert built == [_USER]


def test_source_never_reads_the_bot_token():
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'os.environ.get("SLACK_BOT_TOKEN")' not in src
    assert "os.environ.get('SLACK_BOT_TOKEN')" not in src
    assert 'environ["SLACK_BOT_TOKEN"]' not in src
