"""S1 (2026-09-03, claude-workspace mirror): scripts/mirror_claude_workspace.py.

Covers zone routing, allowlist/stock/unknown handling, the PHI/LEX/personal
screen + allow_files opt-in, the 512KB cap, idempotency, removals (ZONE-K stub /
ZONE-X move), manifest-first + finally, --revert, task-estate delta incl.
unpinned, NOT-FOUND roots, and the "writes only under the two roots" invariant.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import mirror_claude_workspace as m  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def roots(tmp_path, monkeypatch):
    """Isolate every source root + both zone roots into tmp_path."""
    zk = tmp_path / "zone-k"
    zx = tmp_path / "zone-x"
    skills = tmp_path / "skills"
    cowork_mem = tmp_path / "cowork-mem"
    code_projects = tmp_path / "code-projects"
    tasks = tmp_path / "tasks"
    for d in (skills, cowork_mem, code_projects, tasks):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CORA_MIRROR_ZONE_K_ROOT", str(zk))
    monkeypatch.setenv("CORA_MIRROR_ZONE_X_ROOT", str(zx))
    monkeypatch.setenv("CLAUDE_SKILLS_ROOT", str(skills))
    monkeypatch.setenv("COWORK_MEMORY_ROOT", str(cowork_mem))
    monkeypatch.setenv("CLAUDE_PROJECTS_ROOT", str(code_projects))
    monkeypatch.setenv("COWORK_TASKS_ROOT", str(tasks))
    # drive_io breaker can be left tripped by another test; reset it.
    from cora import drive_io
    drive_io.reset_state_for_tests()
    return {
        "zk": zk, "zx": zx, "skills": skills, "cowork_mem": cowork_mem,
        "code_projects": code_projects, "tasks": tasks,
    }


def _skill(root: Path, name: str, desc: str = "does a thing", body: str = "steps here"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8")


def _task(root: Path, tid: str, name: str = "", model: str = "sonnet",
          desc: str = "runs daily 6am", body: str = "do the work"):
    d = root / tid
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name or tid}\n"
    if model:
        fm += f"model: {model}\n"
    fm += f"description: {desc}\n---\n\n{body}\n"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")


def _mem(root: Path, space: str, name: str, text: str):
    d = root / space / "memory"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _codemem(root: Path, slug: str, name: str, text: str):
    d = root / slug / "memory"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _run(argv):
    return m.main(argv)


# ── dry-run writes nothing ────────────────────────────────────────────────────
def test_dry_run_writes_nothing(roots):
    _skill(roots["skills"], "wrap-it")
    assert _run([]) == 0
    assert not roots["zk"].exists() or not list(roots["zk"].rglob("*"))
    assert not roots["zx"].exists() or not list(roots["zx"].rglob("*"))


# ── apply writes only under the two roots ─────────────────────────────────────
def test_apply_writes_only_under_roots(roots, monkeypatch):
    _skill(roots["skills"], "wrap-it")
    opened: list[str] = []
    from cora import drive_io
    real = drive_io.write_text_atomic

    def spy(path, text, **kw):
        opened.append(str(Path(path).resolve()))
        return real(path, text, **kw)

    monkeypatch.setattr(drive_io, "write_text_atomic", spy)
    assert _run(["--apply"]) == 0
    zk = roots["zk"].resolve()
    zx = roots["zx"].resolve()
    for p in opened:
        assert str(zk) in p or str(zx) in p, p


def test_assert_under_roots_raises_outside(roots):
    with pytest.raises(RuntimeError):
        m._write(Path(roots["zk"]).parent / "escape.md", "x")


# ── zone routing ──────────────────────────────────────────────────────────────
def test_zone_routing_cora_slugs_to_zone_x(roots):
    _codemem(roots["code_projects"], "C--Users-Harri-code-cora", "MEMORY.md", "cora mem")
    _codemem(roots["code_projects"], "C--Users-Harri-code-cora-revops", "MEMORY.md", "revops mem")
    _codemem(roots["code_projects"], "C--Users-Harri-code-rogers-ranch-web", "MEMORY.md", "web repo mem")
    _run(["--apply", "--only", "code_memory"])
    assert (roots["zx"] / "code-memory" / "cora" / "cora-mirror-MEMORY.md").exists()
    assert (roots["zx"] / "code-memory" / "cora-revops" / "cora-mirror-MEMORY.md").exists()
    assert (roots["zk"] / "code-memory" / "rogers-ranch-web" / "MEMORY.md").exists()
    # the cora repo memory must NOT land in ZONE-K
    assert not list((roots["zk"] / "code-memory").glob("cora/*")) if (roots["zk"] / "code-memory").exists() else True


# ── skills allow / deny / unknown ─────────────────────────────────────────────
def test_skills_allow_deny_unknown(roots):
    _skill(roots["skills"], "wrap-it")          # allowlisted
    _skill(roots["skills"], "docx")             # stock deny
    _skill(roots["skills"], "brand-new-skill")  # unknown -> WARN, not mirrored
    _run(["--apply", "--only", "skills"])
    assert (roots["zk"] / "skills" / "wrap-it.SKILL.md").exists()
    assert not (roots["zk"] / "skills" / "docx.SKILL.md").exists()
    assert not (roots["zk"] / "skills" / "brand-new-skill.SKILL.md").exists()
    parity = (roots["zk"] / "PARITY-REPORT.md").read_text(encoding="utf-8")
    assert "brand-new-skill" in parity and "not in allowlist" in parity


# ── PHI / LEX / personal screen + allow_files opt-in ──────────────────────────
def test_screen_quarantines_and_allow_files_overrides(roots, monkeypatch):
    _skill(roots["skills"], "cascade", body="benign")
    # a Cowork memory file that trips LEX
    _mem(roots["cowork_mem"], "space1", "lex-note.md", "The LEX-LTS census update.")
    # a memory file that trips personal-family
    _mem(roots["cowork_mem"], "space1", "capital.md", "capital-raise deck v3")
    # a clean one
    _mem(roots["cowork_mem"], "space1", "clean.md", "F3 launch timeline notes")
    _run(["--apply"])
    zk_mem = roots["zk"] / "cowork-memory" / "space1"
    assert (zk_mem / "clean.md").exists()
    assert not (zk_mem / "lex-note.md").exists()
    assert not (zk_mem / "capital.md").exists()
    q = (roots["zx"] / "_quarantine" / "INDEX.md").read_text(encoding="utf-8")
    assert "lex-note.md" in q and "capital.md" in q

    # now opt lex-note.md in by exact name
    real_load = m.load_config

    def patched(path=m.CONFIG_PATH):
        cfg = real_load(path)
        cfg.allow_files = {"lex-note.md"}
        return cfg

    monkeypatch.setattr(m, "load_config", patched)
    _run(["--apply"])
    assert (zk_mem / "lex-note.md").exists()          # opted in
    assert not (zk_mem / "capital.md").exists()       # still quarantined


# ── 512KB cap ─────────────────────────────────────────────────────────────────
def test_oversize_skipped_not_truncated(roots):
    big = "x" * (524288 + 10)
    _skill(roots["skills"], "morning", body=big)
    _run(["--apply", "--only", "skills"])
    assert not (roots["zk"] / "skills" / "morning.SKILL.md").exists()
    parity = (roots["zk"] / "PARITY-REPORT.md").read_text(encoding="utf-8")
    assert "cap" in parity.lower()


# ── idempotency ───────────────────────────────────────────────────────────────
def test_idempotent_second_apply_same_bytes(roots):
    _skill(roots["skills"], "wrap-it")
    _run(["--apply", "--only", "skills"])
    p = roots["zk"] / "skills" / "wrap-it.SKILL.md"
    first = p.read_bytes()
    # a mirror file carries a MIRROR-AT timestamp header, so byte-equality is not
    # expected; the SOURCE sha in the manifest must be stable instead.
    man1 = json.loads((roots["zx"] / "manifest-latest.json").read_text(encoding="utf-8"))
    _run(["--apply", "--only", "skills"])
    man2 = json.loads((roots["zx"] / "manifest-latest.json").read_text(encoding="utf-8"))
    sha1 = {w["dest"]: w["sha256"] for w in man1["writes"] if w["cls"] == "skills" and w["sha256"]}
    sha2 = {w["dest"]: w["sha256"] for w in man2["writes"] if w["cls"] == "skills" and w["sha256"]}
    assert sha1 == sha2 and sha1  # source content unchanged -> identical shas


# ── removals: ZONE-K stub, ZONE-X move ────────────────────────────────────────
def test_removal_zone_k_stub_and_zone_x_move(roots):
    _skill(roots["skills"], "wrap-it")
    _task(roots["tasks"], "daily-brief", model="sonnet")
    _run(["--apply"])
    zk_skill = roots["zk"] / "skills" / "wrap-it.SKILL.md"
    zx_body = roots["zx"] / "cowork-scheduled-tasks" / "cora-mirror-daily-brief.md"
    assert zk_skill.exists() and zx_body.exists()

    # remove both sources
    import shutil
    shutil.rmtree(roots["skills"] / "wrap-it")
    shutil.rmtree(roots["tasks"] / "daily-brief")
    _run(["--apply"])
    # ZONE-K skill overwritten with a SUPERSEDED stub at the SAME path
    assert zk_skill.exists()
    assert "KB-STATUS: SUPERSEDED" in zk_skill.read_text(encoding="utf-8")
    # ZONE-X body moved under _removed/<date>/
    assert not zx_body.exists()
    moved = list((roots["zx"] / "_removed").rglob("cora-mirror-daily-brief.md"))
    assert moved, "ZONE-X body should be moved to _removed/"


# ── manifest-first + finally ──────────────────────────────────────────────────
def test_manifest_written_before_and_after_exception(roots, monkeypatch):
    _skill(roots["skills"], "wrap-it")

    def boom(plan, **kw):
        raise RuntimeError("injected mid-apply")

    monkeypatch.setattr(m, "apply_plan", boom)
    with pytest.raises(RuntimeError):
        _run(["--apply", "--only", "skills"])
    # manifest exists despite the mid-apply crash (written before, re-written in finally)
    assert (roots["zx"] / "manifest-latest.json").exists()
    assert list((roots["zx"] / "_manifests").glob("*.json"))


# ── --revert ──────────────────────────────────────────────────────────────────
def test_revert_restores(roots):
    _skill(roots["skills"], "wrap-it")
    _run(["--apply", "--only", "skills"])
    man = sorted((roots["zx"] / "_manifests").glob("*.json"))[-1]
    assert (roots["zk"] / "skills" / "wrap-it.SKILL.md").exists()
    assert m.main(["--revert", str(man)]) == 0
    assert not (roots["zk"] / "skills" / "wrap-it.SKILL.md").exists()


# ── task-estate delta incl. unpinned ──────────────────────────────────────────
def test_task_estate_delta_added_removed_unpinned(roots):
    _task(roots["tasks"], "task-a", model="sonnet")
    _task(roots["tasks"], "task-b", model="")  # unpinned
    _run(["--apply"])
    parity1 = (roots["zk"] / "PARITY-REPORT.md").read_text(encoding="utf-8")
    assert "task-b" in parity1  # unpinned surfaced
    # add one, remove one
    _task(roots["tasks"], "task-c", model="haiku")
    import shutil
    shutil.rmtree(roots["tasks"] / "task-a")
    _run(["--apply"])
    parity2 = (roots["zk"] / "PARITY-REPORT.md").read_text(encoding="utf-8")
    assert "**added**" in parity2 and "task-c" in parity2
    assert "**removed**" in parity2 and "task-a" in parity2


# ── NOT FOUND root -> WARN, not exception ─────────────────────────────────────
def test_not_found_root_is_warn_not_exception(roots, monkeypatch):
    monkeypatch.setenv("COWORK_TASKS_ROOT", str(roots["tasks"] / "does-not-exist"))
    assert _run([]) == 0  # no exception
    parity = None
    # dry-run prints parity to stdout; also build one directly
    cfg = m.load_config()
    plan = m.build_plan(cfg, None)
    assert plan.roots_found.get("cowork_tasks") is False
    assert any("NOT FOUND" in w for w in plan.warns)


# ── generated INDEX + provenance header present ───────────────────────────────
def test_index_and_provenance(roots):
    _skill(roots["skills"], "cora", desc="reach Cora")
    _run(["--apply", "--only", "skills"])
    idx = roots["zk"] / "skills" / "INDEX.md"
    assert idx.exists() and "reach Cora" in idx.read_text(encoding="utf-8")
    body = (roots["zk"] / "skills" / "cora.SKILL.md").read_text(encoding="utf-8")
    assert "MIRROR-SOURCE" in body and "READ-ONLY MIRROR" in body


# ── config default matches the pin script's $Root (drift guard) ───────────────
def test_tasks_default_root_matches_pin_script():
    cfg = m.load_config()
    pin = Path(r"C:\Users\Harri\code\pin-scheduled-task-models.ps1")
    if not pin.exists():
        pytest.skip("pin script not on this host")
    text = pin.read_text(encoding="utf-8", errors="replace")
    import re
    mt = re.search(r'\$Root\s*=\s*"([^"]+)"', text)
    assert mt, "could not find $Root default in the pin script"
    pin_default = mt.group(1)
    # normalize %USERPROFILE% vs $env:USERPROFILE
    norm = lambda s: s.replace("$env:USERPROFILE", "%USERPROFILE%").lower()
    assert norm(cfg.tasks_default_root) == norm(pin_default), (
        f"config default {cfg.tasks_default_root!r} != pin $Root {pin_default!r}")


def test_quarantine_key_is_unique_path_not_basename(roots, monkeypatch):
    # two spaces each with a MEMORY.md that trips the screen -- the quarantine
    # identity must distinguish them, and a full-key opt-in must affect only one.
    _mem(roots["cowork_mem"], "spaceA", "MEMORY.md", "LEX-LTS roster")
    _mem(roots["cowork_mem"], "spaceB", "MEMORY.md", "LEX-LTS roster")
    _run(["--apply"])
    q = (roots["zx"] / "_quarantine" / "INDEX.md").read_text(encoding="utf-8")
    assert "cowork-memory/spaceA/MEMORY.md" in q
    assert "cowork-memory/spaceB/MEMORY.md" in q

    real_load = m.load_config

    def patched(path=m.CONFIG_PATH):
        cfg = real_load(path)
        cfg.allow_files = {"cowork-memory/spacea/memory.md"}  # only space A, by full key
        return cfg

    monkeypatch.setattr(m, "load_config", patched)
    _run(["--apply"])
    assert (roots["zk"] / "cowork-memory" / "spaceA" / "MEMORY.md").exists()
    assert not (roots["zk"] / "cowork-memory" / "spaceB" / "MEMORY.md").exists()
