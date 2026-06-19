"""WS1: cora_self_check reports LIVE state, never the KB; founder-gated detail."""

import threading
import time
from unittest.mock import patch

from cora.tools import tool_dispatch


class _FakeKB:
    def stats(self):
        return {
            "total_chunks": 123456,
            "by_source": {"slack": 100000, "fireflies": 20000, "static_md": 3456},
            "by_entity": {"FNDR": 50000},
        }

    def get_sync_state(self, source):
        return (int(time.time()) - 3600, None)  # 1h ago


def _patch_kb(kb):
    return patch.object(tool_dispatch, "_notes_kb", return_value=(kb, threading.Lock()))


class TestCoraSelfCheck:
    def test_founder_gets_detail(self):
        with _patch_kb(_FakeKB()), patch("cora.health_endpoint.heartbeat_age_seconds", return_value=30):
            out = tool_dispatch._tool_cora_self_check("U1", "FNDR", {})
        assert "Heartbeat: fresh" in out
        assert "123,456 chunks" in out
        assert "by source:" in out
        assert "last sync:" in out
        # NEVER narrate the KB about its own status.
        assert "knowledge base" in out.lower()
        assert "NOTE for Cora" in out

    def test_entity_channel_no_cross_entity_detail(self):
        with _patch_kb(_FakeKB()), patch("cora.health_endpoint.heartbeat_age_seconds", return_value=30):
            out = tool_dispatch._tool_cora_self_check("U2", "F3E", {})
        assert "Heartbeat: fresh" in out
        assert "123,456 chunks" in out
        assert "by source:" not in out          # cross-entity volume withheld
        assert "founder channel" in out.lower()

    def test_stale_heartbeat(self):
        with _patch_kb(_FakeKB()), patch("cora.health_endpoint.heartbeat_age_seconds", return_value=99999):
            out = tool_dispatch._tool_cora_self_check("U1", "FNDR", {})
        assert "STALE" in out

    def test_missing_heartbeat(self):
        with _patch_kb(_FakeKB()), patch("cora.health_endpoint.heartbeat_age_seconds", return_value=None):
            out = tool_dispatch._tool_cora_self_check("U1", "FNDR", {})
        assert "MISSING" in out

    def test_kb_unavailable_does_not_crash(self):
        with patch.object(tool_dispatch, "_notes_kb", return_value=(None, threading.Lock())), \
                patch("cora.health_endpoint.heartbeat_age_seconds", return_value=30):
            out = tool_dispatch._tool_cora_self_check("U1", "FNDR", {})
        assert "Knowledge base: unavailable" in out

    def test_registered_and_global_core(self):
        assert "cora_self_check" in tool_dispatch._TOOL_FUNCTIONS
        assert "cora_self_check" in tool_dispatch._GLOBAL_CORE_TOOLS
        names = {t["name"] for t in tool_dispatch.TOOL_DEFINITIONS}
        assert "cora_self_check" in names
        # Offered in a non-founder channel (it self-gates detail at runtime).
        f3e_tools = {t["name"] for t in tool_dispatch.tools_for_entity("F3E")}
        assert "cora_self_check" in f3e_tools
