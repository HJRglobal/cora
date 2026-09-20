"""scripts/asana_visibility_diff.py is READ-ONLY by construction (S-B; D-290: a
--dry-run flag is a claim about every write site -- proven here, not asserted).

  * two fake token views -> the set difference is EXACTLY the injected delta
  * a full main() run against a fake httpx.Client issues ZERO non-GET calls
    (patched at every module the script reaches: the script AND asana_client)
  * source-scan pin: the script never imports or names any of asana_client's nine
    write functions and contains no non-GET verb call
  * tokens never reach stdout/stderr; a missing ASANA_PAT_CORA hard-fails BEFORE
    any HTTP call (never a fallback to Harrison's token)
  * LEX-project and PHI-shaped task names are withheld at the read boundary
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))
_SCRIPT = _REPO_ROOT / "scripts" / "asana_visibility_diff.py"

_H = "harrison-token-fixture"
_C = "cora-token-fixture"

_WRITE_FUNCTIONS = (
    "create_task", "create_subtask", "complete_task", "update_task", "delete_task",
    "add_task_followers", "create_task_comment", "set_task_custom_fields",
    "add_project_custom_field_setting",
)


def _load():
    # The script's import-time load_dotenv(override=True) writes straight into
    # os.environ; save/restore so the live .env can never leak into a test.
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location("asana_visibility_diff", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.delenv("CORA_ASANA_IDENTITY", raising=False)
    monkeypatch.setenv("ASANA_PAT", _H)
    monkeypatch.setenv("ASANA_PAT_CORA", _C)


def _resp(data, status=200, next_page=None):
    r = MagicMock()
    r.status_code = status
    r.text = ""
    r.json.return_value = {"data": data, "next_page": next_page}
    return r


def _fake_client(router):
    """A fake httpx.Client context manager whose .get routes on URL. Every other
    verb is a plain MagicMock so a stray write would be COUNTED, not crash."""
    client = MagicMock()

    def _get(url, params=None, headers=None, **_kw):
        return router(url, params or {}, headers or {})

    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return client, MagicMock(return_value=cm)


def _default_router(seen_auth: list[str]):
    def route(url, params, headers):
        seen_auth.append(headers.get("Authorization", ""))
        if url.endswith("/users/me"):
            who = "Cora Bot" if headers.get("Authorization") == f"Bearer {_C}" else "Harrison Fixture"
            return _resp({"name": who, "gid": "42" if who == "Cora Bot" else "7"})
        if "/projects/" in url and url.endswith("/tasks"):
            return _resp([{"gid": "P-T1", "name": "Order more pens"}])
        if "/projects/" in url:
            return _resp({"name": "[FIX] Operations -- General"})
        if url.endswith("/tasks"):
            return _resp([{"gid": "U-T1", "name": "Send the Q3 wholesale deck"}])
        return _resp({}, status=404)
    return route


# --- the diff is exactly the injected delta ---

class TestDiffIsExact:
    def test_set_difference_matches_injected_delta(self, mod):
        h = mod.View(identity="harrison", me={"name": "H", "gid": "7"})
        c = mod.View(identity="cora", me={"name": "Cora Bot", "gid": "42"})
        h.plate["U1"] = {"tasks": [{"gid": "A", "name": "a", "lex": False},
                                    {"gid": "B", "name": "b", "lex": False},
                                    {"gid": "C", "name": "c", "lex": False}]}
        c.plate["U1"] = {"tasks": [{"gid": "B", "name": "b", "lex": False},
                                    {"gid": "C", "name": "c", "lex": False},
                                    {"gid": "D", "name": "d", "lex": False}]}
        h.hygiene["U1"] = {"tasks": []}
        c.hygiene["U1"] = {"tasks": []}
        h.catchall["P1"] = {"tasks": [{"gid": "X", "name": "x", "lex": False}]}
        c.catchall["P1"] = {"error": 403}
        d = mod.diff_views(h, c)
        plate = d["plate"]["U1"]
        assert plate["harrison_count"] == 3 and plate["cora_count"] == 3
        assert plate["harrison_only"] == [("A", "a")]
        assert plate["cora_only"] == [("D", "d")]
        assert d["hygiene"]["U1"]["harrison_only"] == [] and d["hygiene"]["U1"]["cora_only"] == []
        cat = d["catchall"]["P1"]
        assert cat["harrison_count"] == 1 and cat["cora_count"] is None and cat["cora_error"] == 403
        assert cat["harrison_only"] == [] and cat["cora_only"] == []  # no diff across an error

    def test_render_names_the_project_cora_cannot_see(self, mod):
        h = mod.View(identity="harrison", me={"name": "H", "gid": "7"})
        c = mod.View(identity="cora", me={"name": "Cora Bot", "gid": "42"})
        h.catchall["P1"] = {"tasks": [{"gid": "X", "name": "x", "lex": False}]}
        c.catchall["P1"] = {"error": 403}
        d = mod.diff_views(h, c)
        out = mod.render(d, roster=[], catchalls={"P1": ["F3E"]}, project_names={"P1": "[F3E] Ops -- General"})
        assert "ADD THE cora@ SEAT TO: [F3E] [F3E] Ops -- General [P1] (cora GET -> 403)" in out
        assert "Cora Bot . 42" in out and "H . 7" in out
        assert "READ-ONLY" in out

    def test_render_lex_catchall_is_aggregate_only(self, mod):
        h = mod.View(identity="harrison", me={})
        c = mod.View(identity="cora", me={})
        h.catchall["L1"] = {"tasks": [{"gid": "1", "name": "ZZ-synthetic-lex-task", "lex": True}]}
        c.catchall["L1"] = {"tasks": []}
        d = mod.diff_views(h, c)
        out = mod.render(d, roster=[], catchalls={"L1": ["LEX", "LEX-LLC"]}, project_names={"L1": "[LEX-LLC] Ops"})
        assert "ZZ-synthetic-lex-task" not in out
        assert "LEX aggregate-only: harrison-only 1 / cora-only 0" in out

    def test_render_caps_long_lists(self, mod):
        # PIN CHANGED (D-051 B-asana-identity-2): names are rendered from the
        # exhaustive VISIBILITY diff, not the 25/50 windows (which are counts only).
        h = mod.View(identity="harrison", me={})
        c = mod.View(identity="cora", me={})
        h.visibility["U1"] = {"tasks": [{"gid": str(i), "name": f"t{i:03d}", "lex": False} for i in range(50)]}
        c.visibility["U1"] = {"tasks": []}
        h.plate["U1"] = c.plate["U1"] = {"tasks": []}
        h.hygiene["U1"] = c.hygiene["U1"] = {"tasks": []}
        d = mod.diff_views(h, c)
        out = mod.render(d, roster=[("Someone", "U1")], catchalls={}, project_names={}, max_list=5)
        assert "... and 45 more" in out


# --- D-051 B-asana-identity-1: only 403/404 is a membership signal ---

class TestActionListIsMembershipOnly:
    @pytest.mark.parametrize("code", [429, 500, 503, 599, "absent"])
    def test_transient_cora_error_is_retry_not_add_seat(self, mod, code):
        """The review's failing input: harrison sees P1, cora GET -> 429 (or the
        getter's synthetic 599). The old render told Harrison to ADD the seat."""
        h = mod.View(identity="harrison", me={"name": "H", "gid": "7"})
        c = mod.View(identity="cora", me={"name": "Cora Bot", "gid": "42"})
        h.catchall["P1"] = {"tasks": [{"gid": "X", "name": "x", "lex": False}]}
        c.catchall["P1"] = {"error": code}
        d = mod.diff_views(h, c)
        out = mod.render(d, roster=[], catchalls={"P1": ["F3E"]}, project_names={"P1": "[F3E] Ops"})
        assert "ADD THE cora@ SEAT TO" not in out
        assert f"RETRY: [F3E] [F3E] Ops [P1] (cora GET -> {code} -- transient/error, NOT a membership signal)" in out
        # the action list does not claim every project is reachable while a retry is pending
        assert "no membership gap confirmed -- 1 project(s) pending RETRY" in out
        assert "the cora@ seat reaches every catch-all project" not in out

    @pytest.mark.parametrize("code", [403, 404])
    def test_membership_gap_codes_still_produce_the_action_line(self, mod, code):
        h = mod.View(identity="harrison", me={})
        c = mod.View(identity="cora", me={})
        h.catchall["P1"] = {"tasks": [{"gid": "X", "name": "x", "lex": False}]}
        c.catchall["P1"] = {"error": code}
        d = mod.diff_views(h, c)
        out = mod.render(d, roster=[], catchalls={"P1": ["F3E"]}, project_names={"P1": "[F3E] Ops"})
        assert f"ADD THE cora@ SEAT TO: [F3E] [F3E] Ops [P1] (cora GET -> {code})" in out
        assert "RETRY: [F3E]" not in out
        assert mod._MEMBERSHIP_GAP_CODES == frozenset({403, 404})

    def test_getter_network_error_is_599_and_lands_in_retry(self, mod):
        """make_getter's synthetic 599 (httpx.RequestError) must never read as a gap."""
        import httpx

        def boom(*_a, **_k):
            raise httpx.ConnectError("fixture")

        cm = MagicMock()
        cm.__enter__.return_value = MagicMock(get=boom)
        cm.__exit__.return_value = False
        with patch.object(mod.httpx, "Client", MagicMock(return_value=cm)):
            get = mod.make_getter("t")
            res = mod.fetch_project_tasks(get, "P1")
        assert res == {"error": 599}
        assert 599 not in mod._MEMBERSHIP_GAP_CODES


