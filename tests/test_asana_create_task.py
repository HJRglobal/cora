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
    # Patch project_resolver at its source module so the lazy import inside the handler
    # picks up the stub (preserving the original expectation: project_gid=None when not given).
    with patch("cora.tools.project_resolver.resolve_project", return_value=None), \
         patch.object(td.asana_client, "create_task", return_value=fake_created) as mock:
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
    # Defaulted to Harrison (the asker) -- his Asana GID is in the real YAML
    assert call_kwargs["assignee_gid"] == "1204525779609669"
    # Resolver returned None -> project_gid stays None
    assert call_kwargs["project_gid"] is None
    assert call_kwargs["due_on"] is None
    # The formatted response surfaces the task to the LLM
    assert "WRITE_CONFIRMED" in result
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
    with patch.object(td.asana_client, "create_task", return_value=fake_created) as mock, \
         patch.object(td.asana_client, "get_project_tasks", return_value=[]):
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
    assert "WRITE_CONFIRMED" in result


# ---------------------------------------------------------------------------
# WS3: cross-entity-safe routing + LEX PHI scrub + exact-name dedup
# ---------------------------------------------------------------------------

_F3E_CATCH_ALL = "1215470928454227"   # [F3E] Operations — General
_LEX_CATCH_ALL = "1215470944114390"   # [LEX-LLC] Operations — General


def test_explicit_cross_entity_project_is_dropped_and_rerouted():
    """A LEX project GID passed from an F3E channel must NOT be used; the task is
    re-routed to an F3E project and the re-route is surfaced."""
    created = {"gid": "1", "permalink_url": "http://x", "projects": []}
    with patch.object(td.asana_client, "create_task", return_value=created) as mock, \
         patch.object(td.asana_client, "get_project_tasks", return_value=[]):
        result = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK,
            entity="F3E",
            _input={"title": "Plan something", "confirmed": True, "project_gid": _LEX_CATCH_ALL},
        )
    assert mock.call_args.kwargs["project_gid"] != _LEX_CATCH_ALL
    assert "belongs to" in result.lower()


def test_no_orphan_routes_to_entity_catch_all():
    """When the resolver finds nothing, an entity-scoped channel routes to that
    entity's catch-all -- never a silent My-Tasks orphan."""
    created = {"gid": "1", "permalink_url": "http://x", "projects": []}
    with patch("cora.tools.project_resolver.resolve_project", return_value=None), \
         patch.object(td.asana_client, "create_task", return_value=created) as mock, \
         patch.object(td.asana_client, "get_project_tasks", return_value=[]):
        td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK,
            entity="F3E",
            _input={"title": "Some F3E task", "confirmed": True},
        )
    assert mock.call_args.kwargs["project_gid"] == _F3E_CATCH_ALL


def test_lex_channel_scrubs_phi_and_routes_lex():
    """A task created from a Lexington channel is PHI-scrubbed and lands in a LEX
    project, and the scrub is surfaced."""
    created = {"gid": "1", "permalink_url": "http://x", "projects": []}
    with patch.object(td.org_roles, "all_roles", return_value=[]), \
         patch("cora.phi_guard.scrub_lex_phi", side_effect=lambda t, allowed_names=None: (t or "").replace("John Doe", "[client]")), \
         patch.object(td.asana_client, "create_task", return_value=created) as mock, \
         patch.object(td.asana_client, "get_project_tasks", return_value=[]):
        result = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK,
            entity="LEX-LLC",
            _input={"title": "Follow up with John Doe re billing", "confirmed": True},
        )
    assert mock.call_args.kwargs["name"] == "Follow up with [client] re billing"
    from cora.tools import project_resolver as pr
    owners = pr.project_owner_entities(mock.call_args.kwargs["project_gid"])
    assert any(str(o).upper().startswith("LEX") for o in owners)
    assert "phi-scrubbed" in result.lower()


