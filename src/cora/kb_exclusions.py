"""Shared predicate: is a file Cora's OWN build/audit/forensic metadata?

Cora's build docs (forensic findings, rebuild execution logs, code-prompts,
cascade reports, phase scopes, north-star plans, this project's CLAUDE.md and
design/ scaffold) live under ``_shared/projects/cora/`` in the Founder OS Drive
tree. They are OPERATIONAL metadata, not org knowledge. Ingesting them into the
KB lets Cora retrieve and recite her own audit notes — and even her own system
prompts — as fact (the fabricated-"diagnostic" failure mode that prompted WS1).

This predicate keeps that doc set OUT of KB ingestion and powers the one-time
purge (``purge_cora_internal_kb.py``). One rule, several surfaces:

  - ``is_cora_internal_path(Path)``      — INGEST, static_md walk (``incremental_sync_static.py``)
  - ``is_cora_internal_source_id(str)``  — PURGE, by stored source_id (``\\`` or ``/`` separators)
  - ``is_cora_internal_title(str)``      — INGEST (``drive_sweep``) + PURGE, by the stored
                                            filename/``title``. ``drive_sweep`` walks Harrison's
                                            whole Drive (the Founder OS lives there), so these
                                            docs land under a Drive-FILE-ID source_id with the
                                            filename in ``title`` — the path rules can't see
                                            them, so we match the filename instead.

Why the title surface exists (the WS1-completion finding, 2026-06-19): the
static_md path was empty; the real leak was ``drive_sweep`` ingesting
``cora-rebuild-execution-log.md``, ``cora-forensic-findings-report.md``,
``cora-*.log``, etc. straight from Drive under file-id source_ids that no
path rule could match.

Scope notes:
  * The folder rule is the keystone: anything under ``_shared/projects/cora/``.
    Sibling projects (gmail-deep-dive, reddit-strategy, wikipedia-strategy, …)
    are NOT matched and stay ingested — only the ``cora`` project is excluded.
  * The filename rule is the workhorse for Drive copies (no path on the source_id).
    It is deliberately narrow — requires a ``cora-``/``cora_`` prefix AND a build
    keyword, or a ``cora-….log`` runtime log — so ordinary business docs
    (``f3-brand-assets-cora-reference.md``, ``…_cora-wishlist.md``,
    ``…-cora-mapping.md``, "deCORAtions" emails) are never caught.
  * ``broad=True`` (PURGE opt-in, ``--scope broad``) adds the rest of Cora's ops
    docs (reviews, proposals, plans, specs, code-session docs, fixes). Still
    anchored to ``cora-``/``cora_`` + keyword, so the legit business docs above
    stay safe.
"""

from __future__ import annotations

import re
from pathlib import Path

# The Cora build workspace. Any file under this folder sequence is build/ops
# metadata, never org knowledge.
_CORA_WORKSPACE_SEGMENTS: tuple[str, ...] = ("_shared", "projects", "cora")

# TARGETED filename rule: ``cora-``/``cora_`` prefix AND a build-doc keyword.
# This is the default for ingest + purge — the unambiguous build/audit artifacts.
_CORA_BUILD_DOC_RE = re.compile(
    r"cora[-_].*?("
    r"forensic|rebuild|execution-log|code-prompt|build-plan|build-queue|"
    r"master-build|cascade-report|cascade|incident-triage|north-star|"
    r"findings|phase-?\d|synthesis-and-path|report-synthesis|audit-addendum"
    r")",
    re.IGNORECASE,
)

# Cora's raw runtime logs (e.g. ``cora-2026-06-06.log``). Never org knowledge.
_CORA_LOG_RE = re.compile(r"^cora[-_].*\.log$", re.IGNORECASE)

# BROAD (opt-in, purge only): the rest of Cora's ops/build docs. Still requires a
# ``cora-``/``cora_`` prefix + keyword, so legit business docs that merely mention
# Cora (``…-cora-reference``, ``…-cora-wishlist``, ``…-cora-mapping``,
# ``cora-f3-monitor-privacy-policy``) are still NOT matched.
_CORA_BUILD_DOC_BROAD_RE = re.compile(
    r"cora[-_].*?("
    r"audit|review|proposal|backlog|exec-summary|game-plan|overhaul|redesign|"
    r"training|checklist|scaling|comms|infra|sweep|spec|wiring|closeout|kickoff|"
    r"gap|plan|prompt|caching|connector|setup|dedup|session|whats-on|knowledge|"
    r"nudge|guard|filer|fix|brief"
    r")",
    re.IGNORECASE,
)


def _segments(s: str) -> list[str]:
    """Split a path or source_id on either separator into non-empty segments."""
    return [p for p in re.split(r"[\\/]+", s or "") if p]


def _basename(raw: str) -> str:
    parts = _segments(raw)
    return parts[-1] if parts else (raw or "")


def _contains_subsequence(parts: list[str], seq: tuple[str, ...]) -> bool:
    lp = [p.lower() for p in parts]
    ls = [s.lower() for s in seq]
    n = len(ls)
    if n == 0 or len(lp) < n:
        return False
    return any(lp[i : i + n] == ls for i in range(len(lp) - n + 1))


def _name_is_build_doc(name: str, *, broad: bool = False) -> bool:
    if _CORA_BUILD_DOC_RE.search(name) or _CORA_LOG_RE.match(name):
        return True
    return bool(broad and _CORA_BUILD_DOC_BROAD_RE.search(name))


def _is_cora_internal(raw: str, *, broad: bool = False) -> bool:
    parts = _segments(raw)
    if _contains_subsequence(parts, _CORA_WORKSPACE_SEGMENTS):
        return True
    name = parts[-1] if parts else (raw or "")
    return _name_is_build_doc(name, broad=broad)


def is_cora_internal_path(path: Path) -> bool:
    """True if a filesystem path is one of Cora's own build/audit/forensic docs."""
    return _is_cora_internal(str(path))


def is_cora_internal_source_id(source_id: str) -> bool:
    """True if a stored KB source_id refers to a Cora build/audit/forensic doc."""
    return _is_cora_internal(source_id or "")


def is_cora_internal_title(title: str, *, broad: bool = False) -> bool:
    """True if a stored KB ``title`` (a bare filename) is a Cora build/audit doc.

    Used where the source_id carries no path — chiefly ``drive_sweep`` copies of
    Founder-OS Drive files, whose source_id is a Drive file id. Matches the
    filename only (no folder rule). ``broad=True`` widens to Cora's full ops/build
    doc set; the default stays narrow (build/audit/forensic artifacts + logs).
    """
    return _name_is_build_doc(_basename(title or ""), broad=broad)
