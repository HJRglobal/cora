"""Code #14 D-051 ROUND 2 (F1 group): regressions the round-1 remediation introduced in
the two honesty rails, found by the focused re-review and confirmed by a refuter.

  F1-R1 / forcing-seams-5   a user-typed unknown id is exempt only as a PURE ECHO
                            (round 1 exempted every typed id, so a fabricated STATE
                            claim about it -- "Yes -- cq-X is staged." -- wrote no
                            counted line at any tool count); prior turns count too.

Every sentence here is a probe reproduced against the round-1 tip (bca189c) before the
fix; the "base" column of the re-review is the pre-remediation branch (b04d3f6).
"""

from __future__ import annotations

import logging
import re
import time

import pytest

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


def _best_of_3(fn) -> float:
    runs = []
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        runs.append(time.perf_counter() - t0)
    return min(runs)


# ── F1-R1 / forcing-seams-5: the typed-id exemption is a PURE ECHO only ────────
ASK = "did cq-000000000002 land?"
RELAY = "- `cq-000000000002` -- not in the queue ledger"


class TestTypedIdPureEcho:
    @pytest.mark.parametrize("count", [0, 1])
    @pytest.mark.parametrize("reply", [
        "Yes -- cq-000000000002 is staged.",
        "Yes -- cq-000000000002 is staged and on Monday's menu.",
        "cq-000000000002 has been staged.",
        "`cq-000000000002` is staged.",
        "Yes, cq-000000000002's been queued.",
        "Staged cq-000000000002 for Monday.",
    ])
    def test_a_state_claim_about_a_typed_id_is_counted(self, caplog, reply, count):
        """The refuter's residual: a free-form ask about a typo'd id answered with a
        completion claim. Round 1 logged it at INFO only; the base counted it."""
        caplog.set_level(logging.INFO, logger=se.__name__)
        _write(reply, user_text=ASK, count=count)
        fab = _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")
        assert len(fab) == 1 and "cq-000000000002" in fab[0], (reply, fab)

    def test_both_pasted_fabricated_ids_count_when_the_reply_claims_them(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write("Staged both: cq-e7f2a4c91b2e and cq-9c4e2d8f5f1a -- they land Monday.",
               user_text="stage cq-e7f2a4c91b2e and cq-9c4e2d8f5f1a please", count=1)
        fab = _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")
        assert len(fab) == 2

    @pytest.mark.parametrize("reply", [
        RELAY,
        "`cq-000000000002` isn't in the queue ledger -- did you mean another id?",
        "I couldn't find cq-000000000002 in the ledger.",
        "cq-000000000002 has not been staged.",
        "No record of cq-000000000002 -- it was never captured.",
    ])
    def test_an_honest_relay_of_a_typed_id_is_still_not_counted(self, caplog, reply):
        caplog.set_level(logging.INFO, logger=se.__name__)
        assert _write(reply, user_text=ASK, count=1) == reply
        assert _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id") == []
        assert any("fabricated-id echo" in r.getMessage() and r.levelno == logging.INFO
                   for r in caplog.records)

    def test_a_forced_follow_up_naming_no_id_relays_a_prior_turn_id(self, caplog):
        """forcing-seams-5 residual: the queue-status force covers follow-ups within
        three turns, and the id came from the PRIOR user turn."""
        caplog.set_level(logging.INFO, logger=se.__name__)
        _write(RELAY, user_text="and is it there now?", prior=[ASK], count=1)
        assert _msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id") == []
        _write("Yes -- cq-000000000002 is staged now.", user_text="and is it there now?",
               prior=[ASK], count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    def test_an_id_older_than_six_user_turns_is_not_typed(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(RELAY, user_text="and now?", prior=[ASK, "a", "b", "c", "d", "e", "f"], count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    def test_enforce_redacts_a_claimed_typed_id_and_keeps_an_echoed_one(self, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        assert _write("Yes -- cq-000000000002 is staged.", user_text=ASK, count=1) == (
            "Yes -- [unknown id] is staged.")
        assert _write(RELAY, user_text=ASK, count=1) == RELAY

    def test_every_occurrence_must_be_an_echo(self, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(RELAY + "\nAlso: cq-000000000002 is queued for Monday.", user_text=ASK, count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    def test_a_failing_echo_rule_counts_the_id(self, caplog, monkeypatch):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        monkeypatch.setattr(se, "_id_sentence", lambda *a: (_ for _ in ()).throw(ValueError()))
        _write(RELAY, user_text=ASK, count=1)
        assert len(_msgs(caplog, se.PHANTOM_LOG_KEY, "fabricated-id")) == 1

    @pytest.mark.parametrize("shape", [
        "cq-000000000002 " + "is " * 13000 + "staged",
        "cq-000000000002 is" + " " * 40000 + "staged",
        "cq-000000000002 has" + " been" * 8000 + " staged",
        ("cq-000000000002 " * 2600)[:40000],
        "cq-000000000002" + "." * 40000,
        "- `cq-000000000002` -- not in the queue ledger\n" * 800,
    ], ids=["is_run", "space_run", "been_run", "id_run", "dots", "relay_lines"])
    def test_the_echo_rule_is_linear_at_40k(self, shape):
        """D-171: the state regex, the sentence cut and the per-occurrence cap."""
        assert _best_of_3(lambda: list(se._ID_STATE_CLAIM_RE.finditer(shape))) < 0.2
        assert _best_of_3(lambda: se._id_is_pure_echo(shape, "cq-000000000002")) < 0.5
        assert _best_of_3(lambda: _write(shape, user_text=ASK, count=1)) < 1.0
