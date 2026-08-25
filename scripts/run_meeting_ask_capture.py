#!/usr/bin/env python3
"""S3 (cq-f52c6b691127): per-meeting trigger for explicit Cora-directed asks.

WHAT RUNS HERE. Poll Fireflies over a deliberately OVERLAPPING recent window (see
`_window_start` -- the watermark may only widen it, never narrow it); scan each
transcript's sentences for ones in which a human addressed Cora out loud with a
request; DM the addressee ONE propose-only card per ask it has not already carded.
Nothing is created, nothing is assigned, nothing is written anywhere except this
script's own watermark and the card store.

"PER MEETING, NOT PERIODIC" -- AND WHAT THAT HONESTLY MEANS HERE. There is no
Fireflies webhook to subscribe to: the API exposes no webhook-registration
mutation, and the bot has no ingress that could receive one anyway (Slack Socket
Mode is outbound, and the only HTTP listener is a GET-only /health bound to
127.0.0.1). So the trigger is a SHORT-INTERVAL WATERMARK POLL that does work only
when a new transcript has appeared. Functionally that is per-meeting -- one card
set per meeting, fired at the first poll after its transcript lands -- and at
~2.1-2.4 meetings/day the polling itself is nearly free. What it is NOT is
instantaneous, and the acceptance bar the kickoff set ("within hours of the
meeting") is met with a 15-minute interval where the 03:30 daily ingest would have
left a 10am ask waiting ~17 hours.

WHY THIS SITS BESIDE THE INGEST RATHER THAN INSIDE IT. `incremental_sync_fireflies`
owns the KB write and its watermark is a row in the KB `sync_state` table; folding
a Slack-DM side effect into it would mean a card-send failure could interfere with
an ingest run, and the two want different cadences (embedding the whole corpus
hourly is not free; reading titles is). Separate watermark, separate failure
domain.

DOES NOT RE-ENABLE THE RETIRED PUSH. `Cora - Meeting Action Capture` stays
Disabled: it auto-CREATED Asana tasks from Fireflies' AI-generated
`summary.action_items` and D-054 retired it after "Demi's 14 unwanted tasks". It
is also recorded as intended-Disabled in `data/maps/scheduled-task-state.yaml`, so
enabling it would raise a nightly health WARN. This script shares none of its
code path and proposes instead of creating.

Run:  python scripts/run_meeting_ask_capture.py [--dry-run] [--lookback-hours N]
                                                [--transcript-id ID] [--max-meetings N]
Dry-run is the DEFAULT-SAFE mode for inspection: it detects and prints, sends no
DM and advances no watermark.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Windows console guard. These reports print emoji (the card's affordance line,
# the report's status glyphs) and the default Windows stdout codec is cp1252, which
# RAISES UnicodeEncodeError on them -- so the dry-run/inspection path, which is the
# first thing the setup PS1 tells you to run, crashed mid-card. Found by running it,
# not by a test: pytest captures stdout through a UTF-8 pipe and never sees cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from cora import meeting_asks  # noqa: E402
from cora.connectors import fireflies_connector as ffc  # noqa: E402
from cora.connectors import fireflies_diarization as ffd  # noqa: E402

_WATERMARK_PATH = _REPO_ROOT / "data" / "state" / "meeting-ask-watermark.json"

#: Default poll window, MUCH wider than the task interval on purpose. Two reasons,
#: and the second is the load-bearing one: (a) Task Scheduler does not catch up
#: missed firings by design (D-041), so a sleeping host must not leave a permanent
#: hole; (b) the Fireflies query filters on MEETING DATE, so a transcript that
#: lands an hour after its meeting is only visible to a window that reaches back
#: past the meeting -- see `_window_start`. Re-reading a recent transcript is free
#: because `meeting_asks.already_carded` dedups per ASK.
_DEFAULT_LOOKBACK_HOURS = 24

#: Fireflies field block. DELIBERATELY THIS SCRIPT'S OWN, not the shared
#: `_TRANSCRIPTS_QUERY`: it needs `sentences.start_time` (for D-136's timestamp)
#: which the shared query does not request, and an unknown field makes the WHOLE
#: GraphQL query fail -- so putting it in the shared constant would put the
#: nightly KB ingest at risk of a schema change that only this feature cares
#: about. Verified against the live schema by introspection: `Sentence` exposes
#: index, speaker_name, speaker_id, raw_text, start_time, end_time, ai_filters,
#: text.
_ASK_QUERY = """
query AskScan($limit: Int, $skip: Int, $fromDate: DateTime, $toDate: DateTime) {
  transcripts(limit: $limit, skip: $skip, fromDate: $fromDate, toDate: $toDate) {
    id
    title
    date
    duration
    organizer_email
    host_email
    participants
    sentences {
      index
      speaker_name
      text
      start_time
    }
    meeting_attendees {
      displayName
      email
    }
  }
}
"""

_ASK_QUERY_BY_ID = """
query AskScanOne($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    duration
    organizer_email
    host_email
    participants
    sentences { index speaker_name text start_time }
    meeting_attendees { displayName email }
  }
}
"""


def _read_watermark() -> int:
    try:
        data = json.loads(_WATERMARK_PATH.read_text(encoding="utf-8")) or {}
        return int(data.get("last_scanned_ts") or 0)
    except Exception:  # noqa: BLE001 -- absent/malformed reads as "never scanned"
        return 0


def _write_watermark(ts: int) -> None:
    """Atomic. Records when this poll last RAN.

    IT IS A FLOOR-EXTENDER, NOT THE WINDOW. See `_window_start`: narrowing the
    fetch window to the last run time would silently drop meetings, because the
    Fireflies `transcripts` query filters on MEETING DATE, not on when the
    transcript became available. Per-ask idempotency lives in
    `meeting_asks.already_carded`, which is what makes a deliberately overlapping
    window free.
    """
    try:
        _WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _WATERMARK_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"last_scanned_ts": int(ts)}, indent=1),
                       encoding="utf-8")
        tmp.replace(_WATERMARK_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: watermark write failed: {exc}", file=sys.stderr)


#: Hard ceiling on how far back a recovery run reaches. Without it, a watermark
#: left stale by a long outage would ask Fireflies for months of transcripts
#: WITH FULL SENTENCES in one call.
_MAX_LOOKBACK_HOURS = 24 * 14


def _window_start(watermark_ts: int, lookback_hours: int) -> int:
    """The `fromDate` for this poll.

    THE WATERMARK MAY ONLY WIDEN THIS WINDOW, NEVER NARROW IT, and that is the
    whole subtlety of polling Fireflies. `transcripts(fromDate, toDate)` filters on
    the MEETING DATE, not on when the transcript became available -- and a
    transcript appears minutes to hours after the meeting ends. So a 10:00 meeting
    whose transcript lands at 10:45 is, at the 10:45 poll, a meeting whose DATE is
    45 minutes before a window that starts at the last run. Narrowing to the
    watermark (`max(wm, floor)`) would therefore skip it permanently -- the poll
    would run every 15 minutes and never see it.

    So the window is the FIXED lookback, extended backwards to the watermark when
    the last run was longer ago than that (an outage), and capped so a stale
    watermark cannot request months of sentences in one call. The resulting
    overlap re-reads the same recent transcripts every run, which is free:
    `meeting_asks.already_carded` is keyed on the ask, not the run.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    floor = now - int(max(lookback_hours, 1)) * 3600
    ceiling = now - _MAX_LOOKBACK_HOURS * 3600
    if watermark_ts and watermark_ts < floor:
        return max(watermark_ts, ceiling)
    return floor


