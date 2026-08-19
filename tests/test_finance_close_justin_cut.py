"""Justin's necessity-gated close-pack DM (cq-f330d402e5cd part a).

He was DM'd the entire pack -- nine sections, flagged and clean alike -- and had
to work out for himself which parts were addressed to him ("scattered
information... my eyes start to roll", 8/18 Finance x Cora). Nothing is deleted:
the full pack still goes to #hjrg-finance, and the same computed lines are ROUTED
by whether they ask something of him.

PART (b) OF THE SLICE IS DELIBERATELY NOT BUILT -- see TestPartBPremise.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cp():
    path = _REPO_ROOT / "scripts" / "run_finance_close_pack.py"
    spec = importlib.util.spec_from_file_location("close_pack_justin", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["close_pack_justin"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Section:
    """Duck-typed like finance_close.Section on purpose -- the builder must not
    isinstance-check, because this repo runs `cora.*` and `src.cora.*` as
    distinct module objects and a type mismatch would silently shorten the list."""

    def __init__(self, key, title, lines=None, available=True, stub_reason=None,
                 is_partial=False):
        self.key = key
        self.title = title
        self.lines = lines or []
        self.available = available
        self.stub_reason = stub_reason
        self.is_partial = is_partial


class _Pack:
    def __init__(self, sections):
        self.generated_at = "2026-08-19 07:15 AZ"
        self.sections = sections
        self.total_flags = 0


class TestNecessityGate:
    def test_action_sections_reach_him_with_a_purpose_line(self, cp):
        pack = _Pack([_Section("cash", "Cash", ["• F3E: sheet 100, books 90"])])
        out = cp.build_justin_cut(pack)
        assert "Cash" in out
        assert "sheet 100, books 90" in out
        assert "Check these against the bank" in out

    def test_clean_non_action_sections_do_not_reach_him(self, cp):
        """This is the whole fix: an informational all-clear is a record for the
        channel, not an ask for his DM."""
        pack = _Pack([_Section("pnl", "P&L sanity", ["• F3E: expenses flat"])])
        out = cp.build_justin_cut(pack)
        assert "P&L sanity" not in out
        assert "Nothing in this week's pack needs an action from you" in out

    def test_a_flagged_non_action_section_still_rides_along(self, cp):
        """A flag is Cora saying "look at this", which earns a place even in an
        action-only message -- but only the flagged LINES, not the whole body."""
        pack = _Pack([_Section("pnl", "P&L sanity", [
            "• F3E: expenses flat",
            ":triangular_flag_on_post: OSN: expenses +212% MoM",
        ])])
        out = cp.build_justin_cut(pack)
        assert "expenses +212% MoM" in out
        assert "expenses flat" not in out
        assert "Not an action item" in out

    def test_gating_is_on_action_not_on_flags(self, cp):
        """Flags mark ANOMALIES, not actions, and the two come apart in BOTH
        directions. The Monday worksheet carries no flag and is the one thing he
        must do every week; a P&L variance flag is information. Gating on flags
        would have inverted exactly this pair."""
        pack = _Pack([
            _Section("close_prep", "Close-prep notes", ["• reconcile OSN petty cash"]),
            _Section("pnl", "P&L sanity", ["• all within tolerance"]),
        ])
        out = cp.build_justin_cut(pack)
        assert "reconcile OSN petty cash" in out
        assert "P&L sanity" not in out

    def test_an_unavailable_action_section_is_itself_actionable(self, cp):
        """Being told a check he relies on did not run is an action item; a
        silently missing section reads as "nothing to do there"."""
        pack = _Pack([_Section("cash", "Cash", available=False,
                               stub_reason="gsheets 403")])
        out = cp.build_justin_cut(pack)
        assert "Couldn't run this week" in out
        assert "gsheets 403" in out

    def test_an_unavailable_non_action_section_stays_in_the_channel(self, cp):
        pack = _Pack([_Section("pnl", "P&L sanity", available=False,
                               stub_reason="QBO 401")])
        assert "P&L sanity" not in cp.build_justin_cut(pack)

    def test_partial_coverage_travels_with_the_action_list(self, cp):
        """An action list that omits "this ran on 3 of 10 entities" reads as a
        COMPLETE list of what needs doing -- the same failure class as an
        all-clear that was never checked."""
        pack = _Pack([_Section("cash", "Cash", ["• F3E: ok"], is_partial=True)])
        out = cp.build_justin_cut(pack)
        assert "partial list, not a complete one" in out

    def test_always_points_at_where_the_rest_is(self, cp):
        out = cp.build_justin_cut(_Pack([]))
        assert "#hjrg-finance" in out

    def test_no_figure_is_recomputed(self, cp):
        """A deterministic slice: every number in the DM appears verbatim in the
        section it came from."""
        lines = ["• OSN: sheet 1,234.56 vs books (987.65)"]
        out = cp.build_justin_cut(_Pack([_Section("cash", "Cash", lines)]))
        assert "1,234.56" in out and "(987.65)" in out

    def test_a_section_with_no_title_is_skipped_not_crashed(self, cp):
        class Broken:
            key = "cash"
            title = None
        assert cp.build_justin_cut(_Pack([Broken()]))


class TestClassificationCompleteness:
    def test_every_section_build_pack_emits_is_classified(self):
        """A new section defaulting into his DM is the SAFE failure; a new
        section silently vanishing from the one person who acts on this pack is
        not. This pin makes the default loud rather than permanent."""
        import importlib.util as iu
        path = _REPO_ROOT / "scripts" / "run_finance_close_pack.py"
        spec = iu.spec_from_file_location("close_pack_keys", path)
        mod = iu.module_from_spec(spec)
        sys.modules["close_pack_keys"] = mod
        spec.loader.exec_module(mod)

        src = (_REPO_ROOT / "src" / "cora" / "finance_close.py").read_text(
            encoding="utf-8")
        import re
        emitted = set(re.findall(r'key="([a-z_]+)"', src))
        missing = sorted(emitted - set(mod.SECTION_PURPOSE))
        assert missing == [], (
            f"close-pack sections with no SECTION_PURPOSE entry: {missing}")

    def test_unclassified_defaults_to_included(self, cp):
        assert cp.justin_needs("a_section_invented_tomorrow") is True

    def test_every_action_section_has_a_purpose_sentence(self, cp):
        for key, (needs, purpose) in cp.SECTION_PURPOSE.items():
            if needs:
                assert purpose.strip(), f"{key} has no plain-language purpose"
                assert len(purpose) < 260, f"{key} purpose is not one line"


class TestDeliveryWiring:
    def test_the_dm_sends_the_cut_and_the_channel_gets_the_full_pack(self):
        src = (_REPO_ROOT / "scripts" / "run_finance_close_pack.py").read_text(
            encoding="utf-8")
        assert "dm_user(client, JUSTIN_SLACK_ID, justin)" in src
        assert "post_to_channel(client, HJRG_FINANCE_CHANNEL, full)" in src

    def test_the_scoped_run_banner_reaches_the_dm_too(self):
        """A scoped run's DM must not read as the whole portfolio."""
        src = (_REPO_ROOT / "scripts" / "run_finance_close_pack.py").read_text(
            encoding="utf-8")
        assert "banner + justin" in src

    def test_the_finance_surface_allowlist_is_untouched(self, cp):
        assert set(cp.FINANCE_SURFACES) == {cp.HJRG_FINANCE_CHANNEL,
                                            cp.FOUNDER_FINANCE_CHANNEL}
        assert cp.ARCHIVED_HJR_FINANCE not in cp.FINANCE_SURFACES


