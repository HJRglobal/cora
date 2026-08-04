"""Delivery + channel-guard tests for scripts/run_finance_close_pack.py.

THE ACCEPTANCE CRITERION THIS FILE OWNS
---------------------------------------
"No figures appear in any non-finance channel." Asserted against the NEW target
(#hjrg-finance C0B3V5SDNAG), per the 2026-08-04 rider: #hjr-finance (C0BAK65N4TA)
is ARCHIVED, so a post there fails ``is_archived`` and reaches nobody -- the same
silent-failure mode the finance-receipt digest logged through July (W4-02).
#hjrg-finance classifies TIER_1 (function "finance"), so the firewall's intent is
preserved by the repoint rather than weakened by it.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _load_script():
    """Import the delivery script by path (scripts/ is not a package)."""
    path = _REPO / "scripts" / "run_finance_close_pack.py"
    spec = importlib.util.spec_from_file_location("_run_finance_close_pack", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_run_finance_close_pack"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


class _FakeClient:
    """Records every send instead of performing it."""

    def __init__(self, fail_channels: set[str] | None = None, fail_dm: bool = False):
        self.posts: list[tuple[str, str]] = []
        self.fail_channels = fail_channels or set()
        self.fail_dm = fail_dm

    def conversations_open(self, users):
        if self.fail_dm:
            raise RuntimeError("cannot open DM")
        return {"channel": {"id": f"D-{users[0]}"}}

    def chat_postMessage(self, channel, text, **_kw):
        if channel in self.fail_channels:
            raise RuntimeError(f"channel_not_found/{channel}")
        self.posts.append((channel, text))
        return {"ok": True, "ts": "1.0"}


def _pack(flags: int = 0, unavailable: bool = False):
    from cora.finance_close import ClosePack, Section

    lines = ["• F3 Energy: sheet $100,000 vs books $140,000 — delta +$40,000"]
    if flags:
        lines = [":triangular_flag_on_post: " + lines[0]]
    sections = [Section(key="cash", title="Cash", lines=lines, flags=flags)]
    if unavailable:
        sections.append(Section(
            key="renewals", title="Renewals", available=False, stub_reason="map missing",
        ))
    return ClosePack(generated_at="2026-08-03", sections=sections)


# ── target constants ─────────────────────────────────────────────────────────

def test_targets_are_the_live_finance_surfaces(script):
    assert script.HJRG_FINANCE_CHANNEL == "C0B3V5SDNAG"      # #hjrg-finance
    assert script.FOUNDER_FINANCE_CHANNEL == "C0BCXPJDP42"   # #founder-finance
    assert script.JUSTIN_SLACK_ID == "U0B3AEJCYGP"           # Justin Moran


def test_archived_channel_is_never_a_delivery_target(script):
    """#hjr-finance is archived -- a post there fails is_archived and reaches nobody."""
    assert script.ARCHIVED_HJR_FINANCE == "C0BAK65N4TA"
    assert script.ARCHIVED_HJR_FINANCE not in script.FINANCE_SURFACES
    assert script.HJRG_FINANCE_CHANNEL != script.ARCHIVED_HJR_FINANCE
    assert script.FOUNDER_FINANCE_CHANNEL != script.ARCHIVED_HJR_FINANCE


def test_every_allowlisted_surface_classifies_tier_1(script):
    """The repoint must preserve the finance-firewall intent, not sidestep it."""
    from cora.channel_classifier import classify_function, is_tier_1

    for channel_id, name in script.FINANCE_SURFACES.items():
        function = classify_function(name)
        entity = "HJRG" if name.startswith("hjrg") else "FNDR"
        assert is_tier_1(entity, function), (
            f"{name} ({channel_id}) is not a TIER_1 finance surface"
        )


def test_finance_surface_allowlist_is_closed(script):
    """Exactly the two intended channels -- adding one is a deliberate decision."""
    assert set(script.FINANCE_SURFACES) == {
        script.HJRG_FINANCE_CHANNEL, script.FOUNDER_FINANCE_CHANNEL,
    }


# ── the guard: no figures off a finance surface ──────────────────────────────

