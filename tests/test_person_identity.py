"""Tests for cora.person_identity -- the per-person identity resolver.

Covers: resolution from the maintained YAMLs, slug derivation, the flag set
(lex_staff on PRIMARY entity / external / exclude_personal_mailbox / exclude_maricopa),
the all_emails union + DWD mailbox list, fail-closed on an unknown Slack ID, and the
roster-drift assertion vs `_brain/reference/team-identity-map.md`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cora import person_identity as pi  # noqa: E402

# Canonical Slack IDs (from the maintained YAMLs).
TOMMY = "U0B3RU5Q55G"
SHAUN = "U0B3PS82G30"
JUSTIN = "U0B3AEJCYGP"
DEMI = "U0B3RU65TFU"
ALINA = "U0B3TPD2MEV"
JASON = "U0B6LQNSR25"
HARRISON = "U0B2RM2JYJ1"


@pytest.fixture(autouse=True)
def _fresh_cache():
    pi.invalidate_cache()
    yield
    pi.invalidate_cache()


def test_slugify_matches_dossier_filenames():
    assert pi.slugify("Tommy Anderson") == "tommy-anderson"
    assert pi.slugify("Jennifer Mortensen") == "jennifer-mortensen"
    assert pi.slugify("Aaron Ferrucci") == "aaron-ferrucci"


def test_resolve_tommy_full_keys():
    p = pi.resolve(TOMMY)
    assert p is not None
    assert p.name == "Tommy Anderson"
    assert p.slug == "tommy-anderson"
    assert p.entity == "F3E"
    assert p.asana_gid == "1213638047870465"
    assert p.hubspot_owner_id == "162944825"
    assert not p.lex_staff and not p.external
    # both DWD-eligible mailboxes present; all_emails is the union
    assert "tommy@hjrglobal.com" in p.mailboxes and "tommy@f3energy.com" in p.mailboxes
    assert "tommy@f3energy.com" in p.all_emails


def test_lex_staff_flag_on_primary_entity():
    # Shaun's primary entity is LEX-LLC -> lex_staff.
    assert pi.resolve(SHAUN).lex_staff is True
    # Justin works across entities (incl. LEX) but his PRIMARY entity is HJRG ->
    # NOT lex_staff (the non-LEX clinical backstop covers his incidental LEX activity).
    assert pi.resolve(JUSTIN).lex_staff is False


def test_external_flag():
    p = pi.resolve(JASON)
    assert p is not None and p.external is True
    assert p.asana_gid is None  # no Asana account


def test_demi_personal_mailbox_excluded_structurally_and_flagged():
    p = pi.resolve(DEMI)
    assert p.exclude_personal_mailbox is True
    # No monitored mailbox -> Gmail impersonation is empty regardless of the flag.
    assert p.mailboxes == []


def test_alina_maricopa_flag_and_manager():
    p = pi.resolve(ALINA)
    assert p.exclude_maricopa is True
    assert p.manager == "Larry Stone"


def test_manager_blank_resolves_to_harrison():
    assert pi.resolve(TOMMY).manager == "Harrison Rogers"


def test_unknown_slack_id_fails_closed():
    assert pi.resolve("UDOESNOTEXIST") is None
    assert pi.resolve("") is None


def test_resolve_by_name_alias():
    # "Sean" is a Shaun alias in user-aliases.yaml
    p = pi.resolve_by_name("Sean")
    assert p is not None and p.slug == "shaun-hawkins"


def test_all_people_covers_roster_including_registry_only():
    slugs = {p.slug for p in pi.all_people()}
    # registry-only (no Slack) still appears
    assert "tessa-miller" in slugs
    # every seeded dossier slug resolvable from the roster (minus README/desktop.ini)
    for s in ("tommy-anderson", "shaun-hawkins", "demi-bagby", "alina-thomas", "jason-dorfman"):
        assert s in slugs


def test_identity_map_no_strong_key_drift():
    """The hand-written identity map must agree with the YAMLs on the strong keys
    (Slack/Asana/HubSpot). Skips gracefully where the Drive reference mount is absent."""
    from scripts import check_identity_map as cim
    path = cim._map_path()
    if not path.exists():
        pytest.skip(f"identity map not present at {path}")
    errors, _warnings = cim.check(path.read_text(encoding="utf-8"))
    assert errors == [], "identity-map strong-key drift:\n" + "\n".join(errors)
