# Known-answers store — Drive is authoritative

**The live bot reads and writes the Drive store, not these repo files.**

The canonical known-answers store lives on Drive at
`_brain/known-answers/`, resolved from the `KNOWN_ANSWERS_DIR` environment
variable (see `.env`). Both the read path (`context_loader`) and the write
path (`gap_autofill`) resolve the same way:

```python
Path(os.environ.get("KNOWN_ANSWERS_DIR") or <repo>/design/known-answers)
```

So on any host where `KNOWN_ANSWERS_DIR` is set (production), **Drive is
authoritative** and the `*.md` files in this directory are a **DR seed /
offline fallback only** — they are read *solely* when `KNOWN_ANSWERS_DIR` is
unset (local dev / CI / disaster recovery). They are intentionally allowed to
lag the live Drive store; do **not** hand-edit them expecting the change to go
live — edit Drive, or let the gap-autofill / knowledge-review flow write it.

Each seed file carries a one-line HTML-comment banner on line 1 stating this.
`tests/test_known_answers_drift.py` enforces that banner + the structural
invariants (a fallback file must still parse) so the notice can't be silently
dropped and the fallback can't silently break.

## Canonical entity → filename map (D-059)

`src/cora/known_answers_map.py` (`ENTITY_FILES`) is the ONE map both the read
and write sides import — do not diverge filenames from it. The `LEX-*`
sub-entities all map to `lex.md`; the read map additionally excludes the
`LEX-*` keys so a sub-entity answer never surfaces in a sibling channel.

## Files that are NOT part of this contract

- **`people.md`** — a hand-maintained people/roster reference. It is NOT in
  `ENTITY_FILES`, is NOT written by gap-autofill, and is not read via the
  known-answers read map. It carries no banner and is out of scope for the
  drift test.
- **`.resolved-gaps.jsonl`** — the gap-resolution ledger. It uses a *separate*
  env var (`RESOLVED_GAPS_PATH`, normally unset) and so writes to this repo
  directory by design; `drive_materializer` one-way-mirrors it to Drive
  `_brain/_flywheel/`. It is repo-canonical, not a stale fallback.
- **`dynamic/`** — per-entity dynamic-snapshot templates (own loader,
  `dynamic_answers.py`); independent of `KNOWN_ANSWERS_DIR`.

## Do not

- Do NOT change the read/write path — it is the sound D-059 single-map.
- Do NOT run `scripts/ingest_digest_answers.py` expecting a live effect: it is
  deprecated and env-blind (hardcodes this repo dir), so it writes to the
  fallback, invisible to the bot (which reads Drive).