def test_assert_finance_surface_refuses_unknown_channel(script):
    with pytest.raises(script.DeliveryTargetError):
        script._assert_finance_surface("C0B6GT3117Y")   # #f3-athletes (the F-12 surface)
    with pytest.raises(script.DeliveryTargetError):
        script._assert_finance_surface(script.ARCHIVED_HJR_FINANCE)
    with pytest.raises(script.DeliveryTargetError):
        script._assert_finance_surface("")


def test_post_to_channel_refuses_non_finance_channel_before_sending(script):
    """ACCEPTANCE: pack content cannot reach a non-finance channel.

    The refusal must happen BEFORE the send, so nothing is transmitted even if the
    exception is swallowed upstream.
    """
    client = _FakeClient()
    with pytest.raises(script.DeliveryTargetError):
        script.post_to_channel(client, "C0B6GT3117Y", "revenue was $320,615")
    assert client.posts == []


def test_ops_failure_notice_carries_no_money_figure(script):
    """The ops channel is NOT a finance surface, so the notice must be figure-free.

    Uses the same money-figure detector channel_content_guard enforces with, so this
    test tracks that definition rather than a private copy of it.
    """
    from cora.channel_content_guard import _has_money_figure

    notice = script._delivery_failure_notice(["#hjrg-finance", "DM Justin"], 7)
    assert not _has_money_figure(notice)
    assert "$" not in notice
    assert "flag count: 7" in notice          # counts are fine; amounts are not
    assert "#hjrg-finance" in notice


def test_ops_alert_channel_is_not_a_finance_surface(script, monkeypatch):
    monkeypatch.delenv("FINANCE_DIGEST_FALLBACK_CHANNEL", raising=False)
    monkeypatch.delenv("HEALTH_REPORT_CHANNEL", raising=False)
    assert script._ops_alert_channel() == "hjrg-leadership"
    assert script._ops_alert_channel() not in script.FINANCE_SURFACES.values()


# ── founder cut ──────────────────────────────────────────────────────────────

def test_founder_cut_contains_only_flagged_lines(script):
    from cora.finance_close import ClosePack, Section

    pack = ClosePack(generated_at="2026-08-03", sections=[
        Section(key="cash", title="Cash", flags=1, lines=[
            ":triangular_flag_on_post: F3 Energy delta +$40,000",
            "• OSN Warner: sheet $10,000 vs books $10,100 — delta +$100",
        ]),
    ])
    cut = script.build_founder_cut(pack)
    assert "+$40,000" in cut
    assert "+$100" not in cut       # unflagged detail stays out of the founder cut


def test_founder_cut_names_unavailable_sections(script):
    """"No flags" must never be mistakable for "everything checked out"."""
    cut = script.build_founder_cut(_pack(flags=0, unavailable=True))
    assert "unavailable: map missing" in cut


def test_founder_cut_says_so_when_nothing_flagged(script):
    cut = script.build_founder_cut(_pack(flags=0))
    assert "No item crossed a flag threshold" in cut


def test_founder_cut_points_at_the_full_pack(script):
    assert "#hjrg-finance" in script.build_founder_cut(_pack(flags=1))


def test_founder_cut_includes_warning_and_siren_lines(script):
    from cora.finance_close import ClosePack, Section

    pack = ClosePack(generated_at="2026-08-03", sections=[
        Section(key="renewals", title="Renewals", flags=2, lines=[
            ":rotating_light: PAST DUE 14d — Meta Verified",
            ":warning: cash sheet appears BEHIND",
            "• Nothing else due.",
        ]),
    ])
    cut = script.build_founder_cut(pack)
    assert "PAST DUE" in cut and "BEHIND" in cut
    assert "Nothing else due" not in cut


# ── delivery orchestration ───────────────────────────────────────────────────

def test_post_to_channel_success_and_sanitization(script):
    client = _FakeClient()
    assert script.post_to_channel(client, script.HJRG_FINANCE_CHANNEL, "**bold** text") is True
    channel, text = client.posts[0]
    assert channel == script.HJRG_FINANCE_CHANNEL
    assert "**" not in text          # normalize_slack_bold ran on the way out


def test_post_to_channel_failure_is_soft(script):
    client = _FakeClient(fail_channels={script.HJRG_FINANCE_CHANNEL})
    assert script.post_to_channel(client, script.HJRG_FINANCE_CHANNEL, "x") is False


