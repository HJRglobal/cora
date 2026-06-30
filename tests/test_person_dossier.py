"""Tests for cora.tools.person_dossier -- the dossier pull/scrub/synthesize/write-back.

Gate (build spec section 8):
  - Access gate: founder->any allowed; self allowed; peer->refused with NO target leak.
  - LEX-staff PHI scrub: clinical / named-billing dropped; staff names preserved.
  - Exclusions: Demi personal mailbox skipped; Alina Maricopa meetings excluded; Jason limited.
  - Fail-soft: a source raising -> stub, dossier still renders.
  - Fireflies dedupe collapses duplicate meeting copies.
  - HubSpot stage-GID -> label resolution (via the label-resolving formatter).
  - Write-back replaces "Recent involvements", preserves "Durable notes", normalizes Tag->Cora.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.person_identity import PersonIdentity  # noqa: E402
from cora.tools import meeting_actions as ma  # noqa: E402
from cora.tools import person_dossier as pd  # noqa: E402

FOUNDER = "U0B2RM2JYJ1"          # Harrison
TOMMY = "U0B3RU5Q55G"
SHAUN = "U0B3PS82G30"


# ── fakes ───────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, text):
        self.content = [type("C", (), {"text": text})()]


class _FakeMessages:
    def __init__(self, outer):
        self._o = outer

    def create(self, **kw):
        self._o.calls.append(kw)
        return _FakeResp(self._o.text)


class FakeClient:
    """Stand-in for anthropic.Anthropic; .messages.create returns a fixed body."""
    def __init__(self, text):
        self.text = text
        self.calls = []

    @property
    def messages(self):
        return _FakeMessages(self)


def _mk(slug="tommy-anderson", name="Tommy Anderson", entity="F3E", **kw) -> PersonIdentity:
    base = dict(
        slack_id="U_T", name=name, slug=slug, role="Sales Lead", entity=entity,
        primary_email="tommy@f3energy.com", all_emails=["tommy@f3energy.com"],
        mailboxes=["tommy@f3energy.com"], asana_gid="123", hubspot_owner_id=None,
    )
    base.update(kw)
    return PersonIdentity(**base)


def _ms(y, m, d):
    from datetime import datetime, timezone
    return int(datetime(y, m, d, 17, 0, tzinfo=timezone.utc).timestamp() * 1000)


# ── access gate ───────────────────────────────────────────────────────────────

def test_access_self_resolves_own_identity():
    target, refusal = pd.resolve_access(TOMMY, "", FOUNDER)
    assert refusal is None
    assert target is not None and target.slug == "tommy-anderson"


def test_access_founder_can_profile_any_named_teammate():
    target, refusal = pd.resolve_access(FOUNDER, "Shaun", FOUNDER)
    assert refusal is None
    assert target is not None and target.slug == "shaun-hawkins"


def test_access_peer_profiling_refused_with_no_target_leak():
    target, refusal = pd.resolve_access(TOMMY, "Shaun", FOUNDER)
    assert target is None                 # never resolved
    assert refusal is not None
    # The refusal must NOT confirm/deny the named target's existence.
    assert "shaun" not in refusal.lower()


def test_access_founder_unknown_name_graceful():
    target, refusal = pd.resolve_access(FOUNDER, "Nobody McNobody", FOUNDER)
    assert target is None and refusal is not None


# ── LEX PHI wall ────────────────────────────────────────────────────────────────

def test_scrub_lex_block_redacts_client_preserves_staff():
    with patch.object(pd, "_staff_names", return_value={"Shaun Hawkins", "Jen Mortensen"}):
        out = pd._scrub_lex_block(
            "Shaun reviewed client Madison's care plan; diagnosed with autism."
        )
    assert "Shaun" in out                 # staff preserved
    assert "Madison" not in out           # client name redacted
    assert "autism" not in out.lower()    # diagnosis redacted


def test_phi_wall_lex_drops_clinical():
    p = _mk(slug="shaun-hawkins", name="Shaun Hawkins", entity="LEX-LLC", lex_staff=True)
    assert pd._phi_wall(p, "Headline: supported DTA staffing and van logistics.") is not None
    assert pd._phi_wall(p, "A member was diagnosed with autism this week.") is None


def test_phi_wall_nonlex_clinical_backstop_drops():
    p = _mk()  # non-lex F3E
    # clinical PHI from a mis-tagged source must still drop for a non-LEX target
    assert pd._phi_wall(p, "Patient prescribed risperidone.") is None
    # ordinary commercial billing language is NOT dropped (no care-program cue)
    assert pd._phi_wall(p, "Sent Whole Foods their invoice for the PO.") is not None


def test_build_lex_staff_model_leak_is_dropped_not_written(monkeypatch):
    """If synthesis leaks clinical PHI for a LEX-staff target, the wall drops it:
    nothing is surfaced and nothing is written."""
    p = _mk(slug="shaun-hawkins", name="Shaun Hawkins", entity="LEX-LLC", lex_staff=True)
    monkeypatch.setattr(pd, "_gmail_block", lambda p, days: ("ok", "scrubbed email signal"))
    for fn in ("_fireflies_block",):
        monkeypatch.setattr(pd, fn, lambda p, days: ("empty", ""))
    for fn in ("_asana_block", "_hubspot_block", "_calendar_block", "_drive_block"):
        monkeypatch.setattr(pd, fn, lambda p: ("skipped", ""))
    leaky = FakeClient("A client was diagnosed with autism and prescribed risperidone.")
    res = pd.build_dossier(p, client=leaky, write_back_enabled=False)
    assert res.phi_dropped is True
    assert res.body is None and res.written is False


# ── exclusions ──────────────────────────────────────────────────────────────────

def test_demi_personal_mailbox_skipped():
    demi = _mk(slug="demi-bagby", name="Demi Bagby", entity="BDM",
               mailboxes=[], primary_email="", exclude_personal_mailbox=True,
               all_emails=["bigd@bigd.media"])
    assert pd._gmail_block(demi, 14) == ("skipped", "")
    assert pd._calendar_block(demi) == ("skipped", "")


def test_alina_maricopa_meetings_excluded(monkeypatch):
    alina = _mk(slug="alina-thomas", name="Alina Thomas", entity="BDM",
                all_emails=["alina@hjrglobal.com"], exclude_maricopa=True,
                mailboxes=["alina@hjrglobal.com"])
    maricopa = {
        "id": "M1", "title": "Maricopa County Budget Class", "date": _ms(2026, 6, 18),
        "meeting_link": "l1", "participants": ["alina@hjrglobal.com"],
        "summary": {"short_summary": "class logistics", "action_items": ""},
        "meeting_attendees": [{"displayName": "Alina", "email": "alina@hjrglobal.com"}],
    }
    normal = {
        "id": "N1", "title": "BDM Content Weekly", "date": _ms(2026, 6, 19),
        "meeting_link": "l2", "participants": ["alina@hjrglobal.com"],
        "summary": {"short_summary": "edited the F3 reels", "action_items": ""},
        "meeting_attendees": [{"displayName": "Alina", "email": "alina@hjrglobal.com"}],
    }
    monkeypatch.setattr(ma, "_recent_transcripts", lambda emails: [maricopa, normal])
    monkeypatch.setattr(ma, "_dedup_meetings", lambda ts: list(ts))
    monkeypatch.setattr(ma, "_classify_meeting", lambda t: ("BDM", False))
    status, text = pd._fireflies_block(alina, 14)
    assert status == "ok"
    assert "BDM Content Weekly" in text
    assert "Maricopa" not in text


def test_jason_external_limited():
    # External consultant: no DWD mailbox, no Asana, no HubSpot -> the internal
    # sources are all skipped and the dossier naturally narrows to engagement scope.
    jason = _mk(slug="jason-dorfman", name="Jason Dorfman", entity="F3E", external=True,
                mailboxes=[], primary_email="jasrdorfman@gmail.com",
                all_emails=["jasrdorfman@gmail.com"], asana_gid=None, hubspot_owner_id=None)
    assert jason.external is True
    assert pd._gmail_block(jason, 14) == ("skipped", "")      # no DWD mailbox
    assert pd._asana_block(jason) == ("skipped", "")           # no Asana account
    assert pd._hubspot_block(jason) == ("skipped", "")         # no HubSpot owner
    assert pd._calendar_block(jason) == ("skipped", "")        # no impersonable mailbox


# ── fail-soft + dedupe + hubspot ────────────────────────────────────────────────

def test_fail_soft_one_dead_source_still_renders(monkeypatch):
    p = _mk()
    def _boom(p, days=None):
        raise RuntimeError("asana down")
    monkeypatch.setattr(pd, "_gmail_block", lambda p, days: ("ok", "email signal line"))
    monkeypatch.setattr(pd, "_fireflies_block", lambda p, days: ("empty", ""))
    monkeypatch.setattr(pd, "_asana_block", lambda p: (_ for _ in ()).throw(RuntimeError("asana down")))
    monkeypatch.setattr(pd, "_hubspot_block", lambda p: ("skipped", ""))
    monkeypatch.setattr(pd, "_calendar_block", lambda p: ("skipped", ""))
    monkeypatch.setattr(pd, "_drive_block", lambda p: ("pending", ""))
    res = pd.build_dossier(p, client=FakeClient("Headline: shipped things.\n- did work"),
                           write_back_enabled=False)
    assert res.coverage["Tasks"] == "error"      # the crashing source
    assert res.coverage["Email"] == "ok"
    assert res.body is not None                   # dossier still synthesized


def test_assemble_sources_parallel_preserves_order_and_coverage(monkeypatch):
    """Sources run concurrently (2026-06-30 fix for the 25s dispatch timeout), but the
    assembled blocks must stay in canonical _SOURCE_SPECS order, not completion order,
    and coverage must reflect each source's status."""
    p = _mk()
    monkeypatch.setattr(pd, "_gmail_block", lambda p, days: ("ok", "EMAIL_BLOCK"))
    monkeypatch.setattr(pd, "_fireflies_block", lambda p, days: ("ok", "MEETINGS_BLOCK"))
    monkeypatch.setattr(pd, "_asana_block", lambda p: ("empty", ""))
    monkeypatch.setattr(pd, "_hubspot_block", lambda p: ("ok", "DEALS_BLOCK"))
    monkeypatch.setattr(pd, "_calendar_block", lambda p: ("error", ""))
    monkeypatch.setattr(pd, "_drive_block", lambda p: ("pending", ""))
    signals, coverage = pd._assemble_sources(p, 14)
    assert signals.index("EMAIL_BLOCK") < signals.index("MEETINGS_BLOCK") < signals.index("DEALS_BLOCK")
    assert "Tasks" not in signals and "(empty" not in signals  # empty source omitted from blocks
    assert coverage == {
        "Email": "ok", "Meetings": "ok", "Tasks": "empty",
        "Deals": "ok", "Calendar": "error", "Docs": "pending",
    }


