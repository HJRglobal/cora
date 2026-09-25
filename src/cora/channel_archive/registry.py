"""Exemption SOURCES for the dead-channel lane (Code #16 C1): the channel registry,
the strict deny-policy loader, the sprawl keep-list duplicate and the LEX belt.

Every loader here FAILS CLOSED. The lane's failure that matters is a false
"inactive" on a live channel, so a source that cannot be read completely makes the
scan BLIND (it proposes nothing and says why), never "no exemption applies":

  * the channel registry (``_shared/playbooks/slack-channel-registry.md``) counts as
    read only when all nine entity sections, the trailing ``_Coverage note``
    sentinel and at least max(100, 90% of the last good parse) ids are present --
    a truncated Drive read that ends inside the LEX section would otherwise drop
    every later section's channels into the candidate list (amendment A9);
  * the deny policy is read by THIS module per scan and per tap, with no cache.
    ``slack_sweep_policy.is_denied`` fails OPEN to ``{}`` and caches that for the
    life of the process, so one transient read error there would silently expose
    the personal/family channels to the scan for the rest of the bot's uptime (A7).

Import-light on purpose (stdlib + yaml + two tiny cora modules): the monthly script
and the nightly monitor import this, and neither may build the Bolt app.
"""
from __future__ import annotations

import fnmatch
import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[3]

# ── the channel registry ─────────────────────────────────────────────────────
REGISTRY_ENV = "CORA_CHANNEL_REGISTRY_PATH"
_DEFAULT_FOUNDER_OS_ROOT = r"G:\My Drive\HJR-Founder-OS"
REGISTRY_REL = ("_shared", "playbooks", "slack-channel-registry.md")

#: The nine entity sections a complete registry carries, matched as a
#: case-insensitive PREFIX of the ``## `` header text. "lexington" is the LEX section.
ENTITY_SECTIONS: tuple[str, ...] = (
    "hjr global", "f3 energy", "osn", "lexington", "hjr properties",
    "hjr productions", "ufl", "big d media", "founder",
)
LEX_SECTION = "lexington"
COVERAGE_SENTINEL = "_Coverage note"
MIN_REGISTRY_IDS = 100
REGISTRY_FLOOR_FRACTION = 0.9

# Each is ONE bounded class run with a literal terminator: linear on any input.
_ROW_ID_RE = re.compile(r"`(C[A-Z0-9]{8,24})`")
_ROW_NAME_RE = re.compile(r"#([a-z0-9][a-z0-9_-]{0,79})")


@dataclass(frozen=True)
class Registry:
    ok: bool
    reason: str = ""
    ids: frozenset = frozenset()
    names: frozenset = frozenset()
    lex_ids: frozenset = frozenset()
    lex_names: frozenset = frozenset()
    count: int = 0


def registry_path() -> Path:
    """Per call. Tests point ``CORA_CHANNEL_REGISTRY_PATH`` at a synthetic fixture
    (tests/conftest.py), so no test ever reads the live Founder-OS file."""
    raw = str(os.environ.get("CORA_CHANNEL_REGISTRY_PATH", "") or "").strip()
    if raw:
        return Path(raw)
    root = str(os.environ.get("FOUNDER_OS_ROOT", "") or "").strip() or _DEFAULT_FOUNDER_OS_ROOT
    return Path(root).joinpath(*REGISTRY_REL)


def registry_floor(last_good_count: int) -> int:
    try:
        last = int(last_good_count or 0)
    except (TypeError, ValueError):
        last = 0
    return max(MIN_REGISTRY_IDS, math.ceil(REGISTRY_FLOOR_FRACTION * max(0, last)))


