"""WS5: action tools — complete_task (staged), confirm-gated delete, create followers."""

from unittest.mock import patch

import cora.tools.tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"
SHAUN_GID = "1209093544422692"  # Shaun Hawkins, from the real slack-to-asana.yaml


class TestCompleteTask:
    def test_refuses_without_confirmed(self):
        out = td._tool_asana_complete_task(HARRISON, "FNDR", {"task_gid": "123"})
        assert "refused" in out.lower() and "confirmed" in out.lower()

    def test_complete_by_gid_confirmed(self):
        with patch.object(td.asana_client, "complete_task", return_value={}) as mock:
            out = td._tool_asana_complete_task(HARRISON, "FNDR", {"task_gid": "123", "confirmed": True})
        mock.assert_called_once_with("123")
        assert "WRITE_CONFIRMED" in out and "complete" in out.lower()

    def test_complete_by_name_resolves_asker_tasks(self):
        tasks = [{"gid": "g1", "name": "Send the deck", "completed": False}]
        with patch.object(td.asana_client, "get_user_tasks", return_value=tasks), \
             patch.object(td.asana_client, "complete_task", return_value={}) as mock:
            out = td._tool_asana_complete_task(HARRISON, "FNDR",
                                               {"task_name": "send the deck", "confirmed": True})
        mock.assert_called_once_with("g1")
        assert "WRITE_CONFIRMED" in out

    def test_no_match_reports_clearly(self):
        with patch.object(td.asana_client, "get_user_tasks", return_value=[]):
            out = td._tool_asana_complete_task(HARRISON, "FNDR",
                                               {"task_name": "nonexistent", "confirmed": True})
        assert "no open task" in out.lower()

    def test_multiple_matches_asks_to_clarify(self):
        tasks = [
            {"gid": "g1", "name": "review deck draft", "completed": False},
            {"gid": "g2", "name": "review deck final", "completed": False},
        ]
        with patch.object(td.asana_client, "get_user_tasks", return_value=tasks), \
             patch.object(td.asana_client, "complete_task") as mock:
            out = td._tool_asana_complete_task(HARRISON, "FNDR",
                                               {"task_name": "review deck", "confirmed": True})
        mock.assert_not_called()
        assert "several" in out.lower()


class TestDeleteTask:
    def test_refuses_without_confirmed_and_warns_permanent(self):
        out = td._tool_asana_delete_task(HARRISON, "FNDR", {"task_gid": "123"})
        assert "refused" in out.lower() and "permanent" in out.lower()

    def test_delete_by_gid_confirmed(self):
        with patch.object(td.asana_client, "delete_task", return_value=None) as mock:
            out = td._tool_asana_delete_task(HARRISON, "FNDR", {"task_gid": "123", "confirmed": True})
        mock.assert_called_once_with("123")
        assert "WRITE_CONFIRMED" in out and "deleted" in out.lower()


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
        assert mock.call_args.args[1] == [SHAUN_GID] or mock.call_args.args[0] == "T1"
        assert "following" in out.lower()
