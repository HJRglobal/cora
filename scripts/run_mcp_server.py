#!/usr/bin/env python3
"""Entry point for Cora's read-only MCP server (stdio).

Spawned on demand by an MCP client (Claude Code via .mcp.json, or a Cowork
custom connector). Exposes Cora's read surface — KB semantic search, curated
known-answers, the code-session backlog, decision-log search, and health — all
READ-ONLY. See src/cora/mcp_server.py for the design invariants (read-only by
construction, store.search-only KB access, non-custodian PHI scrub, prompt-
injection framing).

Founder-scope, local, no network listener: the process speaks MCP over stdio to
its parent client only. Because it is a separate process that re-imports the
store fresh, running it needs NO Cora bot restart.

Register (Claude Code, .mcp.json in the cora repo):
    {
      "mcpServers": {
        "cora": {
          "command": ".venv\\Scripts\\python.exe",
          "args": ["scripts\\run_mcp_server.py"]
        }
      }
    }

Run standalone (for a manual smoke — it will block waiting for stdio JSON-RPC):
    .venv\\Scripts\\python.exe scripts\\run_mcp_server.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `import cora...` work when launched as a bare script (repo-root cwd or not).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
