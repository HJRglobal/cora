"""scripts/refile_misrouted_session_captures.py -- the Leak #2 repair (I2(c)),
as remediated by the D-051 lens-B review (2026-09-08).

Synthetic Founder-OS tree + a real sqlite KB (schema.connect, so the vector
cascade is exercised for real: the fixture populates the vec0 bin table AND the
f32 table, and the apply test asserts both were cascaded -- the first fixture
proved the vec0 leg vacuously). The classifier is faked; nothing here touches
Drive, Haiku or the live KB.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import session_capture as scap  # noqa: E402
from cora.knowledge_base import schema  # noqa: E402


def _load():
    path = _REPO_ROOT / "scripts" / "refile_misrouted_session_captures.py"
    spec = importlib.util.spec_from_file_location("refile_misrouted_session_captures", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclasses resolve PEP-563 annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


rf = _load()

LEX_CAP = Path("08-Lexington-Services") / "_session-captures" / "2026-09"
PHI = "- PHI: yes (LEX-scoped, access-controlled)"
PROSE_PHI = "client Marcus Johnson was diagnosed with autism at intake"
A = "2026-09-04_cowork-session_aaaaaaaa.md"
B = "2026-09-04_cowork-session_bbbbbbbb.md"
C = "2026-09-04_cowork-session_cccccccc.md"
D = "2026-09-04_cowork-session_dddddddd.md"
E = "2026-09-05_cowork-session_eeeeeeee.md"
F = "2026-08-29_cowork-session_ffffffff.md"


def _note(entity: str, topic: str, *, stamp: bool = True, extra: str = "", sid: str = "local_x",
          cwd: str = r"C:\Users\Harri\code\cora") -> str:
    return (
        f"## 2026-09-04 — cowork-session — {entity} — {topic}\n\n"
        f"- Decisions:\n  - locked X\n- Facts learned:\n  - {extra or 'Y is true'}\n"
        f"- Action items:\n  - (none)\n- Open questions:\n  - (none)\n"
        f"- Source session id: {sid}\n- Source cwd: {cwd}\n"
        + (PHI + "\n" if stamp else "")
        + "- Captured: 2026-09-04T12:34:05+00:00\n"
    )


@pytest.fixture
def fos(tmp_path: Path) -> Path:
    root = tmp_path / "fos"
    d = root / LEX_CAP
    d.mkdir(parents=True)
    (d / A).write_text(_note("LEX", "Walmart maintenance posture", extra="paused Auto_Discovery campaign", sid="local_aaaaaaaa"), encoding="utf-8")
    (d / B).write_text(_note("LEX", "F3 sample shipment", extra=PROSE_PHI, sid="local_bbbbbbbb"), encoding="utf-8")
    (d / C).write_text(_note("LEX", "Lexington website rebuild", extra="Book-a-Tour picker", sid="local_cccccccc"), encoding="utf-8")
    (d / D).write_text(_note("LEX", "LEX recap", stamp=False, sid="local_dddddddd"), encoding="utf-8")
    (d / E).write_text(_note("LEX", "garbage classifier", sid="local_eeeeeeee"), encoding="utf-8")
    (d / "2026-09-03_lex_recap-lexington-progress-meeting.md").write_text("# hand-written recap\n", encoding="utf-8")
    # a non-LEX capture folder already holding a file (twin-disagreement fixture)
    f3 = root / "02-F3-Energy" / "_session-captures" / "2026-08"
    f3.mkdir(parents=True)
    (f3 / F).write_text(_note("F3E", "Air launch", stamp=False), encoding="utf-8")
    return root


def _bin_table(conn) -> str:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return "knowledge_vec_bin_v2" if "knowledge_vec_bin_v2" in names else "knowledge_vec_bin"


@pytest.fixture
def kb_db(tmp_path: Path, fos: Path) -> Path:
    db = tmp_path / "kb.db"
    conn = schema.connect(db)
    schema.init_schema(conn)
    rows = [
        # static_md rows for the misfiled files (old LEX path, backslash form as the harvester writes)
        ("s1", "static_md", str(LEX_CAP / A), "LEX", None, "Session capture — Walmart", "c", None),
        ("s2", "static_md", str(LEX_CAP / A), "LEX", "LEX-LLC", "Session capture — Walmart", "c2", None),
        ("s3", "static_md", str(LEX_CAP / B), "LEX", None, "Session capture — F3", "c", None),
        ("se", "static_md", str(LEX_CAP / E), "LEX", None, "Session capture — garbage", "c", None),
        # drive twins of the misfiled file A (LEX) -- deleted with the move ...
        ("d1", "drive_sweep", "driveFileA", "LEX", None, A, "c", None),
        ("d2", "drive_asset", "driveFileA2", "LEX", None, A, "c", None),
        # ... but a Drive row ALREADY tagged with the target entity is right and stays (lens B #4)
        ("d1b", "drive_sweep", "driveFileA3", "F3E", None, A, "c", None),
        # the HELD file E has a CORRECT (FNDR) Drive twin: the twin pass must leave it alone
        ("de", "drive_sweep", "driveFileE", "FNDR", None, E, "c", None),
        # the F3E capture file: static says F3E, the flat sweep's twin says FNDR -> twin disagreement
        ("s9", "static_md", "02-F3-Energy\\_session-captures\\2026-08\\" + F, "F3E", None, "Session capture — Air", "c", None),
        ("d9", "drive_sweep", "driveFileF", "FNDR", None, F, "c", None),
        ("d9b", "drive_sweep", "driveFileF", "F3E", None, F, "c", None),
        # controls that must survive
        ("x1", "gmail", "msg1", "LEX", None, A, "an email that merely shares the title", None),
        ("x2", "static_md", "08-Lexington-Services\\_session-captures\\2026-09\\2026-09-03_lex_recap-lexington-progress-meeting.md", "LEX", None, "recap", "c", None),
    ]
    conn.executemany(
        "INSERT INTO knowledge_chunks (chunk_id, source, source_id, entity, sub_entity, title, content, metadata, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?, 1788000000)", rows)
    bin_tbl = _bin_table(conn)
    for cid, _src, _sid, ent, *_ in rows:
        conn.execute("INSERT INTO knowledge_vec_f32 (chunk_id, embedding) VALUES (?, ?)", (cid, b"\x00" * 16))
        # a REAL vec0 row (bit[1536] = 192 bytes) so the cascade's vec0 leg is proven, not vacuous
        conn.execute(f"INSERT INTO {bin_tbl} (chunk_id, entity, embedding) VALUES (?, ?, vec_quantize_binary(?))",
                     (cid, ent, b"\x00\x00\x80\x3f" * 1536))     # a float32 vector -> vec0 bit[1536]
    conn.commit()
    conn.close()
    return db


ALL_IDS = {"s1", "s2", "s3", "se", "d1", "d2", "d1b", "de", "s9", "d9", "d9b", "x1", "x2"}


class _Classifier:
    """Fake Haiku: answers by note content; 'garbage' -> unparseable."""
    def __init__(self):
        self.calls: list[str] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        prompt = kw["messages"][0]["content"]
        self.calls.append(prompt)
        if "garbage classifier" in prompt:
            text = "no json here"
        elif "Lexington website rebuild" in prompt:
            text = json.dumps({"entity": "LEX"})
        else:
            text = json.dumps({"entity": "F3E"})
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _ro(db: Path) -> sqlite3.Connection:
    return schema.connect(db, read_only=True)


def _ids(conn, table="knowledge_chunks") -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT chunk_id FROM {table}")}


# ── discovery / parsing / decisions ───────────────────────────────────────────
def test_discovery_finds_only_harvester_files(fos):
    names = [p.name for p in rf.iter_harvester_files(fos)]
    assert names == sorted(names)
    assert "2026-09-03_lex_recap-lexington-progress-meeting.md" not in names
    assert A in names and len(names) == 5


def test_parse_note_reads_header_cwd_and_stamp(fos):
    text = (fos / LEX_CAP / A).read_text(encoding="utf-8")
    info = rf.parse_note(text)
    assert info["header_ok"] and info["entity"] == "LEX" and info["surface"] == "cowork-session"
    assert info["topic"] == "Walmart maintenance posture"
    assert info["cwd"].endswith("code\\cora") and info["session_id"] == "local_aaaaaaaa"
    assert info["phi_stamp"] is True
    assert rf.parse_note("not a note")["header_ok"] is False


def test_decide_matrix():
    action, reason = rf.decide(None, "x")
    assert action == "HOLD" and reason.startswith("classifier failed")
    assert rf.decide("LEX", "x")[0] == "KEEP" and rf.decide("LEX-LTS", "x")[0] == "KEEP"
    assert rf.decide("F3E", "ordinary launch notes; diagnosis of the arc")[0] == "MOVE"
    assert rf.decide("F3E", PROSE_PHI)[0] == "QUARANTINE"


def _answer(text):
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(content=[SimpleNamespace(text=text)])))


def test_classify_entity_fail_closed_aliases_and_unsure():
    assert rf.classify_entity("x", "FNDR", None) is None
    assert rf.classify_entity("x", "FNDR", _answer('{"entity": "MARS"}')) is None
    boom = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("api"))))
    assert rf.classify_entity("x", "FNDR", boom) is None
    assert rf.classify_entity("x", "FNDR", _answer('{"entity": "osn"}')) == "OSN"
    assert rf.classify_entity("x", "FNDR", _answer('{"entity": "Lexington"}')) == "LEX"        # alias folded
    assert rf.classify_entity("x", "FNDR", _answer('{"entity": "F3 Energy"}')) == "F3E"
    assert rf.classify_entity("x", "FNDR", _answer('{"entity": "UNSURE"}')) is None            # an honest UNSURE holds
    assert "UNSURE" in rf._CLASSIFY_PROMPT and "never guess" in rf._CLASSIFY_PROMPT


def test_dest_and_rewrite(fos):
    dst = rf.dest_for("MOVE", "F3E", A, fos)
    assert dst == fos / "02-F3-Energy" / "_session-captures" / "2026-09" / A
    q = rf.dest_for("QUARANTINE", "F3E", B, fos)
    assert q == scap.quarantine_root(fos) / "2026-09" / (scap.QUARANTINE_PREFIX + B)
    text = _note("LEX", "t")
    out = rf.rewrite_note(text, "F3E", "MOVE", "src/rel", "2026-09-08T00:00:00+00:00")
    assert out.startswith("## 2026-09-04 — cowork-session — F3E — t")
    assert PHI not in out and "- Re-filed: 2026-09-08T00:00:00+00:00 from src/rel" in out
    outq = rf.rewrite_note(text, "F3E", "QUARANTINE", "src/rel", "now")
    assert "- QUARANTINED: now" in outq and PHI not in outq


# ── gates ─────────────────────────────────────────────────────────────────────
def test_gates_reject_paths_outside_the_two_folders(fos):
    with pytest.raises(rf.GateTripped):
        rf.assert_src_allowed(fos / "02-F3-Energy" / "_session-captures" / "2026-08" / F, fos)
    with pytest.raises(rf.GateTripped):
        rf.assert_src_allowed(fos / LEX_CAP / "2026-09-03_lex_recap-lexington-progress-meeting.md", fos)
    rf.assert_src_allowed(fos / LEX_CAP / A, fos)
    with pytest.raises(rf.GateTripped):   # never INTO the LEX partition
        rf.assert_dst_allowed(fos / LEX_CAP / A, fos)
    with pytest.raises(rf.GateTripped):
        rf.assert_dst_allowed(fos / "02-F3-Energy" / "notes" / "x.md", fos)
    rf.assert_dst_allowed(fos / "02-F3-Energy" / "_session-captures" / "2026-09" / "x.md", fos)
    rf.assert_dst_allowed(scap.quarantine_root(fos) / "2026-09" / "cora-quarantine-x.md", fos)


# ── plan (dry-run) ────────────────────────────────────────────────────────────
def test_build_plan_actions_counts_topics_and_twins(fos, kb_db, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"session_id": "cowork:local_aaaaaaaa", "note_path": str(fos / LEX_CAP / A),
                                  "captured_at": "2026-09-04T12:34:05+00:00"}) + "\n", encoding="utf-8")
    clf = _Classifier()
    conn = _ro(kb_db)
    rows, twins = rf.build_plan(conn, fos, ledger, clf)
    conn.close()
    by = {r.filename: r for r in rows}
    a = by[A]
    assert a.action == "MOVE" and a.target_entity == "F3E" and a.kb_static_chunks == 2 and a.kb_drive_chunks == 3
    assert a.kb_drive_entities == ["F3E", "LEX"] and a.session_id == "cowork:local_aaaaaaaa"
    assert a.topic == "Walmart maintenance posture"                     # the reviewer's eyeball column
    assert a.dst_rel.replace("\\", "/") == "02-F3-Energy/_session-captures/2026-09/" + A
    assert set(a.static_source_ids) >= {str(LEX_CAP / A)}
    assert by[B].action == "QUARANTINE"
    assert by[B].dst_rel.replace("\\", "/").startswith("_shared/projects/cora/_session-capture-quarantine/2026-09/cora-quarantine-")
    assert by[C].action == "KEEP"
    d = by[D]
    assert d.action == "KEEP" and "not a phi-forced" in d.reason
    assert by[E].action == "HOLD"
    # the un-stamped genuine LEX note was never sent to the classifier
    assert not any("LEX recap" in c for c in clf.calls)
    # twins: ONLY the F3E file's FNDR-tagged drive row disagrees. The HELD file E's
    # FNDR twin is the CORRECT tag of a misfiled file -- never a twin candidate.
    assert [t.filename for t in twins] == [F]
    assert twins[0].static_entity == "F3E" and twins[0].drive_entities == ["FNDR"] and twins[0].drive_chunks == 1
    summ = rf.summarize(rows, twins)
    assert summ["by_action"] == {"KEEP": 2, "MOVE": 1, "QUARANTINE": 1, "HOLD": 1}
    assert summ["moves_by_target"] == {"F3E": 2} and summ["lex_chunks_to_purge_static"] == 3


def test_limit_skips_the_twin_pass(fos, kb_db, tmp_path):
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    conn = _ro(kb_db)
    rows, twins = rf.build_plan(conn, fos, ledger, _Classifier(), limit=2)
    conn.close()
    assert len(rows) == 2 and twins == []


def test_dry_run_main_writes_intent_only(fos, kb_db, tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "_REPO_ROOT", tmp_path)          # logs/ lands in tmp
    clf = _Classifier()
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda api_key: clf))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    rc = rf.main(["--db", str(kb_db), "--root", str(fos), "--ledger", str(ledger)])
    assert rc == 0
    intents = list((tmp_path / "logs").glob("refile-session-captures-INTENT-*.json"))
    assert len(intents) == 1
    data = json.loads(intents[0].read_text(encoding="utf-8"))
    assert data["kind"] == "INTENT" and data["summary"]["by_action"]["MOVE"] == 1
    assert data["partition_counts_before"]["static_md"]["LEX"] == 5
    txt = intents[0].with_suffix(".txt").read_text(encoding="utf-8")
    assert "Walmart maintenance posture" in txt and "|  topic  ->" in txt   # the eyeball column
    # nothing moved, nothing deleted
    assert (fos / LEX_CAP / A).exists()
    conn = _ro(kb_db)
    assert _ids(conn) == ALL_IDS
    conn.close()


# ── apply ─────────────────────────────────────────────────────────────────────
class _FakeKB:
    def __init__(self, conn):
        self._conn = conn
        self.upserts = []

    def upsert_documents(self, docs):
        self.upserts.extend(list(docs))
        return len(self.upserts)

    def close(self):
        pass


def _plan(fos, kb_db, ledger):
    conn = _ro(kb_db)
    try:
        return rf.build_plan(conn, fos, ledger, _Classifier())
    finally:
        conn.close()


def test_apply_moves_quarantines_purges_reingests_and_records(fos, kb_db, tmp_path):
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    rows, twins = _plan(fos, kb_db, ledger)
    conn = schema.connect(kb_db)
    kb = _FakeKB(conn)
    before = rf.partition_counts(conn)
    out = rf.apply_rows(conn, kb, fos, ledger, rows, twins, accept_delta=False)
    after = rf.partition_counts(conn)
    assert out["moved"] == 1 and out["quarantined"] == 1 and out["reingested"] == 1 and out["skipped"] == 0
    assert out["resumed"] == 0 and out["twins_skipped_stale"] == 0
    # files
    dst = fos / "02-F3-Energy" / "_session-captures" / "2026-09" / A
    assert dst.exists() and not (fos / LEX_CAP / A).exists()
    text = dst.read_text(encoding="utf-8")
    assert text.startswith("## 2026-09-04 — cowork-session — F3E — Walmart") and PHI not in text and "- Re-filed:" in text
    q = list(scap.quarantine_root(fos).rglob("cora-quarantine-*.md"))
    assert len(q) == 1 and "QUARANTINED" in q[0].read_text(encoding="utf-8")
    assert not (fos / LEX_CAP / B).exists()
    assert (fos / LEX_CAP / C).exists()      # KEEP
    assert (fos / LEX_CAP / D).exists()      # KEEP
    assert (fos / LEX_CAP / E).exists()      # HOLD
    # KB: A's LEX static + LEX drive rows gone, its F3E-tagged Drive row KEPT (lens B #4),
    # B's static row gone, the held file's static row + correct twin survive, controls
    # survive, twin d9 gone, d9b kept
    left = _ids(conn)
    assert left == {"d1b", "se", "de", "s9", "d9b", "x1", "x2"}
    assert _ids(conn, "knowledge_vec_f32") == left                     # the f32 leg cascaded
    assert _ids(conn, _bin_table(conn)) == left                        # the vec0 leg cascaded (not vacuous)
    assert out["twin_chunks_deleted"] == 1
    # re-ingest under the new partition (MOVE only; the quarantined note never reaches the KB)
    assert len(kb.upserts) == 1
    doc = kb.upserts[0]
    assert doc.entity == "F3E" and doc.source == "static_md" and doc.sub_entity is None
    assert doc.source_id.replace("\\", "/") == "02-F3-Energy/_session-captures/2026-09/" + A
    assert doc.metadata["refiled_from"].replace("\\", "/").startswith("08-Lexington-Services/_session-captures")
    # ledger rows: one per moved/quarantined file, refiled provenance kept, dedup ids unchanged
    lrows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lrows) == 2 and {r["entity"] for r in lrows} == {"F3E"}
    assert any(r["quarantined"] for r in lrows) and any(not r["quarantined"] for r in lrows)
    assert before["static_md"]["LEX"] == 5 and after["static_md"]["LEX"] == 2
    conn.close()


def test_apply_skips_a_file_that_changed_since_the_dry_run(fos, kb_db, tmp_path):
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    rows, twins = _plan(fos, kb_db, ledger)
    target = fos / LEX_CAP / A
    target.write_text(target.read_text(encoding="utf-8") + "- edited after the eyeball\n", encoding="utf-8")
    conn = schema.connect(kb_db)
    out = rf.apply_rows(conn, _FakeKB(conn), fos, ledger, rows, [], accept_delta=False)
    assert out["moved"] == 0 and out["skipped"] == 1
    assert target.exists()
    a = next(r for r in rows if r.filename == A)
    assert "content-changed" in a.result
    # nothing of A's was deleted
    assert conn.execute("SELECT count(*) FROM knowledge_chunks WHERE chunk_id IN ('s1','s2','d1','d2')").fetchone()[0] == 4
    conn.close()


def test_accept_delta_re_decides_on_the_new_text(fos, kb_db, tmp_path):
    """--accept-delta must never execute the STALE decision: a MOVE row whose file now
    carries value-shaped PHI is re-decided (QUARANTINE) and skipped for a fresh dry-run."""
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    rows, twins = _plan(fos, kb_db, ledger)
    target = fos / LEX_CAP / A
    target.write_text(target.read_text(encoding="utf-8") + f"- {PROSE_PHI}\n", encoding="utf-8")
    conn = schema.connect(kb_db)
    out = rf.apply_rows(conn, _FakeKB(conn), fos, ledger, rows, [], accept_delta=True)
    a = next(r for r in rows if r.filename == A)
    assert out["moved"] == 0 and "decision-changed-since-dry-run (was MOVE, now QUARANTINE" in a.result
    assert target.exists() and not (fos / "02-F3-Energy" / "_session-captures" / "2026-09" / A).exists()
    assert conn.execute("SELECT count(*) FROM knowledge_chunks WHERE chunk_id IN ('s1','s2','d1','d2')").fetchone()[0] == 4
    # an edit that does NOT change the decision goes through under --accept-delta
    target.write_text(_note("LEX", "Walmart maintenance posture", extra="paused Auto_Discovery campaign; one more fact", sid="local_aaaaaaaa"), encoding="utf-8")
    rows2, _ = _plan(fos, kb_db, ledger)                     # planned on v2 (MOVE)
    target.write_text(_note("LEX", "Walmart maintenance posture", extra="paused Auto_Discovery campaign; one more fact; and another", sid="local_aaaaaaaa"), encoding="utf-8")
    a2 = [r for r in rows2 if r.filename == A]
    out2 = rf.apply_rows(conn, _FakeKB(conn), fos, ledger, a2, [], accept_delta=True)
    assert out2["moved"] == 1 and not target.exists()
    conn.close()


def test_interrupted_row_resumes_and_converges(fos, kb_db, tmp_path):
    """An earlier run that wrote dst and died before the unlink / purge is finished by
    the next run -- never a 'dst-exists' skip that leaves the LEX copy forever."""
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    rows, twins = _plan(fos, kb_db, ledger)
    a = next(r for r in rows if r.filename == A)
    src = fos / LEX_CAP / A
    dst = fos / a.dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rf.rewrite_note(src.read_text(encoding="utf-8"), "F3E", "MOVE", a.src_rel, "2026-09-08T01:02:03+00:00"),
                   encoding="utf-8")
    conn = schema.connect(kb_db)
    kb = _FakeKB(conn)
    out = rf.apply_rows(conn, kb, fos, ledger, [a], [], accept_delta=False)
    assert out["moved"] == 1 and out["resumed"] == 1 and out["skipped"] == 0
    assert a.result == "moved (resumed)"
    assert dst.exists() and not src.exists()
    assert "2026-09-08T01:02:03+00:00" in dst.read_text(encoding="utf-8")   # the earlier stamp is kept
    assert conn.execute("SELECT count(*) FROM knowledge_chunks WHERE chunk_id IN ('s1','s2','d1','d2')").fetchone()[0] == 0
    assert len(kb.upserts) == 1
    # a third run: src gone, dst present -> already applied, nothing touched
    out3 = rf.apply_rows(conn, _FakeKB(conn), fos, ledger, [a], [], accept_delta=False)
    assert out3["moved"] == 0 and out3["resumed"] == 1 and "already-applied" in a.result
    # a DIFFERENT dst is never overwritten
    b = next(r for r in rows if r.filename == B)
    qdst = fos / b.dst_rel
    qdst.parent.mkdir(parents=True, exist_ok=True)
    qdst.write_text("someone else's note\n", encoding="utf-8")
    out4 = rf.apply_rows(conn, _FakeKB(conn), fos, ledger, [b], [], accept_delta=False)
    assert out4["skipped"] == 1 and "dst-exists-with-different-content" in b.result
    assert (fos / LEX_CAP / B).exists() and qdst.read_text(encoding="utf-8") == "someone else's note\n"
    conn.close()


def test_twin_is_skipped_when_the_static_entity_moved_since_the_dry_run(fos, kb_db, tmp_path):
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    rows, twins = _plan(fos, kb_db, ledger)
    conn = schema.connect(kb_db)
    conn.execute("UPDATE knowledge_chunks SET entity='FNDR' WHERE chunk_id='s9'")    # the static sync re-homed F
    conn.commit()
    out = rf.apply_rows(conn, _FakeKB(conn), fos, ledger, [], twins, accept_delta=False)
    assert out["twin_chunks_deleted"] == 0 and out["twins_skipped_stale"] == 1
    assert twins[0].result.startswith("skipped: static entity is now 'FNDR'")
    assert conn.execute("SELECT count(*) FROM knowledge_chunks WHERE chunk_id='d9'").fetchone()[0] == 1
    conn.close()


def test_main_apply_refusals(fos, kb_db, tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "_REPO_ROOT", tmp_path)
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    # no manifest
    assert rf.main(["--apply", "--db", str(kb_db), "--root", str(fos), "--ledger", str(ledger)]) == 1
    # a manifest whose root differs
    rows, twins = _plan(fos, kb_db, ledger)
    intent = tmp_path / "intent.json"
    conn = _ro(kb_db); before = rf.partition_counts(conn); conn.close()
    rf.write_intent(intent, Path("Z:/elsewhere"), rows, twins, before)
    assert rf.main(["--apply", "--manifest", str(intent), "--db", str(kb_db), "--root", str(fos), "--ledger", str(ledger)]) == 1
    # LARGE sweep + live bot -> refuse unless --allow-live (files OR chunks, twins included)
    rf.write_intent(intent, fos, rows, twins, before)
    monkeypatch.setattr(rf, "_LARGE_SWEEP_FILES", 1)
    monkeypatch.setattr(rf, "live_bot_is_fresh", lambda: True)
    assert rf.main(["--apply", "--manifest", str(intent), "--db", str(kb_db), "--root", str(fos), "--ledger", str(ledger)]) == 1
    monkeypatch.setattr(rf, "_LARGE_SWEEP_FILES", 100)
    monkeypatch.setattr(rf, "_LARGE_SWEEP_CHUNKS", 5)          # 2 + 3 (A) + 1 (twin) = 6 chunks > 5
    assert rf.main(["--apply", "--manifest", str(intent), "--db", str(kb_db), "--root", str(fos), "--ledger", str(ledger)]) == 1
    assert (fos / LEX_CAP / A).exists()
    monkeypatch.setattr(rf, "_LARGE_SWEEP_CHUNKS", 500)
    # a tampered manifest pointing a destination INTO the LEX partition trips the gate before any write
    data = json.loads(intent.read_text(encoding="utf-8"))
    for r in data["rows"]:
        if r["action"] == "MOVE":
            r["dst_rel"] = str(LEX_CAP / A)
    intent.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(rf, "live_bot_is_fresh", lambda: False)
    assert rf.main(["--apply", "--manifest", str(intent), "--db", str(kb_db), "--root", str(fos), "--ledger", str(ledger)]) == 2
    # ... and NOTHING was written or deleted (lens F #13: the claim is now asserted)
    assert (fos / LEX_CAP / A).exists() and (fos / LEX_CAP / B).exists()
    assert not list(scap.quarantine_root(fos).rglob("*.md")) if scap.quarantine_root(fos).exists() else True
    conn = _ro(kb_db)
    assert _ids(conn) == ALL_IDS
    conn.close()
    assert ledger.read_text(encoding="utf-8") == ""


def test_main_apply_happy_path_writes_applied_record(fos, kb_db, tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "_REPO_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("", encoding="utf-8")
    rows, twins = _plan(fos, kb_db, ledger)
    intent = tmp_path / "intent.json"
    conn = _ro(kb_db); before = rf.partition_counts(conn); conn.close()
    rf.write_intent(intent, fos, rows, twins, before)
    made: dict = {}

    class _KB:
        def __init__(self, db):
            self._conn = schema.connect(db)
            self.upserts = []
            made["kb"] = self
        def upsert_documents(self, docs):
            self.upserts.extend(list(docs)); return 1
        def close(self):
            self._conn.close()
    import cora.knowledge_base.store as store_mod
    monkeypatch.setattr(store_mod, "KnowledgeBase", _KB)
    monkeypatch.setattr(rf, "live_bot_is_fresh", lambda: False)
    rc = rf.main(["--apply", "--manifest", str(intent), "--db", str(kb_db), "--root", str(fos), "--ledger", str(ledger)])
    assert rc == 0
    applied = list((tmp_path / "logs").glob("refile-session-captures-APPLIED-*.json"))
    assert len(applied) == 1
    rec = json.loads(applied[0].read_text(encoding="utf-8"))
    assert rec["kind"] == "APPLIED" and rec["outcome"]["moved"] == 1 and rec["outcome"]["quarantined"] == 1
    assert rec["partition_counts_after"]["static_md"].get("LEX", 0) == 2
    assert len(made["kb"].upserts) == 1
