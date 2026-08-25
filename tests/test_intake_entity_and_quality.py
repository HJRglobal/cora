"""C9 (cq-dacabcc2e47e) + C10 (cq-b0e5bc37c41b): where a fact lands, and whether
it is a fact at all.

C9 -- THE LOAD-BEARING QUESTION, SETTLED. Known-answers injection IS entity-
scoped: _build_static_context does ONE `_KNOWN_ANSWERS_PATHS.get(entity)` and
reads ONE file. So a note filed under FNDR can never appear in an F3E channel's
always-injected block.

The live case: Hannah posted "Skylar has authorization to make inventory
adjustments in the f3-hq-inventory-adjustments channel" to #info-for-cora, to fix
a refusal that happened in an F3E channel. It filed under FNDR, because
cross_entity_guard's F3E keywords are brand words ("f3 energy", "f3e", "f3 pure")
and none of them appears in "f3-hq-inventory-adjustments".

PREMISE PARTLY OVERTURNED, and it changes the severity: fndr.md IS reachable from
F3E through the FNDR KB co-scan (every _brain file ingests tagged FNDR), so the
fact is not lost -- it is just not in the always-injected block, which is where a
scope refusal would actually consult it. Retag-at-source, not dual-write:
dual-write breaks known_answers_map's single-target model, doubles what
known_answer_facts dedups against, and doubles the FNDR-tagged KB chunks.

C10 -- the typed-reply write path had NO quality gate at all. answer_quality_ok
exists but is deliberately scoped to the MINE path, on the rationale that "a
click-to-approve always results in a write". That rationale is about
HARRISON-APPROVED writes; a teammate's raw DM reply is neither mined nor
approved, and it goes straight to a proposal.
"""

from __future__ import annotations

import pytest

from cora import info_intake as ii
from cora.gap_autofill import answer_quality_ok, answer_substance


# ── C9: a named channel is an entity claim ──────────────────────────────────

@pytest.mark.parametrize("text,entity", [
    # THE live case
    ("Skylar has authorization to make inventory adjustments in the "
     "f3-hq-inventory-adjustments channel", "F3E"),
    ("post the update in #osn-leadership when ready", "OSN"),
    ("<#C0B6GT3117Y|f3-athletes> is where fighter posts go", "F3E"),
    # keyword detection still works on its own
    ("F3 Pure wholesale tier 1 is $25.15", "F3E"),
])
def test_a_named_channel_resolves_its_entity(text, entity):
    got, ambiguous = ii.resolve_entity(text)
    assert got == entity
    assert ambiguous is False


@pytest.mark.parametrize("text", [
    "we should fix that well-known random-thing issue",
    "the follow-up is a nice-to-have for now",
    "check the read-only mirror after the hand-off",
])
def test_ordinary_hyphenated_prose_is_not_an_entity_claim(text):
    """Gated on entity_router.is_mapped so the trailing '*' catch-all cannot
    turn any hyphenated word pair into an entity."""
    got, ambiguous = ii.resolve_entity(text)
    assert (got, ambiguous) == ("FNDR", False)


def test_the_intake_channel_itself_names_no_entity():
    """Every contribution mentions #info-for-cora; if that counted, every fact
    would resolve to the intake surface's own entity."""
    assert ii.channel_token_entities("posted this in #info-for-cora") == set()


def test_a_channel_token_and_a_keyword_for_the_SAME_business_are_one_hit():
    """Uncollapsed, "#llc-finance" (LEX-LLC) plus a LEX keyword would read as
    TWO entities and resolve to ("FNDR", ambiguous) -- filing the fact nowhere
    useful and flagging it for no reason."""
    got, ambiguous = ii.resolve_entity("the LEX billing cadence per #llc-finance")
    assert ambiguous is False


def test_two_genuinely_different_entities_still_flag_as_ambiguous():
    got, ambiguous = ii.resolve_entity(
        "the F3 Energy launch and the OSN reconciliation both slip")
    assert (got, ambiguous) == ("FNDR", True)


# ── C9: widening the tagger must not widen what gets FILED ──────────────────

