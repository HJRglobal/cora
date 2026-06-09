"""Tests for deterministic filename entity detection (drive_entity_detect.py).

The personal-Drive sweep used Haiku for entity attribution and mis-tagged HJR
files (OSN P&L -> LEX, HJRP invoice -> LEX-LLC). detect_entity_from_filename
trusts the HJR naming convention's entity-code token when it is unambiguous and
returns None otherwise (caller keeps Haiku's guess).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors.drive_entity_detect import detect_entity_from_filename as d


def test_observed_misclassified_files_now_resolve():
    # These are the exact filenames Haiku mis-tagged in the 6/06-6/07 logs.
    assert d("2026-04_osn-gf_pl.xlsx") == "OSN"
    assert d("2026-06-01_hjrp_hjr-properties-invoice-2041.pdf") == "HJRP"


def test_lex_and_hjrp_sub_entities():
    assert d("2026-03_lts_cf.xlsx") == "LEX-LTS"
    assert d("2026-06_lex-lbhs_copa.pdf") == "LEX-LBHS"
    assert d("2026-06_lex-llc_main.pdf") == "LEX-LLC"
    assert d("2026-01_hjrp-rr_launch.md") == "HJRP-RR"
    assert d("2026-01_hjrp-cl_pnl.xlsx") == "HJRP-CL"


def test_core_entity_codes():
    assert d("2026-05-01_ufl_season-2-sponsor-deck.pptx") == "UFL"
    assert d("2026-02_f3e_retailer-pitch.pdf") == "F3E"
    assert d("2026-02_f3c_990n.pdf") == "F3C"
    assert d("2026-02_bdm_brief.md") == "BDM"
    assert d("2026-02_fndr_strategy.md") == "FNDR"


def test_productions_subcodes_roll_up_to_hjrprod():
    assert d("2026-06_pod_episode-42.md") == "HJRPROD"
    assert d("2026-06_ff_chapter-3.docx") == "HJRPROD"
    assert d("2026-06_hjrprod_overview.md") == "HJRPROD"


def test_no_date_prefix_still_detects():
    assert d("hjrg_payroll_2026.csv") == "HJRG"
    assert d("osn_item-sales.xlsx") == "OSN"
    assert d("f3e.md") == "F3E"


def test_osn_store_codes_collapse_to_parent():
    assert d("2026-04_osn-gw_pl.xlsx") == "OSN"
    assert d("2026-04_osn-vvp_pl.xlsx") == "OSN"


def test_conservative_no_false_positives():
    # 'osn' appears only as a 3rd+ token (a description word) -> no override.
    assert d("report_for_osn_team.pdf") is None
    # Ambiguous 'hjr' alone never matches (could be hjrg/hjrp/hjrprod).
    assert d("hjr_x.pdf") is None
    # Space-delimited names don't follow the underscore convention -> None.
    assert d("LLC Main Jan 2026.pdf") is None
    # Description-only token.
    assert d("2026-05-21_b-entity-voice-patch.md") is None
    assert d("") is None
    assert d("noextension") is None


def test_case_insensitive():
    assert d("2026-04_OSN-GF_PL.XLSX") == "OSN"
    assert d("2026-06_LEX-LLC_main.PDF") == "LEX-LLC"