# --- D-051 B-asana-identity-2: the visibility diff is exhaustive, the windows are counts ---

def _paged_getter(gids: list[str], hidden: frozenset[str] = frozenset()):
    """A fake Asana that honours limit/offset over an ordered task list, with
    *hidden* gids invisible to this token (the cora@ seat not in their project)."""
    visible = [{"gid": g, "name": g} for g in gids if g not in hidden]
    pages: list[dict] = []

    def get(path, params):
        if path == "/users/me":
            return 200, {"data": {"name": "who", "gid": "1"}}
        params = params or {}
        limit = int(params["limit"])
        start = int(params.get("offset") or 0)
        pages.append(dict(params))
        page = visible[start:start + limit]
        nxt = {"offset": str(start + limit)} if start + limit < len(visible) else None
        return 200, {"data": page, "next_page": nxt}

    get.pages = pages  # type: ignore[attr-defined]
    return get


class TestVisibilityDiffIsExhaustive:
    def test_hidden_task_inside_window_is_not_reported_as_a_shift(self, mod):
        """The review's failing input: 30 tasks T01..T30, cora cannot see T03.
        The 25-window diff said cora-only [T26] -- but Harrison sees T26."""
        gids = [f"T{i:02d}" for i in range(1, 31)]
        roster = [("Harrison", "7")]
        h = mod.collect_view("harrison", _paged_getter(gids), roster, {})
        c = mod.collect_view("cora", _paged_getter(gids, hidden=frozenset({"T03"})), roster, {})
        d = mod.diff_views(h, c)
        # the window data still shows the shift (that IS what the plate would show) ...
        assert d["plate"]["7"]["cora_only"] == [("T26", "T26")]
        # ... but the visibility diff is the true comparison
        vis = d["visibility"]["7"]
        assert vis["harrison_count"] == 30 and vis["cora_count"] == 29
        assert vis["harrison_only"] == [("T03", "T03")] and vis["cora_only"] == []
        assert vis["harrison_capped"] is False and vis["cora_capped"] is False
        out = mod.render(d, roster=roster, catchalls={}, project_names={})
        assert "T26" not in out, "a task that merely shifted into Cora's window must never be named"
        assert "harrison-only (1) -- Cora cannot see:" in out and "- T03  [T03]" in out
        assert "COUNTS ONLY, not a visibility signal" in out
        assert "window contents differ: 1 / 1 -- see the visibility diff" in out

    def test_hidden_task_beyond_both_windows_is_surfaced(self, mod):
        """Invisible task at position 60 of 70 -- past BOTH the 25 plate window and
        the 50 hygiene window: every window diff is EMPTY and the loss was never
        reported. Harrison (100+ open tasks) is the user most affected."""
        gids = [f"T{i:02d}" for i in range(1, 71)]
        roster = [("Harrison", "7")]
        h = mod.collect_view("harrison", _paged_getter(gids), roster, {})
        c = mod.collect_view("cora", _paged_getter(gids, hidden=frozenset({"T60"})), roster, {})
        d = mod.diff_views(h, c)
        for window in ("plate", "hygiene"):
            assert d[window]["7"]["harrison_only"] == [] and d[window]["7"]["cora_only"] == [], window
        assert d["visibility"]["7"]["harrison_only"] == [("T60", "T60")]
        assert d["visibility"]["7"]["harrison_count"] == 70 and d["visibility"]["7"]["cora_count"] == 69
        out = mod.render(d, roster=roster, catchalls={}, project_names={})
        assert "- T60  [T60]" in out and "Cora cannot see" in out

    def test_visibility_walk_paginates_at_api_max_and_flags_the_cap(self, mod):
        from cora.tools import asana_client as ac
        n = mod._VISIBILITY_MAX_TASKS + 5
        gids = [f"T{i:04d}" for i in range(n)]
        get = _paged_getter(gids)
        res = mod.fetch_user_tasks(get, "7", mod._VISIBILITY_MAX_TASKS)
        assert len(res["tasks"]) == mod._VISIBILITY_MAX_TASKS and res["capped"] is True
        assert all(p["limit"] <= ac._API_MAX_LIMIT for p in get.pages)
        assert get.pages[0]["limit"] == ac._API_MAX_LIMIT and "offset" not in get.pages[0]
        assert len(get.pages) == mod._VISIBILITY_MAX_TASKS // ac._API_MAX_LIMIT
        h = mod.View(identity="harrison", me={})
        c = mod.View(identity="cora", me={})
        h.visibility["7"] = res
        c.visibility["7"] = res
        out = mod.render(mod.diff_views(h, c), roster=[("Harrison", "7")], catchalls={}, project_names={})
        assert f"CAP {mod._VISIBILITY_MAX_TASKS} REACHED" in out

    def test_windows_are_still_fetched_byte_for_byte(self, mod):
        """The 25/50 windows remain real GETs with the live limits (they are what the
        plate / nudge would show); the visibility walk is a THIRD query."""
        gids = [f"T{i:02d}" for i in range(1, 61)]
        get = _paged_getter(gids)
        mod.collect_view("harrison", get, [("Harrison", "7")], {})
        first_page_limits = [p["limit"] for p in get.pages if "offset" not in p]
        assert first_page_limits == [25, 50, 100]


