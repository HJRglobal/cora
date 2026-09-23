#!/usr/bin/env python3
r"""Mirror out-of-tree Claude-workspace knowledge INTO the Founder OS.

WHY THIS EXISTS (2026-09-02 knowledge-parity audit + design)
------------------------------------------------------------
Everything authored inside ``G:\My Drive\HJR-Founder-OS\`` already reaches Cora
(nightly ``static_md``). The knowledge Cora LACKS is the knowledge that never
touches that tree: the distilled memory tiers the Claude agents keep for
themselves (Claude Code ``MEMORY.md`` / ``project_*.md``, Cowork memory files),
the procedure encoded in HJR-custom skills, and the Cowork scheduled-task
estate's definitions (unrecoverable if the registry drops -- TOM 1nnnn).

This script copies those assets, deterministically and with NO LLM and NO
network, into two landing zones -- the D-057 split, by construction:

  ZONE-K  ``_shared/claude-workspace-mirror/``          -> KB-ingested via static_md
  ZONE-X  ``_shared/projects/cora/_mirror/``            -> KB-EXCLUDED (D-057 folder rule)

ZONE-K holds org knowledge (skill bodies, a task INDEX manifest, non-cora repo
memory, screened Cowork memory). ZONE-X holds Cora-operational metadata (full
Cowork task BODIES incl. LEX-prefixed, the cora checkout's memory incl. its
git worktrees such as cora-revops, the
quarantine index, removed files). Nothing that trips the deterministic
PHI/LEX/personal screen reaches ZONE-K unless Harrison name-opts-it-in
(``allow_files:`` in the yaml, D-194).

REVERSIBILITY (D-086): dry-run by default; ``--apply`` writes; a ``manifest.json``
is written BEFORE any mutation and re-written in a ``finally`` block;
``--revert <manifest>`` undoes a run. Every write lands under one of the two
mirror roots -- a guard raises otherwise, and a test asserts it.

The mirrored ``.md`` files are then ingested by the ordinary static_md sweep
(ZONE-K) or read directly by Cowork/DR (ZONE-X). This script writes NO KB rows
itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml
from dotenv import load_dotenv

load_dotenv()

CORA_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORA_REPO_ROOT / "src"))

from cora import drive_io, phi_guard  # noqa: E402
from cora.session_capture import _discover_cowork_roots  # noqa: E402

log = logging.getLogger("mirror-claude-workspace")

# ── Roots (env-overridable for tests) ────────────────────────────────────────
FOUNDER_OS_ROOT = Path(os.environ.get("CORA_MIRROR_FOUNDER_OS_ROOT",
                                      r"G:\My Drive\HJR-Founder-OS"))


def _zone_k_root() -> Path:
    ov = os.environ.get("CORA_MIRROR_ZONE_K_ROOT", "").strip()
    return Path(ov) if ov else FOUNDER_OS_ROOT / "_shared" / "claude-workspace-mirror"


def _zone_x_root() -> Path:
    ov = os.environ.get("CORA_MIRROR_ZONE_X_ROOT", "").strip()
    return Path(ov) if ov else FOUNDER_OS_ROOT / "_shared" / "projects" / "cora" / "_mirror"


CONFIG_PATH = CORA_REPO_ROOT / "data" / "maps" / "claude-workspace-mirror.yaml"

# The source classes this run knows about (for --only + parity reporting).
CLASSES = ("skills", "cowork_tasks", "code_memory", "cowork_memory")

# LEX token family -- same shape as decision_inbox/info_intake._LEX_TOKEN_RE, the
# D-145 intake screen. A ZONE-K candidate tripping this is quarantined.
_LEX_TOKEN_RE = re.compile(
    r"\blex(?:[-_][a-z0-9]+)*\b|\blexington[a-z]*\b|\b(?:lbhs|lts|lla)\b",
    re.IGNORECASE,
)


# ── Config ───────────────────────────────────────────────────────────────────
@dataclass
class Config:
    skills_allow: set[str]
    skills_deny_stock: set[str]
    tasks_default_root: str
    allow_files: set[str]
    max_file_bytes: int
    personal_families: list[str]


def load_config(path: Path = CONFIG_PATH) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    skills = data.get("skills", {}) or {}
    tasks = data.get("cowork_tasks", {}) or {}
    return Config(
        skills_allow={str(s).lower() for s in (skills.get("allow") or [])},
        skills_deny_stock={str(s).lower() for s in (skills.get("deny_stock") or [])},
        tasks_default_root=str(tasks.get("default_root") or
                               r"%USERPROFILE%\OneDrive\Documents\Claude\Scheduled"),
        allow_files={str(n).lower() for n in (data.get("allow_files") or [])},
        max_file_bytes=int(data.get("max_file_bytes") or 524288),
        personal_families=[str(f).lower() for f in (data.get("personal_families") or [])],
    )


# ── Discovery ────────────────────────────────────────────────────────────────
def _skill_files() -> list[Path]:
    """SKILL.md files under the Cowork skills-plugin root. Env CLAUDE_SKILLS_ROOT
    (a directory holding ``<name>/SKILL.md``) overrides discovery."""
    override = os.environ.get("CLAUDE_SKILLS_ROOT", "").strip()
    if override:
        return sorted(Path(override).glob("*/SKILL.md"))
    out: list[Path] = []
    for root in _discover_cowork_roots():
        out.extend(root.glob("skills-plugin/*/*/skills/*/SKILL.md"))
    return sorted(set(out))


def _cowork_memory_files() -> list[Path]:
    """Cowork memory ``.md`` files. Real store nests them under
    ``.../spaces/<uuid>/memory/*.md``. Env COWORK_MEMORY_ROOT overrides (any
    ``memory/*.md`` beneath it, no ``spaces`` requirement, for tests/relocation)."""
    override = os.environ.get("COWORK_MEMORY_ROOT", "").strip()
    roots = [Path(override)] if override else _discover_cowork_roots()
    require_spaces = not override
    out: list[Path] = []
    for root in roots:
        try:
            candidates = root.rglob("*.md")
        except OSError:
            continue
        for md in candidates:
            if md.parent.name != "memory":
                continue
            if require_spaces and "spaces" not in {p.name for p in md.parents}:
                continue
            out.append(md)
    return sorted(set(out))


def _code_projects_root() -> Path:
    ov = os.environ.get("CLAUDE_PROJECTS_ROOT", "").strip()
    return Path(ov) if ov else (Path.home() / ".claude" / "projects")


def _code_memory_files() -> list[Path]:
    """``~/.claude/projects/<slug>/memory/*.md`` across every slug."""
    root = _code_projects_root()
    out: list[Path] = []
    try:
        slugs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return out
    for slug in slugs:
        memdir = slug / "memory"
        if memdir.is_dir():
            out.extend(sorted(memdir.glob("*.md")))
    return out


def _tasks_root(cfg: Config) -> Path:
    ov = os.environ.get("COWORK_TASKS_ROOT", "").strip()
    if ov:
        return Path(ov)
    return Path(os.path.expandvars(cfg.tasks_default_root))


def _task_dirs(cfg: Config) -> list[Path]:
    """Each Cowork scheduled task = ``<root>/<taskid>/SKILL.md`` (skip ``_archive``)."""
    root = _tasks_root(cfg)
    out: list[Path] = []
    try:
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            if (d / "SKILL.md").is_file():
                out.append(d)
    except OSError:
        pass
    return out


# ── Slug -> (repo label, zone) ───────────────────────────────────────────────
# A Claude Code project slug is the session's cwd with every non-alphanumeric
# character replaced by "-" (C:\Users\Harri\code\cora -> C--Users-Harri-code-cora;
# the "." of .claude makes a worktree read ...-cora--claude-worktrees-<name>).
#
# Routing is by PATH PREFIX of the cora checkout (2026-09-03, cowork-side
# findings §2): the base repo, every git worktree under its .claude/worktrees/,
# and every sibling checkout are the cora codebase -> ZONE-X (D-057).
# cora-revops is a git WORKTREE of this same repository on
# claude/revops-loop-2026-08-02 (the 9/3 cowork-side findings saw it marked
# prunable; registration state varies), NOT a fork. The regex belt
# (_CORA_SLUG_RE) stays for cora-ish slugs that do not live under the checkout
# path (e.g. the Founder-OS _shared/projects/cora working-dir slug, which also
# carries a MEMORY.md); each is labelled by its FULL slug so no two can collide.
_CORA_SLUG_RE = re.compile(r"(?:^|[-])cora(?:[-]|$)", re.IGNORECASE)
_WORKTREES_TAIL = "--claude-worktrees-"


def _path_to_slug(p: Path | str) -> str:
    """Claude Code's project-slug encoding of a path (lossy: every non-alphanumeric
    character becomes ``-``). Encode-and-compare, never decode."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def _cora_base_repo_root() -> Path:
    """The BASE cora checkout. When this script runs from a worktree its ``.git``
    is a FILE (``gitdir: <base>/.git/worktrees/<name>``); resolve to ``<base>`` so
    the prefix is the same wherever the mirror runs from."""
    root = Path(CORA_REPO_ROOT)
    dotgit = root / ".git"
    if dotgit.is_file():
        try:
            line = dotgit.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            line = ""
        if line.lower().startswith("gitdir:"):
            gd = Path(line.split(":", 1)[1].strip())
            if not gd.is_absolute():
                # git >= 2.48 can write RELATIVE gitdir paths (worktree.useRelativePaths
                # / --relative-paths); they are relative to the .git file's directory.
                gd = (dotgit.parent / gd).resolve()
            if gd.parent.name == "worktrees" and gd.parent.parent.name == ".git":
                return gd.parent.parent.parent
    return root


def _cora_slug_prefix() -> str:
    return _path_to_slug(_cora_base_repo_root()).lower()


def _cora_codebase_tail(slug: str) -> str | None:
    """``""`` for the base checkout, ``-revops`` for a sibling checkout,
    ``--claude-worktrees-<name>`` for a worktree; ``None`` when the slug is not
    under the cora checkout path (``...-coral-reef`` is not ``cora``)."""
    low = slug.lower()
    prefix = _cora_slug_prefix()
    if low == prefix:
        return ""
    if low.startswith(prefix + "-"):
        return low[len(prefix):]
    return None


def _registered_worktrees() -> dict[str, dict]:
    """``{slug: {"path", "gitfile", "locked", "base"}}`` for the base checkout plus
    every worktree registered in ``<base>/.git/worktrees/<name>/gitdir`` -- read
    from disk, no git subprocess. ``gitfile`` is the worktree's ``.git`` FILE,
    which is what git itself checks: a registration whose gitfile is gone is
    "prunable" unless a ``locked`` marker sits beside the registration. Relative
    gitdir paths (git >= 2.48 ``worktree.useRelativePaths``) are resolved against
    the registration directory."""
    base = _cora_base_repo_root()
    out: dict[str, dict] = {
        _path_to_slug(base).lower(): {"path": base, "gitfile": base / ".git", "locked": False, "base": True},
    }
    try:
        entries = list((base / ".git" / "worktrees").iterdir())
    except OSError:
        entries = []
    for e in entries:
        try:
            gitdir = (e / "gitdir").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not gitdir:
            continue
        gf = Path(gitdir)
        if not gf.is_absolute():
            gf = (e / gf).resolve()
        wt = gf.parent if gf.name == ".git" else gf
        out[_path_to_slug(wt).lower()] = {
            "path": wt, "gitfile": gf, "locked": (e / "locked").exists(), "base": False,
        }
    return out


def _worktree_status(slug: str) -> tuple[str, Path | None]:
    """For a cora-codebase slug:
    ``("ok" | "missing" | "locked" | "unregistered" | "unknown", path)``.

    ``ok`` = the checkout is there (registered gitfile present; the base checkout;
    or a session started in a SUBDIRECTORY of the checkout -- ``cd scripts; claude``
    slugs as ``<base>-scripts``, the same checkout, not a sibling worktree);
    ``missing`` = registered (or decodable) but its gitfile/dir is gone -- git's
    prunable; ``locked`` = registered + locked marker + gone (git will NOT prune
    it); ``unregistered`` = the directory exists but git no longer lists it;
    ``unknown`` = a slug shape this decoder does not know. The caller REPORTS
    these; it never throws and never skips the memory files themselves."""
    low = slug.lower()
    reg = _registered_worktrees()
    if low in reg:
        r = reg[low]
        if r.get("base"):
            return ("ok" if r["path"].is_dir() else "missing"), r["path"]
        if r["gitfile"].is_file():
            return "ok", r["path"]
        return ("locked" if r["locked"] else "missing"), r["path"]
    tail = _cora_codebase_tail(slug)
    base = _cora_base_repo_root()
    if tail is None:
        return "unknown", None
    if tail.startswith(_WORKTREES_TAIL):
        cand = base / ".claude" / "worktrees" / tail[len(_WORKTREES_TAIL):]
        return ("unregistered" if cand.is_dir() else "missing"), cand
    if tail.startswith("-"):
        cand = base.parent / (base.name + tail)          # sibling checkout, e.g. cora-revops
        if cand.is_dir():
            return "unregistered", cand                   # exists but git no longer lists it
        rest = tail[1:]
        for sub in (base / rest, base.joinpath(*[part for part in rest.split("-") if part])):
            if sub.is_dir():
                return "ok", sub                          # a subdirectory session of this checkout
        return "missing", cand
    return "unknown", None


def slug_repo_zone(slug: str) -> tuple[str, str]:
    """Map a Claude Code project slug to (repo_label, zone).

    Anything under the cora checkout path -- the base repo, its
    ``.claude/worktrees/*``, sibling checkouts like ``cora-revops`` -- is
    Cora-operational -> ZONE-X, each under its OWN label (every checkout carries
    a ``MEMORY.md``; one shared label would silently collide). Any OTHER repo's
    memory is org knowledge -> ZONE-K.
    """
    low = slug.lower()
    tail = _cora_codebase_tail(slug)
    if tail is not None:
        if tail == "":
            return "cora", "X"
        if tail.startswith(_WORKTREES_TAIL):
            return "cora-worktree-" + tail[len(_WORKTREES_TAIL):].strip("-"), "X"
        return "cora" + tail.rstrip("-"), "X"            # sibling checkout: cora-revops
    # Belt: cora-ish slugs OUTSIDE the checkout path (the regex covers a stray
    # cora-revops too; a constant label here would collide with the sibling's).
    if _CORA_SLUG_RE.search(low):
        # e.g. the Founder-OS _shared/projects/cora working-dir slug: ZONE-X,
        # labelled by its full slug so it can never collide with the checkout's
        # own "cora" label (both carry a MEMORY.md).
        return low.strip("-") or "cora", "X"
    # non-cora repo: derive a label from the slug's repo component.
    m = re.search(r"code-([a-z0-9][a-z0-9-]*?)(?:--|$)", low)
    label = m.group(1) if m else (low.rsplit("-", 1)[-1] or "unknown-repo")
    return label, "K"


# ── Screens (ZONE-K only, deterministic, no LLM) ─────────────────────────────
def screen_reason(text: str, cfg: Config) -> str | None:
    """Return a short reason string if the text must be quarantined from ZONE-K,
    else None. Union of the PROSE PHI screen, the LEX token family, and the
    personal-container keyword families.

    2026-09-08 (ingest-integrity I2; cq-e4b0d20a313f folded): the first cut used
    phi_guard.is_any_phi, whose is_lex_billing_status_phi leg fires on ordinary
    business vocabulary -- "status APPROVED", "restock pending", "CLAIMS CHECK" --
    so 5 of 6 skill quarantines and ~14 F3 memory quarantines in the first mirror
    run were false positives and the lane needed per-file allow_files opt-ins.
    Skills and memory files are PROSE about many things, the same object class as
    a session transcript, so they take the same value-/individual-shaped screen
    (phi_guard.is_prose_phi_risk). LEX tokens and the personal families are
    unchanged -- a LEX-named memory still quarantines as "lex-token"."""
    legs = phi_guard.prose_phi_legs(text)
    if legs:
        return f"phi ({','.join(legs)})"
    # A mirrored file is PUBLISHED to a shared Drive zone, so a bare clinical
    # DIAGNOSIS term quarantines it too -- the prose screen alone lets "the member
    # has autism" through (D-051 lens A). Curated dx terms only: the "diagnosis of
    # X" phrase is the ops-sense trip token the cq-e4b0d20a313f fold removed, and
    # bare med names are F3E / OSN product copy.
    if phi_guard.has_bare_clinical_dx_term(text):
        return "phi (clinical-term)"
    if _LEX_TOKEN_RE.search(text):
        return "lex-token"
    low = text.lower()
    for fam in cfg.personal_families:
        if fam and fam in low:
            return f"personal ({fam})"
    return None


# ── Planned writes + provenance ──────────────────────────────────────────────
@dataclass
class Planned:
    dest: Path                 # absolute, must be under a zone root
    zone: str                  # "K" or "X"
    text: str                  # full file content (provenance header included)
    source: str = ""           # source path (for manifest/provenance)
    sha256: str = ""           # sha256 of the SOURCE content (dedup / manifest)
    cls: str = ""              # source class
    allow_override: bool = False  # ZONE-K: the screen tripped and allow_files released it (keyed on dest)


@dataclass
class Plan:
    writes: list[Planned] = field(default_factory=list)
    quarantined: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    warns: list[str] = field(default_factory=list)
    roots_found: dict[str, bool] = field(default_factory=dict)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    unpinned_tasks: list[str] = field(default_factory=list)  # task names with no model pin
    task_models: dict[str, str] = field(default_factory=dict)  # task name -> model pin
    # Code #13 slice 9c: per-task run-marker evaluation, keyed by FOLDER id (or a
    # withheld key when the id trips the ZONE-K screen). See evaluate_run_marker.
    run_markers: dict[str, dict] = field(default_factory=dict)
    cadence_error: str | None = None  # the cadence map could not be read (every task -> unknown cadence)


_AZ_OFFSET = "-07:00"  # America/Phoenix (no DST)


def _now_stamps() -> tuple[str, str]:
    utc = datetime.now(timezone.utc)
    az = utc.astimezone(timezone(_td_hours(-7)))
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), az.strftime("%Y-%m-%d %H:%M AZ")


def _td_hours(h: int):
    from datetime import timedelta
    return timedelta(hours=h)


def _provenance(source: str, sha: str) -> str:
    utc, az = _now_stamps()
    return (
        f"<!-- MIRROR-SOURCE: {source} -->\n"
        f"<!-- MIRROR-SHA256: {sha} -->\n"
        f"<!-- MIRROR-AT: {utc} / {az} -->\n"
        f"<!-- READ-ONLY MIRROR -- edit the source, not this file; "
        f"working knowledge, not canon. -->\n\n"
    )


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _rel_source(path: Path) -> str:
    """A stable, non-secret source label. Home-relative when possible."""
    try:
        return "~/" + str(path.relative_to(Path.home())).replace("\\", "/")
    except ValueError:
        return str(path)


def _fm_field(text: str, key: str) -> str:
    """Read a scalar frontmatter field from the FIRST --- block only."""
    m = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    if not m:
        return ""
    fm = m.group(1)
    fmatch = re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", fm)
    if not fmatch:
        return ""
    return fmatch.group(1).strip().strip('"').strip("'")


_SCHEDULE_HINT_RE = re.compile(
    r"\b("
    r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|fri(?:day)?|"
    r"sat(?:urday)?|sun(?:day)?|daily|weekly|monthly|hourly|"
    r"\d{1,2}:\d{2}\s?(?:am|pm)?(?:\s?az)?|\d{1,2}\s?(?:am|pm)\b"
    r")",
    re.IGNORECASE,
)


def _schedule_hint(name: str, description: str) -> str:
    """Best-effort cadence extracted from the task NAME + description prose (the
    SKILL.md carries NO structured cron field -- it lives in the app DB, off
    disk). '' when nothing parses."""
    hits: list[str] = []
    for src in (name, description):
        for m in _SCHEDULE_HINT_RE.finditer(src or ""):
            hits.append(m.group(0))
            if len(hits) >= 4:
                break
    # de-dup preserving order
    seen: set[str] = set()
    out = []
    for h in hits:
        k = h.lower()
        if k not in seen:
            seen.add(k)
            out.append(h)
    return " ".join(out)


def _entity_prefix(name: str) -> str:
    first = name.split("-", 1)[0].lower()
    if first == "cowork":
        return "cowork-cora"
    return first


# ── Cowork run markers (Code #13 slice 9c, cq-06045f418bd2) ──────────────────
# THE CLASS (same as src/cora/run_marker.py): a task that fires and writes nothing is
# indistinguishable from one that never fired. The Cowork estate cannot import cora
# and usually has no G: in bash, so its contract is a FILE DROP, written with the
# file tools by a footer every SKILL.md body ends with
# (deployment/cowork-run-marker-footer.md):
#
#     <ZONE-K>/_runs/<folder-id>/<YYYY-MM-DD>.json
#     {"ok": bool, "outputs": [paths / permalinks], "duration_s": number, "notes": str}
#
# The KEY is the task's FOLDER id (d.name) -- the same id the ZONE-X body dest uses --
# never the frontmatter `name:`. `outputs` is a LIST in this contract (the in-repo
# ledger's is an int); the reader len()s it. Cadence is DECLARED in
# data/maps/cowork-run-cadence.yaml because the SKILL.md carries no cron; the reader
# normalizes to run_marker.evaluate's two alarms (MISSED FIRE / FIRED BUT WROTE
# NOTHING). The mirror only READS _runs/: it never writes, removes or stubs anything
# there (removal detection covers only its own manifest dests -- test-pinned).
RUN_MARKERS_DIRNAME = "_runs"
CADENCE_PATH = CORA_REPO_ROOT / "data" / "maps" / "cowork-run-cadence.yaml"
RUN_MARKER_FIELDS = ("ok", "outputs", "duration_s", "notes")
RUN_MARKER_FOOTER_MARKER = "<!-- run-marker-footer v1 -->"
_MARKER_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# Status vocabulary. The first four are the health lane's WARN triggers; a task with
# a declared cadence and no marker inside 2x that cadence is RM_DID_NOT_RUN -- never
# RM_OK. RM_UNKNOWN_CADENCE is neither ok nor an alarm: a gap cannot be computed.
RM_OK = "ok"
RM_DID_NOT_RUN = "did not run"
RM_WROTE_NOTHING = "fired but wrote nothing"
RM_UNREADABLE = "unreadable marker"
RM_REPORTED_ERROR = "reported error"
RM_UNKNOWN_CADENCE = "unknown cadence"
# Code #14 R14-6 (ruling 9.11(ii)): a row whose cadence entry carries a `registered:`
# date (the day the footer was injected) and that has NO marker yet reads 'awaiting
# first marker' for ONE cadence after that date -- neither ok nor an alarm. It is
# deliberately NOT in RUN_MARKER_ALARM_STATUSES and never collapses to RM_OK (a blind
# row must never render as clean). No `registered:` = no excuse (today's behaviour).
RM_AWAITING_FIRST = "awaiting first marker"
RUN_MARKER_ALARM_STATUSES = (RM_DID_NOT_RUN, RM_WROTE_NOTHING, RM_UNREADABLE, RM_REPORTED_ERROR)
_RM_RENDER_ORDER = (RM_DID_NOT_RUN, RM_WROTE_NOTHING, RM_UNREADABLE, RM_REPORTED_ERROR,
                    RM_UNKNOWN_CADENCE, RM_AWAITING_FIRST, RM_OK)


def _registered_day(value) -> date | None:
    """The DATE part of a cadence row's `registered:` value, or ``None`` (= absent, no
    grace). YAML hands back a ``date`` for ``2026-09-19`` and a ``datetime`` for a full
    timestamp; a quoted value arrives as a str and is parsed as ISO. Anything else
    (bool, int, 'soon', '') is unparseable and therefore counts as ABSENT -- a typo
    must never become an excuse."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):   # before `date`: datetime IS-A date
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s).date()
        except ValueError:
            pass
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    return None


def _today_az() -> date:
    """Today in America/Phoenix (the date the footer tells a task to stamp)."""
    return datetime.now(timezone(_td_hours(-7))).date()


def load_cadence(path: Path | None = None) -> tuple[dict[str, dict], str | None]:
    """``({folder-id: {cadence_hours, expects_output, note}}, error)`` from the DECLARED
    cadence map. Fail-soft: a missing/unparseable file yields ``({}, "<reason>")`` so
    every task reads 'unknown cadence' and the parity report says why -- never a crash,
    never a silent 'ok'."""
    p = Path(path) if path else CADENCE_PATH
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return {}, "cadence map is not a mapping"
    out: dict[str, dict] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[str(k)] = v
    return out, None


def _runs_dir(task_id: str) -> Path:
    return _zk(RUN_MARKERS_DIRNAME, task_id)


def read_run_marker(task_id: str) -> dict | None:
    """The NEWEST ``_runs/<task_id>/<YYYY-MM-DD>.json`` by FILENAME (never mtime --
    Drive File Stream rewrites mtimes on sync). ``None`` when the folder or any dated
    file is absent. A file that exists but is not a JSON object carrying ``ok`` and a
    LIST ``outputs`` -> ``{"unreadable": True, ...}`` (reported, never excused). Every
    G: touch is bounded via drive_io; a mount failure reads as unreadable (the honest
    answer), never as 'no marker' (which would read as 'did not run')."""
    d = _runs_dir(task_id)
    try:
        files = drive_io.glob(d, "*.json", timeout=5.0, retry_seconds=0.0)
    except Exception as exc:  # noqa: BLE001 -- a gone mount must not crash the mirror
        return {"date": "", "unreadable": True, "error": type(exc).__name__}
    dated = sorted(f for f in files if _MARKER_DATE_RE.match(f.stem))
    if not dated:
        return None
    newest = dated[-1]
    try:
        data = json.loads(drive_io.read_text(newest, timeout=5.0, retry_seconds=0.0))
    except Exception as exc:  # noqa: BLE001 -- malformed JSON / read failure
        return {"date": newest.stem, "unreadable": True, "error": type(exc).__name__}
    if not isinstance(data, dict):
        return {"date": newest.stem, "unreadable": True, "error": "not a JSON object"}
    for req in ("ok", "outputs"):
        if req not in data:
            return {"date": newest.stem, "unreadable": True, "error": f"missing field {req}"}
    if not isinstance(data.get("outputs"), (list, tuple)):
        return {"date": newest.stem, "unreadable": True, "error": "outputs is not a list"}
    return {"date": newest.stem, "unreadable": False, "ok": bool(data.get("ok")),
            "outputs": len(data["outputs"]), "duration_s": data.get("duration_s")}


def evaluate_run_marker(task_id: str, marker: dict | None, cadence_entry: dict | None,
                        *, today: date) -> dict:
    """One task's row: ``{task, status, detail, last_marker, ok, outputs, cadence_hours,
    expects_output, note}``. The wording of the two cadence alarms mirrors
    run_marker.evaluate (MISSED FIRE / FIRED BUT WROTE NOTHING).

    Order matters: an unreadable marker is reported as such even when the cadence is
    unknown; a task with no cadence row is 'unknown cadence' (a gap is not
    computable), NOT 'did not run'; a task with a row and no marker inside 2x its
    cadence is 'did not run' -- the row this whole slice exists to render honestly.
    R14-6: a markerless row whose entry carries a parseable, non-future `registered:`
    date less than ONE cadence ago is 'awaiting first marker' (non-alarm, never ok);
    the grace applies only while NO marker exists -- a marker that exists is judged
    exactly as before."""
    row: dict = {"task": task_id, "status": RM_OK, "detail": "", "last_marker": "",
                 "ok": None, "outputs": None, "cadence_hours": None,
                 "expects_output": None, "note": ""}
    if isinstance(cadence_entry, dict):
        try:
            row["cadence_hours"] = float(cadence_entry.get("cadence_hours") or 0) or None
        except (TypeError, ValueError):
            row["cadence_hours"] = None
        row["expects_output"] = bool(cadence_entry.get("expects_output"))
        row["note"] = str(cadence_entry.get("note") or "")
    if marker is not None:
        row["last_marker"] = str(marker.get("date") or "")
        if marker.get("unreadable"):
            row["status"] = RM_UNREADABLE
            row["detail"] = (f"marker {row['last_marker'] or '(listing failed)'} unreadable "
                             f"({marker.get('error') or 'unknown'}) -- contract: a JSON object "
                             f"with ok + outputs[] + duration_s + notes")
            return row
        row["ok"] = marker.get("ok")
        row["outputs"] = int(marker.get("outputs") or 0)
    cadence_h = row["cadence_hours"]
    if cadence_h is None:
        row["status"] = RM_UNKNOWN_CADENCE
        row["detail"] = ("no row in data/maps/cowork-run-cadence.yaml -- gap not computable; "
                         + (f"last marker {row['last_marker']}" if marker else "no marker"))
        return row
    window_h = cadence_h * 2
    if marker is None:
        # R14-6: the `registered:` grace -- ONE cadence (ruling 9.11(ii)), gated on the
        # row carrying a parseable date. Mechanism parity with run_marker.evaluate (a
        # registered-gated grace; no registered = no excuse), NOT numeric parity: that
        # evaluator excuses 2x cadence. A date in the FUTURE is treated as absent
        # (fail-closed: a typo'd year must not silence a real 'did not run').
        reg = _registered_day(cadence_entry.get("registered")) if isinstance(cadence_entry, dict) else None
        if reg is not None and reg <= today and (today - reg).days * 24 < cadence_h:
            row["status"] = RM_AWAITING_FIRST
            row["detail"] = (f"registered {reg.isoformat()}, first marker due within "
                             f"{cadence_h:.0f}h (1x cadence grace) -- not yet an alarm, not ok")
            return row
        row["status"] = RM_DID_NOT_RUN
        row["detail"] = (f"no run marker ever recorded (cadence {cadence_h:.0f}h) -- MISSED FIRE, "
                         f"or the footer is not yet injected")
        if reg is not None:
            row["detail"] += (f" (registered {reg.isoformat()} is in the future -- ignored)"
                              if reg > today else
                              f" (registered {reg.isoformat()}; the 1x cadence grace has elapsed)")
        return row
    try:
        marker_day = date.fromisoformat(row["last_marker"])
    except ValueError:
        row["status"] = RM_UNREADABLE
        row["detail"] = f"marker filename {row['last_marker']!r} is not a YYYY-MM-DD date"
        return row
    age_h = (today - marker_day).days * 24.0
    if age_h > window_h:
        row["status"] = RM_DID_NOT_RUN
        row["detail"] = (f"last marker {row['last_marker']} is {age_h:.0f}h old "
                         f"(cadence {cadence_h:.0f}h) -- MISSED FIRE")
        return row
    if row["expects_output"] and row["outputs"] == 0:
        row["status"] = RM_WROTE_NOTHING
        row["detail"] = (f"FIRED BUT WROTE NOTHING ({row['last_marker']}, ok={row['ok']}) -- "
                         f"exit code alone cannot see this")
        return row
    if row["ok"] is False:
        row["status"] = RM_REPORTED_ERROR
        row["detail"] = f"last marker {row['last_marker']} reports ok=false ({row['outputs']} output(s))"
        return row
    row["detail"] = f"marker {row['last_marker']}, {row['outputs']} output(s), within {window_h:.0f}h"
    return row


def run_marker_summary(plan: "Plan") -> dict:
    """The machine-readable roll-up for mirror-status.json (read by the 08:45 health
    check + the Monday digest): per-alarm id lists, counts, and coverage as
    'N of M tasks have a marker'."""
    rows = plan.run_markers
    out: dict = {
        "total": len(rows),
        "with_marker": sum(1 for r in rows.values() if r.get("last_marker") or r.get("status") == RM_UNREADABLE),
        "ok": sum(1 for r in rows.values() if r.get("status") == RM_OK),
        "unknown_cadence": sum(1 for r in rows.values() if r.get("status") == RM_UNKNOWN_CADENCE),
        "awaiting_first": sum(1 for r in rows.values() if r.get("status") == RM_AWAITING_FIRST),
        "did_not_run": sorted(k for k, r in rows.items() if r.get("status") == RM_DID_NOT_RUN),
        "wrote_nothing": sorted(k for k, r in rows.items() if r.get("status") == RM_WROTE_NOTHING),
        "unreadable": sorted(k for k, r in rows.items() if r.get("status") == RM_UNREADABLE),
        "reported_error": sorted(k for k, r in rows.items() if r.get("status") == RM_REPORTED_ERROR),
        "cadence_map_error": plan.cadence_error,
    }
    out["coverage"] = f"{out['with_marker']} of {out['total']}"
    return out


# ── Plan builders ─────────────────────────────────────────────────────────────
def _record(plan: Plan, cls: str, key: str) -> None:
    plan.counts.setdefault(cls, {}).setdefault(key, 0)
    plan.counts[cls][key] += 1


def _zk(*parts: str) -> Path:
    return _zone_k_root().joinpath(*parts)


def _zx(*parts: str) -> Path:
    return _zone_x_root().joinpath(*parts)


def _mirror_key(dest: Path) -> str:
    """Stable, UNIQUE identity for a ZONE-K candidate: its path relative to the
    ZONE-K root (e.g. ``skills/cascade.SKILL.md``, ``cowork-memory/<space>/MEMORY.md``).
    The bare basename collides (every skill is ``SKILL.md``; ``MEMORY.md`` repeats
    across spaces/repos), so allow_files must key on this, not the basename."""
    try:
        return dest.relative_to(_zone_k_root()).as_posix()
    except ValueError:
        return dest.name


def _allow_files_hit(key: str, cfg: Config) -> bool:
    """A D-194 opt-in matches the FULL mirror key ONLY (never the bare basename).

    D-051 (2026-09-03, PHI lens finding 5): basenames collide massively (every
    skill body is ``SKILL.md``; ``MEMORY.md``/``project_*.md`` repeat across every
    repo and space), so a basename opt-in would silently un-gate every same-named
    sibling -- releasing screened content for all of them. The quarantine INDEX
    shows the full key, so Harrison copies an unambiguous one."""
    return key.lower() in cfg.allow_files


def _add_zone_k_source(plan: Plan, cfg: Config, *, dest: Path, source: Path,
                       text: str, cls: str) -> bool:
    """Screen + size-gate a ZONE-K source file, then plan the write (or quarantine).

    Returns True iff the body was actually planned for mirroring (so a caller's
    INDEX row is only emitted for content that reached ZONE-K -- D-051 PHI finding 2).

    The screen runs over the CONTENT *and* the derived identifiers that ride into
    ZONE-K with it -- the mirror key (dest path) and the source path label the
    provenance header embeds (D-051 PHI finding 1: a LEX/client token in a repo
    label or filename must not reach the KB-ingested zone just because the body
    is clean)."""
    key = _mirror_key(dest)
    src_label = _rel_source(source)
    if len(text.encode("utf-8", "replace")) > cfg.max_file_bytes:
        plan.warns.append(f"[{cls}] {key}: over {cfg.max_file_bytes}B cap -- skipped (never truncated)")
        _record(plan, cls, "skipped_oversize")
        return False
    # Screen the content AND everything derived that lands in ZONE-K with it.
    reason = screen_reason("\n".join([text, key, src_label]), cfg)
    if reason and not _allow_files_hit(key, cfg):
        plan.quarantined.append((key, f"{cls}: {reason}"))
        _record(plan, cls, "quarantined")
        return False
    if reason:  # allow_files opt-in overrode the screen
        _record(plan, cls, "allowlisted_override")
    sha = _sha_text(text)
    plan.writes.append(Planned(dest=dest, zone="K",
                               text=_provenance(src_label, sha) + text,
                               source=src_label, sha256=sha, cls=cls,
                               allow_override=bool(reason)))
    _record(plan, cls, "mirrored")
    return True


def _add_zone_x_source(plan: Plan, cfg: Config, *, dest: Path, source: Path,
                       text: str, cls: str) -> None:
    """Plan a ZONE-X body write (no screen -- ZONE-X is not KB-ingested). Size-gated."""
    if len(text.encode("utf-8", "replace")) > cfg.max_file_bytes:
        plan.warns.append(f"[{cls}] {source.name}: over {cfg.max_file_bytes}B cap -- skipped")
        _record(plan, cls, "skipped_oversize")
        return
    sha = _sha_text(text)
    plan.writes.append(Planned(dest=dest, zone="X",
                               text=_provenance(_rel_source(source), sha) + text,
                               source=_rel_source(source), sha256=sha, cls=cls))
    _record(plan, cls, "mirrored")


def plan_skills(plan: Plan, cfg: Config) -> None:
    files = _skill_files()
    plan.roots_found["skills"] = bool(files) or bool(os.environ.get("CLAUDE_SKILLS_ROOT"))
    if not files and not plan.roots_found["skills"]:
        plan.warns.append("skills: NOT FOUND -- no skills-plugin root discovered")
    index_rows: list[tuple[str, str, int, str, str]] = []
    for f in files:
        skill = f.parent.name.lower()
        if skill in cfg.skills_deny_stock:
            _record(plan, "skills", "denied_stock")
            continue
        if skill not in cfg.skills_allow:
            plan.warns.append(f"skills: '{skill}' not in allowlist -- not mirrored (add to skills.allow)")
            _record(plan, "skills", "unknown_not_allowlisted")
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        desc = _fm_field(text, "description")
        try:
            st = f.stat()
        except OSError:
            continue
        mirrored = _add_zone_k_source(plan, cfg, dest=_zk("skills", f"{skill}.SKILL.md"),
                                      source=f, text=text, cls="skills")
        if mirrored:  # D-051 PHI finding 2: no INDEX row for a quarantined body
            index_rows.append((skill, desc, st.st_size,
                               datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                               _sha_text(text)[:12]))
    if index_rows:
        lines = ["# Skills INDEX (HJR-custom Cowork skills)", "",
                 "| skill | description | bytes | mtime | sha256 |",
                 "|---|---|---|---|---|"]
        for name, desc, size, mtime, sha in sorted(index_rows):
            lines.append(f"| {name} | {_md_cell(desc)} | {size} | {mtime} | {sha} |")
        plan.writes.append(Planned(dest=_zk("skills", "INDEX.md"), zone="K",
                                   text=_generated_header("skills/INDEX.md") + "\n".join(lines) + "\n",
                                   cls="skills"))


def plan_cowork_tasks(plan: Plan, cfg: Config, *, today: date | None = None) -> None:
    dirs = _task_dirs(cfg)
    root = _tasks_root(cfg)
    plan.roots_found["cowork_tasks"] = root.exists()
    if not root.exists():
        plan.warns.append(f"cowork_tasks: NOT FOUND -- task store {root} missing")
        return
    # Code #13 slice 9c: the DECLARED cadence map + the AZ date the markers are
    # stamped in. `today` is injectable so tests pin the clock.
    today = today or _today_az()
    cadence, cadence_err = load_cadence()
    plan.cadence_error = cadence_err
    if cadence_err:
        plan.warns.append(f"cowork_tasks: run-cadence map {CADENCE_PATH.name} unreadable "
                          f"({cadence_err}) -- every task reads 'unknown cadence'")
    index_rows: list[tuple[str, str, str, str, str, str]] = []
    for d in dirs:
        f = d / "SKILL.md"
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            st = f.stat()
        except OSError:
            continue
        name = _fm_field(text, "name") or d.name
        model = _fm_field(text, "model") or "unpinned"
        if model == "unpinned":
            plan.unpinned_tasks.append(name)
        plan.task_models[name] = model
        desc = _fm_field(text, "description")
        # ZONE-X: the FULL body (frontmatter + prompt), LEX-prefixed included.
        _add_zone_x_source(plan, cfg,
                           dest=_zx("cowork-scheduled-tasks", f"cora-mirror-{d.name}.md"),
                           source=f, text=text, cls="cowork_tasks")
        # ZONE-K INDEX row: manifest only, screened. Opt-in for a task is by its
        # id (there is no per-task ZONE-K file to key a mirror-key on).
        opted = d.name.lower() in cfg.allow_files
        # D-051 PHI finding 3: the redaction must cover the task NAME + entity, not
        # only the description -- the trip-test already includes the name, so a LEX
        # task name (cowork-cora-lex-lbhs-*) would otherwise ride into ZONE-K raw.
        # If the NAME itself trips, the whole row is withheld; if only the
        # name+desc trips (name clean), just the description is withheld.
        name_reason = screen_reason(name, cfg)
        pair_reason = screen_reason(f"{name} {desc}", cfg)
        if name_reason and not opted:
            row_name, row_ent, row_desc = "[task withheld -- LEX/PHI screen]", "--", "[withheld]"
            _record(plan, "cowork_tasks", "row_redacted")
        elif pair_reason and not opted:
            row_name, row_ent = name, _entity_prefix(d.name)
            row_desc = "[details withheld -- see ZONE-X body]"
            _record(plan, "cowork_tasks", "desc_redacted")
        else:
            row_name, row_ent, row_desc = name, _entity_prefix(d.name), desc
        # Code #13 slice 9c: the run-marker read, keyed by FOLDER id. Both the parity
        # report and mirror-status.json are ZONE-K, so a folder id that trips the
        # screen is withheld behind a hash key (same posture as the INDEX row above;
        # the hash lets two withheld tasks stay distinguishable without naming either).
        rm_row = evaluate_run_marker(d.name, read_run_marker(d.name), cadence.get(d.name),
                                     today=today)
        id_reason = screen_reason(d.name, cfg)
        rm_key = d.name if (not id_reason or opted) else f"[withheld-{_sha_text(d.name)[:8]}]"
        rm_row["task"] = rm_key
        plan.run_markers[rm_key] = rm_row
        sched = _schedule_hint(name, desc)
        index_rows.append((
            row_name, sched or "unspecified", model, row_ent,
            datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
            _sha_text(text)[:12], row_desc,
        ))
    if index_rows:
        lines = ["# Cowork scheduled-task INDEX (manifest only -- bodies live in ZONE-X)",
                 "",
                 "Schedule + enabled state are NOT on disk (the SKILL.md carries "
                 "name/model/description only; cron + enabled live in the app DB). "
                 "`schedule` below is a best-effort hint parsed from the name + "
                 "description prose. Full prompt bodies: "
                 "`_shared/projects/cora/_mirror/cowork-scheduled-tasks/`.",
                 "",
                 "| task | schedule (hint) | model | entity | mtime | sha256 | description |",
                 "|---|---|---|---|---|---|---|"]
        for row in sorted(index_rows):
            name, sched, model, ent, mtime, sha, rdesc = row  # type: ignore[misc]
            lines.append(f"| {_md_cell(name)} | {_md_cell(sched)} | {model} | {ent} | "
                         f"{mtime} | {sha} | {_md_cell(rdesc)} |")
        plan.writes.append(Planned(dest=_zk("cowork-scheduled-tasks", "INDEX.md"), zone="K",
                                   text=_generated_header("cowork-scheduled-tasks/INDEX.md")
                                        + "\n".join(lines) + "\n",
                                   cls="cowork_tasks"))
        _record(plan, "cowork_tasks", "indexed")


def plan_code_memory(plan: Plan, cfg: Config) -> None:
    files = _code_memory_files()
    plan.roots_found["code_memory"] = _code_projects_root().exists()
    if not files:
        plan.warns.append("code_memory: no ~/.claude/projects/<slug>/memory/*.md found "
                          "(non-cora repos may simply have none)")
    # Worktree check per cora-codebase slug (cowork-side findings §2): a slug whose
    # git worktree is prunable/absent is REPORTED as NOT FOUND -- never thrown,
    # never silently skipped. Its memory files still exist and are still mirrored
    # (to ZONE-X); only the worktree behind them is gone.
    per_slug: dict[str, int] = {}
    for f in files:
        per_slug[f.parent.parent.name] = per_slug.get(f.parent.parent.name, 0) + 1
    for slug, n in sorted(per_slug.items()):
        if _cora_codebase_tail(slug) is None:
            continue
        status, path = _worktree_status(slug)
        if status in ("missing", "unknown"):
            plan.warns.append(
                f"code_memory: worktree NOT FOUND for slug '{slug}' (expected {path}) -- "
                f"prunable/absent git worktree; its {n} memory file(s) are still mirrored to ZONE-X")
            _record(plan, "code_memory", "worktree_not_found")
        elif status == "unregistered":
            plan.warns.append(
                f"code_memory: worktree dir {path} for slug '{slug}' exists but is not registered "
                f"in .git/worktrees (registration pruned) -- {n} memory file(s) still mirrored")
            _record(plan, "code_memory", "worktree_unregistered")
        elif status == "locked":
            plan.warns.append(
                f"code_memory: worktree for slug '{slug}' is registered LOCKED and its checkout is "
                f"absent ({path}) -- git will not prune it; {n} memory file(s) still mirrored")
            _record(plan, "code_memory", "worktree_locked")
    for f in files:
        slug = f.parent.parent.name
        label, zone = slug_repo_zone(slug)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            plan.warns.append(f"code_memory: could not read {_rel_source(f)} ({exc}) -- skipped")
            _record(plan, "code_memory", "unreadable")
            continue
        if zone == "X":
            _add_zone_x_source(plan, cfg,
                               dest=_zx("code-memory", label, f"cora-mirror-{f.name}"),
                               source=f, text=text, cls="code_memory")
        else:
            _add_zone_k_source(plan, cfg,
                               dest=_zk("code-memory", label, f.name),
                               source=f, text=text, cls="code_memory")


def plan_cowork_memory(plan: Plan, cfg: Config) -> None:
    files = _cowork_memory_files()
    override = os.environ.get("COWORK_MEMORY_ROOT", "").strip()
    plan.roots_found["cowork_memory"] = bool(files) or bool(override)
    if not files and not override:
        plan.warns.append("cowork_memory: NOT FOUND -- no spaces/*/memory found")
    for f in files:
        space = f.parent.parent.name
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _add_zone_k_source(plan, cfg,
                           dest=_zk("cowork-memory", space, f.name),
                           source=f, text=text, cls="cowork_memory")


