r"""DR manifest probe runner (DR/VM step 1, slice M2; cq-a296aa8e0a2e; charter D1/D3).

WHY THIS EXISTS (2026-09-23)
    deployment/DR-MANIFEST.md lists every item a bare-metal rebuild of Cora's bot estate
    needs. A manifest that cannot be CHECKED against the machine it describes is prose;
    this runner turns each item into a read-only probe on the current host and prints
    PASS / FAIL / MANUAL / UNMEASURED / INFO per item. Running it on the office host is
    the baseline proof that the manifest describes reality; running it on the VM at
    step 2 is the first real restore drill (the migration validates the manifest).

    Every probe is READ-ONLY: it opens files, queries Task Scheduler / CIM, opens the KB
    read-only, lists the Drive backup folder. It never registers, restarts, writes or
    sends. It never reads a secret VALUE: `.env` is handled by KEY NAME (charter D3), and
    the emitted JSON / markdown are gated by scripts/secrets_scan.py in CI.

    .venv\Scripts\python.exe scripts\dr_manifest_probes.py                       # table on stdout
    .venv\Scripts\python.exe scripts\dr_manifest_probes.py --json                # + the JSON
    .venv\Scripts\python.exe scripts\dr_manifest_probes.py --update-docs         # write deployment/manifest/dr-manifest.json
                                                                                 #   + regenerate the block in deployment/DR-MANIFEST.md
    .venv\Scripts\python.exe scripts\dr_manifest_probes.py --live-root C:\Users\Harri\code\cora   # from a worktree
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

log = logging.getLogger("dr_manifest_probes")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
AZ = timezone(timedelta(hours=-7))

PASS, FAIL, MANUAL, UNMEASURED, INFO = "PASS", "FAIL", "MANUAL", "UNMEASURED", "INFO"
STATUSES = (PASS, FAIL, MANUAL, UNMEASURED, INFO)

#: The checkout the estate runs from (every registered action hardcodes it). A run from a
#: git worktree probes THIS root's .env / logs / data / venv, not the worktree's.
LIVE_ROOT = Path(os.environ.get("CORA_LIVE_ROOT", r"C:\Users\Harri\code\cora"))
BACKUPS_ROOT = Path(os.environ.get("CORA_BACKUPS_ROOT", r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora\backups"))
FOUNDER_OS_ROOT = Path(r"G:\My Drive\HJR-Founder-OS")
KB_SNAPSHOT_DATE = "2026-09-08"                      # the one-off --include-kb copy (stop window, 9/8)
OUT_DIR = _REPO_ROOT / "deployment" / "manifest"
DR_JSON = "dr-manifest.json"
DR_DOC = _REPO_ROOT / "deployment" / "DR-MANIFEST.md"
DR_BLOCK = "dr-probe-baseline"
COWORK_TASKS_ROOT = Path(os.path.expandvars(r"%USERPROFILE%\OneDrive\Documents\Claude\Scheduled"))
COWORK_PIN_TASK = "cowork-model-pin-weekly"

#: The KB connector doors (path (b)): the tasks whose nightly runs rebuild the KB.
KB_DOORS: tuple[str, ...] = (
    "cowork-cora-kb-sync-static", "cowork-cora-kb-sync-slack", "cowork-cora-kb-sync-gmail",
    "cowork-cora-kb-sync-asana", "cowork-cora-kb-sync-fireflies", "cowork-cora-kb-sync-notion",
    "cowork-cora-kb-sync-drive", "Cora - Drive Sweep", "Cora - Drive Materialization",
    "Cora - LEX Dump Folder Sync", "cowork-cora-founders-os-sweep", "cowork-cora-channel-sweep",
    "cowork-cora-session-capture", "cowork-cora-claude-mirror",
)
#: The only bounded datum for a full connector rebuild: the whole corpus was re-ingested
#: 2026-05-28..2026-06-17 through the nightly windows (repo CLAUDE.md TOM, 2026-06-17
#: retention note). ~20 calendar days of nightly windows, NOT a single-window measurement.
KB_REBUILD_HISTORICAL_BOUND = "~20 calendar days via nightly windows (full re-ingest 2026-05-28..06-17, TOM 2026-06-17); no single-window measurement exists"


@dataclass
class Item:
    id: str
    item: str
    source_of_truth: str
    restore_step: str
    owner: str                       # Harrison | Justin | Cora-script | Harrison+Justin
    rto: str                         # estimate or UNMEASURED
    probe: Callable[["Ctx"], tuple[str, str]] | None = None
    manual_note: str = ""            # when probe is None: the manual step
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class Ctx:
    live_root: Path
    repo_root: Path
    backups_root: Path
    now: datetime
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)     # from the task-estate manifest / live
    task_estate: dict[str, Any] = field(default_factory=dict)          # diff_against_committed result


# ── helpers ───────────────────────────────────────────────────────────────────
def _ps(script: str, timeout: int = 90) -> str:
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
                          encoding="utf-8", errors="replace")
    return proc.stdout.strip()


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 60) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
                          cwd=str(cwd) if cwd else None, encoding="utf-8", errors="replace")
    return (proc.stdout or "").strip()


def env_key_names(path: Path) -> set[str]:
    """KEY NAMES of the active (uncommented) assignments in an env file. Values never read into a
    structure -- the line is split and only the left side kept."""
    names: set[str] = set()
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=", raw)
            if m:
                names.add(m.group(1))
    except OSError:
        pass
    return names


def _age(ts: datetime, now: datetime) -> str:
    d = now - ts
    if d.total_seconds() < 3600:
        return f"{int(d.total_seconds() // 60)} min"
    if d.total_seconds() < 86400 * 2:
        return f"{d.total_seconds() / 3600:.1f} h"
    return f"{d.days} d"


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


# ── probes (each returns (status, detail)) ────────────────────────────────────
def p_repo(c: Ctx) -> tuple[str, str]:
    """The RESTORE target is `main` on origin -- record ITS hash. The live checkout may sit
    on a feature branch during a Code session; that is reported, never called PASS."""
    head = _run(["git", "-C", str(c.live_root), "rev-parse", "--short", "HEAD"])
    branch = _run(["git", "-C", str(c.live_root), "rev-parse", "--abbrev-ref", "HEAD"])
    remote = _run(["git", "-C", str(c.live_root), "remote", "get-url", "origin"])
    main_hash = _run(["git", "-C", str(c.live_root), "rev-parse", "--short", "origin/main"]) or \
        _run(["git", "-C", str(c.live_root), "rev-parse", "--short", "main"])
    if not head:
        return FAIL, f"no git repo at {c.live_root}"
    if not remote:
        return FAIL, f"origin remote MISSING; live HEAD {head} on {branch}; restore hash main={main_hash or '?'}"
    if not main_hash:
        return FAIL, f"main not resolvable in the live checkout; live HEAD {head} on {branch}"
    detail = f"restore hash: origin/main {main_hash}; live checkout HEAD {head} on {branch}; origin set"
    if branch != "main":
        return MANUAL, detail + " -- the live checkout is NOT on main (a Code session holds it); the restore target is origin/main, not this HEAD"
    return PASS, detail


def p_lock(c: Ctx) -> tuple[str, str]:
    lock = c.repo_root / "uv.lock"
    py = c.repo_root / "pyproject.toml"
    try:
        has_mcp = 'name = "mcp"' in lock.read_text(encoding="utf-8", errors="replace")
        declared = re.search(r'"mcp[><=~!]', py.read_text(encoding="utf-8", errors="replace")) is not None
    except OSError as exc:
        return FAIL, str(exc)
    if declared and not has_mcp:
        return FAIL, "pyproject declares mcp but uv.lock omits it (uv sync would not install the MCP server deps)"
    return PASS, f"uv.lock present; mcp {'locked' if has_mcp else 'not declared'}"


def p_python(c: Ctx) -> tuple[str, str]:
    exe = c.live_root / ".venv" / "Scripts" / "python.exe"
    pyw = c.live_root / ".venv" / "Scripts" / "pythonw.exe"
    if not exe.exists():
        return FAIL, f"no venv python at {exe}"
    ver = _run([str(exe), "--version"])
    return (PASS if "3.12" in ver else FAIL), f"{ver}; pythonw.exe {'present' if pyw.exists() else 'MISSING (windowless estate needs it)'}"


def p_env_schema(c: Ctx) -> tuple[str, str]:
    live = c.live_root / ".env"
    example = c.repo_root / ".env.example"
    if not live.exists():
        return FAIL, "no live .env (restore from the encrypted secrets bundle: restore_secrets.py)"
    live_keys = env_key_names(live)
    example_active = env_key_names(example)
    missing = sorted(k for k in example_active if k not in live_keys)
    live_only = sorted(k for k in live_keys if k not in example_active)
    # dup keys = the 2026-06-11 HEALTH_PING_URL incident (dotenv takes the LAST duplicate)
    counts: dict[str, int] = {}
    for raw in live.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=", raw)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    dups = sorted(k for k, n in counts.items() if n > 1)
    status = PASS if not dups else FAIL
    return status, (f"{len(live_keys)} keys live; {len(missing)} active example keys missing in live"
                    + (f" ({', '.join(missing)})" if missing else "")   # ALL names (key names are allowed; a truncated list hides the one nobody restores)
                    + f"; {len(live_only)} live-only keys (documented as commented in .env.example or undocumented)"
                    + (f"; DUPLICATE keys: {', '.join(dups)}" if dups else "; no duplicate keys"))


def p_secrets_bundle(c: Ctx) -> tuple[str, str]:
    try:
        encs = sorted(c.backups_root.glob("*/secrets-*.enc"))
    except OSError as exc:
        return FAIL, f"backups root unreadable: {exc}"
    if not encs:
        return FAIL, f"no secrets-*.enc under {c.backups_root}"
    newest = encs[-1]
    mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    age = c.now - mtime
    passphrase = "CORA_BACKUP_PASSPHRASE" in os.environ
    restore = (c.repo_root / "scripts" / "restore_secrets.py").exists()
    status = PASS if age <= timedelta(days=2) and restore else FAIL
    return status, (f"newest {newest.parent.name}/{newest.name} ({newest.stat().st_size} B, {_age(mtime, c.now)} old); "
                    f"restore_secrets.py {'present' if restore else 'MISSING'}; passphrase env var "
                    f"{'visible to this process' if passphrase else 'NOT in this process (User-scope / password manager -- expected)'}")


def p_wdac(c: Ctx) -> tuple[str, str]:
    out = _ps("$d = Get-CimInstance -Namespace root\\Microsoft\\Windows\\DeviceGuard -ClassName Win32_DeviceGuard -ErrorAction SilentlyContinue; "
              "if ($d) { Write-Output ('' + $d.CodeIntegrityPolicyEnforcementStatus + '|' + $d.UsermodeCodeIntegrityPolicyEnforcementStatus) } else { Write-Output 'NA' }")
    cora_exe = (c.live_root / ".venv" / "Scripts" / "cora.exe").exists()
    if out == "NA" or not out:
        return MANUAL, "DeviceGuard CIM class unavailable; confirm the WDAC policy by hand"
    k, _, u = out.partition("|")
    label = {"0": "off", "1": "audit", "2": "enforced"}.get(k.strip(), k)
    return PASS, (f"code-integrity policy {label} (kernel {k}, user-mode {u}); service runs `python.exe -m cora.main`; "
                 f"cora.exe {'present in the venv -- ASSERTED blocked by policy (untested here; never use it as the action)' if cora_exe else 'absent'}")


def p_service_task(c: Ctx) -> tuple[str, str]:
    t = c.tasks.get("cowork-cora-service")
    if not t:
        return FAIL, "cowork-cora-service not registered"
    ok = t.get("state") == "Running" and "cora.main" in t.get("child_command", "") and t.get("windowless")
    return (PASS if ok else FAIL), (f"state {t.get('state')}; child `{t.get('child_command')}`; windowless {t.get('windowless')}; "
                                    f"trigger {t.get('trigger_text')}; restart {t.get('restart_count')} x {t.get('restart_interval')}; "
                                    f"run-as {t['run_as']['logon_type']}/{t['run_as']['run_level']}")


def p_bot_alive(c: Ctx) -> tuple[str, str]:
    hb = c.live_root / "data" / "health" / "heartbeat.txt"
    inst = c.live_root / "logs" / "cora-instances.jsonl"
    if not hb.exists():
        return FAIL, "no heartbeat file"
    age = c.now - datetime.fromtimestamp(hb.stat().st_mtime, tz=timezone.utc)
    pid = ""
    try:
        last = [l for l in inst.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()][-1]
        row = json.loads(last)
        pid = f"pid {row.get('pid')} started {str(row.get('ts', ''))[:19]}Z"
    except Exception:  # noqa: BLE001
        pid = "instances ledger unreadable"
    return (PASS if age <= timedelta(minutes=3) else FAIL), f"heartbeat {int(age.total_seconds())} s old; {pid}"


def p_watchdog(c: Ctx) -> tuple[str, str]:
    t = c.tasks.get("cora-watchdog")
    script = (c.repo_root / "deployment" / "restart-cora.ps1").exists()
    if not t:
        return FAIL, "cora-watchdog not registered"
    ok = t.get("enabled") and t["run_as"]["run_level"] == "Highest" and "PT5M" in t.get("trigger_text", "") and script
    return (PASS if ok else FAIL), (f"{t.get('state')}; {t.get('trigger_text')}; run level {t['run_as']['run_level']}; "
                                    f"restart-cora.ps1 {'present' if script else 'MISSING'}; last result {t.get('last_task_result')}")


def p_healthchecks(c: Ctx) -> tuple[str, str]:
    keys = env_key_names(c.live_root / ".env")
    has_key = "HEALTH_PING_URL" in keys
    failed_recent = 0
    try:
        logs = sorted((c.live_root / "logs").glob("cora-*.log"))[-2:]
        for lp in logs:
            failed_recent += lp.read_text(encoding="utf-8", errors="replace").count("health-ping: ping failed")
    except OSError:
        pass
    status = PASS if has_key and failed_recent == 0 else (FAIL if has_key else MANUAL)
    return status, (f"HEALTH_PING_URL {'set (key present; value never read)' if has_key else 'NOT set -- dead-man ping disabled'}; "
                    f"'ping failed' lines in the last two bot logs: {failed_recent}; the healthchecks.io dashboard state is a manual read")


def p_drive_mount(c: Ctx) -> tuple[str, str]:
    mounted = FOUNDER_OS_ROOT.exists()
    proc = _ps("$p = Get-Process -Name GoogleDriveFS -ErrorAction SilentlyContinue | Select-Object -First 1; if ($p) { Write-Output ('pid ' + $p.Id) } else { Write-Output 'not running' }")
    return (PASS if mounted and proc.startswith("pid") else FAIL), f"G:\\My Drive\\HJR-Founder-OS {'mounted' if mounted else 'ABSENT'}; GoogleDriveFS {proc}"


def p_pinned_folders(c: Ctx) -> tuple[str, str]:
    try:
        from cora import kb_exclusions as ke  # noqa: PLC0415
        ids = sorted(ke.KB_EXCLUDED_FOLDER_IDS)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"kb_exclusions unimportable: {exc}"
    return (PASS if len(ids) >= 6 else FAIL), f"{len(ids)} pinned Drive folder ids in cora.kb_exclusions.KB_EXCLUDED_FOLDER_IDS (identifiers only; the doc lists them)"


def p_scheduled_estate(c: Ctx) -> tuple[str, str]:
    te = c.task_estate
    if not te.get("available"):
        return FAIL, f"manifest check unavailable: {te.get('reason')}"
    n = len(te.get("warn_lines") or [])
    return (PASS if n == 0 else FAIL), (f"{te.get('live_count')} live / {te.get('manifest_count')} manifest; "
                                        f"{n} task-estate-drift line(s)" + (": " + "; ".join(te['warn_lines'][:4]) if n else ""))


def p_interactive_logon(c: Ctx) -> tuple[str, str]:
    n = sum(1 for t in c.tasks.values() if t["run_as"]["logon_type"].lower().startswith("interactive"))
    swa_false = sum(1 for t in c.tasks.values() if not t.get("start_when_available"))
    return MANUAL, (f"{n} of {len(c.tasks)} tasks run LogonType=Interactive -> nothing fires until a user signs in "
                    f"(the 2026-09-09 dark box, 00:34-06:18 AZ); {swa_false} tasks have StartWhenAvailable=false. "
                    "Decision owner Harrison: auto-logon on the host / VM, or re-register with a stored-credential logon type. "
                    "Drill item: cold boot with NO user logged on -> does the estate come up?")


def p_backups(c: Ctx) -> tuple[str, str]:
    today = c.now.astimezone(AZ).date()
    dated = [c.backups_root / d.isoformat() for d in (today, today - timedelta(days=1))]
    present = [p.name for p in dated if p.exists()]
    verify = "unknown"
    try:
        logs = sorted((c.live_root / "logs" / "tasks").glob("cowork-cora-backup-*.log"))
        if logs:
            text = logs[-1].read_text(encoding="utf-8", errors="replace")
            m = re.findall(r"Offsite verify:\s+(\w+)", text)
            verify = (m[-1] if m else "no verify line") + f" ({logs[-1].name})"
    except OSError:
        pass
    ok = bool(present) and verify.startswith("PASS")
    return (PASS if ok else FAIL), f"dated folders present: {present or 'NONE for today/yesterday'}; last backup log offsite verify: {verify}"


def p_kb_snapshot(c: Ctx) -> tuple[str, str]:
    snap = c.backups_root / KB_SNAPSHOT_DATE / "cora_kb.db"
    if not snap.exists():
        return FAIL, f"the {KB_SNAPSHOT_DATE} --include-kb snapshot is not at {snap} (locate it or take a new one inside a stop window)"
    st = snap.stat()
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    return PASS, (f"{snap} = {st.st_size:,} B ({_gb(st.st_size)}), taken {mtime.strftime('%Y-%m-%d %H:%M')}Z, "
                  f"{(c.now - mtime).days} days stale; RTO(a) UNMEASURED (copy {_gb(st.st_size)} from Drive + open + run the nightly syncs to catch up {(c.now - mtime).days} days)")


def p_kb_rebuild_doors(c: Ctx) -> tuple[str, str]:
    doc = (c.repo_root / "deployment" / "kb-rebuild.md").exists()
    rows = []
    for name in KB_DOORS:
        t = c.tasks.get(name)
        rows.append(f"{name}: {'MISSING' if not t else (t.get('state') + ' last ' + (t.get('last_run_time') or 'never') + ' rc ' + str(t.get('last_task_result')))}")
    missing = [n for n in KB_DOORS if n not in c.tasks]
    status = PASS if doc and not missing else FAIL
    return status, (f"kb-rebuild.md {'present' if doc else 'MISSING'}; {len(KB_DOORS) - len(missing)}/{len(KB_DOORS)} doors registered; "
                    f"RTO(b) UNMEASURED -- only bound: {KB_REBUILD_HISTORICAL_BOUND}. Doors: " + " | ".join(rows))


def p_kb_live(c: Ctx) -> tuple[str, str]:
    db = c.live_root / "data" / "cora_kb.db"
    if not db.exists():
        return FAIL, "no data/cora_kb.db"
    size = db.stat().st_size
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            n = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return FAIL, f"{_gb(size)}; read-only open failed: {exc}"
    return PASS, f"{_gb(size)} on disk; {n:,} knowledge_chunks (read-only open)"


def p_slack_app(c: Ctx) -> tuple[str, str]:
    keys = env_key_names(c.live_root / ".env")
    need = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET")
    missing = [k for k in need if k not in keys]
    return (PASS if not missing else FAIL), (f"key names present: {', '.join(k for k in need if k in keys) or 'none'}"
                                             + (f"; MISSING: {', '.join(missing)}" if missing else "") + "; Socket Mode (no inbound URL to restore)")


def p_cowork_estate(c: Ctx) -> tuple[str, str]:
    try:
        dirs = [d for d in COWORK_TASKS_ROOT.iterdir() if d.is_dir() and not d.name.startswith("_") and (d / "SKILL.md").is_file()]
    except OSError:
        return INFO, f"{COWORK_TASKS_ROOT} unreadable on this host (the Cowork estate lives on the office machine only; charter D4)"
    pin = _ps(f"$t = Get-ScheduledTask -TaskName '{COWORK_PIN_TASK}' -ErrorAction SilentlyContinue; if ($t) {{ Write-Output ([string]$t.State) }} else {{ Write-Output 'ABSENT' }}")
    return INFO, f"{len(dirs)} Cowork task folders under {COWORK_TASKS_ROOT}; pin task {COWORK_PIN_TASK} {pin}; NOT migrated (charter D4)"


def p_windowless(c: Ctx) -> tuple[str, str]:
    launcher = (c.repo_root / "deployment" / "run_hidden.py").exists()
    wrapped = sum(1 for t in c.tasks.values() if t.get("windowless"))
    return (PASS if launcher else FAIL), f"run_hidden.py {'present' if launcher else 'MISSING'}; {wrapped}/{len(c.tasks)} tasks wrapped windowless"


def p_task_xml_backups(c: Ctx) -> tuple[str, str]:
    """The committed per-task XML set must cover every manifest task (it is the restore form)."""
    xdir = c.repo_root / "deployment" / "manifest" / "tasks"
    try:
        committed = {p.stem for p in xdir.glob("*.xml")}
    except OSError:
        committed = set()
    expected = {t.get("log_slug") for t in c.tasks.values()} if c.tasks else set()
    missing = sorted(expected - committed) if expected else []
    d = c.live_root / "deployment" / "task-backups"
    try:
        dates = sorted(p.name for p in d.iterdir() if p.is_dir())
    except OSError:
        dates = []
    status = PASS if committed and not missing else (FAIL if expected else INFO)
    return status, (f"{len(committed)} committed task XML files under deployment/manifest/tasks"
                    + (f"; MISSING for {len(missing)} live task(s): {', '.join(missing[:8])}" if missing else "; covers every live task" if expected else "")
                    + f"; local dated exports (gitignored convenience): {dates or 'none'}")


def _manifest_count() -> str:
    """The committed task-estate count (so item titles never hardcode a number)."""
    try:
        return str(json.loads((OUT_DIR / "task-estate.json").read_text(encoding="utf-8")).get("count", "?"))
    except (OSError, ValueError):
        return "?"


# ── the manifest items (order = the restore drill order) ──────────────────────
def build_items() -> list[Item]:
    return [
        Item("D01", "Repo + branch (`main`) + remote", "GitHub HJRglobal/cora; local `C:\\Users\\Harri\\code\\cora`",
             "`git clone <origin> C:\\Users\\Harri\\code\\cora`; checkout `main` at the recorded hash", "Harrison", "~5 min", p_repo),
        Item("D02", "Dependency lock (`uv.lock` incl. `mcp`)", "`pyproject.toml` + `uv.lock` in the repo",
             "`uv sync` (dev: `uv sync --group dev`); the MCP server deps ride the lock since M2", "Harrison", "~10 min", p_lock),
        Item("D03", "Python 3.12 + `.venv` (+ `pythonw.exe`)", "`.venv\\Scripts\\python.exe` (uv-managed venv at the repo root)",
             "install Python 3.12 + uv (bootstrap Phase 1), `uv sync`; every task action hardcodes `.venv\\Scripts\\python.exe`/`pythonw.exe`", "Harrison", "~15 min", p_python),
        Item("D04", "`.env` SCHEMA (key NAMES only, never values)", "`.env.example` (every key read under src/, guarded by tests/test_env_example_coverage.py); live `.env` at the repo root (gitignored)",
             "restore the live `.env` from the encrypted secrets bundle (D05); NEVER retype values from a doc; check for duplicate keys before the first start (2026-06-11 HEALTH_PING_URL incident)", "Harrison", "~15 min", p_env_schema),
        Item("D05", "Encrypted secrets bundle (`secrets-YYYY-MM-DD.enc` = `.env` + the Google SA JSON)", "daily `backup_logs.py` -> Drive `backups/<date>/secrets-*.enc`; passphrase `CORA_BACKUP_PASSPHRASE` in the password manager (+ User-scope env var on the old host)",
             "fetch the passphrase from the password manager FIRST; `restore_secrets.py <secrets-*.enc>` -> `.env` + `cora-calendar-sa.json`", "Harrison", "~10 min (after the passphrase)", p_secrets_bundle),
        Item("D06", "WDAC / code-integrity posture", "Windows Device Guard policy on the host (policy files under `C:\\Windows\\System32\\CodeIntegrity`); service action `python.exe -m cora.main` (the console-script `cora.exe` is BLOCKED)",
             "on a new host: either reproduce the policy (export the .cip set) or run without WDAC; either way register the service as `-m cora.main`, never `cora.exe`", "Harrison", "MANUAL", p_wdac),
        Item("D07", "Service task `cowork-cora-service` (always-on bot)", "`deployment/manifest/tasks/cowork-cora-service.xml` (the live export: AtLogon, RestartOnFailure 999 x PT1M, windowless `python.exe -m cora.main` via run_hidden); `setup-windows-task.ps1` creates it fresh",
             "`deployment\\register-estate-from-manifest.ps1 -Apply -Only cowork-cora-service` (elevated) after the .env is restored, then `Start-ScheduledTask`; proof = a NEW pid row in `logs/cora-instances.jsonl`", "Cora-script (Harrison runs)", "~5 min", p_service_task),
        Item("D08", "Live bot health (heartbeat + instances ledger)", "`data/health/heartbeat.txt` (60 s) + `logs/cora-instances.jsonl`",
             "start the service task; heartbeat must advance within 2 min", "Cora-script", "~2 min", p_bot_alive),
        Item("D09", "`cora-watchdog` (5-min heartbeat watchdog, RunLevel Highest) + `restart-cora.ps1`", "`deployment/setup-cora-watchdog-task.ps1` + `deployment/cora-watchdog.ps1`",
             "run the setup script (elevated); verify a tick line in `logs/tasks/cora-watchdog-<date>.log`", "Cora-script (Harrison runs)", "~5 min", p_watchdog),
        Item("D10", "healthchecks.io dead-man ping (`HEALTH_PING_URL`)", "the ping URL is a SECRET-shaped `.env` key (restored with D05); the check itself lives in the healthchecks.io account",
             "after D05 + D07 the bot pings every 5 min; confirm the check flips UP in the dashboard (manual read)", "Harrison", "~5 min", p_healthchecks),
        Item("D11", "Google Drive for Desktop + the `G:` mount (Founder OS)", "Drive for Desktop signed in as harrison@hjrglobal.com, mounted at `G:`; `G:\\My Drive\\HJR-Founder-OS`",
             "install Drive for Desktop, sign in, set the mount letter to G:, wait for the tree to stream; the bot degrades 14/14 entity contexts without it (9/9 incident)", "Harrison", "~30 min + initial sync UNMEASURED", p_drive_mount),
        Item("D12", "Pinned KB-excluded Drive folder ids", "`src/cora/kb_exclusions.KB_EXCLUDED_FOLDER_IDS` (code; incl. the `_shared/projects/cora` parent pin, the Computers roots, the personal/finance/LEX pins)",
             "nothing to restore -- they ship with the repo; re-verify the ids still resolve after any Drive restructure", "Cora-script", "n/a", p_pinned_folders),
        Item("D13", f"Scheduled estate ({_manifest_count()} tasks) = `deployment/manifest/task-estate.json` + `deployment/manifest/tasks/*.xml`", "the live Task Scheduler registry, derived by `scripts/generate_task_estate_manifest.py` (M1): the JSON is the diff view, the per-task XML is the restore form",
             "bootstrap Phase 5: `Set-TimeZone` to the manifest's host_time_zone, then `deployment\\register-estate-from-manifest.ps1 -Apply` (elevated; registers every task from its XML, keeps the intent-disabled ones disabled); NOT the setup-*.ps1 scripts (drifted clocks / re-enable disabled tasks); `--diff-only` must print zero drift", "Harrison (elevated) + Cora-script", "~15-30 min", p_scheduled_estate),
        Item("D14", "Interactive-logon dependency of the whole estate", "every task `LogonType=Interactive` (manifest column `run_as`)",
             "DECISION: auto-logon on the host/VM or a stored-credential logon type; then the cold-boot drill", "Harrison", "MANUAL", p_interactive_logon),
        Item("D15", "Backups landing folder + offsite verify", "`backup_logs.py` daily 20:30 AZ -> `G:\\...\\_shared\\projects\\cora\\backups\\YYYY-MM-DD\\` (logs, ledgers, feature DBs, snapshots, secrets); log line `Offsite verify: PASS`",
             "nothing to restore -- this is the SOURCE for D05/D16; after a rebuild, confirm the first nightly run prints `Offsite verify: PASS`", "Cora-script", "n/a", p_backups),
        Item("D16", "KB restore path (a): snapshot restore", f"the one-off `--include-kb` copy `backups/{KB_SNAPSHOT_DATE}/cora_kb.db` (taken inside the 9/8 stop window)",
             "copy the snapshot to `data/cora_kb.db` with Cora STOPPED; start Cora; the nightly syncs catch up the gap (watermarks are inside the DB)", "Harrison", "UNMEASURED (copy ~7.9 GB + catch-up)", p_kb_snapshot),
        Item("D17", "KB restore path (b): connector rebuild", "`deployment/kb-rebuild.md` (fast path: nightly tasks repopulate; full path: the backfill scripts) + the door tasks listed in the probe",
             "move the DB aside with Cora stopped; start Cora; let the nightly doors run (fast path) or run the backfills in one window (full path)", "Harrison + Cora-script", "UNMEASURED", p_kb_rebuild_doors),
        Item("D18", "Live KB integrity", "`data/cora_kb.db` (sqlite + sqlite-vec; WAL)",
             "n/a -- the probe is the read-only sanity check after either restore path", "Cora-script", "n/a", p_kb_live),
        Item("D19", "Slack app (Socket Mode) -- token key names", "api.slack.com app config; `.env` keys `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_SIGNING_SECRET`",
             "restore from D05, or regenerate in the Slack app config (bootstrap Phase 3) and revoke the old tokens", "Harrison", "~15 min", p_slack_app),
        Item("D20", "Windowless launcher (`deployment/run_hidden.py` under `pythonw.exe`)", "the repo + the venv",
             "ships with D01/D03; `rewrap-tasks-hidden.ps1 -Apply` after registering tasks", "Cora-script", "~2 min", p_windowless),
        Item("D21", "Cowork estate (Claude desktop scheduled tasks) -- cross-reference", "`%USERPROFILE%\\OneDrive\\Documents\\Claude\\Scheduled\\*` + `C:\\Users\\Harri\\code\\pin-scheduled-task-models.ps1` (weekly task `cowork-model-pin-weekly`)",
             "NOT migrated (charter D4): stays on the office machine; revisit at Phase 3", "Harrison", "n/a", p_cowork_estate),
        Item("D22", "Task XML restore set (`deployment/manifest/tasks/<slug>.xml`, committed) + local `deployment/task-backups/<date>` exports", "the generator exports every task's XML on each run (committed with the manifest); `rewrap-tasks-hidden.ps1` also writes local, gitignored dated exports",
             "the committed XML set IS the restore form (D13); the dated local exports are a convenience that does not survive a bare-metal loss", "Cora-script", "n/a", p_task_xml_backups),
        Item("D23", "Restore drill: cold boot with NO user logged on", "this manifest (D14) + the VM at step 2",
             "power-cycle the VM, do NOT sign in, wait 15 min: does the service start, does the heartbeat advance, do the 02:00-06:10 tasks fire?", "Harrison", "MANUAL (step 2)", None,
             "runs on the VM at step 2; the office host cannot be rebooted for a drill without a stop window"),
        Item("D24", "Full bare-metal RTO", "the sum of D01-D19 measured end to end",
             "measure on the VM at step 2 (the migration IS the drill); record the wall-clock here", "Harrison + Justin", "UNMEASURED", None,
             "no measurement exists; every component RTO above is an estimate or UNMEASURED until step 2"),
    ]


# ── run ───────────────────────────────────────────────────────────────────────
def load_tasks(live_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Live tasks (normalized by the M1 generator) + the drift result vs the committed manifest."""
    import generate_task_estate_manifest as tem  # noqa: PLC0415
    tem.LIVE_ROOT = live_root
    os.environ.setdefault("TASK_RUNS_LEDGER_PATH", str(live_root / "logs" / "task-runs.jsonl"))
    try:
        live = tem.normalize_all(tem.enumerate_cim(), log_dir=live_root / "logs" / "tasks")
    except Exception as exc:  # noqa: BLE001
        log.warning("live enumeration failed: %s", exc)
        return {}, {"available": False, "reason": str(exc)}
    te = tem.diff_against_committed(live_tasks=live)
    return {t["name"]: t for t in live}, te


