"""C6 (cq-0d40bb50bdb1): the same decision must not be filed six times.

MEASURED, not inferred. `data/decisions-inbox.md` holds 70 filed decisions and
the CBS Northstar POS quote (Quote LD-1176) appears SIX times, 2026-08-05 to
08-19, including a same-batch pair four seconds apart. Upstream the same fact was
proposed on EIGHT consecutive nights, 7/17..7/24, one per nightly reconciliation
run: Haiku re-reads the same OSN Drive digest chunk each night and paraphrases
the sentence.

TWO INDEPENDENT CAUSES, both fixed here.

  THE ID WAS RANDOM. `pass5:decision:{hash(summary + entity) & 0xFFFFFFFF:08x}`
  used the BUILTIN hash(), which over a str is siphash-randomized per interpreter
  -- and PYTHONHASHSEED is pinned nowhere in this repo, .env, or any task
  registration. Two nights produced BYTE-IDENTICAL decision text and still got
  different ids (66ec1a48 vs 3fd39fc0), so every downstream dedup -- all of which
  key on exact update_id equality -- was a no-op. The same defect existed on all
  three pass-5 families. Passes 1-4 were never affected: they use `_gap_id`,
  which is already hashlib-based.

  THE TEXT WAS PARAPHRASED. A stable id only collapses identical text. The live
  variants differ by one word: "$200 professional services charge" /
  "...allocation" / "...fee" / "...cost". Replayed over the real 187-row pass-5
  corpus, SequenceMatcher >= 0.85 -- the friction-mining rule the seed remembered
  -- collapses CBS 8 -> 4 and leaves the duplication. Containment >= 0.80 gets it
  to 2. So `same_fact` ORs the two comparators.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cora import fact_fingerprint as ff

# The four live paraphrases, verbatim from data/decisions-inbox.md and the
# proposed-updates ledger.
CBS = [
    "Uncaptured decision in Drive: CBS Northstar POS quotation (Quote LD-1176) "
    "approved for Gilbert & Warner locations with $200 professional services charge",
    "Uncaptured decision in Drive: CBS Northstar POS quotation (Quote LD-1176) "
    "approved for Gilbert & Warner locations with $200 professional services allocation",
    "CBS Northstar POS quotation (Quote LD-1176) approved for Gilbert and Warner "
    "locations at $200 professional services cost",
    "CBS Northstar POS quotation LD-1176 approved for Gilbert & Warner with a "
    "$200 professional service fee",
]

OTHER = [
    "Approved Shopify Plus upgrade for F3 Energy DTC storefront",
    "Uncaptured decision in Drive: Deposco warehouse contract renewal approved",
    "Uncaptured decision in Drive: Klaviyo plan downgrade approved for F3 Energy",
]


# ── determinism (the root cause) ────────────────────────────────────────────

def test_the_fingerprint_is_stable_across_separate_interpreters():
    """The whole bug. `hash()` gives a different answer per process; this must
    not. Run in subprocesses with DIFFERENT hash seeds so a regression back to
    the builtin cannot pass."""
    code = ("import sys; sys.path.insert(0, r'%s');"
            "from cora import fact_fingerprint as f;"
            "print(f.compute_fingerprint('OSN', %r))"
            % (str(Path(__file__).resolve().parents[1] / "src"), CBS[0]))
    seen = set()
    for seed in ("0", "1", "12345"):
        import os
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, check=True)
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"fingerprint is not stable across processes: {seen}"


def test_pass5_ids_are_stable_across_separate_interpreters():
    code = ("import sys; sys.path.insert(0, r'%s');"
            "from cora import reconciliation_engine as r;"
            "print(r._stable_id(%r, 'OSN'))"
            % (str(Path(__file__).resolve().parents[1] / "src"), CBS[0]))
    seen = set()
    for seed in ("0", "7", "99999"):
        import os
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, check=True)
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"pass-5 gap id is not stable across processes: {seen}"


def test_no_pass5_id_still_uses_the_builtin_hash():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cora" / "reconciliation_engine.py").read_text(encoding="utf-8")
    assert "hash(subj + entity)" not in src
    assert "hash(summary + entity)" not in src
    assert "hash(hint + entity)" not in src


# ── same_fact ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant", CBS)
def test_every_live_paraphrase_of_the_cbs_quote_is_one_fact(variant):
    assert ff.same_fact(CBS[0], variant) is True


@pytest.mark.parametrize("other", OTHER)
def test_genuinely_different_decisions_are_not_collapsed(other):
    assert ff.same_fact(CBS[0], other) is False


def test_sequencematcher_alone_would_miss_two_of_them():
    """Documents WHY containment is in the rule. If someone later 'simplifies'
    same_fact down to the friction 0.85 ratio, this is the measurement that
    says the duplication comes back."""
    from difflib import SequenceMatcher
    ratios = [SequenceMatcher(None, ff.normalize(CBS[0]), ff.normalize(v)).ratio()
              for v in CBS]
    assert sum(r < ff.FUZZY_RATIO for r in ratios) == 2
    assert all(ff.containment(CBS[0], v) >= ff.CONTAINMENT_RATIO for v in CBS)


def test_empty_and_junk_inputs_are_never_the_same_fact():
    for a, b in ((None, None), ("", ""), ("x", ""), (None, "x")):
        assert ff.same_fact(a, b) is False


def test_normalization_is_the_friction_mining_rule():
    """Fingerprints computed here and in friction_mining must agree, or the two
    ledgers silently disagree about what a duplicate is."""
    from cora import friction_mining as fm
    for text in CBS + OTHER:
        mine = ff.compute_fingerprint("x", text)
        theirs = fm.compute_fingerprint("x", text)
        # friction_mining renders its id as "<kind>:<digest>"; only the DIGEST
        # half is the shared contract, and it is the half that encodes the
        # normalization rule.
        # An equal digest over the same input IS the proof that both sides
        # normalize identically -- md5 does not collide by accident.
        assert theirs.endswith(mine), f"{theirs!r} vs {mine!r}"


# ── ledger ──────────────────────────────────────────────────────────────────

def test_a_recorded_fact_is_recognised_on_the_next_run(tmp_path):
    led = tmp_path / "fp.jsonl"
    assert ff.already_proposed(led, "decision", CBS[0], scope="OSN") == ""
    ff.record_proposal(led, "decision", CBS[0], scope="OSN", ref="pass5:decision:aaa")
    for variant in CBS:
        assert ff.already_proposed(led, "decision", variant, scope="OSN")


def test_the_scope_gate_keeps_two_entities_separate(tmp_path):
    """Two entities can legitimately make near-identical decisions; suppressing
    the second would lose it entirely."""
    led = tmp_path / "fp.jsonl"
    ff.record_proposal(led, "decision", CBS[0], scope="OSN")
    assert ff.already_proposed(led, "decision", CBS[2], scope="F3E") == ""
    assert ff.already_proposed(led, "decision", CBS[2], scope="OSN")


def test_an_exact_fingerprint_matches_regardless_of_scope(tmp_path):
    led = tmp_path / "fp.jsonl"
    ff.record_proposal(led, "decision", CBS[0], scope="OSN")
    assert ff.already_proposed(led, "decision", CBS[0], scope="F3E")


def test_a_different_kind_never_collides(tmp_path):
    led = tmp_path / "fp.jsonl"
    ff.record_proposal(led, "efficiency", CBS[0], scope="OSN")
    assert ff.already_proposed(led, "decision", CBS[0], scope="OSN") == ""


def test_rows_outside_the_window_are_ignored(tmp_path):
    led = tmp_path / "fp.jsonl"
    led.write_text(json.dumps({
        "ts": "2020-01-01T00:00:00+00:00", "kind": "decision",
        "fingerprint": ff.compute_fingerprint("decision", CBS[0]),
        "scope": "OSN", "representative": CBS[0],
    }) + "\n", encoding="utf-8")
    assert ff.already_proposed(led, "decision", CBS[0], scope="OSN",
                               window_days=30) == ""


def test_a_corrupt_ledger_fails_OPEN(tmp_path):
    """Proposing a duplicate is recoverable; silently dropping a real decision
    is not. A malformed ledger must let the item through."""
    led = tmp_path / "fp.jsonl"
    led.write_text("not json\n[1,2,3]\n\n", encoding="utf-8")
    assert ff.already_proposed(led, "decision", CBS[0], scope="OSN") == ""
    assert ff.already_proposed(tmp_path / "absent.jsonl", "decision", CBS[0]) == ""


def test_a_write_failure_does_not_block_the_proposal(tmp_path):
    """record_proposal returns the fingerprint even if it cannot persist it --
    it must never raise into the proposal it was recording."""
    fp = ff.record_proposal(tmp_path, "decision", CBS[0])  # a DIRECTORY, not a file
    assert fp == ff.compute_fingerprint("decision", CBS[0])


# ── the pass-5 gate, end to end ─────────────────────────────────────────────

def test_pass5_suppresses_a_repeat_decision_and_records_the_first(
        tmp_path, monkeypatch):
    from cora import reconciliation_engine as re_
    led = tmp_path / "fp.jsonl"
    monkeypatch.setattr(re_, "_DECISION_FP_LEDGER", led)

    assert re_._decision_already_proposed(CBS[0], "OSN") is False
    re_._record_decision_proposal(CBS[0], "OSN", "pass5:decision:abc")
    # every later night's paraphrase is now recognised
    for variant in CBS[1:]:
        assert re_._decision_already_proposed(variant, "OSN") is True
    # and an unrelated decision still gets through
    assert re_._decision_already_proposed(OTHER[0], "OSN") is False


def test_the_gate_records_at_proposal_time_not_approval_time(tmp_path, monkeypatch):
    """D-030: a finding never re-proposes regardless of what the human decided
    about it. The ledger row exists the moment the gap is emitted."""
    from cora import reconciliation_engine as re_
    led = tmp_path / "fp.jsonl"
    monkeypatch.setattr(re_, "_DECISION_FP_LEDGER", led)
    re_._record_decision_proposal(CBS[0], "OSN", "pass5:decision:abc")
    rows = [json.loads(ln) for ln in led.read_text(encoding="utf-8").splitlines() if ln]
    assert len(rows) == 1
    assert rows[0]["ref"] == "pass5:decision:abc"
    assert rows[0]["scope"] == "OSN"
    assert "resolved" not in rows[0] and "approved" not in rows[0]
