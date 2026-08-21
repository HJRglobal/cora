"""HOTFIX repro: the TYPED confirm/cancel path went dark after the v2 merge
while button taps kept working (live 2026-08-08 16:00-16:12 MST).

Live evidence this file encodes:
  * three staged-write pendings minted 16:01:05-08 in #cora-build; five typed
    turns against them (three "[QA] cancel", a bare "cancel", a bare "confirm")
    ALL fell through to the model with NO interceptor line in the log;
  * the stashes were provably alive -- button taps at 16:08 claimed and
    executed all three, +7 minutes after mint, with no restart in between;
  * a DM remember preview at 16:10:56 answered a bare "yes" 45 SECONDS later
    with "The preview expired", against a 600s TTL.

Written BEFORE the fix, per the hotfix kickoff: the existing 10,448 tests pass
with this bug, so anything that does not fail here first is not the repro.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

from cora import confirm_cards as cc  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER = "U0B2RM2JYJ1"
CHANNEL = "cora-build"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    for store in (td._PENDING_ASANA_WRITES, td._PENDING_SHOPIFY_WRITES,
                  td._PENDING_CALENDAR_WRITES, td._PENDING_REMEMBER,
                  td._PENDING_FORGET_NOTE):
        store.clear()
    yield
    for store in (td._PENDING_ASANA_WRITES, td._PENDING_SHOPIFY_WRITES,
                  td._PENDING_CALENDAR_WRITES, td._PENDING_REMEMBER,
                  td._PENDING_FORGET_NOTE):
        store.clear()
    cc.reset_cards_for_tests()


def _mint_asana_create(ts: float) -> str:
    sid = cc.mint_stash_id("asana", USER, CHANNEL)
    td._store_pending_asana_write(USER, CHANNEL, {
        "action": "create", "title": "[QA] v2 smoke task",
        "ts": ts, "stash_id": sid,
    })
    return sid


def _mint_shopify(ts: float) -> str:
    sid = cc.mint_stash_id("shopify", USER, CHANNEL)
    td._store_pending_shopify_write(USER, CHANNEL, {
        "sku": "F3-ORIG", "delta": -1, "quantity": 129,
        "ts": ts, "stash_id": sid,
    })
    return sid


def _mint_calendar(ts: float) -> str:
    sid = cc.mint_stash_id("calendar", USER, CHANNEL)
    td._store_pending_calendar_write(USER, CHANNEL, {
        "action": "create", "summary": "[QA] v2 smoke event",
        "ts": ts, "stash_id": sid,
    })
    return sid


# ── the exact live shape: a single pending, typed confirm/cancel at +45s ───


class TestTypedConfirmAtRealisticAges:
    """A stash inside its 600s TTL must be claimable by the TYPED path exactly
    as it is by a tap. These use ONE pending so nothing else can arbitrate."""

    @pytest.mark.parametrize("age", [1.0, 45.0, 420.0])
    def test_typed_confirm_executes_an_asana_create(self, age):
        now = time.time()
        _mint_asana_create(now - age)
        with patch.object(td, "_run_confirm_execute", return_value="Created.") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
                message="confirm", turn_started_at=now,
            )
        ex.assert_called_once()
        assert reply == "Created.", f"typed confirm dark at age {age}s"

    @pytest.mark.parametrize("age", [1.0, 45.0, 420.0])
    def test_typed_cancel_cancels_an_asana_create(self, age):
        now = time.time()
        sid = _mint_asana_create(now - age)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="cancel", turn_started_at=now,
        )
        assert reply is not None, f"typed cancel dark at age {age}s"
        assert not td.stash_is_live(sid), "cancel must consume the pending"

    def test_typed_confirm_executes_a_shopify_set(self):
        now = time.time()
        _mint_shopify(now - 45)
        with patch.object(td, "_run_confirm_execute", return_value="Set.") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
                message="confirm", turn_started_at=now,
            )
        ex.assert_called_once()
        assert reply == "Set."

    def test_a_qa_prefixed_cancel_still_cancels(self):
        """The live smoke typed "[QA] cancel" three times."""
        now = time.time()
        sid = _mint_asana_create(now - 45)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="[QA] cancel", turn_started_at=now,
        )
        assert reply is not None, "a [QA]-tagged cancel must still reach the interceptor"
        assert not td.stash_is_live(sid)


# ── the exact live shape: THREE kinds staged, then typed confirm/cancel ────


class TestThreeKindTrioThenTypedTurn:
    """The live smoke staged asana + shopify + calendar seconds apart, then
    typed against them. Calendar was freshest and DEFERS by design -- but the
    turn must not therefore be a silent no-op for the OTHER two pendings."""

    def _stage_trio(self, base: float) -> dict[str, str]:
        return {
            "shopify": _mint_shopify(base),
            "asana": _mint_asana_create(base + 3),
            "calendar": _mint_calendar(base + 3),
        }

    def test_a_typed_cancel_against_the_trio_is_not_a_silent_no_op(self):
        """Whatever the arbitration decides, a user who typed "cancel" against
        live staged writes must get a deterministic answer OR have the turn
        carry the pending state -- never a bare fall-through that lets the
        model narrate "nothing is staged" while three writes sit armed."""
        now = time.time()
        self._stage_trio(now - 120)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="cancel", turn_started_at=now,
        )
        assert reply is not None, (
            "typed cancel against three live pendings fell through silently -- "
            "the live 16:03/16:05 regression")

    def test_a_typed_confirm_on_a_defer_kind_hands_the_model_the_pending_state(self):
        """A bare "confirm" when a DEFER-kind is freshest correctly returns None
        (this function has no executor for those kinds, so the model must reach
        the kind's own tool). What was wrong is what happened NEXT: the model had
        no idea anything was staged and narrated "nothing is staged" while three
        writes sat armed (live 16:07:48). Deferring is right; deferring blind is
        the defect -- so the turn must carry the pending state."""
        now = time.time()
        self._stage_trio(now - 400)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="confirm", turn_started_at=now,
        )
        assert reply is None, "an affirm on a defer-kind must still reach the tool"
        note = td.describe_live_pendings(USER, CHANNEL)
        assert note, "the deferred turn carries no pending-state context"
        assert "NEVER tell them nothing is staged" in note

    def test_the_pendings_stay_claimable_by_a_tap_afterwards(self):
        """Live: the taps at +7min worked. A typed turn that decides not to act
        must leave every stash intact for the button."""
        now = time.time()
        ids = self._stage_trio(now - 420)
        td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="what's the weather", turn_started_at=now,
        )
        for kind, sid in ids.items():
            if kind == "asana":
                continue  # a create is legitimately abandoned by a fresher write
            assert td.stash_is_live(sid), f"{kind} stash died on an unrelated turn"


# ── the DM remember regression: "expired" at 45 seconds ───────────────────


class TestPendingStateVisibility:
    """S2 / cq-24cc6ac4bbc8: a deferred turn must hand the model the fact that
    writes are staged, without handing it any authority or any payload."""

    def test_nothing_staged_yields_no_note(self):
        assert td.describe_live_pendings(USER, CHANNEL) == ""

    def test_it_names_every_live_kind_newest_first(self):
        now = time.time()
        _mint_shopify(now - 30)
        _mint_asana_create(now - 10)
        note = td.describe_live_pendings(USER, CHANNEL)
        assert "an Asana task change" in note and "an inventory change" in note
        assert note.index("newest first: an Asana task change") > 0

    def test_it_never_leaks_a_payload(self):
        """Factual kind names only -- no task title, SKU, quantity or note text."""
        now = time.time()
        _mint_asana_create(now - 10)
        _mint_shopify(now - 20)
        note = td.describe_live_pendings(USER, CHANNEL)
        for secret in ("v2 smoke task", "F3-ORIG", "129", "[QA]"):
            assert secret not in note, f"payload {secret!r} leaked into model context"

    def test_an_expired_pending_is_not_advertised(self):
        _mint_asana_create(time.time() - (td._ASANA_PENDING_TTL_SECONDS + 5))
        assert td.describe_live_pendings(USER, CHANNEL) == ""

    def test_it_is_scoped_to_the_asker_and_channel(self):
        _mint_asana_create(time.time() - 10)
        assert td.describe_live_pendings("U0SOMEONE_ELSE", CHANNEL) == ""
        assert td.describe_live_pendings(USER, "another-channel") == ""

    def test_the_runtime_context_carries_it(self):
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "describe_live_pendings" in src

    def test_the_probe_runs_AFTER_the_interceptor(self):
        """The interceptor mutates this state while still returning None (it
        abandons a stale destructive Asana pending on a superseding write and on
        a question), so a probe taken earlier told the model a delete was staged
        that had just been abandoned."""
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        interceptor_at = src.index("try_confirm_pending_write(")
        probe_at = src.index("describe_live_pendings(")
        assert probe_at > interceptor_at, (
            "the pending-state probe snapshots state the interceptor then mutates")

    def test_the_probe_is_turn_scoped(self):
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        call = src[src.index("describe_live_pendings("):][:200]
        assert "turn_started_at=_turn_started_at" in call, (
            "the note must apply the same concurrent-turn exclusion as the arbitration")

    def test_a_concurrent_turns_pending_is_never_advertised(self):
        """D-051 lens-1 HIGH: advertising it handed the model an imperative to
        confirm a write for a preview the user has not yet been shown, and the
        per-kind claims are keyed on (user, channel) with no turn check."""
        now = time.time()
        _mint_shopify(now + 30)  # a sibling turn's mint, comfortably past tolerance
        assert td.describe_live_pendings(USER, CHANNEL, turn_started_at=now) == ""

    def test_this_turns_own_pending_is_still_advertised(self):
        now = time.time()
        _mint_shopify(now - 30)
        assert td.describe_live_pendings(USER, CHANNEL, turn_started_at=now) != ""

    def test_a_pending_bearing_turn_is_never_semantically_cached(self):
        """The semantic cache is entity-keyed, not user-keyed. A reply generated
        with pending-state context can name THIS person's staged writes, so
        storing it would serve one person's staged-write state to the next asker
        in the same entity -- the same exclusion unstripped_personal gets."""
        import ast
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and node.targets
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "cache_storable"
                    and isinstance(node.value, ast.BoolOp)):
                names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                if "pending_note" in names:
                    found = True
        assert found, "cache_storable does not exclude pending-bearing turns"

    def test_the_pending_line_rides_the_uncached_context_block(self):
        """It must NOT ride `cached_context` (the prompt-cache block 2), which
        is shared across turns and would bake one turn's state into the cache."""
        src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        assert "pending_block" not in src.split("cached_context=static_text")[0][-2000:], \
            "pending_block appears near the cached-context assembly"
        assert "runtime_context = (" in src


class TestDeferredCancelClaimsExactlyOnce:
    """The typed cancel added for defer-kinds must be a real atomic claim."""

    @pytest.mark.parametrize("mint,kind", [
        (_mint_calendar, "calendar"),
    ])
    def test_cancel_consumes_the_pending(self, mint, kind):
        now = time.time()
        sid = mint(now - 45)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="cancel", turn_started_at=now,
        )
        assert reply is not None
        assert not td.stash_is_live(sid)

    def test_a_second_cancel_does_not_claim_again(self):
        now = time.time()
        _mint_calendar(now - 45)
        first = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="cancel", turn_started_at=now)
        second = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="cancel", turn_started_at=now)
        assert first is not None
        assert second is None, "a second cancel must not claim a cancellation twice"

    def test_a_cancelled_stash_reads_already_handled_to_a_later_tap(self):
        now = time.time()
        sid = _mint_calendar(now - 45)
        td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="cancel", turn_started_at=now)
        result = td.resolve_and_claim_stash(sid, USER, "confirm")
        assert result["outcome"] == "already_handled", (
            "the card must not re-offer a pending a typed cancel removed")

    def test_a_racing_overwrite_is_not_silently_destroyed(self):
        """D-051 lens-1: the first cut popped whatever was in the slot, so a
        sibling turn that overwrote it between peek and pop had ITS never-seen
        preview destroyed while the user was told theirs was cancelled."""
        now = time.time()
        mine = _mint_calendar(now - 45)
        theirs = _mint_calendar(now - 1)  # overwrites the same (user, channel) slot
        assert mine != theirs
        claimed = td._claim_deferred_kind("calendar", USER, CHANNEL, mine)
        assert claimed is None, "claimed a stash id that no longer occupies the slot"
        assert td.stash_is_live(theirs), "destroyed the sibling turn's preview"

    def test_an_expired_pending_is_not_reported_as_cancelled(self):
        now = time.time()
        sid = _mint_calendar(now - (td._CALENDAR_PENDING_TTL_SECONDS + 5))
        assert td._claim_deferred_kind("calendar", USER, CHANNEL, sid) is None

    def test_the_reply_names_what_was_cancelled(self):
        now = time.time()
        _mint_calendar(now - 45)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="cancel", turn_started_at=now)
        assert "calendar event" in reply
        assert "Still staged" not in reply, "nothing else was armed"

    def test_the_reply_names_the_pendings_that_SURVIVE(self):
        """A cancel pops only the FRESHEST pending, so a blanket "nothing was
        changed" reads as "everything is clear" while the others stay armed and
        confirmable by a later bare "yes". This path also RETURNS a reply, which
        ends the turn before the model runs -- so the S2 pending-state context
        never reaches the user here and this sentence is the only chance."""
        now = time.time()
        _mint_shopify(now - 120)
        _mint_asana_create(now - 100)
        _mint_calendar(now - 45)          # freshest -> the one cancelled
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="cancel", turn_started_at=now)
        assert "cancelled a calendar event" in reply
        assert "Still staged" in reply
        assert "an inventory change" in reply and "an Asana task change" in reply
        assert "a calendar event staged" not in reply, "named the one it just cancelled"

    def test_an_affirm_never_takes_the_cancel_path(self):
        now = time.time()
        sid = _mint_calendar(now - 45)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="FNDR",
            message="yes", turn_started_at=now)
        assert reply is None
        assert td.stash_is_live(sid), "an affirm must leave the pending for the tool"


