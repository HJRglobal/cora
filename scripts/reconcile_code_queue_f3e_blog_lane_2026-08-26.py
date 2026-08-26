"""Step 7.5 queue reconciliation for the 2026-08-26 F3E blog publish lane.

Transitions ONLY the seed this branch actually closed. Dry-run by default;
``--apply`` writes through ``code_queue.process_queue_action`` -- never by
hand-editing the jsonl or the backlog (loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers shipped
work. The inverse failure is quieter and just as bad -- marking a seed SHIPPED
because part of it landed -- so the note below records exactly what shipped.

    SHIPPED
      cq-2577936d2809  F3E blog one-tap publish lane (News+Learn pipeline target
                       state). Delivered in full: S1 article ops with
                       source-level write-safety pins, S2 the weekly Learn draft
                       job, S3 the deterministic 11-rail claims preflight
                       mirroring the human 14-rail checklist, S4 the Harrison-only
                       one-tap publish card with a two-stage read-back, and S5 the
                       News lane.

                       S5 shipped LARGER than the seed asked for. The kickoff said
                       Cora's press-tracker read access was unconfirmed and told
                       the session not to assume it, with a typed-ask fallback as
                       v1 if no lane existed. A live probe found the lane already
                       wired (notion_client._PRESS_DB_ID, 211 rows, 2 Published),
                       so the real weekly sweep was built instead and the
                       follow-up seed the kickoff anticipated is NOT needed.

                       Not built, and deliberately: the "Cora, stage a news post
                       about X" typed ask. It existed only as the fallback for the
                       no-read-lane case that did not materialise. If Harrison
                       wants ad-hoc news staging as well as the sweep, that is a
                       new ask, not an unfinished part of this one.

    NEW FOLLOW-UP FOUND WHILE BUILDING (seed separately, not closed here)
      Four PUBLISHED legacy News articles apply clean/cleaner language to the F3
      Energy line, and six carry em-dashes. Found by running the new preflight
      over all 12 live articles. The 2026-08-26 legacy claims audit fixed one
      summary instance and missed these bodies. This lane never edits published
      articles by design, so it is not fixed here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402

SHIPS: list[tuple[str, str]] = [
    ("cq-2577936d2809",
     "F3E blog one-tap publish lane: S1-S5 all delivered; S5 built as the real "
     "press sweep after the read-lane premise was overturned"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the shipped events (default: dry-run).")
    args = ap.parse_args()

    shipped = 0
    for cid, why in SHIPS:
        rec = code_queue.get_item(cid)
        if not rec:
            print("SKIP (missing id): %s" % cid)
            continue
        if rec.get("status") == "SHIPPED":
            print("SKIP (already shipped): %s" % cid)
            continue
        line = "%s [%s] %r  <- %s" % (cid, rec.get("status"),
                                      (rec.get("title") or "")[:60], why)
        if args.apply:
            code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cid, code_queue.HARRISON_ID)
            print("SHIPPED: %s" % line)
            shipped += 1
        else:
            print("WOULD SHIP: %s" % line)

    if args.apply:
        print("\nDone. Shipped %d." % shipped)
        try:
            code_queue.render_backlog()
            print("Backlog regenerated.")
        except Exception as exc:  # noqa: BLE001
            print("(backlog render skipped: %s)" % exc)
    else:
        print("\nDry run. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
