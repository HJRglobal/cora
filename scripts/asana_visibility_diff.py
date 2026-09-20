#!/usr/bin/env python3
"""Asana visibility diff -- what Harrison's PAT sees vs what the cora@ seat sees.

Code #13 RIDER 1 / S-B (bundle code-identity-v1). READ-ONLY BY CONSTRUCTION:
this script issues HTTP GET requests only. There is no write mode, no --apply,
and nothing to confirm; `--dry-run` is accepted for symmetry with the other
scripts and is the ONLY mode (tests/scripts/test_asana_visibility_diff.py pins
zero non-GET calls and that none of asana_client's nine write functions is
imported or named here).

Under BOTH tokens (harrison via ASANA_PAT, cora via ASANA_PAT_CORA -- read
through cora.asana_identity.resolve_pat_for, hard-fail naming the KEY when one
is missing) it runs the four live read shapes, byte-for-byte the params the
live consumers send:

  1. users/me                      -> "name . gid" per identity (Cora's gid then
                                      lands in slack-to-asana.yaml as the commented
                                      bot row -- Harrison fills it, never this script)
  2. plate-tool tasks query        -> GET /tasks?assignee=<gid>&workspace=<ws>
                                      &completed_since=now&limit=25&opt_fields=<rich>
                                      per slack-to-asana.yaml user (asana_client.
                                      get_user_tasks defaults, system-noise filtered
                                      per page exactly as the plate does)
  3. meeting-capture catch-all     -> GET /projects/<gid>/tasks?completed_since=now
                                      &limit=100&opt_fields=<get_project_tasks set>
                                      for every non-empty, de-duplicated gid in the
                                      `projects:` map of meeting-capture-projects.yaml
                                      (FNDR/HJRG and LEX/LEX-LLC share gids)
  4. hygiene-nudge query           -> the same GET /tasks shape as (2) with limit=50
                                      (scripts/run_asana_hygiene_nudges.py:236
                                      get_user_tasks(str(asana_gid), max_tasks=50))
  5. visibility query              -> the same GET /tasks shape as (2) paginated to
                                      EXHAUSTION (cap _VISIBILITY_MAX_TASKS=1000,
                                      still GET-only). This is the ONLY per-user
                                      query whose set difference is reported by
                                      name: (2) and (4) are first-N WINDOWS, so a
                                      window diff names tasks that merely shifted
                                      position (Harrison sees them) and misses any
                                      invisible task beyond the window. The windows
                                      are reported as labelled COUNTS only.

Output: per-query counts under both identities and, for the visibility query and
the catch-alls, the set difference of task / project gids with NAMES ONLY -- no
descriptions, no notes. A project Cora cannot reach (403/404 under ASANA_PAT_CORA
-- _MEMBERSHIP_GAP_CODES) is printed as an ACTION line naming it for Harrison to
add the cora@ seat to in Asana. NEVER a code workaround. Any OTHER cora-side error
(429, 5xx, the getter's synthetic 599 on a network error, "absent") is a RETRY
line, never a membership signal -- an action list that told Harrison to change
Asana membership on a rate-limit would be wrong in the irreversible direction.

PHI posture (D-145): tasks in LEX-prefixed projects and tasks whose name trips
phi_guard.is_phi_risk are COUNTED but their names are withheld; LEX catch-all
projects are reported aggregate-only (counts + project name).

Usage (Harrison's hand, before flipping CORA_ASANA_IDENTITY):
    .venv\\Scripts\\python.exe scripts\\asana_visibility_diff.py
    .venv\\Scripts\\python.exe scripts\\asana_visibility_diff.py --users-only
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import asana_identity  # noqa: E402
from cora.asana_filters import is_system_noise_task, task_belongs_to_entity  # noqa: E402
from cora.phi_guard import is_phi_risk  # noqa: E402
# Constants ONLY -- so the query params are byte-for-byte the live client's and
# cannot drift. No function from asana_client is imported (source-scan pinned).
from cora.tools.asana_client import (  # noqa: E402
    _API_MAX_LIMIT,
    _BASE,
    _DEFAULT_MAX_TASKS,
    _DEFAULT_TASK_OPT_FIELDS,
    _WORKSPACE_GID,
)

USER_MAP_FILE = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
CAPTURE_MAP_FILE = _REPO_ROOT / "data" / "maps" / "meeting-capture-projects.yaml"

_TIMEOUT = 10.0
# scripts/run_asana_hygiene_nudges.py:236 -- get_user_tasks(str(asana_gid), max_tasks=50)
_HYGIENE_MAX_TASKS = 50
# The visibility comparison paginates to exhaustion under this cap (GET-only; the
# cap only bounds the walk). A user at the cap is flagged, never silently truncated.
_VISIBILITY_MAX_TASKS = 1000
# The ONLY cora-side status codes that mean "the cora@ seat is not a member".
# Everything else (429 / 5xx / 599 network / "absent") is transient -> RETRY.
_MEMBERSHIP_GAP_CODES = frozenset({403, 404})
# asana_client.get_project_tasks opt_fields (kept in the same order).
_PROJECT_TASK_OPT_FIELDS = ["name", "due_on", "due_at", "completed", "assignee.name", "permalink_url"]
_WITHHELD = "[name withheld -- LEX/PHI-shaped]"
_DEFAULT_MAX_LIST = 40

Getter = Callable[[str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


# --- HTTP (GET only) ---

def make_getter(token: str) -> Getter:
    """Return a GET-only fetcher bound to *token*. The token never leaves the
    Authorization header (never logged, never returned)."""
    headers = {"Authorization": f"Bearer {token}"}

    def _get(path: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                r = c.get(f"{_BASE}{path}", params=params or {}, headers=headers)
        except httpx.RequestError as exc:
            return 599, {"_error": f"network error: {type(exc).__name__}"}
        try:
            body = r.json() if r.status_code == 200 else {}
        except ValueError:
            body = {}
        return r.status_code, body

    return _get


def _paginate(get: Getter, path: str, params: dict[str, Any], max_items: int,
              per_item: Callable[[dict[str, Any]], bool] | None = None) -> tuple[int, list[dict[str, Any]]]:
    """Walk the Asana offset cursor exactly like asana_client does: limit =
    min(_API_MAX_LIMIT, remaining), filter per page, stop on no next_page."""
    items: list[dict[str, Any]] = []
    offset: str | None = None
    while len(items) < max_items:
        page_params = dict(params)
        page_params["limit"] = min(_API_MAX_LIMIT, max_items - len(items))
        if offset:
            page_params["offset"] = offset
        status, body = get(path, page_params)
        if status != 200:
            return status, items
        for t in (body.get("data", []) or []):
            if per_item is None or per_item(t):
                items.append(t)
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            break
    return 200, items[:max_items]


# --- the four read shapes ---

def fetch_me(get: Getter) -> dict[str, Any]:
    status, body = get("/users/me", {"opt_fields": "name,gid"})
    if status != 200:
        return {"error": status}
    data = body.get("data", {}) or {}
    return {"name": str(data.get("name", "")), "gid": str(data.get("gid", ""))}


def fetch_user_tasks(get: Getter, user_gid: str, max_tasks: int) -> dict[str, Any]:
    """asana_client.get_user_tasks byte-for-byte: assignee + workspace +
    completed_since=now + rich opt_fields, system-noise dropped PER PAGE."""
    fields = list(_DEFAULT_TASK_OPT_FIELDS)
    if "name" not in fields:
        fields = ["name", *fields]
    params = {
        "assignee": user_gid,
        "workspace": _WORKSPACE_GID,
        "completed_since": "now",
        "opt_fields": ",".join(fields),
    }
    status, tasks = _paginate(get, "/tasks", params, max_tasks,
                              per_item=lambda t: not is_system_noise_task(t.get("name", "")))
    if status != 200:
        return {"error": status}
    # capped: the walk stopped at max_tasks, so more may exist. Meaningful for the
    # exhaustive visibility query (flagged in the report); normal for the windows.
    return {"tasks": [_task_row(t) for t in tasks], "capped": len(tasks) >= max_tasks}


def fetch_project_tasks(get: Getter, project_gid: str) -> dict[str, Any]:
    """asana_client.get_project_tasks byte-for-byte (limit 100, incomplete-only)."""
    params = {
        "completed_since": "now",
        "opt_fields": ",".join(_PROJECT_TASK_OPT_FIELDS),
    }
    status, tasks = _paginate(get, f"/projects/{project_gid}/tasks", params, _API_MAX_LIMIT)
    if status != 200:
        return {"error": status}
    return {"tasks": [_task_row(t) for t in tasks]}


def fetch_project_name(get: Getter, project_gid: str) -> str:
    status, body = get(f"/projects/{project_gid}", {"opt_fields": "name"})
    if status != 200:
        return ""
    return str((body.get("data", {}) or {}).get("name", ""))


def _task_row(t: dict[str, Any]) -> dict[str, Any]:
    """gid + name + a LEX flag. Names of LEX-project tasks and PHI-shaped names
    are withheld HERE, at the read boundary, so nothing downstream can print them."""
    name = str(t.get("name", "") or "")
    lex = task_belongs_to_entity(t, "LEX")
    if lex or (name and is_phi_risk(name)):
        name = _WITHHELD
    return {"gid": str(t.get("gid", "")), "name": name, "lex": lex}


# --- roster + map loading ---

def load_roster_users(path: Path = USER_MAP_FILE) -> list[tuple[str, str]]:
    """(display_name, asana_user_gid) for every mapped human -- the exact rows the
    plate tool and the hygiene nudge iterate (REPLACE placeholders skipped)."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: list[tuple[str, str]] = []
    for u in data.get("users", []) or []:
        gid = str(u.get("asana_user_gid", "") or "")
        if not gid or "REPLACE" in gid:
            continue
        out.append((str(u.get("display_name", "") or gid), gid))
    return out


