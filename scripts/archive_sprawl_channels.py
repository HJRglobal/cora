#!/usr/bin/env python
"""One-off Slack sprawl-archive maintenance script.

Standalone: does NOT import any ``cora`` module, does NOT touch the running bot,
does NOT restart anything, does NOT touch ``main`` logic. It archives a fixed,
Harrison-authorized list of sprawl channels (archiving is reversible) behind a
hard keep-list guard (defense-in-depth) plus a per-group expected-name guard.

Usage:
    # dry-run (DEFAULT) -- archives nothing, prints the full plan + Group-2 report
    python scripts/archive_sprawl_channels.py

    # execute the archives
    python scripts/archive_sprawl_channels.py --apply

    # execute + also archive Harrison's chosen Group-2 channels (still keep-guarded)
    python scripts/archive_sprawl_channels.py --apply --also C0XXXX,C0YYYY

Auth: prefers SLACK_USER_TOKEN (Harrison's owner token) from .env, falls back to
SLACK_BOT_TOKEN. Writes a log to logs/archive-sprawl-<date>.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)

# ---------------------------------------------------------------------------
# Keep-list guard (defense-in-depth) -- runs on EVERY channel before archiving.
# ---------------------------------------------------------------------------
KEEP_IDS = {
    "C0B2T18R3FG",  # social
    "C0B2Z7Z7C84",  # media
    "C0B4QT25AUT",  # f3-events
    "C0B5XHS0648",  # f3-energy-social
    "C0B517928RL",  # f3-mood-social
    "C0B5178T1N2",  # f3-pure-social
    "C0B56T9SEUC",  # pod-social
    "C0B2E5Z8MTR",  # pod
    "C0BBUMAU4KG",  # f3-bdm
    "C0B3K6DEEAF",  # f3e-sales
}

KEEP_FINANCE = {
    "hjrg-finance", "f3e-finance", "osn-finance", "hjrp-finance", "lex-finance",
    "llc-finance", "lts-finance", "lbhs-finance", "lla-finance",
    "osngm-finance", "osngw-finance", "osngf-finance", "osnvv-finance",
    "founder-finance",
}

KEEP_EXPLICIT = {
    "founder-finance", "founder-operations", "general-do-not-use",
    "tiktok-shop-build", "tucson-site-launch",
    "wikipedia-presence-press-acquisition-build", "f3-production-run-2",
    "reddit-presence-90-day-build", "f3-pure-launch", "f3-ops-cockpit",
    "cowork-daily-briefs",
}

# lex-/llc/lts/lbhs/lla prefixed channels are KEPT, except these explicit allows.
LEX_PREFIX_ALLOW = {"lex-dta", "lex-faq-builder"}
_LEX_PREFIXES = ("lex-", "llc", "lts", "lbhs", "lla")
_OSN_STORE_PREFIXES = ("osngw-", "osngm-", "osngf-", "osnvv-")


def keep_reason(channel_id: str, name: str):
    """Return a non-None reason string if this channel must be KEPT (never archived)."""
    if channel_id in KEEP_IDS:
        return f"KEEP_ID ({channel_id})"
    if name.endswith("-leadership"):
        return "ends-with -leadership"
    if name in KEEP_FINANCE:
        return "finance keep-set"
    if name.startswith("cora-"):
        return "cora- prefix"
    if name.startswith(_OSN_STORE_PREFIXES):
        return "osn-store prefix"
    if name.startswith(_LEX_PREFIXES) and name not in LEX_PREFIX_ALLOW:
        return "lex/llc/lts/lbhs/lla prefix"
    if name in KEEP_EXPLICIT:
        return "explicit keep-set"
    return None


# ---------------------------------------------------------------------------
# Target lists
# ---------------------------------------------------------------------------
GROUP1_IDS = [
    "C0B7YQVB0FM", "C0B834DU3JA", "C0B8ZEY4W2C", "C0B5YLSC28G", "C0B567EGP7B",
    "C0B8ZEXKZ32", "C0B856E1KEE", "C0B7PN20JUF", "C0B7YQWLVRR", "C0B871J1ESV",
    "C0B554KD7BK", "C0B7YQS7K19", "C0B97KEMH1P", "C0BA036BHPS", "C0B8QAG0DB9",
    "C0B88QGCKT6", "C0B8LR77751", "C0B8PQU15QV", "C0B8WQWATE0", "C0B7YQMS1D1",
    "C0B92CSTEDB", "C0B8343NM62", "C0B88QAQXGC", "C0B88QB3A8L", "C0B8ZEPJJJC",
    "C0B7PMTTP0X", "C0B8566ASBC", "C0B8ZEPK1KJ", "C0B7PMM6JGP", "C0B8341RZML",
    "C0B8340TYT0", "C0B8565H6SW",
]

# Resolved Group-1 names must fall inside this set, otherwise flag + skip
# (catches a transposed / wrong ID). Under-archiving is the safe failure mode.
EXPECTED_GROUP1_NAMES = {
    "influencer-portfolio-2", "ecom-portfolio-2", "retail-portfolio-2",
    "paid-ads-portfolio", "brand-system-portfolio",
    "social-energy", "social-mood", "social-pure", "social-podcast",
    "social-harrisonjrogers", "harrisonjrogers-social",
    "big-d-media-social-account",
    "sales-account-management",
    "sales-distributor-relations", "sales-wholesale-retail-pipeline",
    "events", "events-energy", "events-mood", "events-pure",
    "2026-activations-and-events", "activations-events-sampling",
    "location-gilbert-warner", "location-gilbert-mckellips",
    "location-greenfield-60", "location-val-vista-pecos",
    "phoenix-location", "ellsworth-location", "payson-location", "hampton-location",
    "osn-transition", "ufl-strategic-planning", "fleet-safety-improvements",
}

GROUP1B_NAMES = [
    "tech-pos-systems", "wu-pos-systems", "homebase-integration",
    "2026-planning", "2026-planning-annual", "strategic-2026-planning",
    "scopes-of-work", "job-description-updates",
    "grant-pipeline", "donor-stewardship", "grow-to-750-members",
    "nsf-certification", "paylocity-transition", "milanote-review",
    "lex-dta", "lex-faq-builder", "payson-cabin", "cabin-management",
]

# Report only -- never archived by default. (id, name) -- id may be None (resolve by name).
GROUP2 = [
    ("C0B2P8JHZFV", "osn"), ("C0B3V5X85N0", "osn-ops"), ("C0B43TR0F3Q", "osn-hr"),
    ("C0B2XG2D1CZ", "ufl"), ("C0B3K6H59FV", "ufl-finance"),
    ("C0B2E5Z6YGP", "bdm"), ("C0B3V5V3DQC", "bdm-finance"),
    ("C0B3RH6LMDY", "bdm-sales"), ("C0B3RH626PL", "bdm-clients"),
    ("C0B2THM2W86", "osn-recon-pilot"), ("C0B5X49CN6A", "bdm-osn"),
    ("C0B5Z7CAW74", "bdm-lac"), ("C0B6TEUMMB2", "bdm-mclaren"),
    ("C0B5Z68FBNE", "bdm-redbull"), ("C0B5X5BK39U", "bdm-arie-lauren"),
    ("C0B5SRVGWRZ", "bdm-demi-brand"), ("C0B5X5D3QQN", "bdm-hjrpodcast"),
    ("C0B5SQS9X7D", "bdm-berry-divine"), ("C0B6TEUV0RE", "bdm-lifted-trucks"),
    ("C0B610J3PH7", "bdm-f3energy"), ("C0B8569BXRQ", "open-tucson-dta-location"),
    (None, "hjrg-legal"), (None, "hjrg-it"), (None, "good-pudding"),
    (None, "vendor-good-pudding"), (None, "barclay"), (None, "mclaren"),
    (None, "redbull"), (None, "lifted-trucks"), (None, "lac"),
    (None, "berry-divine"), (None, "push-performance"), (None, "ari-x-lauren"),
    (None, "rogers-ranch-bookings"),
]

_SYSTEM_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "pinned_item",
    "bot_add", "bot_remove", "reminder_add", "bot_message", "tombstone",
    "group_join", "group_leave",
}


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------
def build_channel_index(client: WebClient):
    """Full {id: channel} and {name: channel} maps, INCLUDING archived channels."""
    by_id, by_name = {}, {}
    cursor = None
    while True:
        resp = client.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=False,
            limit=1000,
            cursor=cursor,
        )
        for ch in resp.get("channels", []):
            by_id[ch["id"]] = ch
            existing = by_name.get(ch["name"])
            # prefer the active (non-archived) channel when a name collides
            if existing is None or (existing.get("is_archived") and not ch.get("is_archived")):
                by_name[ch["name"]] = ch
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)
    return by_id, by_name


def resolve_info(client: WebClient, channel_id: str, by_id: dict):
    """Authoritative per-ID resolution via conversations.info, falling back to the index."""
    try:
        return client.conversations_info(channel=channel_id)["channel"]
    except SlackApiError as e:
        return by_id.get(channel_id) or {"error": e.response.get("error")}


def last_human_message(client: WebClient, channel_id: str):
    """Return (iso_date, days_idle) of the newest non-bot, non-system message, or (None/err, None)."""
    cursor = None
    pages = 0
    try:
        while pages < 3:
            resp = client.conversations_history(channel=channel_id, limit=100, cursor=cursor)
            for m in resp.get("messages", []):
                if m.get("bot_id"):
                    continue
                if m.get("subtype") in _SYSTEM_SUBTYPES:
                    continue
                if not m.get("user"):
                    continue
                d = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc).date()
                return d.isoformat(), (date.today() - d).days
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            pages += 1
            if not cursor:
                break
            time.sleep(0.3)
        return None, None
    except SlackApiError as e:
        return f"error:{e.response.get('error')}", None


def plan_target(label, channel_id, name_hint, ch, group):
    """Decide (no side effects) what would happen to one archive target."""
    rec = {"group": group, "label": label}
    if not ch or ch.get("error"):
        rec.update(id=channel_id, name=name_hint, status="not_found",
                   detail=(ch or {}).get("error"))
        return rec
    cid, cname = ch["id"], ch["name"]
    rec.update(id=cid, name=cname, is_archived=bool(ch.get("is_archived")),
               is_private=bool(ch.get("is_private")))
    kr = keep_reason(cid, cname)
    if kr:
        rec.update(status="COLLISION_KEEP", detail=kr)
        return rec
    if group == "GROUP1" and cname not in EXPECTED_GROUP1_NAMES:
        rec.update(status="UNEXPECTED_NAME", detail="resolved name not in expected Group-1 set")
        return rec
    if ch.get("is_archived"):
        rec.update(status="already_archived")
        return rec
    rec.update(status="would_archive")
    return rec


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="execute archives (default is dry-run)")
    ap.add_argument("--also", default="", help="comma-separated extra channel IDs to archive (Group-2 picks)")
    args = ap.parse_args()

    user_token = os.environ.get("SLACK_USER_TOKEN")
    token = user_token or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        sys.exit("ERROR: no SLACK_USER_TOKEN or SLACK_BOT_TOKEN in .env")
    token_kind = "SLACK_USER_TOKEN" if user_token else "SLACK_BOT_TOKEN"

    client = WebClient(token=token, retry_handlers=[
        RateLimitErrorRetryHandler(max_retry_count=5),
        ConnectionErrorRetryHandler(max_retry_count=3),
    ])

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== archive_sprawl_channels  [{mode}]  auth={token_kind} ===\n")

    print("Indexing channels (includes archived)...")
    by_id, by_name = build_channel_index(client)
    print(f"  indexed {len(by_id)} by id, {len(by_name)} by name\n")

    records = []
    for cid in GROUP1_IDS:
        records.append(plan_target("group1", cid, None, resolve_info(client, cid, by_id), "GROUP1"))
    for nm in GROUP1B_NAMES:
        records.append(plan_target("group1b", None, nm, by_name.get(nm), "GROUP1B"))

    also_ids = [x.strip() for x in args.also.split(",") if x.strip()]
    for cid in also_ids:
        records.append(plan_target("also", cid, None, resolve_info(client, cid, by_id), "ALSO"))

    # Execute archives (only in --apply; only would_archive records)
    if args.apply:
        for rec in records:
            if rec.get("status") == "would_archive":
                try:
                    client.conversations_archive(channel=rec["id"])
                    rec["status"] = "archived"
                    time.sleep(0.4)
                except SlackApiError as e:
                    rec["status"] = f"FAILED:{e.response.get('error')}"

    # Group 2 -- report only (read-only; runs in both modes)
    group2 = []
    for cid, nm in GROUP2:
        ch = by_id.get(cid) if cid else by_name.get(nm)
        if not ch:
            group2.append({"id": cid, "name": nm, "status": "not_found",
                           "last_human_message_date": None, "days_idle": None})
            continue
        lhd, idle = last_human_message(client, ch["id"])
        group2.append({
            "id": ch["id"], "name": ch["name"],
            "is_archived": bool(ch.get("is_archived")),
            "last_human_message_date": lhd, "days_idle": idle,
        })

    # ---- write JSONL log ----
    log_dir = _REPO_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"archive-sprawl-{date.today().isoformat()}.jsonl"
    run_ts = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps({"run_ts": run_ts, "mode": mode, **rec}) + "\n")
        for g in group2:
            f.write(json.dumps({"run_ts": run_ts, "mode": mode, "group": "GROUP2", **g}) + "\n")

    # ---- print tables ----
    def row(rec):
        flag = ""
        st = rec.get("status", "")
        if st in ("COLLISION_KEEP", "UNEXPECTED_NAME", "not_found") or st.startswith("FAILED"):
            flag = "  <-- REVIEW"
        return f"  {st:<16} {str(rec.get('id')):<12} {str(rec.get('name')):<34} {rec.get('detail') or ''}{flag}"

    print("------ ARCHIVE PLAN (Group 1 + Group 1b + --also) ------")
    for grp in ("GROUP1", "GROUP1B", "ALSO"):
        grp_recs = [r for r in records if r["group"] == grp]
        if not grp_recs:
            continue
        print(f"\n[{grp}]")
        for rec in grp_recs:
            print(row(rec))

    # summary counts
    from collections import Counter
    counts = Counter(r["status"].split(":")[0] for r in records)
    print("\n------ SUMMARY ------")
    for k in ("would_archive", "archived", "already_archived", "not_found",
              "COLLISION_KEEP", "UNEXPECTED_NAME", "FAILED"):
        if counts.get(k):
            print(f"  {k:<16} {counts[k]}")

    # Group 2 report, most idle first
    def sort_key(g):
        di = g.get("days_idle")
        return (-1 if di is None else di)
    print("\n------ GROUP 2 (REPORT ONLY -- Harrison decides) ------")
    print(f"  {'days_idle':<10} {'last_human_msg':<14} {'id':<12} name")
    for g in sorted(group2, key=sort_key, reverse=True):
        di = g.get("days_idle")
        di_s = "n/a" if di is None else str(di)
        lhd = g.get("last_human_message_date") or g.get("status") or "?"
        arch = " [ARCHIVED]" if g.get("is_archived") else ""
        print(f"  {di_s:<10} {str(lhd):<14} {str(g.get('id')):<12} {g['name']}{arch}")

    print(f"\nLog: {log_path}")
    if not args.apply:
        n = counts.get("would_archive", 0)
        print(f"\nDRY-RUN complete. {n} channel(s) WOULD be archived. Nothing was changed.")
        print("Review flags above. To execute:  python scripts/archive_sprawl_channels.py --apply")
        print("Add Harrison's Group-2 picks with:  --also C0XXXX,C0YYYY")


if __name__ == "__main__":
    main()
