"""S2b — gmail alias-mailbox dedup + quote-strip + dup purge (2026-07-31 audit).

The alias-duplication class: one physical mailbox enrolled under 2-3 roster
addresses was swept 2-3x nightly; source_id embeds user_email so
replace-on-conflict never folded the copies (~48% of the gmail partition).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


def _load_sweep():
    try:
        import gmail_threaded_sweep as m
        return m
    except ImportError:
        pytest.skip("gmail_threaded_sweep not importable")


def _load_purge():
    try:
        import purge_gmail_alias_dup_chunks as m
        return m
    except ImportError:
        pytest.skip("purge_gmail_alias_dup_chunks not importable")


# ── quote-strip + duplicate-delivery fingerprint ─────────────────────────────

class TestStripQuotedLines:
    def test_drops_quoted_lines_keeps_new_content(self):
        m = _load_sweep()
        body = ("Thanks, sounds good.\n"
                "> On Mon Jul 28 Harrison wrote:\n"
                "> the original message text\n"
                ">> an even older quote\n"
                "See you then.")
        out = m._strip_quoted_lines(body)
        assert "sounds good" in out and "See you then" in out
        assert "original message text" not in out
        assert "older quote" not in out

    def test_entirely_quoted_body_collapses_to_empty(self):
        m = _load_sweep()
        assert m._strip_quoted_lines("> a\n> b\n  > c") == ""

    def test_plain_body_unchanged(self):
        m = _load_sweep()
        assert m._strip_quoted_lines("hello\nworld") == "hello\nworld"

    def test_none_and_empty_safe(self):
        m = _load_sweep()
        assert m._strip_quoted_lines("") == ""
        assert m._strip_quoted_lines(None) == ""


class TestBodyFingerprint:
    def test_whitespace_and_case_normalized(self):
        m = _load_sweep()
        a = m._body_fingerprint("Hello   World\n\n")
        b = m._body_fingerprint("hello world")
        assert a == b != ""

    def test_different_content_differs(self):
        m = _load_sweep()
        assert m._body_fingerprint("abc") != m._body_fingerprint("xyz")

    def test_empty_returns_empty(self):
        m = _load_sweep()
        assert m._body_fingerprint("   ") == ""


class TestDeliveryFingerprint:
    """D-051 review: body-only fingerprints dropped a DIFFERENT sender's
    identical short reply ('Approved.') — sender + attachments are in the key."""

    def test_same_sender_same_body_matches(self):
        m = _load_sweep()
        a = m._delivery_fingerprint("A <a@x.com>", [], "Approved.")
        b = m._delivery_fingerprint("a <a@x.com>", [], "approved.")
        assert a == b != ""

    def test_different_sender_same_body_differs(self):
        m = _load_sweep()
        a = m._delivery_fingerprint("A <a@x.com>", [], "Approved.")
        b = m._delivery_fingerprint("B <b@x.com>", [], "Approved.")
        assert a != b

    def test_different_attachments_differ(self):
        m = _load_sweep()
        a = m._delivery_fingerprint("A", ["inv1.pdf"], "see attached")
        b = m._delivery_fingerprint("A", ["inv2.pdf"], "see attached")
        assert a != b

    def test_attachment_order_irrelevant(self):
        m = _load_sweep()
        a = m._delivery_fingerprint("A", ["x.pdf", "y.pdf"], "b")
        b = m._delivery_fingerprint("A", ["y.pdf", "x.pdf"], "b")
        assert a == b


class TestEffectiveBody:
    """D-051 review: a forward's '>'-quoted block can be the ONLY copy of an
    external exchange — forwards keep their body; ordinary replies strip."""

    def test_forwarded_message_keeps_quoted_block(self):
        m = _load_sweep()
        body = ("FYI see below\n\n---------- Forwarded message ---------\n"
                "> From: External Party\n> the original external exchange")
        out = m._effective_body(body)
        assert "original external exchange" in out

    def test_original_message_marker_keeps_quoted_block(self):
        m = _load_sweep()
        body = "see below\n-----Original Message-----\n> external content here"
        assert "external content here" in m._effective_body(body)

    def test_ordinary_reply_still_strips(self):
        m = _load_sweep()
        body = "Sounds good.\n> earlier thread text"
        out = m._effective_body(body)
        assert out == "Sounds good."

    def test_empty_safe(self):
        m = _load_sweep()
        assert m._effective_body("") == ""
        assert m._effective_body(None) == ""


class TestFilterAccounts:
    """D-051 review: --accounts must apply AFTER the alias collapse and match
    a kept entry via its aliases, else a targeted alias run re-creates the
    purged duplicate class under the alias identity."""

    def test_alias_address_matches_kept_primary(self):
        m = _load_sweep()
        accounts = [_acct("harrison@hjrglobal.com"), _acct("hannah@hjrglobal.com")]
        alias_map = {"harrison@hjrglobal.com": ["harrison@f3energy.com"]}
        kept = m._filter_accounts(accounts, {"harrison@f3energy.com"}, alias_map)
        assert [a["email"] for a in kept] == ["harrison@hjrglobal.com"]

    def test_primary_address_matches_directly(self):
        m = _load_sweep()
        accounts = [_acct("a@x.com"), _acct("b@x.com")]
        kept = m._filter_accounts(accounts, {"b@x.com"}, {})
        assert [a["email"] for a in kept] == ["b@x.com"]

    def test_empty_filter_returns_all(self):
        m = _load_sweep()
        accounts = [_acct("a@x.com")]
        assert m._filter_accounts(accounts, set(), {}) == accounts


# ── alias-account dedup ───────────────────────────────────────────────────────

def _acct(email, **kw):
    return {"email": email, "enabled": True, **kw}


class TestDedupAliasAccounts:
    def _resolver(self, mapping):
        return lambda e: mapping.get(e, e)

    def test_aliases_collapse_to_canonical_entry(self):
        m = _load_sweep()
        mapping = {
            "harrison@f3energy.com": "harrison@hjrglobal.com",
            "harrison@lexingtonservices.com": "harrison@hjrglobal.com",
        }
        accounts = [
            _acct("harrison@f3energy.com"),
            _acct("harrison@hjrglobal.com"),
            _acct("harrison@lexingtonservices.com"),
            _acct("hannah@hjrglobal.com"),
        ]
        kept, alias_map = m._dedup_alias_accounts(accounts, resolve=self._resolver(mapping))
        kept_emails = [a["email"] for a in kept]
        # The canonical-address entry wins even though an alias came first.
        assert kept_emails == ["harrison@hjrglobal.com", "hannah@hjrglobal.com"]
        assert sorted(alias_map["harrison@hjrglobal.com"]) == [
            "harrison@f3energy.com", "harrison@lexingtonservices.com"]

    def test_no_canonical_entry_keeps_first_in_roster_order(self):
        m = _load_sweep()
        mapping = {"a@x.com": "shared@x.com", "b@x.com": "shared@x.com"}
        kept, alias_map = m._dedup_alias_accounts(
            [_acct("a@x.com"), _acct("b@x.com")], resolve=self._resolver(mapping))
        assert [a["email"] for a in kept] == ["a@x.com"]
        assert alias_map["a@x.com"] == ["b@x.com"]

    def test_resolver_error_fails_soft_entry_kept(self):
        m = _load_sweep()

        def boom(_email):
            raise RuntimeError("network")

        def resolve(email):
            # _canonical_mailbox itself fails soft; simulate its contract here
            try:
                return boom(email)
            except RuntimeError:
                return email

        kept, alias_map = m._dedup_alias_accounts(
            [_acct("a@x.com"), _acct("b@x.com")], resolve=resolve)
        assert [a["email"] for a in kept] == ["a@x.com", "b@x.com"]
        assert alias_map == {}

    def test_distinct_mailboxes_untouched(self):
        m = _load_sweep()
        accounts = [_acct("a@x.com"), _acct("b@x.com")]
        kept, alias_map = m._dedup_alias_accounts(accounts, resolve=lambda e: e)
        assert kept == accounts and alias_map == {}


class TestGroupWatermark:
    def test_max_over_alias_keys(self):
        m = _load_sweep()
        wm = {"harrison@f3energy.com": 2000, "harrison@hjrglobal.com": 1500}
        got = m._group_watermark(wm, "harrison@hjrglobal.com",
                                 ["harrison@f3energy.com"], fallback_ts=100)
        assert got == 2000

    def test_fallback_when_no_keys(self):
        m = _load_sweep()
        assert m._group_watermark({}, "a@x.com", [], fallback_ts=42) == 42


# ── purge script ──────────────────────────────────────────────────────────────

def _mk_kb(tmp_path):
    db = tmp_path / "kb.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE knowledge_chunks (
        chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, entity TEXT,
        sub_entity TEXT, content TEXT, title TEXT, metadata TEXT,
        ingested_at INTEGER)""")
    for t in ("knowledge_vec_bin", "knowledge_vec_bin_v2", "knowledge_vec_f32"):
        conn.execute(f"CREATE TABLE {t} (chunk_id TEXT PRIMARY KEY, v BLOB)")
    return db, conn