# --- read-boundary withholding (D-145) ---

class TestNamesWithheld:
    def test_lex_project_task_name_is_withheld(self, mod):
        row = mod._task_row({"gid": "1", "name": "ZZ-synthetic", "projects": [{"name": "[LEX-LLC] Ops"}]})
        assert row["lex"] is True and row["name"] == mod._WITHHELD

    def test_lex_via_memberships_is_withheld(self, mod):
        row = mod._task_row({"gid": "1", "name": "ZZ-synthetic",
                             "memberships": [{"project": {"name": "[LTS] Something"}}]})
        assert row["lex"] is True and row["name"] == mod._WITHHELD

    def test_phi_shaped_name_is_withheld_even_outside_lex(self, mod):
        row = mod._task_row({"gid": "1", "name": "Update treatment plan for member X", "projects": [{"name": "[F3E] Ops"}]})
        assert row["lex"] is False and row["name"] == mod._WITHHELD

    def test_ordinary_name_survives(self, mod):
        row = mod._task_row({"gid": "1", "name": "Order more pens", "projects": [{"name": "[F3E] Ops"}]})
        assert row == {"gid": "1", "name": "Order more pens", "lex": False}


# --- zero non-GET calls through main() ---

def _run_main(mod, argv, router=None, capsys=None):
    seen_auth: list[str] = []
    client, factory = _fake_client(router or _default_router(seen_auth))
    from cora.tools import asana_client
    with patch.object(mod.httpx, "Client", factory), patch.object(asana_client.httpx, "Client", factory):
        rc = mod.main(argv)
    return rc, client, factory, seen_auth


