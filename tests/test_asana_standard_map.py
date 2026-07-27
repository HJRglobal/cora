"""Asana Standard v1 (2026-07-27) -- pins for the catch-all repoint + LEX-LTS.

Slice 1 of the standard: UFL/HJRP/F3C catch-alls repointed to their new
"[CODE] Operations — General" projects, and a LEX-LTS entity section added. These
tests read the REAL data/maps/*.yaml (not a fake) so a drift between the two map
files, or a lost repoint, fails the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import cora.tools.project_resolver as resolver  # noqa: E402

_MAP_PATH = _REPO / "data" / "maps" / "asana-project-map.yaml"
_CAPTURE_PATH = _REPO / "data" / "maps" / "meeting-capture-projects.yaml"

# The 4 new "[CODE] Operations — General" catch-alls created 2026-07-27.
NEW_CATCH_ALLS = {
    "UFL": "1216928707369575",
    "HJRP": "1216928758714643",
    "F3C": "1216928758905250",
    "LEX-LTS": "1216928755480116",
}


@pytest.fixture(autouse=True)
def _real_map():
    """Force the resolver to read the on-disk map (not any leftover fake)."""
    resolver.reload_map()
    yield
    resolver.reload_map()


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


class TestCatchAllRepoint:
    """resolve_project falls back to the NEW catch-all for an unmatched task."""

    def test_ufl_routes_to_new_catch_all(self):
        # UFL is paused -> everything routes to catch-all (Tier 0).
        assert resolver.resolve_project(entity="UFL", task_text="random ufl task") == NEW_CATCH_ALLS["UFL"]

    def test_hjrp_unmatched_routes_to_new_catch_all(self):
        assert (
            resolver.resolve_project(entity="HJRP", task_text="totally unrelated hjrp task xyz")
            == NEW_CATCH_ALLS["HJRP"]
        )

    def test_f3c_unmatched_routes_to_new_catch_all(self):
        assert (
            resolver.resolve_project(entity="F3C", task_text="totally unrelated f3c task xyz")
            == NEW_CATCH_ALLS["F3C"]
        )

    def test_lex_lts_routes_to_lts_catch_all_not_llc(self):
        # Before the standard, LEX-LTS fell back to the LEX parent (LLC catch-all).
        gid = resolver.resolve_project(entity="LEX-LTS", task_text="lts operations task")
        assert gid == NEW_CATCH_ALLS["LEX-LTS"]
        assert gid != resolver.entity_catch_all("LEX-LLC"), "LEX-LTS must not home in the LLC catch-all"

    def test_entity_catch_all_helper_new_gids(self):
        for entity, gid in NEW_CATCH_ALLS.items():
            assert resolver.entity_catch_all(entity) == gid, entity


class TestTwoMapsAgree:
    """meeting-capture-projects.yaml `projects:` must equal each entity's
    catch_all_gid in asana-project-map.yaml -- the capture pipeline and the
    conversational create path both rely on that invariant."""

    def test_capture_projects_match_map_catch_alls(self):
        cap = _load_yaml(_CAPTURE_PATH)
        mp = _load_yaml(_MAP_PATH)
        entities = mp.get("entities") or {}
        mismatches = []
        for entity, gid in (cap.get("projects") or {}).items():
            if not gid:  # BDM is intentionally "" (excluded from capture)
                continue
            map_gid = (entities.get(entity) or {}).get("catch_all_gid")
            if str(map_gid) != str(gid):
                mismatches.append(f"{entity}: capture={gid} map={map_gid}")
        assert not mismatches, "capture/map catch-all disagreement: " + "; ".join(mismatches)

    def test_new_catch_alls_present_in_both_files(self):
        cap = _load_yaml(_CAPTURE_PATH)
        mp = _load_yaml(_MAP_PATH)
        for entity, gid in NEW_CATCH_ALLS.items():
            assert (cap.get("projects") or {}).get(entity) == gid, f"capture missing {entity}"
            assert (mp.get("entities") or {}).get(entity, {}).get("catch_all_gid") == gid, f"map missing {entity}"


class TestLexLtsIsKnownLexProject:
    """The LTS catch-all must be recognised as a LEX-scoped project so captured
    LTS tasks pass the hard-rail #1 allowlist (never leak to a non-LEX project)."""

    def test_lts_catch_all_in_lex_allowlist(self):
        import cora.connectors.fireflies_action_extractor as fae

        # Reset the module-level caches so the allowlist is rebuilt from disk.
        fae._capture_project_cfg = None
        fae._known_lex_projects = None
        resolver.reload_map()
        known = fae._known_lex_project_gids()
        assert NEW_CATCH_ALLS["LEX-LTS"] in known
        # Cleanup so we don't leak a rebuilt cache into other tests.
        fae._capture_project_cfg = None
        fae._known_lex_projects = None
