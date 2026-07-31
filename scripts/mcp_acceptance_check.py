#!/usr/bin/env python3
"""Acceptance check for Cora's read-only MCP server.

Spawns scripts/run_mcp_server.py as a stdio MCP child (exactly as an MCP client
would), then drives an initialize -> list_tools -> call_tool round-trip across
the five read-only tools and prints a PASS/FAIL summary. Use it as the first-run
smoke for a Cowork/Claude Code session that mounts the connector, and after any
change to mcp_server.py.

    .venv\\Scripts\\python.exe scripts\\mcp_acceptance_check.py

Exit code 0 = all queries returned non-error results; 1 = a failure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402


async def _run() -> int:
    params = StdioServerParameters(
        command=".venv/Scripts/python.exe",
        args=["scripts/run_mcp_server.py"],
        cwd=str(_REPO_ROOT),
    )
    failures = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            expected = {
                "cora_kb_search", "cora_decisions_search", "cora_known_answers",
                "cora_code_queue", "cora_health",
                # Listed but deliberately NOT called below: it is the surface's one
                # gated WRITE tool and an acceptance check must never write.
                "cora_code_queue_seed",
            }
            ok = tools == expected
            print(f"[{'PASS' if ok else 'FAIL'}] list_tools -> {sorted(tools)}")
            failures += 0 if ok else 1

            checks = [
                ("cora_health", {}),
                ("cora_kb_search", {"query": "what is the MCP read-only server", "entity": "FNDR", "limit": 3}),
                ("cora_decisions_search", {"query": "QBO primary financial source", "limit": 3}),
                ("cora_known_answers", {"entity": "F3E"}),
                ("cora_code_queue", {}),
            ]
            for name, args in checks:
                r = await session.call_tool(name, args)
                head = r.content[0].text.splitlines()[0] if r.content else "(no content)"
                bad = bool(r.is_error)
                print(f"[{'FAIL' if bad else 'PASS'}] {name} -> is_error={r.is_error} | {head[:80]}")
                failures += 1 if bad else 0

    print("\nACCEPTANCE:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
