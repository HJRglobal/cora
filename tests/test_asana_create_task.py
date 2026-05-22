"""Unit tests for the asana_create_task tool — focuses on the staged-write
confirmation gate and assignee resolution. Real Asana API calls are stubbed.
"""

from unittest.mock import patch

import cora.tools.tool_dispatch as td


# Slack user IDs that exist in the real slack-to-asana.yaml shipped in the repo.
HARRISON_SLACK = "U0B2RM2JYJ1"
SHAUN_SLACK = "U0B3PS82G30"


def test_create_task_refuses_without_confirmed_flag():
    """The defense-in-depth refusal — even if Claude tries to skip the preview
    step, the tool itself blocks the create until confirmed=true is set."""
    result = td._tool_asana_create_task(
        slack_user_id=HARRISON_SLACK,
        entity="FNDR",
        _input={"title": "Try to skip the preview"},
    )
    assert "refused" in result.lower()
    assert "confirmed" in result.lower()
    assert "preview" in result.lower()


def test_create_task_refuses_with_confirmed_false():
    """Explicit false is treated the same as missing."""
    result = td._tool_asana_create_task(
        slack_user_id=HARRISON_SLACK,
        entity="FNDR",
        _input={"title": "Try false", "confirmed": False},
    )
    assert "refused" in result.lower()


def test_create_task_refuses_without_title():
    result = td._tool_asana_create_task(
        slack_user_id=HARRISON_SLACK,
        entity="FNDR",
        _input={"confirmed": True},
    )
    assert "title" in result.lower()


def test_create_task_with_confirmation_calls_asana_client():
    """When confirmed=true is set and the asker is mapped, the tool calls
    asana_client.create_task with the right arguments."""
    fake_created = {
        "gid": "999888777",
        "name": "Test task",
        "permalink_url": "https://app.asana.com/0/0/999888777",
        "assignee": {"name": "Harrison Rogers"},
        "due_on": None,
        "projects": [],
    }
    with patch.object(td.asana_client, "create_task", return_value=fake_created) as mock:
        result = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK,
            entity="FNDR",
            _input={
                "title": "Test task",
                "confirmed": True,
                "notes": "test context",
            },
        )

    mock.assert_called_once()
    call_kwargs = mock.call_args.kwargs
    assert call_kwargs["name"] == "Test task"
    assert call_kwargs["notes"] == "test context"
    # Defaulted to Harrison (the asker) — his Asana GID is in the real YAML
    assert call_kwargs["assignee_gid"] == "1204525779609669"
    # Project + due_on weren't specified, should be None
    assert call_kwargs["project_gid"] is None
    assert call_kwargs["due_on"] is None
    # The formatted response surfaces the task to the LLM
    assert "CREATED" in result
    assert "999888777" in result  # permalink fragment


def test_create_task_resolves_assignee_via_aliases():
    """`assignee_name=Sean` should resolve to Shaun Hawkins through user-aliases.yaml."""
    fake_created = {
        "gid": "111222333",
        "name": "Sean task",
        "permalink_url": "https://app.asana.com/0/0/111222333",
        "assignee": {"name": "Shaun Hawkins"},
        "due_on": None,
        "projects": [],
    }
    with patch.object(td.asana_client, "create_task", return_value=fake_created) as mock:
        result = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK,
            entity="FNDR",
            _input={
                "title": "Sean task",
                "assignee_name": "Sean",
                "confirmed": True,
            },
        )

    call_kwargs = mock.call_args.kwargs
    # Shaun's Asana GID from the real slack-to-asana.yaml shipped in the repo
    assert call_kwargs["assignee_gid"] == "1209093544422692"
    assert "CREATED" in result


def test_create_task_refuses_unresolvable_assignee():
    """Unknown assignee → tool returns a graceful error string (no API call)."""
    with patch.object(td.asana_client, "create_task") as mock:
        result = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK,
            entity="FNDR",
            _input={
                "title": "Bogus task",
                "assignee_name": "Nobody McGhost",
                "confirmed": True,
            },
        )

    mock.assert_not_called()
    assert "didn't match" in result.lower() or "didn't match" in result
