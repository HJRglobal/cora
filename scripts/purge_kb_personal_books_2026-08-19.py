"""D-194: purge Harrison's personal books from the KB, and re-tag the archive
files a Haiku guess mis-filed. STAGED -- Harrison runs `--apply`.

WHY
---
`01-HJR-Global/accounting/monthly-reports/` is swept into the KB by
justin@lexingtonservices.com, whose roster entry carries `entity_default: LEX`.
Any filename slug absent from `drive_entity_detect._CODE_TO_LABEL` therefore had
its entity decided by a Haiku guess anchored to LEX. Two distinct consequences,
and they need OPPOSITE remedies:

  PURGE  `hjrllc` = "Harrison Rogers, LLC" -- Harrison's PERSONAL books. Verified
         live on 2026-08-18: `2026-05_hjrllc_pl.xlsx` ingested as entity=LEX, the
         balance sheet as LEX/LEX-LLC (named bank accounts with last-4s, personal
         vehicle depreciation), the cash flow as LEX/LEX-LLA. So any #llc-*
         member -- TIER_1, financial discussion permitted, no PHI relaxation
         needed -- asking "what were our expenses last month?" could be served
         Harrison's personal balance sheet. There is no KB entity that means
         "personal", so the only correct answer is that these rows do not exist.
         The sweep-side exclusion shipped with this branch stops NEW ones
         (drive_entity_detect._EXCLUDED_CODES); this pass removes the ones the
         pre-exclusion sweeps already wrote.

  RETAG  `osn-core4` / `f3comm` / `hjrpod` / `mv` / `lexcorp` are legitimate
         business books that merely landed under the wrong entity. Purging them
         would throw away institutional knowledge to fix a labelling error, and
         a re-sweep would NOT restore them: the sweep is watermark-bounded, so an
         untouched file is never revisited. A metadata-only UPDATE fixes the
         placement in situ -- no RE-EMBEDDING (the content is unchanged, so the
         vector is too). It is NOT vec-table-free: `entity` is a vec0 PARTITION
         KEY on the coarse bin table as well as a column on knowledge_chunks, so
         the bin row is re-written from the stored float. See apply_retag.

TARGETING
---------
drive_sweep chunks carry a bare Drive file-id `source_id` and NO path, so the
filename (`title`) is the only usable key -- which is exactly what the detector
parses. Every row's bucket is decided by running the SAME functions the sweep
runs (`excluded_slug_from_filename`, `split_entity_label`), never a re-derived
LIKE pattern.

The two buckets are scoped DIFFERENTLY, on purpose:

  * PURGE is broad -- any filename whose first two naming positions carry an
    excluded slug, dated or not. Personal books must not survive anywhere.
  * RETAG is narrow -- ONLY `YYYY-MM_<slug>_<doctype>` filenames whose slug is in
    `_D194_RETAG_SLUGS`. See the comment on that constant for the two live
    over-reaches this narrowing prevents; both were found by dry-run, not by
    reasoning.

Anything else is left strictly alone.

Live dry-run 2026-08-19 over 253,478 Drive-sourced rows: 24 chunks / 18 files to
purge (hjrllc, 2025-12..2026-05, ingested across LEX, LEX-LLC and LEX-LLA -- the
same file family in three different placements, the signature of a guess); 52
chunks / 40 files to re-tag, including hjrpod books sitting under HJRP.

SAFETY
------
* Dry-run by DEFAULT. `--apply` performs the writes; `--dry-run` forces report-only.
* Deletes cascade through `kb_archive.delete_chunks`, whose table set comes from
  `schema.vec_cascade_tables` -- a NAMED candidate list existence-checked against
  sqlite_master, never a bare `LIKE 'knowledge_vec%'` (that also matches vec0's
  internal shadow tables, and deleting from those corrupts the virtual table).
* `--apply` opens via `schema.connect`, which loads sqlite-vec; a plain sqlite3
  connection cannot DELETE from the vec0 tables at all.
* A manifest is written on BOTH dry-run and apply, so the intended change is
  reviewable before it happens and auditable after.
* Stop Cora before `--apply` (the bot holds the KB open); a stale heartbeat is
  checked and reported, and `--force` overrides.

USAGE
-----
    .venv\\Scripts\\python.exe scripts\\purge_kb_personal_books_2026-08-19.py
    .venv\\Scripts\\python.exe scripts\\purge_kb_personal_books_2026-08-19.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import kb_archive  # noqa: E402
from cora.connectors.drive_entity_detect import (  # noqa: E402
    _CODE_TO_LABEL,
    excluded_slug_from_filename,
    has_date_token,
    naming_tokens,
    split_entity_label,
)
from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = _REPO_ROOT / "data" / "cora_kb.db"
HEARTBEAT_PATH = _REPO_ROOT / "data" / "health" / "heartbeat.txt"
MANIFEST_PATH = _REPO_ROOT / "logs" / "purge-kb-personal-books-2026-08-19.json"

# Only Drive-sourced rows carry a filename in `title`; every other source keys
# differently and must not be swept up by a title match.
_DRIVE_SOURCES = ("drive_sweep", "drive_asset")
_BATCH = 500

# The ONLY slugs eligible for re-tagging: accounting-archive slugs the detector
# could not resolve until this branch (plus osn-core4, added 8/18 for the same
# reason). For these, and ONLY these, the stored entity is PROVABLY a Haiku guess
# -- the deterministic override returned None at ingest, so nothing else could
# have set it.
#
# WHY THIS LIST AND NOT "the row disagrees with the detector"
# ----------------------------------------------------------
# That broader rule was written first and a live dry-run refuted it: it selected
# 17,523 chunks across 843 files, nearly all of them dated `..._lex_...` files
# currently carrying a LEX sub-entity. Their sub_entity was NOT a guess -- it was
# set deliberately by knowledge_base.lex_sub_entity at the upsert chokepoint
# (Part 2, 2026-06-07), which refines LEX rows from CONTENT after the filename
# detector has spoken. The filename says only "LEX", so "disagrees with the
# detector" read every one of those refinements as an error and would have
# STRIPPED them -- undoing a shipped feature while claiming to fix a mis-tag.
#
# The same dry-run showed the second failure mode: undated files like
# `LBHS.xlsx` and `Balance Sheet - Detail_LLA.xlsx` sit in the Founder OS tree,
# whose sweep derives entity from the FOLDER PATH and never consults the
# filename. Re-tagging those would replace a deterministic path fact with a
# filename inference. Hence the leading-date requirement below: the archive
# convention is `YYYY-MM_<slug>_<doctype>`, and only that family is in scope.
_D194_RETAG_SLUGS: frozenset[str] = frozenset({
    "osn-core4", "f3comm", "hjrpod", "mv", "lexcorp",
})


def _heartbeat_is_fresh(max_age_s: int = 180) -> bool:
    try:
        return (time.time() - HEARTBEAT_PATH.stat().st_mtime) < max_age_s
    except OSError:
        return False


def archive_slug(title: str) -> str | None:
    """Return the archive slug of a `YYYY-MM_<slug>_<doctype>` filename, else None.

    Requires BOTH the leading date token and the slug in the first naming
    position -- the accounting archive's convention. Undated files (the Founder
    OS tree, whose entity comes from the folder path) never qualify.
    """
    tokens = naming_tokens(title or "")
    if not tokens or not has_date_token(title or ""):
        return None
    return tokens[0]


def classify_row(title: str, entity: str | None, sub_entity: str | None) -> tuple[str, dict]:
    """Return ``(bucket, detail)`` for one KB row. Buckets: purge / retag / keep.

    Pure -- no DB, no IO -- so the decision is unit-testable against the real
    detector without a database.
    """
    excluded = excluded_slug_from_filename(title or "")
    if excluded:
        return "purge", {"slug": excluded}

    slug = archive_slug(title or "")
    if slug is None or slug not in _D194_RETAG_SLUGS:
        return "keep", {"reason": "not the D-194 archive class"}

    want_entity, want_sub = split_entity_label(_CODE_TO_LABEL[slug])
    have_entity = (entity or "") or None
    have_sub = (sub_entity or "") or None
    if have_entity == want_entity and have_sub == want_sub:
        return "keep", {"reason": "already correct"}
    return "retag", {
        "slug": slug,
        "from": {"entity": have_entity, "sub_entity": have_sub},
        "to": {"entity": want_entity, "sub_entity": want_sub},
    }


def scan(conn) -> tuple[list[str], dict[str, list[str]], dict]:
    """Walk every Drive-sourced row and bucket it.

    Returns ``(purge_ids, retag_ids_by_target, report)`` where the retag key is
    ``"ENTITY|SUB_ENTITY"`` (sub blank when None).
    """
    ph = ",".join("?" * len(_DRIVE_SOURCES))
    rows = conn.execute(
        f"SELECT chunk_id, source_id, title, entity, sub_entity "
        f"FROM knowledge_chunks WHERE source IN ({ph})",
        _DRIVE_SOURCES,
    ).fetchall()

    purge_ids: list[str] = []
    retag: dict[str, list[str]] = {}
    purge_titles: dict[str, dict] = {}
    retag_titles: dict[str, dict] = {}

    for chunk_id, source_id, title, entity, sub_entity in rows:
        bucket, detail = classify_row(title, entity, sub_entity)
        if bucket == "keep":
            continue
        if bucket == "purge":
            purge_ids.append(chunk_id)
            rec = purge_titles.setdefault(
                title, {"slug": detail["slug"], "chunks": 0, "file_ids": set(),
                        "entities": set()})
            rec["chunks"] += 1
            rec["file_ids"].add(source_id)
            rec["entities"].add(f"{entity or '-'}/{sub_entity or '-'}")
            continue
        want = detail["to"]
        key = f"{want['entity']}|{want['sub_entity'] or ''}"
        retag.setdefault(key, []).append(chunk_id)
        rec = retag_titles.setdefault(
            title, {"chunks": 0, "file_ids": set(), "from": set(), "to": key})
        rec["chunks"] += 1
        rec["file_ids"].add(source_id)
        rec["from"].add(f"{entity or '-'}/{sub_entity or '-'}")

    def _render(d: dict[str, dict]) -> list[dict]:
        out = []
        for title, rec in sorted(d.items()):
            item = {"title": title, "chunks": rec["chunks"],
                    "file_ids": len(rec["file_ids"])}
            if "slug" in rec:
                item["slug"] = rec["slug"]
            if "entities" in rec:
                item["ingested_as"] = sorted(rec["entities"])
            if "from" in rec:
                item["from"] = sorted(rec["from"])
                item["to"] = rec["to"]
            out.append(item)
        return out

    report = {
        "rows_scanned": len(rows),
        "purge": {"chunks": len(purge_ids), "files": _render(purge_titles)},
        "retag": {"chunks": sum(len(v) for v in retag.values()),
                  "by_target": {k: len(v) for k, v in sorted(retag.items())},
                  "files": _render(retag_titles)},
    }
    return purge_ids, retag, report


def apply_retag(conn, retag: dict[str, list[str]]) -> int:
    """Re-file chunks under their correct entity -- in BOTH stores that hold it.

    `entity` is NOT metadata. It lives in `knowledge_chunks` AND, as a vec0
    PARTITION KEY, in the coarse bin table(s) (`schema.py`:
    `entity TEXT PARTITION KEY`). `store._search_binary` filters stage 1 on the
    BIN table's copy and then re-filters stage 2 on `knowledge_chunks`, so
    updating only the latter makes a chunk unreachable under BOTH entities:
      * search the NEW entity -> the bin row still says the old one, so the chunk
        is never a coarse candidate;
      * search the OLD entity -> it IS a candidate, and stage 2 reads the new
        entity off knowledge_chunks and filters it out.
    The first cut of this pass did exactly that and called itself
    "metadata-only", which is true of `sub_entity` (enforced only at re-rank) and
    of the retro_tag_bot_slack_chunks precedent, but NOT of `entity`. It would
    have silently darkened all 52 rows it claimed to be fixing -- the same class
    the repo already locked as doctrine (a plain-sqlite3 test over plain tables
    passes vacuously), here on the UPDATE side rather than the DELETE side.

    The bin row is REPLACED rather than updated: a vec0 partition key is not
    reliably updatable in place, so the row is deleted and re-inserted with the
    stored float re-quantized -- byte-identical to what store.upsert_documents
    writes (`vec_quantize_binary` over the knowledge_vec_f32 blob).
    """
    from cora.knowledge_base import schema as _schema

    # Same named-candidate discovery store._bin_write_tables uses -- never a
    # LIKE sweep, which also matches vec0's internal shadow tables.
    bin_tables = _schema.bin_tables_present(conn)

    total = 0
    for key, ids in retag.items():
        entity, _, sub = key.partition("|")
        sub_val = sub or None
        for i in range(0, len(ids), _BATCH):
            batch = ids[i:i + _BATCH]
            ph = ",".join("?" * len(batch))

            # 1. The authoritative row.
            cur = conn.execute(
                f"UPDATE knowledge_chunks SET entity = ?, sub_entity = ? "
                f"WHERE chunk_id IN ({ph})",
                [entity, sub_val, *batch],
            )
            total += cur.rowcount

            # 2. The coarse index's copy of the same key, per bin table.
            for tbl in bin_tables:
                vecs = conn.execute(
                    f"SELECT chunk_id, embedding FROM knowledge_vec_f32 "
                    f"WHERE chunk_id IN ({ph})",
                    batch,
                ).fetchall()
                conn.execute(
                    f"DELETE FROM {tbl} WHERE chunk_id IN ({ph})", batch)
                for chunk_id, emb in vecs:
                    conn.execute(
                        f"INSERT INTO {tbl} (chunk_id, entity, embedding) "
                        f"VALUES (?, ?, vec_quantize_binary(?))",
                        (chunk_id, entity, emb),
                    )
    conn.commit()
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the purge + re-tag. Default is a read-only dry-run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Force report-only even if --apply is passed.")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--force", action="store_true",
                    help="Proceed with --apply even if Cora looks live.")
    args = ap.parse_args(argv)

    apply_changes = args.apply and not args.dry_run
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"KB not found: {db_path}")
        return 1

    if apply_changes and _heartbeat_is_fresh() and not args.force:
        print("REFUSING: Cora's heartbeat is fresh -- the bot holds the KB open.")
        print("Stop the service first (deployment doctrine #5), or pass --force.")
        return 2

    conn = schema.connect(db_path, read_only=not apply_changes)
    try:
        purge_ids, retag, report = scan(conn)

        print(f"D-194 personal-books pass ({'APPLY' if apply_changes else 'DRY-RUN'})")
        print(f"  Drive-sourced rows scanned: {report['rows_scanned']}")
        print(f"  PURGE : {report['purge']['chunks']} chunk(s) across "
              f"{len(report['purge']['files'])} file(s)")
        for f in report["purge"]["files"]:
            print(f"      - {f['title']}  slug={f['slug']}  chunks={f['chunks']}"
                  f"  file_ids={f['file_ids']}  ingested_as={','.join(f['ingested_as'])}")
        print(f"  RETAG : {report['retag']['chunks']} chunk(s) across "
              f"{len(report['retag']['files'])} file(s)")
        for f in report["retag"]["files"]:
            print(f"      - {f['title']}  {','.join(f['from'])} -> {f['to']}"
                  f"  chunks={f['chunks']}  file_ids={f['file_ids']}")

        result: dict = {"mode": "apply" if apply_changes else "dry-run", **report}

        if apply_changes:
            if purge_ids:
                deleted = kb_archive.delete_chunks(conn, purge_ids)
                result["deleted"] = deleted
                print(f"  deleted: {deleted}")
            if retag:
                updated = apply_retag(conn, retag)
                result["retagged_rows"] = updated
                print(f"  retagged rows: {updated}")
            print("\n  Reclaim disk separately if wanted: scripts\\reclaim_kb_space.py")
        else:
            print("\n  Dry-run only -- nothing was written. Re-run with --apply "
                  "(Cora stopped) to execute.")

        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"  manifest: {MANIFEST_PATH}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