def _ins(conn, chunk_id, content, mid, user_email, entity="F3E"):
    conn.execute(
        "INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?,?,?,?)",
        (chunk_id, "gmail", f"gmail:{user_email}:{mid}", entity, None, content,
         "t", json.dumps({"message_id": mid, "user_email": user_email}),
         int(time.time())))
    for t in ("knowledge_vec_bin", "knowledge_vec_bin_v2", "knowledge_vec_f32"):
        conn.execute(f"INSERT INTO {t} VALUES (?, ?)", (chunk_id, b"v"))


class TestPurgePlan:
    def test_triplicate_keeps_primary_domain_row(self, tmp_path):
        p = _load_purge()
        db, conn = _mk_kb(tmp_path)
        _ins(conn, "c1", "wholesale body", "mid1", "harrison@f3energy.com")
        _ins(conn, "c2", "wholesale body", "mid1", "harrison@hjrglobal.com")
        _ins(conn, "c3", "wholesale body", "mid1", "harrison@lexingtonservices.com")
        conn.commit()
        delete_ids, stats = p.plan_purge(conn)
        assert stats["dup_groups"] == 1
        assert sorted(delete_ids) == ["c1", "c3"]  # @hjrglobal row kept

    def test_same_entity_scope_distinct_entities_both_kept(self, tmp_path):
        p = _load_purge()
        db, conn = _mk_kb(tmp_path)
        # Same message swept under two aliases landed in DIFFERENT entities —
        # each entity partition must keep its copy (retrieval coverage).
        _ins(conn, "c1", "body", "mid1", "harrison@f3energy.com", entity="F3E")
        _ins(conn, "c2", "body", "mid1", "harrison@hjrglobal.com", entity="FNDR")
        conn.commit()
        delete_ids, stats = p.plan_purge(conn)
        assert delete_ids == [] and stats["dup_groups"] == 0

    def test_same_mailbox_dup_out_of_scope(self, tmp_path):
        p = _load_purge()
        db, conn = _mk_kb(tmp_path)
        # Duplicate rows under ONE user_email are not the alias class.
        _ins(conn, "c1", "body", "mid1", "a@x.com")
        conn.execute("UPDATE knowledge_chunks SET chunk_id='c1' WHERE chunk_id='c1'")
        _ins(conn, "c2", "body", "mid1", "a@x.com")
        conn.commit()
        delete_ids, _stats = p.plan_purge(conn)
        assert delete_ids == []

    def test_different_content_same_mid_not_grouped(self, tmp_path):
        p = _load_purge()
        db, conn = _mk_kb(tmp_path)
        # Chunk-suffixed rows: same message id, different chunk content.
        _ins(conn, "c1", "chunk zero text", "mid1", "a@x.com")
        _ins(conn, "c2", "chunk one text", "mid1", "b@x.com")
        conn.commit()
        delete_ids, _stats = p.plan_purge(conn)
        assert delete_ids == []


