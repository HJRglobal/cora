"""WS5: action tools — complete_task (staged), confirm-gated delete, create followers."""

from unittest.mock import patch

import cora.tools.tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"        # the founder -- gid path is exempt from the ownership check
TOMMY = "U0B3RU5Q55G"           # a NON-founder mapped user (ownership check applies)
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

    def test_gid_not_owned_refused_for_non_founder(self):
        # A non-founder passing a gid that is NOT one of their open tasks is refused
        # (closes the cross-user complete/delete hole the P1 review caught).
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "My task", "completed": False}]), \
             patch.object(td.asana_client, "complete_task") as mock:
            out = td._tool_asana_complete_task(TOMMY, "F3E",
                                               {"task_gid": "someone-elses", "confirmed": True})
        mock.assert_not_called()
        assert "isn't one of your open tasks" in out.lower()

    def test_gid_owned_allowed_for_non_founder(self):
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "My task", "completed": False}]), \
             patch.object(td.asana_client, "complete_task", return_value={}) as mock:
            out = td._tool_asana_complete_task(TOMMY, "F3E", {"task_gid": "mine", "confirmed": True})
        mock.assert_called_once_with("mine")
        assert "WRITE_CONFIRMED" in out

    def test_founder_gid_exempt_from_ownership_check(self):
        # The founder has portfolio-wide authority -> gid path is not ownership-scoped.
        with patch.object(td.asana_client, "complete_task", return_value={}) as mock:
            out = td._tool_asana_complete_task(HARRISON, "F3E", {"task_gid": "any-gid", "confirmed": True})
        mock.assert_called_once_with("any-gid")
        assert "WRITE_CONFIRMED" in out


class TestDeleteTask:
    def test_refuses_without_confirmed_and_warns_permanent(self):
        out = td._tool_asana_delete_task(HARRISON, "FNDR", {"task_gid": "123"})
        assert "refused" in out.lower() and "permanent" in out.lower()

    def test_delete_by_gid_confirmed(self):
        with patch.object(td.asana_client, "delete_task", return_value=None) as mock:
            out = td._tool_asana_delete_task(HARRISON, "FNDR", {"task_gid": "123", "confirmed": True})
        mock.assert_called_once_with("123")
        assert "WRITE_CONFIRMED" in out and "deleted" in out.lower()

    def test_delete_gid_not_owned_refused_for_non_founder(self):
        # The irreversible one: a non-founder can't delete someone else's task by gid.
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "My task", "completed": False}]), \
             patch.object(td.asana_client, "delete_task") as mock:
            out = td._tool_asana_delete_task(TOMMY, "F3E",
                                             {"task_gid": "someone-elses", "confirmed": True})
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