def test_dm_user_success_and_failure(script):
    ok = _FakeClient()
    assert script.dm_user(ok, script.JUSTIN_SLACK_ID, "x") is True
    assert ok.posts[0][0] == f"D-{script.JUSTIN_SLACK_ID}"
    bad = _FakeClient(fail_dm=True)
    assert script.dm_user(bad, script.JUSTIN_SLACK_ID, "x") is False


def test_post_ops_alert_never_raises(script):
    client = _FakeClient(fail_channels={"hjrg-leadership"})
    script.post_ops_alert(client, ["#hjrg-finance"], 3)   # must not raise


# ── dedup ────────────────────────────────────────────────────────────────────

def test_iso_week_format(script):
    assert script._iso_week(datetime.date(2026, 8, 3)) == "2026-W32"


def test_dedup_roundtrip(script, tmp_path, monkeypatch):
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    assert script._already_sent("2026-W32") is False
    script._mark_sent("2026-W32")
    assert script._already_sent("2026-W32") is True
    assert script._already_sent("2026-W33") is False


def test_dedup_tolerates_corrupt_state(script, tmp_path, monkeypatch):
    path = tmp_path / "sent.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(script, "_DEDUP_PATH", path)
    assert script._already_sent("2026-W32") is False


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_dry_run_posts_nothing_and_writes_no_snapshot(script, monkeypatch, capsys, tmp_path):
    """A dry run must not advance the WoW baseline.

    Advancing it would leave the next real run diffing against a snapshot nobody
    ever read, silently reporting zero movement.
    """
    from cora import finance_close

    seen: dict = {}

    def fake_build(**kwargs):
        seen.update(kwargs)
        return _pack(flags=1)

    monkeypatch.setattr(finance_close, "build_pack", fake_build)
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py", "--dry-run"])
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")

    assert script.main() == 0
    assert seen["persist_snapshot"] is False
    out = capsys.readouterr().out
    assert "[DRY RUN] FULL PACK" in out and "FOUNDER CUT" in out
    assert not (tmp_path / "sent.json").exists()


def test_dry_run_ignores_dedup(script, monkeypatch, tmp_path):
    from cora import finance_close

    path = tmp_path / "sent.json"
    monkeypatch.setattr(script, "_DEDUP_PATH", path)
    script._mark_sent(script._iso_week())
    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack())
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py", "--dry-run"])
    assert script.main() == 0


def test_entities_filter_is_parsed_and_uppercased(script, monkeypatch, tmp_path):
    from cora import finance_close

    seen: dict = {}

    def fake_build(**kwargs):
        seen.update(kwargs)
        return _pack()

    monkeypatch.setattr(finance_close, "build_pack", fake_build)
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setattr(sys, "argv",
                        ["x", "--dry-run", "--entities", " f3e , osngw "])
    script.main()
    assert seen["entities"] == ["F3E", "OSNGW"]


def test_no_entities_flag_means_all(script, monkeypatch, tmp_path):
    from cora import finance_close

    seen: dict = {}
    monkeypatch.setattr(finance_close, "build_pack",
                        lambda **kw: (seen.update(kw), _pack())[1])
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run"])
    script.main()
    assert seen["entities"] is None


def test_dedup_skips_a_second_live_run(script, monkeypatch, tmp_path):
    from cora import finance_close

    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    script._mark_sent(script._iso_week())
    called = {"n": 0}
    monkeypatch.setattr(finance_close, "build_pack",
                        lambda **_k: (called.update(n=called["n"] + 1), _pack())[1])
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py"])
    assert script.main() == 0
    assert called["n"] == 0        # skipped before doing any QBO work


def test_missing_bot_token_exits_nonzero(script, monkeypatch, tmp_path):
    from cora import finance_close

    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack())
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py"])
    assert script.main() == 1