def test_fireflies_dedupe_collapses_duplicate_copies(monkeypatch):
    p = _mk()
    dup_a = {
        "id": "A", "title": "F3 Weekly", "date": _ms(2026, 6, 19), "meeting_link": "same-link",
        "participants": ["tommy@f3energy.com"],
        "summary": {"short_summary": "short", "action_items": "do x"},
        "meeting_attendees": [{"displayName": "Tommy", "email": "tommy@f3energy.com"}],
    }
    dup_b = dict(dup_a)
    dup_b["id"] = "B"
    dup_b["summary"] = {"short_summary": "a longer more complete summary", "action_items": "do x and y and z"}
    monkeypatch.setattr(ma, "_recent_transcripts", lambda emails: [dup_a, dup_b])
    monkeypatch.setattr(ma, "_classify_meeting", lambda t: ("F3E", False))
    status, text = pd._fireflies_block(p, 14)
    assert status == "ok"
    assert text.count("F3 Weekly") == 1          # the two copies collapsed to one


def test_hubspot_block_uses_label_resolving_formatter(monkeypatch):
    p = _mk(hubspot_owner_id="162944825")
    monkeypatch.setattr(pd.hubspot_client, "get_owner_deals", lambda owner, pipeline_id=None: [{"id": "d1"}])
    monkeypatch.setattr(
        pd.hubspot_client, "format_deals_for_llm",
        lambda deals, **kw: "ACTIVE PIPELINE: 1 deal\n- Whole Foods: Proposal ($25,000)",
    )
    status, text = pd._hubspot_block(p)
    assert status == "ok"
    assert "Proposal" in text                    # stage GID resolved to its label