_MD_CELL_RE = re.compile(r"[|\r\n]")


def _md_cell(s: str) -> str:
    return _MD_CELL_RE.sub(" ", s or "").strip()


def _generated_header(rel: str) -> str:
    utc, az = _now_stamps()
    return (f"<!-- GENERATED MIRROR VIEW: {rel} -- {utc} / {az}. "
            f"Regenerated each mirror run; do not edit. -->\n\n")


# ── Apply / manifest / revert ────────────────────────────────────────────────
def _assert_under_roots(dest: Path) -> None:
    d = dest.resolve()
    roots = (_zone_k_root().resolve(), _zone_x_root().resolve())
    if not any(_is_relative_to(d, r) for r in roots):
        raise RuntimeError(f"REFUSING write outside the mirror roots: {dest}")


def _is_relative_to(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _write(dest: Path, text: str) -> None:
    _assert_under_roots(dest)
    drive_io.write_text_atomic(dest, text)


def _manifest_dir() -> Path:
    return _zone_x_root() / "_manifests"


# The manifest carries the cora-mirror- prefix (ZONE-X) so, like every other
# ZONE-X file, its title trips the drive_sweep belt even before the folder-id is
# pinned. (It is JSON, which drive_sweep skips by MIME today -- belt + suspenders.)
_MANIFEST_LATEST = "cora-mirror-manifest-latest.json"


def _load_prev_manifest() -> dict | None:
    latest = _zone_x_root() / _MANIFEST_LATEST
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _prev_present(prev: dict | None) -> tuple[set[str], dict[str, str]]:
    """From a previous manifest, the set of dests that are actually ON DISK
    (written or skipped-unchanged, i.e. present==True) and their source shas. A
    write that failed mid-apply (present==False) is excluded so it is not mistaken
    for live state by the removal-diff or the skip-unchanged check."""
    dests: set[str] = set()
    shas: dict[str, str] = {}
    for w in (prev or {}).get("writes", []):
        if not w.get("present", True):
            continue
        d = w.get("dest", "")
        dests.add(d)
        if w.get("sha256"):
            shas[d] = w["sha256"]
    return dests, shas


def _write_manifest(run_id: str, payload: dict) -> Path:
    mdir = _manifest_dir()
    path = mdir / f"cora-mirror-{run_id}.json"
    _write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    _write(_zone_x_root() / _MANIFEST_LATEST,
           json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


_SUPERSEDE_STUB = (
    "<!-- KB-STATUS: SUPERSEDED {date} by source-removal -- the Claude-workspace "
    "source of this mirror no longer exists. -->\n\n"
    "# Removed\n\nThe source this file mirrored was removed on {date}. This stub "
    "keeps the stable path so the monthly kb_hygiene sweep (D-087) archives and "
    "purges the old chunks.\n"
)

_SUPERSEDE_MARKER = "KB-STATUS: SUPERSEDED"


def _under_a_root(dest: Path) -> bool:
    """Lexical containment (no ``.resolve()`` -- that touches the G: mount, which
    drive_io exists to keep off hot paths). Dests are built from _zk()/_zx()
    joinpath of separator-free names, so there is no ``..`` to collapse."""
    return _is_relative_to(dest, _zone_k_root()) or _is_relative_to(dest, _zone_x_root())


# The SOURCE classes whose disappearance is a real removal. Generated VIEWS
# (parity/status/quarantine/ladder/removal-stub/manifest) are regenerated every
# run and must NEVER be removal-detected -- they are not sources.
SOURCE_CLASSES: frozenset[str] = frozenset(CLASSES)


def _handle_removals(plan: Plan, prev: dict | None, run_id: str,
                     removed_date: str, scoped_classes: frozenset[str] | set[str]) -> list[dict]:
    """Compare the current plan's dest set against the previous manifest's PRESENT
    SOURCE dests. A ZONE-K dest gone from the plan -> overwrite in place with a
    SUPERSEDED stub (stable path, D-087) UNLESS it is already a stub (idempotent --
    D-051 write finding 4, no daily re-stub churn). A ZONE-X dest gone -> move to
    ``_removed/<date>/``.

    Only prev SOURCE entries whose cls is in ``scoped_classes`` are considered, so
    (a) a regenerated generated VIEW is never mistaken for a removed source, and
    (b) a ``--only <class>`` partial run does not falsely "remove" every OTHER
    class's mirror (D-051 defect found in remediation)."""
    events: list[dict] = []
    if not prev:
        return events
    current = {str(w.dest) for w in plan.writes}
    prev_zone: dict[str, str] = {}
    prev_present: set[str] = set()
    for w in prev.get("writes", []):
        if not w.get("present", True):
            continue
        if w.get("cls") not in scoped_classes:
            continue  # a generated view, or a class not processed this run
        d = w.get("dest", "")
        prev_present.add(d)
        prev_zone[d] = w.get("zone", "")
    for dest in sorted(prev_present):
        if dest in current:
            continue
        p = Path(dest)
        if not _under_a_root(p):
            continue
        zone = prev_zone.get(dest, "")
        if zone == "K":
            # Already a stub? leave it -- it settles here and kb_hygiene takes it.
            try:
                existing = drive_io.read_text(dest, timeout=5.0, retry_seconds=0.0)
            except Exception:  # noqa: BLE001
                existing = ""
            if _SUPERSEDE_MARKER in existing:
                continue
            stub = _SUPERSEDE_STUB.format(date=removed_date)
            plan.writes.append(Planned(dest=p, zone="K", text=stub, cls="removal"))
            events.append({"dest": dest, "action": "superseded-stub"})
        elif zone == "X" and drive_io.exists(dest):
            rel = _safe_rel(p, _zone_x_root())
            events.append({"dest": dest, "action": "removed-move",
                           "moved_to": str(_zx("_removed", removed_date, rel))})
    return events


def _safe_rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root)).replace("\\", "/")
    except ValueError:
        return p.name


