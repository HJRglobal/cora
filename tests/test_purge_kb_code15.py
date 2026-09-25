"""Code #15 combined KB purge (scripts/purge_kb_code15_2026-09.py): the S1 lane
``s1_tokens`` + the lane registry RIDER B plugs into.

Contract under test:
  * the dry-run is READ-ONLY (the DB file's bytes are unchanged, no sidecar appears --
    D-290: a --dry-run is a claim about every write site) and writes an INTENT that
    carries ids and counts only (self-scanned: secrets_scan finds nothing, no token,
    title or content substring);
  * the apply refuses without a manifest, for another DB, on drift / a vanished chunk
    (unless --accept-delta), on an unrecorded or unrepeated LEX release, on a lane with
    stops, and -- for a LARGE DELETE union -- unless Cora is PROVEN stopped (a missing
    heartbeat refuses); every refusal writes NOTHING;
  * the S1 REDACT touches only the planned chunk's content/title (vectors, count-only
    surfaces, the banking chunk, the FP rows and the HELD LEX row stay byte-identical);
  * a chunk in a DELETE lane is not also redacted; a RIDER B stub lane round-trips;
  * the APPLIED record is written after the act and carries no text.

Every token is ASSEMBLED at runtime (the pre-commit hook greps staged files).
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import secrets_scan as ss  # noqa: E402
from cora import secret_tokens as st  # noqa: E402

_SCRIPT = _REPO / "scripts" / "purge_kb_code15_2026-09.py"


def _load():
    name = "purge_kb_code15_2026_09"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pk = _load()


@pytest.fixture(autouse=True)
def _s1_registry_only(monkeypatch):
    """This file tests S1 + the registry mechanics. RIDER B's lanes (tested in
    tests/test_code15_rider_b_purge.py) need a CSV, a Founder-OS root and a Drive
    service; without them they STOP by design, which would block every all-lanes
    apply here -- so they are withdrawn from the registry for these tests."""
    for name in getattr(pk, "RIDER_B_LANES", ()):
        monkeypatch.delitem(pk.LANES, name, raising=False)


D16A = "1204" + "567890123456"
D16B = "1205" + "678901234567"
HEX32 = "0123456789abcdef" * 2
SLACK_TAIL = "AbCdEfGhIjKlMnOpQrStUvWx"
SLACK_BOT = "xo" + "xb-" + "1234567890123-1234567890123-" + SLACK_TAIL
SLACK_USER = "xo" + "xp-" + "1234567890-1234567890-1234567890123-" + HEX32
ANTHROPIC = "s" + "k-ant-api03-" + ("Ab1_" * 23) + "AA"
GOOGLE = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
TOKENS = (SLACK_BOT, SLACK_USER, ANTHROPIC, GOOGLE, SLACK_TAIL, HEX32, "Ab1_Ab1_")
TITLE_SECRET_WORDS = ("Deploy runbook", "Lex staff roster", "Wire Instructions", "Launch notes")
WIRE = ("Tradition Capital Bank\nWire Instructions for HJR Global\nABA/Routing Number: 123456789\n"
        "Beneficiary Account Number: 9876543210123\n")
ASANA_URL = "https://app.asana.com/0/1204525841563032" + "/" + "1218516997344812/f"


def _mkdb(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    dbp = tmp_path / "kb.db"
    conn = sqlite3.connect(dbp)
    conn.executescript("""
        CREATE TABLE knowledge_chunks (
            chunk_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
            entity TEXT NOT NULL, date_created INTEGER, date_modified INTEGER, author TEXT,
            title TEXT, content TEXT NOT NULL, deep_link TEXT, metadata TEXT,
            ingested_at INTEGER NOT NULL, sub_entity TEXT);
        CREATE TABLE knowledge_vec_f32 (chunk_id TEXT PRIMARY KEY, embedding BLOB);
        CREATE TABLE drive_extracted_facts (id INTEGER PRIMARY KEY, file_id TEXT, fact TEXT);
        CREATE TABLE sync_state (source TEXT PRIMARY KEY, last_sync_at INTEGER NOT NULL,
                                 last_source_modified INTEGER);
    """)
    rows = [
        # the S1 target: tokens in content AND title
        ("c-tok", "drive_sweep", "f-tok", "FNDR", "Deploy runbook " + GOOGLE,
         "Slack bot token: " + SLACK_BOT + "\nAnthropic key: " + ANTHROPIC + "\n", None, None),
        # a LEX-partition row carrying a token: HELD by default
        ("c-lex", "drive_sweep", "f-lex", "LEX", "Lex staff roster", "old bot token " + SLACK_BOT, None, None),
        # FP rows: the GLOB prefilter selects them, the regex clears them
        ("c-fp", "gmail", "m-fp", "FNDR", "Launch notes",
         "see " + ASANA_URL + " and task-" + "abcdefghij0123456789ABCD and sk-" + "learn-pipeline-tests", None, None),
        # a banking chunk: S1 never banking-redacts AT REST
        ("c-bank", "gmail", "m-bank", "FNDR", "Wire Instructions", WIRE, None, None),
        # count-only surfaces: metadata + deep_link carry tokens, content/title are clean
        ("c-meta", "static_md", "notes/x.md", "FNDR", "notes", "clean body",
         json.dumps({"k": ANTHROPIC}), "https://maps.example/x?key=" + GOOGLE),
        ("c-plain", "static_md", "notes/y.md", "F3E", "plain", "nothing here", None, None),
    ]
    for cid, src, sid, ent, title, content, meta, link in rows:
        conn.execute("INSERT INTO knowledge_chunks (chunk_id, source, source_id, entity, title, content, "
                     "metadata, deep_link, ingested_at) VALUES (?,?,?,?,?,?,?,?,1)",
                     (cid, src, sid, ent, title, content, meta, link))
        conn.execute("INSERT INTO knowledge_vec_f32 VALUES (?, ?)", (cid, b"\x00\x01"))
    conn.execute("INSERT INTO drive_extracted_facts (file_id, fact) VALUES ('f1', ?)", ("uses " + SLACK_USER,))
    conn.execute("INSERT INTO sync_state VALUES ('gmail', 1, 1)")
    conn.commit()
    conn.close()
    return dbp


def _bytes(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _row(db: Path, cid: str):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT content, title, metadata, deep_link FROM knowledge_chunks WHERE chunk_id=?",
                            (cid,)).fetchone()
    finally:
        conn.close()


def _snapshot(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT chunk_id, content, title, metadata, deep_link FROM knowledge_chunks")}
    finally:
        conn.close()


def _vec_ids(db: Path) -> set:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT chunk_id FROM knowledge_vec_f32")}
    finally:
        conn.close()


def _no_text(blob: str) -> None:
    assert not any(t in blob for t in TOKENS), "a token reached a record"
    assert not any(w in blob for w in TITLE_SECRET_WORDS), "a title reached a record"
    assert "Slack bot token" not in blob and "Tradition Capital" not in blob and "clean body" not in blob
    assert ss.scan_text(blob, "record", env_keys=set(), live_values=ss.live_secret_values()) == []
    assert not st.has_secret_token(blob)


def _dry(db, out, *extra) -> Path:
    before = set(out.glob("*")) if out.exists() else set()
    assert pk.main(["--db", str(db), "--out-dir", str(out), *extra]) == 0
    new = [p for p in out.glob("kb-purge-code15-INTENT-*.json") if p not in before]
    assert len(new) == 1
    return new[0]


def _hb(path: Path, age_s: float) -> Path:
    when = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    path.write_text(when.isoformat(), encoding="utf-8")
    ts = time.time() - age_s
    os.utime(path, (ts, ts))
    return path


# ── dry-run ──────────────────────────────────────────────────────────────────
class TestDryRun:
    def test_read_only_and_writes_an_id_only_intent(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        files_before = sorted(p.name for p in db.parent.iterdir())
        digest = _bytes(db)
        intent = _dry(db, tmp_path / "out")
        assert _bytes(db) == digest                                     # D-290: nothing written
        assert sorted(p.name for p in db.parent.iterdir()) == files_before   # no sidecar either
        data = json.loads(intent.read_text(encoding="utf-8"))
        lane = data["lanes"]["s1_tokens"]
        assert lane["action"] == "REDACT"
        assert lane["chunk_ids"] == ["c-tok"]
        assert [h["chunk_id"] for h in lane["holds"]] == ["c-lex"]      # LEX held by default
        assert set(lane["pre_sha256"]) == {"c-tok"}
        c = lane["counts"]
        assert c["occurrences"] == {
            "slack-token": {"drive_sweep.content": 2},
            "sk-key": {"drive_sweep.content": 1, "static_md.metadata": 1},
            "google-api-key": {"drive_sweep.title": 1, "static_md.deep_link": 1},
        }
        assert c["count_only_chunks"] == {"metadata": 1, "source_id": 0, "deep_link": 1, "author": 0}
        assert c["count_only_only_chunks"] == 1
        assert c["by_partition"] == {"LEX": {"redact": 0, "held": 1}, "non-LEX": {"redact": 1, "held": 0}}
        assert c["other_tables"]["drive_extracted_facts"] == {"rows": 1, "hit_rows": 1,
                                                              "occurrences": {"slack-token": 1}}
        assert c["other_tables"]["sync_state"]["hit_rows"] == 0
        assert "knowledge_vec_f32" not in c["other_tables"]
        assert data["release_lex"] == [] and data["union"]["is_large"] is False
        for blob in (intent.read_text(encoding="utf-8"), intent.with_suffix(".txt").read_text(encoding="utf-8")):
            _no_text(blob)

    def test_default_out_dir_is_the_conftest_redirect(self, tmp_path):
        out = pk.default_out_dir()
        assert (_REPO / "logs").resolve() not in out.resolve().parents and out.resolve() != (_REPO / "logs").resolve()
        db = _mkdb(tmp_path / "kb")
        assert pk.main(["--db", str(db)]) == 0
        assert list(out.glob("kb-purge-code15-INTENT-*.json"))

    def test_a_tripped_self_scan_writes_nothing(self, tmp_path, monkeypatch):
        db = _mkdb(tmp_path / "kb")
        monkeypatch.setattr(pk, "record_leaks", lambda text: ["shape:test"])
        out = tmp_path / "out"
        assert pk.main(["--db", str(db), "--out-dir", str(out)]) == 1
        assert not out.exists() or not list(out.iterdir())

    def test_missing_db_refuses(self, tmp_path):
        assert pk.main(["--db", str(tmp_path / "absent.db"), "--out-dir", str(tmp_path / "o")]) == 1
        assert not (tmp_path / "absent.db").exists()

    def test_release_lex_plans_the_lex_row_and_is_recorded(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        data = json.loads(_dry(db, tmp_path / "out", "--release-lex", "s1_tokens").read_text(encoding="utf-8"))
        lane = data["lanes"]["s1_tokens"]
        assert sorted(lane["chunk_ids"]) == ["c-lex", "c-tok"] and lane["holds"] == []
        assert data["release_lex"] == ["s1_tokens"] and lane["counts"]["lex_released"] is True

    def test_unknown_lane_refuses(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        assert pk.main(["--db", str(db), "--out-dir", str(tmp_path / "o"), "--release-lex", "nope"]) == 1


# ── apply ────────────────────────────────────────────────────────────────────
class TestApply:
    def test_redacts_only_the_planned_chunk(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        before = _snapshot(db)
        vec_before = _vec_ids(db)
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]) == 0
        after = _snapshot(db)
        content, title, meta, link = after["c-tok"]
        assert not any(t in content + title for t in TOKENS)
        assert content.count(st.MARKER) == 2 and title == "Deploy runbook " + st.MARKER
        assert content == "Slack bot token: " + st.MARKER + "\nAnthropic key: " + st.MARKER + "\n"
        for cid in ("c-lex", "c-fp", "c-bank", "c-meta", "c-plain"):
            assert after[cid] == before[cid], cid        # held LEX, FP, banking, count-only: untouched
        assert _vec_ids(db) == vec_before                # no re-embed, no vector touched
        applied = sorted(out.glob("kb-purge-code15-APPLIED-*.json"))
        assert len(applied) == 1
        rec = json.loads(applied[0].read_text(encoding="utf-8"))
        o = rec["outcome"]["s1_tokens"]
        assert (o["rows_updated"], o["redactions"], o["occurrences_before"], o["occurrences_after"]) == (1, 3, 3, 0)
        assert o["holds"] == 1 and o["errors"] == []
        assert rec["heartbeat"].keys() == {"exists", "content_age_s", "mtime_age_s", "stopped"}
        assert rec["residuals"] and any("re-ingest" in r for r in rec["residuals"])
        _no_text(applied[0].read_text(encoding="utf-8"))

    def test_apply_without_manifest_refuses_and_writes_nothing(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        digest = _bytes(db)
        assert pk.main(["--apply", "--db", str(db), "--out-dir", str(tmp_path / "o")]) == 1
        assert _bytes(db) == digest

    def test_manifest_for_another_db_refuses(self, tmp_path):
        a, b = _mkdb(tmp_path / "a"), _mkdb(tmp_path / "b")
        intent = _dry(a, tmp_path / "out")
        digest = _bytes(b)
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(b), "--out-dir", str(tmp_path / "out")]) == 1
        assert _bytes(b) == digest

    def test_drift_refuses_unless_accept_delta(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE knowledge_chunks SET content = content || ' edited' WHERE chunk_id='c-tok'")
        conn.commit()
        conn.close()
        digest = _bytes(db)
        args = ["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]
        assert pk.main(args) == 1
        assert _bytes(db) == digest                      # refused BEFORE any write (no WAL flip either)
        assert not list(out.glob("kb-purge-code15-APPLIED-*"))
        assert pk.main(args + ["--accept-delta"]) == 0
        content = _row(db, "c-tok")[0]
        assert SLACK_BOT not in content and content.endswith(" edited")
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        assert rec["drift"]["changed"] == ["c-tok"] and rec["accept_delta"] is True

    def test_a_vanished_chunk_refuses_unless_accept_delta(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM knowledge_chunks WHERE chunk_id='c-tok'")
        conn.commit()
        conn.close()
        args = ["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]
        assert pk.main(args) == 1
        assert pk.main(args + ["--accept-delta"]) == 0
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        assert rec["drift"]["vanished"] == ["c-tok"] and rec["outcome"]["s1_tokens"]["skipped_vanished"] == 1

    def test_lex_release_must_be_recorded_and_repeated(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        plain_intent = _dry(db, out)
        digest = _bytes(db)
        # a release the INTENT does not record is refused
        assert pk.main(["--apply", "--manifest", str(plain_intent), "--db", str(db), "--out-dir", str(out),
                        "--release-lex", "s1_tokens"]) == 1
        released = _dry(db, out, "--release-lex", "s1_tokens")
        # a recorded release the apply does not REPEAT is refused
        assert pk.main(["--apply", "--manifest", str(released), "--db", str(db), "--out-dir", str(out)]) == 1
        assert _bytes(db) == digest
        assert pk.main(["--apply", "--manifest", str(released), "--db", str(db), "--out-dir", str(out),
                        "--release-lex", "s1_tokens"]) == 0
        assert SLACK_BOT not in _row(db, "c-lex")[0] and st.MARKER in _row(db, "c-lex")[0]

    def test_a_tampered_action_refuses(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        data = json.loads(intent.read_text(encoding="utf-8"))
        data["lanes"]["s1_tokens"]["action"] = "DELETE"
        intent.write_text(json.dumps(data), encoding="utf-8")
        digest = _bytes(db)
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]) == 1
        assert _bytes(db) == digest

    def test_a_redactor_error_skips_the_chunk_never_persists_withheld(self, tmp_path, monkeypatch):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        before = _snapshot(db)

        def _boom(_text):
            raise RuntimeError("engine")
        lane = pk.LANES["s1_tokens"]
        monkeypatch.setitem(pk.LANES, "s1_tokens", pk.Lane(name=lane.name, action=lane.action,
                                                            select=lane.select, redact=_boom,
                                                            residual=lane.residual))
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]) == 0
        assert _snapshot(db)["c-tok"] == before["c-tok"]
        assert st.WITHHELD not in (_row(db, "c-tok")[0] or "")
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        assert rec["outcome"]["s1_tokens"]["errors"] == [{"chunk_id": "c-tok", "error": "RuntimeError"}]

    # D-051 r1 lens-integration#2: the at-rest belt must not rest on the lane binding alone.
    def test_a_redactor_returning_withheld_is_an_error_never_persisted(self, tmp_path, monkeypatch):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        before = _snapshot(db)
        lane = pk.LANES["s1_tokens"]
        # the egress redactor's fail-closed shape: (WITHHELD, 1) instead of a raise
        monkeypatch.setitem(pk.LANES, "s1_tokens", pk.Lane(name=lane.name, action=lane.action,
                                                            select=lane.select,
                                                            redact=lambda _text: (st.WITHHELD, 1),
                                                            residual=lane.residual))
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]) == 0
        assert st.WITHHELD not in (_row(db, "c-tok")[0] or "") and st.WITHHELD not in (_row(db, "c-tok")[1] or "")
        assert _snapshot(db)["c-tok"] == before["c-tok"]
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        o = rec["outcome"]["s1_tokens"]
        assert o["errors"] == [{"chunk_id": "c-tok", "error": "withheld-returned"}] and o["rows_updated"] == 0

    def test_the_s1_lane_is_bound_to_the_strict_redactor(self):
        # the egress redactor returns WITHHELD on an engine error; only the strict one raises
        assert pk.LANES["s1_tokens"].redact is st.redact_secret_tokens_strict

    # D-051 r1 purge#0: the LEX hold is re-decided at --apply from the CURRENT entity.
    def test_a_planned_row_retagged_to_lex_after_the_dry_run_refuses(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE knowledge_chunks SET entity='LEX' WHERE chunk_id='c-tok'")   # content/title unchanged
        conn.commit()
        conn.close()
        digest = _bytes(db)
        before = _snapshot(db)
        args = ["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]
        assert pk.main(args) == 1
        assert pk.main(args + ["--accept-delta"]) == 1               # a re-tag is not drift to accept
        assert _bytes(db) == digest and _snapshot(db) == before     # refused on the read-only handle
        assert SLACK_BOT in _row(db, "c-tok")[0]
        assert not list(out.glob("kb-purge-code15-APPLIED-*"))

    def test_an_edited_intent_cannot_plan_a_held_lex_row(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        data = json.loads(intent.read_text(encoding="utf-8"))
        content, title = _row(db, "c-lex")[:2]
        data["lanes"]["s1_tokens"]["chunk_ids"].append("c-lex")
        data["lanes"]["s1_tokens"]["pre_sha256"]["c-lex"] = pk.chunk_digest(content, title)   # a correct digest
        intent.write_text(json.dumps(data), encoding="utf-8")
        before = _snapshot(db)
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]) == 1
        assert _snapshot(db) == before and SLACK_BOT in _row(db, "c-lex")[0]

    def test_a_retag_between_the_pre_check_and_the_write_lock_refuses(self, tmp_path, monkeypatch):
        # the re-verification runs AFTER the read-only partition pre-check; a re-tag that
        # lands meanwhile is caught by the in-lock re-read
        def _retag(conn, ctx, plan):
            c = sqlite3.connect(db)
            c.execute("UPDATE knowledge_chunks SET entity='LEX-LLC' WHERE chunk_id='c-plain'")
            c.commit()
            c.close()
            return []
        monkeypatch.setitem(pk.LANES, "rb_stub", dataclasses.replace(_stub_delete_lane(["c-plain"]), verify=_retag))
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out),
                        "--lanes", "rb_stub"]) == 1
        assert "c-plain" in _snapshot(db) and "c-plain" in _vec_ids(db)
        assert not list(out.glob("kb-purge-code15-APPLIED-*"))

    def test_a_released_lane_records_its_lex_count(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        released = _dry(db, out, "--release-lex", "s1_tokens")
        assert pk.main(["--apply", "--manifest", str(released), "--db", str(db), "--out-dir", str(out),
                        "--release-lex", "s1_tokens"]) == 0
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        assert rec["lex_partition_gate"] == {"released_lanes": ["s1_tokens"],
                                             "lex_chunks_in_released_lanes": {"s1_tokens": 1}}


# ── the lane registry (RIDER B plugs in here) ────────────────────────────────
def _stub_delete_lane(ids, *, files=0, stops=None, holds=None):
    def _select(conn, ctx):
        plan = pk.LanePlan(lane="rb_stub", action="DELETE", files=files,
                           holds=list(holds or []), stops=list(stops or []))
        for cid, content, title in conn.execute(
                f"SELECT chunk_id, content, title FROM knowledge_chunks WHERE chunk_id IN ({','.join('?' * len(ids))})",
                list(ids)):
            plan.chunk_ids.append(cid)
            plan.pre_sha256[cid] = pk.chunk_digest(content, title)
        plan.counts = {"selected": len(plan.chunk_ids)}
        return plan
    return pk.Lane(name="rb_stub", action="DELETE", select=_select, description="test stub")


class TestLaneRegistry:
    def test_register_validates(self):
        with pytest.raises(ValueError):
            pk.register_lane(pk.Lane(name="bad", action="MOVE", select=lambda c, x: None))
        with pytest.raises(ValueError):
            pk.register_lane(pk.Lane(name="bad2", action="REDACT", select=lambda c, x: None))
        with pytest.raises(ValueError):
            pk.register_lane(pk.LANES["s1_tokens"])            # already registered

    def test_stub_delete_lane_round_trips_and_wins_the_overlap(self, tmp_path, monkeypatch):
        monkeypatch.setitem(pk.LANES, "rb_stub", _stub_delete_lane(["c-tok", "c-plain"]))
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        data = json.loads(intent.read_text(encoding="utf-8"))
        assert sorted(data["lanes"]["rb_stub"]["chunk_ids"]) == ["c-plain", "c-tok"]
        assert data["union"]["redact_also_deleted"] == 1 and data["union"]["delete_chunks"] == 2
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]) == 0
        snap = _snapshot(db)
        assert "c-tok" not in snap and "c-plain" not in snap
        assert {"c-tok", "c-plain"}.isdisjoint(_vec_ids(db))     # the cascade reached the vectors
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        s1 = rec["outcome"]["s1_tokens"]
        assert s1["skipped_also_deleted"] == 1 and s1["targets"] == 0 and s1["rows_updated"] == 0
        assert rec["outcome"]["_delete_union"]["deleted_ids"] == 2
        assert rec["outcome"]["_delete_union"]["remaining_after"] == 0
        _no_text(json.dumps(rec))

    def test_lanes_subset_applies_only_the_named_lane(self, tmp_path, monkeypatch):
        monkeypatch.setitem(pk.LANES, "rb_stub", _stub_delete_lane(["c-plain"]))
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out),
                        "--lanes", "s1_tokens"]) == 0
        snap = _snapshot(db)
        assert "c-plain" in snap and SLACK_BOT not in snap["c-tok"][0]

    def test_a_lane_with_stops_refuses_unless_deselected(self, tmp_path, monkeypatch):
        monkeypatch.setitem(pk.LANES, "rb_stub", _stub_delete_lane(["c-plain"], stops=[{"reason": "gate"}]))
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        digest = _bytes(db)
        args = ["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out)]
        assert pk.main(args) == 1 and _bytes(db) == digest
        assert pk.main(args + ["--lanes", "s1_tokens"]) == 0

    def test_a_selector_error_stops_its_lane(self, tmp_path, monkeypatch):
        def _raise(conn, ctx):
            raise RuntimeError("boom")
        monkeypatch.setitem(pk.LANES, "rb_stub", pk.Lane(name="rb_stub", action="DELETE", select=_raise))
        db = _mkdb(tmp_path / "kb")
        data = json.loads(_dry(db, tmp_path / "out").read_text(encoding="utf-8"))
        assert data["lanes"]["rb_stub"]["stops"] == [{"reason": "selector error: RuntimeError"}]


class TestLargeGate:
    @pytest.fixture
    def large(self, tmp_path, monkeypatch):
        monkeypatch.setitem(pk.LANES, "rb_stub", _stub_delete_lane(["c-plain"], files=101))
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        assert json.loads(intent.read_text(encoding="utf-8"))["union"]["is_large"] is True
        return db, out, intent, tmp_path / "heartbeat.txt"

    def _args(self, db, out, intent, hb):
        return ["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out), "--heartbeat", str(hb)]

    def test_fresh_heartbeat_refuses(self, large):
        db, out, intent, hb = large
        _hb(hb, 30)
        digest = _bytes(db)
        assert pk.main(self._args(db, out, intent, hb)) == 1 and _bytes(db) == digest

    def test_missing_heartbeat_refuses_fail_closed(self, large):
        db, out, intent, hb = large
        digest = _bytes(db)
        assert not hb.exists()
        assert pk.main(self._args(db, out, intent, hb)) == 1 and _bytes(db) == digest

    def test_unparseable_heartbeat_refuses(self, large):
        db, out, intent, hb = large
        hb.write_text("not a timestamp", encoding="utf-8")
        old = time.time() - 3600
        os.utime(hb, (old, old))
        assert pk.main(self._args(db, out, intent, hb)) == 1

    def test_stale_by_one_clock_only_refuses(self, large):
        db, out, intent, hb = large
        _hb(hb, 900)
        now = time.time()
        os.utime(hb, (now, now))                     # content old, mtime fresh
        assert pk.main(self._args(db, out, intent, hb)) == 1

    def test_stale_heartbeat_passes(self, large):
        db, out, intent, hb = large
        _hb(hb, 900)
        assert pk.main(self._args(db, out, intent, hb)) == 0
        assert "c-plain" not in _snapshot(db)
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        assert rec["heartbeat"]["stopped"] is True and rec["union"]["is_large"] is True

    # D-051 r1 purge#1: the LARGE gate is re-read UNDER the write lock.
    def test_cora_back_during_reverification_refuses_at_the_write_lock(self, tmp_path, monkeypatch):
        hb = _hb(tmp_path / "heartbeat.txt", 900)                  # stale by both clocks at the first read

        def _cora_comes_back(conn, ctx, plan):                     # stands in for the minutes-long Drive re-walk
            _hb(hb, 0)
            return []
        monkeypatch.setitem(pk.LANES, "rb_stub", dataclasses.replace(
            _stub_delete_lane(["c-plain"], files=101), verify=_cora_comes_back))
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        assert json.loads(intent.read_text(encoding="utf-8"))["union"]["is_large"] is True
        assert pk.main(self._args(db, out, intent, hb)) == 1
        assert "c-plain" in _snapshot(db) and "c-plain" in _vec_ids(db)
        assert not list(out.glob("kb-purge-code15-APPLIED-*"))
        _hb(hb, 900)
        monkeypatch.setitem(pk.LANES, "rb_stub", _stub_delete_lane(["c-plain"], files=101))
        assert pk.main(self._args(db, out, intent, hb)) == 0       # stopped throughout: applies
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        assert rec["heartbeat"]["stopped"] is True and rec["heartbeat_at_write_lock"]["stopped"] is True
        assert rec["heartbeat_at_write_lock"].keys() == {"exists", "content_age_s", "mtime_age_s", "stopped"}

    def test_allow_live_overrides(self, large):
        db, out, intent, hb = large
        _hb(hb, 30)
        assert pk.main(self._args(db, out, intent, hb) + ["--allow-live"]) == 0
        rec = json.loads(sorted(out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        assert rec["allow_live"] is True and rec["heartbeat"]["stopped"] is False

    def test_s1_alone_is_never_large(self, tmp_path):
        db = _mkdb(tmp_path / "kb")
        out = tmp_path / "out"
        intent = _dry(db, out)
        # no heartbeat anywhere near: a REDACT-only apply needs no stop window
        assert pk.main(["--apply", "--manifest", str(intent), "--db", str(db), "--out-dir", str(out),
                        "--heartbeat", str(tmp_path / "absent-heartbeat.txt")]) == 0


def test_conftest_disables_the_real_drive_factory():
    # Code #15 RIDER B conftest block: once this script is loaded, no test can build
    # the real Drive service (nor run its load_dotenv(<repo>/.env, override=True)).
    with pytest.raises(RuntimeError, match="disabled in tests"):
        pk.DRIVE_SERVICE_FACTORY()