def load_catchall_projects(path: Path = CAPTURE_MAP_FILE) -> dict[str, list[str]]:
    """project_gid -> [entity codes] for every non-empty gid in the `projects:`
    map, de-duplicated (FNDR/HJRG share one gid; LEX/LEX-LLC share one)."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: dict[str, list[str]] = {}
    for entity, gid in (data.get("projects", {}) or {}).items():
        gid = str(gid or "").strip()
        if not gid:
            continue
        out.setdefault(gid, []).append(str(entity))
    return out


# --- views + diff ---

@dataclass
class View:
    identity: str
    me: dict[str, Any] = field(default_factory=dict)
    plate: dict[str, dict[str, Any]] = field(default_factory=dict)      # user_gid -> result (window 25)
    hygiene: dict[str, dict[str, Any]] = field(default_factory=dict)    # user_gid -> result (window 50)
    visibility: dict[str, dict[str, Any]] = field(default_factory=dict)  # user_gid -> result (exhaustive)
    catchall: dict[str, dict[str, Any]] = field(default_factory=dict)   # project_gid -> result


def collect_view(identity: str, get: Getter, roster: list[tuple[str, str]],
                 catchalls: dict[str, list[str]], users_only: bool = False) -> View:
    v = View(identity=identity, me=fetch_me(get))
    if users_only:
        return v
    for _name, gid in roster:
        v.plate[gid] = fetch_user_tasks(get, gid, _DEFAULT_MAX_TASKS)
        v.hygiene[gid] = fetch_user_tasks(get, gid, _HYGIENE_MAX_TASKS)
        v.visibility[gid] = fetch_user_tasks(get, gid, _VISIBILITY_MAX_TASKS)
    for pgid in catchalls:
        v.catchall[pgid] = fetch_project_tasks(get, pgid)
    return v


def _gid_map(result: dict[str, Any]) -> dict[str, str]:
    return {t["gid"]: t["name"] for t in result.get("tasks", []) or []}


def diff_results(h: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    """One query, two views -> counts + the gid set difference (names carried)."""
    out: dict[str, Any] = {
        "harrison_count": None, "cora_count": None,
        "harrison_error": h.get("error"), "cora_error": c.get("error"),
        "harrison_only": [], "cora_only": [],
        "harrison_capped": bool(h.get("capped")), "cora_capped": bool(c.get("capped")),
    }
    hm = _gid_map(h) if "error" not in h else None
    cm = _gid_map(c) if "error" not in c else None
    if hm is not None:
        out["harrison_count"] = len(hm)
    if cm is not None:
        out["cora_count"] = len(cm)
    if hm is not None and cm is not None:
        out["harrison_only"] = sorted(((g, hm[g]) for g in hm.keys() - cm.keys()), key=lambda x: x[1])
        out["cora_only"] = sorted(((g, cm[g]) for g in cm.keys() - hm.keys()), key=lambda x: x[1])
    return out


def diff_views(h: View, c: View) -> dict[str, Any]:
    keys = {"plate": set(h.plate) | set(c.plate),
            "hygiene": set(h.hygiene) | set(c.hygiene),
            "visibility": set(h.visibility) | set(c.visibility),
            "catchall": set(h.catchall) | set(c.catchall)}
    return {
        "me": {"harrison": h.me, "cora": c.me},
        "plate": {k: diff_results(h.plate.get(k, {"error": "absent"}), c.plate.get(k, {"error": "absent"}))
                  for k in sorted(keys["plate"])},
        "hygiene": {k: diff_results(h.hygiene.get(k, {"error": "absent"}), c.hygiene.get(k, {"error": "absent"}))
                    for k in sorted(keys["hygiene"])},
        "visibility": {k: diff_results(h.visibility.get(k, {"error": "absent"}), c.visibility.get(k, {"error": "absent"}))
                       for k in sorted(keys["visibility"])},
        "catchall": {k: diff_results(h.catchall.get(k, {"error": "absent"}), c.catchall.get(k, {"error": "absent"}))
                     for k in sorted(keys["catchall"])},
    }


# --- rendering (names only) ---

def _fmt_count(n: Any, err: Any) -> str:
    if err is not None:
        return f"ERR {err}"
    return str(n)


def _fmt_list(rows: list[tuple[str, str]], max_list: int) -> list[str]:
    lines = [f"      - {name or '(unnamed)'}  [{gid}]" for gid, name in rows[:max_list]]
    if len(rows) > max_list:
        lines.append(f"      ... and {len(rows) - max_list} more")
    return lines


def render(delta: dict[str, Any], roster: list[tuple[str, str]], catchalls: dict[str, list[str]],
           project_names: dict[str, str], max_list: int = _DEFAULT_MAX_LIST) -> str:
    L: list[str] = []
    L.append("ASANA VISIBILITY DIFF -- READ-ONLY (GET only; there is no write mode)")
    L.append("")
    L.append("users/me")
    for ident in ("harrison", "cora"):
        me = delta["me"].get(ident, {}) or {}
        if "error" in me:
            L.append(f"  {ident:<9} ERR {me['error']}")
        else:
            L.append(f"  {ident:<9} {me.get('name', '')} . {me.get('gid', '')}")
    L.append("")

    names_by_gid = {gid: name for name, gid in roster}

    # The visibility comparison -- the ONE per-user diff whose names are reported.
    # It walks GET /tasks to exhaustion under both tokens, so harrison-only really
    # is "Cora cannot see" and cora-only really is "Harrison cannot see".
    L.append(f"visibility diff (GET /tasks paginated to exhaustion, cap {_VISIBILITY_MAX_TASKS}) -- the comparison to act on")
    for ugid, d in delta.get("visibility", {}).items():
        who = names_by_gid.get(ugid, ugid)
        L.append(f"  {who}: harrison {_fmt_count(d['harrison_count'], d['harrison_error'])} / "
                 f"cora {_fmt_count(d['cora_count'], d['cora_error'])}")
        if d.get("harrison_capped") or d.get("cora_capped"):
            L.append(f"    CAP {_VISIBILITY_MAX_TASKS} REACHED -- the walk stopped; this diff may be incomplete")
        if d["harrison_only"]:
            L.append(f"    harrison-only ({len(d['harrison_only'])}) -- Cora cannot see:")
            L.extend(_fmt_list(d["harrison_only"], max_list))
        if d["cora_only"]:
            L.append(f"    cora-only ({len(d['cora_only'])}) -- Harrison cannot see:")
            L.extend(_fmt_list(d["cora_only"], max_list))
    L.append("")

    # The two live WINDOWS -- counts only. A first-N window under each token is not
    # a visibility comparison: a task that merely shifts into Cora's window reads as
    # "cora-only" though Harrison sees it, and an invisible task past position N is
    # never reported. Names for these live in the visibility diff above.
    for section, title in (("plate", f"plate-tool window (GET /tasks, limit {_DEFAULT_MAX_TASKS}, rich opt_fields) -- "
                                     "what the plate would show; COUNTS ONLY, not a visibility signal"),
                           ("hygiene", f"hygiene-nudge window (GET /tasks, limit {_HYGIENE_MAX_TASKS}) -- "
                                       "what the nudge would iterate; COUNTS ONLY, not a visibility signal")):
        L.append(title)
        for ugid, d in delta[section].items():
            who = names_by_gid.get(ugid, ugid)
            line = (f"  {who}: harrison {_fmt_count(d['harrison_count'], d['harrison_error'])} / "
                    f"cora {_fmt_count(d['cora_count'], d['cora_error'])}")
            if d["harrison_only"] or d["cora_only"]:
                line += (f"  (window contents differ: {len(d['harrison_only'])} / {len(d['cora_only'])}"
                         " -- see the visibility diff)")
            L.append(line)
        L.append("")

    L.append("meeting-capture catch-all projects (GET /projects/<gid>/tasks, incomplete-only)")
    action_lines: list[str] = []
    retry_lines: list[str] = []
    for pgid, d in delta["catchall"].items():
        entities = "/".join(catchalls.get(pgid, ["?"]))
        pname = project_names.get(pgid) or "(name unavailable)"
        is_lex = any(e.upper().startswith("LEX") for e in catchalls.get(pgid, []))
        L.append(f"  [{entities}] {pname} [{pgid}]: harrison {_fmt_count(d['harrison_count'], d['harrison_error'])} / "
                 f"cora {_fmt_count(d['cora_count'], d['cora_error'])}")
        if d["cora_error"] is not None and d["harrison_error"] is None:
            # Only a 403/404 under the cora@ token is a MEMBERSHIP signal. A 429,
            # a 5xx, the getter's synthetic 599 or an "absent" view is transient:
            # it goes to RETRY, never to the action list Harrison acts on in Asana.
            if d["cora_error"] in _MEMBERSHIP_GAP_CODES:
                action_lines.append(f"  ADD THE cora@ SEAT TO: [{entities}] {pname} [{pgid}] (cora GET -> {d['cora_error']})")
            else:
                retry_lines.append(f"  RETRY: [{entities}] {pname} [{pgid}] (cora GET -> {d['cora_error']}"
                                   " -- transient/error, NOT a membership signal)")
        elif is_lex:
            # LEX catch-alls are aggregate-only (D-145): counts + project name, never tasks.
            if d["harrison_only"] or d["cora_only"]:
                L.append(f"    LEX aggregate-only: harrison-only {len(d['harrison_only'])} / cora-only {len(d['cora_only'])} (names withheld)")
        else:
            if d["harrison_only"]:
                L.append(f"    harrison-only ({len(d['harrison_only'])}) -- Cora cannot see:")
                L.extend(_fmt_list(d["harrison_only"], max_list))
            if d["cora_only"]:
                L.append(f"    cora-only ({len(d['cora_only'])}):")
                L.extend(_fmt_list(d["cora_only"], max_list))
    L.append("")
    L.append("HARRISON ACTION LIST (Asana membership -- never a code workaround; 403/404 under the cora@ token ONLY)")
    if action_lines:
        L.extend(action_lines)
    elif retry_lines:
        L.append(f"  (no membership gap confirmed -- {len(retry_lines)} project(s) pending RETRY below)")
    else:
        L.append("  (none -- the cora@ seat reaches every catch-all project Harrison's PAT reaches)")
    L.append("")
    L.append("RETRY (transient/error under the cora@ token -- NOT a membership signal; re-run before acting)")
    if retry_lines:
        L.extend(retry_lines)
    else:
        L.append("  (none)")
    return "\n".join(L)


# --- main ---

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="READ-ONLY Asana visibility diff (harrison vs cora identity).")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="accepted for symmetry; this script has no other mode (GET only)")
    ap.add_argument("--users-only", action="store_true", help="only the two users/me probes")
    ap.add_argument("--max-list", type=int, default=_DEFAULT_MAX_LIST,
                    help="max names printed per set-difference list")
    args = ap.parse_args(argv)

    tokens: dict[str, str] = {}
    for ident in (asana_identity.IDENTITY_HARRISON, asana_identity.IDENTITY_CORA):
        try:
            tokens[ident] = asana_identity.resolve_pat_for(ident)
        except asana_identity.AsanaIdentityError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)  # key NAME only
            return 2

    roster = load_roster_users()
    catchalls = load_catchall_projects()
    getters = {ident: make_getter(tok) for ident, tok in tokens.items()}

    views = {ident: collect_view(ident, getters[ident], roster, catchalls, users_only=args.users_only)
             for ident in getters}
    project_names = {} if args.users_only else {
        pgid: fetch_project_name(getters[asana_identity.IDENTITY_HARRISON], pgid) for pgid in catchalls}

    delta = diff_views(views[asana_identity.IDENTITY_HARRISON], views[asana_identity.IDENTITY_CORA])
    print(render(delta, roster, catchalls, project_names, max_list=args.max_list))
    return 0


if __name__ == "__main__":
    sys.exit(main())