def test_hubspot_block_skipped_when_no_owner():
    assert pd._hubspot_block(_mk(hubspot_owner_id=None)) == ("skipped", "")


# ── write-back ──────────────────────────────────────────────────────────────────

_SEED = """# Tommy Anderson — involvement dossier

**Access:** Founder (Harrison) + Tommy only. NOT peer-visible. Work-involvement only — not personal life.

## Identity keys
- Role / entity: F3E Sales Lead · F3E

## Profile
F3 Energy sales lead.

## Recent involvements (auto-refreshed by Tag)
_Populated when Harrison checks in on Tommy, or by the weekly involvement refresh._

(none yet)

## Durable notes
- A durable handling fact that must survive.
"""


def test_write_back_replaces_section_preserves_durable_normalizes_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PEOPLE_DIR", str(tmp_path))
    f = tmp_path / "tommy-anderson.md"
    f.write_text(_SEED, encoding="utf-8")
    p = _mk()
    ok = pd.write_back(p, "Headline: ran the retail pipeline.\n- 16 proposals out", as_of="2026-06-30")
    assert ok is True
    out = f.read_text(encoding="utf-8")
    assert "auto-refreshed by Cora" in out          # Tag -> Cora
    assert "auto-refreshed by Tag" not in out
    assert "ran the retail pipeline" in out          # new body present
    assert "**As of 2026-06-30**" in out
    assert "(none yet)" not in out                   # old placeholder replaced
    assert "## Durable notes" in out                 # preserved
    assert "A durable handling fact that must survive." in out
    assert "## Profile" in out and "F3 Energy sales lead." in out  # pre-section preserved


