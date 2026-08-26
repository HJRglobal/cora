"""Weekly F3E blog pipeline: draft -> preflight -> stage unpublished -> card.

Scheduled task: "Cora - F3E Blog Pipeline", Monday 08:50 AZ.

    --dry-run          draft and preflight, stage NOTHING (the rollout gate)
    --learn-only       skip the News sweep
    --news-only        skip the Learn draft
    --ack-checklist    record the current claims-checklist fingerprint as
                       reviewed, re-arming staging after a drift block
    --no-alert         print the report, do not post it to #f3-marketing

Nothing here can publish. `shopify_client.publish_article` is reachable only from
the Slack confirm-tap handler, and the suite pins that this pipeline's source does
not even name it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora.f3e_blog import pipeline, publish_cards  # noqa: E402


def _setup_logging() -> None:
    from datetime import datetime, timedelta, timezone
    az = timezone(timedelta(hours=-7))
    log_dir = _REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(
                log_dir / ("f3e-blog-pipeline-%s.log"
                           % datetime.now(az).strftime("%Y-%m-%d")),
                encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _post_report(text: str) -> None:
    """Post the run report to #f3-marketing. Fail-soft.

    #f3-hq does not exist in the workspace -- the interim task's first fire proved
    it, and Harrison named #f3-marketing on 2026-08-26.
    """
    try:
        client = publish_cards._default_client_factory()  # noqa: SLF001
        if client is None:
            print("(no Slack token -- report not posted)")
            return
        client.chat_postMessage(
            channel=publish_cards.MARKETING_CHANNEL,
            text="*F3E blog pipeline*\n" + text,
            unfurl_links=False, unfurl_media=False,
        )
    except Exception as exc:  # noqa: BLE001
        print("(report post failed: %s)" % exc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--learn-only", action="store_true")
    ap.add_argument("--news-only", action="store_true")
    ap.add_argument("--ack-checklist", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args()

    _setup_logging()

    if args.ack_checklist:
        fingerprint = pipeline.ack_checklist()
        print("Claims checklist acknowledged at fingerprint %s. Staging is "
              "re-armed for the next run." % fingerprint)
        return 0

    if args.news_only:
        report = pipeline.run_news(dry_run=args.dry_run)
    elif args.learn_only:
        report = pipeline.run_learn(dry_run=args.dry_run)
    else:
        report = pipeline.run_weekly(dry_run=args.dry_run)

    text = report.render()
    print(text)
    if not args.no_alert and not args.dry_run:
        _post_report(text)

    # A drift block is a real stop that someone must act on, so it exits non-zero
    # and shows up as a failed task rather than as a quiet success.
    return 1 if report.drift_blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