class TestPartBPremise:
    """Part (b) -- one-tap cards for the candidate lane -- is NOT built.

    Two independent findings, both verified against live config rather than
    reasoned about, and both recorded here so the next session does not
    re-derive them.
    """

    def test_forecast_delta_cards_stay_unbuilt_because_f9_is_still_parked(self):
        """The kickoff made forecast-delta cards CONDITIONAL on F9 un-parking
        and said to check before building UX for a dead lane. Checked: still
        parked."""
        src = (_REPO_ROOT / "scripts" / "run_finance_close_pack.py").read_text(
            encoding="utf-8")
        assert "build_forecast_confirm" not in src
        assert "forecast_delta_card" not in src

    def test_intercompany_pairings_are_not_a_one_bit_decision(self):
        """A Confirm/Cancel card captures ONE bit. A confirmed pairing needs
        THREE values -- left account, right account, and `opposite_signs` -- and
        the map's own header states the sign convention CANNOT be inferred. So a
        one-tap card cannot complete a pairing; it could only ever triage.

        Building it as specified would produce 32 cards that each collect a
        fraction of what the config needs, and (per confirm_cards' own design
        note) one card per Slack message with MAX_ITEM_CARDS = 6 -- so 32 would
        be five times the cap and 32 separate DMs. Worse than the list it
        replaces. Recorded, not built.
        """
        cfg = yaml.safe_load(
            (_REPO_ROOT / "data" / "maps" / "qbo-intercompany-accounts.yaml")
            .read_text(encoding="utf-8"))
        assert cfg["pairs"] == [], "pairs are seeded EMPTY on purpose (D-118)"

        from cora import confirm_cards
        assert confirm_cards.MAX_ITEM_CARDS == 6

    def test_the_intercompany_section_still_states_its_own_unconfirmed_status(self):
        """Until pairing is solved properly, the DM must keep saying these are
        unconfirmed -- an action list implying they were checked is worse than
        one that admits they were not."""
        src = (_REPO_ROOT / "src" / "cora" / "finance_close.py").read_text(
            encoding="utf-8")
        assert "UNCONFIRMED pairing" in src