def test_write_back_missing_file_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PEOPLE_DIR", str(tmp_path))
    assert pd.write_back(_mk(slug="nobody"), "body") is False


def test_write_back_empty_body_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PEOPLE_DIR", str(tmp_path))
    (tmp_path / "tommy-anderson.md").write_text(_SEED, encoding="utf-8")
    assert pd.write_back(_mk(), "   ") is False


def test_gmail_per_mailbox_scrub_isolates_lex_box(monkeypatch):
    """REGRESSION (PHI HIGH): a non-lex_staff target whose LEX mailbox is NOT first in
    the list must still have that mailbox's content scrubbed before synthesis -- the
    single-scrub-by-mailboxes[0] bug would have leaked it."""
    justin = _mk(slug="justin-moran", name="Justin Moran", entity="HJRG", lex_staff=False,
                 primary_email="justin@hjrglobal.com",
                 mailboxes=["justin@hjrglobal.com", "justin@lexingtonservices.com"],
                 all_emails=["justin@hjrglobal.com", "justin@lexingtonservices.com"])

    def fake_inbox(email, query=None, max_results=None):
        if "lexingtonservices" in email:
            return [{"subject": "client Madison diagnosed with autism - billing follow-up",
                     "from": "x@x.com", "to": "", "date_ts": None}]
        return [{"subject": "Q3 budget review", "from": "hayden@visibilitycpa.com",
                 "to": "", "date_ts": None}]

    monkeypatch.setattr(pd.gmail_reader, "get_inbox_summary", fake_inbox)
    monkeypatch.setattr(pd, "_staff_names", lambda: {"Justin Moran"})
    status, text = pd._gmail_block(justin, 14)
    assert status == "ok"
    assert "Q3 budget review" in text          # non-LEX mailbox passes through unredacted
    assert "Madison" not in text                # LEX mailbox client name scrubbed
    assert "autism" not in text.lower()         # diagnosis scrubbed


