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
Cowork task BODIES incl. LEX-prefixed, the cora/cora-revops repo memory, the
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
from datetime import datetime, timezone
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
        slugs = [d for d in root.iterdir() if d.is_dir()]
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
_CORA_SLUG_RE = re.compile(r"(?:^|[-])cora(?:[-]|$)", re.IGNORECASE)


def slug_repo_zone(slug: str) -> tuple[str, str]:
    """Map a Claude Code project slug to (repo_label, zone).

    cora / cora-revops memory is Cora-operational -> ZONE-X. Any OTHER repo's
    memory is org knowledge -> ZONE-K. Worktree slugs
    (``...-cora--claude-worktrees-...``) resolve to their base repo.
    """
    low = slug.lower()
    if "cora-revops" in low:
        return "cora-revops", "X"
    if _CORA_SLUG_RE.search(low):
        return "cora", "X"
    # non-cora repo: derive a label from the slug's repo component.
    m = re.search(r"code-([a-z0-9][a-z0-9-]*?)(?:--|$)", low)
    label = m.group(1) if m else (low.rsplit("-", 1)[-1] or "unknown-repo")
    return label, "K"


# ── Screens (ZONE-K only, deterministic, no LLM) ─────────────────────────────
def screen_reason(text: str, cfg: Config) -> str | None:
    """Return a short reason string if the text must be quarantined from ZONE-K,
    else None. Union of phi_guard.is_any_phi, the LEX token family, and the
    personal-container keyword families."""
    if phi_guard.is_any_phi(text):
        preds = ",".join(phi_guard.which_predicates(text)) or "phi"
        return f"phi ({preds})"
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


@dataclass
class Plan:
    writes: list[Planned] = field(default_factory=list)
    quarantined: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    warns: list[str] = field(default_factory=list)
    roots_found: dict[str, bool] = field(default_factory=dict)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    unpinned_tasks: list[str] = field(default_factory=list)  # task names with no model pin
    task_models: dict[str, str] = field(default_factory=dict)  # task name -> model pin


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
    """A D-194 opt-in matches the full mirror key OR its basename. Basename is a
    convenience; the quarantine INDEX shows the full key so Harrison can copy an
    unambiguous one when a basename would opt in siblings too."""
    low = key.lower()
    return low in cfg.allow_files or low.rsplit("/", 1)[-1] in cfg.allow_files


def _add_zone_k_source(plan: Plan, cfg: Config, *, dest: Path, source: Path,
                       text: str, cls: str) -> None:
    """Screen + size-gate a ZONE-K source file, then plan the write (or quarantine)."""
    key = _mirror_key(dest)
    if len(text.encode("utf-8", "replace")) > cfg.max_file_bytes:
        plan.warns.append(f"[{cls}] {key}: over {cfg.max_file_bytes}B cap -- skipped (never truncated)")
        _record(plan, cls, "skipped_oversize")
        return
    reason = screen_reason(text, cfg)
    if reason and not _allow_files_hit(key, cfg):
        plan.quarantined.append((key, f"{cls}: {reason}"))
        _record(plan, cls, "quarantined")
        return
    if reason:  # allow_files opt-in overrode the screen
        _record(plan, cls, "allowlisted_override")
    sha = _sha_text(text)
    plan.writes.append(Planned(dest=dest, zone="K",
                               text=_provenance(_rel_source(source), sha) + text,
                               source=_rel_source(source), sha256=sha, cls=cls))
    _record(plan, cls, "mirrored")


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
        _add_zone_k_source(plan, cfg, dest=_zk("skills", f"{skill}.SKILL.md"),
                           source=f, text=text, cls="skills")
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


def plan_cowork_tasks(plan: Plan, cfg: Config) -> None:
    dirs = _task_dirs(cfg)
    root = _tasks_root(cfg)
    plan.roots_found["cowork_tasks"] = root.exists()
    if not root.exists():
        plan.warns.append(f"cowork_tasks: NOT FOUND -- task store {root} missing")
        return
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
        # ZONE-K INDEX row: manifest only. Redact the description if it trips the
        # screen (a task name is structural org knowledge; a description may carry
        # a LEX/PHI detail). D-145: nothing screened reaches ZONE-K un-redacted.
        row_desc = desc
        if screen_reason(f"{name} {desc}", cfg) and not _allow_files_hit(d.name, cfg):
            row_desc = "[details withheld -- see ZONE-X body]"
        sched = _schedule_hint(name, desc)
        index_rows.append((
            name, sched or "unspecified", model,
            _entity_prefix(d.name),
            datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
            _sha_text(text)[:12],
        ))
        # stash redacted desc alongside for the INDEX render
        index_rows[-1] = index_rows[-1] + (row_desc,)  # type: ignore[assignment]
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
    for f in files:
        slug = f.parent.parent.name
        label, zone = slug_repo_zone(slug)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
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