def test_live_run_delivers_three_cuts(script, monkeypatch, tmp_path):
    from cora import finance_close

    client = _FakeClient()
    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack(flags=1))
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setitem(sys.modules, "slack_sdk",
                        type(sys)("slack_sdk"))
    sys.modules["slack_sdk"].WebClient = lambda token: client
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py"])

    assert script.main() == 0
    targets = [c for c, _ in client.posts]
    assert script.HJRG_FINANCE_CHANNEL in targets
    assert script.FOUNDER_FINANCE_CHANNEL in targets
    assert f"D-{script.JUSTIN_SLACK_ID}" in targets
    assert len(client.posts) == 3
    assert script._already_sent(script._iso_week()) is True


def test_total_delivery_failure_returns_nonzero_and_does_not_mark_sent(
    script, monkeypatch, tmp_path,
):
    """All three surfaces dead: exit nonzero and leave dedup clear so a retry runs."""
    from cora import finance_close

    client = _FakeClient(
        fail_channels={script.HJRG_FINANCE_CHANNEL, script.FOUNDER_FINANCE_CHANNEL,
                       "hjrg-leadership"},
        fail_dm=True,
    )
    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack(flags=2))
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setitem(sys.modules, "slack_sdk", type(sys)("slack_sdk"))
    sys.modules["slack_sdk"].WebClient = lambda token: client
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py"])

    assert script.main() == 1
    assert script._already_sent(script._iso_week()) is False


def test_partial_failure_records_only_the_delivered_targets(script, monkeypatch, tmp_path):
    """Per-target dedup: the two that worked are recorded, the failure is not."""
    from cora import finance_close

    client = _FakeClient(fail_channels={script.FOUNDER_FINANCE_CHANNEL})
    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack(flags=1))
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setitem(sys.modules, "slack_sdk", type(sys)("slack_sdk"))
    sys.modules["slack_sdk"].WebClient = lambda token: client
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py"])

    assert script.main() == 0
    sent = script._sent_targets(script._iso_week())
    assert sent == {script.TARGET_HJRG, script.TARGET_DM}
    # Not fully sent, so a retry is still allowed -- for the failed target only.
    assert script._already_sent(script._iso_week()) is False
    ops = [t for c, t in client.posts if c == "hjrg-leadership"]
    assert ops and "could not be fully delivered" in ops[0]
    assert "#founder-finance" in ops[0]


def test_retry_after_partial_failure_only_posts_the_missing_target(
    script, monkeypatch, tmp_path,
):
    """A kill mid-delivery must not re-post the pack to surfaces that already got it."""
    from cora import finance_close

    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack(flags=1))
    monkeypatch.setattr(finance_close, "narrate", lambda _p: None)
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(sys, "argv", ["run_finance_close_pack.py"])

    # First attempt: #founder-finance is dead.
    first = _FakeClient(fail_channels={script.FOUNDER_FINANCE_CHANNEL})
    monkeypatch.setitem(sys.modules, "slack_sdk", type(sys)("slack_sdk"))
    sys.modules["slack_sdk"].WebClient = lambda token: first
    script.main()

    # Retry: everything healthy. Only the previously-failed target should post.
    second = _FakeClient()
    sys.modules["slack_sdk"].WebClient = lambda token: second
    assert script.main() == 0
    assert [c for c, _ in second.posts] == [script.FOUNDER_FINANCE_CHANNEL]
    assert script._already_sent(script._iso_week()) is True


def test_legacy_scalar_dedup_record_is_treated_as_fully_sent(script, tmp_path, monkeypatch):
    """A pre-per-target state file must not cause a re-post of the whole pack."""
    path = tmp_path / "sent.json"
    path.write_text('{"last_week": "2026-W32"}', encoding="utf-8")
    monkeypatch.setattr(script, "_DEDUP_PATH", path)
    assert script._already_sent("2026-W32") is True


def test_narration_is_prefixed_when_present(script, monkeypatch, tmp_path, capsys):
    from cora import finance_close

    monkeypatch.setattr(finance_close, "build_pack", lambda **_k: _pack())
    monkeypatch.setattr(finance_close, "narrate", lambda _p: "Check F3 Energy first.")
    monkeypatch.setattr(script, "_DEDUP_PATH", tmp_path / "sent.json")
    monkeypatch.setattr(sys, "argv", ["x", "--dry-run"])
    script.main()
    assert "Check F3 Energy first." in capsys.readouterr().out