def _fetch_since(since_ts: int, max_meetings: int) -> list[dict]:
    from_dt = datetime.fromtimestamp(max(since_ts, 0), tz=timezone.utc)
    variables = {
        "limit": min(max_meetings, 25),
        "skip": 0,
        "fromDate": from_dt.isoformat(),
        "toDate": datetime.now(timezone.utc).isoformat(),
    }
    data = ffc._graphql_query(_ASK_QUERY, variables)
    return list(data.get("transcripts") or [])[:max_meetings]


def _fetch_one(transcript_id: str) -> list[dict]:
    data = ffc._graphql_query(_ASK_QUERY_BY_ID, {"id": transcript_id})
    t = data.get("transcript")
    return [t] if isinstance(t, dict) else []


def _excluded(transcript: dict) -> str:
    """Why this transcript must not be scanned, or ''.

    MIRRORS THE INGEST'S OWN EXCLUSIONS, in its order, and fails CLOSED on a
    detector error. This is not belt-and-braces: the ingest's exclusions are the
    only thing standing between an NDA'd or clinical transcript and a downstream
    consumer, and a new consumer that reads transcripts directly from the API
    inherits none of them for free.
    """
    title = (transcript.get("title") or "").strip()
    if not title:
        return "no title"
    try:
        from cora.kb_exclusions import is_copa_meeting_title
        if is_copa_meeting_title(title):
            return "COPA/NDA title"
    except Exception:  # noqa: BLE001 -- an unavailable exclusion must exclude
        return "COPA detector unavailable"
    try:
        verdict = ffc.classify_lex_meeting(transcript)
        # THE FIELD IS `hard_exclude_kb`. The first cut read `exclude`, which does
        # not exist on LexVerdict, so `getattr(..., False)` was ALWAYS False and
        # this gate -- the one standing between an excluded Lexington
        # program/client meeting and a DM'd card -- never fired once. A
        # defaulted getattr on a typo'd attribute is a guard that reads as
        # present and is not.
        if verdict.hard_exclude_kb:
            return f"LEX hard-exclude ({verdict.reason or 'program/client'})"
    except Exception as exc:  # noqa: BLE001 -- fail closed, as the ingest does
        return f"LEX detector error ({exc})"
    entity = _entity_for(transcript)
    if ffc._is_phi_meeting(title, "LEX" if entity.startswith("LEX") else entity):
        return "PHI/clinical title"
    return ""


