#!/usr/bin/env python3
"""Read-only probe: which Slack user id is Harrison? (Code #13 RIDER 1, slice S-C)

Repo ``CLAUDE.md`` (KEY IDS block) lists Harrison as ``U02P3D6AT2C`` while every
code site uses ``U0B2RM2JYJ1`` (``tool_dispatch._FOUNDER_SLACK_ID`` /
``_HARRISON_SLACK_ID``, ``user_access.py``, ``review_lanes.py``,
``data/maps/send-trust.yaml``) and the live queue ledger records Harrison's taps
under ``U0B2RM2JYJ1``. This probe resolves BOTH ids with ``users.info`` and
prints, per id, ONLY ``real_name`` / ``deleted`` / ``is_bot`` (or the API error
string, scrubbed of any token-shaped text). It decides which id stays in
``CLAUDE.md``; a human strikes the loser with a dated note.

    .venv\\Scripts\\python.exe scripts\\probe_slack_user_ids.py
    .venv\\Scripts\\python.exe scripts\\probe_slack_user_ids.py --ids U02P3D6AT2C,U0B2RM2JYJ1

Posture (binding):
  * READ-ONLY. The ONLY Slack method this file calls is ``users_info``. A source
    pin in ``tests/test_identity_docs.py`` fails if any other ``client.<method>``
    appears. There is no write site, so there is no ``--dry-run`` flag to claim.
  * The bot token is read from the environment (``SLACK_BOT_TOKEN``, loaded from
    ``.env`` at RUN time inside ``main``, never at import) and is never printed.
    Missing token -> the KEY NAME is named on stderr, exit 2.
  * STOP condition (kickoff section 8): if BOTH ids resolve to live, non-deleted,
    non-bot users the probe prints ``BOTH RESOLVE -- ESCALATE`` and exits 3; a
    human decides, not this script.

Exit codes: 0 = exactly one id resolves to a live human (the answer is printed);
1 = neither resolves / API errors; 2 = no token; 3 = both resolve (escalate).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_IDS: tuple[str, ...] = ("U02P3D6AT2C", "U0B2RM2JYJ1")

# Slack user ids: U or W prefix + 8..12 uppercase alphanumerics. Anchored both
# ends, a single bounded class -- no nesting, linear in the input length.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{8,12}$")

# Belt: an SDK error message may echo request context; strip anything that
# looks like a Slack token before the text reaches stdout/stderr.
_TOKEN_SHAPE_RE = re.compile(r"xox[abpser]-[A-Za-z0-9-]+")


def _scrub(text: str) -> str:
    return _TOKEN_SHAPE_RE.sub("xox?-<redacted>", text)


def _make_client(token: str):
    """Construct the Slack WebClient lazily so importing this module needs no
    network and no slack_sdk at import time."""
    from slack_sdk import WebClient

    return WebClient(token=token)


def probe(ids, client) -> list[dict]:
    """Resolve each id with users.info. Returns one row per id; never raises."""
    rows: list[dict] = []
    for uid in ids:
        uid = str(uid).strip()
        if not _SLACK_USER_ID_RE.match(uid):
            rows.append({"id": uid, "error": "not a Slack user-id shape"})
            continue
        try:
            resp = client.users_info(user=uid)
        except Exception as exc:  # slack_sdk.errors.SlackApiError and transport errors
            rows.append({"id": uid, "error": _scrub(str(exc))})
            continue
        user = {}
        try:
            user = resp.get("user") or {}
        except Exception:
            user = {}
        if not user or not (user.get("real_name") or user.get("name")):
            # Fail CLOSED: an empty / nameless payload is never counted as a live
            # human (Slack raises user_not_found for unknown ids; a transport
            # that returns ok with no user would otherwise read as "resolves").
            rows.append({"id": uid, "error": "empty user payload"})
            continue
        rows.append(
            {
                "id": uid,
                "real_name": str(user.get("real_name") or user.get("name") or ""),
                "deleted": bool(user.get("deleted")),
                "is_bot": bool(user.get("is_bot")),
            }
        )
    return rows


def _is_live_human(row: dict) -> bool:
    return "error" not in row and not row.get("deleted") and not row.get("is_bot")


def render(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"{r['id']} -> ERROR {r['error']}")
        else:
            lines.append(
                f"{r['id']} -> real_name={r['real_name']!r} deleted={r['deleted']} is_bot={r['is_bot']}"
            )
    live = [r["id"] for r in rows if _is_live_human(r)]
    if len(live) == 1:
        lines.append(f"ANSWER: {live[0]} is the live human; strike the other id in CLAUDE.md with a dated note.")
    elif len(live) > 1:
        lines.append("BOTH RESOLVE -- ESCALATE (kickoff section 8): a human decides, not this script.")
    else:
        lines.append("NO LIVE HUMAN RESOLVED -- check the token scope (users:read) and the ids.")
    return "\n".join(lines)


def exit_code(rows: list[dict]) -> int:
    live = [r for r in rows if _is_live_human(r)]
    if len(live) == 1:
        return 0
    if len(live) > 1:
        return 3
    return 1


def main(argv=None, *, make_client=_make_client, load_env: bool = True) -> int:
    ap = argparse.ArgumentParser(description="Read-only users.info probe for the two Harrison Slack ids.")
    ap.add_argument(
        "--ids",
        default=",".join(DEFAULT_IDS),
        help="comma-separated Slack user ids (default: both candidate Harrison ids)",
    )
    args = ap.parse_args(argv)
    ids = [s for s in (p.strip() for p in args.ids.split(",")) if s]

    if load_env:
        from dotenv import load_dotenv

        load_dotenv(_REPO_ROOT / ".env", override=True)

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN is not set (key name only; nothing else is read)", file=sys.stderr)
        return 2

    rows = probe(ids, make_client(token))
    print(render(rows))
    return exit_code(rows)


if __name__ == "__main__":
    sys.exit(main())
