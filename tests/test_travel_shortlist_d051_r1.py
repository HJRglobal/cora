"""Code #16 C2 -- D-051 round-1 regressions for the travel lane's predicates, parser,
routing and wiring (fixer B1).

Every test drives the REAL code path (the pure predicates on Slack WIRE text, or the
real handle_mention / handle_message_event / _dispatch_qa entry points with only
infrastructure stubbed, via the wiring module's ``lane`` fixture). The guest name is
SYNTHETIC ("Jordan Riverstone"); the loyalty number is a made-up digit run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cora.app as app_module
from cora import active_thread_store
from cora import travel_shortlist as ts
from test_travel_shortlist import _slack_client
from test_travel_shortlist_wiring import (  # noqa: F401 -- `lane` is a fixture
    ASK, ASK_TS, TRAVEL_CHANNEL, _drain, _mention, _tessa, lane,
)

_REPO_ROOT = Path(app_module.__file__).resolve().parents[2]


# ── harness-isolation#1: the lane's active-thread register never hits the repo db ─

class TestActiveThreadStoreIsRedirected:
    def test_a_lane_turn_registers_in_the_tmp_store_never_the_repo_db(self, lane, tmp_path):
        repo_db = _REPO_ROOT / "data" / "active_threads.db"
        before = repo_db.stat().st_mtime_ns if repo_db.exists() else None
        # the conftest belt points the module constant at THIS test's tmp dir
        assert Path(active_thread_store._DB_PATH).resolve().parent == tmp_path.resolve()
        _mention(_slack_client(), MagicMock(), ASK, user=_tessa())
        _drain()
        # the row the travel intercept registered landed in the redirected store...
        assert active_thread_store.is_active(TRAVEL_CHANNEL, ASK_TS)
        # ...and the repo's live SQLite store was never touched
        after = repo_db.stat().st_mtime_ns if repo_db.exists() else None
        assert after == before