def _phi_flagged(ask: dict) -> bool:
    """Does this ask's own text trip the PHI screen? Fail CLOSED.

    Screens the QUOTED LINE and the derived body together, because either is
    rendered on the card. `is_any_phi` is the broad predicate (the base
    clinical/identifier patterns plus the LEX admin-status class from D-050) --
    the widest available screen is the right one for text about to be DM'd
    verbatim. An unavailable screen means SKIP, never send.
    """
    text = f"{ask.get('quoted_line') or ''}\n{ask.get('body') or ''}"
    try:
        from cora import phi_guard
        for name in ("is_any_phi", "is_phi_risk"):
            fn = getattr(phi_guard, name, None)
            if callable(fn) and fn(text):
                return True
        return False
    except Exception:  # noqa: BLE001 -- an unavailable screen must exclude
        print("  WARN PHI screen unavailable -- skipping this ask", file=sys.stderr)
        return True


def _entity_for(transcript: dict) -> str:
    """The entity this meeting belongs to.

    THE LEX TEST COMES FIRST AND USES THE SHARED DETECTOR. `_classify_entity` is
    title-keyword matching that DEFAULTS TO FNDR, so a Lexington meeting whose
    title carries no LEX keyword came out as FNDR -- and then nothing was
    PHI-scrubbed, because every scrub downstream keys on the entity starting with
    "LEX". `classify_lex_meeting` is the detector that exists precisely because
    the name-only version missed real meetings (it reads attendee email DOMAINS;
    see the 2026-06-18 D-054 entry, "a Lexington email-DOMAIN LEX signal --
    name-only detector missed Jen/Aaron meetings").
    """
    title = (transcript.get("title") or "").strip()
    try:
        verdict = ffc.classify_lex_meeting(transcript)
        if verdict.is_lex:
            return verdict.sub_entity or "LEX"
    except Exception:  # noqa: BLE001 -- an unavailable detector must not mislabel
        # Fail toward LEX rather than toward FNDR: mislabelling a LEX meeting as
        # FNDR turns off the scrub, which is the direction that leaks.
        log_line = "meeting-ask: LEX detector unavailable -- treating as LEX"
        print(f"  WARN {log_line}", file=sys.stderr)
        return "LEX"
    return ffc._classify_entity(title)


