"""Lexicon review-rail executor: apply an approved lexicon proposal to its file
of record (Lexicon Flywheel S4).

Routing by type (design lock -- one resolver, canonical stores stay canonical):
  - product + F3E  -> data/maps/f3e-sku-aliases.yaml, append-only ``learned:``
    LIST of {sku, aliases} rows.
  - person         -> data/maps/user-aliases.yaml, append-only
    ``learned_aliases:`` LIST of {name, aliases} rows. The canonical MUST match
    the staff roster (slack-to-asana display names / org-roles names) or the
    write is REFUSED -- a LEX client can never become a canonical.
  - everything else -> data/maps/lexicon/{entity}.yaml ``terms:`` list.

Write contract (load-bearing for the autowrite diff/revert machinery):
  - ONE contiguous self-contained block appended per term, atomic tmp+replace.
  - NEVER edits an existing line (a revert removes the appended block wholesale;
    editing a line would make revert lossy).
  - Post-append REPARSE VALIDATION: the new file must parse, contain every
    pre-existing row, and gain exactly the appended row -- else the write is
    aborted and the original file is left untouched (fail-closed).
  - ``is_any_phi`` fail-closed INSIDE the applier (the apply_autowrite contract:
    the applier is the last PHI gate before disk).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from . import lexicon
from .lexicon import norm_term

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRITE_LOCK = threading.Lock()

_WRITABLE_TYPES = frozenset(
    {"location", "project", "acronym", "vendor", "channel", "process", "product", "person"}
)
_LEXICON_ENTITIES = frozenset(
    {"F3E", "OSN", "LEX", "HJRP", "UFL", "BDM", "HJRPROD", "F3C", "HJRG", "FNDR", "SHARED"}
)


def _slack_asana_path() -> Path:
    return Path(os.environ.get("LEXICON_ROSTER_PATH")
                or _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml")


def _roster_names() -> set[str]:
    """Staff roster (lowercased): slack-to-asana display names + org-roles names.
    Fail-soft per source; an empty roster REFUSES person writes (fail-closed at
    the caller -- never 'no roster, so anyone goes')."""
    names: set[str] = set()
    try:
        raw = yaml.safe_load(_slack_asana_path().read_text(encoding="utf-8")) or {}
        for row in (raw.get("users") or []):
            if isinstance(row, dict):
                n = str(row.get("display_name") or "").strip().lower()
                if n:
                    names.add(n)
    except Exception as exc:  # noqa: BLE001
        log.warning("lexicon_writer: slack-to-asana roster read failed: %s", exc)
    try:
        from .org_roles import all_roles
        for rec in all_roles():
            n = str(getattr(rec, "name", "") or "").strip().lower()
            if n:
                names.add(n)
    except Exception as exc:  # noqa: BLE001
        log.warning("lexicon_writer: org-roles roster read failed: %s", exc)
    return names


def _phi_screen(*texts: str) -> bool:
    """True when ANY text is PHI-shaped OR the screen itself fails (fail-closed)."""
    try:
        from .phi_guard import is_any_phi
        return any(is_any_phi(t) for t in texts if t)
    except Exception:  # noqa: BLE001 -- an erroring screen refuses the write
        return True


def _atomic_replace(path: Path, new_text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)


def _yaml_quote(s: str) -> str:
    """Double-quoted YAML scalar (safe for the flow-style rows we append)."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validated_append(path: Path, block_lines: list[str], count_rows) -> tuple[bool, str]:
    """Append one contiguous block to ``path``, validating by REPARSE that the
    row count grew by exactly one and no pre-existing row vanished. On any
    validation failure the file is left byte-identical (fail-closed)."""
    old_text = path.read_text(encoding="utf-8")
    try:
        old_rows = count_rows(yaml.safe_load(old_text) or {})
    except Exception as exc:  # noqa: BLE001
        return False, f"target file does not parse -- refusing to append ({exc})"
    base = old_text if old_text.endswith("\n") else old_text + "\n"
    new_text = base + "\n".join(block_lines) + "\n"
    try:
        new_rows = count_rows(yaml.safe_load(new_text) or {})
    except Exception as exc:  # noqa: BLE001
        return False, f"append would corrupt the file -- left untouched ({exc})"
    missing = [r for r in old_rows if r not in new_rows]
    if missing or len(new_rows) != len(old_rows) + 1:
        return False, "append would drop or shadow existing rows -- left untouched"
    _atomic_replace(path, new_text)
    return True, ""


# ── Per-store row counters (identity of a row for the reparse validation) ────


def _lexicon_rows(data: dict) -> list[tuple]:
    out = []
    for item in (data.get("terms") or []):
        if isinstance(item, dict):
            out.append((norm_term(str(item.get("term") or "")),
                        str(item.get("canonical") or "")))
    return out


