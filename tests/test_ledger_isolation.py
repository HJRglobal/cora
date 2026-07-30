"""CI guard (Slice 5, 2026-07-29 audit): no test may write to a real ledger.

The 2026-07-29 Slack-output audit found test-fixture rows in two LIVE ledgers
(logs/cora-autowrite-audit.jsonl, data/state/code-session-queue*.jsonl): the two
rollout flags were flipped ON in .env and the test process inherited them, so
tests that drove the autowrite / code-queue write paths appended to the real files.

conftest.py's autouse `_isolate_cross_test_global_state` now (a) resets both flags
to "off" and (b) redirects every module-constant ledger writer to a tmp path. These
tests fail loudly if that isolation is ever weakened -- the pattern mirrors
test_no_raw_slack_post.py's "the guard must stay in place" contract.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

# Keep in sync with conftest._LEDGER_CONSTS. Each module-constant ledger writer
# must resolve to a tmp path during tests, never the repo's real logs/ or data/.
_LEDGER_CONSTS = [
    ("cora.code_queue", "_EVENT_LEDGER"),
    ("cora.code_queue", "_FINGERPRINT_LEDGER"),
    ("cora.code_queue", "_SIGNALS_LEDGER"),
    ("cora.knowledge_review", "_AUTOWRITE_AUDIT_PATH"),
    ("cora.pm_metrics", "_ACTION_LOG"),
    ("cora.finance_receipts", "_AUDIT_LOG_PATH"),
    ("cora.historical_access", "_AUDIT_LOG_PATH"),
    ("cora.session_capture", "LEDGER_PATH"),
    ("cora.feedback_log", "_LOG_PATH"),
    ("cora.user_feedback_tracker", "_LOG_PATH"),
    ("cora.tools.tool_dispatch", "_SHOPIFY_WRITE_AUDIT_PATH"),
]


@pytest.mark.parametrize("mod_name,attr", _LEDGER_CONSTS)
def test_ledger_constant_redirected_to_tmp(mod_name, attr):
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:  # pragma: no cover - module rename would surface here
        pytest.skip(f"{mod_name} not importable: {exc}")
    if not hasattr(mod, attr):
        pytest.skip(f"{mod_name}.{attr} no longer exists")
    val = getattr(mod, attr)
    p = Path(str(val)).resolve()
    real_logs = (_REPO / "logs").resolve()
    real_data = (_REPO / "data").resolve()
    assert real_logs not in p.parents and real_data not in p.parents, (
        f"{mod_name}.{attr} = {p} points inside the repo's real logs/ or data/ tree "
        "during a test -- the conftest ledger isolation has been weakened. A test "
        "can now pollute a live ledger. Restore the autouse redirect in conftest.py."
    )


def test_rollout_flags_reset_off():
    """The two rollout flags must read 'off' inside every test regardless of .env,
    so a test never drives a live autowrite / code-queue write path by accident."""
    assert os.environ.get("CORA_AUTOWRITE_LIVE") == "off", (
        "CORA_AUTOWRITE_LIVE is not reset to 'off' in tests -- .env leakage can "
        "re-pollute logs/cora-autowrite-audit.jsonl"
    )
    assert os.environ.get("CORA_CODE_QUEUE") == "off", (
        "CORA_CODE_QUEUE is not reset to 'off' in tests -- .env leakage can "
        "re-pollute data/state/code-session-queue.jsonl"
    )