def _meeting_date(transcript: dict) -> str:
    ts = ffc._parse_date(transcript.get("date"))
    if not ts:
        return "unknown date"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def process_transcript(transcript: dict, *, dry_run: bool,
                       email_to_slack: dict[str, str], client=None) -> dict:
    """Detect, route and (unless dry_run) card one transcript's asks."""
    out = {"asks": 0, "carded": 0, "skipped_dup": 0, "no_recipient": 0,
           "overflow": 0, "phi_skipped": 0, "excluded": ""}
    reason = _excluded(transcript)
    if reason:
        out["excluded"] = reason
        return out

    transcript_id = str(transcript.get("id") or "")
    asks = meeting_asks.detect_asks(transcript.get("sentences"))
    out["asks"] = len(asks)
    if not asks:
        return out

    # CAP BEFORE DEDUP, DELIBERATELY -- and this is the second time this code has
    # been written, because I got it wrong in between.
    #
    # The review found that capping first means a 4th ask is never carded at all,
    # and proposed deduping first so the freed slots go to it. I made that change,
    # and the adversarial verifier was right to refuse it: deduping first converts
    # a HARD CAP into a RATE LIMIT. A meeting with ten detections would then card
    # three per run, and at a 15-minute interval all ten arrive inside an hour --
    # which is precisely the flood the cap exists to stop, and precisely the
    # incident that retired the last push ("Demi's 14 unwanted tasks", D-054). The
    # cap has to bound the TOTAL, not the rate.
    #
    # So the ordering stands and the WORDING is what was wrong: over-cap asks are
    # DROPPED, not queued. That is now what the code and the message both say.
    # A meeting that trips the cap is far likelier to be a detector false-positive
    # storm than a meeting containing four separate spoken requests, and the
    # honest response to a suspected storm is to card a bounded sample and say so.
    #
    # Actionable kinds first, which IS a real improvement and costs the cap
    # nothing: a meeting with three "draft X" asks and one "make a task" ask must
    # not spend all three slots on the buttonless ones. Stable sort, so ties keep
    # transcript order and the same three are chosen on every run -- which is what
    # makes the dedup below a no-op rather than a rotation.
    asks = sorted(asks, key=lambda a: 0 if a["kind"] in
                  (meeting_asks.KIND_TASK, meeting_asks.KIND_NOTE) else 1)
    asks, overflow = meeting_asks.cap_overflow(asks)
    out["overflow"] = overflow
    for a in asks:
        a["_ask_id"] = meeting_asks.ask_key(
            transcript_id, a["quoted_line"], a["start_time"])
    # Now drop the ones already carded on a previous run.
    kept = []
    for a in asks:
        if meeting_asks.already_carded(a["_ask_id"]):
            out["skipped_dup"] += 1
            continue
        kept.append(a)
    asks = kept
    if overflow:
        # REPORTED, never silent: a cap that truncates quietly reads as coverage.
        print(f"  NOTE: {overflow} further ask(s) in this meeting were DROPPED "
              f"(not queued) by the per-meeting cap of "
              f"{meeting_asks.MAX_ASKS_PER_MEETING}. A meeting over the cap is "
              f"more likely a detector false-positive storm than four separate "
              f"spoken requests -- use --transcript-id to inspect it.")

    health = ffd.assess(transcript)
    entity = _entity_for(transcript)
    title = (transcript.get("title") or "").strip()
    date_str = _meeting_date(transcript)

    for ask in asks:
        ask_id = ask["_ask_id"]

        # PHI IS SCREENED HERE, BEFORE THE DM -- not at tap time. The card carries
        # the VERBATIM transcript sentence (D-136's grounding requirement), so the
        # PHI reaches the recipient the moment the card is posted; the D-050 gate
        # inside `save_meeting_ask_note` runs only when somebody TAPS, which is
        # far too late to matter. A PHI-shaped sentence is dropped outright rather
        # than routed to a custodian: a proposal is not worth a PHI egress, and
        # the meeting's own content is still in the KB behind the normal gates.
        if _phi_flagged(ask):
            out["phi_skipped"] += 1
            print(f"  SKIP (PHI-flagged sentence -- not carded): "
                  f"[{meeting_asks.format_offset(ask['start_time'])}] "
                  f"{ask['quoted_line'][:60]}...")
            continue

        sid, email, why = meeting_asks.resolve_addressee(
            ask, transcript,
            attribution_unreliable=health.collapsed,
            email_to_slack=email_to_slack,
        )
        if not sid:
            # No addressable recipient => no proposal. A card with no recipient is
            # not a proposal, and guessing one is how the wrong colleague gets a
            # DM about somebody else's meeting.
            out["no_recipient"] += 1
            print(f"  SKIP (no Slack recipient; owner={email or 'unknown'}): "
                  f"[{meeting_asks.format_offset(ask['start_time'])}] "
                  f"{ask['quoted_line'][:90]}")
            continue

        rec = {
            "ask_id": ask_id, "transcript_id": transcript_id,
            "meeting_title": title, "meeting_date": date_str, "entity": entity,
            "kind": ask["kind"], "body": ask["body"],
            "quoted_line": ask["quoted_line"], "start_time": ask["start_time"],
            "speaker": ask["speaker"], "addressee_id": sid,
            "addressee_email": email, "routing_reason": why,
        }
        text = meeting_asks.build_card_text(rec)
        if dry_run:
            print(f"  WOULD CARD -> {sid} ({email}) [{ask['kind']}]")
            print("  " + text.replace("\n", "\n  "))
            out["carded"] += 1
            continue

        posted = _post_card(client, sid, rec, text)
        if posted:
            out["carded"] += 1
        else:
            out["no_recipient"] += 1
    return out


