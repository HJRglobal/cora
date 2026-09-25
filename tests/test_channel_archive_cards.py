"""Code #16 C1 -- the proposal card: pages under a block budget (A1), the Keep count
and the reversibility line on BOTH tiers (kickoff section 3; A33), store-truth text=
(A22), the buttons-off variant (A20), supersede/expiry (A14), and copy that never
trips the honesty rails (A27: caplog + enforce-mode equality on RENDERED output).
"""
from __future__ import annotations

import logging

import pytest

from _chanarch_fakes import DAY, HARRISON, NOW
from cora import slack_egress as se
from cora.channel_archive import cards
from cora.channel_archive import classify as cl
from cora.channel_archive import store as st

PID = "chanarch-cccccccccccc"


def row(cid, section="A", tier="T0", **kw):
    r = {"cid": cid, "section": section, "reason": kw.pop("reason", ""), "tier": tier,
         "lex": kw.pop("lex", False), "name_fp": "fp", "is_private": kw.pop("is_private", False),
         "last_person_days": kw.pop("last_person_days", 143), "history_complete": True,
         "scanned_older": 12, "bot_posts": kw.pop("bot_posts", 0),
         "bot_latest_days": kw.pop("bot_latest_days", None), "member_count": 4,
         "pins": 0, "bookmarks": 1, "created_days": 400, "canvas": False, "tabs": 0,
         "keep_count": kw.pop("keep_count", 0)}
    if not r["lex"]:
        r["name"] = kw.pop("name", f"fx-{cid.lower()}")
        r["entity"] = "FNDR (catch-all)"
    r.update(kw)
    return r


def staged(rows, *, pid=PID, ts=NOW, counts=None, blind=None, deliver=True, buttons=True,
           scanned=None):
    st.append_event("staged", proposal_id=pid, ts=ts, expires_ts=ts + 14 * DAY, rows=rows,
                    scanned=scanned if scanned is not None else len(rows) + 3,
                    counts=counts or {"exempt": {cl.X_PINNED: 2}, "active": 9, "unreadable": 0},
                    blind=blind)
    if deliver:
        for page, cids in enumerate(cards.paginate(rows), 1):
            st.append_event("delivered", proposal_id=pid, page=page, dm_channel="DH",
                            message_ts=f"{ts + page:.6f}", rendered_cids=cids, buttons=buttons, ts=ts)
    return st.fold(now=ts)


def texts(blocks):
    out = []
    for b in blocks:
        if b.get("type") == "section":
            out.append(b["text"]["text"])
        elif b.get("type") == "context":
            out.extend(e["text"] for e in b["elements"])
        elif b.get("type") == "actions":
            out.extend(e["text"]["text"] for e in b["elements"])
    return out


def actions(blocks):
    return [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]


