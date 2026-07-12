"""F-23 Slice 2: the app-side Asana task-action intent detector that forces the
tool (via tool_choice) so a TOOL preview + pending entry is produced, not a
haiku-fabricated one."""

import pytest

import cora.app as app


class TestAsanaDestructiveIntent:
    @pytest.mark.parametrize("msg", [
        "delete the SMOKE F23 CLEAN task",
        "please delete that task",
        "remove the onboarding task",
        "trash this task",
        "get rid of the duplicate task",
    ])
    def test_delete(self, msg):
        assert app._asana_destructive_intent(msg) == "asana_delete_task"

    @pytest.mark.parametrize("msg", [
        "mark the Anaheim task done",
        "complete my onboarding task",
        "finish the deck task",
        "mark that task as complete",
        "close out the review task",
    ])
    def test_complete(self, msg):
        assert app._asana_destructive_intent(msg) == "asana_complete_task"

    @pytest.mark.parametrize("msg", [
        "create a task to follow up with Bob",
        "make a task for the deck review",
        "set up a task to call the vendor",
        "add a new task for Q3 planning",
    ])
    def test_create(self, msg):
        assert app._asana_destructive_intent(msg) == "asana_create_task"

    @pytest.mark.parametrize("msg", [
        "did we delete that task?",
        "which tasks are complete?",
        "who created this task",
        "is that task done yet",
        "what task did Hannah finish",
    ])
    def test_interrogatives_excluded(self, msg):
        assert app._asana_destructive_intent(msg) is None

    @pytest.mark.parametrize("msg", [
        "delete the old draft",           # not a task
        "create a report",                # not a task
        "cancel the meeting",             # cancel is not a task-delete verb
        "add a comment to the task",      # 'add' near 'task' but not a create-a-task
        "remove Bob from the channel",    # not a task
    ])
    def test_non_task_or_unrelated_excluded(self, msg):
        assert app._asana_destructive_intent(msg) is None

    @pytest.mark.parametrize("msg", [
        "yes, delete it permanently",     # confirm turn -> interceptor / Slice 3, not force
        "yes",
        "confirm",
        "go ahead",
    ])
    def test_confirm_phrases_not_detected(self, msg):
        assert app._asana_destructive_intent(msg) is None

    def test_empty_and_none(self):
        assert app._asana_destructive_intent("") is None
        assert app._asana_destructive_intent(None) is None
