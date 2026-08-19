"""D-194: personal books never enter the KB; mis-filed archive books get re-tagged.

Covers the three halves of the slice:
  1. the slug map is COMPLETE for the accounting archive (no slug may fall
     through to a Haiku guess anchored to LEX);
  2. the sweep EXCLUDES personal books at the chokepoint, before download; and
  3. the staged purge/re-tag pass buckets rows using the same functions the
     sweep writes with.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from cora.connectors import drive_sweep
from cora.connectors.drive_entity_detect import (
    _CODE_TO_LABEL,
    _EXCLUDED_CODES,
    detect_entity_from_filename,
    excluded_slug_from_filename,
    naming_tokens,
    split_entity_label,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SLUG_MAP = _REPO_ROOT / "data" / "maps" / "qbo-monthly-report-slugs.yaml"


def _load_purge_module():
    path = _REPO_ROOT / "scripts" / "purge_kb_personal_books_2026-08-19.py"
    spec = importlib.util.spec_from_file_location("purge_personal_books", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["purge_personal_books"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. Slug-map completeness
# ---------------------------------------------------------------------------

class TestArchiveSlugCoverage:
    def test_every_archive_slug_is_mapped_or_explicitly_excluded(self):
        """No archive slug may fall through to a Haiku guess.

        The archive's slug universe is DATA (qbo-monthly-report-slugs.yaml holds
        both the realms Cora produces and the ones that stay a manual export), so
        this test cannot drift from reality the way a hard-coded list would: add
        a realm there and this fails until the detector learns it.
        """
        cfg = yaml.safe_load(_SLUG_MAP.read_text(encoding="utf-8"))
        slugs = {r["slug"] for r in cfg["realms"].values()}
        slugs |= set(cfg["unmapped_slugs"])

        unhandled = sorted(
            s for s in slugs
            if s not in _CODE_TO_LABEL and s not in _EXCLUDED_CODES
        )
        assert unhandled == [], (
            f"archive slugs with no deterministic home: {unhandled} -- each one "
            f"is decided by a Haiku guess anchored to the sweeping user's "
            f"entity_default (LEX for justin@)"
        )

    @pytest.mark.parametrize("filename,expected", [
        ("2026-05_osn-core4_pl.xlsx", "OSN"),
        ("2026-05_f3comm_bs.xlsx", "F3C"),
        ("2026-05_hjrpod_cf.xlsx", "HJRPROD"),
        ("2026-05_mv_pl.xlsx", "LEX-LLA"),
        ("2026-05_lexcorp_bs.xlsx", "LEX"),
        ("2026-05_llc_pl.xlsx", "LEX-LLC"),
        ("2026-06_hjrg_bs.xlsx", "HJRG"),
    ])
    def test_archive_filenames_resolve_deterministically(self, filename, expected):
        assert detect_entity_from_filename(filename) == expected

    def test_hjrllc_is_excluded_not_mapped(self):
        """The remedy for personal books is an exclusion, NOT an entity.

        Mapping it to FNDR would be strictly worse than leaving it unmapped:
        FNDR chunks co-scan into every non-LEX retrieval.
        """
        assert "hjrllc" not in _CODE_TO_LABEL
        assert "hjrllc" in _EXCLUDED_CODES
        assert detect_entity_from_filename("2026-05_hjrllc_pl.xlsx") is None
        assert excluded_slug_from_filename("2026-05_hjrllc_pl.xlsx") == "hjrllc"


# ---------------------------------------------------------------------------
# 2. Detector / exclusion behaviour
# ---------------------------------------------------------------------------

class TestExclusionDetector:
    def test_exclusion_wins_over_a_mappable_second_token(self):
        """A file naming both an excluded slug and a mappable one is EXCLUDED.

        Otherwise `2026-05_hjrllc_llc-summary.xlsx` would file Harrison's
        personal summary under LEX-LLC -- exactly the exposure being closed.
        """
        name = "2026-05_hjrllc_llc-summary.xlsx"
        assert excluded_slug_from_filename(name) == "hjrllc"
        assert detect_entity_from_filename(name) is None

    def test_prose_mentioning_the_slug_is_not_excluded(self):
        """The exclusion is a naming-convention token match, not a substring
        scan -- an ordinary document that merely mentions the slug is untouched."""
        assert excluded_slug_from_filename("notes about hjrllc.docx") is None
        assert excluded_slug_from_filename("hjrllc-history-writeup.pdf") is None

    def test_exclusion_and_detection_share_one_token_window(self):
        """Both APIs read the same window, so they cannot disagree about what a
        filename offers."""
        name = "2026-05_hjrllc_pl.xlsx"
        assert naming_tokens(name) == ["hjrllc", "pl"]
        assert naming_tokens("") == []

    def test_empty_and_extensionless_inputs_are_safe(self):
        for bad in ("", "   ", "no-underscores"):
            assert excluded_slug_from_filename(bad) is None

    @pytest.mark.parametrize("label,expected", [
        ("LEX-LLA", ("LEX", "LEX-LLA")),
        ("HJRP-RR", ("HJRP", "HJRP-RR")),
        ("OSN", ("OSN", None)),
        ("LEX", ("LEX", None)),
        ("F3C", ("F3C", None)),
    ])
    def test_split_entity_label(self, label, expected):
        assert split_entity_label(label) == expected


# ---------------------------------------------------------------------------
# 3. Sweep wiring
# ---------------------------------------------------------------------------

class TestSweepWiring:
    def test_both_per_file_loops_check_the_exclusion(self):
        """Two ingest loops exist (personal-Drive flat sweep + shared-folder
        sweep). A guard wired into only one of them leaves a live path open."""
        src = (_REPO_ROOT / "src" / "cora" / "connectors" / "drive_sweep.py").read_text(
            encoding="utf-8")
        assert src.count("excluded_slug_from_filename(filename)") == 2
        assert src.count('stats["personal_books_skipped"] += 1') == 2

    def test_exclusion_precedes_content_extraction(self):
        """The file must never even be downloaded -- extraction is the expensive
        step AND the one that would put personal content in memory."""
        src = (_REPO_ROOT / "src" / "cora" / "connectors" / "drive_sweep.py").read_text(
            encoding="utf-8")
        first_excl = src.index("excluded_slug_from_filename(filename)")
        first_extract = src.index("content = _extract_content(")
        assert first_excl < first_extract

    def test_a_deterministic_parent_level_name_is_not_re_scattered(self):
        """`lexcorp -> LEX` alone does NOT hold.

        store.upsert_documents Step 0 re-derives sub_entity for ANY LEX doc
        arriving with sub_entity=None, from title + content -- so a whole-company
        statement whose filename deliberately withholds a sub-entity would still
        be scattered into LEX-LLC or LEX-LTS by keyword, and the map entry would
        have fixed the parent while leaving the scatter it was added to stop.
        metadata.lex_gm_level is the existing opt-out for exactly this shape.
        """
        src = (_REPO_ROOT / "src" / "cora" / "connectors" / "drive_sweep.py").read_text(
            encoding="utf-8")
        assert '"lex_gm_level": True' in src
        assert 'classification["entity_from_filename"] = True' in src

    def test_gm_level_is_only_claimed_when_the_filename_decided(self):
        """An entity_default fallback asserts nothing about the file, so it must
        NOT suppress content detection -- that would silently turn every
        undetected LEX file into a GM-level one."""
        src = (_REPO_ROOT / "src" / "cora" / "connectors" / "drive_sweep.py").read_text(
            encoding="utf-8")
        block = src[src.index("gm_level = bool("):]
        block = block[:block.index("\n    )") + 6]
        assert 'classification.get("entity_from_filename")' in block
        assert 'entity == "LEX"' in block
        assert "sub_entity is None" in block

    def test_ingest_uses_the_shared_split_helper(self):
        """_ingest_file and the re-tag pass must compute placement from ONE
        function, or a re-tag can silently disagree with what the sweep writes."""
        src = (_REPO_ROOT / "src" / "cora" / "connectors" / "drive_sweep.py").read_text(
            encoding="utf-8")
        assert "split_entity_label(label)" in src


# ---------------------------------------------------------------------------
# 4. The staged purge / re-tag pass
# ---------------------------------------------------------------------------

class TestPurgePass:
    def test_classify_row_buckets(self):
        mod = _load_purge_module()
        # personal books -> purge, whatever they were tagged as
        assert mod.classify_row("2026-05_hjrllc_bs.xlsx", "LEX", "LEX-LLC")[0] == "purge"
        # mis-filed archive book -> retag
        bucket, detail = mod.classify_row("2026-05_osn-core4_pl.xlsx", "LEX", None)
        assert bucket == "retag"
        assert detail["to"] == {"entity": "OSN", "sub_entity": None}
        # sub-entity target
        bucket, detail = mod.classify_row("2026-05_mv_pl.xlsx", "LEX", "LEX-LLC")
        assert bucket == "retag"
        assert detail["to"] == {"entity": "LEX", "sub_entity": "LEX-LLA"}
        # already correct -> keep
        assert mod.classify_row("2026-05_osn-core4_pl.xlsx", "OSN", None)[0] == "keep"
        # no archive slug -> keep (never guess)
        assert mod.classify_row("meeting notes.docx", "LEX", None)[0] == "keep"

    def test_retag_never_strips_a_lex_sub_entity_refinement(self):
        """REGRESSION (found by live dry-run, not by review).

        The first cut of this pass re-tagged any row that "disagreed with the
        detector". Dated `..._lex_...` files carry a LEX sub_entity set at the
        upsert chokepoint by knowledge_base.lex_sub_entity from CONTENT -- the
        filename only ever says "LEX". That rule therefore read 843 files'
        deliberate refinements as errors and would have stripped 17,523 chunks
        back to bare LEX.
        """
        mod = _load_purge_module()
        bucket, _ = mod.classify_row(
            "2023-05-18_lex_lts-lexingtontherapies-profitandlossdetail.xlsx",
            "LEX", "LEX-LTS")
        assert bucket == "keep"
        assert "lex" not in mod._D194_RETAG_SLUGS

    def test_retag_ignores_undated_founder_os_files(self):
        """REGRESSION: undated files come from the Founder OS tree sweep, whose
        entity is derived from the FOLDER PATH and never from the filename.
        Re-tagging them replaces a path fact with a filename inference."""
        mod = _load_purge_module()
        for title in ("LBHS.xlsx", "Balance Sheet - Detail_LLA.xlsx",
                      "LexCorp_Unspecified.xlsx", "MV.xlsx"):
            assert mod.archive_slug(title) is None
            assert mod.classify_row(title, "HJRG", None)[0] == "keep"

    def test_archive_slug_requires_the_slug_in_the_first_position(self):
        mod = _load_purge_module()
        assert mod.archive_slug("2026-05_mv_pl.xlsx") == "mv"
        assert mod.archive_slug("2026-05-01_hjrpod_bs.xlsx") == "hjrpod"
        # dated, but the slug is in position 2 -- not the archive convention
        assert mod.archive_slug("2026-05_summary_mv.xlsx") == "summary"

    def test_purge_is_broader_than_retag_on_purpose(self):
        """An undated personal-books file must STILL be purged even though the
        same shape is out of scope for re-tagging."""
        mod = _load_purge_module()
        assert mod.classify_row("hjrllc_pl.xlsx", "LEX", None)[0] == "purge"

    def test_scan_over_a_real_sqlite_table(self, tmp_path):
        mod = _load_purge_module()
        db = tmp_path / "kb.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE knowledge_chunks (chunk_id TEXT, source TEXT, "
            "source_id TEXT, title TEXT, entity TEXT, sub_entity TEXT)")
        conn.executemany(
            "INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?)",
            [
                ("c1", "drive_sweep", "f1", "2026-05_hjrllc_pl.xlsx", "LEX", None),
                ("c2", "drive_sweep", "f2", "2026-05_hjrllc_bs.xlsx", "LEX", "LEX-LLC"),
                ("c3", "drive_sweep", "f3", "2026-05_osn-core4_pl.xlsx", "LEX", None),
                ("c4", "drive_sweep", "f4", "2026-05_mv_pl.xlsx", "LEX", "LEX-LLC"),
                ("c5", "drive_sweep", "f5", "2026-05_llc_pl.xlsx", "LEX", "LEX-LLC"),
                ("c6", "gmail", "g1", "2026-05_hjrllc_pl.xlsx", "LEX", None),
            ],
        )
        conn.commit()

        purge_ids, retag, report = mod.scan(conn)

        assert sorted(purge_ids) == ["c1", "c2"]
        assert report["retag"]["by_target"] == {"LEX|LEX-LLA": 1, "OSN|": 1}
        # c5 is already correct; c6 is a non-Drive source and must be untouched.
        assert "c5" not in purge_ids
        assert all("c6" not in ids for ids in retag.values())
        assert report["rows_scanned"] == 5
        conn.close()

    def test_apply_retag_rewrites_only_the_targeted_rows(self, tmp_path):
        mod = _load_purge_module()
        db = tmp_path / "kb.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE knowledge_chunks (chunk_id TEXT, source TEXT, "
            "source_id TEXT, title TEXT, entity TEXT, sub_entity TEXT)")
        conn.executemany(
            "INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?)",
            [
                ("c3", "drive_sweep", "f3", "2026-05_osn-core4_pl.xlsx", "LEX", None),
                ("c4", "drive_sweep", "f4", "2026-05_mv_pl.xlsx", "LEX", "LEX-LLC"),
                ("c9", "drive_sweep", "f9", "unrelated.docx", "F3E", None),
            ],
        )
        conn.commit()

        _, retag, _ = mod.scan(conn)
        assert mod.apply_retag(conn, retag) == 2

        rows = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute(
                "SELECT chunk_id, entity, sub_entity FROM knowledge_chunks")
        )
        assert rows["c3"] == ("OSN", None)
        assert rows["c4"] == ("LEX", "LEX-LLA")
        assert rows["c9"] == ("F3E", None)
        conn.close()

    def test_apply_is_not_the_default(self):
        """A KB delete is Harrison-gated; running the script bare must never write."""
        src = (_REPO_ROOT / "scripts" / "purge_kb_personal_books_2026-08-19.py").read_text(
            encoding="utf-8")
        assert 'apply_changes = args.apply and not args.dry_run' in src
        # the cascade must come from the shared, existence-checked table list
        assert "kb_archive.delete_chunks" in src

        # Read EXECUTABLE lines only. A naive substring scan over the whole file
        # matches the docstring that explains why bare-LIKE discovery is banned,
        # so the pin would pass on a script that actually did it.
        code = "\n".join(
            line for line in src.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "*"))
        )
        body = code.split('"""', 2)[-1]
        assert "LIKE 'knowledge_vec" not in body
        assert "sqlite_master" not in body
