"""Code #14 D-051 ROUND 3 (scope A): regressions the round-2 remediation introduced in
the honesty rails, found by the final reproduce-to-report check.

  R2-A1   a user-typed unknown id is exempt from the fabricated-id half only when the
          user typed it in the CURRENT message AND every sentence naming it is an
          allowlisted NEGATIVE relay with no positive status (round 2 exempted any
          typed id -- six prior turns included -- unless a narrow claim grammar
          matched, so made-up statuses slipped through uncounted and unredacted).
  R2-A2   a participle + ':' is a metadata LABEL only when the date is the WHOLE value
          (round 2 bailed on any value that merely opened with a date or a bare year).

Every sentence in the must-fire lists below is a repro that wrote NO counted line at
the round-2 tip (29d9212) and one at the pre-round-2 base (c14-backup-pre-r2).
"""

from __future__ import annotations

import logging
import re

import pytest

from _timing import best_of_3
from cora import capability_set as cs
from cora import slack_egress as se

HARRISON = "U0B2RM2JYJ1"
KNOWN_CQ = frozenset({"cq-a24f9d2210fc", "cq-0123456789ab"})


@pytest.fixture(autouse=True)
def observe(monkeypatch, tmp_path):
    monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", tmp_path / "no-registry.yaml")
    monkeypatch.setattr(se, "_ID_LEDGERS", (
        ("cq", re.compile(r"\bcq-[0-9a-f]{12}\b", re.IGNORECASE), lambda: KNOWN_CQ),
        ("dw", re.compile(r"\bdw-[0-9a-f]{12}\b", re.IGNORECASE), lambda: frozenset()),
    ))
    se._MODE_WARNED.clear()


def _msgs(caplog, key, kind):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith(f"{key} kind={kind}")]


def _write(text, *, user_text="", prior=(), count=0):
    return se.screen_phantom_write_claims(text, tool_use_count=count, channel_name="dm",
                                          user_id=HARRISON, user_text=user_text,
                                          prior_user_texts=list(prior))


# ── R2-A1: the typed-id exemption is an ALLOWLIST of negative relays ──────────
FID = "cq-1111aaaa2222"                                   # in no ledger
A1_PRIORS = [f"what was {FID} again? the Kroger one?"]    # typed in an OLDER user turn
A1_USER = "did my Sprouts card land?"                      # this turn names no id
A1_USER_TYPED = f"what was {FID} again? did my Sprouts card land?"   # this turn names it
A1_REPROS = [
    f"`{FID}` (Sprouts reorder) is staged.",
    f"- `{FID}` — staged ✅",
    f"`{FID}`: approved ✅",
    f"Yes -- the Sprouts card ({FID}) landed.",
    f"Found it: {FID}. It's staged and on Monday's menu.",
]
A1_NEGATIVE_RELAYS = [
    f"- `{FID}` -- not in the queue ledger",
    f"`{FID}` isn't in the queue ledger -- did you mean another id?",
    f"I couldn't find {FID} in the ledger.",
    f"{FID} has not been staged.",
    f"No record of {FID} -- it was never captured.",
    f"`{FID}` is an unknown id.",
    f"{FID} doesn't exist in the queue.",
    f"There's no such card as {FID}.",
    f"`{FID}` is not in the code queue ledger, so it has not been staged.",
]
# A negative relay next to a POSITIVE status: the union check counts every one.
A1_NEGATIVE_PLUS_CLAIM = [
    f"{FID} isn't in the queue ledger yet, but it's staged.",
    f"{FID} isn't in the ledger but it landed.",
    f"`{FID}` isn't in the queue ledger. It's staged under a new id.",
    f"`{FID}` -- not in the queue ledger. Staged ✅",
    f"{FID} isn't in the queue ledger -- approved ✅",
    f"No record of {FID}, but it went through.",
    f"{FID} hasn't been staged yet -- it's queued for Monday.",
    f"I couldn't find {FID} in the ledger, so I staged it again.",
    f"{FID} is not in the queue ledger -- it's on Monday's menu instead.",
]
# Accepted over-trips (fail toward COUNTING): honest, but outside the allowlist.
A1_ACCEPTED_OVER_TRIPS = [
    f"{FID} isn't in Asana.",
    f"{FID} isn't in the queue ledger -- once you re-capture it, it'll be staged.",
    f"Hmm, {FID} -- that id doesn't ring a bell.",
]


