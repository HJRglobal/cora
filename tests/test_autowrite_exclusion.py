"""Per-person auto-write opt-out, and Sara Fonseca's Asana mapping.

Two Harrison rulings from 2026-08-18/19 that share one file:

  * `autowrite_excluded: true` separates "this person is ACTIVE" from "this
     person's claims may write canon". Activating a registry user used to
     confer both, because `contributor_recognized` keys on presence in the
     registry alone.
  * Sara's live Asana GID, carried manually because the code queue's
    LEX-sensitivity guard refuses named-person seeds (cq-795b9caa0b3a).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from cora import graduated_trust_shadow as gts
from cora import org_roles

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ORG_ROLES = _REPO_ROOT / "data" / "maps" / "org-roles.yaml"
_SLACK_TO_ASANA = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"

TESSA = "U0B3KH5UZJ7"
SARA = "U0B9JS3JW07"
HANNAH_ENTITY = "HJRG"


def _raw_users() -> list[dict]:
    return yaml.safe_load(_ORG_ROLES.read_text(encoding="utf-8"))["users"]


def _entry(slack_id: str) -> dict:
    for u in _raw_users():
        if u.get("slack_id") == slack_id:
            return u
    raise AssertionError(f"{slack_id} missing from org-roles.yaml")


class TestAutowriteExclusionMechanism:
    def test_loader_parses_the_key(self):
        """A key absent from org_roles._parse is SILENTLY IGNORED -- the YAML
        would look correct while conferring the authority anyway."""
        rec = org_roles.get_role(TESSA)
        assert rec is not None
        assert rec.autowrite_excluded is True

    def test_default_is_false_for_everyone_else(self):
        excluded = [u["name"] for u in _raw_users() if u.get("autowrite_excluded")]
        assert excluded == ["Tessa Miller"], (
            f"unexpected auto-write exclusions: {excluded}")
        harrison = org_roles.get_role("U0B2RM2JYJ1")
        assert harrison.autowrite_excluded is False

    def test_excluded_contributor_is_not_recognized(self):
        for entity in ("HJRG", "F3E", "OSN", "HJRP"):
            assert gts.contributor_recognized(TESSA, entity) is False, entity

    def test_exclusion_does_not_make_her_external(self):
        """`external: true` would have been the lazy way to do this and it is
        wrong: it changes routing and proactive comms, and misdescribes an
        internal teammate."""
        rec = org_roles.get_role(TESSA)
        assert rec.external is False
        assert "external" not in _entry(TESSA)

    def test_delegated_work_eligibility_is_kept(self):
        """DW eligibility is `get_role(...) is not None` -- that grant was
        reviewed and wanted, and the ruling keeps it."""
        assert org_roles.get_role(TESSA) is not None

    def test_a_recognized_teammate_is_still_recognized(self):
        """The guard must exclude one person, not break the mechanism."""
        assert gts.contributor_recognized("U0B2RM2JYJ1", "HJRG") is True

    def test_tier0_is_unreachable_for_an_excluded_contributor(self):
        """End-to-end through classify_tier, not just the predicate: TIER_0
        needs corroborated AND allowlisted AND recognized_teammate."""
        kwargs = dict(
            coras_read_verdict="CORROBORATED",
            category=sorted(gts.ALLOWLIST_CATEGORIES)[0],
            entity="HJRG",
            claim_text="the office printer is on the second floor",
        )
        tier, decision, _ = gts.classify_tier(contributor_id=TESSA, **kwargs)
        assert tier != gts.TIER_0
        assert decision != gts.DECISION_AUTO

        # Positive control: the SAME claim from a non-excluded teammate must
        # still reach Tier 0, or this test would pass on a broken classifier.
        tier_ok, decision_ok, _ = gts.classify_tier(
            contributor_id="U0B2RM2JYJ1", **kwargs)
        assert (tier_ok, decision_ok) == (gts.TIER_0, gts.DECISION_AUTO)


class TestExclusionIsDocumented:
    def test_key_is_documented_in_the_yaml_header(self):
        raw = _ORG_ROLES.read_text(encoding="utf-8")
        header = raw.split("users:", 1)[0]
        assert "autowrite_excluded" in header

    def test_revisit_stamp_is_a_comment_not_an_injected_note(self):
        """D-193: `notes` is injected into the LLM context on every reply and a
        reply built on it is storable in the entity-keyed semantic cache, so a
        dated internal review stamp must never live there."""
        raw = _ORG_ROLES.read_text(encoding="utf-8")
        comments = "\n".join(
            ln for ln in raw.splitlines() if ln.lstrip().startswith("#"))
        assert "2026-08-24" in comments

        notes = (org_roles.get_role(TESSA).notes or "").lower()
        for banned in ("2026-08-24", "re-eval", "auto-write", "autowrite",
                       "excluded"):
            assert banned not in notes, f"{banned!r} leaked into injected notes"


class TestSaraAsanaMapping:
    def test_gid_is_mapped(self):
        users = yaml.safe_load(_SLACK_TO_ASANA.read_text(encoding="utf-8"))["users"]
        row = next(u for u in users if u["slack_user_id"] == SARA)
        assert str(row["asana_user_gid"]) == "1215786741942728"
        assert row["display_name"] == "Sara Fonseca"

    def test_no_google_identity_is_asserted(self):
        """`asana_email` doubles as the Google identity Cora impersonates for
        gmail_create_draft (the SENDER) and calendar. An Asana roster lookup is
        not evidence of mailbox ownership -- the Aaron precedent."""
        users = yaml.safe_load(_SLACK_TO_ASANA.read_text(encoding="utf-8"))["users"]
        row = next(u for u in users if u["slack_user_id"] == SARA)
        assert "asana_email" not in row

    def test_still_in_the_registry_and_still_not_a_phi_custodian(self):
        rec = org_roles.get_role(SARA)
        assert rec is not None
        assert rec.entity == "LEX-LLC"
        assert "not a phi custodian" in (rec.notes or "").lower()

    def test_the_stale_no_mapping_claim_is_gone(self):
        """The note claimed "No Asana/Google mapping yet" -- injected context
        that is now false, and would have Cora tell people she cannot resolve
        her."""
        notes = (org_roles.get_role(SARA).notes or "").lower()
        assert "no asana" not in notes
        raw = _SLACK_TO_ASANA.read_text(encoding="utf-8")
        assert "Sara Fonseca -- LLC marketing freelancer. No Asana" not in raw

    def test_gid_is_not_in_the_injected_note(self):
        """D-193 again: identifiers belong in the map, not in injected prose."""
        assert "1215786741942728" not in (org_roles.get_role(SARA).notes or "")
