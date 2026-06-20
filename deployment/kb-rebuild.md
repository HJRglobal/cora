# Rebuilding `data/cora_kb.db` from scratch

The knowledge base is **regenerable** — it is built entirely by re-running the
connector sync/backfill scripts against the source systems (Drive, Asana,
Fireflies, Notion, Slack, Gmail) plus the local static-markdown tree. That is
why `cora_kb.db` is **NOT** backed up to Drive by default (see `backup_logs.py`):
backing up a ~6 GB regenerable file daily is large Drive cost for ~zero DR value.

If the DB is lost or corrupted, rebuild it — don't restore it.

## Fast path — automatic (no manual work, ~1–3 nights)

1. Stop Cora (elevated): `schtasks /End /TN "cowork-cora-service"` + kill bot procs.
2. Move the DB aside: `mv data/cora_kb.db data/cora_kb.db.bak` (the schema
   auto-creates an empty DB on next open).
3. Start Cora. The nightly KB-sync tasks (registered by
   `deployment/setup-kb-sync-tasks.ps1`) will repopulate incrementally over the
   next runs. No watermark = each connector falls back to its lookback window.

## Full path — manual, immediate (one maintenance window)

With Cora stopped and from the repo root, using the repo venv
(`.venv\Scripts\python.exe`, NOT uv — D-005). Each writes into a fresh
`data/cora_kb.db` (schema auto-created on first open); all ingest-time guards
(deny-list, LEX hard-exclude, cora-internal exclusion) apply automatically.

```
# 1. Static markdown (CLAUDE.md / decisions / memory / project briefs)
.venv\Scripts\python.exe scripts\migrate_static_md.py          # full load
# 2. Per-connector backfills (full history)
.venv\Scripts\python.exe scripts\backfill_asana.py
.venv\Scripts\python.exe scripts\backfill_fireflies.py
.venv\Scripts\python.exe scripts\backfill_drive_assets.py
.venv\Scripts\python.exe scripts\run_drive_sweep.py --backfill --freshness-days 730
# (gmail/slack/notion are picked up by their incremental_sync_* on the nightly tasks;
#  run them directly for an immediate load:)
.venv\Scripts\python.exe scripts\incremental_sync_slack.py --backfill
.venv\Scripts\python.exe scripts\incremental_sync_notion.py --backfill
# 3. Sub-entity tags (LEX) + the binary search index
.venv\Scripts\python.exe scripts\backfill_lex_sub_entity.py
.venv\Scripts\python.exe scripts\migrate_kb_binary_index.py
```

Check each script's `--help` for the exact flags; some take `--backfill` /
`--freshness-days` / `--since`. Confirm the result:

```
.venv\Scripts\python.exe scripts\kb_audit.py        # counts by source/entity
```
or ask Cora `@Cora are you working?` (the `cora_self_check` tool reports total
chunks + by-source + sync watermarks).

## Notes

- **Float vector table:** the 2026-06-07 binary-index migration added
  `knowledge_vec_bin` + `knowledge_vec_f32`; the legacy `knowledge_vec` float
  table (~1.4 GB) was slated for a follow-up drop. Confirm during a stopped
  window: `SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_vec%'`. If
  `knowledge_vec` is still present alongside the two, drop it + `VACUUM INTO` to
  reclaim (irreversible — recall-check first; see TOM 0nn).
- **Retention:** `scripts/prune_kb_retention.py` exists (gmail/drive_sweep only,
  dry-run default) — not scheduled; run manually if the KB grows unwieldy.
- **Live-log retention** is separate (`compact_logs.py` gzips/purges dated logs);
  this backup only prunes backup *directories* to `--keep-days` (default 30).
