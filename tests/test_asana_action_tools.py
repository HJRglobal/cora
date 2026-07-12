"""WS5 + F-23: destructive Asana tools -- server-side staged writes.

F-23 (2026-07-12) replaced the honor-system `confirmed` flag with a server-side
pending store: the FIRST call previews + stashes the resolved gid, only the confirm
turn executes, and it executes the STASHED gid (never a confirm-turn-echoed name/
gid). Every non-write return is WRITE_BLOCKED-wrapped; success is WRITE_CONFIRMED.
"""

from unittest.mock import patch

import pytest

import cora.tools.tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"        # the founder -- gid path is exempt from the ownership check
TOMMY = "U0B3RU5Q55G"           # a NON-founder mapped user (ownership check applies)
SHAUN_GID = "1215737571684638"  # Shaun Hawkins, from the real slack-to-asana.yaml
_CH = "hjrg-leadership"


@pytest.fixture(autouse=True)
def _clear_asana_pending():
    td._PENDING_ASANA_WRITES.clear()
    yield
    td._PENDING_ASANA_WRITES.clear()


def _complete(inp, user=HARRISON, entity="FNDR"):
    return td._tool_asana_complete_task(user, entity, {**inp, "_channel_name": _CH})


def _delete(inp, user=HARRISON, entity="FNDR"):
    return td._tool_asana_delete_task(user, entity, {**inp, "_channel_name": _CH})


class TestCompleteTaskStaged:
    def test_first_call_previews_does_not_complete(self):
        with patch.object(td.asana_client, "complete_task") as mock:
            out = _complete({"task_gid": "123"})
        assert "WRITE_BLOCKED" in out and "not done yet" in out.lower()
        mock.assert_not_called()
        assert td.has_pending_asana_write(HARRISON, _CH)

    def test_first_call_confirmed_true_re_previews_never_completes(self):
        with patch.object(td.asana_client, "complete_task") as mock:
            out = _complete({"task_gid": "123", "confirmed": True})
        mock.assert_not_called()
        assert "WRITE_BLOCKED" in out

    def test_two_call_flow_completes_stashed_gid(self):
        with patch.object(td.asana_client, "complete_task", return_value={}) as mock:
            _complete({"task_gid": "123"})
            out = _complete({"confirmed": True})  # bare confirm
        mock.assert_called_once_with("123")
        assert "WRITE_CONFIRMED" in out and "complete" in out.lower()

    def test_confirm_uses_stashed_gid_not_confirm_turn_fields(self):
        # F-23 invariant: the confirm turn cannot act on a DIFFERENT task than
        # previewed. Preview resolves g1 by name; confirm echoes a different name.
        tasks = [{"gid": "g1", "name": "Send the deck", "completed": False}]
        with patch.object(td.asana_client, "get_user_tasks", return_value=tasks), \
             patch.object(td.asana_client, "complete_task", return_value={}) as mock:
            _complete({"task_name": "send the deck"})
            _complete({"task_name": "some other task", "confirmed": True})
        mock.assert_called_once_with("g1")

    def test_no_match_reports_on_preview(self):
        with patch.object(td.asana_client, "get_user_tasks", return_value=[]):
            out = _complete({"task_name": "nonexistent"})
        assert "no open task" in out.lower()

    def test_multiple_matches_asks_to_clarify_on_preview(self):
        tasks = [
            {"gid": "g1", "name": "review deck draft", "completed": False},
            {"gid": "g2", "name": "review deck final", "completed": False},
        ]
        with patch.object(td.asana_client, "get_user_tasks", return_value=tasks), \
             patch.object(td.asana_client, "complete_task") as mock:
            out = _complete({"task_name": "review deck"})
        mock.assert_not_called()
        assert "several" in out.lower()

    def test_gid_not_owned_refused_for_non_founder(self):
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "My task", "completed": False}]), \
             patch.object(td.asana_client, "complete_task") as mock:
            out = _complete({"task_gid": "someone-elses"}, user=TOMMY, entity="F3E")
        mock.assert_not_called()
        assert "isn't one of your open tasks" in out.lower()
        assert not td.has_pending_asana_write(TOMMY, _CH)  # nothing stashed to confirm

    def test_gid_owned_allowed_for_non_founder(self):
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "My task", "completed": False}]), \
             patch.object(td.asana_client, "complete_task", return_value={}) as mock:
            _complete({"task_gid": "mine"}, user=TOMMY, entity="F3E")
            out = _complete({"confirmed": True}, user=TOMMY, entity="F3E")
        mock.assert_called_once_with("mine")
        assert "WRITE_CONFIRMED" in out


class TestDeleteTaskStaged:
    def test_first_call_previews_warns_permanent_does_not_delete(self):
        with patch.object(td.asana_client, "delete_task") as mock:
            out = _delete({"task_gid": "123"})
        assert "WRITE_BLOCKED" in out and "permanent" in out.lower()
        mock.assert_not_called()
        assert td.has_pending_asana_write(HARRISON, _CH)

    def test_two_call_flow_deletes_stashed_gid(self):
        with patch.object(td.asana_client, "delete_task", return_value=None) as mock:
            _delete({"task_gid": "123"})
            out = _delete({"confirmed": True})
        mock.assert_called_once_with("123")
        assert "WRITE_CONFIRMED" in out and "deleted" in out.lower()

    def test_first_call_confirmed_true_never_deletes(self):
        with patch.object(td.asana_client, "delete_task") as mock:
            out = _delete({"task_gid": "123", "confirmed": True})
        mock.assert_not_called()  # no stash -> re-preview, never a blind delete
        assert "WRITE_BLOCKED" in out

    def test_delete_gid_not_owned_refused_for_non_founder(self):
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "My task", "completed": False}]), \
             patch.object(td.asana_client, "delete_task") as mock:
            out = _delete({"task_gid": "someone-elses"}, user=TOMMY, entity="F3E")
        mock.assert_not_called()
        assert "isn't one of your open tasks" in out.lower()


class TestCreateWithFollowers:
    def test_followers_added_and_surfaced(self):
        created = {"gid": "T1", "permalink_url": "http://x", "projects": []}
        with patch.object(td.asana_client, "create_task", return_value=created), \
             patch.object(td.asana_client, "get_project_tasks", return_value=[]), \
             patch.object(td.asana_client, "add_task_followers", return_value={}) as mock:
            out = td._tool_asana_create_task(HARRISON, "FNDR", {
                "title": "Coordinate the launch",
                "confirmed": True,
                "follower_names": ["Shaun"],
            })
        mock.assert_called_once()
        assert mock.call_args.args[0] == "T1"            # added to the created task
        assert mock.call_args.args[1] == [SHAUN_GID]     # 'Shaun' resolved to his real gid
        assert "following" in out.lower()
