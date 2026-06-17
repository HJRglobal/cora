"""QBO multi-realm token-validity report + monitor (Phase 3.3 / F-18).

Cora keys ~11 QBO realms; a stale/expired refresh token makes finance answers
silently fail for that entity. This reports every realm's refresh-token validity
read PURELY from the stored `refresh_token_expires_at` + `last_refreshed_at`
(NO Intuit calls -- non-destructive), exits non-zero on any EXPIRED/INVALID realm
so a scheduler/wrapper surfaces the failure, and can optionally DM Harrison on
trouble (--alert). The daily refresh task (qbo_oauth_flow.py --refresh-all) is the
ROTATION mechanism; this is the read-only health check layered on top of it.

Usage:
  python scripts/qbo_token_status.py                # print table, exit 1 if any EXPIRED/INVALID
  python scripts/qbo_token_status.py --alert        # also DM Harrison on EXPIRED/INVALID/WARN/STALE
  python scripts/qbo_token_status.py --alert --dry-run   # print the would-be DM, don't send
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:  # printed output stays clean on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

try:  # best-effort .env load so --alert can reach SLACK_BOT_TOKEN
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # noqa: BLE001
    pass

from cora.connectors import qbo_oauth  # noqa: E402

HARRISON_SLACK_ID = "U0B2RM2JYJ1"
_DAY = 86400
_DEFAULT_WARN_DAYS = 14   # refresh-token expiry lead window -> WARN (exit 0)
_STALE_REFRESH_DAYS = 3   # refresh runs daily; > this since last refresh is suspicious
_FAILURE = frozenset({"EXPIRED", "INVALID"})       # -> exit 1
_ALERTABLE = _FAILURE | {"WARN", "STALE"}          # -> DM on --alert


def _num(v) -> float:
    """Coerce a stored timestamp to a number; non-numeric (str/None/bool/etc.) -> 0,
    so a malformed entry classifies INVALID rather than crashing the comparison."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _classify(tok: dict, now: float, warn_days: int) -> tuple[str, str]:
    """Classify one realm's token from stored timestamps only. Returns (status, detail)."""
    rt_exp = _num(tok.get("refresh_token_expires_at"))
    last = _num(tok.get("last_refreshed_at"))
    err = tok.get("error")
    if err:
        return "INVALID", f"error: {str(err)[:60]}"
    if not tok.get("refresh_token") or not rt_exp:
        return "INVALID", "missing refresh token / expiry"
    if rt_exp < now:
        return "EXPIRED", f"expired {int((now - rt_exp) / _DAY)}d ago"
    days_left = int((rt_exp - now) / _DAY)
    days_since = int((now - last) / _DAY) if last else 999
    # A stale last-refresh means the daily rotation task may be FAILING for this
    # realm even though expiry still reads valid (it lags up to ~100d). Warn-only.
    if days_since > _STALE_REFRESH_DAYS:
        return "STALE", f"valid ({days_left}d) but last refresh {days_since}d ago"
    if days_left <= warn_days:
        return "WARN", f"expires in {days_left}d"
    return "OK", f"valid ({days_left}d left)"


def evaluate(tokens: dict, now: float, warn_days: int = _DEFAULT_WARN_DAYS):
    """Return (rows, has_failure). Pure; takes `now` for testability."""
    rows = []
    for entity, tok in sorted(tokens.items()):
        status, detail = _classify(tok if isinstance(tok, dict) else {}, now, warn_days)
        rows.append({"entity": entity, "status": status, "detail": detail})
    has_failure = any(r["status"] in _FAILURE for r in rows)
    return rows, has_failure


def _format_report(rows: list[dict]) -> str:
    lines = [f"{'Entity':<12} {'Status':<8} Detail", "-" * 60]
    for r in rows:
        lines.append(f"{r['entity']:<12} {r['status']:<8} {r['detail']}")
    if not rows:
        lines.append("(no QBO realms in the token store)")
    return "\n".join(lines)


def _format_alert(rows: list[dict]) -> str:
    bad = [r for r in rows if r["status"] in _ALERTABLE]
    lines = [f"QBO token monitor: {len(bad)} realm(s) need attention"]
    for r in bad:
        lines.append(f"- {r['entity']}: {r['status']} -- {r['detail']}")
    return "\n".join(lines)


def _send_alert(text: str, dry_run: bool) -> None:
    import os

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("[alert] SLACK_BOT_TOKEN not set; skipping DM")
        return
    if dry_run:
        print("[alert dry-run] would DM Harrison:\n" + text)
        return
    try:
        # Importing cora.* (above) already installed the egress sanitizer on
        # WebClient, so this DM is auto-sanitized.
        from slack_sdk import WebClient

        client = WebClient(token=token)
        dm = client.conversations_open(users=HARRISON_SLACK_ID)
        client.chat_postMessage(channel=dm["channel"]["id"], text=text)
        print("[alert] DM sent to Harrison")
    except Exception as exc:  # noqa: BLE001 -- alert failure must not mask the exit code
        print(f"[alert] DM failed: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QBO multi-realm token-validity monitor")
    ap.add_argument("--alert", action="store_true",
                    help="DM Harrison if any realm is EXPIRED/INVALID/WARN/STALE")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --alert, print the would-be DM instead of sending")
    ap.add_argument("--warn-days", type=int, default=_DEFAULT_WARN_DAYS,
                    help=f"expiry lead window for WARN (default {_DEFAULT_WARN_DAYS})")
    args = ap.parse_args(argv)

    try:
        tokens = qbo_oauth._load_all_tokens()
    except Exception as exc:  # noqa: BLE001 -- corrupt store is a distinct failure
        print(f"ERROR reading token store: {exc}")
        return 2
    # A file that parses but has the wrong shape (null / [] / a scalar) would crash
    # evaluate(tokens.items()); treat it as corrupt (exit 2), not a token failure.
    if not isinstance(tokens, dict):
        print(f"ERROR: token store is not a dict (got {type(tokens).__name__})")
        return 2

    now = time.time()
    rows, has_failure = evaluate(tokens, now, args.warn_days)
    print(f"Now: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}\n")
    print(_format_report(rows))

    if args.alert and any(r["status"] in _ALERTABLE for r in rows):
        # An alert failure must NEVER affect the exit code (the contract _send_alert
        # documents); enforce it at the call site too.
        try:
            _send_alert(_format_alert(rows), args.dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"[alert] dispatch failed: {exc}")

    return 1 if has_failure else 0


if __name__ == "__main__":
    sys.exit(main())