def apply_plan(plan: Plan, *, run_id: str, removals: list[dict],
               prev_dests: set[str], prev_sha: dict[str, str],
               applied: list[dict]) -> None:
    """Apply the plan. Appends a per-write outcome dict to ``applied`` AS IT GOES,
    so a mid-apply exception still leaves an accurate record for the manifest
    (D-051 write finding 3: no overclaiming). Skips a source-mirror write whose
    source sha matches the previous run AND whose file still exists -- unchanged
    files keep their bytes (and their MIRROR-AT), so static_md does not re-embed
    the whole mirror every run (D-051 write finding 1). Generated views (empty
    sha) always write."""
    # Execute ZONE-X removal-moves first (read old, write to _removed, delete old).
    for ev in removals:
        if ev.get("action") == "removed-move":
            src = Path(ev["dest"])
            try:
                body = drive_io.read_text(str(src))
            except Exception:  # noqa: BLE001
                body = ""
            _write(Path(ev["moved_to"]), body)
            try:
                src.unlink()
            except OSError:
                pass
    for w in plan.writes:
        dest_s = str(w.dest)
        created = dest_s not in prev_dests
        unchanged = bool(w.sha256) and prev_sha.get(dest_s) == w.sha256 and drive_io.exists(dest_s)
        entry = {"dest": dest_s, "zone": w.zone, "source": w.source,
                 "sha256": w.sha256, "cls": w.cls, "created": created}
        if unchanged:
            entry.update({"written": False, "present": True, "skipped": "unchanged"})
            applied.append(entry)
            continue
        _write(w.dest, w.text)
        entry.update({"written": True, "present": True})
        applied.append(entry)


