"""Capability-denial + internal-tool-name screen (Code #13 slice 1, cq-2a88e32a75ea)
and the derived capability set (cora.capability_set).

DIFFERENTIAL FIXTURE (D-256 -- a rail must replay its own incident): the
2026-09-10 09:16-10:30 AZ founder-DM transcript (session capture
2026-09-10_fndr_cora-roadmap-thread-post-12-execution.md, quoted in the Code #13
kickoff section 0). Five unmatched verb-shaped DMs drew four capability DENIALS
about capabilities the bot has and one reply that named an internal tool; the
same exchange's three REAL tool lines (the 09:16 stage ack, the 10:29 dismiss
acks, the 10:35 approve acks) are code-authored outcome strings and must not trip.

Contract under test:
  * the four denial replies TRIP (kind=denial) in a zero-tool_use turn for the
    founder; the toolname mention TRIPS (kind=toolname);
  * the three tool-line acks do NOT trip;
  * ten benign "I can't tell from here" / "I don't have that document" sentences
    about things the bot truly LACKS do NOT trip -- the capability set is the
    discriminator, not the phrase;
  * the same HubSpot denial trips in an F3E channel (hubspot tools offered) and
    is CLEAN in a LEX channel (Tier-1: no HubSpot tools) -- registry-derived;
  * observe mode: text returned BYTE-IDENTICAL, WARN keyed `phantom-capability-claim`;
    enforce: the honest template + `Try:` hint is PREPENDED (body byte-identical,
    D-282 never a strip); a symbol is replaced by `[internal tool]`;
  * developer surfaces (#cora-build) skip the toolname half only;
  * tool_use_count None skips the denial half, never the toolname half;
  * a capability set that cannot be derived means NO denial can trip;
  * the alias table contributes only for tokens PRESENT in the offered registry;
  * the ladder registry's explicit `capability_terms` join the set; lane ids do not;
  * app._dispatch_qa calls the screen at all three sites, AFTER the S2' screen and
    BEFORE the semantic-cache store.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from cora import capability_set as cs
from cora import slack_egress as se

HARRISON = "U0B2RM2JYJ1"

# ── the 9/10 transcript (kickoff section 0, verbatim fragments) ───────────────
DENIAL_0930_VISIBILITY = (
    "I don't have visibility into the code queue or staged items right now. "
    "To check on these, you'd want to use the Asana interface directly."
)
DENIAL_0935_SHIP = "I don't have a way to ship code sessions directly."
DENIAL_1000_CLOSE = "I don't have a tool to close code-queue items."
DENIAL_1030_APPROVE_STAGE = "I don't have tools to approve or stage code-queue items."
TOOLNAME_MENTION = (
    "I can add that to the queue with cora_queue_code_session if you'd like -- just say the word."
)
DENIALS = [DENIAL_0930_VISIBILITY, DENIAL_0935_SHIP, DENIAL_1000_CLOSE, DENIAL_1030_APPROVE_STAGE]

# the exchange's REAL tool lines -- code-authored outcome strings (S1' / card taps)
TOOL_LINES = [
    "📝 Prompt staged: `G:/My Drive/HJR-Founder-OS/_shared/projects/cora/_notes/2026-09-10_fndr_cora-code-prompt-x.md`",
    "🗑️ Dismissed -- this fingerprint won't resurface.",
    "✅ Queued (APPROVED).",
]

# ten honest sentences about things the bot truly LACKS
BENIGN_TRUE_LACK = [
    "I can't tell from here whether Tessa signed it.",
    "I don't have that document in my sources.",
    "I don't have the Q3 board deck -- it was never shared with me.",
    "I don't see a transcript for that call in my sources.",
    "I can't find that thread; it may predate the sweep.",
    "I don't have visibility into what the landlord decided.",
    "I'm not able to confirm the vendor's mailing address from what I have.",
    "I don't have a way to know when the shipment lands.",
    "I can't reach a conclusion on that from the numbers alone.",
    "There's no record of that meeting in my sources.",
]


@pytest.fixture
def observe(monkeypatch, tmp_path):
    monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
    monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", tmp_path / "no-registry.yaml")
    se._MODE_WARNED.clear()


def _hits(caplog, kind: str) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if se.CAPABILITY_LOG_KEY in r.getMessage() and f"kind={kind}" in r.getMessage()]


def _screen(text, *, count=0, channel="dm", entity="FNDR", founder=True, user=HARRISON):
    return se.screen_capability_claims(text, tool_use_count=count, channel_name=channel,
                                       user_id=user, entity=entity, cross_entity=founder,
                                       founder=founder)


# ── the transcript replays ────────────────────────────────────────────────────

class TestDifferentialFixture:
    @pytest.mark.parametrize("reply", DENIALS)
    def test_each_denial_trips_once_and_is_delivered_byte_identical(self, observe, caplog, reply):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        out = _screen(reply)
        assert out == reply
        hits = _hits(caplog, "denial")
        assert len(hits) == 1, hits
        assert "mode=observe" in hits[0] and "channel=#dm" in hits[0]
        assert all(r.levelno == logging.WARNING for r in caplog.records)

    def test_the_visibility_denial_names_the_queue_term(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen(DENIAL_0930_VISIBILITY)
        (hit,) = _hits(caplog, "denial")
        # longest-match: the sentence names both queue objects; either is the queue family
        assert "term='code queue'" in hit or "term='staged items'" in hit, hit

    def test_the_toolname_mention_trips_kind_toolname(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        out = _screen(TOOLNAME_MENTION, count=1)   # even in a turn that used a tool
        assert out == TOOLNAME_MENTION
        hits = _hits(caplog, "toolname")
        assert len(hits) == 1 and "symbol=cora_queue_code_session" in hits[0]
        assert _hits(caplog, "denial") == []

    @pytest.mark.parametrize("line", TOOL_LINES)
    def test_the_real_tool_lines_are_clean(self, observe, caplog, line):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        assert _screen(line) == line
        assert not [r for r in caplog.records if se.CAPABILITY_LOG_KEY in r.getMessage()]

    @pytest.mark.parametrize("sentence", BENIGN_TRUE_LACK)
    def test_true_lack_sentences_never_trip(self, observe, caplog, sentence):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        assert _screen(sentence) == sentence
        assert not [r for r in caplog.records if se.CAPABILITY_LOG_KEY in r.getMessage()]

    def test_ten_benign_sentences_zero_trips_total(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        for s in BENIGN_TRUE_LACK:
            _screen(s)
        assert not [r for r in caplog.records if se.CAPABILITY_LOG_KEY in r.getMessage()]

    def test_the_four_denials_trip_four_times_in_sequence(self, observe, caplog):
        """The health count is per REPLY: four replies -> four lines."""
        caplog.set_level(logging.WARNING, logger=se.__name__)
        for d in DENIALS:
            _screen(d)
        assert len(_hits(caplog, "denial")) == 4


# ── the discriminator is the registry, not the phrase ─────────────────────────

class TestRegistryDerivedDiscriminator:
    HUBSPOT_DENIAL = "I don't have access to HubSpot, so I can't see the pipeline from here."

    def test_hubspot_denial_trips_where_hubspot_tools_are_offered(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen(self.HUBSPOT_DENIAL, entity="F3E", founder=False, user="U_TOMMY")
        assert len(_hits(caplog, "denial")) == 1

    def test_same_denial_is_honest_in_a_lex_channel(self, observe, caplog):
        """LEX never gets HubSpot (Tier-1 doctrine) -- there the denial is TRUE."""
        caplog.set_level(logging.WARNING, logger=se.__name__)
        assert "hubspot" not in cs.capability_terms("LEX", cross_entity=False, founder=False)
        _screen(self.HUBSPOT_DENIAL, entity="LEX", founder=False, user="U_SHAUN")
        assert _hits(caplog, "denial") == []

    def test_alias_contributes_only_when_its_token_is_offered(self):
        f3e = cs.capability_terms("F3E", cross_entity=False, founder=False)
        lex = cs.capability_terms("LEX", cross_entity=False, founder=False)
        assert "crm" in f3e and "pipeline" in f3e            # hubspot aliases ride the hubspot token
        assert "crm" not in lex and "pipeline" not in lex     # no hubspot token -> no aliases
        assert "asana" in lex                                  # asana is offered everywhere

    def test_plain_english_tool_name_halves_are_never_terms(self):
        """f3e_ai_visibility must not make 'visibility' a capability (the first cut
        tripped 'I don't have visibility into what the landlord decided')."""
        f = cs.capability_terms("FNDR", cross_entity=True, founder=True)
        for word in ("visibility", "ai", "action", "content", "personal", "travel", "capital",
                     "brand", "press", "production", "channel", "items", "sales", "program"):
            assert word not in f, word
        assert "ai visibility" in f and "action items" in f and "content pipeline" in f

    def test_queue_objects_are_founder_capabilities_with_the_typed_verb_hint(self):
        f = cs.capability_terms("FNDR", cross_entity=True, founder=True)
        assert "code queue" in f and "staged items" in f and "code sessions" in f
        assert "stage cq-<12 hex>" in f["code queue"]
        t = cs.capability_terms("F3E", cross_entity=False, founder=False)
        assert "code queue" in t and "queue a code session" in t["code queue"]

    def test_queue_verbs_read_from_the_grammar(self):
        assert set(cs.queue_verbs()) == {"stage", "approve", "dismiss", "ship"}

    def test_find_capability_term_prefers_the_longest_match(self):
        terms = {"queue": "x", "code queue": "y", "asana": "z"}
        assert cs.find_capability_term("visibility into the code-queue right now", terms) == ("code queue", "y")
        assert cs.find_capability_term("the asanas were green", terms) is None       # whole word only
        assert cs.find_capability_term("", terms) is None

    def test_ladder_registry_explicit_terms_join_and_lane_ids_do_not(self, observe, tmp_path, monkeypatch, caplog):
        reg = tmp_path / "ladder-registry.yaml"
        reg.write_text(
            "lanes:\n"
            "  - lane: recap_card\n    tier: T0\n    capability_terms: ['recap card', 'meeting recap']\n"
            "    ask_hint: \"ask 'send the recap to the internal attendees'\"\n"
            "  - lane: phantom_write_screen\n    tier: T0\n",
            encoding="utf-8")
        monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", reg)
        terms = cs.capability_terms("FNDR", cross_entity=True, founder=True)
        assert "recap card" in terms and "meeting recap" in terms
        assert "phantom" not in terms and "write" not in terms and "screen" not in terms
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen("I don't have a way to send a recap card to the attendees.")
        assert len(_hits(caplog, "denial")) == 1

    def test_malformed_registry_contributes_nothing_and_does_not_raise(self, tmp_path, monkeypatch):
        reg = tmp_path / "ladder-registry.yaml"
        reg.write_text("lanes: [::not yaml", encoding="utf-8")
        monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", reg)
        assert cs._ladder_terms() == {}

    def test_registry_unavailable_means_no_denial_can_trip(self, observe, caplog, monkeypatch):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        monkeypatch.setattr(se, "_capability_terms", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no registry")))
        out = _screen(DENIAL_0930_VISIBILITY)
        assert out == DENIAL_0930_VISIBILITY
        assert _hits(caplog, "denial") == []
        assert any("UNAVAILABLE" in r.getMessage() for r in caplog.records)


# ── modes, counts, surfaces ───────────────────────────────────────────────────

class TestModesAndSurfaces:
    def test_unknown_tool_count_skips_denial_but_still_screens_symbols(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen(DENIAL_0930_VISIBILITY + " " + TOOLNAME_MENTION, count=None)
        assert _hits(caplog, "denial") == []
        assert len(_hits(caplog, "toolname")) == 1

    def test_tool_use_turn_never_trips_denial(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen(DENIAL_0930_VISIBILITY, count=1)
        assert _hits(caplog, "denial") == []

    def test_developer_surface_skips_the_toolname_half_only(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen(TOOLNAME_MENTION + " " + DENIAL_1000_CLOSE, channel="cora-build")
        assert _hits(caplog, "toolname") == []
        assert len(_hits(caplog, "denial")) == 1
        assert se.is_developer_surface("lex-cora-build") and se.is_developer_surface("cora-health")
        assert not se.is_developer_surface("dm") and not se.is_developer_surface("f3e-sales")

    def test_registry_tool_names_are_symbols_too(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen("I could run hubspot_get_my_deals for you.", count=0, entity="F3E", founder=False, user="U_T")
        assert any("symbol=hubspot_get_my_deals" in h for h in _hits(caplog, "toolname"))

    def test_enforce_prepends_template_with_hint_and_keeps_body(self, observe, monkeypatch, caplog):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        caplog.set_level(logging.WARNING, logger=se.__name__)
        out = _screen(DENIAL_0930_VISIBILITY)
        first, _, body = out.partition("\n\n")
        assert first.startswith(se.CAPABILITY_HONEST_TEMPLATE) and "Try:" in first
        assert "stage cq-<12 hex>" in first
        assert body == DENIAL_0930_VISIBILITY                     # byte-identical body (D-282)
        assert all(r.levelno == logging.ERROR for r in caplog.records if se.CAPABILITY_LOG_KEY in r.getMessage())
        assert se.screen_capability_claims(out, tool_use_count=0, entity="FNDR", cross_entity=True,
                                           founder=True) == out  # idempotent

    def test_enforce_redacts_the_symbol_not_the_sentence(self, observe, monkeypatch, caplog):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        caplog.set_level(logging.WARNING, logger=se.__name__)
        out = _screen(TOOLNAME_MENTION, count=1)
        assert out == TOOLNAME_MENTION.replace("cora_queue_code_session", se.INTERNAL_TOOL_REDACTION)

    def test_unknown_flag_value_reads_observe(self, observe, monkeypatch, caplog):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "true")
        caplog.set_level(logging.WARNING, logger=se.__name__)
        assert _screen(DENIAL_0935_SHIP) == DENIAL_0935_SHIP

    def test_link_tokens_are_masked_before_the_denial_runs(self, observe, caplog):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen("Here: <https://app.asana.com/x|I can't access the code queue from here>. All good.")
        assert _hits(caplog, "denial") == []

    def test_non_string_and_empty_pass_through(self, observe):
        assert _screen("") == "" and _screen(None) is None


# ── wiring ────────────────────────────────────────────────────────────────────

class TestWiring:
    def test_dispatch_qa_screens_at_all_three_sites_after_s2_and_before_the_cache_store(self):
        from cora import app
        src = inspect.getsource(app._dispatch_qa)
        assert src.count("slack_egress.screen_capability_claims(") == 3
        assert src.count("slack_egress.screen_phantom_write_claims(") == 3
        # every capability call follows the S2' call that precedes it
        idx_cap = [i for i in range(len(src)) if src.startswith("slack_egress.screen_capability_claims(", i)]
        idx_s2 = [i for i in range(len(src)) if src.startswith("slack_egress.screen_phantom_write_claims(", i)]
        for a, b in zip(idx_s2, idx_cap):
            assert a < b
        # and the two final-reply sites land before the cache store
        stores = [i for i in range(len(src)) if src.startswith("_try_cache_store(", i)]
        assert len(stores) >= 2 and idx_cap[1] < stores[0] and idx_cap[2] < stores[1]
        # the entity + founder flags are threaded (registry scoping is per channel)
        assert src.count("entity=entity, cross_entity=is_founder, founder=is_founder") == 3

    def test_class_level_sanitizer_does_not_run_the_screen(self):
        src = inspect.getsource(se.sanitize_text) + inspect.getsource(se._make_wrapper)
        assert "screen_capability_claims" not in src

    def test_third_rail_key_matches_the_log_key(self):
        from cora import egress_rails as er
        assert er.RAIL_CAPABILITY == se.CAPABILITY_LOG_KEY
        assert er.RAIL_CAPABILITY in er.RAIL_KEYS
        assert er._FIRING_RE[er.RAIL_CAPABILITY].search("phantom-capability-claim kind=denial phrase='x'")


# ── D-051 Code #13 review AD-4 / AD-5 / AD-6: honest sentences that named a family word ──
# Every sentence here tripped kind=denial on the branch tip (verified in observe mode);
# none is a denial of something the bot has in that channel.
BENIGN_FAMILY_WORDS_AD4 = [
    # (sentence, entity, founder, channel) -- the family word each one carried
    ("I can't check the stock at the Tucson store from here.", "OSN", False, "osn-leadership"),   # 'stock' <- cora_self_inventory
    ("I can't see the image you attached", "FNDR", True, "dm"),                                    # 'image' <- f3_generate_image
    ("I don't have access to the meeting notes from Friday's call.", "FNDR", True, "dm"),         # 'notes' <- cora_my_notes
    ("I can't open the deck you shared.", "FNDR", True, "dm"),                                     # 'deck' <- f3_create_sales_deck
    ("I don't have access to that Slack channel's history.", "F3E", False, "f3e-sales"),          # 'slack' <- slack_send_dm
    ("I can't see Larry's emails -- only your own mailbox is in scope for me.", "F3E", False, "f3e-sales"),  # 'mailbox' <- gmail_inbox
    ("I can't DM Tessa -- she isn't on the roster.", "F3E", False, "f3e-sales"),                  # 'dm' <- slack_send_dm
]
THIRD_PERSON_FACTS_AD5 = [
    "The Asana board is not set up for that project yet.",
    "Klaviyo isn't integrated with the CRM yet, so the list won't sync.",
    "The calendar invite is not available yet; Tessa hasn't sent it.",
    "Our inventory isn't available at the Anaheim warehouse until Friday.",
]
RULED_REFUSALS_AD6 = [
    "I can't pull P&L figures in this channel -- ask in #f3e-finance.",                         # Tier-3 finance refusal
    "I can't pull P&L figures in this channel -- ask in <#C0BAK65N4TA|hjr-finance>.",           # the same, Slack channel token
    "I can't see Larry's emails -- only your own mailbox is in scope for me.",                  # D-043 Tier-2 own-mailbox
    "That list is Harrison-only, so I can't share the open decisions here.",                    # founder-only scope
]
# ... while REAL denials of offered capabilities keep tripping (the rail is not blunted)
STILL_TRIP = [
    ("I don't have access to HubSpot, so I can't see the pipeline from here.", "F3E", False, "f3e-sales"),
    ("HubSpot isn't connected for me, so I can't see the pipeline.", "F3E", False, "f3e-sales"),   # AD-5 beneficiary anchor
    ("I'm not connected to HubSpot.", "F3E", False, "f3e-sales"),                                   # AD-5 bot-subject anchor
    ("I don't have a way to send a DM to Tommy.", "F3E", False, "f3e-sales"),                       # AD-4 compound alias
    ("I don't have a way to generate an image for that.", "F3E", False, "f3e-sales"),               # AD-4 compound alias
    ("I can't pull up my inbox from here.", "F3E", False, "f3e-sales"),                             # AD-4 compound alias
    ("I can't check the stock levels for the 12-pack.", "F3E", False, "f3e-sales"),                 # F3E DOES have inventory tools
]


class TestBenignFamilyWordsAD4:
    @pytest.mark.parametrize("sentence,entity,founder,channel", BENIGN_FAMILY_WORDS_AD4)
    def test_medium_words_and_attachments_never_trip(self, observe, caplog, sentence, entity, founder, channel):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        out = _screen(sentence, entity=entity, founder=founder, channel=channel, user="U_X")
        assert out == sentence and _hits(caplog, "denial") == [], sentence

    def test_self_inventory_tool_contributes_no_family(self):
        """cora_self_inventory is the inventory-of-SOURCES tool: OSN (no inventory tool
        since D-027) must not gain 'inventory' / 'stock' from its NAME."""
        osn = cs.capability_terms("OSN", cross_entity=False, founder=False)
        for t in ("inventory", "stock", "stock levels", "inventory levels"):
            assert t not in osn, t
        f3e = cs.capability_terms("F3E", cross_entity=False, founder=False)
        assert "inventory" in f3e and "stock levels" in f3e          # the f3e_*inventory* tools still carry it

    def test_bare_medium_aliases_are_gone_and_compounds_remain(self):
        f = cs.capability_terms("FNDR", cross_entity=True, founder=True)
        for bare in ("slack", "image", "images", "deck", "notes", "mailbox", "emails", "email", "inbox", "dm", "dms"):
            assert bare not in f, bare
        for compound in ("slack dm", "send a dm", "generate an image", "sales deck", "my notes", "my inbox", "gmail"):
            assert compound in f, compound


class TestThirdPersonFactsAD5:
    @pytest.mark.parametrize("sentence", THIRD_PERSON_FACTS_AD5)
    def test_business_facts_about_external_systems_never_trip(self, observe, caplog, sentence):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        out = _screen(sentence, entity="F3E", founder=False, channel="f3e-sales", user="U_TOMMY")
        assert out == sentence and _hits(caplog, "denial") == [], sentence
        assert se._DENIAL_RE.search(sentence) is None                 # no bot-subject -> not a denial phrase at all

    def test_not_connected_growth_shape_is_linear(self):
        import time
        for adv in ("I am not " + " " * 40_000 + "x", "is not " + "currently " * 5_000, "my tools " * 5_000):
            t0 = time.perf_counter()
            se._DENIAL_RE.search(adv)
            assert time.perf_counter() - t0 < 0.2, adv[:20]


class TestRuledRefusalsExemptAD6:
    @pytest.mark.parametrize("sentence", RULED_REFUSALS_AD6)
    def test_tier_and_scope_refusals_are_exempt_by_shape(self, observe, caplog, sentence):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        out = _screen(sentence, entity="F3E", founder=False, channel="f3e-sales", user="U_TOMMY")
        assert out == sentence and _hits(caplog, "denial") == [], sentence

    def test_enforce_never_prepends_a_try_hint_under_a_tier_refusal(self, observe, monkeypatch, caplog):
        """The whole point: enforce must not invite the finance ask in the Tier-3 channel."""
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        text = RULED_REFUSALS_AD6[0]
        assert _screen(text, entity="F3E", founder=False, channel="f3e-sales", user="U_TOMMY") == text
        assert se.CAPABILITY_HONEST_TEMPLATE not in text

    def test_docstring_states_the_exemption(self):
        assert "EXEMPT BY SHAPE" in (se.screen_capability_claims.__doc__ or "")

    def test_ruled_refusal_growth_shape_is_linear(self):
        import time
        for adv in ("ask in " + " " * 40_000 + "x", "this channel " * 4_000, "in #" + "a" * 40_000, "own " * 10_000):
            t0 = time.perf_counter()
            se._RULED_REFUSAL_RE.search(adv)
            assert time.perf_counter() - t0 < 0.2, adv[:20]


class TestRealDenialsStillTripAfterAD456:
    @pytest.mark.parametrize("sentence,entity,founder,channel", STILL_TRIP)
    def test_offered_capability_denials_trip_once(self, observe, caplog, sentence, entity, founder, channel):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _screen(sentence, entity=entity, founder=founder, channel=channel, user="U_X")
        assert len(_hits(caplog, "denial")) == 1, sentence