def test_fireflies_drops_lbhs_by_attendee_domain(monkeypatch):
    """REGRESSION: a generically-titled meeting with a @lexingtonbhs.com attendee is
    dropped (42 CFR Part 2) even when the classifier reports non-LEX."""
    p = _mk()
    lbhs = {
        "id": "L", "title": "Weekly Sync", "date": _ms(2026, 6, 18), "meeting_link": "l",
        "participants": [], "summary": {"short_summary": "client progress", "action_items": ""},
        "meeting_attendees": [{"displayName": "Jen", "email": "jen@lexingtonbhs.com"}],
    }
    monkeypatch.setattr(ma, "_recent_transcripts", lambda emails: [lbhs])
    monkeypatch.setattr(ma, "_dedup_meetings", lambda ts: list(ts))
    monkeypatch.setattr(ma, "_classify_meeting", lambda t: ("FNDR", False))  # mis-tag as non-LEX
    status, text = pd._fireflies_block(p, 14)
    assert status == "empty"                    # the only meeting was dropped


def test_handler_dm_only_surface_gate(monkeypatch):
    """REGRESSION (peer-wall MED): the dossier renders ONLY in a DM. A non-DM channel
    refuses-and-redirects WITHOUT building; a DM builds. Access gate still runs first."""
    from cora.tools import tool_dispatch as td
    built: dict = {}

    def fake_build(target, **kw):
        built["slug"] = target.slug
        return pd.DossierResult(target.slug, "THE REPLY")

    monkeypatch.setattr(pd, "build_dossier", fake_build)

    # Self check-in in a shared channel -> redirect, no build.
    out = td._tool_cora_person_dossier(TOMMY, "F3E", {"_channel_name": "f3e-leadership"})
    assert "built" not in built and "slug" not in built
    assert "DM me" in out

    # Self check-in in a DM -> builds and returns the reply.
    out2 = td._tool_cora_person_dossier(TOMMY, "F3E", {"_channel_name": "dm"})
    assert built.get("slug") == "tommy-anderson"
    assert out2 == "THE REPLY"

    # Peer naming a teammate in a DM -> STILL refused by the access gate (no leak),
    # and the surface gate is never reached.
    built.clear()
    out3 = td._tool_cora_person_dossier(TOMMY, "F3E", {"_channel_name": "dm", "person": "Shaun"})
    assert "slug" not in built
    assert "shaun" not in out3.lower()


def test_source_specs_all_resolve_to_callables():
    """Guard against a typo'd _SOURCE_SPECS name silently becoming 'unavailable' forever."""
    for label, fname, _takes_days in pd._SOURCE_SPECS:
        fn = getattr(pd, fname, None)
        assert callable(fn), f"_SOURCE_SPECS entry {label!r} -> {fname!r} is not a callable"


def test_build_writes_back_on_clean_synthesis(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PEOPLE_DIR", str(tmp_path))
    (tmp_path / "tommy-anderson.md").write_text(_SEED, encoding="utf-8")
    p = _mk()
    monkeypatch.setattr(pd, "_gmail_block", lambda p, days: ("ok", "email signal"))
    monkeypatch.setattr(pd, "_fireflies_block", lambda p, days: ("empty", ""))
    monkeypatch.setattr(pd, "_asana_block", lambda p: ("empty", ""))
    monkeypatch.setattr(pd, "_hubspot_block", lambda p: ("skipped", ""))
    monkeypatch.setattr(pd, "_calendar_block", lambda p: ("skipped", ""))
    monkeypatch.setattr(pd, "_drive_block", lambda p: ("pending", ""))
    res = pd.build_dossier(p, client=FakeClient("Headline: did the work.\n- a bullet"),
                           write_back_enabled=True)
    assert res.written is True
    assert "did the work" in (tmp_path / "tommy-anderson.md").read_text(encoding="utf-8")