@pytest.mark.parametrize("text", [
    "file this under the llc-finance channel please",
    "the note belongs in #lts-scheduling",
    "put it in the lbhs-clinical channel",
])
def test_a_lex_channel_token_is_refused_fail_closed(text):
    """THE coupled risk. is_lex_content must consume the SAME union, or a
    contribution naming a LEX channel would newly resolve to LEX in
    resolve_entity while sailing past the blanket LEX skip."""
    assert ii.is_lex_content(text) is True


def test_a_non_lex_channel_token_does_not_trip_the_lex_skip():
    assert ii.is_lex_content("post in the f3-hq-inventory-adjustments channel") is False


@pytest.mark.parametrize("text", [
    "we agreed at the llc-level that this is fine",
    "the hand-off went fine at the team-level",
])
def test_hyphenated_prose_is_not_a_channel_and_is_not_refused(text):
    """D-051: a BARE hyphenated token is only a channel reference when the text
    says so. Without the corroboration rule, "we agreed at the llc-level" matched
    the llc-* route, resolved LEX, and was HARD-REFUSED by the blanket LEX skip
    -- ordinary English killing a benign contribution."""
    assert ii.channel_token_entities(text) == set()
    assert ii.is_lex_content(text) is False


@pytest.mark.parametrize("text", [
    "F3 Pure pricing per the drive-shares channel",
    "the OSN recon per the asana-feed channel",
])
def test_a_founder_routed_utility_channel_adds_no_entity_claim(text):
    """FNDR is the DEFAULT, and utility channels all route there. Counting it as
    a hit turned an otherwise-unambiguous contribution ambiguous (D-051)."""
    assert "FNDR" not in ii.channel_token_entities(text)
    _entity, ambiguous = ii.resolve_entity(text)
    assert ambiguous is False


def test_detection_failure_never_breaks_intake(monkeypatch):
    monkeypatch.setattr(ii.entity_router, "is_mapped",
                        lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ii.channel_token_entities("anything at all") == set()


# ── C10: the non-answer floor ───────────────────────────────────────────────

def test_substance_ignores_a_mention_token():
    """A 13-character opaque user id is not durable content, and resolving it to
    a name does not make it more of a fact."""
    assert answer_substance("<@U0B3AEJCYGP> yes exactly") == "yes exactly"
    assert answer_substance("@Hannah yes") == "yes"
    assert answer_substance("plain text") == "plain text"


@pytest.mark.parametrize("text", [
    "<@U0B3AEJCYGP> yes exactly",   # the live 8/19 junk entry's shape
    "@Hannah yes",
    "yes",
    "correct",
])
def test_a_non_answer_is_vetoed(text):
    ok, why = answer_quality_ok(text)
    assert ok is False
    assert "too short" in why


@pytest.mark.parametrize("text", [
    "Tier 1 wholesale for Pure is $25.15 per 12-pack",
    "<@U0B3AEJCYGP> the MOQ for Pure wholesale is 12 units",
    "Skylar may adjust inventory in the F3 HQ channel",
])
def test_a_real_fact_still_passes(text):
    ok, why = answer_quality_ok(text)
    assert ok is True, why


@pytest.mark.parametrize("text,fragment", [
    ("ask Justin about it", "punts to a person"),
    ("still working on that one", "in-progress"),
])
def test_the_existing_vetoes_are_unchanged(text, fragment):
    ok, why = answer_quality_ok(text)
    assert ok is False and fragment in why


def test_the_typed_reply_path_now_applies_the_floor():
    import inspect
    from cora import gap_autofill
    src = inspect.getsource(gap_autofill.record_ask_answer)
    assert "answer_quality_ok(reply_text)" in src
    # D-051: the ask stays PENDING on a quality rejection. A terminal state would
    # close the door the rejection message itself holds open -- match_pending_ask
    # only considers PENDING asks, so the invited retry could never be captured.
    assert 'stored["state"] = "REJECTED_QUALITY"' not in src
    assert "last_rejected_reason" in src
    # and it resolves mentions before storing, so no raw <@U...> reaches canon
    assert "resolve_slack_mentions" in src


def test_the_rejection_tells_the_replier_what_to_do():
    """A silent drop would leave them believing it was filed."""
    import inspect
    from cora import gap_autofill
    src = inspect.getsource(gap_autofill.record_ask_answer)
    assert "Reply with the fact itself" in src
