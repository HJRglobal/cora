r"""Secrets scan for emitted docs / manifests / reports (DR/VM step 1, kickoff section 4 #5; charter D3).

THE RULE: secret VALUES never enter a manifest, doc, fixture, card or report -- key NAMES
only. This scanner is the gate: tests/test_secrets_scan.py runs it over deployment/,
docs/ (when present), README.md and CLAUDE.md on every CI run; the CLI runs it by hand over
the Founder-OS packet + report before they are filed (kickoff smoke #5).

WHAT IT FLAGS
  1. Known token SHAPES (Slack xox*/xapp, Google API keys + OAuth refresh/access tokens,
     Anthropic/OpenAI sk-, AWS AKIA, GitHub ghp_, Shopify shpat_, Airtable pat..., Asana
     PATs v1 `<n>/<gid>:<32 alnum>` + v2 `<n>/<gid>/<gid>:<32 alnum>`, healthchecks ping URLs, Slack/Make webhook URLs,
     PEM private keys) -- minus obvious PLACEHOLDERS (your/paste/example/xxx/dummy/test/
     redacted/here/changeme/placeholder).
  2. A `KEY=value` line whose KEY is documented in .env.example and whose value is not a
     placeholder / flag / short literal (a doc must show `KEY=<value>`, never the value).
  3. BELT -- any LIVE .env value (secret-shaped keys only, >= 12 chars, not a path) appearing
     anywhere in the scanned text. The value is never printed: hits name the KEY.

Every hit is reported as (path, line, kind, key-or-shape) -- the matched TEXT is never
echoed, so the scanner's own output can be pasted into a report.

    .venv\Scripts\python.exe scripts\secrets_scan.py deployment README.md CLAUDE.md
    .venv\Scripts\python.exe scripts\secrets_scan.py "G:\My Drive\...\packet.md" --no-env-belt
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # D-266: every spawn windowless (tests/test_run_hidden.py rail)

_REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = _REPO_ROOT / ".env.example"
#: The live .env: this checkout's, else the LIVE checkout's (a git worktree has none -- the
#: belt must not silently become a no-op there; D-051 lens E, 2026-09-23).
_LIVE_ROOT = Path(os.environ.get("CORA_LIVE_ROOT", r"C:\Users\Harri\code\cora"))
LIVE_ENV = (_REPO_ROOT / ".env") if (_REPO_ROOT / ".env").exists() else (_LIVE_ROOT / ".env")

#: Keys that are secrets whatever their value looks like (the passphrase that decrypts every
#: other secret is a plain phrase -- no token shape, possibly short; D-051 lens B HIGH).
EXTRA_SECRET_KEYS: frozenset[str] = frozenset({"CORA_BACKUP_PASSPHRASE"})

SCAN_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".txt", ".ps1", ".py", ".toml", ".cfg", ".ini", ".example"}

#: (kind, compiled regex). Case-sensitive on purpose: token prefixes are.
SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("slack-token", re.compile(r"\bxox[abpers]-[A-Za-z0-9-]{10,}")),
    ("slack-app-token", re.compile(r"\bxapp-\d-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{10,}")),
    ("make-webhook", re.compile(r"https://hook\.[a-z0-9.-]*make\.com/[A-Za-z0-9]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("google-oauth-refresh", re.compile(r"\b1//0[A-Za-z0-9_-]{20,}")),
    ("google-oauth-access", re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}")),
    ("anthropic-openai-key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("shopify-token", re.compile(r"\bshp(?:at|ca|pa|ss)_[a-f0-9]{32}\b")),
    ("airtable-pat", re.compile(r"\bpat[A-Za-z0-9]{14}\.[a-f0-9]{64}\b")),
    # Asana PAT v1 `<n>/<gid>:<32>` AND v2 `<n>/<gid>/<gid>:<32>` (Code #15 S1: the pre-S1
    # `\b\d/\d{16}:...` shape missed v2 -- the exact 9/11 paste). Left edge "not a digit" (a
    # letter / JSON escape may precede), gid 10-20 digits: a superset of
    # cora.secret_tokens' asana leg (tests/test_secret_tokens.py drift test binds the two).
    ("asana-pat", re.compile(r"(?<![0-9])\d/\d{10,20}(?:/\d{10,20})?:[A-Za-z0-9]{32}(?![A-Za-z0-9])")),
    ("healthchecks-ping", re.compile(r"hc-ping\.com/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")),
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("hubspot-private-app", re.compile(r"\bpat-(?:na|eu)\d-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
)

_PLACEHOLDER_RE = re.compile(
    r"your|paste|example|xxx|dummy|test|redact|placeholder|changeme|<[^>]*>|\.\.\.|here\b|sample|fake|todo",
    re.IGNORECASE)
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=\s*(.+?)\s*$")
#: `KEY=value` anywhere in a line (bullet / inline code / table cell); the value stops at
#: whitespace, a closing backtick/quote or a table pipe.
_INLINE_ASSIGN_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{2,})\s*=\s*([^\s`'\"|]+)")
_SECRET_KEY_RE = re.compile(
    r"(TOKEN|SECRET|API_KEY|_KEY$|PASS(WORD|WD|PHRASE)?$|_PASS$|_PWD$|PASSPHRASE|CREDENTIAL|PAT$|PAT_|WEBHOOK|PING_URL|ACCESS_TOKEN|PRIVATE|CLIENT_SECRET|_JSON$)")
#: Password-class keys: a real value can be short (8+), so the belt floor is lower for them.
_PASSWORD_KEY_RE = re.compile(r"(PASS(WORD|WD|PHRASE)?$|_PASS$|_PWD$|PASSPHRASE)")
_NON_SECRET_KEY_RE = re.compile(r"(_ID$|_IDS$|_CHANNEL$|_MODEL$|_DIR$|_STORE$|_ENVIRONMENT$|_BU$|_TENANT$|_URI$|_CERT$|_HOURS$|_ENABLED$|_USER$)")
_PATH_LIKE_RE = re.compile(r"^(?:[A-Za-z]:\\|\\\\|/|\./|\.\\|~|data[\\/]|logs[\\/]|scripts[\\/])")
_LITERAL_VALUES = {"0", "1", "true", "false", "on", "off", "yes", "no", "none", "null", "all", "observe", "enforce",
                   "production", "sandbox", "dev", "prod"}


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    kind: str
    key: str          # the KEY name (env rules) or the shape name -- never the matched text

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} [{self.key}]"


_ENV_KEY_ANY_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})\s*=")


def env_example_keys(path: Path = ENV_EXAMPLE) -> set[str]:
    """Every documented KEY (active or commented), including `KEY=` with an EMPTY value --
    a key documented without a placeholder is still a key the rules must know."""
    keys: set[str] = set(EXTRA_SECRET_KEYS)
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _ENV_KEY_ANY_RE.match(raw)
            if m:
                keys.add(m.group(1))
    except OSError:
        pass
    return keys


_TOKEN_LIKE_RE = re.compile(r"^[A-Za-z0-9_\-./+=:]{24,}$")


def _token_like(value: str) -> bool:
    """A long, spaceless, mixed-class string: letters AND digits, no obvious words. A Slack
    channel id (C0B6GT3117Y, 11 chars) or a Drive folder id (33 chars, but no lowercase +
    digit + '-' mix? it HAS one) is NOT flagged unless it also has 24+ chars with at
    least 3 digit runs -- Drive ids do, so they are flagged only when they sit under a
    secret-shaped KEY (see scan_text); here the heuristic is deliberately conservative."""
    v = value.strip()
    if not _TOKEN_LIKE_RE.match(v):
        return False
    has_digit = any(c.isdigit() for c in v)
    has_alpha = any(c.isalpha() for c in v)
    digit_runs = len(re.findall(r"\d+", v))
    return has_digit and has_alpha and digit_runs >= 5 and len(v) >= 32


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip("'\"`")
    if not v or v.lower() in _LITERAL_VALUES or len(v) < 8:
        return True
    if v[0] in "<[{$":          # `<long-lived token from Step 4>` split at its first space is still a placeholder
        return True
    if _PLACEHOLDER_RE.search(v):
        return True
    if _PATH_LIKE_RE.match(v):
        return True
    return False


def live_secret_values(path: Path = LIVE_ENV) -> dict[str, str]:
    """KEY -> value for the live .env's SECRET-shaped keys (values >= 12 chars, not paths).
    Never printed by anything in this module. Empty when the file is absent (CI)."""
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for raw in lines:
        m = _ENV_LINE_RE.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip("'\"")
        extra = key in EXTRA_SECRET_KEYS
        if not extra and (not _SECRET_KEY_RE.search(key) or _NON_SECRET_KEY_RE.search(key)):
            continue
        floor = 8 if (extra or _PASSWORD_KEY_RE.search(key)) else 12   # passwords can be short; tokens are not
        if len(val) < floor or _PATH_LIKE_RE.match(val) or val.lower() in _LITERAL_VALUES:
            continue
        out[key] = val
    return out


def is_test_path(rel: str) -> bool:
    """A repo-relative path under tests/ (either separator). Test files are BELT-ONLY: their
    secret-SHAPED fixtures are inputs by construction (the scanner's own tests carry an AWS
    example key, an assembled xoxb-, `CORA_BACKUP_PASSPHRASE=correct horse battery` ...), so
    the shape + env-assignment rules would block every commit that touches them. A LIVE .env
    value in a test file is a real leak and still blocks (the belt runs everywhere)."""
    parts = rel.replace("\\", "/").split("/")
    return bool(parts) and parts[0] == "tests"


def scan_text(text: str, path: str, *, env_keys: set[str] | None = None,
              live_values: dict[str, str] | None = None, shapes: bool = True) -> list[Hit]:
    hits: list[Hit] = []
    env_keys = env_keys if env_keys is not None else env_example_keys()
    live_values = live_values if live_values is not None else {}
    for i, line in enumerate(text.splitlines(), 1):
        if shapes:
            for kind, rx in SHAPES:
                for m in rx.finditer(line):
                    if not _is_placeholder(m.group(0)):
                        hits.append(Hit(path, i, "shape", kind))
        # Rule 2: a documented KEY=value with a REAL value. Line-anchored for every documented
        # key; ANYWHERE in the line (a bullet, inline code, a table cell) for secret-shaped
        # keys -- the shapes a runbook actually uses (D-051 lens B: `- DEPOSCO_PROD_PASS=...`,
        # "set `KEY=...` in .env", `| KEY | value |` all scanned clean before).
        m = _ENV_LINE_RE.match(line)
        if m and m.group(1) in env_keys and not _is_placeholder(m.group(2)):
            key, val = m.group(1), m.group(2).strip().strip("'\"`")
            identifier_key = bool(_NON_SECRET_KEY_RE.search(key))
            secret_key = key in EXTRA_SECRET_KEYS or (bool(_SECRET_KEY_RE.search(key)) and not identifier_key)
            if secret_key or (not identifier_key and _token_like(val)):
                hits.append(Hit(path, i, "env-assignment", key))
        else:
            for m2 in _INLINE_ASSIGN_RE.finditer(line):
                key, val = m2.group(1), m2.group(2).strip().strip("'\"`")
                if key not in env_keys:
                    continue
                secret_key = key in EXTRA_SECRET_KEYS or (bool(_SECRET_KEY_RE.search(key)) and not _NON_SECRET_KEY_RE.search(key))
                if secret_key and not _is_placeholder(val):
                    hits.append(Hit(path, i, "env-assignment", key))
                    break
        for key, val in live_values.items():
            if val in line:
                hits.append(Hit(path, i, "live-env-value", key))
    return hits


def iter_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for r in roots:
        if r.is_file():
            files.append(r)
            continue
        if not r.is_dir():
            continue
        for p in sorted(r.rglob("*")):
            if p.is_file() and p.suffix.lower() in SCAN_SUFFIXES and "__pycache__" not in p.parts \
                    and not p.name.endswith(".bak"):
                files.append(p)
    return files


def scan_paths(roots: list[Path], *, env_belt: bool = True, env_keys: set[str] | None = None,
               env_file: Path | None = None) -> list[Hit]:
    live = live_secret_values(env_file or LIVE_ENV) if env_belt else {}
    keys = env_keys if env_keys is not None else env_example_keys()
    hits: list[Hit] = []
    for p in iter_files(roots):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = str(p.relative_to(_REPO_ROOT))
        except ValueError:
            rel = str(p)
        if is_test_path(rel):
            hits.extend(scan_text(text, rel, env_keys=set(), live_values=live, shapes=False))   # belt only
        else:
            hits.extend(scan_text(text, rel, env_keys=keys, live_values=live))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scan docs/manifests for secret VALUES (key names are fine).")
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--staged", action="store_true",
                    help="scan the files staged for commit (git diff --cached --name-only) -- the pre-commit hook path")
    ap.add_argument("--no-env-belt", action="store_true", help="skip the live-.env value belt")
    ap.add_argument("--env-file", type=Path, default=None,
                    help="the live .env to take belt values from (default: <repo>/.env, else the live checkout's)")
    args = ap.parse_args(argv)
    paths = list(args.paths)
    if args.staged:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"], capture_output=True,
                             text=True, cwd=str(_REPO_ROOT), encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        paths += [_REPO_ROOT / p.strip() for p in out.stdout.splitlines() if p.strip()
                  and not p.strip().startswith(".githooks/") and p.strip() != ".env"]
    if not paths:
        print("secrets-scan: nothing to scan"); return 0
    env_file = args.env_file or LIVE_ENV
    hits = scan_paths(paths, env_belt=not args.no_env_belt, env_file=env_file)
    files = iter_files(paths)
    print(f"secrets-scan: {len(files)} file(s), {len(hits)} hit(s)"
          + ("" if args.no_env_belt else f"; live-env belt keys={len(live_secret_values(env_file))}"))
    for h in hits:
        print("HIT " + h.render())
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