def test_dedup_refuses_then_force_creates():
    """An exact-name open task in the target project blocks the create (surfaced);
    force_duplicate=true overrides."""
    existing = [{"gid": "OLD", "name": "Send the deck", "permalink_url": "http://old"}]
    created = {"gid": "NEW", "permalink_url": "http://new", "projects": []}
    with patch.object(td.asana_client, "get_project_tasks", return_value=existing), \
         patch.object(td.asana_client, "create_task", return_value=created) as mock:
        blocked = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK, entity="F3E",
            _input={"title": "Send the deck", "confirmed": True},
        )
        assert "already exists" in blocked.lower()
        assert "force_duplicate" in blocked
        mock.assert_not_called()

        td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK, entity="F3E",
            _input={"title": "Send the deck", "confirmed": True, "force_duplicate": True},
        )
        mock.assert_called_once()


def test_unconfirmed_preview_surfaces_lex_scrub():
    """The unconfirmed refusal includes the resolved preview + the LEX scrub note
    so the user sees it BEFORE approving (the WS3 invariant clamp)."""
    with patch.object(td.org_roles, "all_roles", return_value=[]), \
         patch("cora.phi_guard.scrub_lex_phi", side_effect=lambda t, allowed_names=None: (t or "").replace("John Doe", "[client]")):
        out = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK,
            entity="LEX-LLC",
            _input={"title": "Call John Doe"},
        )
    assert "refused" in out.lower()
    assert "[client]" in out          # scrubbed title shown in preview
    assert "phi-scrubbed" in out.lower()


def test_lex_unmapped_project_gid_fails_closed():
    """Review fix #1: a LEX-channel task with an UNMAPPED explicit project_gid must
    be dropped to a LEX project, never honored (fail-CLOSED, not fail-OPEN)."""
    created = {"gid": "1", "permalink_url": "http://x", "projects": []}
    with patch.object(td.org_roles, "all_roles", return_value=[]), \
         patch("cora.phi_guard.scrub_lex_phi", side_effect=lambda t, allowed_names=None: t), \
         patch.object(td.asana_client, "create_task", return_value=created) as mock, \
         patch.object(td.asana_client, "get_project_tasks", return_value=[]):
        out = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK, entity="LEX-LLC",
            _input={"title": "Follow up", "confirmed": True, "project_gid": "999999999999"},
        )
    used = mock.call_args.kwargs["project_gid"]
    assert used != "999999999999"
    from cora.tools import project_resolver as pr
    assert used and any(str(o).upper().startswith("LEX") for o in pr.project_owner_entities(used))
    assert "unverified" in out.lower()


def test_lex_scrub_failure_fails_closed():
    """Review fix #5: if PHI scrub raises, refuse and NEVER create the task."""
    with patch.object(td.org_roles, "all_roles", return_value=[]), \
         patch("cora.phi_guard.scrub_lex_phi", side_effect=RuntimeError("scrub boom")), \
         patch.object(td.asana_client, "create_task") as mock_create, \
         patch.object(td.asana_client, "get_project_tasks", return_value=[]):
        out = td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK, entity="LEX-LLC",
            _input={"title": "Follow up with John Doe re billing", "confirmed": True},
        )
    assert "couldn't safely prepare" in out.lower()
    mock_create.assert_not_called()   # raw PHI title must NEVER reach Asana


def test_dedup_fails_open_on_read_error():
    """Review fix #7: a dedup read error must NOT block a legitimate create."""
    created = {"gid": "NEW", "permalink_url": "http://new", "projects": []}
    with patch.object(td.asana_client, "get_project_tasks", side_effect=Exception("read failed")), \
         patch.object(td.asana_client, "create_task", return_value=created) as mock:
        td._tool_asana_create_task(
            slack_user_id=HARRISON_SLACK, entity="F3E",
            _input={"title": "Send the deck", "confirmed": True},
        )
    mock.assert_called_once()


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