def _post_card(client, slack_id: str, rec: dict, text: str) -> bool:
    """DM one card. Returns False on failure WITHOUT recording it, so the next
    run retries rather than losing the ask."""
    from cora.slack_egress import sanitize_text
    from cora import meeting_asks as ma

    kind = str(rec.get("kind") or "")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": sanitize_text(text)}}]
    if kind in (ma.KIND_TASK, ma.KIND_NOTE):
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Yes, do it"},
                 "style": "primary", "action_id": ma.ACTION_ACCEPT,
                 "value": rec["ask_id"]},
                {"type": "button", "text": {"type": "plain_text", "text": "No, drop it"},
                 "action_id": ma.ACTION_DISMISS, "value": rec["ask_id"]},
            ],
        })
    try:
        opened = client.conversations_open(users=slack_id)
        channel = (opened.get("channel") or {}).get("id") or ""
        resp = client.chat_postMessage(
            channel=channel,
            text=sanitize_text(text),
            blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
        ma.record_card(
            dm_channel_id=channel,
            card_message_ts=str(resp.get("ts") or ""),
            **{k: rec[k] for k in (
                "ask_id", "transcript_id", "meeting_title", "meeting_date",
                "entity", "kind", "body", "quoted_line", "start_time",
                "speaker", "addressee_id", "addressee_email", "routing_reason")},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR posting card to {slack_id}: {exc}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and print; send no DM, advance no watermark")
    ap.add_argument("--lookback-hours", type=int, default=None,
                    help=f"override the poll window (default {_DEFAULT_LOOKBACK_HOURS}h "
                         "or the watermark, whichever is newer)")
    ap.add_argument("--transcript-id", default="",
                    help="scan exactly one transcript (implies no watermark move)")
    ap.add_argument("--max-meetings", type=int, default=10)
    args = ap.parse_args(argv)

    if not os.environ.get("FIREFLIES_API_KEY"):
        print("FIREFLIES_API_KEY not set -- nothing to do.", file=sys.stderr)
        return 1

    run_start = int(time.time())
    if args.transcript_id:
        transcripts = _fetch_one(args.transcript_id)
    else:
        since = _window_start(_read_watermark(),
                              args.lookback_hours or _DEFAULT_LOOKBACK_HOURS)
        try:
            transcripts = _fetch_since(since, args.max_meetings)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: Fireflies query failed: {exc}", file=sys.stderr)
            return 1

    print(f"meeting-ask capture: {len(transcripts)} transcript(s) in window "
          f"(dry_run={args.dry_run})")

    client = None
    if not args.dry_run:
        from slack_sdk import WebClient
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token:
            print("SLACK_BOT_TOKEN not set -- cannot send cards.", file=sys.stderr)
            return 1
        client = WebClient(token=token)

    email_to_slack = ffc._load_email_to_slack()
    totals = {"asks": 0, "carded": 0, "skipped_dup": 0, "no_recipient": 0,
              "overflow": 0, "phi_skipped": 0, "excluded": 0}
    for t in transcripts:
        title = (t.get("title") or "").strip() or "(untitled)"
        print(f"- {title}")
        res = process_transcript(t, dry_run=args.dry_run,
                                 email_to_slack=email_to_slack, client=client)
        if res["excluded"]:
            print(f"  excluded: {res['excluded']}")
            totals["excluded"] += 1
        for k in ("asks", "carded", "skipped_dup", "no_recipient", "overflow",
                  "phi_skipped"):
            totals[k] += res[k]

    if not args.dry_run and not args.transcript_id:
        _write_watermark(run_start)

    print(f"done: {totals['asks']} ask(s) detected, {totals['carded']} carded, "
          f"{totals['skipped_dup']} already carded, "
          f"{totals['no_recipient']} unaddressable, "
          f"{totals['phi_skipped']} PHI-skipped, "
          f"{totals['overflow']} over cap, {totals['excluded']} meeting(s) excluded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