class TestQaMarkerStripping:
    """The section-F smoke battery is specified as [QA]-tagged, so the marker
    must never disqualify a confirm -- but it must not soften a message either."""

    @pytest.mark.parametrize("msg", ["[QA] confirm", "[QA] yes", "*[QA]* confirm",
                                     "[qa] confirm", "  [QA]   confirm  "])
    def test_a_leading_marker_is_stripped(self, msg):
        assert td._confirm_intent(msg, None) == "affirm"

    @pytest.mark.parametrize("msg", ["[QA] cancel", "[QA] no", "[qa] stop"])
    def test_a_leading_marker_on_a_cancel_is_stripped(self, msg):
        assert td._confirm_intent(msg, None) == "negate"

    def test_a_mid_sentence_marker_does_not_rescue_a_content_word(self):
        """Stripped as a PREFIX only: the marker must not be smuggled into a
        message to make an otherwise-disqualifying sentence read as a confirm."""
        assert td._confirm_intent("delete the [QA] budget spreadsheet", None) is None

    def test_a_bare_marker_alone_is_not_a_confirm(self):
        assert td._confirm_intent("[QA]", None) is None

    def test_ordinary_confirms_are_unchanged(self):
        assert td._confirm_intent("confirm", None) == "affirm"
        assert td._confirm_intent("cancel", None) == "negate"
        assert td._confirm_intent("what is our cash position", None) is None