class TestT0Card:
    def test_the_first_page_carries_every_required_line(self):
        f = staged([row("C0AAAAAAA1"), row("C0AAAAAAA2", keep_count=1),
                    row("C0BBBBBBB1", section="B", reason=cl.B_REGISTRY)])
        blocks, text = cards.render_page(f, PID, 1, now=NOW)
        body = "\n".join(texts(blocks))
        assert cards.HEADER_T0 in body and cards.REVERSIBLE in body and cards.BUTTONS_ONLY in body
        assert "kept x1" in body                                   # the Keep count
        assert "Not listed — pinned 2 · active 9" in body
        acts = actions(blocks)
        ids = [a["action_id"] for a in acts]
        assert ids.count(cards.ACTION_ROW) == 2 and ids.count(cards.ACTION_KEEP) == 3
        assert ids.count(cards.ACTION_OVERRIDE) == 1 and cards.ACTION_AGREED in ids
        allb = next(a for a in acts if a["action_id"] == cards.ACTION_ALL)
        assert allb["value"] == f"{PID}:p1:T0" and allb["text"]["text"] == "Mark all 2 shown to archive"
        assert all(a["value"].startswith(PID) for a in acts)
        assert text == "Dead-channel proposal (T0 — nothing archived): 3 listed, you marked 0, kept 0."
        assert len(blocks) <= cards.BLOCK_BUDGET

    def test_a_lex_row_renders_only_the_channel_token(self):
        f = staged([row("C0LEXLEX01", section="B", reason=cl.B_LEX, lex=True)])
        blocks, text = cards.render_page(f, PID, 1, now=NOW)
        body = "\n".join(texts(blocks))
        assert "<#C0LEXLEX01>  (LEX — name withheld)" in body and "fx-" not in body

    def test_section_b_rows_carry_their_reason_and_an_override_button(self):
        f = staged([row("C0BOTBOT01", section="B", reason=cl.B_BOT_TRAFFIC, bot_posts=3,
                        bot_latest_days=6)])
        blocks, _ = cards.render_page(f, PID, 1, now=NOW)
        body = "\n".join(texts(blocks))
        assert "Cora/app posts in the last 90 days: 3, latest 6 days ago" in body
        ov = [a for a in actions(blocks) if a["action_id"] == cards.ACTION_OVERRIDE]
        assert ov and ov[0]["text"]["text"] == "Mark to archive (this one)"
        assert not [a for a in actions(blocks) if a["action_id"] == cards.ACTION_ALL]  # B never in all

    def test_keep_capped_rows_lose_keep_and_say_so(self):
        f = staged([row("C0AAAAAAA1", keep_count=2)])
        blocks, _ = cards.render_page(f, PID, 1, now=NOW)
        assert "KEPT x2 (capped) — mark it to archive, or add it to the channel registry." in \
            "\n".join(texts(blocks))
        assert cards.ACTION_KEEP not in [a["action_id"] for a in actions(blocks)]

    def test_decided_rows_lose_their_buttons_failed_and_claimed_keep_them(self):
        rows = [row(f"C0AAAAAAA{i}") for i in range(1, 5)]
        staged(rows)
        st.append_event(st.AGREED, proposal_id=PID, cid="C0AAAAAAA1", by=HARRISON, ts=NOW)
        st.append_event(st.KEPT, proposal_id=PID, cid="C0AAAAAAA2", by=HARRISON, ts=NOW)
        st.append_event(st.FAILED, proposal_id=PID, cid="C0AAAAAAA3", code="not_in_channel", ts=NOW)
        st.append_event(st.CLAIMED, proposal_id=PID, cid="C0AAAAAAA4", ts=NOW)
        f = st.fold(now=NOW + 1)
        blocks, text = cards.render_page(f, PID, 1, now=NOW + 1)
        acted = {a["value"].split(":")[1] for a in actions(blocks) if a["action_id"] == cards.ACTION_ROW}
        assert acted == {"C0AAAAAAA3", "C0AAAAAAA4"}
        body = "\n".join(texts(blocks))
        assert "recorded as T1 promotion evidence; nothing archived" in body
        assert "Last attempt did not go through (not_in_channel)" in body and "In progress" in body
        assert "you marked 1, kept 1" in text
        allb = next(a for a in actions(blocks) if a["action_id"] == cards.ACTION_ALL)
        assert allb["text"]["text"] == "Mark all 1 shown to archive"        # open + failed, not claimed

    def test_card_agreed_replaces_the_button_with_a_line(self):
        staged([row("C0AAAAAAA1")])
        st.append_event("card_agreed", proposal_id=PID, by=HARRISON, ts=NOW)
        blocks, _ = cards.render_page(st.fold(now=NOW), PID, 1, now=NOW)
        assert cards.AGREED_LINE in "\n".join(texts(blocks))
        assert cards.ACTION_AGREED not in [a["action_id"] for a in actions(blocks)]