def _sku_rows(data: dict) -> list[tuple]:
    out = [("skus", str(k)) for k in (data.get("skus") or {})]
    for row in (data.get("learned") or []):
        if isinstance(row, dict):
            out.append(("learned", str(row.get("sku") or ""),
                        tuple(row.get("aliases") or ())))
    return out


def _person_rows(data: dict) -> list[tuple]:
    out = [("aliases", str(k)) for k in (data.get("aliases") or {})]
    for row in (data.get("learned_aliases") or []):
        if isinstance(row, dict):
            out.append(("learned", str(row.get("name") or ""),
                        tuple(row.get("aliases") or ())))
    return out


# ── Store writers (insert-only; one contiguous block each) ───────────────────


def _append_lexicon_term(payload: dict[str, Any]) -> tuple[bool, str]:
    entity = str(payload.get("entity") or "").strip().upper()
    parent, derived_scope = lexicon._collapse_entity(entity)
    if parent not in _LEXICON_ENTITIES:
        return False, f"unknown entity {entity!r} -- refused"
    scope = str(payload.get("scope") or derived_scope or "").strip().upper()
    fname = "_shared.yaml" if parent == "SHARED" else f"{parent.lower()}.yaml"
    path = lexicon._lexicon_dir() / fname
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"version: 1\nentity: {parent}\nterms:\n", encoding="utf-8")

    term = str(payload.get("term")).strip()
    canonical = str(payload.get("canonical")).strip()
    # Idempotency (B6 pattern): (normalized term, canonical) already present
    # in the target store = clean no-op success, never a duplicate block.
    try:
        existing = lexicon._parse_lexicon_file(path)
        for e in existing:
            if norm_term(e.term) == norm_term(term) and e.canonical == canonical:
                return True, f"'{term}' -> {canonical} already in {fname} (no-op)"
    except Exception:  # noqa: BLE001 -- unparseable file is caught by _validated_append
        pass

    data = {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        pass
    block: list[str] = []
    if not (isinstance(data, dict) and data.get("terms")):
        # Empty/missing terms list: the appended block re-opens the key (pyyaml
        # last-key-wins folds it; the reparse validation proves nothing is lost;
        # a revert that removes this block restores the original empty list).
        block.append("terms:")
    fields = [f"term: {_yaml_quote(term)}"]
    aliases = [str(a).strip() for a in (payload.get("aliases") or []) if str(a).strip()]
    if aliases:
        fields.append("aliases: [" + ", ".join(_yaml_quote(a) for a in aliases) + "]")
    fields.append(f"type: {payload.get('type')}")
    fields.append(f"canonical: {_yaml_quote(canonical)}")
    fields.append(f"canonical_name: {_yaml_quote(str(payload.get('canonical_name')).strip())}")
    if scope:
        fields.append(f"scope: {_yaml_quote(scope)}")
    notes = str(payload.get("notes") or "").strip()
    if notes:
        fields.append(f"notes: {_yaml_quote(notes)}")
    fields.append(f"source: {payload.get('lane') or 'approved'}")
    fields.append(f"added: {_yaml_quote(date.today().isoformat())}")
    contributor = str(payload.get("contributor_id") or "").strip()
    fields.append(f"added_by: {_yaml_quote(contributor or 'autowrite')}")
    evidence = str(payload.get("evidence") or "").strip()
    if evidence:
        fields.append(f"evidence: {_yaml_quote(evidence[:200])}")
    block.append("  - {" + ", ".join(fields) + "}")

    ok, err = _validated_append(path, block, _lexicon_rows)
    if not ok:
        return False, err
    return True, f"'{term}' -> {canonical} appended to {fname}"


def _append_sku_alias(payload: dict[str, Any]) -> tuple[bool, str]:
    path = lexicon._sku_aliases_path()
    if not path.exists():
        return False, "f3e-sku-aliases.yaml missing -- refused"
    term = str(payload.get("term")).strip()
    sku = str(payload.get("canonical")).strip()
    aliases = [term] + [str(a).strip() for a in (payload.get("aliases") or [])
                        if str(a).strip()]
    try:
        existing, _ = _current_sku_map(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"sku map does not parse -- refused ({exc})"
    # Conflict check FIRST: an alias that already maps to a DIFFERENT SKU is a
    # refusal, never a silent no-op (a no-op here would mask a retarget attempt).
    conflicting = [a for a in aliases if existing.get(norm_term(a), sku) != sku]
    if conflicting:
        return False, (f"alias {conflicting[0]!r} already maps to a different SKU "
                       f"-- refused (ask Harrison)")
    new_aliases = [a for a in aliases if norm_term(a) not in existing]
    if not new_aliases:
        return True, f"alias(es) for {sku} already present (no-op)"

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block: list[str] = []
    if "learned" not in data:
        block.append("# Appended by the lexicon review rail (Harrison-gated / audited"
                     " autowrite). Rows only -- never edit the seed map above.")
        block.append("learned:")
    row = "  - {sku: " + _yaml_quote(sku) + ", aliases: [" + \
        ", ".join(_yaml_quote(a) for a in new_aliases) + "]}"
    block.append(row)
    ok, err = _validated_append(path, block, _sku_rows)
    if not ok:
        return False, err
    return True, f"'{term}' -> SKU {sku} appended to f3e-sku-aliases.yaml (learned)"


def _current_sku_map(path: Path) -> tuple[dict[str, str], list[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    alias_to_sku: dict[str, str] = {}
    display: list[str] = []
    for sku, aliases in (data.get("skus") or {}).items():
        for a in (aliases or []):
            alias_to_sku.setdefault(norm_term(str(a)), str(sku))
            display.append(str(a))
    for row in (data.get("learned") or []):
        if isinstance(row, dict):
            for a in (row.get("aliases") or []):
                alias_to_sku.setdefault(norm_term(str(a)), str(row.get("sku") or ""))
                display.append(str(a))
    return alias_to_sku, display


def _append_person_alias(payload: dict[str, Any]) -> tuple[bool, str]:
    canonical = str(payload.get("canonical") or payload.get("canonical_name") or "").strip()
    roster = _roster_names()
    if not roster:
        return False, "staff roster unavailable -- person write refused (fail-closed)"
    if canonical.lower() not in roster:
        return False, (f"person canonical {canonical!r} is not on the staff roster "
                       f"-- REFUSED (roster validation; a non-staff name can never "
                       f"become a lexicon canonical)")
    path = lexicon._user_aliases_path()
    if not path.exists():
        return False, "user-aliases.yaml missing -- refused"
    term = str(payload.get("term")).strip()
    aliases = [term] + [str(a).strip() for a in (payload.get("aliases") or [])
                        if str(a).strip()]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return False, f"user-aliases.yaml does not parse -- refused ({exc})"
    current: set[str] = set()
    for name, variants in (data.get("aliases") or {}).items():
        for v in [name, *(variants or [])]:
            current.add(norm_term(str(v)))
    for row in (data.get("learned_aliases") or []):
        if isinstance(row, dict):
            for v in (row.get("aliases") or []):
                current.add(norm_term(str(v)))
    new_aliases = [a for a in aliases if norm_term(a) not in current]
    if not new_aliases:
        return True, f"alias(es) for {canonical} already present (no-op)"

    block: list[str] = []
    if "learned_aliases" not in data:
        block.append("# Appended by the lexicon review rail (Harrison-gated). Rows"
                     " only -- never edit the seed aliases above.")
        block.append("learned_aliases:")
    block.append("  - {name: " + _yaml_quote(canonical) + ", aliases: [" +
                 ", ".join(_yaml_quote(a) for a in new_aliases) + "]}")
    ok, err = _validated_append(path, block, _person_rows)
    if not ok:
        return False, err
    return True, f"'{term}' -> {canonical} appended to user-aliases.yaml (learned)"


# ── Public executor ──────────────────────────────────────────────────────────


def apply_lexicon_update(payload: dict[str, Any]) -> tuple[bool, str]:
    """Apply one approved lexicon proposal. Returns (ok, summary); never raises.

    Contract: PHI re-check fail-closed INSIDE the applier; person canonicals
    roster-validated (refused, not merely gated); idempotent on
    (normalized term, canonical); ONE contiguous appended block per term so the
    autowrite snapshot/diff revert removes exactly what was added.
    """
    try:
        payload = payload or {}
        term = str(payload.get("term") or "").strip()
        etype = str(payload.get("type") or "").strip().lower()
        entity = str(payload.get("entity") or "").strip().upper()
        canonical = str(payload.get("canonical") or "").strip()
        canonical_name = str(payload.get("canonical_name") or "").strip()
        if etype == "person" and not canonical_name:
            canonical_name = canonical
        if not term or not canonical or not canonical_name:
            return False, "lexicon payload missing term/canonical/canonical_name"
        if etype not in _WRITABLE_TYPES:
            return False, f"unknown lexicon type {payload.get('type')!r}"
        if not entity:
            return False, "lexicon payload missing entity"

        aliases = [str(a) for a in (payload.get("aliases") or [])]
        if _phi_screen(term, canonical, canonical_name,
                       str(payload.get("notes") or ""),
                       str(payload.get("evidence") or ""), *aliases):
            return False, ("REFUSED: PHI-shaped content in a lexicon payload "
                           "(fail-closed applier screen)")

        with _WRITE_LOCK:
            if etype == "person":
                ok, summary = _append_person_alias(payload)
            elif etype == "product" and entity == "F3E":
                ok, summary = _append_sku_alias(payload)
            else:
                ok, summary = _append_lexicon_term(payload)
        if ok:
            lexicon.invalidate_cache()
        return ok, summary
    except Exception as exc:  # noqa: BLE001 -- executor must never crash the rail
        log.error("lexicon_writer: apply failed: %s", exc, exc_info=True)
        return False, f"apply failed: {exc}"
