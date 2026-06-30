"""Tests for the nightly Drive materialization (drive_materializer.py, 2026-06-29).

Builds a tiny real KB (schema.connect loads sqlite-vec) in a temp dir, seeds chunks
directly (no embeddings), and drives run() with a fake LLM client. Covers: the
non-vector watermark query, per-entity write + watermark advance, fail-closed distill,
the LEX PHI wall (LBHS hard-exclude + scrub + drop-if-residual), and the _brain/swept
loop-guard on BOTH ingest walks.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import drive_materializer as dm  # noqa: E402
from cora.knowledge_base import KnowledgeBase  # noqa: E402

NOW = 1_780_000_000
DAY = 86400


# ── fixtures / helpers ──────────────────────────────────────────────────────

@pytest.fixture()
def kb(tmp_path):
    k = KnowledgeBase(tmp_path / "kb.db")
    yield k
    k.close()


def _insert(kb, *, source, entity, ingested_at, content="some swept body text",
            sub_entity=None, cid=None, title=""):
    cid = cid or f"{source}-{entity}-{ingested_at}-{sub_entity or 'na'}"
    kb._conn.execute(
        "INSERT INTO knowledge_chunks "
        "(chunk_id, source, source_id, entity, content, ingested_at, sub_entity, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, source, f"src-{cid}", entity, content, int(ingested_at), sub_entity, title),
    )
    kb._conn.commit()


_CLEAN_DIGEST = (
    "## Decisions\n- Approved the Q3 plan.\n"
    "## Action items / follow-ups\n- Larry to send the deck.\n"
    "## Key facts & updates\n- Launch on track.\n"
    "## Notable communications\n- Harrison -> Tommy: confirmed pricing.\n"
    "## Who-owns-what changes\n- (none)\n"
)


class FakeClient:
    """Stands in for anthropic.Anthropic; records the prompts it received."""
    def __init__(self, text=_CLEAN_DIGEST, raise_exc=False):
        self.text = text
        self.raise_exc = raise_exc
        self.prompts: list[str] = []
        self.messages = self  # so client.messages.create(...) resolves to .create

    def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        if self.raise_exc:
            raise RuntimeError("boom")
        return SimpleNamespace(content=[SimpleNamespace(text=self.text)])


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWEPT_DIR", str(tmp_path / "swept"))
    monkeypatch.setenv("MATERIALIZATION_WATERMARK_PATH", str(tmp_path / "wm.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # so _get_client wouldn't bail (we inject anyway)
    # CRITICAL: point the flywheel mirror at tmp so run()'s end-of-run mirror never
    # touches the real Drive _brain/_flywheel during tests.
    monkeypatch.setenv("FLYWHEEL_MIRROR_DIR", str(tmp_path / "flywheel"))
    return tmp_path


# ── KB query (get_chunks_since) ──────────────────────────────────────────────

class TestGetChunksSince:
    def test_filters_source_entity_and_watermark(self, kb):
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW - 5 * DAY, cid="old")
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW - 1 * DAY, cid="new")
        _insert(kb, source="gmail", entity="OSN", ingested_at=NOW, cid="osn")
        _insert(kb, source="slack", entity="F3E", ingested_at=NOW, cid="slack")
        out = kb.get_chunks_since(source="gmail", entity="F3E", since_ts=NOW - 3 * DAY)
        ids = {c["chunk_id"] for c in out}
        assert ids == {"new"}  # old is before watermark; OSN + slack are other source/entity

    def test_oldest_first_ordering(self, kb):
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW - 1 * DAY, cid="b")
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW - 2 * DAY, cid="a")
        out = kb.get_chunks_since(source="gmail", entity="F3E", since_ts=0)
        assert [c["chunk_id"] for c in out] == ["a", "b"]

    def test_user_note_never_returned(self, kb):
        _insert(kb, source="user_note", entity="FNDR", ingested_at=NOW, cid="note")
        assert kb.get_chunks_since(source="user_note", entity="FNDR", since_ts=0) == []

    def test_lex_excludes_lbhs_keeps_gm_and_subs(self, kb):
        _insert(kb, source="gmail", entity="LEX", ingested_at=NOW, sub_entity=None, cid="gm")
        _insert(kb, source="gmail", entity="LEX", ingested_at=NOW, sub_entity="LEX-LLC", cid="llc")
        _insert(kb, source="gmail", entity="LEX", ingested_at=NOW, sub_entity="LEX-LTS", cid="lts")
        _insert(kb, source="gmail", entity="LEX", ingested_at=NOW, sub_entity="LEX-LBHS", cid="lbhs")
        out = kb.get_chunks_since(source="gmail", entity="LEX", since_ts=0,
                                  exclude_sub_entities=("LEX-LBHS",))
        ids = {c["chunk_id"] for c in out}
        assert ids == {"gm", "llc", "lts"}
        assert "lbhs" not in ids


# ── run(): write / watermark / skip / fail-closed ────────────────────────────

class TestRun:
    def test_writes_file_and_advances_watermark(self, kb, env):
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW, cid="x")
        fc = FakeClient()
        stats = dm.run(today=date(2026, 6, 29), client=fc, kb=kb, lookback_hours=24 * 3650)
        out = Path(env) / "swept" / "F3E" / "2026-06-29.md"
        assert out.exists()
        txt = out.read_text(encoding="utf-8")
        assert "F3E — swept-knowledge digest — 2026-06-29" in txt
        assert "Harrison -> Tommy: confirmed pricing." in txt
        assert stats["entities_written"] == 1
        wm = dm._load_watermarks()
        assert wm[dm._wm_key("F3E", "gmail")] == NOW

    def test_no_new_chunks_writes_nothing(self, kb, env):
        # lookback tiny so the seeded old chunk is before the seed window
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW - 100 * DAY, cid="old")
        fc = FakeClient()
        stats = dm.run(today=date(2026, 6, 29), client=fc, kb=kb, lookback_hours=1)
        assert stats["entities_written"] == 0
        assert stats["entities_no_new"] >= 1
        assert not (Path(env) / "swept").exists() or not list((Path(env) / "swept").rglob("*.md"))

    def test_failclosed_on_llm_error_no_write_no_watermark(self, kb, env):
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW, cid="x")
        fc = FakeClient(raise_exc=True)
        stats = dm.run(today=date(2026, 6, 29), client=fc, kb=kb, lookback_hours=24 * 3650)
        assert stats["entities_written"] == 0
        assert stats["entities_skipped"] >= 1
        assert not (Path(env) / "swept" / "F3E" / "2026-06-29.md").exists()
        assert dm._load_watermarks().get(dm._wm_key("F3E", "gmail")) is None  # not advanced

    def test_dry_run_writes_nothing_and_no_watermark(self, kb, env):
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW, cid="x")
        fc = FakeClient()
        stats = dm.run(today=date(2026, 6, 29), client=fc, kb=kb, lookback_hours=24 * 3650, dry_run=True)
        assert stats["entities_written"] == 1  # it distilled
        assert not (Path(env) / "swept" / "F3E" / "2026-06-29.md").exists()  # but wrote nothing
        assert dm._load_watermarks() == {}  # and advanced no watermark

    def test_aborts_cleanly_without_llm_client(self, kb, env, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW, cid="x")
        stats = dm.run(today=date(2026, 6, 29), client=None, kb=kb)
        assert stats.get("aborted") == "no_llm_client"
        assert stats["entities_written"] == 0


# ── LEX PHI wall ─────────────────────────────────────────────────────────────

class TestLexPhiWall:
    def test_lex_dropped_when_residual_phi_survives_scrub(self, kb, env):
        _insert(kb, source="gmail", entity="LEX", ingested_at=NOW, sub_entity=None, cid="gm")
        # scrub_lex_phi does NOT touch client-status phrasing, so it survives -> DROP.
        dirty = (
            "## Decisions\n- (none)\n## Action items / follow-ups\n- (none)\n"
            "## Key facts & updates\n- (none)\n"
            "## Notable communications\n- Reviewed client status: the member is active and was discharged Tuesday.\n"
            "## Who-owns-what changes\n- (none)\n"
        )
        fc = FakeClient(text=dirty)
        stats = dm.run(today=date(2026, 6, 29), client=fc, kb=kb, lookback_hours=24 * 3650)
        assert stats["lex_dropped"] == 1
        assert not (Path(env) / "swept" / "LEX" / "2026-06-29.md").exists()
        assert dm._load_watermarks().get(dm._wm_key("LEX", "gmail")) is None  # not advanced

    def test_lex_written_when_clean_gm_level(self, kb, env):
        _insert(kb, source="gmail", entity="LEX", ingested_at=NOW, sub_entity=None, cid="gm",
                content="DTA program staffing update")
        clean = (
            "## Decisions\n- (none)\n## Action items / follow-ups\n- (none)\n"
            "## Key facts & updates\n- The DTA program added two staff this week.\n"
            "## Notable communications\n- (none)\n## Who-owns-what changes\n- (none)\n"
        )
        fc = FakeClient(text=clean)
        stats = dm.run(today=date(2026, 6, 29), client=fc, kb=kb, lookback_hours=24 * 3650)
        assert stats["entities_written"] == 1
        out = Path(env) / "swept" / "LEX" / "2026-06-29.md"
        assert out.exists()
        assert "LBHS (42 CFR Part 2) excluded" in out.read_text(encoding="utf-8")

    def test_lbhs_chunk_never_reaches_the_llm(self, kb, env):
        _insert(kb, source="gmail", entity="LEX", ingested_at=NOW, sub_entity=None,
                cid="gm", content="GM-LEVEL-OPERATIONAL-NOTE")
        _insert(kb, source="gmail", entity="LEX", ingested_at=NOW, sub_entity="LEX-LBHS",
                cid="lbhs", content="LBHS-PART2-SECRET-CONTENT")
        fc = FakeClient()
        dm.run(today=date(2026, 6, 29), client=fc, kb=kb, lookback_hours=24 * 3650)
        all_prompts = "\n".join(fc.prompts)
        assert "GM-LEVEL-OPERATIONAL-NOTE" in all_prompts        # GM content reached distill
        assert "LBHS-PART2-SECRET-CONTENT" not in all_prompts    # LBHS excluded at the query


# ── _brain/swept loop-guard on BOTH ingest walks ─────────────────────────────

class TestSweptIngestGuards:
    def test_static_is_swept_path(self):
        import incremental_sync_static as iss
        root = Path(r"G:\My Drive\HJR-Founder-OS")
        assert iss.is_swept_path(root / "_brain" / "swept" / "F3E" / "2026-06-29.md")
        # MUST NOT exclude the curated _brain layers
        assert not iss.is_swept_path(root / "_brain" / "known-answers" / "f3e.md")
        assert not iss.is_swept_path(root / "_brain" / "reference" / "org-roles.yaml")
        assert not iss.is_swept_path(root / "02-F3-Energy" / "CLAUDE.md")

    def test_static_file_to_document_skips_swept(self, tmp_path, monkeypatch):
        import incremental_sync_static as iss
        # a real file under a _brain/swept path (use tmp + monkeypatch the root)
        swept = tmp_path / "_brain" / "swept" / "F3E" / "2026-06-29.md"
        swept.parent.mkdir(parents=True, exist_ok=True)
        swept.write_text("# digest", encoding="utf-8")
        monkeypatch.setattr(iss, "FOUNDER_OS_ROOT", tmp_path)
        assert iss.file_to_document(swept) is None

    def test_drive_connector_blacklists_brain_swept_only(self):
        from cora.connectors import drive_connector as dc
        assert dc._is_blacklisted_path(["_brain", "swept", "F3E", "2026-06-29.md"])
        assert dc._is_blacklisted_path(["HJR-Founder-OS", "_BRAIN", "Swept", "x.md"])  # case-insensitive
        # the curated layers are NOT blacklisted by the swept rule
        assert not dc._is_blacklisted_path(["_brain", "known-answers", "f3e.md"])
        assert not dc._is_blacklisted_path(["_brain", "people", "harrison.md"])

    def test_drive_sweep_skip_folders_includes_swept(self):
        from cora.connectors import drive_sweep as ds
        assert "swept" in ds._FOUNDERS_OS_SKIP_FOLDERS


# ── Change 3: flywheel-ledger mirror ─────────────────────────────────────────

class TestFlywheelMirror:
    def test_mirrors_existing_ledgers_skips_missing(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / "data").mkdir(parents=True)
        (repo / "logs").mkdir(parents=True)
        (repo / "design" / "known-answers").mkdir(parents=True)
        (repo / "data" / "cora-proposed-memory-updates.jsonl").write_text('{"a":1}\n', encoding="utf-8")
        (repo / "logs" / "knowledge-gaps.jsonl").write_text('{"g":1}\n', encoding="utf-8")
        (repo / "design" / "known-answers" / ".resolved-gaps.jsonl").write_text('{"id":"x"}\n', encoding="utf-8")
        # cora-reply-log.jsonl + the archive intentionally absent -> must be skipped, not error
        dest = tmp_path / "flywheel"
        monkeypatch.setenv("FLYWHEEL_MIRROR_DIR", str(dest))
        mirrored = dm.mirror_flywheel_ledgers(repo_root=repo)
        assert set(mirrored) == {
            "cora-proposed-memory-updates.jsonl", "knowledge-gaps.jsonl", ".resolved-gaps.jsonl",
        }
        assert (dest / "cora-proposed-memory-updates.jsonl").read_text(encoding="utf-8") == '{"a":1}\n'
        assert (dest / ".resolved-gaps.jsonl").read_text(encoding="utf-8") == '{"id":"x"}\n'
        assert not (dest / "cora-reply-log.jsonl").exists()   # missing source -> skipped
        assert list(dest.glob("*.tmp")) == []                 # atomic, no temp residue

    def test_mirror_never_raises_when_all_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLYWHEEL_MIRROR_DIR", str(tmp_path / "flywheel"))
        assert dm.mirror_flywheel_ledgers(repo_root=tmp_path / "empty-repo") == []

    def test_run_invokes_mirror_at_end(self, kb, env):
        _insert(kb, source="gmail", entity="F3E", ingested_at=NOW, cid="x")
        stats = dm.run(today=date(2026, 6, 29), client=FakeClient(), kb=kb, lookback_hours=24 * 3650)
        assert "flywheel_mirrored" in stats   # mirror ran (key present) on a non-dry run