class TestPages:
    def test_45_a_and_10_b_rows_each_act_only_on_their_own_message(self):
        rows = [row(f"C0A{i:07d}") for i in range(45)] + \
               [row(f"C0B{i:07d}", section="B", reason=cl.B_REGISTRY) for i in range(10)]
        f = staged(rows)
        pages = cards.paginate(rows)
        assert len(pages) == 3
        seen_row_buttons: set[str] = set()
        for n in range(1, 4):
            blocks, _ = cards.render_page(f, PID, n, now=NOW)
            assert len(blocks) <= cards.BLOCK_BUDGET
            page_cids = set(pages[n - 1])
            for a in actions(blocks):
                if a["action_id"] in (cards.ACTION_ROW, cards.ACTION_OVERRIDE):
                    cid = a["value"].split(":")[1]
                    assert cid in page_cids
                    seen_row_buttons.add(cid)
                if a["action_id"] == cards.ACTION_ALL:
                    assert a["value"] == f"{PID}:p{n}:T0"
            covered = set(cards.undecided_a_on_page(f.proposals[PID], n))
            assert covered <= page_cids and all(c.startswith("C0A") for c in covered)
        assert seen_row_buttons == {r["cid"] for r in rows}     # every actionable row has buttons

    def test_continuation_pages_say_so(self):
        rows = [row(f"C0A{i:07d}") for i in range(25)]
        f = staged(rows)
        blocks, _ = cards.render_page(f, PID, 2, now=NOW)
        assert "part 2 of 2" in "\n".join(texts(blocks))


class TestVariants:
    def test_buttons_off_renders_no_actions_and_says_why(self):
        f = staged([row("C0AAAAAAA1")], buttons=False)
        blocks, _ = cards.render_page(f, PID, 1, now=NOW)
        assert not actions(blocks) and cards.BUTTONS_OFF in "\n".join(texts(blocks))

    @pytest.mark.parametrize("cause", ["registry_unreadable", "policy_unreadable",
                                       "list_incomplete", "bot_id_unknown", "store_unreadable"])
    def test_a_blind_scan_never_renders_the_clean_sentence(self, cause):
        f = staged([], blind=cause)
        blocks, text = cards.render_page(f, PID, 1, now=NOW)
        body = "\n".join(texts(blocks))
        assert cards.BLIND_CAUSES[cause] in body and "nothing was archived" in body
        assert "No channel I could read" not in body and cause in text

    def test_zero_candidates_is_clean_only_when_nothing_was_unreadable(self):
        f = staged([], counts={"exempt": {}, "active": 20, "unreadable": 0}, scanned=20)
        body = "\n".join(texts(cards.render_page(f, PID, 1, now=NOW)[0]))
        assert "No channel I could read has gone 90+ days" in body
        st.store_path().unlink()
        f2 = staged([], counts={"exempt": {}, "active": 17, "unreadable": 3}, scanned=20)
        body2 = "\n".join(texts(cards.render_page(f2, PID, 1, now=NOW)[0]))
        assert "No candidates among the 17 channels I could read; 3 could not be read" in body2
        assert "No channel I could read has gone" not in body2

    def test_superseded_and_expired_cards_render_actions_free(self):
        staged([row("C0AAAAAAA1")])
        staged([row("C0AAAAAAA2")], pid="chanarch-dddddddddddd", ts=NOW + 100)
        f = st.fold(now=NOW + 101)
        blocks, _ = cards.render_page(f, PID, 1, now=NOW + 101)
        assert not actions(blocks) and "Superseded by the" in "\n".join(texts(blocks))
        blocks2, _ = cards.render_page(f, "chanarch-dddddddddddd", 1, now=NOW + 100 + 15 * DAY)
        assert not actions(blocks2) and "Expired" in "\n".join(texts(blocks2))


class TestT1Card:
    def test_t1_copy_and_buttons(self, monkeypatch):
        f = staged([row("C0AAAAAAA1", tier="T1"), row("C0BBBBBBB1", section="B",
                                                       reason=cl.B_LEX, lex=True, tier="T1")])
        blocks, text = cards.render_page(f, PID, 1, now=NOW)
        body = "\n".join(texts(blocks))
        assert cards.HEADER_T1 in body and cards.REVERSIBLE in body
        labels = {a["text"]["text"] for a in actions(blocks)}
        assert {"Archive", "Archive (override)", "Archive all 1 shown", "Keep"} <= labels
        assert text.startswith("Dead-channel proposal (T1):")

    def test_a_demotion_rerenders_t1_rows_t0_equivalent(self, monkeypatch, tmp_path):
        f = staged([row("C0AAAAAAA1", tier="T1")])
        from cora.channel_archive import policy
        policy.demotion_path().write_text('{"demoted": true}', encoding="utf-8")
        blocks, text = cards.render_page(f, PID, 1, now=NOW)
        labels = {a["text"]["text"] for a in actions(blocks)}
        assert "Archive" not in labels and "Mark to archive" in labels
        body = "\n".join(texts(blocks))
        assert cards.HEADER_DEMOTED in body and cards.HEADER_T0 not in body
        assert text.startswith("Dead-channel proposal (lane demoted")