def _load_prev_manifest() -> dict | None:
    latest = _zone_x_root() / "manifest-latest.json"
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _write_manifest(run_id: str, payload: dict) -> Path:
    mdir = _manifest_dir()
    path = mdir / f"{run_id}.json"
    _write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    _write(_zone_x_root() / "manifest-latest.json",
           json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


_SUPERSEDE_STUB = (
    "<!-- KB-STATUS: SUPERSEDED {date} by source-removal -- the Claude-workspace "
    "source of this mirror no longer exists. -->\n\n"
    "# Removed\n\nThe source this file mirrored was removed on {date}. This stub "
    "keeps the stable path so the monthly kb_hygiene sweep (D-087) archives and "
    "purges the old chunks.\n"
)


def _handle_removals(plan: Plan, prev: dict | None, run_id: str,
                     removed_date: str) -> list[dict]:
    """Compare the current plan's dest set against the previous manifest. A
    ZONE-K dest gone from the plan -> overwrite in place with a SUPERSEDED stub
    (stable path, D-087). A ZONE-X dest gone -> move to ``_removed/<date>/``."""
    events: list[dict] = []
    if not prev:
        return events
    current = {str(w.dest): w.zone for w in plan.writes}
    for entry in prev.get("writes", []):
        dest = entry.get("dest", "")
        zone = entry.get("zone", "")
        if dest in current:
            continue
        p = Path(dest)
        if not _is_relative_to(p.resolve() if p.exists() else p, _zone_k_root().resolve()) and \
           not _is_relative_to(p.resolve() if p.exists() else p, _zone_x_root().resolve()):
            continue
        if zone == "K":
            stub = _SUPERSEDE_STUB.format(date=removed_date)
            plan.writes.append(Planned(dest=p, zone="K", text=stub, cls="removal"))
            events.append({"dest": dest, "action": "superseded-stub"})
        elif zone == "X" and p.exists():
            rel = _safe_rel(p, _zone_x_root())
            events.append({"dest": dest, "action": "removed-move",
                           "moved_to": str(_zx("_removed", removed_date, rel))})
    return events


def _safe_rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root)).replace("\\", "/")
    except ValueError:
        return p.name


def apply_plan(plan: Plan, *, run_id: str, removals: list[dict]) -> None:
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
        _write(w.dest, w.text)


def build_manifest(plan: Plan, run_id: str, removals: list[dict]) -> dict:
    utc, az = _now_stamps()
    return {
        "run_id": run_id,
        "at_utc": utc, "at_az": az,
        "zone_k_root": str(_zone_k_root()),
        "zone_x_root": str(_zone_x_root()),
        "writes": [{"dest": str(w.dest), "zone": w.zone, "source": w.source,
                    "sha256": w.sha256, "cls": w.cls} for w in plan.writes],
        "removals": removals,
        "task_models": plan.task_models,
        "quarantined": [{"name": n, "reason": r} for n, r in plan.quarantined],
        "warns": plan.warns,
        "counts": plan.counts,
    }


