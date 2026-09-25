"""Code #16 C1 -- deliver_proposal (the ONE function behind the DM ask and the monthly
script), the per-row tier gate (A12, D-326 in code) and the pytest client belt (A26).
"""
from __future__ import annotations

import json

import pytest

from _chanarch_fakes import (BOT_UID, HARRISON, NOW, FakeSlack, api_error, chan, msg, no_sleep)
from cora.channel_archive import clients, deliver, gates, policy
from cora.channel_archive import store as st


def world(**kw):
    chans = [chan("C0CANDID01", "fx-dead-one"), chan("C0CANDID02", "fx-dead-two", private=True),
             chan("C0ACTIVE01", "fx-alive")]
    hist = {"C0CANDID01": [msg(200)], "C0CANDID02": [msg(300)], "C0ACTIVE01": [msg(1)]}
    return FakeSlack(channels=chans, history=hist, **kw)


@pytest.fixture
def fake(monkeypatch):
    f = world()
    monkeypatch.setattr(clients, "read_client_factory", lambda: f)
    monkeypatch.setattr(clients, "write_client_factory", lambda: f)
    return f


def t1_registry(monkeypatch, tmp_path, tier="T1"):
    p = tmp_path / "ladder.yaml"
    p.write_text(f"version: 1\nlanes:\n  - lane: slack-channel-archive\n    tier: {tier}\n", encoding="utf-8")
    monkeypatch.setenv("CORA_LADDER_REGISTRY_PATH", str(p))


class TestDeliver:
    def test_a_delivered_card_is_staged_before_it_is_posted(self, fake):
        seen_staged: list[bool] = []

        def _post(kw):
            f = st.fold(now=NOW)
            seen_staged.append(bool(f.order))
        fake.post_behaviour = _post
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["delivered"] and out["reason"] == "delivered" and out["pages"] == 1
        assert out["candidate_ids"] == ["C0CANDID01", "C0CANDID02"]
        assert seen_staged == [True]
        assert ("conversations_open", {"users": [HARRISON]}) in fake.calls
        f = st.fold(now=NOW)
        p = f.proposals[out["proposal_id"]]
        assert p.delivered and p.pages[1]["rendered_cids"] == ["C0CANDID02", "C0CANDID01"] \
            or p.pages[1]["rendered_cids"] == ["C0CANDID01", "C0CANDID02"]
        assert p.pages[1]["dm_channel"] == "DHARRISON1" and p.trigger == "ask"
        assert all(r["tier"] == "T0" for r in p.rows) and p.tier_at_stage == "T0"
        assert [e["event"] for e in st.read_events()][:2] == ["scan_started", "staged"]
        post = fake.posts[-1]
        assert post["channel"] == "DHARRISON1" and post["unfurl_links"] is False
        assert "T0 — nothing archived" in post["text"]
        assert not st.scan_lock_path().exists()                     # released

    def test_nothing_is_archived_by_a_delivery(self, fake):
        deliver.deliver_proposal(trigger="monthly", now=NOW, sleep=no_sleep)
        assert "conversations_archive" not in fake.method_names()
        assert not [p for p in fake.posts if p["channel"].startswith("C")]   # no channel posts

    def test_eval_mode_makes_no_slack_call(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        monkeypatch.setattr(clients, "read_client_factory", lambda: pytest.fail("client built"))
        assert deliver.deliver_proposal(trigger="ask", now=NOW)["reason"] == "eval_mode"
        assert st.read_events() == []

    def test_off_returns_before_any_read(self, monkeypatch):
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "off")
        monkeypatch.setattr(clients, "read_client_factory", lambda: pytest.fail("client built"))
        assert deliver.deliver_proposal(trigger="ask", now=NOW)["reason"] == "off"

    def test_dry_run_reads_and_writes_nothing(self, fake):
        out = deliver.deliver_proposal(trigger="manual", now=NOW, sleep=no_sleep, dry_run=True)
        assert out["reason"] == "dry_run" and out["candidate_ids"] == ["C0CANDID01", "C0CANDID02"]
        assert not st.store_path().exists() and not st.scan_lock_path().exists()
        assert not fake.posts

    def test_a_running_scan_refuses_a_second(self, fake):
        tok = st.acquire_scan_lock(now=NOW)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["reason"] == "scan_running" and not out["delivered"]
        assert st.read_events() == [] and not fake.posts
        st.release_scan_lock(tok)

    def test_a_post_failure_is_recorded_and_not_reported_delivered(self, fake):
        fake.post_behaviour = lambda kw: api_error("channel_not_found")
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert not out["delivered"] and out["reason"] == "post_failed:channel_not_found"
        assert [e for e in st.read_events() if e["event"] == "delivery_failed"]
        assert not st.fold(now=NOW).proposals[out["proposal_id"]].delivered

    def test_a_blind_scan_still_stages_and_delivers_its_honest_card(self, fake, monkeypatch):
        monkeypatch.setattr(deliver.reg, "load_deny_policy", lambda *a, **k: None)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["blind"] == "policy_unreadable" and out["delivered"] and out["rows"] == 0
        assert "could not complete" in fake.posts[-1]["text"]
        assert "conversations_history" not in fake.method_names()

    def test_the_registry_floor_comes_from_the_last_good_parse(self, fake):
        st.append_event("staged", proposal_id="chanarch-111111111111", ts=NOW - 100,
                        rows=[], registry_count=500)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["blind"] == "registry_unreadable"      # 108 fixture ids < 0.9 * 500

    def test_a_many_row_scan_posts_one_message_per_page(self, monkeypatch):
        chans = [chan(f"C0DEAD{i:04d}", f"fx-dead-{i}") for i in range(45)]
        f = FakeSlack(channels=chans, history={c["id"]: [msg(200)] for c in chans})
        monkeypatch.setattr(clients, "read_client_factory", lambda: f)
        monkeypatch.setattr(clients, "write_client_factory", lambda: f)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["pages"] == 3 and len(f.posts) == 3
        pages = st.fold(now=NOW).proposals[out["proposal_id"]].pages
        assert sum(len(p["rendered_cids"]) for p in pages.values()) == 45
        for p in f.posts:
            assert len(p["blocks"]) <= 48