class TestStoreTruthAfterADemotion:
    """c1-intents-copy#3 / c1-state-machine#1 (A22): after a demotion, a card that
    already archived rows must never re-render text= / header / continuation as
    'nothing archived' -- the history readers hand text= to the model as Cora's words."""

    def _card_with_archives(self, n=25):
        rows = [row(f"C0A{i:07d}", tier="T1") for i in range(n)]
        staged(rows)
        st.append_event(st.ARCHIVED, proposal_id=PID, cid="C0A0000000", by=HARRISON, ts=NOW + 5)
        st.append_event(st.UNKNOWN, proposal_id=PID, cid="C0A0000001", by=HARRISON, ts=NOW + 6)
        return rows

    @pytest.mark.parametrize("how", ["demoted_now", "cleared_after_the_card"])
    def test_archived_and_unknown_counts_survive_a_demotion(self, how):
        from cora.channel_archive import policy
        self._card_with_archives()
        if how == "demoted_now":
            policy.demotion_path().write_text('{"since": "x"}', encoding="utf-8")
        else:
            st.append_ledger("acknowledged", channel_id="C0ROGUE", archive_ts="1.1", by=HARRISON,
                             demoted_since="2099-01-01T00:00:00-07:00")
        f = st.fold(now=NOW + 10)
        blocks, text = cards.render_page(f, PID, 1, now=NOW + 10)
        assert "nothing archived" not in text.lower(), text
        assert "archived 1" in text and "outcome unknown 1" in text, text
        body = "\n".join(texts(blocks))
        assert cards.HEADER_DEMOTED in body and cards.HEADER_T0 not in body
        assert "nothing will be archived" not in body.lower()
        assert "_Archived " in body                                 # the row still says so
        blocks2, text2 = cards.render_page(f, PID, 2, now=NOW + 10)
        body2 = "\n".join(texts(blocks2))
        assert "Nothing is archived at T0" not in body2 and "demoted" in body2
        assert text2 == text

    def test_a_plain_t0_card_keeps_its_nothing_archived_line(self):
        staged([row("C0AAAAAAA1"), row("C0AAAAAAA2")])
        blocks, text = cards.render_page(st.fold(now=NOW), PID, 1, now=NOW)
        assert text == "Dead-channel proposal (T0 — nothing archived): 2 listed, you marked 0, kept 0."
        assert cards.HEADER_T0 in "\n".join(texts(blocks))


