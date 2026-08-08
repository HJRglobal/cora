"""v2 S8: preview-content nits.

One real fix and two VERIFY-FIRST corrections, all pinned so they cannot
regress in either direction.

FIXED -- cq-8e2761423833 / cq-8e2771423833 (LOW): _resolve_asker_task's
unrestricted branch (the portfolio founder, and any FNDR/HJRG channel) skips the
ownership lookup by design, which also meant it never learned the task's NAME.
A gid-only ask therefore previewed the raw gid back at the user, which is not
reviewable -- the entire point of a preview is seeing WHAT is about to change.

PREMISE OVERTURNED -- cq-2778868827ab: the DW preview's quota and cost lines are
already unconditional on both surfaces (verified live).

PREMISE OVERTURNED -- cq-2c5d864691fb: delegated_level() already reads
os.environ per call, so the TRIAL MODE label is never a process snapshot
(verified live by flipping the variable between calls). The residual -- editing
the .env FILE does not reach a running bot -- is the documented restart
requirement for the kill switch, not a mislabel.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

from cora import delegated_work  # noqa: E402
from cora.tools import asana_client, tool_dispatch as td  # noqa: E402

FOUNDER = td._FOUNDER_SLACK_ID
GID = "1215070431336670"


# ── cq-8e2771423833: a gid-only ask must preview a real task NAME ──────────


class TestGidOnlyPreviewResolvesTheName:
    def test_founder_gid_only_ask_shows_the_task_name(self):
        with patch.object(asana_client, "get_task_name", return_value="Ship the Q3 deck") as g:
            gid, label, err = td._resolve_asker_task(FOUNDER, GID, "", "FNDR")
        g.assert_called_once_with(GID)
        assert err is None
        assert gid == GID
        assert label == "Ship the Q3 deck"

    def test_fndr_channel_gid_only_ask_shows_the_task_name(self):
        """The unrestricted branch is entered by CHANNEL as well as by user."""
        with patch.object(asana_client, "get_task_name", return_value="Renew the lease"):
            _gid, label, err = td._resolve_asker_task("U0SOMEONE", GID, "", "HJRG")
        assert err is None and label == "Renew the lease"

    def test_an_explicit_task_name_is_never_overridden_by_a_lookup(self):
        with patch.object(asana_client, "get_task_name") as g:
            _gid, label, _err = td._resolve_asker_task(FOUNDER, GID, "The name I gave", "FNDR")
        g.assert_not_called()
        assert label == "The name I gave"

    def test_lookup_failure_falls_back_to_the_gid_and_still_allows_the_write(self):
        """Fail-soft: a name is presentation only and must never block a write
        this asker is already authorized to make."""
        with patch.object(asana_client, "get_task_name",
                          side_effect=RuntimeError("asana down")):
            gid, label, err = td._resolve_asker_task(FOUNDER, GID, "", "FNDR")
        assert err is None
        assert gid == GID
        assert label == GID

    def test_lookup_returning_none_falls_back_to_the_gid(self):
        with patch.object(asana_client, "get_task_name", return_value=None):
            _gid, label, err = td._resolve_asker_task(FOUNDER, GID, "", "FNDR")
        assert err is None and label == GID

    def test_the_preview_no_longer_echoes_a_bare_gid(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
        td._PENDING_ASANA_WRITES.clear()
        with patch.object(asana_client, "get_task_name", return_value="Ship the Q3 deck"):
            out = td._tool_asana_complete_task(FOUNDER, "FNDR", {
                "_channel_name": "hjrg-leadership", "task_gid": GID,
            })
        td._PENDING_ASANA_WRITES.clear()
        assert '"Ship the Q3 deck"' in out
        assert GID not in out, "the preview must show the name, not the raw gid"

    def test_the_restricted_path_is_unchanged(self):
        """A non-founder in a normal channel still goes through the ownership
        lookup and must NOT gain a name-resolution side channel."""
        with patch.object(td, "_load_slack_asana_map",
                          return_value={"U0X": {"asana_user_gid": "999"}}), \
             patch.object(asana_client, "get_user_tasks",
                          return_value=[{"gid": GID, "name": "Their task", "completed": False}]), \
             patch.object(asana_client, "get_task_name") as g:
            _gid, label, err = td._resolve_asker_task("U0X", GID, "", "F3E")
        g.assert_not_called()
        assert err is None and label == "Their task"


class TestGetTaskName:
    def test_blank_gid_returns_none_without_a_call(self):
        assert asana_client.get_task_name("") is None
        assert asana_client.get_task_name("   ") is None

    def test_it_never_raises(self):
        """Contract: presentation-only, so it swallows everything."""
        src = inspect.getsource(asana_client.get_task_name)
        assert "except Exception" in src
        assert "return None" in src


# ── cq-2778868827ab: premise overturned, pinned so it stays true ───────────


class TestDelegatedPreviewAlwaysCarriesQuotaAndCost:
    _ENTRY = {"archetype": "research", "brief": "map the OSN vendor list",
              "deliverable": "md", "entity": "FNDR"}

    @pytest.mark.parametrize("channel", ["dm", "hjrg-leadership", ""])
    def test_quota_and_cost_render_on_every_surface(self, channel):
        txt = td._delegated_preview_text(
            self._ENTRY, "Quota: 2 of 3 jobs left today.", channel)
        assert "Quota: 2 of 3 jobs left today." in txt
        assert "Cost: capped at $" in txt
        assert "Turnaround: async" in txt

    def test_the_brief_still_renders_verbatim(self):
        """Design 3: paraphrase would defeat the drift check."""
        txt = td._delegated_preview_text(self._ENTRY, "Quota: 1 left.", "dm")
        assert "map the OSN vendor list" in txt

    def test_the_preview_posts_verbatim_not_via_model_paraphrase(self):
        from cora import claude_client
        assert "cora_delegate_work" in claude_client._CONTRACT_WRITE_TOOLS


# ── cq-2c5d864691fb: premise overturned, pinned so it stays true ───────────


class TestTrialModeLabelReadsEnvAtCallTime:
    @pytest.mark.parametrize("value,expect_trial", [
        ("log", True), ("live", False), ("off", False), ("nonsense", False), ("", False),
    ])
    def test_level_tracks_the_current_environment(self, monkeypatch, value, expect_trial):
        monkeypatch.setenv("CORA_DELEGATED_WORK", value)
        assert (delegated_work.delegated_level() == "log") is expect_trial

    def test_flipping_between_calls_changes_the_label_immediately(self, monkeypatch):
        """The specific claim under test: no process-level snapshot exists."""
        monkeypatch.setenv("CORA_DELEGATED_WORK", "log")
        assert delegated_work.delegated_level() == "log"
        monkeypatch.setenv("CORA_DELEGATED_WORK", "live")
        assert delegated_work.delegated_level() == "live"
        monkeypatch.setenv("CORA_DELEGATED_WORK", "log")
        assert delegated_work.delegated_level() == "log"

    def test_the_level_is_read_inside_the_function_not_at_import(self):
        src = inspect.getsource(delegated_work.delegated_level)
        assert 'os.environ.get("CORA_DELEGATED_WORK"' in src

    def test_no_module_level_snapshot_of_the_flag_exists(self):
        """Drift guard: a module-level `_LEVEL = delegated_level()` would
        reintroduce exactly the snapshot this ticket suspected.

        Walked as an AST, not by line: a text scan for "delegated_level()"
        matches the function's own `def` line and its docstring, the same
        pin-matches-the-thing-it-describes trap S4's guard hit."""
        import ast
        src = (_REPO_ROOT / "src" / "cora" / "delegated_work.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        for node in tree.body:  # module level ONLY, never inside a def
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                for sub in ast.walk(node.value):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                            and sub.func.id in ("delegated_level", "_int_env")):
                        offenders.append((node.lineno, sub.func.id))
        assert not offenders, f"module-level snapshot of a rollout flag: {offenders}"
