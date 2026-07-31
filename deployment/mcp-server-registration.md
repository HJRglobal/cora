# Cora read-only MCP server — registration

_A local stdio MCP server that lets Harrison's interactive Claude surfaces
(Cowork sessions, Claude Code) query Cora's live read surface — KB semantic
search, curated known-answers, the code-session backlog, the decision log, and
health — instead of file-grepping the Founder OS._

- **Module:** `src/cora/mcp_server.py` · **Entry:** `scripts/run_mcp_server.py`
- **Decision of record:** `memory/decisions.md` 2026-07-28 [FNDR/CORA]; eval doc
  `_shared/projects/cora/2026-07-28_cora_claude-integration-opportunities-evaluation.md` §8.
- **Read-only, founder-scope, local** (stdio child process — no network listener,
  no port). It attaches to the live KB in SQLite `mode=ro`; every write raises on
  that handle. One named write exception (2026-07-30): `cora_code_queue_seed`,
  a passthrough to the Harrison-gated, PHI-fail-closed code-queue seed path.
- **No bot restart** — it is a separate process the MCP client spawns on demand.

> **Lanes (updated 2026-07-31).** The **Cowork lane is now the `cora-tools`
> PLUGIN** (source committed at `deployment/cora-tools-plugin/`; proven live in
> the 2026-07-30 session-comms spike) — section B below is the legacy manual
> connector route, kept as fallback. For **Claude Code sessions OUTSIDE this
> repo**, register once at user scope instead of a per-repo `.mcp.json`:
> `claude mcp add --scope user cora -- C:\Users\Harri\code\cora\.venv\Scripts\python.exe C:\Users\Harri\code\cora\scripts\run_mcp_server.py`.
> Surfaces with no MCP at all (claude.ai web/mobile via the Drive connector,
> mount-less sandboxes) read the snapshot files instead:
> `data/session-bus/snapshots/` (mirrored to `_brain/_bus/snapshots/`), written
> by `src/cora/session_snapshots.py`.

> **Registration is Harrison's action** (a connector write). The steps below are
> the exact snippets; nothing here has been applied. Prereq: `mcp` is installed
> in the venv — it is declared in `pyproject.toml` (`mcp>=2.0`); if a fresh sync
> is needed, run `uv pip install mcp` (or `uv sync`).

---

## Tools (all read-only)

| Tool | Args | Returns |
|---|---|---|
| `cora_kb_search` | `query`, `entity?` (default FNDR), `limit?` (default 8, max 20) | Top KB chunks, founder scope, distance-gated, PHI-scrubbed |
| `cora_decisions_search` | `query`, `limit?` | Matches in the founder TOM (CLAUDE.md) + `decisions.md` |
| `cora_known_answers` | `entity` | The entity's curated known-answers file (LEX sub-entities excluded) |
| `cora_code_queue` | — | The generated code-session backlog view |
| `cora_health` | — | Heartbeat age / uptime / recent task-fire results (never restarts) |
| `cora_code_queue_seed` | `kind`, `severity`, `title`, `summary`, `entity`, `status?`, `subsystem_guess?` | **The one write tool**: seeds a backlog item via the gated `code_queue.seed_item` path (PHI-fail-closed, idempotent; backlog only, never canon) |

`entity` codes: `F3E OSN LEX UFL BDM HJRP HJRPROD HJRG F3C FNDR` (+ LEX sub-entities
`LEX-LLC/LEX-LTS/LEX-LBHS/LEX-LLA` for `cora_kb_search`).

---

## A. Claude Code (`.mcp.json` in the cora repo)

Create `C:\Users\Harri\code\cora\.mcp.json` (a template lives beside this file at
`deployment/mcp.json.example`):

```json
{
  "mcpServers": {
    "cora": {
      "command": ".venv\\Scripts\\python.exe",
      "args": ["scripts\\run_mcp_server.py"]
    }
  }
}
```

Claude Code launches the command from the workspace root, so the relative paths
resolve. (The server also derives its own repo root + `.env` from its file
location, so it is cwd-independent regardless.) Restart the Claude Code session
in the cora repo; `/mcp` should list the `cora` server with its five tools.

## B. Cowork custom connector

In Cowork → **Settings → Connectors → Add custom connector** (stdio):

- **Command:** `C:\Users\Harri\code\cora\.venv\Scripts\python.exe`
- **Arguments:** `scripts\run_mcp_server.py`
- **Working directory:** `C:\Users\Harri\code\cora`

Use absolute paths in Cowork (it does not launch from the repo root). Save, then
mount the connector in a session.

---

## C. Acceptance check (first mount)

From the repo root (or have the first mounting session run it):

```bash
.venv\Scripts\python.exe scripts\mcp_acceptance_check.py
```

It spawns the server exactly as a client would and drives an
`initialize → list_tools → call_tool` round-trip across the five read-only tools,
printing a PASS/FAIL summary (exit 0 = all green). Expected: `list_tools` shows
all six tools (`cora_code_queue_seed` is listed but deliberately never called —
an acceptance check must not write) and every call returns `is_error=False` with
a provenance-framed result.

In a mounted session, the 3-query smoke is simply:

1. `cora_health` → alive, heartbeat age < 300s.
2. `cora_kb_search` `{query: "<a topic you know is in the KB>"}` → relevant chunks,
   each prefixed with the `[Cora read-only KB …]` provenance line.
3. `cora_decisions_search` `{query: "<a known D-0xx doctrine>"}` → the matching
   decision text.

---

## Notes / boundaries

- **Read-only by construction:** the KB handle is `mode=ro`; no KB/canon write
  code path exists. The one named exception (shipped 2026-07-30) is
  `cora_code_queue_seed` — the same Harrison-gated, PHI-fail-closed,
  fingerprint-idempotent path the code-queue capture flow uses; backlog only,
  never canon (D-011 untouched).
- **PHI:** LEX content is returned scrubbed to a non-custodian view (client-name
  citations neutralized) — the same scrub the founder retrieval path uses, reused
  verbatim. The KB-excluded confidential stores (dashboard / OneAmerica /
  capital-raise / COPA-NDA / Fireflies-COPA) are absent from the corpus by
  construction (dropped at ingest), so they cannot surface here.
- **Untrusted results:** returned KB/known-answers/backlog text is reference DATA.
  Every result carries a provenance line telling the consuming session to treat
  any embedded instructions as content to evaluate, not commands.
- The server versions with the repo (pinned module + entry script), so it stays
  aligned with the store schema.
```