def run_probes(items: list[Item], ctx: Ctx) -> list[Item]:
    for it in items:
        if it.probe is None:
            status, detail = (UNMEASURED if it.rto.startswith("UNMEASURED") else MANUAL), it.manual_note
        else:
            try:
                status, detail = it.probe(ctx)
            except Exception as exc:  # noqa: BLE001 -- a probe must never take the run down
                status, detail = FAIL, f"probe raised: {exc}"
        it.result = {"status": status, "detail": detail}
    return items


def summarize(items: list[Item]) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for it in items:
        out[it.result.get("status", FAIL)] = out.get(it.result.get("status", FAIL), 0) + 1
    return out


def to_json(items: list[Item], ctx: Ctx) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at_utc": ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_az": ctx.now.astimezone(AZ).strftime("%Y-%m-%d %H:%M AZ"),
        "host": os.environ.get("COMPUTERNAME", ""),
        "live_root": str(ctx.live_root),
        "summary": summarize(items),
        "kb_rto": {"a_snapshot": "UNMEASURED", "b_connector_rebuild": "UNMEASURED", "b_historical_bound": KB_REBUILD_HISTORICAL_BOUND},
        "items": [{"id": it.id, "item": it.item, "source_of_truth": it.source_of_truth, "restore_step": it.restore_step,
                   "owner": it.owner, "rto": it.rto, "status": it.result.get("status"), "detail": it.result.get("detail")}
                  for it in items],
    }