class TestConnectorNoiseStripIsLinear:
    """Third ReDoS of this session, so this gets a permanent timing guard.

    _strip_connector_noise runs on the pre-model hot path for EVERY turn of
    every user, before rate limiting. It holds two super-linear patterns: the
    [QA] prefix added by this hotfix (7,065 ms at 40k spaces as first written)
    and the PRE-EXISTING Sent-using footer, which is cubic (~703 ms at 800
    spaces). CPython's re holds the GIL, so either one blocks the whole process.
    """

    # Explicit ids: a 40,000-character parameter becomes the test id otherwise,
    # and pytest prints it on every summary line.
    @pytest.mark.parametrize("payload", [
        pytest.param(" " * 40000, id="40k-spaces"),
        pytest.param(" " * 40000 + "confirm", id="40k-spaces-then-confirm"),
        pytest.param("*" * 20000 + " " * 20000, id="20k-stars-20k-spaces"),
        pytest.param("> " * 20000, id="20k-quote-markers"),
        pytest.param("confirm" + " " * 3000 + "Sent using <@U123>",
                     id="cubic-footer-pattern"),
    ])
    def test_pathological_whitespace_returns_fast(self, payload):
        t0 = time.perf_counter()
        td._strip_connector_noise(payload)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.25, f"took {elapsed * 1000:.0f} ms -- super-linear"

    @pytest.mark.parametrize("payload", [
        pytest.param(" " * 40000 + "confirm", id="40k-spaces-then-confirm"),
        pytest.param("x" * 5000, id="5k-chars"),
    ])
    def test_the_whole_intent_classifier_returns_fast(self, payload):
        t0 = time.perf_counter()
        td._confirm_intent(payload, None)
        assert (time.perf_counter() - t0) < 0.25

    def test_the_length_bail_cannot_hide_a_real_confirm(self):
        """The bail only skips messages far longer than any bare confirm --
        _confirm_intent independently rejects anything over 10 tokens."""
        assert len("confirm") < td._STRIP_MAX_CHARS
        assert td._confirm_intent("confirm", None) == "affirm"
        assert td._confirm_intent("[QA] confirm", None) == "affirm"
        long_but_not_a_confirm = "word " * 500
        assert td._confirm_intent(long_but_not_a_confirm, None) is None

    def test_stripping_still_works_on_normal_length_input(self):
        assert td._strip_connector_noise("[QA] confirm").strip() == "confirm"
        # The real Cowork footer shape: the pattern requires a <@...> token.
        assert "Sent using" not in td._strip_connector_noise(
            "confirm\nSent using <@U0B2RM2JYJ1>")
        assert td._confirm_intent("confirm\nSent using <@U0B2RM2JYJ1>", None) == "affirm"


