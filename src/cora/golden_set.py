"""Golden-set auto-growth (WS-3): the flywheel writes its own regression suite.

When Harrison 👍s a known_answer or an #info-for-cora contribution, the
knowledge-review executor writes the fact into design/known-answers/{entity}.md
(the runtime-loaded store) -- and, via this module, ALSO appends an eval case
to data/evals/golden-set-auto.yaml. scripts/run_kb_evals.py merges that file
with the hand-curated data/evals/golden-set.yaml, so every approved fact
becomes a standing L1 regression check: "does this fact still surface in the
context the production pipeline would assemble?" A later rollback of the
known-answers file, an entity-file-map drift, or a context_loader regression
turns the case red on the next weekly eval run.

Design constraints:
  - Auto cases are L1-only (retrieval/static presence). No L2 answer
    assertions -- machine-derived must_contain phrases are brittle against
    paraphrase and would make the weekly run noisy.
  - Attach AFTER apply_* returns ok=True (the durable write's PHI re-check has
    already passed) and re-screen here anyway (belt-and-braces, lens R1).
  - LEX* entities never enter the corpus (the golden set is a plain-text repo
    file that rides git and eval output).
  - Idempotent by case id (crash-recovery re-runs and dedup-skip returns from
    apply_* must not double-append).
  - Fail-soft: an append error never affects the executor / the D-011 gate.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any

from .phi_guard import is_phi_risk, is_clinical_phi, is_lex_billing_status_phi

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK = Lock()

# Cap the expect_substring length: long enough to be distinctive, short enough
# to survive minor known-answers reformatting.
_SNIPPET_CHARS = 80


def _auto_path() -> Path:
    return Path(os.environ.get("GOLDEN_SET_AUTO_PATH")
                or _REPO_ROOT / "data" / "evals" / "golden-set-auto.yaml")


def _normalize_snippet(text: str) -> str:
    return " ".join((text or "").split())[:_SNIPPET_CHARS].strip()


def _load_auto() -> dict:
    import yaml
    path = _auto_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        data = {}
    except Exception as exc:  # noqa: BLE001
        log.warning("golden_set: could not read %s: %s", path.name, exc)
        return {"version": 1, "cases": None}  # sentinel: do NOT overwrite
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    cases = data.get("cases")
    data["cases"] = cases if isinstance(cases, list) else []
    return data


def _write_auto(data: dict) -> None:
    import yaml
    path = _auto_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        "# AUTO-GROWN eval cases -- appended by the knowledge-review executor on\n"
        "# Harrison-approved knowledge writes (WS-3). Merged with golden-set.yaml\n"
        "# by scripts/run_kb_evals.py. Do not hand-edit ids; prune freely.\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def _screen(texts: list[str], entity: str) -> str | None:
    """Return a skip-reason string, or None when the case is safe to append."""
    ent = (entity or "FNDR").strip().upper()
    if ent.startswith("LEX"):
        return "LEX entity -- golden set never carries LEX content"
    blob = "\n".join(t for t in texts if t)
    if is_phi_risk(blob) or is_clinical_phi(blob) or is_lex_billing_status_phi(blob):
        return "PHI screen tripped"
    return None


def _append_case(case: dict[str, Any]) -> bool:
    with _LOCK:
        data = _load_auto()
        if data.get("cases") is None:
            log.warning("golden_set: auto file unreadable -- skipping append "
                        "(never overwrite a corrupt corpus)")
            return False
        if any(c.get("id") == case["id"] for c in data["cases"]
               if isinstance(c, dict)):
            return False  # idempotent
        data["cases"].append(case)
        _write_auto(data)
        log.info("golden_set: appended auto case %s", case["id"])
        return True


def append_case_from_known_answer(payload: dict[str, Any]) -> bool:
    """Auto-grow from an approved gap-fill (question + answer). Never raises."""
    try:
        entity = (payload.get("entity") or "FNDR").strip().upper()
        question = (payload.get("question") or "").strip()
        answer = (payload.get("answer") or "").strip()
        if not question or not answer:
            return False
        reason = _screen([question, answer], entity)
        if reason:
            log.info("golden_set: known_answer case skipped -- %s", reason)
            return False
        gap_ts = (payload.get("gap_ts") or "").strip()
        # Full digit string (incl. microseconds) -- truncating to seconds made
        # two same-second approvals collide and silently drop the second case
        # (adversarial review LOW).
        suffix = (re.sub(r"[^0-9]", "", gap_ts)[:20]
                  or hashlib.md5(f"{question}|{answer}".encode()).hexdigest()[:12])
        return _append_case({
            "id": f"auto-ka-{suffix}",
            "entity": entity,
            "question": question[:300],
            "expect_substring": _normalize_snippet(answer),
            "source": "known_answer_approval",
        })
    except Exception:  # noqa: BLE001 -- executor safety
        log.warning("golden_set: append_case_from_known_answer failed",
                    exc_info=True)
        return False


def append_case_from_note(payload: dict[str, Any]) -> bool:
    """Auto-grow from an approved #info-for-cora / folded team note.

    A note has no question, so the case asks for the fact itself; the L1
    static-context check verifies the written known-answers line still loads.
    Never raises.
    """
    try:
        entity = (payload.get("entity") or "FNDR").strip().upper()
        text = " ".join((payload.get("text") or payload.get("note") or "").split())
        if not text:
            return False
        reason = _screen([text], entity)
        if reason:
            log.info("golden_set: note case skipped -- %s", reason)
            return False
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
        return _append_case({
            "id": f"auto-note-{digest}",
            "entity": entity,
            "question": text[:300],
            "expect_substring": _normalize_snippet(text),
            "source": "contributed_note_approval",
        })
    except Exception:  # noqa: BLE001 -- executor safety
        log.warning("golden_set: append_case_from_note failed", exc_info=True)
        return False
