#!/usr/bin/env python3
"""
Security monitor for Cora's Windows deployment.

Scans log files and checks file integrity every 15 minutes.
Sends a Slack alert when suspicious activity is detected.
Duplicate alerts for the same event are suppressed for 1 hour.

First-run (initialize the integrity baseline):
    uv run python scripts/security_monitor.py --init

Normal use (called by Task Scheduler):
    uv run python scripts/security_monitor.py

Dry run (print findings, skip Slack):
    uv run python scripts/security_monitor.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

CORA_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = CORA_ROOT / "logs"
SECURITY_DIR = CORA_ROOT / "data" / "security"
INTEGRITY_FILE = SECURITY_DIR / "file_hashes.json"
ALERT_HISTORY_FILE = SECURITY_DIR / "alert_history.json"

# ---------------------------------------------------------------------------
# Log patterns to watch for
# ---------------------------------------------------------------------------
LOG_EVENTS = [
    {
        "name": "auth_failure",
        "pattern": r"AuthenticationError|Invalid token|Unauthorized|token.*invalid|invalid.*token",
        "severity": "HIGH",
        "threshold": 1,
        "message": "Authentication failure — a token may be invalid, revoked, or leaked",
    },
    {
        "name": "repeated_403",
        "pattern": r"\b403\b|Forbidden",
        "severity": "HIGH",
        "threshold": 3,
        "message": "Repeated HTTP 403 errors — possible unauthorized API access",
    },
    {
        "name": "restart_loop",
        "pattern": r"Restarting in \d+",
        "severity": "MEDIUM",
        "threshold": 5,
        "message": "Excessive restart loop — Cora is crashing repeatedly",
    },
    {
        "name": "api_error_spike",
        "pattern": r"ClaudeClientError",
        "severity": "MEDIUM",
        "threshold": 10,
        "message": "High rate of Claude API errors — possible service disruption",
    },
    {
        "name": "uncaught_exception",
        "pattern": r"SocketModeHandler raised",
        "severity": "LOW",
        "threshold": 3,
        "message": "Repeated uncaught exceptions in Socket Mode handler",
    },
]

# Files that should never change outside of a deliberate git commit.
# The first --init run records a SHA-256 baseline; every later run
# compares against it and alerts on any mismatch.
INTEGRITY_FILES = [
    ".env",
    ".githooks/pre-commit",
    "deployment/setup-windows-task.ps1",
    "src/cora/app.py",
    "src/cora/claude_client.py",
    "src/cora/config.py",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_env() -> dict[str, str]:
    env_path = CORA_ROOT / ".env"
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def get_recent_log_lines(window_minutes: int) -> list[str]:
    cutoff = datetime.now() - timedelta(minutes=window_minutes)
    lines: list[str] = []
    for days_back in (0, 1):
        date = (datetime.now() - timedelta(days=days_back)).date()
        log_file = LOG_DIR / f"cora-{date}.log"
        if not log_file.exists():
            continue
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
            if m:
                try:
                    if datetime.fromisoformat(m.group(1)) >= cutoff:
                        lines.append(line)
                except ValueError:
                    pass
    return lines


def sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_logs(window_minutes: int) -> list[dict]:
    recent_text = "\n".join(get_recent_log_lines(window_minutes))
    issues: list[dict] = []
    for event in LOG_EVENTS:
        count = len(re.findall(event["pattern"], recent_text, re.IGNORECASE))
        if count >= event["threshold"]:
            issues.append({
                "event": event["name"],
                "severity": event["severity"],
                "message": event["message"],
                "count": count,
            })
    return issues


def check_integrity(init_mode: bool = False) -> list[dict]:
    stored = load_json(INTEGRITY_FILE)
    current = {rel: sha256(CORA_ROOT / rel) for rel in INTEGRITY_FILES}
    current = {k: v for k, v in current.items() if v}  # drop missing files

    issues: list[dict] = []
    if not init_mode:
        for rel, h in current.items():
            if rel in stored and stored[rel] != h:
                issues.append({
                    "event": f"file_changed:{rel}",
                    "severity": "HIGH",
                    "message": f"Monitored file changed unexpectedly: `{rel}`",
                })

    save_json(INTEGRITY_FILE, {**stored, **current})
    if init_mode:
        print(f"  Baseline recorded for {len(current)} file(s).")
    return issues


def check_windows_failed_logins() -> list[dict]:
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            [
                "wevtutil", "qe", "Security",
                "/c:50", "/rd:true", "/f:text",
                "/q:*[System[(EventID=4625) and TimeCreated[timediff(@SystemTime) <= 900000]]]",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        count = result.stdout.count("Event ID: 4625")
        if count >= 3:
            return [{
                "event": "failed_windows_logins",
                "severity": "HIGH",
                "message": f"Windows: {count} failed login attempt(s) in the last 15 min",
                "count": count,
            }]
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        pass
    return []


# ---------------------------------------------------------------------------
# Alert deduplication
# ---------------------------------------------------------------------------

def filter_new_issues(issues: list[dict]) -> list[dict]:
    """Return only issues not already alerted within the last hour."""
    history = load_json(ALERT_HISTORY_FILE)
    cutoff = datetime.now() - timedelta(hours=1)
    new: list[dict] = []
    for issue in issues:
        last_str = history.get(issue["event"])
        if last_str:
            try:
                if datetime.fromisoformat(last_str) >= cutoff:
                    continue
            except ValueError:
                pass
        new.append(issue)

    if new:
        now_str = datetime.now().isoformat()
        for issue in new:
            history[issue["event"]] = now_str
        save_json(ALERT_HISTORY_FILE, history)

    return new


# ---------------------------------------------------------------------------
# Slack alert
# ---------------------------------------------------------------------------

def send_alert(token: str, channel: str, issues: list[dict]) -> bool:
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    issues_sorted = sorted(issues, key=lambda i: severity_order.get(i["severity"], 9))

    emoji_map = {"HIGH": ":red_circle:", "MEDIUM": ":large_yellow_circle:", "LOW": ":white_circle:"}

    lines = [":rotating_light: *Cora Security Alert*", ""]
    for issue in issues_sorted:
        em = emoji_map.get(issue["severity"], ":white_circle:")
        line = f"{em} [{issue['severity']}] {issue['message']}"
        if "count" in issue:
            line += f"  _(×{issue['count']})_"
        lines.append(line)

    lines += [
        "",
        f"_Detected {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. "
        f"Check `logs/cora-{datetime.now().strftime('%Y-%m-%d')}.log` for details._",
        "_Suppress dupes for 1 h automatically. See `data/security/alert_history.json` to reset._",
    ]

    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"channel": channel, "text": "\n".join(lines)},
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"Slack error: {data.get('error')}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as e:
        print(f"Slack request failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Cora security monitor")
    parser.add_argument(
        "--init", action="store_true",
        help="Record initial file-integrity baseline (run once after setup)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print findings to stdout without sending a Slack alert",
    )
    parser.add_argument(
        "--window", type=int, default=15, metavar="MIN",
        help="How many minutes of logs to scan (default 15)",
    )
    args = parser.parse_args()

    env = load_env()
    slack_token = env.get("SLACK_BOT_TOKEN", "")
    alert_channel = env.get("SECURITY_ALERT_CHANNEL", "cora-build")

    if args.init:
        print("Initializing security baseline...")
        check_integrity(init_mode=True)
        print("Done. Run without --init on a schedule going forward.")
        return

    all_issues = (
        check_logs(args.window)
        + check_integrity()
        + check_windows_failed_logins()
    )

    if not all_issues:
        print(f"[{datetime.now():%H:%M:%S}] Security check passed — no issues found.")
        return

    for issue in all_issues:
        print(f"  [{issue['severity']}] {issue['message']}")

    if args.dry_run:
        print("Dry run — Slack alert suppressed.")
        return

    new_issues = filter_new_issues(all_issues)
    if not new_issues:
        print("Issue(s) found but already alerted within the last hour — suppressing.")
        return

    if not slack_token:
        print("ERROR: SLACK_BOT_TOKEN not in .env — cannot send alert.", file=sys.stderr)
        sys.exit(1)

    ok = send_alert(slack_token, alert_channel, new_issues)
    if ok:
        print(f"Alert sent to #{alert_channel}: {len(new_issues)} new issue(s).")
    else:
        print("Failed to send Slack alert.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
