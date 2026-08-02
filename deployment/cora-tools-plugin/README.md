# cora-tools — Cowork plugin (v1.0.0)

Exposes Cora's read-only(+1) MCP surface (`src/cora/mcp_server.py`, D-092) as NATIVE tools in
every Cowork session, by having the Cowork desktop app spawn the stdio server on demand.
Proven live 2026-07-30 (session-comms spike: all tools connected + full kb_search round trip
incl. OpenAI embed + PHI scrub, spawned host-side from the Windows venv).

Tools (7; prefix `mcp__plugin_cora-tools_cora__`):
cora_kb_search · cora_decisions_search · cora_known_answers · cora_code_queue ·
cora_health · cora_delegated_jobs (delegated-work overview: ids/state/cost only, never
titles/briefs) · cora_code_queue_seed (the ONE gated write — backlog only, never canon; D-011).
Tools are discovered at runtime from `_TOOL_SPECS`, so a server-side tool addition needs NO
plugin reinstall.

## Install / update
1. Package (any Linux/WSL/sandbox shell with zip):
   `cd deployment/cora-tools-plugin && zip -r /tmp/cora-tools.plugin . -x "*.DS_Store"`
2. In the Claude desktop app: Settings -> Plugins -> uninstall the old version, install the
   new `.plugin` file (or accept from a chat card). Fully restart the app.
3. Smoke: fresh Cowork session -> "call cora_health" -> live heartbeat JSON.

## Notes / hazards
- Paths are machine-absolute (this repo at `C:\Users\Harri\code\cora`). Moving the repo or
  venv breaks the plugin — update `.mcp.json` and reinstall.
- This lane is UNDOCUMENTED-adjacent app behavior (plugin-bundled stdio MCP). An app update
  may break it silently. Fallback ladder lives in the Cowork `cora` skill: plugin tools ->
  snapshot files (`data/session-bus/snapshots/`, mirrored to `_brain/_bus/`) -> Slack `@Cora`
  (mention required). If tools vanish across sessions after an app update, re-run the
  native-lane spike and/or revive the shelved mailbox build
  (`_shared/projects/cora/_notes/2026-07-30_fndr_cora-code-prompt-session-bus.md`, slim scope).
- The server is a separate on-demand process: no bot restart ever needed to ship plugin or
  server changes; concurrent sessions spawn concurrent read-only instances (mode=ro, safe).
- History: the spike version also carried a `cora-http` entry (the shelved 8791 TLS bridge)
  — it never connected and was dropped in v1. The bridge stays shelved, unused by this lane.
