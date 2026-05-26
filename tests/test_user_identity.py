"""Tests for cora.tools.user_identity — unified Slack ↔ Asana ↔ HubSpot identity layer.

All tests use tmp_path YAML fixtures so they never touch the real data/maps/ files.
The module-level cache is invalidated between tests via monkeypatch.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from cora.tools import user_identity
from cora.tools.user_identity import (
    UserRecord,
    all_asana_gids,
    all_users,
    asana_gid,
    display_name,
    get_user,
    hubspot_owner_id,
    invalidate_cache,
    resolve_person,
    slack_id_from_asana,
    slack_id_from_name,
    slack_mention,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_cache():
    """Invalidate the in-process identity cache before every test."""
    invalidate_cache()
    yield
    invalidate_cache()


def _write_asana_map(path: Path, users: list[dict]) -> None:
    path.write_text(yaml.dump({"users": users}), encoding="utf-8")


def _write_hubspot_map(path: Path, users: list[dict]) -> None:
    path.write_text(yaml.dump({"users": users}), encoding="utf-8")


def _write_alias_map(path: Path, aliases: dict, disambiguation_rules=None) -> None:
    data = {"aliases": aliases, "disambiguation_rules": disambiguation_rules or []}
    path.write_text(yaml.dump(data), encoding="utf-8")


@pytest.fixture
def map_dir(tmp_path, monkeypatch):
    """Point user_identity module constants at tmp YAML files."""
    asana_file = tmp_path / "slack-to-asana.yaml"
    hubspot_file = tmp_path / "slack-to-hubspot.yaml"
    aliases_file = tmp_path / "user-aliases.yaml"

    # Write minimal valid (but empty) files so _load doesn't warn
    _write_asana_map(asana_file, [])
    _write_hubspot_map(hubspot_file, [])
    _write_alias_map(aliases_file, {})

    monkeypatch.setattr(user_identity, "_ASANA_MAP", asana_file)
    monkeypatch.setattr(user_identity, "_HUBSPOT_MAP", hubspot_file)
    monkeypatch.setattr(user_identity, "_ALIASES_MAP", aliases_file)

    return tmp_path


def _seed_full(map_dir: Path) -> None:
    """Seed a realistic two-user set used across many tests."""
    _write_asana_map(
        map_dir / "slack-to-asana.yaml",
        [
            {
                "slack_user_id": "U001",
                "asana_user_gid": "111111",
                "asana_email": "harrison@hjrglobal.com",
                "display_name": "Harrison Rogers",
            },
            {
                "slack_user_id": "U002",
                "asana_user_gid": "222222",
                "asana_email": "hannah@hjrglobal.com",
                "display_name": "Hannah Grant",
            },
        ],
    )
    _write_hubspot_map(
        map_dir / "slack-to-hubspot.yaml",
        [
            {
                "slack_user_id": "U001",
                "hubspot_owner_id": "9001",
                "hubspot_email": "harrison@hjrglobal.com",
                "display_name": "Harrison Rogers",
            }
        ],
    )
    _write_alias_map(
        map_dir / "user-aliases.yaml",
        {
            "Harrison Rogers": ["Harrison", "HJR"],
            "Hannah Grant": ["Hannah"],
        },
    )
    invalidate_cache()


# ── UserRecord dataclass ───────────────────────────────────────────────────────

class TestUserRecord:
    def test_slack_mention_property(self):
        rec = UserRecord(slack_user_id="UABC", display_name="Alice")
        assert rec.slack_mention == "<@UABC>"

    def test_first_name_single_word(self):
        rec = UserRecord(slack_user_id="U1", display_name="Alice")
        assert rec.first_name == "Alice"

    def test_first_name_full_name(self):
        rec = UserRecord(slack_user_id="U1", display_name="Harrison Rogers")
        assert rec.first_name == "Harrison"

    def test_first_name_empty(self):
        rec = UserRecord(slack_user_id="U1", display_name="")
        assert rec.first_name == ""

    def test_optional_fields_default_none(self):
        rec = UserRecord(slack_user_id="U1", display_name="Alice")
        assert rec.asana_gid is None
        assert rec.asana_email is None
        assert rec.hubspot_owner_id is None
        assert rec.hubspot_email is None
        assert rec.aliases == []


# ── Cache loading — Asana map ──────────────────────────────────────────────────

class TestAsanaMapLoading:
    def test_basic_load(self, map_dir):
        _write_asana_map(
            map_dir / "slack-to-asana.yaml",
            [{"slack_user_id": "U001", "asana_user_gid": "111", "display_name": "Alice"}],
        )
        invalidate_cache()
        rec = get_user("U001")
        assert rec is not None
        assert rec.display_name == "Alice"
        assert rec.asana_gid == "111"

    def test_asana_gid_field_alias(self, map_dir):
        """Both asana_user_gid and asana_gid field names are accepted."""
        _write_asana_map(
            map_dir / "slack-to-asana.yaml",
            [{"slack_user_id": "U002", "asana_gid": "999", "display_name": "Bob"}],
        )
        invalidate_cache()
        rec = get_user("U002")
        assert rec.asana_gid == "999"

    def test_missing_slack_id_skipped(self, map_dir):
        _write_asana_map(
            map_dir / "slack-to-asana.yaml",
            [{"asana_user_gid": "111", "display_name": "Ghost"}],
        )
        invalidate_cache()
        assert all_users() == []

    def test_display_name_falls_back_to_sid(self, map_dir):
        _write_asana_map(
            map_dir / "slack-to-asana.yaml",
            [{"slack_user_id": "UXXX"}],
        )
        invalidate_cache()
        rec = get_user("UXXX")
        assert rec.display_name == "UXXX"

    def test_asana_email_preserved(self, map_dir):
        _write_asana_map(
            map_dir / "slack-to-asana.yaml",
            [{"slack_user_id": "U1", "asana_user_gid": "1", "asana_email": "a@b.com", "display_name": "A"}],
        )
        invalidate_cache()
        assert get_user("U1").asana_email == "a@b.com"

    def test_missing_file_returns_empty(self, map_dir, monkeypatch):
        monkeypatch.setattr(user_identity, "_ASANA_MAP", map_dir / "nonexistent.yaml")
        invalidate_cache()
        assert all_users() == []

    def test_invalid_entry_type_skipped(self, map_dir):
        """Non-dict entries in the users list are silently skipped."""
        content = "users:\n  - U001\n  - {slack_user_id: U002, display_name: Valid}\n"
        (map_dir / "slack-to-asana.yaml").write_text(content, encoding="utf-8")
        invalidate_cache()
        users = all_users()
        assert len(users) == 1
        assert users[0].slack_user_id == "U002"


# ── Cache loading — HubSpot map ────────────────────────────────────────────────

class TestHubSpotMapLoading:
    def test_hubspot_merges_into_existing_record(self, map_dir):
        _seed_full(map_dir)
        rec = get_user("U001")
        assert rec.hubspot_owner_id == "9001"
        assert rec.hubspot_email == "harrison@hjrglobal.com"

    def test_hubspot_only_user_created(self, map_dir):
        """HubSpot entry for a user not in the Asana map creates a stub record."""
        _write_asana_map(map_dir / "slack-to-asana.yaml", [])
        _write_hubspot_map(
            map_dir / "slack-to-hubspot.yaml",
            [{"slack_user_id": "U999", "hubspot_owner_id": "5555", "display_name": "Orphan"}],
        )
        invalidate_cache()
        rec = get_user("U999")
        assert rec is not None
        assert rec.hubspot_owner_id == "5555"
        assert rec.asana_gid is None

    def test_hubspot_missing_owner_id_skipped(self, map_dir):
        _write_asana_map(map_dir / "slack-to-asana.yaml", [])
        _write_hubspot_map(
            map_dir / "slack-to-hubspot.yaml",
            [{"slack_user_id": "U1", "display_name": "NoOwner"}],
        )
        invalidate_cache()
        assert get_user("U1") is None

    def test_hubspot_missing_slack_id_skipped(self, map_dir):
        _write_asana_map(map_dir / "slack-to-asana.yaml", [])
        _write_hubspot_map(
            map_dir / "slack-to-hubspot.yaml",
            [{"hubspot_owner_id": "9999", "display_name": "NoSlack"}],
        )
        invalidate_cache()
        assert all_users() == []

    def test_hubspot_email_merges_correctly(self, map_dir):
        _seed_full(map_dir)
        assert get_user("U001").hubspot_email == "harrison@hjrglobal.com"

    def test_user_without_hubspot_has_none(self, map_dir):
        _seed_full(map_dir)
        rec = get_user("U002")  # Hannah — not in hubspot map
        assert rec.hubspot_owner_id is None


# ── Cache loading — Aliases ────────────────────────────────────────────────────

class TestAliasLoading:
    def test_alias_resolves_to_slack_id(self, map_dir):
        _seed_full(map_dir)
        assert slack_id_from_name("Harrison") == "U001"

    def test_alias_case_insensitive(self, map_dir):
        _seed_full(map_dir)
        assert slack_id_from_name("harrison") == "U001"
        assert slack_id_from_name("HARRISON") == "U001"

    def test_canonical_name_is_implicit_alias(self, map_dir):
        _seed_full(map_dir)
        assert slack_id_from_name("Harrison Rogers") == "U001"

    def test_multiple_aliases_all_resolve(self, map_dir):
        _seed_full(map_dir)
        assert slack_id_from_name("HJR") == "U001"
        assert slack_id_from_name("hjr") == "U001"

    def test_unknown_alias_returns_none(self, map_dir):
        _seed_full(map_dir)
        assert slack_id_from_name("Zephyr") is None

    def test_alias_for_user_not_in_asana_map_is_skipped(self, map_dir):
        """Tessa has aliases but no Asana entry → should not crash, just skip."""
        _write_asana_map(map_dir / "slack-to-asana.yaml", [])
        _write_alias_map(map_dir / "user-aliases.yaml", {"Tessa Miller": ["Tessa"]})
        invalidate_cache()
        # Should not raise; just returns None
        assert slack_id_from_name("Tessa") is None

    def test_aliases_stored_on_record(self, map_dir):
        _seed_full(map_dir)
        rec = get_user("U001")
        assert "harrison" in rec.aliases
        assert "hjr" in rec.aliases

    def test_empty_alias_list_handled(self, map_dir):
        _write_asana_map(
            map_dir / "slack-to-asana.yaml",
            [{"slack_user_id": "U1", "display_name": "Solo"}],
        )
        _write_alias_map(map_dir / "user-aliases.yaml", {"Solo": None})
        invalidate_cache()
        # Should not crash
        assert get_user("U1") is not None


# ── Public API — lookup functions ──────────────────────────────────────────────

class TestPublicLookupAPI:
    def test_get_user_returns_record(self, map_dir):
        _seed_full(map_dir)
        rec = get_user("U001")
        assert isinstance(rec, UserRecord)
        assert rec.slack_user_id == "U001"

    def test_get_user_unknown_returns_none(self, map_dir):
        _seed_full(map_dir)
        assert get_user("UXXX") is None

    def test_display_name_known_user(self, map_dir):
        _seed_full(map_dir)
        assert display_name("U001") == "Harrison Rogers"

    def test_display_name_unknown_falls_back_to_id(self, map_dir):
        _seed_full(map_dir)
        assert display_name("UNOBODY") == "UNOBODY"

    def test_asana_gid_known_user(self, map_dir):
        _seed_full(map_dir)
        assert asana_gid("U001") == "111111"

    def test_asana_gid_unknown_returns_none(self, map_dir):
        _seed_full(map_dir)
        assert asana_gid("UXXX") is None

    def test_slack_id_from_asana_reverse_lookup(self, map_dir):
        _seed_full(map_dir)
        assert slack_id_from_asana("111111") == "U001"
        assert slack_id_from_asana("222222") == "U002"

    def test_slack_id_from_asana_unknown_returns_none(self, map_dir):
        _seed_full(map_dir)
        assert slack_id_from_asana("999999") is None

    def test_hubspot_owner_id_known(self, map_dir):
        _seed_full(map_dir)
        assert hubspot_owner_id("U001") == "9001"

    def test_hubspot_owner_id_unknown_returns_none(self, map_dir):
        _seed_full(map_dir)
        assert hubspot_owner_id("U002") is None

    def test_slack_mention_format(self, map_dir):
        assert slack_mention("U001") == "<@U001>"
        assert slack_mention("UABC123") == "<@UABC123>"

    def test_all_users_returns_list(self, map_dir):
        _seed_full(map_dir)
        users = all_users()
        assert len(users) == 2
        ids = {u.slack_user_id for u in users}
        assert "U001" in ids
        assert "U002" in ids

    def test_all_asana_gids_returns_list(self, map_dir):
        _seed_full(map_dir)
        gids = all_asana_gids()
        assert "111111" in gids
        assert "222222" in gids

    def test_all_users_empty_when_no_maps(self, map_dir):
        # Maps written empty in fixture by default
        assert all_users() == []


# ── resolve_person — multi-system fallback chain ───────────────────────────────

class TestResolvePerson:
    def test_resolves_by_slack_id(self, map_dir):
        _seed_full(map_dir)
        rec = resolve_person("U001")
        assert rec.display_name == "Harrison Rogers"

    def test_resolves_by_asana_gid(self, map_dir):
        _seed_full(map_dir)
        rec = resolve_person("111111")
        assert rec.slack_user_id == "U001"

    def test_resolves_by_name_alias(self, map_dir):
        _seed_full(map_dir)
        rec = resolve_person("Hannah")
        assert rec.slack_user_id == "U002"

    def test_resolves_by_canonical_name(self, map_dir):
        _seed_full(map_dir)
        rec = resolve_person("Harrison Rogers")
        assert rec.slack_user_id == "U001"

    def test_unknown_identifier_returns_none(self, map_dir):
        _seed_full(map_dir)
        assert resolve_person("nobody_here") is None

    def test_prefers_slack_id_over_name(self, map_dir):
        """If identifier matches a Slack ID directly, return that even if
        the same string happens to be an alias too (edge case)."""
        _seed_full(map_dir)
        # U001 is Harrison's Slack ID — resolve_person checks slack first
        rec = resolve_person("U001")
        assert rec.slack_user_id == "U001"


# ── invalidate_cache ───────────────────────────────────────────────────────────

class TestInvalidateCache:
    def test_invalidate_forces_reload(self, map_dir):
        _seed_full(map_dir)
        assert get_user("U001") is not None

        # Replace both maps (HubSpot still has U001; must clear it too)
        _write_asana_map(
            map_dir / "slack-to-asana.yaml",
            [{"slack_user_id": "U999", "display_name": "New User"}],
        )
        _write_hubspot_map(map_dir / "slack-to-hubspot.yaml", [])
        invalidate_cache()

        assert get_user("U001") is None
        assert get_user("U999") is not None

    def test_double_invalidate_does_not_crash(self, map_dir):
        invalidate_cache()
        invalidate_cache()  # should be idempotent


# ── Thread safety sanity ───────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_reads_same_result(self, map_dir):
        import threading
        _seed_full(map_dir)
        results = []

        def _read():
            results.append(get_user("U001"))

        threads = [threading.Thread(target=_read) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 20
        assert all(r is not None for r in results)
        assert all(r.display_name == "Harrison Rogers" for r in results)


# ── Real maps smoke test ───────────────────────────────────────────────────────

class TestRealMapsSmoke:
    """Load the actual data/maps/ files and spot-check key lookups.
    These tests will fail if the real YAML files drift from expected shape.
    """

    @pytest.fixture(autouse=True)
    def _use_real_maps(self):
        """Use the real _ASANA_MAP / _HUBSPOT_MAP / _ALIASES_MAP paths."""
        invalidate_cache()
        yield
        invalidate_cache()

    def test_harrison_resolvable_by_slack_id(self):
        rec = get_user("U0B2RM2JYJ1")
        assert rec is not None
        assert "Harrison" in rec.display_name

    def test_harrison_asana_gid_lookup(self):
        assert asana_gid("U0B2RM2JYJ1") == "1204525779609669"

    def test_asana_reverse_lookup_hannah(self):
        sid = slack_id_from_asana("1209060959783860")
        assert sid == "U0B3AEQS0NB"

    def test_alias_harrison(self):
        assert slack_id_from_name("Harrison") == "U0B2RM2JYJ1"

    def test_alias_case_insensitive_real(self):
        assert slack_id_from_name("hannah") == "U0B3AEQS0NB"

    def test_jeff_montgomery_in_asana_map(self):
        rec = get_user("U0B3KHBJJ91")
        assert rec is not None
        assert rec.asana_gid == "1212753858765058"

    def test_tessa_alias_safe_without_asana_entry(self):
        """Tessa has aliases but no Asana GID — should return None gracefully."""
        assert slack_id_from_name("Tessa") is None

    def test_all_users_non_empty(self):
        assert len(all_users()) >= 10

    def test_hubspot_harrison_owner_id(self):
        assert hubspot_owner_id("U0B2RM2JYJ1") == "160459333"

    def test_hubspot_matt_owner_id(self):
        assert hubspot_owner_id("U0B3PS7RFJA") == "83346026"