def _all_rendered_strings():
    """Every string the lane can show, RENDERED with realistic data (A27)."""
    out: list[str] = []
    rows = [row("C0AAAAAAA1", keep_count=1), row("C0AAAAAAA2", keep_count=2),
            row("C0BBBBBBB1", section="B", reason=cl.B_REGISTRY, bot_posts=40, bot_latest_days=0),
            row("C0BBBBBBB2", section="B", reason=cl.B_BOT_TRAFFIC, bot_posts=2, bot_latest_days=4),
            row("C0BBBBBBB3", section="B", reason=cl.B_UNARCHIVED_BEFORE,
                unarchived_by="UPERSON01", unarchived_at=NOW - 100 * DAY),
            row("C0LEXLEX01", section="B", reason=cl.B_LEX, lex=True)]
    for tier in ("T0", "T1"):
        for r in rows:
            r["tier"] = tier
        pid = f"chanarch-{'e' if tier == 'T0' else 'f'}ccccccccccc"
        staged(rows, pid=pid, ts=NOW + (0 if tier == "T0" else 500))
        for ev, cid, extra in ((st.AGREED, "C0AAAAAAA1", {}), (st.KEPT, "C0AAAAAAA2", {}),
                               (st.ARCHIVED, "C0BBBBBBB1", {}), (st.UNKNOWN, "C0BBBBBBB2", {}),
                               (st.STALE, "C0BBBBBBB3", {"code": "a person posted since the card"}),
                               (st.FAILED, "C0LEXLEX01", {"code": "restricted_action"})):
            st.append_event(ev, proposal_id=pid, cid=cid, by=HARRISON, ts=NOW + 600, **extra)
        f = st.fold(now=NOW + 700)
        blocks, text = cards.render_page(f, pid, 1, now=NOW + 700)
        out.extend(texts(blocks))
        out.append(text)
    # the demoted re-render of the T1 card (archived + unknown rows present, A12/A22)
    from cora.channel_archive import policy
    policy.demotion_path().write_text('{"since": "x"}', encoding="utf-8")
    blocks, text = cards.render_page(st.fold(now=NOW + 700), "chanarch-fccccccccccc", 1, now=NOW + 700)
    assert text.startswith("Dead-channel proposal (lane demoted")
    out.extend(texts(blocks))
    out.append(text)
    out.append(cards.CONTINUED_DEMOTED)
    policy.demotion_path().unlink()
    out.append(cards.notice_text(143, HARRISON))
    out.append(cards.CORRECTION_TEXT)
    for cause in cards.BLIND_CAUSES:
        f = staged([], blind=cause, pid="chanarch-000000000000", ts=NOW + 900)
        b, t = cards.render_page(f, "chanarch-000000000000", 1, now=NOW + 900)
        out.extend(texts(b))
        out.append(t)
        st.store_path().unlink()
    return out


def test_every_rendered_string_passes_both_rails_in_observe_and_enforce(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=se.__name__)
    strings = _all_rendered_strings()
    assert len(strings) > 30
    for s in strings:
        assert se.screen_phantom_write_claims(s, tool_use_count=0) == s, s
        assert se.sanitize_text(s) == s, s
    assert not [r for r in caplog.records if se.PHANTOM_LOG_KEY in r.getMessage()]
    monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
    for s in strings:
        assert se.screen_phantom_write_claims(s, tool_use_count=0) == s, s


def test_past_tense_archived_appears_only_on_an_archived_row():
    strings = _all_rendered_strings()
    for s in strings:
        if "Archived " in s and "Archiving" not in s:
            assert "ledger row written" in s or s.startswith("Dead-channel"), s


def test_the_notice_and_correction_copy():
    n = cards.notice_text(143, HARRISON)
    assert n.startswith(":package: Archiving for inactivity — 143 days since the last message from a person.")
    assert "anyone here can unarchive it from the channel settings" in n
    assert f"<@{HARRISON}>" in n and "Cora" in n
    assert "asking Cora" not in n            # no phantom unarchive capability (premise 14)
    assert cards.CORRECTION_TEXT == "Archive did not go through — the channel stays open."


def test_a_registry_row_that_cora_still_posts_to_says_so():
    """Live-data finding (2026-09-25 preview): #cora-health is registry-exempt AND Cora's
    own daily destination; its row said only "in the channel registry". An override tap
    must not hide that archiving would stop Cora's posts landing there."""
    f = staged([row("C0BBBBBBB1", section="B", reason=cl.B_REGISTRY, bot_posts=40, bot_latest_days=0,
                    last_person_days=None),
                row("C0BBBBBBB2", section="B", reason=cl.B_REGISTRY)])
    blocks, _text = cards.render_page(f, PID, 1, now=NOW)
    body = "\n".join(texts(blocks))
    first, second = body.split("C0BBBBBBB2", 1)
    assert "Cora/app still posts here: 40 in the last 90 days, latest 0 days ago" in first
    assert "would stop those posts landing" in first
    assert "still posts here" not in second
    # the bot-traffic-primary row keeps its own single line (no doubled sentence)
    f2 = staged([row("C0BBBBBBB3", section="B", reason=cl.B_BOT_TRAFFIC, bot_posts=2,
                     bot_latest_days=4)], pid="chanarch-dddddddddddd")
    body2 = "\n".join(texts(cards.render_page(f2, "chanarch-dddddddddddd", 1, now=NOW)[0]))
    assert body2.count("Cora/app posts in the last 90 days: 2") == 1
    assert "still posts here" not in body2