class TestPurgeApply:
    def test_cascade_hits_all_vec_tables_including_bin_v2(self, tmp_path):
        p = _load_purge()
        db, conn = _mk_kb(tmp_path)
        _ins(conn, "c1", "body", "mid1", "harrison@f3energy.com")
        _ins(conn, "c2", "body", "mid1", "harrison@hjrglobal.com")
        conn.commit()
        delete_ids, _ = p.plan_purge(conn)
        assert delete_ids == ["c1"]
        deleted = p.apply_purge(conn, delete_ids)
        for t in ("knowledge_vec_bin", "knowledge_vec_bin_v2",
                  "knowledge_vec_f32", "knowledge_chunks"):
            assert deleted[t] == 1
            remaining = conn.execute(
                f"SELECT chunk_id FROM {t}").fetchall()
            assert remaining == [("c2",)]


class TestPurgeApplyRealVec0:
    """D-051 review: the first apply test used PLAIN tables named like vec
    tables and passed vacuously while the live --apply crashed ('no such
    module: vec0'). This one uses REAL vec0 virtual tables over the same
    extension-loaded connection main() now builds via schema.connect."""

    def test_apply_deletes_from_real_vec0_tables(self, tmp_path):
        pytest.importorskip("sqlite_vec")
        p = _load_purge()
        from cora.knowledge_base import schema
        db = tmp_path / "kb_vec0.db"
        conn = schema.connect(db)
        conn.execute("""CREATE TABLE knowledge_chunks (
            chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, entity TEXT,
            sub_entity TEXT, content TEXT, title TEXT, metadata TEXT,
            ingested_at INTEGER)""")
        for t in ("knowledge_vec_bin", "knowledge_vec_bin_v2"):
            conn.execute(
                f"CREATE VIRTUAL TABLE {t} USING vec0("
                f"chunk_id TEXT PRIMARY KEY, embedding bit[8])")
        conn.execute(
            "CREATE TABLE knowledge_vec_f32 (chunk_id TEXT PRIMARY KEY, v BLOB)")
        import struct
        f32 = struct.pack("8f", *([1.0] * 8))
        for cid, email in (("c1", "harrison@f3energy.com"),
                           ("c2", "harrison@hjrglobal.com")):
            conn.execute(
                "INSERT INTO knowledge_chunks VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, "gmail", f"gmail:{email}:mid1", "F3E", None, "body", "t",
                 json.dumps({"message_id": "mid1", "user_email": email}),
                 int(time.time())))
            for t in ("knowledge_vec_bin", "knowledge_vec_bin_v2"):
                # Same insert shape the store uses (vec_quantize_binary over f32)
                conn.execute(
                    f"INSERT INTO {t}(chunk_id, embedding) "
                    "VALUES (?, vec_quantize_binary(?))", (cid, f32))
            conn.execute("INSERT INTO knowledge_vec_f32 VALUES (?, ?)", (cid, b"v"))
        conn.commit()

        delete_ids, _ = p.plan_purge(conn)
        assert delete_ids == ["c1"]
        deleted = p.apply_purge(conn, delete_ids)
        assert deleted["knowledge_vec_bin"] >= 1
        assert deleted["knowledge_vec_bin_v2"] >= 1
        assert deleted["knowledge_chunks"] == 1
        for t in ("knowledge_vec_bin", "knowledge_vec_bin_v2",
                  "knowledge_vec_f32", "knowledge_chunks"):
            remaining = conn.execute(f"SELECT chunk_id FROM {t}").fetchall()
            assert remaining == [("c2",)], t
        conn.close()


class TestKbArchiveCascadeDiscovery:
    def test_delete_chunks_cascades_bin_v2_when_present(self, tmp_path):
        from cora import kb_archive
        db, conn = _mk_kb(tmp_path)
        _ins(conn, "c1", "body", "mid1", "a@x.com")
        conn.commit()
        totals = kb_archive.delete_chunks(conn, ["c1"])
        assert totals["knowledge_vec_bin_v2"] == 1
        assert totals["knowledge_chunks"] == 1

    def test_delete_chunks_ok_without_bin_v2(self, tmp_path):
        from cora import kb_archive
        db = tmp_path / "kb2.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE knowledge_vec_bin (chunk_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE knowledge_vec_f32 (chunk_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO knowledge_chunks VALUES ('c1')")
        conn.commit()
        totals = kb_archive.delete_chunks(conn, ["c1"])
        assert totals["knowledge_chunks"] == 1
        assert "knowledge_vec_bin_v2" not in totals