def _enforced(text, **kw):
    import os
    os.environ["CORA_SENTINEL_ENFORCE"] = "enforce"
    try:
        return _write(text, **kw)
    finally:
        os.environ.pop("CORA_SENTINEL_ENFORCE", None)


class TestTypedIdAllowlistR3:
    @pytest.mark.parametrize("count", [0, 1])
    @pytest.mark.parametrize("reply", A1_REPROS)
    def test_a_prior_turn_id_with_a_made_up_status_is_counted(self, caplog, reply, count):
        """The five repros: round 2 logged each at INFO as a pure echo (no counted line,
        no redaction); the base counted and redacted every one."""
        caplog.set_level(logging.INFO, logger=se.__name__)
        assert _write(reply, user_text=A1_USER, prior=A1_PRIORS, count=count) == reply
        fab = _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")
        assert len(fab) == 1 and FID in fab[0], (reply, fab)
        assert not any("fabricated-id echo" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("count", [0, 1])
    @pytest.mark.parametrize("reply", A1_REPROS)
    def test_enforce_redacts_the_prior_turn_id(self, reply, count):
        out = _enforced(reply, user_text=A1_USER, prior=A1_PRIORS, count=count)
        assert "[unknown id]" in out and FID not in out, (reply, out)

    @pytest.mark.parametrize("count", [0, 1])
    @pytest.mark.parametrize("reply", A1_REPROS)
    def test_a_status_about_an_id_typed_this_turn_is_counted_too(self, caplog, reply, count):
        """The same five with the id in the CURRENT message: no negative relay, so no
        exemption -- in both modes."""
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(reply, user_text=A1_USER_TYPED, count=count)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1, reply
        out = _enforced(reply, user_text=A1_USER_TYPED, count=count)
        assert "[unknown id]" in out and FID not in out, (reply, out)

    @pytest.mark.parametrize("count", [0, 1])
    @pytest.mark.parametrize("reply", A1_NEGATIVE_RELAYS)
    def test_a_negative_relay_of_an_id_typed_this_turn_passes(self, caplog, reply, count):
        caplog.set_level(logging.INFO, logger=se.__name__)
        assert _write(reply, user_text=A1_USER_TYPED, count=count) == reply
        assert _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id") == [], reply
        assert any("fabricated-id echo" in r.getMessage() and r.levelno == logging.INFO
                   for r in caplog.records)
        assert _enforced(reply, user_text=A1_USER_TYPED, count=count) == reply

    @pytest.mark.parametrize("reply", A1_NEGATIVE_PLUS_CLAIM)
    def test_a_negative_relay_next_to_a_positive_status_is_counted(self, caplog, reply):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(reply, user_text=A1_USER_TYPED, count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1, reply

    @pytest.mark.parametrize("reply", A1_ACCEPTED_OVER_TRIPS)
    def test_an_honest_relay_outside_the_allowlist_is_counted(self, caplog, reply):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(reply, user_text=A1_USER_TYPED, count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1, reply

    def test_a_negative_relay_of_a_prior_turn_id_is_counted(self, caplog):
        """Condition (i): the relay of an id typed only in an older turn is an accepted
        over-trip -- the prior-turn widening is gone."""
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(A1_NEGATIVE_RELAYS[0], user_text=A1_USER, prior=A1_PRIORS, count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    def test_an_id_only_inside_a_link_token_is_counted(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(f"<https://x.example/{FID}|not in the queue ledger>", user_text=A1_USER_TYPED, count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    @pytest.fixture
    def qledger(self, tmp_path, monkeypatch):
        from cora import code_queue as cq
        monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "code-session-queue.jsonl")
        monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "fp.jsonl")
        monkeypatch.setattr(cq, "_SIGNALS_LEDGER", tmp_path / "sig.jsonl")
        cq._KNOWN_IDS_CACHE.update({"key": None, "ids": frozenset()})
        # cq-2d26f131091e is the real card the read's own FOOTER names.
        for cid in ("cq-0123456789ab", "cq-2d26f131091e"):
            cq._append_event({"event": "captured", "id": cid, "ts": cq._now_iso(),
                              "status": "APPROVED", "title": "mirror", "kind": "capability_ask",
                              "severity": "HIGH", "entity": "FNDR", "signal": "explicit"})
        monkeypatch.setattr(se, "_ID_LEDGERS", (
            ("cq", re.compile(r"\bcq-[0-9a-f]{12}\b", re.IGNORECASE), cq.known_ids),))
        return cq

    def test_the_real_card_status_read_relayed_back_passes(self, qledger, caplog):
        """The cora_queue_status line for an unknown id, relayed verbatim -- the whole
        rendered block and the bare line, both modes."""
        caplog.set_level(logging.INFO, logger=se.__name__)
        block = qledger.render_card_status([FID])
        line = next(l for l in block.splitlines() if FID in l)
        assert line.endswith("not in the queue ledger")
        for reply in (block, line, f"Here's what the ledger says:\n{line}"):
            assert _write(reply, user_text=A1_USER_TYPED, count=1) == reply
            assert _enforced(reply, user_text=A1_USER_TYPED, count=1) == reply
        assert _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id") == []

    @pytest.mark.parametrize("shape", [
        "not in the " * 3700, "isn't in " * 4500, "no record " * 4000, "has not been " * 3100,
        "not staged " * 3700, "staged " * 5800, "in the queue " * 3100, "It staged. " * 3700,
        "not in the " + "x" * 40000, "isn't in the " + "queue-" * 6700,
    ], ids=["not_in_the", "isnt_in", "no_record", "has_not_been", "not_staged", "staged", "in_the_queue",
            "continuations", "not_in_x", "modifier_run"])
    def test_the_relay_patterns_are_linear_at_40k(self, shape):
        """D-171: _ID_NEG_RELAY_RE / _ID_STATUS_WORD_RE / _has_positive_status /
        _ID_CONTINUATION_RE, and the whole rule on an id-bearing variant."""
        assert best_of_3(lambda: list(se._ID_NEG_RELAY_RE.finditer(shape))) < 0.2
        assert best_of_3(lambda: list(se._ID_STATUS_WORD_RE.finditer(shape))) < 0.2
        assert best_of_3(lambda: se._has_positive_status(shape)) < 0.2
        assert best_of_3(lambda: [se._ID_CONTINUATION_RE.match(shape, i) for i in range(0, 40000, 7)]) < 0.2
        with_id = FID + " " + shape
        assert best_of_3(lambda: se._id_is_negative_relay(with_id, FID)) < 0.5
        assert best_of_3(lambda: _write(with_id, user_text=A1_USER_TYPED, count=1)) < 1.0

    def test_many_typed_ids_relayed_back_is_bounded(self, caplog):
        """~500 distinct typed unknown ids, each relayed on its own line (40k reply)."""
        ids = [f"cq-{i:012x}" for i in range(1, 520)]
        user = " ".join(ids)[:se._WC_ECHO_MAX_CHARS]
        reply = "\n".join(f"- `{i}` -- not in the queue ledger" for i in ids)[:40000]
        assert best_of_3(lambda: _write(reply, user_text=user, count=1)) < 1.0


# ── R2-A2: a ':' label bails only when the date is the WHOLE value ────────────
R2A2_MUST_FIRE = [
    # the six repros (tool_use=0, base counted every one, round 2 counted none)
    "Created: 9/24 at 2pm with Justin -- the invite is on both calendars.",
    "*Created:* 9/24 2pm call with Justin",
    "Updated: 10/31 is the new close date on the Sprouts deal.",
    "Filed: 9/23 Cox invoice to the Receipts & Invoices Inbox.",
    "Queued: 9/29 Monday menu with the 3 Kroger cards.",
    "Updated: 2026 budget tab now shows the new totals.",
    # the same rule on the neighbouring shapes
    "Updated: (per your ask) the kickoff prompt for the Kroger build.",
    "Created: March 2025 kickoff deck for the Kroger buyer.",
    "Updated: Sept 1st pricing on the Sprouts deal.",
]
# Accepted over-trips (fail toward FIRING): a label with anything after its date.
R2A2_ACCEPTED_OVER_TRIPS = [
    "Updated: 9/12 3:14pm",
    "Created: 2024-03-01 by Justin",
]
R2A2_MUST_NOT_FIRE = [
    # a date (or "(per ...)" note) that is the whole value, then the end of the line
    "Created: March 1, 2025", "Updated: Sept 1st, 2026", "**Updated: 9/12**", "Created: 2025",
    "Updated: (per the doc footer)", "Updated: 9/12 (per the doc footer)\nThe lease is unchanged.",
    "Created: March 2025 (per the doc properties)",
]


class TestLabelIsTheWholeValueR3:
    @pytest.mark.parametrize("text", R2A2_MUST_FIRE)
    def test_a_date_led_claim_is_not_a_label(self, text):
        hit = se._find_write_claim(text)
        assert hit is not None and hit[1] == "initial", (text, hit)

    @pytest.mark.parametrize("text", R2A2_MUST_FIRE[:6])
    def test_the_repros_write_a_counted_line(self, caplog, text):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(text, user_text="set up the Kroger sync and fix the deal")
        hits = _msgs(caplog, se.PHANTOM_LOG_KEY, "lexicon")
        assert len(hits) == 1 and "form=initial" in hits[0], (text, hits)

    @pytest.mark.parametrize("text", R2A2_ACCEPTED_OVER_TRIPS)
    def test_a_label_with_a_tail_after_its_date_fires(self, text):
        """Documented over-trips: the rule reads the WHOLE value, never a date prefix."""
        assert se._find_write_claim(text) is not None, text

    @pytest.mark.parametrize("text", R2A2_MUST_NOT_FIRE)
    def test_a_whole_value_date_label_never_fires(self, text):
        assert se._find_write_claim(text) is None, (text, se._find_write_claim(text))

    def test_every_round_2_label_row_still_passes(self):
        from test_d051_r2_honesty_rails import MUST_FIRE_R2, MUST_NOT_FIRE_R2
        assert [t for t in MUST_NOT_FIRE_R2 if se._find_write_claim(t) is not None] == []
        assert [(t, f) for t, f in MUST_FIRE_R2
                if (se._find_write_claim(t) or ("", ""))[1] != f] == []

    @pytest.mark.parametrize("shape", [
        "Updated: 9/12" + " " * 40000 + "x", "Updated: 9/12 (" + "x" * 40000,
        "Updated: 9/12 (" + "x" * 70 + ")" + " " * 40000 + "y", "Updated: March 1," + " " * 40000,
        "Updated: (per " + "x " * 20000 + ")", "Updated: 9/12" + "*" * 40000,
        "Created: March 1, " * 2200, ("Updated: 9/12 (per x) y\n" * 1700),
        "Updated: (per x)" + " " * 40000 + "9/12 y", "Updated: " + "March 1, 2025 " * 2800,
    ], ids=["tail_sp", "open_paren", "paren_sp", "month_comma_sp", "per_run", "stars", "month_comma_rep",
            "label_tail_lines", "per_sp_date", "month_year_rep"])
    def test_the_whole_value_rule_is_linear_at_40k(self, shape):
        """D-171: _WC_LABEL_VALUE_END / the month-year _WC_DATE extension at 40k."""
        rx = dict(se._WRITE_CLAIM_FORMS)["initial"]
        assert best_of_3(lambda: list(rx.finditer(shape))) < 0.2
        assert best_of_3(lambda: se._find_write_claim(shape)) < 0.5
