# Fireflies API-key migration runbook (Phase 3, Harrison executes)

**Seed:** cq-ffcf6e4ffe7c (S4 -- prep only). **Plan of record:**
`_shared/projects/fireflies-deep-dive/2026-08-27_fndr_one-cora-notetaker-resolution-plan.md`.

**Nothing in this file has been executed.** No key was rotated, and no key is
printed here. This is the checklist for the Phase-3 cutover, written now so the
step that takes every Fireflies pipeline dark is not improvised later.

---

## Why this exists

Every Fireflies pipeline Cora runs authenticates as **Harrison's seat** today
(verified live 2026-08-27 via `fireflies_get_user`: the authenticated user is
harrison@hjrglobal.com, `is_admin: true`). Phase 3 deactivates the other six seats
and collapses billing to one. **If Harrison's seat is deactivated before the key is
migrated, every pipeline below goes dark at once** -- the nightly KB ingest, the
action extractor, the ask-capture cards, the coverage monitor, and the new daily
auditor.

## What depends on the key

One endpoint, one auth header, one token reader -- the chokepoint is real:

| Consumer | Path |
|---|---|
| Nightly KB ingest | `scripts/incremental_sync_fireflies.py` (03:30 AZ) |
| Backfill | `scripts/backfill_fireflies.py` |
| Meeting action capture | `src/cora/connectors/fireflies_action_extractor.py` |
| Meeting-ask cards | `scripts/run_meeting_ask_capture.py` (07:08 AZ) |
| Weekly coverage monitor | `scripts/run_fireflies_coverage.py` (Mon 08:10 AZ) |
| **Daily capture auditor** | `scripts/run_meeting_capture_audit.py` (07:22 AZ) |

All of them read the token through `fireflies_connector._token()`.

## The trap in `_token()` -- read this before editing `.env`

`_token()` tries **three** env var names in order and takes the first non-empty one:

```
FIREFLIES_API_KEY  ->  FIREFLIES_API_TOKEN  ->  FIREFLIES_TOKEN
```

Only `FIREFLIES_API_KEY` is set today. The failure mode to avoid: adding the new
key under a *different* one of those three names while the old key remains under
`FIREFLIES_API_KEY`. Precedence would silently keep using the **old** key, every
pipeline would keep working, and the migration would look successful while nothing
had migrated. Set the new value on the **existing `FIREFLIES_API_KEY` line** and
confirm the other two names are absent:

```bash
grep -c "^FIREFLIES_" .env
```

That must print `1`. (Related: the 2026-06-11 duplicate-`HEALTH_PING_URL` incident
-- dotenv takes the LAST duplicate of a repeated key, so never append a second
`FIREFLIES_API_KEY` line either.)

## Order of operations

1. **Do not start until the daily auditor reports the capture identity ACTIVE.**
   Every audit post ends with a seat line; it reads
   `capture identity NOT YET ACTIVE` until cora@'s seat exists. As of 2026-08-27 it
   does not (the Fireflies `users` query returns 7 seats, none of them cora@).
2. **Issue a new API key from cora@'s Fireflies account** (sign in as cora@ ->
   Settings -> Developer Settings -> API key). Do not revoke Harrison's yet.
3. **Swap the value on the existing `FIREFLIES_API_KEY` line in `.env`.** Keep the
   old value somewhere retrievable until step 5 passes.
4. **Prove the new key on a read before trusting it to a write-adjacent job:**
   ```bash
   .venv/Scripts/python.exe scripts/run_meeting_capture_audit.py --day <yesterday>
   ```
   Dry-run by default, posts nothing. It exercises the same `_graphql_query` path
   the ingest uses and prints the seat roster line.
5. **Prove ingest specifically** -- it is the one job whose failure is silent for a
   day:
   ```bash
   .venv/Scripts/python.exe scripts/incremental_sync_fireflies.py
   ```
   Expect a `Fireflies sync complete` line in `logs/kb-sync-fireflies-<date>.log`
   with a non-zero transcript count. **The watermark is NOT a file** -- it lives in
   the KB's `sync_state` table (`kb.get_sync_state("fireflies")`,
   `scripts/incremental_sync_fireflies.py:81`); there has never been a Fireflies
   watermark under `data/cache/`. Check the transcript count in the log rather than
   the exit code alone: the watermark advances on success, so a *silently empty*
   run still looks clean from the outside.
6. **Restart the bot.** `_token()` reads the environment, and the always-on process
   loaded `.env` at ITS startup -- so it keeps serving the OLD key until restarted.
   This matters because the bot really does hold a Fireflies consumer:
   `tool_dispatch.py:1069` imports `fireflies_action_extractor`. Scheduled scripts
   are unaffected (each fire is a fresh process and reads the new value straight
   away), which is exactly why this step is easy to forget.
   Elevated: `.\deployment\restart-cora.ps1`, then confirm a NEW pid in
   `logs/cora-instances.jsonl`.
7. **Re-auth the Cowork Fireflies connector** to cora@ (separate credential from
   `.env`; it is an interactive OAuth in the Cowork app).
8. **Only then** deactivate the other seats -- **"Deactivate and KEEP data"**, never
   "Deactivate and delete data" (which purges after 30 days) and never "Remove"
   (which is for out-of-domain users). Per the Fireflies help centre, deactivate-and-
   keep leaves all their meetings in the workspace and searchable, and is reversible.
9. **Reduce the purchased seat count on the Billing page.** Deactivation alone does
   not lower the bill; the docs only promise automatic billing relief on Remove.

## Rollback

Put the previous value back on the `FIREFLIES_API_KEY` line and re-run step 4.
Nothing else in the repo holds Fireflies state that a key swap invalidates -- the
watermarks, the dedup ledger and the KB are all keyed on transcript ids, which do
not change with the authenticating seat.

## What this runbook deliberately does not do

- It does not rotate anything. Credential actions are Harrison's (a sign-in as
  cora@ is a credential action by definition).
- It does not contact Fireflies. Harrison ruled 2026-08-27 that the vendor is not
  contacted for any part of this work.
- It does not touch the `FIREFLIES_API_TOKEN` / `FIREFLIES_TOKEN` fallbacks in
  `_token()`. Collapsing those three names to one is a sensible follow-up, but it
  is a code change with its own blast radius and does not belong inside a
  credential cutover.
