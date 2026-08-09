#!/usr/bin/env python3
"""Step-7.5 queue reconciliation for the v2b S5 Class-B batch
(branch claude/slack-buttons-v2b-classb).

Run AFTER Harrison FF-merges the branch. Idempotent; dry-run by default;
--apply to write.

Closed by this branch (1):
  * cq-b5460ae7aca3 -- meeting_action_items per-item Slack confirm cards. The
    preview now stashes the asker's verified item list server-side and posts one
    Confirm/Skip card per item, valued "{stash_id}:{item_index}". The
    confirmed=true re-call idiom still works and is now filtered against that
    verified list rather than the meeting's whole action-items blob.

DELIBERATELY LEFT OPEN (2 named in the kickoff as "fix only if they fall out
naturally" -- neither did; both are model-layer, not staged-write-layer):
  * cq-904f849bc59a -- cora_remember phantom preview on Sonnet. The fix is the
    tool_choice force pattern at the PREVIEW turn, which is a router change, not
    a stash change. This branch touched no preview-turn routing.
  * cq-8866d3f7ac3b -- lexicon teach misroutes to cora_remember with
    confirmed=True on the first call. VERIFIED still holding on this branch: the
    F-23 no-stash refusal means that first call finds no pending and refuses
    honestly rather than writing. The misroute itself is a router/prompt issue.

SEEDED by this script (1) -- the v2b kickoff's optional rider, assessed and
declined as NOT trivially cheap:
  * Extend the S7 verbatim ambiguity scan (lexicon.find_ambiguous_in_text) to the
    Shopify BULK write path. The single-item path's version carries a D-051
    lens-3 HIGH guard: the override may fire ONLY when the model resolved EXACT
    to one of the meanings the user's own phrase is ambiguous between, because a
    second, unrelated product named in the same sentence would otherwise hijack
    the request. A bulk request names several products in ONE verbatim message by
    construction, which is precisely that hazard, so per-row attribution needs
    its own design and its own tests rather than a ride on this branch.

Standalone script -- does NOT import the bot process; no restart needed.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue  # noqa: E402

SHIPS: list[tuple[str, str]] = [
    ("cq-b5460ae7aca3", "meeting_action_items per-item confirm cards"),
]

LEFT_OPEN: list[tuple[str, str]] = [
    ("cq-904f849bc59a", "cora_remember phantom preview -- model-layer, needs the tool_choice force"),
    ("cq-8866d3f7ac3b", "lexicon teach misroute -- router-layer; the F-23 no-stash refusal still holds"),
]

SEEDS = [
    {
        "kind": "bug",
        "severity": "LOW",
        "title": "Shopify BULK write path: verbatim ambiguity scan not applied per row",
        "summary": (
            "v2 S7 made the lexicon ask-on-ambiguity judge the USER's verbatim words "
            "instead of the model's rewritten tool arg, closing the rewrite bypass on "
            "the SINGLE-item inventory write. The BULK rows[] path still judges only "
            "the model-normalized arg, so the same bypass survives there. Not a "
            "mechanical copy: the single-item override is guarded to fire ONLY when "
            "the model resolved EXACT to one of the meanings the user's own phrase is "
            "ambiguous between (a D-051 lens-3 HIGH -- an unrelated second product "
            "named in the same sentence otherwise hijacks the request and stashes an "
            "ask carrying the WRONG row's location and quantity). A bulk request names "
            "several products in one message by construction, so this needs per-row "
            "attribution of the verbatim phrase, plus eval-golden-set coverage for the "
            "multi-product bypass class."
        ),
        "entity": "F3E",
        "signal": "v2b_s5_declined_rider",
        "status": "PROPOSED",
        "subsystem_guess": "tool_dispatch/lexicon",
    },
    {
        "kind": "bug",
        "severity": "MEDIUM",
        "title": "Confirm cards render model-facing directive prose for 3 Class-B kinds",
        "summary": (
            "On a button tap the card text is the executor's return value with the "
            "WRITE_CONFIRMED sentinel stripped. Three kinds return no sentinel and "
            "instead return prose addressed to the MODEL, so the card shows the user "
            "text like 'Surface this to the user:' and 'Format the Drafts link as a "
            "Slack hyperlink (preserve the <url|name> syntax)': gmail_draft (via "
            "gmail_client.format_created_draft_for_llm), influencer_handle (inline "
            "string), influencer_deliverable (via "
            "influencer_client.format_logged_deliverable_for_llm). Cosmetic, no data "
            "or authorization impact, and the TYPED path is unaffected because the "
            "model paraphrases the same string. The fix is a clean user-facing half "
            "per executor, which touches formatters shared with other callers -- "
            "deliberately NOT rushed into the v2b remediation, since the v2 bundle's "
            "own lesson is that six of ten defects came from its late fixes. Doing it "
            "also makes those kinds eligible for _CONTRACT_WRITE_TOOLS."
        ),
        "entity": "HJRG",
        "signal": "v2b_s5_review_deferred",
        "status": "PROPOSED",
        "subsystem_guess": "tool_dispatch/confirm_cards",
    },
    {
        "kind": "improvement",
        "severity": "MEDIUM",
        "title": "slack_send_dm approval surface is model-authored (not in _CONTRACT_WRITE_TOOLS)",
        "summary": (
            "slack_send_dm now has a real preview that renders the SERVER-RESOLVED "
            "recipient, which is what lets a human catch a mis-resolution before a "
            "message goes to another person. But the tool is not in "
            "_CONTRACT_WRITE_TOOLS, so that preview is a prompt request rather than "
            "code-enforced: the model may paraphrase or drop the 'To:' line, and the "
            "confirm card is built from the model's reply rather than the tool's own "
            "text. The preview turn also has no pending yet, so the Sonnet-force chain "
            "does not fire and the turn can run on Haiku -- the exact shape the S4 "
            "finding recorded Haiku fabricating preview-shaped text for. Both returns "
            "are already clean sentinel-wrapped text, so membership is close to a "
            "one-line change, but it converts every turn of this tool to verbatim "
            "posting and wants its own tests. D-034 argues for it: this is the only "
            "tool that messages a third party."
        ),
        "entity": "HJRG",
        "signal": "v2b_s5_review_deferred",
        "status": "PROPOSED",
        "subsystem_guess": "claude_client/tool_dispatch",
    },
    {
        "kind": "bug",
        "severity": "MEDIUM",
        "title": "phi_guard precision: programme-id adjacency gap + bare business words",
        "summary": (
            "Two measured precision faults surfaced while choosing the outbound-DM "
            "screen. (1) RECALL: _PROGRAM_ID_TAIL_RE re-admits a beneficiary number "
            "only when it sits IMMEDIATELY after the programme name, so "
            "is_phi_risk_person_linked returns False for \"Marcus's AHCCCS is "
            "84213365\" and \"His Medicaid, ID 84213365, expires next month\" -- the "
            "word 'is' and a comma both break it. That predicate is documented as the "
            "one to use on request-shaped text, so every such consumer inherits the "
            "gap. (2) PRECISION: is_phi_risk fires on the bare words 'assessment' and "
            "'incident report', which are ordinary business language ('the Q3 revenue "
            "assessment', 'the incident report from the checkout outage'), and that "
            "leaks through every predicate built on it. slack_send_dm currently takes "
            "is_any_phi and accepts three known false refusals as the fail-safe "
            "trade-off for an irreversible send; sharpening these would remove them "
            "without weakening recall. Pinned in tests/test_slack_send_dm_staged.py "
            "(TestPhiAndLexFloor) so a fix shows up there as a deliberate change."
        ),
        "entity": "LEX",
        "signal": "v2b_s5_review_deferred",
        "status": "PROPOSED",
        "subsystem_guess": "phi_guard",
    },
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the shipped events + the seed (default: dry-run).")
    args = ap.parse_args()

    shipped = 0
    for cid, why in SHIPS:
        rec = code_queue.get_item(cid)
        if not rec:
            print(f"SKIP (missing id): {cid}")
            continue
        if rec.get("status") == "SHIPPED":
            print(f"SKIP (already shipped): {cid}")
            continue
        line = f"{cid} [{rec.get('status')}] '{rec.get('title', '')[:60]}'  <- {why}"
        if args.apply:
            code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cid, code_queue.HARRISON_ID)
            print(f"SHIPPED: {line}")
            shipped += 1
        else:
            print(f"WOULD SHIP: {line}")

    print("\nLeft OPEN on purpose:")
    for cid, why in LEFT_OPEN:
        rec = code_queue.get_item(cid)
        status = rec.get("status") if rec else "missing"
        print(f"  {cid} [{status}] <- {why}")

    print("\nSeeded for a later session (1 declined rider + 3 D-051 deferrals):")
    for seed in SEEDS:
        if args.apply:
            # seed_item is idempotent on fingerprint, so a re-run does not duplicate.
            new_id = code_queue.seed_item(**seed)
            print(f"  SEEDED: {new_id} -- {seed['title']}")
        else:
            print(f"  WOULD SEED: [{seed['severity']}] {seed['title']}")

    if args.apply:
        print(f"\nDone. Shipped {shipped}.")
        try:
            code_queue.render_backlog()
            print("Backlog regenerated.")
        except Exception as exc:  # noqa: BLE001
            print(f"(backlog render skipped: {exc})")
    else:
        print("\nDry-run. Re-run with --apply AFTER the branch is merged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
