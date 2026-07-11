"""Tests for the dashboard-store KB exclusion (Slice 4): the kb_exclusions
predicates, the store.upsert_documents Step-0 drop, the drive_sweep excluded-
folder expansion, and the staged purge's targeting logic."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from cora import kb_exclusions
from cora.knowledge_base import embeddings
from cora.knowledge_base.store import Document, KnowledgeBase

_REPO = Path(__file__).resolve().parents[1]
_DIM = 1536

CAPITAL_FOLDER = "1BZI6v5pmpgrt7G2dPsAib3u3S-HqB7ZP"
ONEAMERICA_FOLDER = "1INi4fLXG23xao-d_yf56Wrbrah54pIBB"
TRAVEL_FOLDER = "1NPBNBfx3MMjqQM_WnmL6jOJSaRAQf752"


# --------------------------------------------------------------------------- #
# Predicates                                                                  #
# --------------------------------------------------------------------------- #
def test_excluded_folder_membership():
    assert kb_exclusions.is_excluded_folder(CAPITAL_FOLDER)
    assert kb_exclusions.is_excluded_folder(ONEAMERICA_FOLDER)
    assert kb_exclusions.is_excluded_folder(TRAVEL_FOLDER)
    assert not kb_exclusions.is_excluded_folder("1someOtherFolderId")
    assert not kb_exclusions.is_excluded_folder("")


def test_folder_ids_excluded_base_and_expanded():
    assert kb_exclusions.folder_ids_excluded(["x", CAPITAL_FOLDER])
    assert not kb_exclusions.folder_ids_excluded(["x", "y"])
    assert not kb_exclusions.folder_ids_excluded(None)
    # expanded set (roots + descendants) is honored
    expanded = frozenset({CAPITAL_FOLDER, "childFolder123"})
    assert kb_exclusions.folder_ids_excluded(["childFolder123"], expanded)
    assert not kb_exclusions.folder_ids_excluded(["childFolder123"])  # not in base set


@pytest.mark.parametrize(
    "path",
    [
        r"02-F3-Energy\projects\capital-raise\2026-07-02_f3e_blueprint.md",
        "02-F3-Energy/projects/capital-raise/2026-07-02_f3e_blueprint.md",
        "00-Founder/insurance/oneamerica/OneAmerica_Whole_Life_Tracker.xlsx",
        "00-Founder/travel-points/data/cards.json",
        "HJR-Founder-OS/02-F3-Energy/projects/capital-raise",
    ],
)
def test_is_dashboard_store_path_positive(path):
    assert kb_exclusions.is_dashboard_store_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "02-F3-Energy/projects/pure-launch/plan.md",
        "00-Founder/insurance/other/x.md",
        "gmail:harrison@hjrglobal.com:19f4950cbd19dc5d:chunk0",  # OneAmerica email, no path segment
        "1rMZJSgbnHgVnXgGvC6emRMr_jvy-NS7s",  # bare drive file id
        "",
    ],
)
def test_is_dashboard_store_path_negative(path):
    assert not kb_exclusions.is_dashboard_store_path(path)


# --------------------------------------------------------------------------- #
# store.upsert_documents Step-0 drop                                          #
# --------------------------------------------------------------------------- #
def _unit_vec():
    v = [0.0] * _DIM
    v[0] = 1.0
    return v


@pytest.fixture(autouse=True)
def _patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [_unit_vec() for _ in texts])
    monkeypatch.setattr(embeddings, "embed_query", lambda q: _unit_vec())


@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "test_kb.db")
    yield db
    db.close()


def _chunks(kb: KnowledgeBase, source_id: str) -> int:
    return kb._conn.execute(
        "SELECT COUNT(*) FROM knowledge_chunks WHERE source_id = ?", (source_id,)
    ).fetchone()[0]


def test_static_md_capital_raise_dropped(kb):
    sid = r"02-F3-Energy\projects\capital-raise\2026-07-02_f3e_blueprint.md"
    n = kb.upsert_documents([Document(
        source="static_md", source_id=sid, entity="F3E",
        content="Recap structure and raise terms for the capital program.",
        title="Blueprint",
    )])
    assert n == 0
    assert _chunks(kb, sid) == 0


def test_drive_asset_oneamerica_dropped_via_metadata_path(kb):
    sid = "1rMZJSgbnHgVnXgGvC6emRMr_jvy-NS7s"
    n = kb.upsert_documents([Document(
        source="drive_asset", source_id=sid, entity="FNDR",
        content="OneAmerica whole life tracker workbook.",
        title="OneAmerica_Whole_Life_Tracker",
        metadata={"path": "00-Founder/insurance/oneamerica/OneAmerica_Whole_Life_Tracker.xlsx"},
    )])
    assert n == 0
    assert _chunks(kb, sid) == 0


def test_normal_doc_still_ingested(kb):
    n = kb.upsert_documents([Document(
        source="asana", source_id="ok-1", entity="F3E",
        content="A perfectly normal operational note about the retail pipeline.",
        title="Retail note",
    )])
    assert n > 0
    assert _chunks(kb, "ok-1") > 0


def test_mixed_batch_only_drops_excluded(kb):
    excluded = Document(
        source="static_md",
        source_id="00-Founder/travel-points/README.md",
        entity="FNDR", content="Travel points optimizer notes.", title="Travel",
    )
    normal = Document(
        source="asana", source_id="keep-1", entity="FNDR",
        content="Board meeting follow-ups for the holdco.", title="Board",
    )
    kb.upsert_documents([excluded, normal])
    assert _chunks(kb, "00-Founder/travel-points/README.md") == 0
    assert _chunks(kb, "keep-1") > 0


# --------------------------------------------------------------------------- #
# drive_sweep excluded-folder expansion (fake Drive service)                  #
# --------------------------------------------------------------------------- #
def test_drive_sweep_folder_expansion():
    from cora.connectors import drive_sweep

    # Fake service: capital-raise has one subfolder "_notes"; _notes is a leaf.
    tree = {
        CAPITAL_FOLDER: [{"id": "_notes"}],
        "_notes": [],
        ONEAMERICA_FOLDER: [],
        TRAVEL_FOLDER: [],
    }

    class _Req:
        def __init__(self, fid):
            self._fid = fid

        def execute(self):
            return {"files": tree.get(self._fid, [])}

    class _Files:
        def list(self, *, q, **k):
            fid = q.split("'")[1]  # "'<fid>' in parents ..."
            return _Req(fid)

    class _Service:
        def files(self):
            return _Files()

    expanded = drive_sweep._expanded_excluded_folder_ids(_Service())
    assert CAPITAL_FOLDER in expanded
    assert "_notes" in expanded  # descendant folder captured


# --------------------------------------------------------------------------- #
# Purge targeting logic (synthetic KB rows; no Drive, no delete)              #
# --------------------------------------------------------------------------- #
def _load_purge_module():
    path = _REPO / "scripts" / "purge_dashboard_kb.py"
    spec = importlib.util.spec_from_file_location("purge_dashboard_kb", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_purge_targeting():
    purge = _load_purge_module()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE knowledge_chunks (chunk_id TEXT, source TEXT, source_id TEXT, "
        "title TEXT, entity TEXT, metadata TEXT)"
    )
    rows = [
        # static_md path -> PATH match
        ("c1", "static_md", r"02-F3-Energy\projects\capital-raise\x.md", "x", "F3E", None),
        # drive_asset with metadata.path -> PATH match
        ("c2", "drive_asset", "fileABC", "recap", "F3E", '{"path": "00-Founder/insurance/oneamerica/t.xlsx"}'),
        # drive_sweep bare file id, IN the live folder set -> DRIVE match
        ("c3", "drive_sweep", "fileXYZ", "blueprint", "F3E", '{"mime_type": "text/markdown"}'),
        # control: normal doc -> NO match
        ("c4", "asana", "task:123", "normal task", "FNDR", None),
        # control: drive_sweep file NOT in the folder set -> NO match
        ("c5", "drive_sweep", "otherFile", "other", "F3E", None),
    ]
    conn.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?)", rows)
    conn.commit()

    to_delete, per_source, _ = purge.target_chunks(conn, frozenset({"fileXYZ"}))
    assert set(to_delete) == {"c1", "c2", "c3"}
    assert "c4" not in to_delete and "c5" not in to_delete
    conn.close()


def test_purge_no_drive_still_matches_paths():
    purge = _load_purge_module()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE knowledge_chunks (chunk_id TEXT, source TEXT, source_id TEXT, "
        "title TEXT, entity TEXT, metadata TEXT)"
    )
    conn.executemany(
        "INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?)",
        [
            ("c1", "static_md", "00-Founder/travel-points/README.md", "t", "FNDR", None),
            ("c2", "drive_sweep", "fileXYZ", "b", "F3E", None),  # no drive set -> not matched
        ],
    )
    conn.commit()
    to_delete, _, _ = purge.target_chunks(conn, frozenset())  # --no-drive
    assert to_delete == ["c1"]
    conn.close()