def parse_registry(text: str, *, last_good_count: int = 0) -> Registry:
    """Parse the registry markdown. Only table rows (lines starting with ``|``) are
    read; the first backticked channel id on a row is its id and the first
    ``#name`` its name; the LEX section is tracked by its ``## `` header."""
    if not isinstance(text, str) or not text.strip():
        return Registry(ok=False, reason="registry_unreadable: empty")
    seen_sections: set[str] = set()
    current = ""
    sentinel = False
    ids: set[str] = set()
    names: set[str] = set()
    lex_ids: set[str] = set()
    lex_names: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            head = line[3:].strip().lower()
            current = next((s for s in ENTITY_SECTIONS if head.startswith(s)), "")
            if current:
                seen_sections.add(current)
            continue
        if line.startswith(COVERAGE_SENTINEL):
            sentinel = True
            continue
        if not line.startswith("|"):
            continue
        m = _ROW_ID_RE.search(line)
        if not m:
            continue
        cid = m.group(1)
        ids.add(cid)
        first_cell = line.split("|")[1] if line.count("|") >= 2 else line
        nm = _ROW_NAME_RE.search(first_cell)
        name = nm.group(1).rstrip("-") if nm else ""
        if name:
            names.add(name)
        if current == LEX_SECTION:
            lex_ids.add(cid)
            if name:
                lex_names.add(name)
    missing = [s for s in ENTITY_SECTIONS if s not in seen_sections]
    if missing:
        return Registry(ok=False, reason=f"registry_unreadable: {len(missing)} entity section(s) missing",
                        count=len(ids))
    if not sentinel:
        return Registry(ok=False, reason="registry_unreadable: coverage sentinel missing (truncated?)",
                        count=len(ids))
    floor = registry_floor(last_good_count)
    if len(ids) < floor:
        return Registry(ok=False, reason=f"registry_unreadable: {len(ids)} ids < floor {floor}",
                        count=len(ids))
    return Registry(ok=True, ids=frozenset(ids), names=frozenset(names),
                    lex_ids=frozenset(lex_ids), lex_names=frozenset(lex_names), count=len(ids))


#: The operator's escape hatch for a LEGITIMATE registry trim of more than 10% (D-051 r1
#: registry-ops#1): a blind scan persists no count, so without it the floor never moves.
REBASELINE_CMD = (r".venv\Scripts\python.exe scripts\run_channel_archive_proposal.py "
                  "--rebaseline-registry --apply")
#: What clears a blind card once its cause is fixed (or the registry re-baselined): a
#: fresh sighted scan (D-051 r2 c1-monitor#4).
FRESH_SCAN_HINT = ("a fresh scan clears it: ask 'archive the dead channels' in the DM, or run "
                   r"`.venv\Scripts\python.exe scripts\run_channel_archive_proposal.py --apply`")
# Bounded digit runs around a literal: linear on any input (timed on 40k whitespace).
_FLOOR_RE = re.compile(r"(\d{1,7}) ids < floor (\d{1,7})")


def shrink_copy(blind_detail: str | None) -> tuple[str, str] | None:
    """(cause, hint) when a registry parsed COMPLETE (every section + the sentinel) but
    below its id floor -- the file shrank, it was not "read incompletely". A registry
    of at least 100 ids gets the re-baseline command; below that COUNT there is no hint
    (a re-baseline refuses fewer than 100 ids, whatever the old floor was -- D-051 r2
    registry-ops#2). None for every other blind detail."""
    m = _FLOOR_RE.search(str(blind_detail or ""))
    if m is None:
        return None
    n, floor = int(m.group(1)), int(m.group(2))
    if n < MIN_REGISTRY_IDS:
        return (f"the channel registry has {n} ids, below the {MIN_REGISTRY_IDS}-id minimum", "")
    return (f"the channel registry has {n} ids, fewer than 90% of the last good read "
            f"(floor {floor})",
            f"If the registry was trimmed on purpose, Harrison re-baselines it: `{REBASELINE_CMD}`.")


