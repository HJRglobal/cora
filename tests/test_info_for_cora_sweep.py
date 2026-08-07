"""R7(a): regression pins for the two HIGH watermark fixes, which shipped with
ZERO coverage (fan-out Lens D-1/D-2).

Both are the same failure -- silently losing input while reporting success:
  * the cap kept the NEWEST messages from a newest-first feed, so the watermark
    jumped past older backlog forever (D-038 class);
  * a mid-run ERROR was skipped anyway, because processing is oldest-first and the
    next success advanced the watermark past the failure.

Each test is written revert-and-fail (D-134/D-105): it must FAIL if the fix is
removed, not merely pass alongside it.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

try:
    import scripts.run_info_for_cora_sweep as sweep
    _IMPORT_OK = True
except Exception:  # noqa: BLE001
    _IMPORT_OK = False

pytestmark = pytest.mark.skipif(not _IMPORT_OK, reason="sweep script unavailable")


def _msg(ts, text="The Tucson stove vendor is Apex Appliance", user="U1"):
    return {"ts": f"{ts}.000000", "user": user, "text": text}


def _client(pages):
    """Stub whose conversations_history returns `pages` in order, NEWEST-first
    within each page, exposing next_cursor until the last page."""
    c = MagicMock()
    state = {"i": 0}

    def _history(**kwargs):
        i = state["i"]
        state["i"] += 1
        msgs, more = pages[i]
        return {"messages": msgs,
                "response_metadata": {"next_cursor": "CUR" if more else ""}}

    c.conversations_history.side_effect = _history
    return c


class TestCapKeepsOldest:
    def test_cap_trims_the_future_end_not_the_past(self):
        """Slack returns newest-first. Keeping the newest N and then advancing the
        watermark would skip the older tail permanently."""
        newest_first = [_msg(t) for t in range(120, 100, -1)]  # 120..101
        got, complete = sweep._fetch_messages(_client([(newest_first, False)]), "0", 5)
        assert complete is True
        ts = [m["ts"] for m in got]
        assert ts == [f"{t}.000000" for t in (101, 102, 103, 104, 105)], (
            "cap must keep the OLDEST 5 so the watermark stays contiguous")

    def test_backlog_larger_than_cap_drains_contiguously(self):
        """Consecutive runs must cover the window with no hole."""
        allmsgs = [_msg(t) for t in range(110, 100, -1)]  # 110..101
        first, _ = sweep._fetch_messages(_client([(allmsgs, False)]), "0", 4)
        assert [m["ts"] for m in first] == [f"{t}.000000" for t in (101, 102, 103, 104)]
        remaining = [m for m in allmsgs if float(m["ts"]) > 104]
        second, _ = sweep._fetch_messages(_client([(remaining, False)]), "104.000000", 4)
        assert [m["ts"] for m in second] == [f"{t}.000000" for t in (105, 106, 107, 108)]


class TestUnfetchedTailFreezes:
    def test_live_cursor_at_page_cap_reports_incomplete(self):
        """R6a: pagination stopping with a live cursor means an OLDER tail was
        never fetched at all -- the run must not advance the watermark."""
        pages = [([_msg(200 + i)], True) for i in range(sweep._MAX_PAGES)]
        _got, complete = sweep._fetch_messages(_client(pages), "0", 500)
        assert complete is False

    def test_exhausted_cursor_reports_complete(self):
        _got, complete = sweep._fetch_messages(_client([([_msg(1)], False)]), "0", 500)
        assert complete is True


def _run_main(tmp_path, monkeypatch, messages, ingest_side_effect, argv=None):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    wm = tmp_path / "wm.json"
    monkeypatch.setattr(sweep, "_WATERMARK_PATH", wm)
    monkeypatch.setattr(sys, "argv", argv or ["run_info_for_cora_sweep.py"])
    client = _client([(list(reversed(messages)), False)])
    client.auth_test.return_value = {"user_id": "UCORA"}
    with patch("slack_sdk.WebClient", return_value=client), \
         patch.object(sweep.info_intake, "ingest", side_effect=ingest_side_effect):
        rc = sweep.main()
    stored = json.loads(wm.read_text(encoding="utf-8"))["last_ts"] if wm.exists() else None
    return rc, stored


def _result(outcome):
    return sweep.info_intake.IntakeResult(outcome)


class TestFreezeOnError:
    def test_post_error_success_does_not_advance_past_the_failure(
            self, tmp_path, monkeypatch):
        """Processing is oldest-first, so without a run-scoped freeze the message
        AFTER the failure would move the watermark past it."""
        msgs = [_msg(10), _msg(11), _msg(12)]
        outcomes = {"10.000000": sweep.info_intake.QUEUED,
                    "11.000000": sweep.info_intake.ERROR,
                    "12.000000": sweep.info_intake.QUEUED}

        def _ingest(**kw):
            return _result(outcomes[kw["ts"]])

        rc, stored = _run_main(tmp_path, monkeypatch, msgs, _ingest)
        assert rc == 0
        assert stored is None, (
            "an ERROR must freeze the watermark for the REST of the run; "
            f"got {stored!r}")

    def test_clean_run_does_advance(self, tmp_path, monkeypatch):
        msgs = [_msg(10), _msg(11)]
        rc, stored = _run_main(tmp_path, monkeypatch, msgs,
                               lambda **kw: _result(sweep.info_intake.QUEUED))
        assert rc == 0 and stored == "11.000000"


class TestSinceDaysNeverAdvances:
    def test_recovery_flag_does_not_write_a_watermark(self, tmp_path, monkeypatch):
        """R6c / D-130(c): a --since-days window can start AFTER unswept backlog."""
        rc, stored = _run_main(
            tmp_path, monkeypatch, [_msg(10)],
            lambda **kw: _result(sweep.info_intake.QUEUED),
            argv=["run_info_for_cora_sweep.py", "--since-days", "90"])
        assert rc == 0
        assert stored is None
