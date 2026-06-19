"""Shared predicate: is a file Cora's OWN build/audit/forensic metadata?

Cora's build docs (forensic findings, rebuild execution logs, code-prompts,
cascade reports, phase scopes, north-star plans, this project's CLAUDE.md and
design/ scaffold) live under ``_shared/projects/cora/`` in the Founder OS Drive
tree. They are OPERATIONAL metadata, not org knowledge. Ingesting them into the
KB lets Cora retrieve and recite her own audit notes — and even her own system
prompts — as fact (the fabricated-"diagnostic" failure mode that prompted WS1).

This predicate keeps that doc set OUT of static_md ingestion
(``incremental_sync_static.py``) and powers the one-time purge
(``purge_cora_internal_kb.py``). One rule, two surfaces:

  - ``is_cora_internal_path(Path)``      — used at INGEST time (walk + file_to_document)
  - ``is_cora_internal_source_id(str)``  — used at PURGE time (stored source_id; may
                                            use ``\\`` or ``/`` separators)

Scope notes:
  * The folder rule is the keystone: anything under ``_shared/projects/cora/``.
    Sibling projects (gmail-deep-dive, reddit-strategy, wikipedia-strategy, …)
    are NOT matched and stay ingested — only the ``cora`` project is excluded.
  * The filename rule is defense-in-depth for a Cora build doc COPIED elsewhere
    (e.g. a code-prompt pasted into a session-capture or an entity projects/
    folder). It is deliberately narrow — requires a ``cora-``/``cora_`` prefix
    AND a build keyword — so ordinary business docs are never caught.
"""

from __future__ import annotations

import re
from pathlib import Path

# The Cora build workspace. Any file under this folder sequence is build/ops
# metadata, never org knowledge.
_CORA_WORKSPACE_SEGMENTS: tuple[str, ...] = ("_shared", "projects", "cora")

# Defense-in-depth: catch Cora build docs copied outside the workspace folder.
# Must contain a ``cora-``/``cora_`` prefix AND a build-doc keyword.
_CORA_BUILD_DOC_RE = re.compile(
    r"cora[-_].*?("
    r"forensic|rebuild|execution-log|code-prompt|build-plan|build-queue|"
    r"master-build|cascade-report|cascade|incident-triage|north-star|"
    r"findings|phase-?\d|synthesis-and-path|report-synthesis|audit-addendum"
    r")",
    re.IGNORECASE,
)


def _segments(s: str) -> list[str]:
    """Split a path or source_id on either separator into non-empty segments."""
    return [p for p in re.split(r"[\\/]+", s or "") if p]


def _contains_subsequence(parts: list[str], seq: tuple[str, ...]) -> bool:
    lp = [p.lower() for p in parts]
    ls = [s.lower() for s in seq]
    n = len(ls)
    if n == 0 or len(lp) < n:
        return False
    return any(lp[i : i + n] == ls for i in range(len(lp) - n + 1))


def _is_cora_internal(raw: str) -> bool:
    parts = _segments(raw)
    if _contains_subsequence(parts, _CORA_WORKSPACE_SEGMENTS):
        return True
    name = parts[-1] if parts else (raw or "")
    return bool(_CORA_BUILD_DOC_RE.search(name))


def is_cora_internal_path(path: Path) -> bool:
    """True if a filesystem path is one of Cora's own build/audit/forensic docs."""
    return _is_cora_internal(str(path))


def is_cora_internal_source_id(source_id: str) -> bool:
    """True if a stored KB source_id refers to a Cora build/audit/forensic doc."""
    return _is_cora_internal(source_id or "")
