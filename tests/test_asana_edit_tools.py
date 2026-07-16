"""PM-hub Phase 1 Slice 1b: conversational task-EDIT tools.

asana_update_task / asana_add_comment / asana_add_subtask -- staged-write,
ownership-scoped (reuse _resolve_asker_task WS5 + the F-23 pending store), LEX
PHI-scrubbed, wired into the deterministic confirm interceptor. No new autonomy:
every one is preview -> explicit confirm; the confirm executes the STASHED payload.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import cora.tools.tool_dispatch as td
from cora import app as cora_app

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "design" / "system-prompts"

HARRISON = "U0B2RM2JYJ1"        # founder -- gid path exempt from the ownership check
TOMMY = "U0B3RU5Q55G"           # NON-founder mapped user (ownership applies)
SHAUN_GID = "1215737571684638"  # Shaun Hawkins, from the real slack-to-asana.yaml
_CH = "hjrg-leadership"
_F3E = "f3e-leadership"


@pytest.fixture(autouse=True)
def _clear_asana_pending():
    td._PENDING_ASANA_WRITES.clear()
    yield
    td._PENDING_ASANA_WRITES.clear()


def _update(inp, user=HARRISON, entity="FNDR", channel=_CH):
    return td._tool_asana_update_task(user, entity, {**inp, "_channel_name": channel})


def _comment(inp, user=HARRISON, entity="FNDR", channel=_CH):
    return td._tool_asana_add_comment(user, entity, {**inp, "_channel_name": channel})


def _subtask(inp, user=HARRISON, entity="FNDR", channel=_CH):
    return td._tool_asana_add_subtask(user, entity, {**inp, "_channel_name": channel})


# ─────────────────────────── asana_update_task ───────────────────────────
class TestUpdateStaged:
    def test_preview_stashes_does_not_write(self):
        with patch.object(td.asana_client, "update_task") as mock:
            out = _update({"task_gid": "T1", "new_due_on": "2026-08-01"})
        assert "WRITE_BLOCKED" in out and "not updated yet" in out.lower()
        assert "set due 2026-08-01" in out
        mock.assert_not_called()
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert pend and pend["action"] == "update" and pend["fields"] == {"due_on": "2026-08-01"}

    def test_two_call_flow_applies_stashed_fields(self):
        with patch.object(td.asana_client, "update_task", return_value={}) as mock:
            _update({"task_gid": "T1", "new_due_on": "2026-08-01"})
            out = _update({"confirmed": True})  # bare confirm
        mock.assert_called_once_with("T1", {"due_on": "2026-08-01"})
        assert "WRITE_CONFIRMED" in out and "updated" in out.lower()

    def test_confirm_uses_stashed_gid_not_confirm_turn_fields(self):
        with patch.object(td.asana_client, "update_task", return_value={}) as mock:
            _update({"task_gid": "T1", "new_title": "Renamed"})
            _update({"task_gid": "OTHER", "new_title": "Different", "confirmed": True})
        # the STASHED T1/Renamed applies, not the confirm-turn OTHER/Different
        mock.assert_called_once_with("T1", {"name": "Renamed"})

    def test_reassign_resolves_assignee(self):
        out = _update({"task_gid": "T1", "new_assignee_name": "Shaun"})
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert pend["fields"] == {"assignee": SHAUN_GID}
        assert "reassign to" in out.lower()

    def test_unassign_sends_null(self):
        _update({"task_gid": "T1", "unassign": True})
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert pend["fields"] == {"assignee": None}

    def test_clear_due_sends_null(self):
        _update({"task_gid": "T1", "clear_due": True})
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert pend["fields"] == {"due_on": None}

    def test_bad_due_shape_refused(self):
        out = _update({"task_gid": "T1", "new_due_on": "Friday"})
        assert "WRITE_BLOCKED" in out and "yyyy-mm-dd" in out.lower()
        assert not td.has_pending_asana_write(HARRISON, _CH)

    def test_nothing_to_change_refused(self):
        out = _update({"task_gid": "T1"})
        assert "WRITE_BLOCKED" in out and "what to change" in out.lower()
        assert not td.has_pending_asana_write(HARRISON, _CH)

    def test_status_resolves_enum_option(self):
        opts = [{"gid": "opt-inprog", "name": "In Progress", "enabled": True}]
        with patch.object(td.asana_client, "list_custom_field_enum_options", return_value=opts):
            _update({"task_gid": "T1", "new_status": "in progress"})
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert pend["custom_fields"] == {td._ASANA_STATUS_FIELD_GID: "opt-inprog"}

    def test_unknown_status_surfaced_not_stashed(self):
        opts = [{"gid": "o1", "name": "Not Started", "enabled": True}]
        with patch.object(td.asana_client, "list_custom_field_enum_options", return_value=opts):
            out = _update({"task_gid": "T1", "new_status": "Blocked"})
        assert "WRITE_BLOCKED" in out
        assert "recognized option" in out.lower() and "Not Started" in out
        assert not td.has_pending_asana_write(HARRISON, _CH)  # nothing else to change

    def test_custom_field_only_failure_reports_not_updated(self):
        opts = [{"gid": "o2", "name": "In Progress", "enabled": True}]
        with patch.object(td.asana_client, "list_custom_field_enum_options", return_value=opts), \
             patch.object(td.asana_client, "set_task_custom_fields", return_value=False), \
             patch.object(td.asana_client, "update_task") as upd:
            _update({"task_gid": "T1", "new_status": "In Progress"})
            out = _update({"confirmed": True})
        upd.assert_not_called()  # no native fields to write
        assert "WRITE_BLOCKED" in out and "not updated" in out.lower()

    def test_native_ok_custom_fail_still_confirmed_with_caveat(self):
        opts = [{"gid": "o2", "name": "High", "enabled": True}]
        with patch.object(td.asana_client, "list_custom_field_enum_options", return_value=opts), \
             patch.object(td.asana_client, "set_task_custom_fields", return_value=False), \
             patch.object(td.asana_client, "update_task", return_value={}) as upd:
            _update({"task_gid": "T1", "new_due_on": "2026-08-01", "new_priority": "High"})
            out = _update({"confirmed": True})
        upd.assert_called_once()
        assert "WRITE_CONFIRMED" in out and "couldn't set" in out.lower()

    def test_gid_not_owned_refused_for_non_founder(self):
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "Mine", "completed": False}]):
            out = _update({"task_gid": "someone-elses", "new_due_on": "2026-08-01"},
                          user=TOMMY, entity="F3E")
        assert "isn't one of your open tasks" in out.lower()
        assert not td.has_pending_asana_write(TOMMY, _CH)

    def test_lex_title_scrubbed_before_stash(self):
        # A possessive client name in a LEX rename must be scrubbed before it is stored.
        out = _update({"task_gid": "T1", "new_title": "Call John Smith's guardian"},
                      entity="LEX")
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert "John Smith" not in pend["fields"]["name"]
        assert "WRITE_BLOCKED" in out

    def test_update_failure_reports_not_updated(self):
        with patch.object(td.asana_client, "update_task",
                          side_effect=td.asana_client.AsanaClientError("boom")):
            _update({"task_gid": "T1", "new_due_on": "2026-08-01"})
            out = _update({"confirmed": True})
        assert "WRITE_BLOCKED" in out and "not updated" in out.lower()


# ─────────────────────────── asana_add_comment ───────────────────────────
class TestCommentStaged:
    def test_preview_stashes_does_not_post(self):
        with patch.object(td.asana_client, "create_task_comment") as mock:
            out = _comment({"task_gid": "T1", "text": "Following up on this"})
        assert "WRITE_BLOCKED" in out and "not added yet" in out.lower()
        mock.assert_not_called()
        assert td._peek_pending_asana(HARRISON, _CH)["action"] == "comment"

    def test_two_call_flow_posts_stashed_text(self):
        with patch.object(td.asana_client, "create_task_comment", return_value={}) as mock:
            _comment({"task_gid": "T1", "text": "Following up"})
            out = _comment({"confirmed": True})
        mock.assert_called_once_with("T1", "Following up")
        assert "WRITE_CONFIRMED" in out and "comment added" in out.lower()

    def test_empty_text_refused(self):
        out = _comment({"task_gid": "T1", "text": ""})
        assert "WRITE_BLOCKED" in out and "what the comment should say" in out.lower()
        assert not td.has_pending_asana_write(HARRISON, _CH)

    def test_gid_not_owned_refused_for_non_founder(self):
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "Mine", "completed": False}]):
            out = _comment({"task_gid": "not-mine", "text": "hi"}, user=TOMMY, entity="F3E")
        assert "isn't one of your open tasks" in out.lower()

    def test_lex_comment_scrubbed(self):
        _comment({"task_gid": "T1", "text": "Discussed Jane Doe's medication with staff"},
                 entity="LEX")
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert "Jane Doe" not in pend["text"]


# ─────────────────────────── asana_add_subtask ───────────────────────────
class TestSubtaskStaged:
    def test_preview_stashes_does_not_create(self):
        with patch.object(td.asana_client, "create_subtask") as mock:
            out = _subtask({"parent_task_gid": "P1", "title": "Draft the deck"})
        assert "WRITE_BLOCKED" in out and "not added yet" in out.lower()
        mock.assert_not_called()
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert pend["action"] == "subtask" and pend["parent_gid"] == "P1"

    def test_two_call_flow_creates_under_stashed_parent(self):
        with patch.object(td.asana_client, "create_subtask",
                          return_value={"gid": "S1", "permalink_url": "http://x"}) as mock:
            _subtask({"parent_task_gid": "P1", "title": "Draft the deck"})
            out = _subtask({"confirmed": True})
        assert mock.call_args.args[0] == "P1"
        assert mock.call_args.kwargs["name"] == "Draft the deck"
        assert "WRITE_CONFIRMED" in out and "subtask" in out.lower()

    def test_default_assignee_is_asker(self):
        _subtask({"parent_task_gid": "P1", "title": "Do it"})
        pend = td._peek_pending_asana(HARRISON, _CH)
        # Harrison's real gid from slack-to-asana.yaml
        assert pend["assignee_gid"] == "1204525779609669"

    def test_explicit_assignee_resolved(self):
        _subtask({"parent_task_gid": "P1", "title": "Do it", "assignee_name": "Shaun"})
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert pend["assignee_gid"] == SHAUN_GID

    def test_empty_title_refused(self):
        out = _subtask({"parent_task_gid": "P1", "title": ""})
        assert "WRITE_BLOCKED" in out and "what the subtask should be called" in out.lower()

    def test_parent_not_owned_refused_for_non_founder(self):
        with patch.object(td.asana_client, "get_user_tasks",
                          return_value=[{"gid": "mine", "name": "Mine", "completed": False}]):
            out = _subtask({"parent_task_gid": "not-mine", "title": "x"}, user=TOMMY, entity="F3E")
        assert "isn't one of your open tasks" in out.lower()

    def test_lex_subtask_name_scrubbed(self):
        _subtask({"parent_task_gid": "P1", "title": "Follow up with Bob Jones's family"},
                 entity="LEX")
        pend = td._peek_pending_asana(HARRISON, _CH)
        assert "Bob Jones" not in pend["name"]


# ─────────────────────── confirm interceptor wiring ───────────────────────
class TestConfirmInterceptor:
    def _confirm(self, user=HARRISON, entity="FNDR", channel=_CH, msg="yes"):
        return td.try_confirm_pending_write(
            slack_user_id=user, channel_name=channel, entity=entity, message=msg,
        )

    def test_bare_yes_fires_pending_update(self):
        with patch.object(td.asana_client, "update_task", return_value={}) as mock:
            _update({"task_gid": "T1", "new_due_on": "2026-08-01"})
            reply = self._confirm()
        mock.assert_called_once_with("T1", {"due_on": "2026-08-01"})
        assert reply and "updated" in reply.lower()
        assert "WRITE_CONFIRMED" not in reply  # sentinel stripped for the user

    def test_bare_yes_fires_pending_comment(self):
        with patch.object(td.asana_client, "create_task_comment", return_value={}) as mock:
            _comment({"task_gid": "T1", "text": "hi"})
            reply = self._confirm()
        mock.assert_called_once_with("T1", "hi")
        assert reply and "comment added" in reply.lower()

    def test_bare_yes_fires_pending_subtask(self):
        with patch.object(td.asana_client, "create_subtask", return_value={"gid": "S1"}) as mock:
            _subtask({"parent_task_gid": "P1", "title": "step 1"})
            reply = self._confirm()
        mock.assert_called_once()
        assert reply and "subtask" in reply.lower()

    def test_negative_cancels_update(self):
        with patch.object(td.asana_client, "update_task") as mock:
            _update({"task_gid": "T1", "new_due_on": "2026-08-01"})
            reply = self._confirm(msg="no")
        mock.assert_not_called()
        assert reply and "won't" in reply.lower()
        assert not td.has_pending_asana_write(HARRISON, _CH)

    def test_content_message_defers_to_model(self):
        _update({"task_gid": "T1", "new_due_on": "2026-08-01"})
        reply = self._confirm(msg="actually what's due tomorrow?")
        assert reply is None  # defer to the model; non-destructive pending left intact


# ─────────────────────────── registration/exposure ───────────────────────────
class TestRegistration:
    NEW = ["asana_update_task", "asana_add_comment", "asana_add_subtask"]

    def test_in_tool_functions(self):
        for n in self.NEW:
            assert n in td._TOOL_FUNCTIONS

    def test_in_global_core(self):
        for n in self.NEW:
            assert n in td._GLOBAL_CORE_TOOLS

    def test_has_timeout(self):
        for n in self.NEW:
            assert n in td._TOOL_TIMEOUTS

    def test_in_tool_definitions(self):
        names = {t["name"] for t in td.TOOL_DEFINITIONS}
        for n in self.NEW:
            assert n in names

    def test_exposed_in_normal_entity_channel(self):
        names = {t["name"] for t in td.tools_for_entity("F3E")}
        for n in self.NEW:
            assert n in names

    def test_exposed_in_lex_channel(self):
        names = {t["name"] for t in td.tools_for_entity("LEX")}
        for n in self.NEW:
            assert n in names

    def test_in_contract_write_tools(self):
        from cora import claude_client
        for n in self.NEW:
            assert n in claude_client._CONTRACT_WRITE_TOOLS


# ─────────────────────────── intent detector (app.py) ───────────────────────────
class TestIntentDetector:
    @pytest.mark.parametrize("msg,expect", [
        ("reassign the deck task to Hannah", "asana_update_task"),
        ("change the due date of the vendor task to Friday", "asana_update_task"),
        ("push the deadline on my onboarding task", "asana_update_task"),
        ("set the priority on the launch task to high", "asana_update_task"),
        ("rename the follow-up task", "asana_update_task"),
        ("comment on the deck task that it's blocked", "asana_add_comment"),
        ("add a note to the vendor task saying done", "asana_add_comment"),
        ("add a subtask to the launch task", "asana_add_subtask"),
        ("break the launch task into subtasks", "asana_add_subtask"),
        ("delete the vendor task", "asana_delete_task"),
        ("create a task to call the vendor", "asana_create_task"),
    ])
    def test_true_positives(self, msg, expect):
        assert cora_app._asana_destructive_intent(msg) == expect

    @pytest.mark.parametrize("msg", [
        "what tasks do I have?",
        "who is assigned to the deck task?",
        "show me the subtasks of the launch task",
        "what's the status of the vendor task?",
        "move the meeting to Friday",          # calendar, no task referent
        "reassign this to me later maybe",     # no task referent -> gated out
        "can you change the priority?",         # interrogative
    ])
    def test_true_negatives(self, msg):
        assert cora_app._asana_destructive_intent(msg) is None


# ─────────────────────── prompt coverage (drift guard) ───────────────────────
class TestPromptCoverage:
    """Every entity prompt must carry the 'Managing tasks' mandatory-tool-call section
    (the tools are global-core, so every channel needs the instruction)."""

    def _prompts(self):
        files = sorted(_PROMPTS_DIR.glob("*.md"))
        assert len(files) == 17, f"expected 17 entity prompts, found {len(files)}"
        return files

    def test_all_prompts_have_managing_tasks_section(self):
        for f in self._prompts():
            text = f.read_text(encoding="utf-8")
            assert "## Managing tasks (mandatory tool call, staged write)" in text, f.name

    def test_all_prompts_name_the_three_edit_tools(self):
        for f in self._prompts():
            text = f.read_text(encoding="utf-8")
            for tool in ("asana_update_task", "asana_add_comment", "asana_add_subtask"):
                assert tool in text, f"{tool} missing from {f.name}"