def revert(manifest_path: Path) -> int:
    """Undo a run: delete every file the manifest created. Best-effort; a file
    already gone is fine. Does NOT resurrect removal-moved ZONE-X files (their
    bodies live under ``_removed/`` -- restore by hand if needed)."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("cannot read manifest %s: %s", manifest_path, exc)
        return 1
    n = 0
    for w in data.get("writes", []):
        dest = Path(w.get("dest", ""))
        try:
            _assert_under_roots(dest)
        except RuntimeError:
            log.warning("skip revert of out-of-root path %s", dest)
            continue
        try:
            if dest.exists():
                dest.unlink()
                n += 1
        except OSError as exc:
            log.warning("could not delete %s: %s", dest, exc)
    log.info("revert: deleted %d file(s) from %s", n, manifest_path.name)
    return 0


# ── Parity report ─────────────────────────────────────────────────────────────
def render_parity(plan: Plan, prev: dict | None, removals: list[dict]) -> str:
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
    lines.append("## Quarantined (ZONE-K screen tripped; opt in by name via allow_files)")
    if plan.quarantined:
        for name, reason in sorted(plan.quarantined):
            lines.append(f"- `{name}` -- {reason}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Removals this run")
    if removals:
        for ev in removals:
            lines.append(f"- {ev.get('action')}: `{ev.get('dest')}`")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## WARNs")
    if plan.warns:
        for w in plan.warns:
            lines.append(f"- {w}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines) + "\n"


_ALL_COUNT_KEYS = ("mirrored", "quarantined", "denied_stock", "unknown_not_allowlisted",
                   "skipped_oversize", "allowlisted_override", "indexed")


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
def build_plan(cfg: Config, only: str | None) -> Plan:
    plan = Plan()
    if only in (None, "skills"):
        plan_skills(plan, cfg)
    if only in (None, "cowork_tasks"):
        plan_cowork_tasks(plan, cfg)
    if only in (None, "code_memory"):
        plan_code_memory(plan, cfg)
    if only in (None, "cowork_memory"):
        plan_cowork_memory(plan, cfg)
    return plan


def _quarantine_index_write(plan: Plan) -> None:
    """The quarantine INDEX (names + reason, NEVER content) lives in ZONE-X."""
    lines = ["# Quarantined ZONE-K candidates (names + reason only; NO content)",
             "",
             "These tripped the deterministic PHI / LEX / personal screen and were "
             "NOT mirrored into the KB-ingested ZONE-K. Harrison opts one in by exact "
             "basename via `allow_files:` in data/maps/claude-workspace-mirror.yaml "
             "(D-194).", "",
             "| file | reason |", "|---|---|"]
    for name, reason in sorted(plan.quarantined):
        lines.append(f"| {_md_cell(name)} | {_md_cell(reason)} |")
    plan.writes.append(Planned(dest=_zx("_quarantine", "INDEX.md"), zone="X",
                               text=_generated_header("_mirror/_quarantine/INDEX.md")
                                    + "\n".join(lines) + "\n",
                               cls="quarantine"))


_LADDER_ROW = """<!-- READ-ONLY MIRROR -- working knowledge, not canon. -->

# Autonomy-ladder row -- claude-workspace-mirror

| field | value |
|---|---|
| lane | claude-workspace-mirror |
| tier | T0 (permanent cap) |
| promotion_criteria | none sought -- read-only sources, writes only to the two mirror roots, no egress, no LLM |
| demotion | n/a |
| evidence_monitor | the PARITY-REPORT.md WARNs (missing root / new quarantine / new skill / task-estate delta / stale run) |
| audit_surface | PARITY-REPORT.md + the Monday cora_health_report digest line |
| authority | Harrison (allowlist yaml is Harrison-edited; Cowork proposes) |

Registry file = #13 (October). Until then this row lives here + verbatim in the cascade report.
"""


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
    removals = _handle_removals(plan, prev, run_id, removed_date)

    # Generated ZONE-X views (quarantine index + ladder row) + ZONE-K parity report.
    _quarantine_index_write(plan)
    plan.writes.append(Planned(dest=_zx("LADDER-ROW.md"), zone="X", text=_LADDER_ROW, cls="ladder"))
    parity_text = render_parity(plan, prev, removals)
    plan.writes.append(Planned(dest=_zk("PARITY-REPORT.md"), zone="K",
                               text=_generated_header("PARITY-REPORT.md") + parity_text,
                               cls="parity"))

    apply = args.apply and not args.dry_run
    log.info("mirror plan: %d writes, %d quarantined, %d warns, %d removals (apply=%s)",
             len(plan.writes), len(plan.quarantined), len(plan.warns), len(removals), apply)
    print(parity_text)

    if not apply:
        log.info("DRY-RUN -- nothing written. Re-run with --apply to write.")
        return 0

    manifest = build_manifest(plan, run_id, removals)
    # Manifest-first (D-086): write it BEFORE any mutation, and again in finally.
    manifest_path = _write_manifest(run_id, manifest)
    try:
        apply_plan(plan, run_id=run_id, removals=removals)
    finally:
        _write_manifest(run_id, manifest)
    log.info("APPLIED -- manifest at %s", manifest_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