def load_registry(*, last_good_count: int = 0, path: Path | None = None,
                  reader: Callable[[Path], str] | None = None) -> Registry:
    """Read + parse, timeout-bounded through drive_io (the default path is on G:)."""
    p = Path(path) if path is not None else registry_path()
    try:
        if reader is not None:
            text = reader(p)
        else:
            from .. import drive_io  # noqa: PLC0415 -- lazy: bounded Drive read
            text = drive_io.read_text(p, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 -- unreadable = BLIND, said out loud
        return Registry(ok=False, reason=f"registry_unreadable: {type(exc).__name__}")
    return parse_registry(text or "", last_good_count=last_good_count)


# ── the deny policy (strict, uncached) ───────────────────────────────────────
DENY_POLICY_PATH = _REPO_ROOT / "data" / "maps" / "slack-sweep-policy.yaml"
MIN_DENY_IDS = 10


@dataclass(frozen=True)
class DenyPolicy:
    ids: frozenset = frozenset()
    names: frozenset = frozenset()
    globs: tuple = ()


def load_deny_policy(path: Path | None = None) -> DenyPolicy | None:
    """None on ANY problem: an exception, a non-mapping, an empty deny_by_id or
    deny_by_name, or fewer than MIN_DENY_IDS ids. No cache -- read per scan and per
    tap -- so a corrupt edit after a good load can never be masked by the good one."""
    p = Path(path) if path is not None else DENY_POLICY_PATH
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    by_id = data.get("deny_by_id")
    by_name = data.get("deny_by_name")
    by_glob = data.get("deny_by_glob") or []
    if not isinstance(by_id, list) or not isinstance(by_name, list) or not isinstance(by_glob, list):
        return None
    ids = frozenset(str(x).strip() for x in by_id if str(x).strip())
    names = frozenset(str(x).strip().lower() for x in by_name if str(x).strip())
    if not ids or not names or len(ids) < MIN_DENY_IDS:
        return None
    globs = tuple(str(g).strip().lower() for g in by_glob if str(g).strip())
    return DenyPolicy(ids=ids, names=names, globs=globs)


def is_denied(policy: DenyPolicy, name: str, cid: str) -> bool:
    nm = (name or "").strip().lower()
    if (cid or "").strip() in policy.ids:
        return True
    if nm and nm in policy.names:
        return True
    return bool(nm) and any(fnmatch.fnmatch(nm, g) for g in policy.globs)


# ── hard-coded exemptions (duplicated, each pinned by a test) ────────────────
#: == app._BLOCKED_CHANNEL_IDS. Duplicated, not imported: importing cora.app builds
#: the Bolt App (auth.test) inside the monthly script and the nightly monitor (A30).
BLOCKED_CHANNEL_IDS: frozenset = frozenset({"C0B2NMLK7CK"})
#: == info_intake.CHANNEL_ID. Duplicated for the same import-weight reason
#: (info_intake imports knowledge_review); pinned equal by a test.
INFO_FOR_CORA_CHANNEL_ID = "C0B5BNP6YKY"

# The keep-list DUPLICATE (A2). scripts/archive_sprawl_channels.py is never
# imported: it runs load_dotenv(override=True) at import. An AST pin test
# (tests/test_channel_archive_registry.py) fails the moment the two drift.
KEEP_IDS: frozenset = frozenset({
    "C0B2T18R3FG", "C0B2Z7Z7C84", "C0B4QT25AUT", "C0B5XHS0648", "C0B517928RL",
    "C0B5178T1N2", "C0B56T9SEUC", "C0B2E5Z8MTR", "C0BBUMAU4KG", "C0B3K6DEEAF",
})
KEEP_FINANCE: frozenset = frozenset({
    "hjrg-finance", "f3e-finance", "osn-finance", "hjrp-finance", "lex-finance",
    "llc-finance", "lts-finance", "lbhs-finance", "lla-finance",
    "osngm-finance", "osngw-finance", "osngf-finance", "osnvv-finance",
    "founder-finance",
})
KEEP_EXPLICIT: frozenset = frozenset({
    "founder-finance", "founder-operations", "general-do-not-use",
    "tiktok-shop-build", "tucson-site-launch",
    "wikipedia-presence-press-acquisition-build", "f3-production-run-2",
    "reddit-presence-90-day-build", "f3-pure-launch", "f3-ops-cockpit",
    "cowork-daily-briefs",
})
KEEP_PREFIXES: tuple = ("cora-",)
OSN_STORE_PREFIXES: tuple = ("osngw-", "osngm-", "osngf-", "osnvv-")


def keep_list_reason(cid: str, name: str) -> str | None:
    nm = (name or "").strip().lower()
    if cid in KEEP_IDS:
        return "keep-list id"
    if nm in KEEP_FINANCE or nm in KEEP_EXPLICIT:
        return "keep-list name"
    if nm.startswith(KEEP_PREFIXES):
        return "cora- prefix"
    if nm.startswith(OSN_STORE_PREFIXES):
        return "OSN store prefix"
    return None


def is_leadership_or_finance(name: str) -> bool:
    nm = (name or "").strip().lower()
    return nm.endswith("-leadership") or nm.endswith("-finance") or nm in KEEP_FINANCE


# ── the LEX belt (A6) ────────────────────────────────────────────────────────
LEX_NAME_PREFIXES: tuple = ("lex", "llc", "lts", "lbhs", "lla")
LEX_NAME_TOKENS: frozenset = frozenset({"lexington", "dta", "ddd", "hcbs", "copa"})
_SEGMENT_SPLIT_RE = re.compile(r"[-_]")


def lex_by_name(cid: str, name: str, registry: Registry | None) -> bool:
    """Any one belt => LEX: the routed entity is LEX-scoped; the name starts with a
    LEX prefix; a ``-``/``_`` segment is a LEX program token; the id or name sits in
    the registry's LEX section. Over-exempting is the safe direction."""
    nm = (name or "").strip().lower()
    if registry is not None and (cid in registry.lex_ids or (nm and nm in registry.lex_names)):
        return True
    if not nm:
        return False
    if nm.startswith(LEX_NAME_PREFIXES):
        return True
    if any(seg in LEX_NAME_TOKENS for seg in _SEGMENT_SPLIT_RE.split(nm)):
        return True
    try:
        from .. import entity_router  # noqa: PLC0415
        from ..web_guard import is_lex_scope  # noqa: PLC0415
        return bool(is_lex_scope(entity_router.route(nm)))
    except Exception:  # noqa: BLE001 -- a routing failure must not un-exempt
        return True


def lex_by_members(member_ids: list[str] | None, *, roles: Any = None) -> bool:
    """The membership belt: any member whose PRIMARY org_roles entity is LEX-scoped.

    Keyed on the primary ``entity``, not the ``entities`` access list: Harrison's own
    roster row lists LEX among ``entities`` (and so does the controller's), and he
    belongs to nearly every channel -- an ``entities`` belt would mark the whole
    workspace LEX and the lane could never propose a row. Members unreadable (None)
    => LEX (fail closed)."""
    if member_ids is None:
        return True
    try:
        if roles is None:
            from .. import org_roles as roles  # noqa: PLC0415
        from ..web_guard import is_lex_scope  # noqa: PLC0415
        for uid in member_ids:
            rec = roles.get_role(uid)
            if rec is not None and is_lex_scope(str(getattr(rec, "entity", "") or "")):
                return True
    except Exception:  # noqa: BLE001
        return True
    return False


def name_fp(name: str) -> str:
    return hashlib.sha256((name or "").strip().lower().encode("utf-8")).hexdigest()[:12]


def route_label(name: str) -> str:
    """Entity label for a card row (non-LEX rows only)."""
    try:
        from .. import entity_router  # noqa: PLC0415
        ent = entity_router.route(name)
        return ent if entity_router.is_mapped(name) else f"{ent} (catch-all)"
    except Exception:  # noqa: BLE001
        return "?"


@dataclass
class Context:
    """Everything the ONE classifier needs, loaded FRESH per scan and per tap (A5)."""
    registry: Registry
    deny: DenyPolicy | None
    bot_uid: str
    bot_id: str
    harrison_id: str
    keep_state: dict = field(default_factory=dict)       # cid -> {"count": n, "until": epoch}
    unarchive_state: dict = field(default_factory=dict)  # cid -> {"at": epoch, "by": uid}
    user_cache: dict = field(default_factory=dict)       # uid -> True (person) / False (bot/app)
    roles: Any = None
    store_ok: bool = True    # the proposals store + ledger were readable (keeps, unarchives)

    def blind_cause(self) -> str | None:
        if not self.store_ok:
            return "store_unreadable"
        if not self.registry.ok:
            return "registry_unreadable"
        if self.deny is None:
            return "policy_unreadable"
        if not self.bot_uid:
            return "bot_id_unknown"
        return None
