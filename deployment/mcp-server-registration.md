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
  that handle, and no write tool is exposed.
- **No bot restart** — it is a separate process the MCP client spawns on demand.

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
`initialize → list_tools → call_tool` round-trip across all five tools, printing
a PASS/FAIL summary (exit 0 = all green). Expected: `list_tools` shows the five
tools and every call returns `is_error=False` with a provenance-framed result.

In a mounted session, the 3-query smoke is simply:

1. `cora_health` → alive, heartbeat age < 300s.
2. `cora_kb_search` `{query: "<a topic you know is in the KB>"}` → relevant chunks,
   each prefixed with the `[Cora read-only KB …]` provenance line.
3. `cora_decisions_search` `{query: "<a known D-0xx doctrine>"}` → the matching
   decision text.

---

## Notes / boundaries

- **Read-only by construction:** the KB handle is `mode=ro`; there is no write
  tool and no write code path. The one future exception (code-queue seed via its
  confirm gate) is explicitly out of v1.
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
