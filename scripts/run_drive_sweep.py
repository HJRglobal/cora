#!/usr/bin/env python3
"""Drive sweep — scheduled entry point.

For every account in data/maps/monitored-email-accounts.yaml with
drive_sweep: true and dwd_eligible: true, impersonates the user via
Domain-wide Delegation, enumerates all Drive files modified within the
freshness window, filters noise with Claude Haiku, and embeds surviving
content into Cora's KB.

Usage (called by Windows Task Scheduler — see deployment/setup-drive-sweep-task.ps1):
    .venv\\Scripts\\python.exe scripts\\run_drive_sweep.py

Options:
    --dry-run             Classify and log what would be ingested; write nothing to KB.
    --freshness-days N    Look back N days on first run / backfill (default: 730).
    --only-email EMAIL    Sweep a single account (useful for testing / manual backfill).
    --backfill            Ignore watermarks -- re-sweep all files in the freshness window.
    --with-slack          Post a summary message to #cora-drive-sweep after the run.

Environment variables required (already in .env if Cora is running):
    GOOGLE_SERVICE_ACCOUNT_JSON   Path to service account JSON
    ANTHROPIC_API_KEY             For Claude Haiku classification

One-time Harrison action before first run:
    In admin.google.com --> Security --> API Controls --> Domain-wide Delegation
    --> edit the Cora SA entry --> add scope:
        https://www.googleapis.com/auth/drive.readonly
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

LOG_DIR = _REPO_ROOT / "logs"
_ACCOUNTS_YAML = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"drive-sweep-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _post_slack_summary(stats: dict, dry_run: bool, channel: str) -> None:
    """Post aggregate stats to a Slack channel after the sweep."""
    import anthropic  # noqa: F401 -- just check env is loaded
    try:
        from slack_sdk import WebClient
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token:
            return
        client = WebClient(token=token)
        mode = " *(dry-run)*" if dry_run else ""
        text = (
            f":file_folder: *Drive Sweep complete{mode}*\n"
            f"Accounts swept: {stats['accounts_swept']}\n"
            f"Files enumerated: {stats['files_enumerated']}\n"
            f"Files extracted: {stats['files_extracted']}\n"
            f"KB chunks ingested: {stats['chunks_ingested']}\n"
            f"PHI-guarded (Lex): {stats['phi_skipped']}\n"
            f"Noise-filtered: {stats['noise_filtered']}\n"
            f"Cross-user dedup skipped: {stats['dedup_skipped']}"
        )
        client.chat_postMessage(channel=channel, text=text)
    except Exception as exc:
        logging.getLogger("run_drive_sweep").warning(
            "Could not post Slack summary: %s", exc
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Classify only; don't write to KB"
    )
    parser.add_argument(
        "--freshness-days", type=int, default=730,
        help="How many days back to look for modified files (default: 730)"
    )
    parser.add_argument(
        "--only-email", default=None,
        help="Sweep only this email address"
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Ignore watermarks and re-sweep the full freshness window"
    )
    parser.add_argument(
        "--with-slack", action="store_true",
        help="Post summary to Slack after run (channel: DRIVE_SWEEP_NOTIFY_CHANNEL env var or cora-drive-sweep)"
    )
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("run_drive_sweep")
    log.info("=" * 60)
    log.info(
        "Drive sweep starting (dry_run=%s, freshness_days=%d, only_email=%s, backfill=%s)",
        args.dry_run, args.freshness_days, args.only_email or "all", args.backfill
    )

    # Validate environment
    sa_json_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_path or not Path(sa_json_path).exists():
        log.error(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set or file not found: %r", sa_json_path
        )
        return 1

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set")
        return 1

    # Open KB
    try:
        from cora.knowledge_base import KnowledgeBase
        kb_path = _REPO_ROOT / "data" / "cora_kb.db"
        kb = KnowledgeBase(kb_path)
    except Exception as exc:
        log.error("Could not open KB at %s: %s", kb_path, exc)
        return 1

    # Build Anthropic client (Haiku for classification)
    try:
        import anthropic
        anthropic_client = anthropic.Anthropic(api_key=anthropic_api_key)
    except Exception as exc:
        log.error("Could not build Anthropic client: %s", exc)
        return 1

    # If --backfill, wipe watermarks for targeted accounts before sweep
    if args.backfill:
        import yaml
        with open(_ACCOUNTS_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        accounts_to_backfill = [
            a["email"] for a in cfg.get("accounts", [])
            if a.get("enabled") and a.get("dwd_eligible") and a.get("drive_sweep")
        ]
        if args.only_email:
            accounts_to_backfill = [
                e for e in accounts_to_backfill if e == args.only_email
            ]
        for email in accounts_to_backfill:
            try:
                kb.set_sync_state(f"drive_sweep_{email}", None)
                log.info("Cleared watermark for %s (backfill mode)", email)
            except Exception:
                pass

    # Run the sweep
    from cora.connectors.drive_sweep import run_sweep

    try:
        stats = run_sweep(
            sa_json_path=sa_json_path,
            accounts_yaml_path=str(_ACCOUNTS_YAML),
            kb=kb,
            anthropic_client=anthropic_client,
            freshness_days=args.freshness_days,
            dry_run=args.dry_run,
            only_email=args.only_email,
        )
    except Exception as exc:
        log.exception("Drive sweep crashed: %s", exc)
        return 1
    finally:
        try:
            kb.close()
        except Exception:
            pass

    log.info(
        "Drive sweep DONE -- accounts=%d enumerated=%d extracted=%d "
        "ingested=%d phi=%d noise=%d dedup=%d",
        stats["accounts_swept"], stats["files_enumerated"],
        stats["files_extracted"], stats["chunks_ingested"],
        stats["phi_skipped"], stats["noise_filtered"], stats["dedup_skipped"],
    )

    if args.with_slack and not args.dry_run:
        channel = os.environ.get("DRIVE_SWEEP_NOTIFY_CHANNEL", "cora-drive-sweep")
        _post_slack_summary(stats, dry_run=args.dry_run, channel=channel)

    return 0


if __name__ == "__main__":
    sys.exit(main())