def _cell(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def render_block(items: list[Item], ctx: Ctx) -> str:
    """The `dr-probe-baseline` GENERATED block (date-only stamp -> same-day idempotent)."""
    s = summarize(items)
    date = ctx.now.astimezone(AZ).strftime("%Y-%m-%d")
    L = [f"_Probe baseline {date} AZ on `{os.environ.get('COMPUTERNAME', 'this host')}` by `scripts/dr_manifest_probes.py` "
         f"(live root `{ctx.live_root}`): **PASS {s[PASS]} / FAIL {s[FAIL]} / MANUAL {s[MANUAL]} / UNMEASURED {s[UNMEASURED]} / INFO {s[INFO]}** "
         f"of {len(items)} items. Machine-readable twin: `deployment/manifest/{DR_JSON}`. Regenerate with `--update-docs`; do not hand-edit._", "",
         "| # | Item | Source of truth | Restore step | Probe (this host) | Owner | RTO |", "|---|---|---|---|---|---|---|"]
    for it in items:
        st = it.result.get("status", "?")
        badge = f"**{st}**" if st in (FAIL, MANUAL, UNMEASURED) else st
        L.append("| " + " | ".join(_cell(x) for x in (it.id, it.item, it.source_of_truth, it.restore_step,
                                                      f"{badge} -- {it.result.get('detail', '')}", it.owner, it.rto)) + " |")
    # .env SCHEMA -- KEY NAMES ONLY (charter D3), read from .env.example (the documented schema),
    # never from the live file. A rebuild restores VALUES from the encrypted bundle (D05).
    active, commented = env_example_schema(ctx.repo_root / ".env.example")
    L += ["", f"**`.env` schema (key NAMES from `.env.example`; {len(active)} active + {len(commented)} commented/optional; values live ONLY in the encrypted bundle):**", "",
          "- active: " + ", ".join(f"`{k}`" for k in active),
          "- commented / optional: " + (", ".join(f"`{k}`" for k in commented) or "none")]
    return "\n".join(L) + "\n"


def env_example_schema(path: Path) -> tuple[list[str], list[str]]:
    """(active keys, commented keys) documented in .env.example -- NAMES only."""
    active: set[str] = set()
    commented: set[str] = set()
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=", raw)
            if m:
                active.add(m.group(1))
                continue
            m = re.match(r"^\s*#\s*([A-Z][A-Z0-9_]{2,})\s*=", raw)
            if m:
                commented.add(m.group(1))
    except OSError:
        pass
    return sorted(active), sorted(commented - active)


def render_console(items: list[Item]) -> str:
    L = []
    for it in items:
        L.append(f"[{it.result.get('status', '?'):10}] {it.id} {it.item}")
        L.append(f"             {it.result.get('detail', '')}")
    s = summarize(items)
    L.append(f"SUMMARY PASS={s[PASS]} FAIL={s[FAIL]} MANUAL={s[MANUAL]} UNMEASURED={s[UNMEASURED]} INFO={s[INFO]} of {len(items)}")
    return "\n".join(L)


def update_doc(items: list[Item], ctx: Ctx, doc: Path = DR_DOC) -> bool:
    import generate_task_estate_manifest as tem  # noqa: PLC0415
    old = doc.read_text(encoding="utf-8")
    new = tem.replace_generated_block(old, DR_BLOCK, render_block(items, ctx))
    if new != old:
        doc.write_text(new, encoding="utf-8", newline="\n")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the DR manifest probes on this host (read-only).")
    ap.add_argument("--live-root", type=Path, default=LIVE_ROOT)
    ap.add_argument("--backups-root", type=Path, default=BACKUPS_ROOT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--update-docs", action="store_true", help=f"write deployment/manifest/{DR_JSON} + regenerate the block in DR-MANIFEST.md")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    now = datetime.now(timezone.utc)
    tasks, te = load_tasks(args.live_root)
    ctx = Ctx(live_root=args.live_root, repo_root=_REPO_ROOT, backups_root=args.backups_root, now=now, tasks=tasks, task_estate=te)
    items = run_probes(build_items(), ctx)
    print(render_console(items))
    payload = to_json(items, ctx)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    if args.update_docs:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / DR_JSON).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {args.out_dir / DR_JSON}")
        # The block carries LIVE measurements (heartbeat age, bundle age, current pid), so a
        # re-run within the same day still rewrites it; that is a refresh, not drift.
        print("DR-MANIFEST.md block " + ("refreshed (probe details are live measurements)" if update_doc(items, ctx)
                                         else "already current"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