class TestZeroNonGet:
    def test_main_issues_only_get_calls(self, mod, capsys):
        rc, client, factory, seen_auth = _run_main(mod, [])
        assert rc == 0
        assert factory.called
        verbs = {c[0] for c in client.method_calls}
        assert verbs == {"get"}, verbs
        for verb in ("post", "put", "delete", "patch", "request", "stream", "send"):
            assert getattr(client, verb).call_count == 0, verb
        # both identities were exercised, each with its own token
        assert f"Bearer {_H}" in seen_auth and f"Bearer {_C}" in seen_auth

    def test_dry_run_flag_is_accepted_and_still_only_get(self, mod):
        rc, client, _f, _s = _run_main(mod, ["--dry-run"])
        assert rc == 0
        assert {c[0] for c in client.method_calls} == {"get"}

    def test_users_only_probes_only_users_me(self, mod):
        rc, client, _f, _s = _run_main(mod, ["--users-only"])
        assert rc == 0
        urls = [c.args[0] for c in client.get.call_args_list]
        assert urls and all(u.endswith("/users/me") for u in urls)
        assert len(urls) == 2

    def test_there_is_no_apply_mode(self, mod):
        with pytest.raises(SystemExit) as ei:
            mod.main(["--apply"])
        assert ei.value.code == 2

    def test_tokens_never_reach_stdout_or_stderr(self, mod, capsys):
        rc, _c, _f, _s = _run_main(mod, [])
        out = capsys.readouterr()
        assert rc == 0
        assert _H not in out.out + out.err and _C not in out.out + out.err
        assert "Cora Bot . 42" in out.out and "Harrison Fixture . 7" in out.out

    def test_missing_cora_key_hard_fails_before_any_http(self, mod, monkeypatch, capsys):
        monkeypatch.delenv("ASANA_PAT_CORA", raising=False)
        rc, client, factory, _s = _run_main(mod, [])
        assert rc == 2
        assert not factory.called and client.get.call_count == 0
        err = capsys.readouterr().err
        assert "ASANA_PAT_CORA" in err and _H not in err

    def test_missing_harrison_key_hard_fails_before_any_http(self, mod, monkeypatch, capsys):
        monkeypatch.delenv("ASANA_PAT", raising=False)
        rc, client, factory, _s = _run_main(mod, [])
        assert rc == 2 and not factory.called
        assert "ASANA_PAT" in capsys.readouterr().err

    def test_query_params_are_the_live_clients(self, mod):
        """The plate GET is byte-for-byte asana_client.get_user_tasks' params."""
        from cora.tools import asana_client as ac
        rc, client, _f, _s = _run_main(mod, [])
        task_calls = [c for c in client.get.call_args_list if c.args[0].endswith("/tasks") and "/projects/" not in c.args[0]]
        assert task_calls
        p = task_calls[0].kwargs["params"]
        assert p["workspace"] == ac._WORKSPACE_GID
        assert p["completed_since"] == "now"
        assert p["opt_fields"] == ",".join(ac._DEFAULT_TASK_OPT_FIELDS)
        limits = {c.kwargs["params"]["limit"] for c in task_calls}
        # PIN CHANGED (D-051 B-asana-identity-2): plate 25 + hygiene 50 + the exhaustive
        # visibility walk (first page at _API_MAX_LIMIT=100, still GET-only).
        assert limits == {ac._DEFAULT_MAX_TASKS, mod._HYGIENE_MAX_TASKS, ac._API_MAX_LIMIT}
        assert mod._VISIBILITY_MAX_TASKS == 1000
        proj_calls = [c for c in client.get.call_args_list if "/projects/" in c.args[0] and c.args[0].endswith("/tasks")]
        assert proj_calls
        pp = proj_calls[0].kwargs["params"]
        assert pp["completed_since"] == "now" and pp["limit"] == ac._API_MAX_LIMIT
        assert pp["opt_fields"] == "name,due_on,due_at,completed,assignee.name,permalink_url"

    def test_pagination_walks_the_offset_cursor(self, mod):
        pages = [_resp([{"gid": "1", "name": "a"}], next_page={"offset": "o1"}),
                 _resp([{"gid": "2", "name": "b"}], next_page=None)]
        calls: list[dict] = []

        def get(path, params):
            calls.append(dict(params))
            r = pages[len(calls) - 1]
            return r.status_code, r.json()

        status, items = mod._paginate(get, "/tasks", {"assignee": "U"}, 25)
        assert status == 200 and [t["gid"] for t in items] == ["1", "2"]
        assert "offset" not in calls[0] and calls[1]["offset"] == "o1"
        assert calls[0]["limit"] == 25 and calls[1]["limit"] == 24

    def test_system_noise_filtered_per_page_like_the_plate(self, mod):
        def get(path, params):
            return 200, {"data": [{"gid": "n", "name": "It's time to update your goals"},
                                  {"gid": "r", "name": "Order more pens"}], "next_page": None}
        from cora.asana_filters import is_system_noise_task
        assert is_system_noise_task("It's time to update your goals")  # the live term, not a guess
        out = mod.fetch_user_tasks(get, "U", 25)
        assert [t["gid"] for t in out["tasks"]] == ["r"]


