"""Code #13 slice 8 (D-303, ruled 2026-09-10; cq-c5ed06e1f0bf) -- ALLOWLIST-BY-FOLDER
sweep mode for the flat per-user Drive sweep, keyed per account.

v1: the founder's primary mailbox with the HJR-Founder-OS root as the only
allowlisted folder. Inside the tree the 9/8 rule stands (non-.md ingested, .md is
static_md's); everything outside is SKIPPED and COUNTED (skipped_outside_allowlist),
never held; unresolved ancestry still HOLDS the watermark; the pinned exclusions
and title belts stay the BELT and WIN even inside the allowlist; every other
account (mode absent = denylist) is byte-identical to today.

Harness reused verbatim from tests/test_sweep_user_founders_owned.py (_Tree /
_flat / _kb / _anth / _f), extended with a Downloads-shaped folder, a pinned
folder INSIDE the tree (belt-wins case) and an unresolvable folder.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import kb_exclusions  # noqa: E402
from cora import self_inventory as si  # noqa: E402
from cora.connectors import drive_sweep  # noqa: E402
from test_sweep_user_founders_owned import EXPANDED, FOS, PARENTS, USER, _Tree, _anth, _f, _flat, _kb  # noqa: E402

# A REAL pinned id (not a walk-only Computers root) placed INSIDE the allowlisted
# tree, so the belt-wins case is exercised against the live constant, not a stand-in.
PINNED = sorted(kb_exclusions.KB_EXCLUDED_FOLDER_IDS - kb_exclusions.KB_EXCLUDED_WALK_ONLY_IDS)[0]

TREE = {
    **PARENTS,
    "DL": ("MY", "Downloads"),              # Downloads-shaped: a sibling of the tree under My Drive
    "UNRES": ("HJRG", "unresolvable"),      # in-tree folder whose lookup fails (fail_get)
    PINNED: ("F3E", "pinned-store"),        # a pinned exclusion INSIDE the allowlisted tree
    "PINSUB": (PINNED, "sub"),              # ... and a sub-folder of it (only the walk can see it)
}

ALLOW_USER = {**USER, "drive_sweep_mode": "allowlist", "drive_sweep_allowlist": [FOS]}


def _sweep(user, listed, svc, kb=None):
    kb = kb or _kb()
    with patch("cora.connectors.drive_sweep._build_drive_service", return_value=_flat(listed, svc)) as build, \
         patch("cora.connectors.drive_sweep._build_sheets_service", return_value=None), \
         patch("cora.connectors.drive_sweep._expanded_excluded_folder_ids", return_value=(EXPANDED, True)), \
         patch("cora.connectors.drive_sweep._extract_content", return_value="Meaningful business content " * 20), \
         patch("cora.connectors.drive_sweep._ingest_file", return_value=1) as ingest:
        stats = drive_sweep.sweep_user(user, "/fake/sa.json", kb, _anth(), freshness_days=30, dry_run=False)
    return stats, ingest, kb, build


def _ingested(ingest) -> set[str]:
    return {c[0][1]["id"] for c in ingest.call_args_list}


def _fixture(*, with_unresolved: bool = True) -> list[dict]:
    orphan = _f("orphan", "quartz-lantern-brief.pdf", "application/pdf", "MY")
    orphan.pop("parents")                                     # shared-with-me / orphaned: NO parents at all
    listed = [
        _f("intree", "velvet-harbor-receipt.pdf", "application/pdf", "ACC"),          # in-tree NON-.md -> ingested
        _f("intreemd", "amber-tide-notes.md", "text/markdown", "MEM"),                 # in-tree .md -> static_md's
        _f("dl1", "copper-meadow-scan.pdf", "application/pdf", "DL"),                  # Downloads-shaped -> outside
        _f("pinned1", "silver-orchard-sheet.pdf", "application/pdf", PINNED),          # pinned inside tree (fast path)
        _f("pinsub1", "ivory-canyon-ledger.pdf", "application/pdf", "PINSUB"),         # pinned ancestor (walk belt)
        orphan,                                                                        # parentless -> outside
    ]
    if with_unresolved:
        listed.append(_f("unres", "cobalt-ridge-invoice.pdf", "application/pdf", "UNRES"))  # ancestry fails -> held
    return listed


# ── the kickoff fixture through sweep_user ───────────────────────────────────
class TestAllowlistSweep:
    def test_kickoff_fixture_dispositions(self):
        """1 ingested / 1 static_md_owned / 2 outside (counted) / 2 excluded (belt wins) / 1 held."""
        stats, ingest, kb, _ = _sweep(ALLOW_USER, _fixture(), _Tree(TREE, fail_get={"UNRES"}))
        assert _ingested(ingest) == {"intree"}
        assert stats["static_md_owned_skipped"] == 1
        assert stats["skipped_outside_allowlist"] == 2                      # dl1 + orphan
        assert stats["dashboard_excluded_skipped"] == 2                     # pinned1 (fast path) + pinsub1 (walk)
        assert stats["ancestry_unresolved_skipped"] == 1
        assert stats["chunks_ingested"] == 1 and stats["files_enumerated"] == 7

    def test_unresolved_ancestry_still_holds_the_watermark(self):
        """A held watermark is the retry; advancing it past `unres` would drop the file forever."""
        stats, _, kb, _ = _sweep(ALLOW_USER, _fixture(), _Tree(TREE, fail_get={"UNRES"}))
        kb.set_sync_state.assert_not_called()
        kb.delete_checkpoint.assert_called()                                # the resume checkpoint still clears

    def test_outside_allowlist_never_holds_the_watermark(self):
        """Outside skips are counted, not held: with no unresolved file the watermark ADVANCES
        even though two files were skipped as outside the allowlist."""
        stats, ingest, kb, _ = _sweep(ALLOW_USER, _fixture(with_unresolved=False), _Tree(TREE))
        assert stats["skipped_outside_allowlist"] == 2
        assert "ancestry_unresolved_skipped" not in stats
        kb.set_sync_state.assert_called_once()

    def test_belt_wins_inside_the_allowlist_at_both_layers(self):
        """A pinned folder INSIDE the allowlisted tree is still excluded -- by the expanded fast
        path for a direct child and by the ancestry walk for a grand-child."""
        svc = _Tree(TREE)
        assert drive_sweep._file_disposition(svc, [PINNED], EXPANDED, True, {}, allowlist=frozenset({FOS})) == "excluded"
        assert drive_sweep._file_disposition(svc, ["PINSUB"], EXPANDED, True, {}, allowlist=frozenset({FOS})) == "excluded"

    def test_denylist_account_is_byte_identical_to_today(self):
        """Regression pin: the same tree under an account WITHOUT a mode key behaves exactly as
        before the slice -- Downloads + parentless files INGEST, no outside counter exists."""
        stats, ingest, kb, _ = _sweep(USER, _fixture(), _Tree(TREE, fail_get={"UNRES"}))
        assert _ingested(ingest) == {"intree", "dl1", "orphan"}
        assert stats == {
            "files_enumerated": 7, "files_extracted": 3, "chunks_ingested": 3,
            "phi_skipped": 0, "noise_filtered": 0, "dedup_skipped": 0,
            "static_md_owned_skipped": 1, "dashboard_excluded_skipped": 2,
            "ancestry_unresolved_skipped": 1,
        }
        assert "skipped_outside_allowlist" not in stats
        kb.set_sync_state.assert_not_called()                               # held by `unres`, as today

    def test_per_account_done_line_carries_mode_and_counter(self, caplog):
        with caplog.at_level(logging.INFO, logger="cora.drive_sweep"):
            _sweep(ALLOW_USER, _fixture(with_unresolved=False), _Tree(TREE))
        done = [r.getMessage() for r in caplog.records if " done -- " in r.getMessage()]
        assert done and "mode=allowlist" in done[-1] and "skipped_outside_allowlist=2" in done[-1]


# ── disposition mechanics ────────────────────────────────────────────────────
class TestDispositionEx:
    def test_returns_chain_ids_from_one_cached_walk(self):
        svc = _Tree(TREE)
        cache: dict = {}
        disp, ids = drive_sweep._file_disposition_ex(svc, ["ACC"], EXPANDED, True, cache,
                                                     mime_type="application/pdf", filename="x.pdf",
                                                     allowlist=frozenset({FOS}))
        assert disp is None and {"ACC", "HJRG", FOS, "MY"} <= set(ids)
        n_gets = len(svc.got)
        # a second file in the same folder costs no new lookups (the cache is the one walk)
        drive_sweep._file_disposition_ex(svc, ["ACC"], EXPANDED, True, cache, allowlist=frozenset({FOS}))
        assert len(svc.got) == n_gets

    def test_allowlist_forces_the_walk_when_denylist_would_skip_it(self, monkeypatch):
        """Legacy shape (no walk-only roots, complete expansion): denylist returns None WITHOUT
        walking; allowlist mode must still walk -- membership needs the chain."""
        monkeypatch.setattr(drive_sweep, "KB_EXCLUDED_WALK_ONLY_IDS", frozenset())
        svc = _Tree(TREE)
        assert drive_sweep._file_disposition(svc, ["DL"], EXPANDED, True, {}) is None
        assert svc.got == []
        assert drive_sweep._file_disposition(svc, ["DL"], EXPANDED, True, {}, allowlist=frozenset({FOS})) == "outside_allowlist"
        assert svc.got                                                       # the walk ran

    def test_parentless_is_outside_in_allowlist_mode_and_ingest_in_denylist(self):
        svc = _Tree(TREE)
        assert drive_sweep._file_disposition(svc, None, EXPANDED, True, {}) is None
        assert drive_sweep._file_disposition(svc, [], EXPANDED, True, {}, allowlist=frozenset({FOS})) == "outside_allowlist"

    def test_in_tree_markdown_stays_static_md_owned_in_allowlist_mode(self):
        svc = _Tree(TREE)
        assert drive_sweep._file_disposition(svc, ["MEM"], EXPANDED, True, {}, mime_type="text/markdown",
                                             filename="decisions.md", allowlist=frozenset({FOS})) == "static_md_owned"

    def test_unresolved_beats_outside(self):
        """An unresolvable chain is HELD (retry), never counted as outside (a permanent drop)."""
        svc = _Tree(TREE, fail_get={"UNRES"})
        assert drive_sweep._file_disposition(svc, ["UNRES"], EXPANDED, True, {}, allowlist=frozenset({FOS})) == "unresolved"

    def test_wrapper_shape_unchanged_for_legacy_callers(self):
        """_file_disposition still returns the bare disposition; _file_under_excluded_folder still works."""
        svc = _Tree(TREE)
        assert drive_sweep._file_disposition(svc, ["DL"], EXPANDED, True, {}) is None
        assert drive_sweep._file_under_excluded_folder(svc, ["PINSUB"], EXPANDED, True, {}) is True


# ── mode resolution + refusals ───────────────────────────────────────────────
class TestModeResolution:
    def test_absent_key_is_denylist(self):
        assert drive_sweep.resolve_sweep_mode(USER) == ("denylist", frozenset(), None)

    def test_allowlist_mode_case_insensitive_and_string_list(self):
        mode, allow, err = drive_sweep.resolve_sweep_mode({**USER, "drive_sweep_mode": " Allowlist ",
                                                           "drive_sweep_allowlist": FOS})
        assert (mode, allow, err) == ("allowlist", frozenset({FOS}), None)

    def test_empty_allowlist_is_refused(self):
        for empty in ([], None, [""], ""):
            mode, allow, err = drive_sweep.resolve_sweep_mode({**USER, "drive_sweep_mode": "allowlist",
                                                               "drive_sweep_allowlist": empty})
            assert mode == "allowlist" and not allow and err and "REFUSED" in err

    def test_unknown_mode_is_refused_not_denylist(self):
        """A typo must fail CLOSED: falling back to denylist would ingest everything."""
        mode, allow, err = drive_sweep.resolve_sweep_mode({**USER, "drive_sweep_mode": "allowlst",
                                                           "drive_sweep_allowlist": [FOS]})
        assert err and "unknown" in err and mode != "denylist"

    def test_sweep_user_refuses_empty_allowlist_before_any_service_or_state(self, caplog):
        """ERROR logged, zero stats returned, no Drive service built, watermark AND checkpoint untouched."""
        bad = {**USER, "drive_sweep_mode": "allowlist", "drive_sweep_allowlist": []}
        with caplog.at_level(logging.ERROR, logger="cora.drive_sweep"):
            stats, ingest, kb, build = _sweep(bad, _fixture(), _Tree(TREE))
        build.assert_not_called()
        ingest.assert_not_called()
        assert stats["files_enumerated"] == 0 and stats["chunks_ingested"] == 0
        kb.set_sync_state.assert_not_called()
        kb.delete_checkpoint.assert_not_called()
        kb.get_sync_state.assert_not_called()
        assert any("REFUSED" in r.getMessage() for r in caplog.records)

    def test_sweep_user_refuses_unknown_mode(self):
        bad = {**USER, "drive_sweep_mode": "blocklist"}
        stats, ingest, kb, build = _sweep(bad, _fixture(), _Tree(TREE))
        build.assert_not_called()
        kb.set_sync_state.assert_not_called()


# ── the roster (v1 config) ───────────────────────────────────────────────────
class TestRosterPin:
    def _rows(self):
        cfg = yaml.safe_load((_REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml").read_text(encoding="utf-8"))
        return cfg["accounts"]

    def test_primary_harrison_row_is_allowlist_with_the_founders_os_root(self):
        """The yaml value == drive_sweep.FOUNDERS_OS_ROOT_ID (mirrors test_root_id_constant_matches_drive_sweep)."""
        rows = [r for r in self._rows() if r["email"] == "harrison@hjrglobal.com"]
        assert len(rows) == 1
        row = rows[0]
        assert row["drive_sweep_mode"] == "allowlist"
        assert row["drive_sweep_allowlist"] == [drive_sweep.FOUNDERS_OS_ROOT_ID]
        assert drive_sweep.resolve_sweep_mode(row) == ("allowlist", frozenset({FOS}), None)
        assert row.get("drive_sweep") is True and row.get("enabled") is True     # the row is actually swept

    def test_every_other_row_including_harrison_aliases_carries_no_mode(self):
        """v1 keys the mode on the PRIMARY mailbox row ONLY; alias rows and every other account stay denylist."""
        others = [r for r in self._rows() if r["email"] != "harrison@hjrglobal.com"]
        assert others
        for r in others:
            assert "drive_sweep_mode" not in r and "drive_sweep_allowlist" not in r, r["email"]
            assert drive_sweep.resolve_sweep_mode(r)[0] == "denylist"

    def test_header_documents_both_keys(self):
        text = (_REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml").read_text(encoding="utf-8")
        head = text.split("\naccounts:", 1)[0]          # the TOP-LEVEL key, not the PHI-note prose
        assert "# drive_sweep_mode: allowlist" in head and "# drive_sweep_allowlist:" in head


# ── run_sweep: cross-user dedup + aggregate ──────────────────────────────────
def _two_account_yaml(tmp_path) -> str:
    data = {"accounts": [
        {"email": "harrison@hjrglobal.com", "name": "Harrison", "enabled": True, "dwd_eligible": True,
         "drive_sweep": True, "entity_default": "FNDR",
         "drive_sweep_mode": "allowlist", "drive_sweep_allowlist": [FOS]},
        {"email": "tommy@f3energy.com", "name": "Tommy", "enabled": True, "dwd_eligible": True,
         "drive_sweep": True, "entity_default": "F3E"},
    ]}
    p = tmp_path / "accounts.yaml"
    p.write_text(yaml.dump(data), encoding="utf-8")
    return str(p)


class TestRunSweepAllowlist:
    def test_outside_skip_does_not_poison_a_denylist_account(self, tmp_path, caplog):
        """harrison@ (allowlist) skips a shared out-of-tree file; tommy@ (denylist) still ingests it --
        the outside skip withdraws its cross-user dedup mark (ruling (c))."""
        shared = [_f("shared1", "maple-signal-vendor-sheet.pdf", "application/pdf", "LOOSE")]
        svc = _Tree(TREE)
        with patch("cora.connectors.drive_sweep._build_drive_service", side_effect=lambda sa, email: _flat(shared, svc)), \
             patch("cora.connectors.drive_sweep._build_sheets_service", return_value=None), \
             patch("cora.connectors.drive_sweep._expanded_excluded_folder_ids", return_value=(EXPANDED, True)), \
             patch("cora.connectors.drive_sweep._extract_content", return_value="Meaningful business content " * 20), \
             patch("cora.connectors.drive_sweep._ingest_file", return_value=1) as ingest, \
             caplog.at_level(logging.INFO, logger="cora.drive_sweep"):
            agg = drive_sweep.run_sweep("/fake/sa.json", _two_account_yaml(tmp_path), _kb(), _anth(), freshness_days=30)
        assert ingest.call_count == 1
        assert ingest.call_args[0][4]["email"] == "tommy@f3energy.com"      # the `user` arg of _ingest_file
        assert agg["skipped_outside_allowlist"] == 1 and agg["dedup_skipped"] == 0 and agg["chunks_ingested"] == 1
        complete = [r.getMessage() for r in caplog.records if "COMPLETE --" in r.getMessage()]
        assert complete and "skipped_outside_allowlist=1" in complete[-1]

    def test_aggregate_key_list_carries_the_new_counter(self):
        assert "skipped_outside_allowlist" in drive_sweep.AGGREGATE_COUNTER_KEYS
        assert {"dashboard_excluded_skipped", "static_md_owned_skipped", "ancestry_unresolved_skipped"} <= set(
            drive_sweep.AGGREGATE_COUNTER_KEYS)

    def test_aggregate_sums_the_counter_across_accounts(self, tmp_path):
        kb = MagicMock()
        with patch("cora.connectors.drive_sweep.sweep_user") as sw:
            sw.return_value = {"files_enumerated": 1, "files_extracted": 0, "chunks_ingested": 0, "phi_skipped": 0,
                               "noise_filtered": 0, "dedup_skipped": 0, "skipped_outside_allowlist": 3}
            agg = drive_sweep.run_sweep("/fake/sa.json", _two_account_yaml(tmp_path), kb, MagicMock())
        assert agg["skipped_outside_allowlist"] == 6


# ── scripts/run_drive_sweep.py surfaces (were stale for the 9/8 counters) ────
def _load_run_drive_sweep():
    spec = importlib.util.spec_from_file_location("run_drive_sweep_slice8", _REPO_ROOT / "scripts" / "run_drive_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_STATS = {"accounts_swept": 2, "files_enumerated": 10, "files_extracted": 5, "chunks_ingested": 7, "phi_skipped": 1,
          "noise_filtered": 2, "dedup_skipped": 1, "dashboard_excluded_skipped": 3, "static_md_owned_skipped": 4,
          "ancestry_unresolved_skipped": 1, "skipped_outside_allowlist": 6}


class TestRunDriveSweepSurfaces:
    def test_done_line_carries_every_aggregate_counter(self):
        line = _load_run_drive_sweep()._format_done_line(_STATS)
        assert line.startswith("Drive sweep DONE -- accounts=2")
        for tok in ("excluded_folder=3", "static_md_owned=4", "ancestry_unresolved=1", "skipped_outside_allowlist=6"):
            assert tok in line

    def test_slack_summary_carries_every_aggregate_counter_and_tolerates_missing(self):
        mod = _load_run_drive_sweep()
        text = mod._format_slack_summary(_STATS, dry_run=False)
        for tok in ("Excluded-folder skipped: 3", "static_md-owned (in-tree .md) skipped: 4",
                    "Ancestry unresolved (held): 1", "Outside allowlist skipped (D-303): 6"):
            assert tok in text
        legacy = mod._format_slack_summary({"accounts_swept": 1}, dry_run=True)         # a legacy-shaped dict
        assert "(dry-run)" in legacy and "Outside allowlist skipped (D-303): 0" in legacy


# ── self-inventory (I4): the mode renders beside the pinned exclusions ───────
def _modes_stub():
    return [{"email": "harrison@hjrglobal.com", "mode": "allowlist", "swept": True, "error": None,
             "allowlist": [{"id": FOS, "label": drive_sweep.ALLOWLIST_FOLDER_LABELS[FOS]}]}]


def _inv(detail, **over):
    kw = dict(kb=None, detail=detail, entity="F3E", registry_reader=lambda: [], markers_reader=lambda: {},
              gmail_reader=lambda: {}, parity_reader=lambda: {"available": False}, connectors_reader=lambda e: [],
              sweep_modes_reader=_modes_stub, now=1_800_000_000.0)
    kw.update(over)
    return si.build_inventory(**kw)


class TestSelfInventoryModes:
    def test_reader_lists_the_v1_row_from_the_live_roster(self):
        rows = si.read_drive_sweep_modes()
        assert [r["email"] for r in rows] == ["harrison@hjrglobal.com"]
        assert rows[0]["mode"] == "allowlist" and rows[0]["error"] is None
        assert rows[0]["allowlist"] == [{"id": FOS, "label": drive_sweep.ALLOWLIST_FOLDER_LABELS[FOS]}]

    def test_reader_is_fail_soft_and_surfaces_a_config_error(self, tmp_path):
        assert si.read_drive_sweep_modes(tmp_path / "missing.yaml") == []
        bad = tmp_path / "r.yaml"
        bad.write_text(yaml.dump({"accounts": [{"email": "x@y.com", "drive_sweep_mode": "allowlist",
                                                "drive_sweep_allowlist": []}]}), encoding="utf-8")
        rows = si.read_drive_sweep_modes(bad)
        assert len(rows) == 1 and "REFUSED" in rows[0]["error"]

    def test_founder_render_names_mailbox_and_folder_channel_render_does_not(self):
        founder = si.render_inventory(_inv(True))
        channel = si.render_inventory(_inv(False))
        assert "DRIVE SWEEP MODES" in founder and "harrison@hjrglobal.com | allowlist" in founder and FOS in founder
        assert "ALLOWLIST-BY-FOLDER" in channel and "1 account(s)" in channel
        assert "harrison@" not in channel and FOS not in channel               # mailbox + folder id are founder-level

    def test_reader_failure_is_fail_soft_in_build(self):
        def boom():
            raise RuntimeError("roster exploded")
        inv = _inv(True, sweep_modes_reader=boom)
        assert inv["drive_sweep_modes"] == [] and "roster exploded" in inv["drive_sweep_modes_error"]
        assert "roster unreadable this turn" in si.render_inventory(inv)

    def test_door_note_mentions_allowlist_mode(self):
        assert "ALLOWLIST-BY-FOLDER" in si._SOURCE_DOOR_NOTES["drive_sweep"]