def build_manifest(plan: Plan, run_id: str, removals: list[dict],
                   *, applied: list[dict] | None = None) -> dict:
    utc, az = _now_stamps()
    if applied is not None:
        writes = applied
    else:  # intent manifest (written before apply) -- every planned dest, unresolved
        writes = [{"dest": str(w.dest), "zone": w.zone, "source": w.source,
                   "sha256": w.sha256, "cls": w.cls, "present": True} for w in plan.writes]
    return {
        "run_id": run_id,
        "at_utc": utc, "at_az": az,
        "zone_k_root": str(_zone_k_root()),
        "zone_x_root": str(_zone_x_root()),
        "writes": writes,
        "removals": removals,
        "task_models": plan.task_models,
        "quarantined": [{"name": n, "reason": r} for n, r in plan.quarantined],
        "warns": plan.warns,
        "counts": plan.counts,
    }


def revert(manifest_path: Path) -> int:
    """Undo a run: delete ONLY the files this run CREATED (dest not present in the
    prior run). A file the run merely OVERWROTE (present before) is left in place --
    deleting it would destroy content this run did not author and cannot restore
    (D-051 write finding 2). The mirror is deterministically regenerable, so a
    full content rollback is a fresh ``--apply``, not a delete. Removal-moved
    ZONE-X bodies live under ``_removed/`` -- restore by hand if needed."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("cannot read manifest %s: %s", manifest_path, exc)
        return 1
    n = skipped = 0
    for w in data.get("writes", []):
        dest = Path(w.get("dest", ""))
        if not w.get("created"):
            skipped += 1
            continue  # overwrote a pre-existing file -- not ours to delete
        if not _under_a_root(dest):
            log.warning("skip revert of out-of-root path %s", dest)
            continue
        try:
            if drive_io.exists(str(dest)):
                dest.unlink()
                n += 1
        except OSError as exc:
            log.warning("could not delete %s: %s", dest, exc)
    log.info("revert: deleted %d created file(s), left %d pre-existing, from %s",
             n, skipped, manifest_path.name)
    return 0


# ── Parity report ─────────────────────────────────────────────────────────────
def render_parity(plan: Plan, prev: dict | None, removals: list[dict], cfg: Config) -> str:
    utc, az = _now_stamps()
    lines = [f"# Claude-workspace mirror -- PARITY REPORT", "",
             f"_Generated {utc} / {az} by scripts/mirror_claude_workspace.py._", ""]
    lines.append("## Roots")
    for cls in CLASSES:
        found = plan.roots_found.get(cls)
        state = "FOUND" if found else "**NOT FOUND**"
        lines.append(f"- `{cls}`: {state}")
    lines.append("")
    lines.append("## Per-class counts")
    lines.append("| class | " + " | ".join(_ALL_COUNT_KEYS) + " |")
    lines.append("|---|" + "|".join(["---"] * len(_ALL_COUNT_KEYS)) + "|")
    for cls in CLASSES:
        c = plan.counts.get(cls, {})
        lines.append(f"| {cls} | " + " | ".join(str(c.get(k, 0)) for k in _ALL_COUNT_KEYS) + " |")
    lines.append("")
    # Task-estate delta vs previous manifest. `unpinned` is a CURRENT-state
    # property (every Cowork task create arrives unpinned, 1nnnn) so it surfaces
    # every run; added/removed/changed are diffs and only exist once there is a
    # previous manifest.
    lines.append("## Task-estate delta")
    delta = task_estate_delta(plan, prev)
    if not prev:
        lines.append("- first run -- no previous manifest to diff (added/removed/changed).")
    else:
        for k in ("added", "removed", "cron_changed", "model_changed"):
            items = delta.get(k, [])
            lines.append(f"- **{k}** ({len(items)}): " + (", ".join(items[:20]) if items else "none"))
    up = delta.get("unpinned", [])
    lines.append(f"- **unpinned** ({len(up)}): " + (", ".join(up[:20]) if up else "none"))
    lines.append("")
    lines.extend(_render_run_markers(plan, cfg))
    # D-051 PHI finding 4: this report is ZONE-K (KB-ingested). A quarantined
    # FILENAME can itself carry a client/LEX token, so the ZONE-K report shows only
    # COUNTS by reason -- the full names live in ZONE-X _quarantine/cora-mirror-INDEX.md
    # (never ingested).
    lines.append("## Quarantined (ZONE-K screen tripped) -- counts only")
    if plan.quarantined:
        by_reason: dict[str, int] = {}
        for _name, reason in plan.quarantined:
            head = reason.split("(", 1)[0].strip()
            by_reason[head] = by_reason.get(head, 0) + 1
        lines.append(f"- total: {len(plan.quarantined)}")
        for r, c in sorted(by_reason.items()):
            lines.append(f"  - {r}: {c}")
        lines.append("- (full list with filenames: ZONE-X `_mirror/_quarantine/cora-mirror-INDEX.md`; "
                     "opt one in by its full key via `allow_files:`.)")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Removals this run")
    if removals:
        for ev in removals:
            lines.append(f"- {ev.get('action')}: `{_md_cell(ev.get('dest', ''))}`")
    else:
        lines.append("- none")
    lines.append("")
    # WARN lines can interpolate a candidate FILENAME (the oversize warn) -- screen
    # each before it enters this ZONE-K report.
    lines.append("## WARNs")
    if plan.warns:
        for w in plan.warns:
            safe = w if not screen_reason(w, cfg) else "[warning withheld -- screened]"
            lines.append(f"- {safe}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_run_markers(plan: Plan, cfg: Config) -> list[str]:
    """The '## Run markers' section of the ZONE-K parity report (Code #13 slice 9c).
    Alarmed rows first. Task ids that trip the screen were already replaced by a
    withheld key in plan_cowork_tasks; the free-text cadence `note` is screened here
    because this report is KB-ingested."""
    lines = ["## Run markers (Cowork estate file-drop contract, cq-06045f418bd2)"]
    if not plan.roots_found.get("cowork_tasks"):
        lines.append("- task store NOT FOUND -- no markers evaluated")
        lines.append("")
        return lines
    rm = run_marker_summary(plan)
    lines.append(
        f"- coverage: **{rm['coverage']}** tasks have a marker "
        f"(`_runs/<folder-id>/<YYYY-MM-DD>.json` with ok / outputs[] / duration_s / notes; "
        f"contract + injector: deployment/cowork-run-marker-footer.md)")
    lines.append(
        f"- alarms: {len(rm['did_not_run'])} did not run, {len(rm['wrote_nothing'])} fired but "
        f"wrote nothing, {len(rm['unreadable'])} unreadable marker, {len(rm['reported_error'])} "
        f"reported error; {rm['unknown_cadence']} unknown cadence (no row in "
        f"data/maps/cowork-run-cadence.yaml -- not computable, so neither alarmed nor ok); "
        f"{rm['awaiting_first']} awaiting first marker (inside the 1x cadence `registered:` "
        f"grace -- neither alarmed nor ok)")
    if plan.cadence_error:
        lines.append(f"- cadence map UNREADABLE ({_md_cell(plan.cadence_error)}) -- every task "
                     f"reads 'unknown cadence' until it parses")
    lines.append("- rule: a task with a declared cadence and no marker inside 2x that cadence "
                 "reads **did not run** -- never ok. A marker with `outputs: []` on an "
                 "`expects_output` task reads **fired but wrote nothing**. A markerless task "
                 "whose row carries `registered:` reads **awaiting first marker** for ONE "
                 "cadence after that date, then **did not run**.")
    lines.append("")
    lines.append("| task | status | last marker | ok | outputs | cadence (h) | detail |")
    lines.append("|---|---|---|---|---|---|---|")
    order = {s: i for i, s in enumerate(_RM_RENDER_ORDER)}
    for key in sorted(plan.run_markers, key=lambda k: (order.get(plan.run_markers[k]["status"], 99), k)):
        r = plan.run_markers[key]
        ok = "" if r.get("ok") is None else ("true" if r["ok"] else "false")
        outs = "" if r.get("outputs") is None else str(r["outputs"])
        cad = "" if r.get("cadence_hours") is None else f"{r['cadence_hours']:.0f}"
        detail = r.get("detail", "")
        note = r.get("note") or ""
        if note:
            detail += " | " + (note if not screen_reason(note, cfg) else "[note withheld -- screened]")
        lines.append(f"| {_md_cell(key)} | **{r['status']}** | {r.get('last_marker') or '--'} | {ok} | "
                     f"{outs} | {cad} | {_md_cell(detail)} |")
    lines.append("")
    return lines


_ALL_COUNT_KEYS = ("mirrored", "quarantined", "denied_stock", "unknown_not_allowlisted",
                   "skipped_oversize", "allowlisted_override", "indexed",
                   # every key _record() writes must be a column, or the table silently
                   # hides it (D-051 mirror lens LOW-5, 2026-09-03)
                   "row_redacted", "desc_redacted", "worktree_not_found",
                   "worktree_unregistered", "worktree_locked", "unreadable", "dest_uniquified")


# ── Structured status (for the health lane, S5) ──────────────────────────────
def status_payload(plan: "Plan", prev: dict | None, removals: list, cfg: Config) -> dict:
    """A machine-readable summary the health checks read instead of re-parsing the
    markdown report. Written to ZONE-K as ``mirror-status.json`` every run."""
    utc, az = _now_stamps()
    delta = task_estate_delta(plan, prev)
    return {
        "at_utc": utc, "at_az": az,
        "roots_missing": [c for c in CLASSES if not plan.roots_found.get(c)],
        "quarantined_count": len(plan.quarantined),
        "unknown_skills": [w.split("'")[1] for w in plan.warns
                           if "not in allowlist" in w and "'" in w],
        "warns": [w if not screen_reason(w, cfg) else "[warning withheld -- screened]"
                  for w in plan.warns],
        "unpinned": sorted(plan.unpinned_tasks),
        "added": delta.get("added", []),
        "removed": delta.get("removed", []),
        "model_changed": delta.get("model_changed", []),
        "counts": plan.counts,
        # Code #13 slice 9c: the Cowork run-marker roll-up (ids already withheld
        # where the folder id trips the screen -- this file is ZONE-K too).
        "run_markers": run_marker_summary(plan),
    }


PARITY_REPORT_NAME = "PARITY-REPORT.md"
STATUS_JSON_NAME = "mirror-status.json"


def status_json_path() -> Path:
    return _zone_k_root() / STATUS_JSON_NAME


def read_parity_status(*, max_age_hours: float = 26.0) -> dict:
    """Read the latest ``mirror-status.json`` from ZONE-K for the health lane.

    Returns ``{available, error, age_hours, stale, roots_missing, ...}``. Never
    raises -- a missing/unreadable file (incl. a gone G: mount) yields
    ``available=False`` with an error string, which the caller surfaces as a WARN.
    """
    from datetime import datetime as _dt, timezone as _tz
    path = status_json_path()
    try:
        raw = drive_io.read_text(str(path), timeout=5.0, retry_seconds=0.0)
    except Exception as exc:  # noqa: BLE001 -- report, never raise (D-214)
        return {"available": False, "error": f"{type(exc).__name__}: {exc}",
                "path": str(path)}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"available": False, "error": f"unparseable status json: {exc}",
                "path": str(path)}
    age_h = None
    try:
        at = _dt.strptime(data.get("at_utc", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
        age_h = (_dt.now(_tz.utc) - at).total_seconds() / 3600.0
    except (ValueError, TypeError):
        pass
    data["available"] = True
    data["age_hours"] = age_h
    data["stale"] = bool(age_h is not None and age_h > max_age_hours)
    data["max_age_hours"] = max_age_hours
    return data


def task_estate_delta(plan: Plan, prev: dict | None) -> dict[str, list[str]]:
    """Diff the Cowork task INDEX rows this run against the previous manifest's,
    keyed on the ZONE-X body dest (one per task). Reports added/removed and, from
    the INDEX text, model/cron changes + unpinned."""
    out: dict[str, list[str]] = {"added": [], "removed": [], "cron_changed": [],
                                 "model_changed": [], "unpinned": []}
    cur_tasks = {Path(w.dest).name: w for w in plan.writes
                 if w.cls == "cowork_tasks" and w.zone == "X"}
    prev_tasks = {Path(e["dest"]).name for e in (prev or {}).get("writes", [])
                  if e.get("cls") == "cowork_tasks" and e.get("zone") == "X"}
    for name in sorted(set(cur_tasks) - prev_tasks):
        out["added"].append(name.replace("cora-mirror-", "").replace(".md", ""))
    for name in sorted(prev_tasks - set(cur_tasks)):
        out["removed"].append(name.replace("cora-mirror-", "").replace(".md", ""))
    # unpinned is a current-state list gathered during plan building (every
    # Cowork create arrives unpinned, 1nnnn) -- read it straight off the plan
    # rather than re-parsing the rendered INDEX table.
    out["unpinned"] = sorted(plan.unpinned_tasks)
    # model_changed: a task whose model pin differs from the previous manifest.
    prev_models = {}
    for e in (prev or {}).get("task_models", {}).items():
        prev_models[e[0]] = e[1]
    for name, model in (plan.task_models or {}).items():
        if name in prev_models and prev_models[name] != model:
            out["model_changed"].append(f"{name} ({prev_models[name]}->{model})")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────
def _uniquify_dest_collisions(plan: Plan) -> None:
    """Two sources planning the SAME dest would race silently (last write wins,
    and the manifest would record only one of them). Keep BOTH: the later one is
    renamed ``<stem>-<sha8 of its source><suffix>`` in the same directory, with a
    WARN -- the mirror exists for parity, so dropping content is the wrong failure
    mode (D-051 mirror lens, 2026-09-03). The rename keeps the leading
    ``cora-mirror-`` prefix, so the ZONE-X title belt and the ZONE-K ingest
    predicates see the same shape. Deterministic: the same inputs rename the same
    way run after run. Origin: the cora checkout slug and the Founder-OS
    _shared/projects/cora working-dir slug both carry a MEMORY.md and both used
    to label as "cora"."""
    seen: dict[str, Planned] = {}
    kept: list[Planned] = []
    for w in plan.writes:
        key = os.path.normcase(str(w.dest))
        if key not in seen:
            seen[key] = w
            kept.append(w)
            continue
        first = seen[key]
        if w.zone == "K" and w.allow_override:
            # The allow_files opt-in was reviewed under the ORIGINAL key, which two
            # sources now share; releasing the second under a derived key nobody
            # reviewed would put screened content into the KB-ingested zone.
            # Quarantine it (the INDEX shows the shared key twice -- that is the
            # signal) rather than rename it.
            cls = w.cls or "unknown"
            plan.quarantined.append((_mirror_key(w.dest),
                                     f"{cls}: dest collision on an opted-in key -- second source "
                                     f"NOT released; review it under its own key"))
            _record(plan, cls, "quarantined")
            plan.counts[cls]["mirrored"] = plan.counts[cls].get("mirrored", 1) - 1
            plan.warns.append(
                f"[{cls}] dest collision -- {w.dest} already planned from "
                f"{first.source or first.cls}; {w.source or w.cls} QUARANTINED (its allow_files "
                f"key is ambiguous)")
            continue
        tag = hashlib.sha256((w.source or w.cls or "").encode("utf-8")).hexdigest()[:8]
        new_dest = w.dest.with_name(f"{w.dest.stem}-{tag}{w.dest.suffix}")
        n = 0
        while os.path.normcase(str(new_dest)) in seen:
            n += 1
            new_dest = w.dest.with_name(f"{w.dest.stem}-{tag}-{n}{w.dest.suffix}")
        plan.warns.append(
            f"[{w.cls}] dest collision -- {w.dest} already planned from "
            f"{first.source or first.cls}; {w.source or w.cls} written as {new_dest.name}")
        _record(plan, w.cls or "unknown", "dest_uniquified")
        w.dest = new_dest
        seen[os.path.normcase(str(new_dest))] = w
        kept.append(w)
    plan.writes = kept


def build_plan(cfg: Config, only: str | None, *, today: date | None = None) -> Plan:
    plan = Plan()
    if only in (None, "skills"):
        plan_skills(plan, cfg)
    if only in (None, "cowork_tasks"):
        plan_cowork_tasks(plan, cfg, today=today)
    if only in (None, "code_memory"):
        plan_code_memory(plan, cfg)
    if only in (None, "cowork_memory"):
        plan_cowork_memory(plan, cfg)
    _uniquify_dest_collisions(plan)
    return plan


def _quarantine_index_write(plan: Plan) -> None:
    """The quarantine INDEX (names + reason, NEVER content) lives in ZONE-X."""
    lines = ["# Quarantined ZONE-K candidates (names + reason only; NO content)",
             "",
             "These tripped the deterministic PHI / LEX / personal screen and were "
             "NOT mirrored into the KB-ingested ZONE-K. Harrison opts one in by its "
             "EXACT FULL KEY (the `file` column below, not the bare basename) via "
             "`allow_files:` in data/maps/claude-workspace-mirror.yaml (D-194).", "",
             "| file (full mirror key) | reason |", "|---|---|"]
    for name, reason in sorted(plan.quarantined):
        lines.append(f"| {_md_cell(name)} | {_md_cell(reason)} |")
    plan.writes.append(Planned(dest=_zx("_quarantine", "cora-mirror-INDEX.md"), zone="X",
                               text=_generated_header("_mirror/_quarantine/cora-mirror-INDEX.md")
                                    + "\n".join(lines) + "\n",
                               cls="quarantine"))


# Code #13 slice 7 (cq-6afa86210ba0): the single-lane row this constant carried is
# now ONE row of data/ladder-registry.yaml, rendered beside it as
# cora-mirror-LADDER-REGISTRY.md. The old file is kept as a SUPERSEDED stub because
# generated classes sit outside SOURCE_CLASSES removal detection (a plain delete
# from the plan would leave the stale row on Drive forever).
_LADDER_ROW = """<!-- READ-ONLY MIRROR -- working knowledge, not canon. -->

# Autonomy-ladder row -- claude-workspace-mirror (SUPERSEDED 2026-09-19)

This single-lane row was superseded by the autonomy-ladder REGISTRY
(data/ladder-registry.yaml, Code #13 slice 7), rendered beside this file as
cora-mirror-LADDER-REGISTRY.md. The claude-workspace-mirror lane is one row there
(tier T0, permanent cap). Nothing here is canon.
"""
LADDER_REGISTRY_FILENAME = "cora-mirror-LADDER-REGISTRY.md"


def render_ladder_registry() -> str:
    """The ZONE-X copy of the registry (Code #13 slice 7): deterministic markdown
    from cora.ladder_registry, KB-excluded by the `_shared/projects/cora` folder pin
    AND the `cora-mirror-` title belt. Fail-soft: an unreadable registry renders its
    reason -- never an empty file that reads as 'no lanes'."""
    try:
        from cora import ladder_registry  # noqa: PLC0415
        reg = ladder_registry.load()
        return ladder_registry.render_markdown(
            reg, source_note="rendered by the claude-workspace mirror from data/ladder-registry.yaml; "
                             "edit the YAML in a Code/Cowork commit, never this file")
    except Exception as exc:  # noqa: BLE001
        return ("<!-- READ-ONLY MIRROR -- working knowledge, not canon. -->\n\n"
                "# Cora autonomy-ladder registry\n\n"
                f"REGISTRY UNAVAILABLE: {type(exc).__name__}: {exc}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--revert", metavar="MANIFEST", help="Undo a run from its manifest.json")
    parser.add_argument("--only", choices=CLASSES, help="Mirror a single source class")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run (default anyway)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    if args.revert:
        return revert(Path(args.revert))

    cfg = load_config()
    plan = build_plan(cfg, args.only)

    run_id = datetime.now(timezone.utc).strftime("mirror-%Y%m%dT%H%M%SZ")
    removed_date = datetime.now().strftime("%Y-%m-%d")
    prev = _load_prev_manifest()
    # Removal detection is scoped to the SOURCE classes actually processed this run
    # (a --only run must not "remove" every other class's mirror).
    scoped = frozenset({args.only}) if args.only else SOURCE_CLASSES
    removals = _handle_removals(plan, prev, run_id, removed_date, scoped)

    # Generated ZONE-X views (quarantine index + ladder row) + ZONE-K parity report.
    # ZONE-X files carry the cora-mirror- prefix so the drive_sweep TITLE belt
    # (is_cora_internal_title) catches them even before the _mirror folder-id is
    # pinned -- LADDER-ROW.md / _quarantine/INDEX.md had no cora/mirror token and
    # leaked via drive_sweep in that window (D-051 exclusion finding 1).
    _quarantine_index_write(plan)
    plan.writes.append(Planned(dest=_zx("cora-mirror-LADDER-ROW.md"), zone="X",
                               text=_LADDER_ROW, cls="ladder"))
    # Code #13 slice 7: the registry itself, rendered (replaces the single row above).
    plan.writes.append(Planned(dest=_zx(LADDER_REGISTRY_FILENAME), zone="X",
                               text=render_ladder_registry(), cls="ladder"))
    parity_text = render_parity(plan, prev, removals, cfg)
    plan.writes.append(Planned(dest=_zk("PARITY-REPORT.md"), zone="K",
                               text=_generated_header("PARITY-REPORT.md") + parity_text,
                               cls="parity"))
    # Structured status for the health lane (S5) -- both health tools read this
    # instead of re-parsing the markdown.
    plan.writes.append(Planned(dest=status_json_path(), zone="K",
                               text=json.dumps(status_payload(plan, prev, removals, cfg),
                                               indent=2, ensure_ascii=False) + "\n",
                               cls="status"))

    apply = args.apply and not args.dry_run
    log.info("mirror plan: %d writes, %d quarantined, %d warns, %d removals (apply=%s)",
             len(plan.writes), len(plan.quarantined), len(plan.warns), len(removals), apply)
    print(parity_text)

    if not apply:
        log.info("DRY-RUN -- nothing written. Re-run with --apply to write.")
        return 0

    # Manifest-first (D-086): write an INTENT manifest BEFORE any mutation, then
    # apply, then re-write the manifest from what ACTUALLY landed (D-051 write
    # finding 3: no overclaiming -- a mid-apply G: failure leaves an accurate
    # record for the next run's removal-diff + --revert).
    prev_dests, prev_sha = _prev_present(prev)
    applied: list[dict] = []
    manifest_path = _write_manifest(run_id, build_manifest(plan, run_id, removals))
    try:
        apply_plan(plan, run_id=run_id, removals=removals,
                   prev_dests=prev_dests, prev_sha=prev_sha, applied=applied)
    finally:
        _write_manifest(run_id, build_manifest(plan, run_id, removals, applied=applied))
    log.info("APPLIED -- manifest at %s (%d written, %d unchanged)",
             manifest_path, sum(1 for a in applied if a.get("written")),
             sum(1 for a in applied if a.get("skipped") == "unchanged"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
