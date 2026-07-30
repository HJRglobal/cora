"""Slice 2 (2026-07-29 audit): a literal <, >, or & in an Asana task name must be
escaped inside the Slack mrkdwn link label, so the link token can't break early and
leave a stray &gt; after it (the 2026-07-29 07:35 daily-briefing artifact).

The daily briefing composes its Asana links via asana_client.format_tasks_for_llm;
format_created_task_for_llm shares the identical link-construction seam.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.tools import asana_client
from cora.tools.asana_client import (
    _escape_slack_link_label,
    format_created_task_for_llm,
    format_tasks_for_llm,
)

_URL = "https://app.asana.com/0/1/2"


class TestEscapeHelper:
    def test_escapes_gt_lt_amp_in_order(self):
        assert _escape_slack_link_label("R&D <urgent> now") == "R&amp;D &lt;urgent&gt; now"

    def test_plain_name_unchanged(self):
        assert _escape_slack_link_label("Tommy pricing rec") == "Tommy pricing rec"


class TestFormatTasksForLlm:
    def test_gt_in_name_is_escaped_not_left_loose(self):
        task = {
            "name": "Reclaim > Calendly reinstatement with Tessa",
            "permalink_url": _URL,
            "due_on": "2026-08-01",
        }
        out = format_tasks_for_llm([task])
        # Well-formed escaped link (the > lives INSIDE the label as &gt;)
        assert f"<{_URL}|Reclaim &gt; Calendly reinstatement with Tessa>" in out
        # The malformed shape that produced the stray &gt; must NOT appear
        assert f"<{_URL}|Reclaim > Calendly" not in out

    def test_clean_name_has_no_entity(self):
        task = {"name": "Tommy pricing rec", "permalink_url": _URL, "due_on": "2026-08-01"}
        out = format_tasks_for_llm([task])
        assert f"<{_URL}|Tommy pricing rec>" in out
        assert "&gt;" not in out and "&amp;" not in out

    def test_amp_and_lt_in_name_escaped(self):
        task = {"name": "R&D <spec> review", "permalink_url": _URL, "due_on": "2026-08-01"}
        out = format_tasks_for_llm([task])
        assert f"<{_URL}|R&amp;D &lt;spec&gt; review>" in out


class TestFormatCreatedTaskForLlm:
    def test_gt_in_created_name_escaped(self):
        out = format_created_task_for_llm(
            {"name": "A > B follow-up", "permalink_url": _URL, "due_on": "2026-08-02"}
        )
        assert f"<{_URL}|A &gt; B follow-up>" in out
        assert f"<{_URL}|A > B" not in out
