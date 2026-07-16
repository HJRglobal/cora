"""asana_client edit primitives (PM-hub Phase 1, 2026-07-15).

update_task (PUT native fields), create_subtask (POST subtasks), and
list_custom_field_enum_options (resolve a Status/Priority value name -> option
GID). These back the new conversational edit tools.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.tools import asana_client  # noqa: E402


def _resp(data, status=200):
    r = MagicMock()
    r.status_code = status
    r.text = "err"
    r.json.return_value = {"data": data}
    return r


def _client(verb_responses):
    """Mock httpx.Client CM. verb_responses maps 'get'/'put'/'post'/'delete' -> a
    response (or side_effect list)."""
    client = MagicMock()
    for verb, resp in verb_responses.items():
        m = getattr(client, verb)
        if isinstance(resp, list):
            m.side_effect = resp
        else:
            m.return_value = resp
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return client, MagicMock(return_value=cm)


@pytest.fixture(autouse=True)
def _pat_env(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "test-pat")


class TestUpdateTask:
    def test_puts_only_provided_fields(self):
        client, factory = _client({"put": _resp({"gid": "T1", "name": "New name"})})
        with patch.object(asana_client.httpx, "Client", factory):
            out = asana_client.update_task("T1", {"name": "New name", "due_on": "2026-08-01"})
        assert out["gid"] == "T1"
        sent = client.put.call_args.kwargs["json"]["data"]
        assert sent == {"name": "New name", "due_on": "2026-08-01"}
        assert client.put.call_args.args[0].endswith("/tasks/T1")

    def test_assignee_none_unassigns(self):
        client, factory = _client({"put": _resp({"gid": "T1"})})
        with patch.object(asana_client.httpx, "Client", factory):
            asana_client.update_task("T1", {"assignee": None})
        assert client.put.call_args.kwargs["json"]["data"] == {"assignee": None}

    def test_due_none_clears_without_shape_error(self):
        client, factory = _client({"put": _resp({"gid": "T1"})})
        with patch.object(asana_client.httpx, "Client", factory):
            asana_client.update_task("T1", {"due_on": None})  # deliberate clear
        assert client.put.call_args.kwargs["json"]["data"] == {"due_on": None}

    def test_bad_due_shape_raises(self):
        with pytest.raises(asana_client.AsanaClientError, match="YYYY-MM-DD"):
            asana_client.update_task("T1", {"due_on": "Aug 1"})

    def test_empty_fields_raises(self):
        with pytest.raises(asana_client.AsanaClientError, match="at least one field"):
            asana_client.update_task("T1", {})

    def test_missing_gid_raises(self):
        with pytest.raises(asana_client.AsanaClientError, match="non-empty task_gid"):
            asana_client.update_task("", {"name": "x"})

    def test_404_raises(self):
        client, factory = _client({"put": _resp({}, status=404)})
        with patch.object(asana_client.httpx, "Client", factory):
            with pytest.raises(asana_client.AsanaClientError, match="404"):
                asana_client.update_task("T1", {"name": "x"})


class TestCreateSubtask:
    def test_posts_to_subtasks_endpoint(self):
        client, factory = _client({"post": _resp({"gid": "S1", "permalink_url": "http://x"})})
        with patch.object(asana_client.httpx, "Client", factory):
            out = asana_client.create_subtask("PARENT", name="Do the thing", assignee_gid="U9")
        assert out["gid"] == "S1"
        assert client.post.call_args.args[0].endswith("/tasks/PARENT/subtasks")
        sent = client.post.call_args.kwargs["json"]["data"]
        assert sent["name"] == "Do the thing"
        assert sent["assignee"] == "U9"
        # subtask inherits parent's project/workspace -> we never send them
        assert "workspace" not in sent and "projects" not in sent

    def test_requires_name(self):
        with pytest.raises(asana_client.AsanaClientError, match="non-empty `name`"):
            asana_client.create_subtask("PARENT", name="")

    def test_requires_parent(self):
        with pytest.raises(asana_client.AsanaClientError, match="non-empty parent_gid"):
            asana_client.create_subtask("", name="x")

    def test_bad_due_shape_raises(self):
        with pytest.raises(asana_client.AsanaClientError, match="YYYY-MM-DD"):
            asana_client.create_subtask("PARENT", name="x", due_on="soon")


class TestEnumOptions:
    def test_returns_options(self):
        data = {"enum_options": [
            {"gid": "o1", "name": "Not Started", "enabled": True},
            {"gid": "o2", "name": "In Progress", "enabled": True},
        ]}
        client, factory = _client({"get": _resp(data)})
        with patch.object(asana_client.httpx, "Client", factory):
            opts = asana_client.list_custom_field_enum_options("F1")
        assert [o["name"] for o in opts] == ["Not Started", "In Progress"]
        assert opts[0]["gid"] == "o1"

    def test_empty_field_gid_returns_empty(self):
        assert asana_client.list_custom_field_enum_options("") == []

    def test_error_returns_empty_never_raises(self):
        client, factory = _client({"get": _resp({}, status=500)})
        with patch.object(asana_client.httpx, "Client", factory):
            assert asana_client.list_custom_field_enum_options("F1") == []