class TestPartialDelivery:
    """c1-state-machine#6: page 1 posted, a later page failed -- Harrison is told which
    parts are missing, the result says it was only partly delivered, and the partly
    delivered card does not supersede (and so strand) an older, complete card."""

    def _many(self, monkeypatch, n=45, fail_page=2):
        chans = [chan(f"C0DEAD{i:04d}", f"fx-dead-{i}") for i in range(n)]
        f = FakeSlack(channels=chans, history={c["id"]: [msg(200)] for c in chans})
        cards_seen = {"n": 0}

        def _post(kw):
            if kw.get("blocks"):
                cards_seen["n"] += 1
                if cards_seen["n"] == fail_page:
                    return api_error("ratelimited")
            return None
        f.post_behaviour = _post
        monkeypatch.setattr(clients, "read_client_factory", lambda: f)
        monkeypatch.setattr(clients, "write_client_factory", lambda: f)
        return f

    def test_a_missing_page_is_named_in_the_dm_and_the_result_says_partial(self, monkeypatch):
        f = self._many(monkeypatch)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["pages"] == 1 and out["pages_expected"] == 3 and out["partial"] is True
        assert out["reason"] == "post_failed:ratelimited"
        lines = [p["text"] for p in f.posts if not p.get("blocks")]
        assert len(lines) == 1 and "parts 2 and 3 of 3" in lines[0], lines
        assert "nothing was archived" in lines[0] and f.posts[-1]["channel"] == "DHARRISON1"
        from cora import slack_egress as se                 # A27: both honesty rails
        assert se.screen_phantom_write_claims(lines[0], tool_use_count=0) == lines[0]
        assert se.sanitize_text(lines[0]) == lines[0]
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        assert se.screen_phantom_write_claims(lines[0], tool_use_count=0) == lines[0]

    def test_a_partly_delivered_card_does_not_supersede_an_older_complete_one(self, monkeypatch):
        st.append_event("staged", proposal_id="chanarch-000000000001", ts=NOW - 100,
                        expires_ts=NOW + 14 * 86400, rows=[{"cid": "C0OLDROW01", "section": "A"}],
                        n_pages=1)
        st.append_event("delivered", proposal_id="chanarch-000000000001", page=1,
                        dm_channel="DHARRISON1", message_ts="1.1", rendered_cids=["C0OLDROW01"],
                        buttons=True, ts=NOW - 100)
        self._many(monkeypatch)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        f = st.fold(now=NOW + 1)
        assert f.superseded_by("chanarch-000000000001") is None
        assert f.is_live("chanarch-000000000001", NOW + 1)
        assert not f.proposals[out["proposal_id"]].fully_delivered

    def test_a_complete_delivery_still_supersedes(self, monkeypatch):
        st.append_event("staged", proposal_id="chanarch-000000000001", ts=NOW - 100,
                        expires_ts=NOW + 14 * 86400, rows=[{"cid": "C0OLDROW01", "section": "A"}])
        st.append_event("delivered", proposal_id="chanarch-000000000001", page=1,
                        dm_channel="DHARRISON1", message_ts="1.1", rendered_cids=["C0OLDROW01"],
                        buttons=True, ts=NOW - 100)
        self._many(monkeypatch, fail_page=99)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["pages"] == 3 and not out.get("partial")
        f = st.fold(now=NOW + 1)
        assert f.superseded_by("chanarch-000000000001").proposal_id == out["proposal_id"]