class TestDmRememberTypedConfirm:
    DM = "dm"

    def _mint_remember(self, ts: float) -> str:
        sid = cc.mint_stash_id("remember", USER, self.DM)
        td._store_pending_remember(USER, self.DM, {
            "note_text": "[QA] the v2 smoke fruit is plum",
            "entity": "FNDR", "scope": "FNDR", "share_requested": False,
            "ts": ts, "stash_id": sid,
        })
        return sid

    @pytest.mark.parametrize("age", [1.0, 45.0, 300.0])
    def test_the_tool_finds_its_own_pending_well_inside_the_ttl(self, age):
        """Live: a bare "yes" 45s after the preview got "The preview expired."
        TTL is 600s."""
        self._mint_remember(time.time() - age)
        with patch.object(td, "_execute_claimed_remember", return_value="Saved.") as ex:
            out = td._tool_cora_remember(USER, "FNDR", {
                "_channel_name": self.DM, "confirmed": True,
            })
        assert "expired" not in out.lower(), f"remember read as expired at {age}s"
        ex.assert_called_once()

    def test_the_pending_is_visible_to_the_interceptor_peek(self):
        sid = self._mint_remember(time.time() - 45)
        assert td._peek_pending_remember(USER, self.DM) is not None
        assert td.stash_is_live(sid)

    def test_a_dm_typed_confirm_executes_the_remember_deterministically(self):
        """cq-236fd0310eb8. The interceptor used to DEFER remember to the model,
        which is how the live 8/20 DM produced three typed "Confirm" turns that
        each reached cora_remember(confirmed=true) and saved nothing. It now
        claims and executes the stash itself, so the model never sees the turn."""
        now = time.time()
        sid = self._mint_remember(now - 45)
        with patch.object(td, "_execute_claimed_remember",
                          return_value=("WRITE_CONFIRMED -- post it" + chr(10) * 2
                                        + "Saved to your notes.")) as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER, channel_name=self.DM, entity="FNDR",
                message="Confirm", turn_started_at=now,
            )
        ex.assert_called_once()
        assert reply == "Saved to your notes.", "sentinel must be stripped before posting"
        assert not td.stash_is_live(sid), "the confirmed pending must be consumed"
        assert td._peek_pending_remember(USER, self.DM) is None

    def test_an_unclassifiable_message_still_leaves_the_remember_pending_intact(self):
        """The execute branch is gated on a STRICT affirm. Anything else keeps
        the "ambiguous -> pending intact" contract every other kind holds, so a
        passing remark can never consume a staged note."""
        now = time.time()
        sid = self._mint_remember(now - 45)
        with patch.object(td, "_execute_claimed_stash") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER, channel_name=self.DM, entity="FNDR",
                message="the plum one, from the smoke test earlier", turn_started_at=now,
            )
        ex.assert_not_called()
        assert reply is None
        assert td.stash_is_live(sid)

    def test_a_conflicting_action_verb_never_executes_the_remember(self):
        """"remember" is not a canonical action verb, so any action verb in the
        message conflicts and the turn falls through to the model rather than
        writing the note the user did not mean to confirm."""
        now = time.time()
        sid = self._mint_remember(now - 45)
        with patch.object(td, "_execute_claimed_stash") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER, channel_name=self.DM, entity="FNDR",
                message="yes delete it", turn_started_at=now,
            )
        ex.assert_not_called()
        assert reply is None
        assert td.stash_is_live(sid)

    def test_a_concurrent_re_stash_is_NOT_what_gets_executed(self):
        """D-051 lens-1 MED. The execute branch used to re-peek the slot for the
        stash_id it claimed, which defeats _claim_deferred_kind's exact-id match
        -- the guard that exists so a sibling turn's never-seen preview is not
        destroyed. With a write on the end of it, the failure is worse than the
        one that guard was written for: the note the user was shown is discarded
        and a DIFFERENT one, whose preview they have never seen, is saved.

        Simulated by overwriting the slot between the arbitration peek and the
        claim, which is exactly what a concurrent cora_remember phase-1 call
        does."""
        now = time.time()
        seen_sid = self._mint_remember(now - 45)
        real_peek = td._peek_pending_remember

        def _peek_then_overwrite(user, channel):
            entry = real_peek(user, channel)
            # A sibling turn lands its own preview after the arbitration read.
            td._store_pending_remember(user, channel, {
                "note_text": "[QA] a note the user has never seen",
                "entity": "FNDR", "scope": "FNDR", "share_requested": False,
                "ts": time.time(), "stash_id": cc.mint_stash_id("remember", user, channel),
            })
            return entry

        with patch.object(td, "_peek_pending_remember", side_effect=_peek_then_overwrite), \
             patch.object(td, "_execute_claimed_stash",
                          return_value=("Saved.", None)) as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER, channel_name=self.DM, entity="FNDR",
                message="Confirm", turn_started_at=now,
            )
        # Either it executed the stash the user was actually shown, or it
        # deferred -- never the one that arrived behind their back.
        if ex.called:
            assert ex.call_args[0][1].get("stash_id") == seen_sid
        assert reply is None or "never seen" not in str(reply)

    def test_the_notes_unavailable_reply_is_addressed_to_the_USER(self):
        """This path's return is POSTED verbatim by the interceptor and by the
        button tap. "tell the user ..." was an instruction addressed to nobody
        on both -- and it falsified the reasoning that admitted this executor to
        the interceptor, which claimed every return here was already the exact
        user-facing sentence."""
        with patch.object(td, "_notes_kb", return_value=(None, None)):
            out = td._execute_claimed_remember({"note_text": "x", "entity": "FNDR"}, USER)
        assert "tell the user" not in out.lower()
        assert "NOT SAVED" in out

    def test_a_typed_cancel_still_dismisses_the_remember_pending(self):
        """The cancel half of the deferred path is unchanged by the execute
        branch: a negate is classified first and still pops the stash."""
        now = time.time()
        sid = self._mint_remember(now - 45)
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=self.DM, entity="FNDR",
            message="no, cancel", turn_started_at=now,
        )
        assert reply and "cancel" in reply.lower()
        assert not td.stash_is_live(sid)