# --- source-scan pins ---

class TestSourceScan:
    def test_never_imports_or_names_a_write_function(self):
        text = _SCRIPT.read_text(encoding="utf-8")
        for fn in _WRITE_FUNCTIONS:
            assert re.search(rf"\b{fn}\b", text) is None, fn

    def test_no_non_get_verb_call(self):
        text = _SCRIPT.read_text(encoding="utf-8")
        assert re.search(r"\.(?:post|put|delete|patch|request|stream|send)\s*\(", text) is None

    def test_asana_client_import_is_constants_only(self):
        text = _SCRIPT.read_text(encoding="utf-8")
        m = re.search(r"from cora\.tools\.asana_client import \((.*?)\)", text, re.S)
        assert m, "expected a parenthesised constants-only import"
        names = [n.strip().split("#")[0].strip().rstrip(",") for n in m.group(1).splitlines()]
        names = [n for n in names if n]
        assert names and all(n.startswith("_") for n in names), names
        assert re.search(r"^\s*import cora\.tools\.asana_client|^\s*from cora\.tools import asana_client", text, re.M) is None

    def test_write_functions_still_exist_so_the_pin_is_live(self):
        from cora.tools import asana_client
        for fn in _WRITE_FUNCTIONS:
            assert callable(getattr(asana_client, fn)), fn


# --- map loading (read-only reads of the real maps) ---

class TestMaps:
    def test_catchall_map_dedups_shared_gids(self, mod):
        m = mod.load_catchall_projects()
        assert "" not in m
        shared_fndr = [k for k, v in m.items() if "FNDR" in v]
        assert len(shared_fndr) == 1 and "HJRG" in m[shared_fndr[0]]
        shared_lex = [k for k, v in m.items() if "LEX" in v]
        assert len(shared_lex) == 1 and "LEX-LLC" in m[shared_lex[0]]
        assert all(len(k) >= 10 and k.isdigit() for k in m)

    def test_roster_skips_placeholders(self, mod, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text("users:\n  - slack_user_id: U1\n    asana_user_gid: 123\n    display_name: A\n"
                     "  - slack_user_id: U2\n    asana_user_gid: REPLACE_WITH_GID\n    display_name: B\n"
                     "  - slack_user_id: U3\n    display_name: C\n", encoding="utf-8")
        assert mod.load_roster_users(p) == [("A", "123")]