class TestTierGate:
    def test_default_registry_t0_stages_every_row_t0_even_with_act(self, fake, monkeypatch):
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")
        fake.scopes = ["channels:manage", "groups:write"]
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        rows = st.fold(now=NOW).proposals[out["proposal_id"]].rows
        assert {r["tier"] for r in rows} == {"T0"}          # the real registry says T0

    def test_registry_t1_plus_act_plus_scopes_stages_t1_per_channel_type(self, fake, monkeypatch, tmp_path):
        t1_registry(monkeypatch, tmp_path)
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")
        fake.scopes = ["channels:manage", "chat:write"]           # groups:write missing
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        rows = {r["cid"]: r for r in st.fold(now=NOW).proposals[out["proposal_id"]].rows}
        assert rows["C0CANDID01"]["tier"] == "T1" and rows["C0CANDID02"]["tier"] == "T0"

    def test_unknown_scopes_or_propose_mode_or_demotion_stage_t0(self, fake, monkeypatch, tmp_path):
        t1_registry(monkeypatch, tmp_path)
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")
        fake.scopes = None                                          # UNKNOWN grant
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert {r["tier"] for r in st.fold(now=NOW).proposals[out["proposal_id"]].rows} == {"T0"}
        policy.demotion_path().write_text("{}", encoding="utf-8")
        fake.scopes = ["channels:manage", "groups:write"]
        out2 = deliver.deliver_proposal(trigger="ask", now=NOW + 10, sleep=no_sleep)
        assert {r["tier"] for r in st.fold(now=NOW + 10).proposals[out2["proposal_id"]].rows} == {"T0"}

    def test_stage_tier_truth_table(self):
        for reg_ok in (True, False, None):
            for acting in ("T0", "T1"):
                for scopes in (None, frozenset({"channels:manage"}), frozenset()):
                    want = "T1" if (reg_ok is True and acting == "T1" and scopes and True) else "T0"
                    assert gates.stage_tier(False, scopes=scopes, registry_t1=reg_ok,
                                            acting=acting) == want

    def test_registry_allows_t1_reads_the_live_row(self, monkeypatch, tmp_path):
        assert gates.registry_allows_t1() is False                  # the committed row is T0
        t1_registry(monkeypatch, tmp_path)
        assert gates.registry_allows_t1() is True
        monkeypatch.setenv("CORA_LADDER_REGISTRY_PATH", str(tmp_path / "missing.yaml"))
        assert gates.registry_allows_t1() is None


class TestClientBelt:
    def test_default_factories_refuse_under_pytest(self):
        with pytest.raises(clients.LiveClientRefused):
            clients._build_read()
        with pytest.raises(clients.LiveClientRefused):
            clients._build_write()

    def test_write_client_has_no_retry_handlers_and_reads_retry_on_429(self, monkeypatch):
        """Built with the belt lifted for ONE call: never used to talk to Slack."""
        monkeypatch.setattr(clients, "_under_pytest", lambda: False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-" + "test-not-a-real-token")
        w = clients._build_write()
        r = clients._build_read()
        assert list(w.retry_handlers) == []
        names = {type(h).__name__ for h in r.retry_handlers}
        assert "RateLimitErrorRetryHandler" in names
