#!/usr/bin/env python3
"""Monday 09:00 AZ -- build and deliver the weekly finance close-support pack.

WHAT THIS DOES
--------------
Builds the deterministic pack (``cora.finance_close.build_pack`` -- every figure
computed in Python, never by a model) and delivers three cuts:

  * FULL pack   -> #hjrg-finance   (C0B3V5SDNAG)
  * FULL pack   -> DM Justin Moran (U0B3AEJCYGP)
  * FOUNDER cut -> #founder-finance (C0BCXPJDP42)   -- flagged items only

CHANNEL TARGETS ARE HARDCODED AND FINANCE-ONLY
----------------------------------------------
#hjr-finance (C0BAK65N4TA) is ARCHIVED -- verified via the Slack API 2026-08-04 --
so posting there fails ``is_archived``, which is precisely the silent-failure mode
the finance-receipt digest logged through July (W4-02). This job therefore targets
#hjrg-finance, which is live and classifies TIER_1 (``channel_classifier``:
function "finance"), so the finance firewall's intent is preserved.

Every delivery target passes through ``_assert_finance_surface`` before a single
byte is sent. There is NO fallback to a non-finance channel: if a finance post
fails, the notice that goes to the ops channel is METADATA-ONLY (counts + reason +
fix, never a figure), so a delivery break is loud without leaking financial
content off a finance surface.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_finance_close_pack.py --dry-run
    .venv\\Scripts\\python.exe scripts\\run_finance_close_pack.py
    .venv\\Scripts\\python.exe scripts\\run_finance_close_pack.py --force
    .venv\\Scripts\\python.exe scripts\\run_finance_close_pack.py --entities F3E,OSNGW

Registered as Windows Task Scheduler task: cowork-cora-finance-close-pack
Schedule: Monday 09:00 AZ (after the weekly cash flow refresh).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _REPO_ROOT / "logs"
            / f"finance-close-pack-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("finance-close-pack")

# ── delivery targets (hardcoded; finance surfaces only) ──────────────────────

# #hjrg-finance -- live, private, TIER_1 (function "finance"). Full pack.
HJRG_FINANCE_CHANNEL = "C0B3V5SDNAG"
# #founder-finance -- live, TIER_1 founder finance surface. Founder cut.
FOUNDER_FINANCE_CHANNEL = "C0BCXPJDP42"
# Justin Moran -- full pack by DM.
JUSTIN_SLACK_ID = "U0B3AEJCYGP"

# The closed allowlist. A channel id absent from this set can never receive pack
# content -- see _assert_finance_surface. Keyed by id AND carrying the name so the
# guard test can assert the name classifies TIER_1.
FINANCE_SURFACES: dict[str, str] = {
    HJRG_FINANCE_CHANNEL: "hjrg-finance",
    FOUNDER_FINANCE_CHANNEL: "founder-finance",
}

# ARCHIVED 2026-08-04. Pinned here ONLY so the guard test can assert it is never a
# delivery target -- posting to it fails is_archived and reaches nobody.
ARCHIVED_HJR_FINANCE = "C0BAK65N4TA"

_DEDUP_PATH = _REPO_ROOT / "data" / "cache" / "finance-close-pack-sent.json"


class DeliveryTargetError(RuntimeError):
    """A non-finance surface was offered pack content. Never caught -- it is a bug."""


def _assert_finance_surface(channel_id: str) -> None:
    """Refuse to send pack content anywhere but a known-live finance channel."""
    if channel_id not in FINANCE_SURFACES:
        raise DeliveryTargetError(
            f"refusing to post finance close-support content to channel {channel_id!r}: "
            f"not in the finance-surface allowlist {sorted(FINANCE_SURFACES)}"
        )


def _ops_alert_channel() -> str:
    """Where a DELIVERY-FAILURE notice goes. NOT a finance surface -- see W4-02.

    The notice is metadata-only by construction (``_delivery_failure_notice``), so
    routing it to an ops channel leaks no financial content.
    """
    return (
        os.environ.get("FINANCE_DIGEST_FALLBACK_CHANNEL", "").strip()
        or os.environ.get("HEALTH_REPORT_CHANNEL", "").strip()
        or "hjrg-leadership"
    )


# ── founder cut ──────────────────────────────────────────────────────────────

def build_founder_cut(pack) -> str:
    """Flagged-items-only view for #founder-finance.

    Deterministic slice of the same computed lines -- it re-renders nothing and
    recomputes nothing. Section headings are kept so a flag is always attributable,
    and unavailable sections are named so "no flags" can never be mistaken for
    "everything checked out".
    """
    from cora.finance_close import Section  # noqa: PLC0415

    lines = [
        ":ledger: *Finance close-support — founder cut*",
        f"_Generated {pack.generated_at}. Flagged items only; "
        "the full pack is in #hjrg-finance._",
        "",
    ]
    flagged_any = False
    for section in pack.sections:
        if not isinstance(section, Section):
            continue
        if not section.available:
            lines.append(f"*{section.title}* — _unavailable: {section.stub_reason}_")
            continue
        hits = [
            ln for ln in section.lines
            if ":triangular_flag_on_post:" in ln or ":rotating_light:" in ln or ":warning:" in ln
        ]
        if not hits:
            continue
        flagged_any = True
        lines.append(f"*{section.title}*")
        lines.extend(f"  {h}" for h in hits)
    if not flagged_any:
        lines.append("_No item crossed a flag threshold this week._")
    lines.append("")
    lines.append(f"_{pack.total_flags} item(s) flagged across {len(pack.sections)} section(s)._")
    return "\n".join(lines)


def _delivery_failure_notice(failed: list[str], n_flags: int) -> str:
    """METADATA-ONLY failure notice. Must contain no money figure of any kind.

    Pinned by a test that runs channel_content_guard's money-figure detector over
    this string -- the ops channel is not a finance surface.
    """
    return (
        ":warning: *Weekly finance close-support pack could not be fully delivered.* "
        f"The pack was built (flag count: {n_flags}) but delivery failed for: "
        f"{', '.join(failed)}. The channel may be archived, or Cora may not be a "
        "member. *Fix:* confirm those surfaces are live and Cora is in them, or "
        "repoint the target constants in scripts/run_finance_close_pack.py "
        "(FINANCE_SURFACES is a closed allowlist -- adding a channel there is a "
        "deliberate finance-firewall decision). No figures are included in this "
        "notice by design."
    )


# ── dedup ────────────────────────────────────────────────────────────────────

def _iso_week(today: datetime.date | None = None) -> str:
    day = today or datetime.date.today()
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _already_sent(week: str) -> bool:
    try:
        data = json.loads(_DEDUP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("last_week") == week


def _mark_sent(week: str) -> None:
    _DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DEDUP_PATH.write_text(
        json.dumps({"last_week": week, "sent_at": datetime.datetime.now().isoformat()}),
        encoding="utf-8",
    )


# ── Slack delivery ───────────────────────────────────────────────────────────

def _sanitized(text: str) -> str:
    """Egress boundary + Slack-bold normalization.

    This script imports cora, so the class-level WebClient patch installs in-process
    (B1); sanitizing explicitly as well is belt-and-suspenders on the highest-stakes
    egress surface in the system and satisfies the CI guard on its own terms.
    """
    from cora.reply_formatter import normalize_slack_bold  # noqa: PLC0415
    from cora.slack_egress import sanitize_text  # noqa: PLC0415
    return normalize_slack_bold(sanitize_text(text))


def post_to_channel(client, channel_id: str, text: str) -> bool:
    """Post pack content to an allowlisted finance channel. True on success."""
    _assert_finance_surface(channel_id)
    try:
        client.chat_postMessage(
            channel=channel_id, text=_sanitized(text),
            unfurl_links=False, unfurl_media=False,
        )
        log.info("close-pack: posted to %s (%s)", FINANCE_SURFACES[channel_id], channel_id)
        return True
    except Exception as exc:  # noqa: BLE001 -- one dead surface must not kill the rest
        log.error("close-pack: post to %s failed: %s", channel_id, exc)
        return False


def dm_user(client, user_id: str, text: str) -> bool:
    """DM the full pack. A DM is the user's own private surface, not a channel."""
    try:
        opened = client.conversations_open(users=[user_id])
        dm_channel = opened["channel"]["id"]
        client.chat_postMessage(
            channel=dm_channel, text=_sanitized(text),
            unfurl_links=False, unfurl_media=False,
        )
        log.info("close-pack: DM sent to %s", user_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("close-pack: DM to %s failed: %s", user_id, exc)
        return False


def post_ops_alert(client, failed: list[str], n_flags: int) -> None:
    notice = _delivery_failure_notice(failed, n_flags)
    try:
        client.chat_postMessage(
            channel=_ops_alert_channel(), text=_sanitized(notice),
            unfurl_links=False, unfurl_media=False,
        )
        log.info("close-pack: delivery-failure notice posted to #%s", _ops_alert_channel())
    except Exception as exc:  # noqa: BLE001
        log.error("close-pack: delivery-failure notice ALSO failed: %s", exc)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print both cuts; post nothing and write no snapshot")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the once-per-ISO-week dedup")
    parser.add_argument("--entities", default="",
                        help="Comma-separated QBO entity codes to limit the run (testing)")
    args = parser.parse_args()

    from cora import finance_close  # noqa: PLC0415

    week = _iso_week()
    log.info("=== finance close-support pack starting (week=%s dry_run=%s force=%s) ===",
             week, args.dry_run, args.force)

    if not args.dry_run and not args.force and _already_sent(week):
        log.info("close-pack: already sent for %s -- skipping (use --force to override)", week)
        return 0

    entities = [e.strip().upper() for e in args.entities.split(",") if e.strip()] or None

    pack = finance_close.build_pack(
        entities=entities,
        # A dry run must not advance the WoW baseline, or the next real run would
        # diff against a snapshot nobody ever saw and report zero movement.
        persist_snapshot=not args.dry_run,
    )
    full = pack.render()
    narration = finance_close.narrate(pack)
    if narration:
        full = f"_{narration}_\n\n{full}"
    founder = build_founder_cut(pack)

    log.info("close-pack: built -- %d flag(s), %d unavailable section(s)",
             pack.total_flags, len(pack.unavailable_sections))

    if args.dry_run:
        print("\n" + "=" * 72)
        print(f"[DRY RUN] FULL PACK -> #hjrg-finance ({HJRG_FINANCE_CHANNEL}) + DM Justin")
        print("=" * 72)
        print(full)
        print("\n" + "=" * 72)
        print(f"[DRY RUN] FOUNDER CUT -> #founder-finance ({FOUNDER_FINANCE_CHANNEL})")
        print("=" * 72)
        print(founder)
        return 0

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("close-pack: SLACK_BOT_TOKEN not set -- cannot deliver")
        return 1

    from slack_sdk import WebClient  # noqa: PLC0415
    client = WebClient(token=bot_token)

    failed: list[str] = []
    if not post_to_channel(client, HJRG_FINANCE_CHANNEL, full):
        failed.append("#hjrg-finance")
    if not dm_user(client, JUSTIN_SLACK_ID, full):
        failed.append("DM Justin")
    if not post_to_channel(client, FOUNDER_FINANCE_CHANNEL, founder):
        failed.append("#founder-finance")

    if failed:
        post_ops_alert(client, failed, pack.total_flags)

    # Dedup marks on ANY successful delivery: a partial success plus a re-run would
    # double-post to the surfaces that already worked. The ops notice is the signal
    # for the ones that did not.
    if len(failed) < 3:
        _mark_sent(week)

    log.info("=== finance close-support pack complete (%d/3 delivered) ===", 3 - len(failed))
    return 1 if len(failed) == 3 else 0


if __name__ == "__main__":
    sys.exit(main())
