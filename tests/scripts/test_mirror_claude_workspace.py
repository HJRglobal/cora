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
def test_zone_routing_cora_slugs_to_zone_x(roots, monkeypatch):
    monkeypatch.setattr(m, "CORA_REPO_ROOT", Path(r"C:\Users\Harri\code\cora"))
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
    q = (roots["zx"] / "_quarantine" / "cora-mirror-INDEX.md").read_text(encoding="utf-8")
    assert "lex-note.md" in q and "capital.md" in q

    # now opt lex-note.md in by exact name
    real_load = m.load_config

    def patched(path=m.CONFIG_PATH):
        cfg = real_load(path)
        cfg.allow_files = {"cowork-memory/space1/lex-note.md"}
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
    man1 = json.loads((roots["zx"] / "cora-mirror-manifest-latest.json").read_text(encoding="utf-8"))
    _run(["--apply", "--only", "skills"])
    man2 = json.loads((roots["zx"] / "cora-mirror-manifest-latest.json").read_text(encoding="utf-8"))
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
    assert (roots["zx"] / "cora-mirror-manifest-latest.json").exists()
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
    q = (roots["zx"] / "_quarantine" / "cora-mirror-INDEX.md").read_text(encoding="utf-8")
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


# ── D-051 remediation coverage (2026-09-03) ──────────────────────────────────
def test_unchanged_source_is_not_rewritten(roots):
    """M3: an unchanged source keeps its bytes (and MIRROR-AT) across runs, so
    static_md does not re-embed the whole mirror every run."""
    _skill(roots["skills"], "wrap-it")
    _run(["--apply", "--only", "skills"])
    p = roots["zk"] / "skills" / "wrap-it.SKILL.md"
    b1 = p.read_bytes()
    import time; time.sleep(1.1)
    _run(["--apply", "--only", "skills"])
    assert p.read_bytes() == b1, "unchanged source must not be rewritten (no re-embed churn)"
    man = json.loads((roots["zx"] / "cora-mirror-manifest-latest.json").read_text(encoding="utf-8"))
    skill_entry = [w for w in man["writes"] if w["cls"] == "skills" and w["dest"].endswith("wrap-it.SKILL.md")][0]
    assert skill_entry["written"] is False and skill_entry.get("skipped") == "unchanged"


def test_changed_source_is_rewritten(roots):
    _skill(roots["skills"], "wrap-it", body="v1")
    _run(["--apply", "--only", "skills"])
    p = roots["zk"] / "skills" / "wrap-it.SKILL.md"
    assert "v1" in p.read_text(encoding="utf-8")
    _skill(roots["skills"], "wrap-it", body="v2-changed")
    _run(["--apply", "--only", "skills"])
    assert "v2-changed" in p.read_text(encoding="utf-8")


def test_revert_leaves_preexisting_files(roots):
    """M4: reverting run 2 must NOT delete a file that run 1 created (run 2 only
    overwrote/left it) -- revert deletes only what THIS run created."""
    _skill(roots["skills"], "wrap-it", body="v1")
    _run(["--apply", "--only", "skills"])
    p = roots["zk"] / "skills" / "wrap-it.SKILL.md"
    _skill(roots["skills"], "morning", body="new")  # add a second skill in run 2
    _run(["--apply", "--only", "skills"])
    man2 = sorted((roots["zx"] / "_manifests").glob("*.json"))[-1]
    m.main(["--revert", str(man2)])
    # morning was CREATED by run 2 -> deleted; wrap-it pre-existed -> kept
    assert not (roots["zk"] / "skills" / "morning.SKILL.md").exists()
    assert p.exists(), "revert must not delete a file a prior run created"


def test_stub_is_idempotent_not_rechurned(roots):
    """M6: once a removed source is stubbed, later runs must not re-stub it."""
    _skill(roots["skills"], "wrap-it")
    _run(["--apply", "--only", "skills"])
    import shutil
    shutil.rmtree(roots["skills"] / "wrap-it")
    _run(["--apply", "--only", "skills"])  # writes the stub
    stub = roots["zk"] / "skills" / "wrap-it.SKILL.md"
    assert "KB-STATUS: SUPERSEDED" in stub.read_text(encoding="utf-8")
    b1 = stub.read_bytes()
    _run(["--apply", "--only", "skills"])  # must NOT re-stub
    assert stub.read_bytes() == b1
    man = json.loads((roots["zx"] / "cora-mirror-manifest-latest.json").read_text(encoding="utf-8"))
    assert not any(ev.get("action") == "superseded-stub" for ev in man.get("removals", [])), \
        "an already-stubbed path must not be re-detected as a removal"


def test_task_name_redacted_when_name_trips_screen(roots):
    """H4: a LEX task NAME must not ride into the ZONE-K INDEX."""
    _task(roots["tasks"], "cowork-cora-lex-lbhs-digest", name="cowork-cora-lex-lbhs-digest",
          model="sonnet", desc="LBHS COPA weekly")
    _task(roots["tasks"], "daily-brief", name="daily-brief", model="sonnet", desc="morning brief")
    _run(["--apply", "--only", "cowork_tasks"])
    idx = (roots["zk"] / "cowork-scheduled-tasks" / "INDEX.md").read_text(encoding="utf-8")
    assert "lex-lbhs" not in idx.lower(), "LEX task name leaked into ZONE-K INDEX"
    assert "task withheld" in idx.lower()
    assert "daily-brief" in idx  # the clean one is intact
    # the full body still lands in ZONE-X (with the cora-mirror- prefix)
    assert (roots["zx"] / "cowork-scheduled-tasks" / "cora-mirror-cowork-cora-lex-lbhs-digest.md").exists()


def test_skills_index_row_absent_when_body_quarantined(roots):
    """H3: a skill whose body is quarantined must not leave an INDEX row (whose
    description would leak the screened text)."""
    _skill(roots["skills"], "cascade", desc="handles the LEX-LTS census", body="clean body")
    _skill(roots["skills"], "wrap-it", desc="closes a session", body="clean")
    _run(["--apply", "--only", "skills"])
    assert not (roots["zk"] / "skills" / "cascade.SKILL.md").exists()  # quarantined (LEX in desc)
    idx = (roots["zk"] / "skills" / "INDEX.md").read_text(encoding="utf-8")
    assert "census" not in idx.lower() and "lex-lts" not in idx.lower()
    assert "wrap-it" in idx


def test_zone_k_parity_has_no_quarantined_filenames(roots):
    """M1: the ZONE-K parity report shows quarantine COUNTS only, never the
    (possibly client-named) filenames -- those live in ZONE-X."""
    _mem(roots["cowork_mem"], "space1", "lexington-lbhs-billing.md", "LEX-LBHS billing note")
    _run(["--apply"])
    parity = (roots["zk"] / "PARITY-REPORT.md").read_text(encoding="utf-8")
    assert "lexington-lbhs-billing" not in parity.lower()
    assert "counts only" in parity.lower() and "total:" in parity.lower()
    # the full name is in the ZONE-X quarantine index
    q = (roots["zx"] / "_quarantine" / "cora-mirror-INDEX.md").read_text(encoding="utf-8")
    assert "lexington-lbhs-billing.md" in q


def test_path_token_screened_into_quarantine(roots):
    """H2: a clean-content memory file whose KEY carries a LEX token is quarantined
    (the dest path + provenance header would otherwise ride the token into ZONE-K)."""
    _mem(roots["cowork_mem"], "space1", "lexington-notes.md", "totally benign body text")
    _run(["--apply"])
    assert not (roots["zk"] / "cowork-memory" / "space1" / "lexington-notes.md").exists()
    q = (roots["zx"] / "_quarantine" / "cora-mirror-INDEX.md").read_text(encoding="utf-8")
    assert "lexington-notes.md" in q


def test_zone_x_generated_files_trip_the_drive_sweep_belt():
    """H1: every ZONE-X filename must trip is_cora_internal_title so drive_sweep
    can't ingest it before the folder-id is pinned."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from cora.kb_exclusions import is_cora_internal_title
    for name in ("cora-mirror-LADDER-ROW.md", "cora-mirror-INDEX.md",
                 "cora-mirror-daily-brief.md", "cora-mirror-MEMORY.md"):
        assert is_cora_internal_title(name, broad=True), name


def test_null_at_utc_status_does_not_crash_health(roots, monkeypatch):
    """M7: a status file with an unparseable at_utc yields available=True but
    age_hours=None -- read_parity_status must not crash, and the value is flagged."""
    (roots["zk"]).mkdir(parents=True, exist_ok=True)
    (roots["zk"] / "mirror-status.json").write_text(
        json.dumps({"at_utc": "", "roots_missing": [], "quarantined_count": 0,
                    "unknown_skills": [], "warns": [], "unpinned": [], "added": [],
                    "removed": [], "model_changed": []}), encoding="utf-8")
    st = m.read_parity_status()
    assert st["available"] and st["age_hours"] is None and st["stale"] is False


def test_only_run_does_not_remove_other_classes(roots):
    """A --only partial run must NOT treat every OTHER class's prior mirror as a
    removal (regression from the remediation)."""
    _skill(roots["skills"], "wrap-it")
    _task(roots["tasks"], "daily-brief", name="daily-brief")
    _run(["--apply"])  # full run mirrors both
    zx_body = roots["zx"] / "cowork-scheduled-tasks" / "cora-mirror-daily-brief.md"
    assert zx_body.exists()
    # now a skills-only run -- the task mirror must be untouched, not moved to _removed
    _run(["--apply", "--only", "skills"])
    assert zx_body.exists(), "a --only skills run wrongly removed the task mirror"
    assert not list((roots["zx"] / "_removed").rglob("*")) if (roots["zx"] / "_removed").exists() else True


# ── worktree handling (2026-09-03, cowork-side findings §2) ───────────────────
def test_path_to_slug_matches_claude_code_scheme():
    assert m._path_to_slug(r"C:\Users\Harri\code\cora") == "C--Users-Harri-code-cora"
    assert m._path_to_slug(r"C:\Users\Harri\code\cora\.claude\worktrees\lexicon-flywheel") == \
        "C--Users-Harri-code-cora--claude-worktrees-lexicon-flywheel"
    assert m._path_to_slug(r"G:\My Drive\HJR-Founder-OS") == "G--My-Drive-HJR-Founder-OS"


def test_cora_codebase_slugs_route_to_zone_x_by_path_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "CORA_REPO_ROOT", Path(r"D:\work\code\cora"))
    assert m.slug_repo_zone("D--work-code-cora") == ("cora", "X")
    assert m.slug_repo_zone("D--work-code-cora-revops") == ("cora-revops", "X")            # sibling worktree
    assert m.slug_repo_zone("D--work-code-cora--claude-worktrees-lexicon-flywheel") == \
        ("cora-worktree-lexicon-flywheel", "X")                                             # .claude/worktrees
    assert m.slug_repo_zone("d--WORK-code-cora") == ("cora", "X")                           # case-insensitive
    # a lookalike sibling repo is NOT the cora codebase (prefix + "-" boundary)
    assert m.slug_repo_zone("D--work-code-coral-reef") == ("coral-reef", "K")
    assert m.slug_repo_zone("D--work-code-rogers-ranch-web") == ("rogers-ranch-web", "K")
    # belt: a cora-ish slug OUTSIDE the checkout path still goes to ZONE-X, under its own label
    label, zone = m.slug_repo_zone("G--My-Drive-HJR-Founder-OS--shared-projects-cora")
    assert zone == "X" and label != "cora"
    lab, zone = m.slug_repo_zone("C--elsewhere-cora-revops")
    assert zone == "X" and lab == "c--elsewhere-cora-revops"        # belt: full slug, never the sibling's label


def test_prefix_resolves_to_the_base_checkout_when_run_from_a_worktree(monkeypatch, tmp_path):
    base = tmp_path / "cora"
    (base / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    wt = tmp_path / "cora-wt1"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {base / '.git' / 'worktrees' / 'wt1'}\n", encoding="utf-8")
    monkeypatch.setattr(m, "CORA_REPO_ROOT", wt)
    assert m._cora_base_repo_root() == base
    assert m._cora_slug_prefix() == m._path_to_slug(base).lower()


def _fake_repo(tmp_path, *, registered: dict):
    """A fake cora checkout with .git/worktrees/<name>/gitdir registrations."""
    base = tmp_path / "code" / "cora"
    (base / ".git").mkdir(parents=True)
    for name, path in registered.items():
        d = base / ".git" / "worktrees" / name
        d.mkdir(parents=True)
        (d / "gitdir").write_text(str(path / ".git") + "\n", encoding="utf-8")
    return base


def test_prunable_worktree_slug_is_reported_not_found_not_thrown_not_skipped(roots, monkeypatch, tmp_path):
    gone = tmp_path / "code" / "cora-revops"                       # registered, NOT on disk = prunable
    base = _fake_repo(tmp_path, registered={"cora-revops": gone})
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    slug_gone = m._path_to_slug(gone)
    slug_absent = m._path_to_slug(base) + "--claude-worktrees-web-gate-notes-fix"   # never registered, not on disk
    _codemem(roots["code_projects"], slug_gone, "MEMORY.md", "revops memory survives the worktree")
    _codemem(roots["code_projects"], slug_absent, "project_x.md", "worktree memory")
    _codemem(roots["code_projects"], m._path_to_slug(base), "MEMORY.md", "base memory")
    cfg = m.load_config()
    plan = m.build_plan(cfg, "code_memory")                         # must not raise
    nf = [w for w in plan.warns if "worktree NOT FOUND" in w]
    assert len(nf) == 2
    assert any(slug_gone in w and "cora-revops" in w for w in nf)
    assert any(slug_absent in w and "web-gate-notes-fix" in w for w in nf)
    assert not any(f"'{m._path_to_slug(base)}'" in w for w in nf)  # the base checkout itself is found
    assert plan.counts["code_memory"]["worktree_not_found"] == 2
    # the memory files behind a gone worktree are STILL mirrored (ZONE-X), never skipped
    dests = {w.dest.as_posix() for w in plan.writes}
    assert any(d.endswith("code-memory/cora-revops/cora-mirror-MEMORY.md") for d in dests)
    assert any(d.endswith("code-memory/cora-worktree-web-gate-notes-fix/cora-mirror-project_x.md") for d in dests)
    assert any(d.endswith("code-memory/cora/cora-mirror-MEMORY.md") for d in dests)
    assert all(w.zone == "X" for w in plan.writes if w.cls == "code_memory")


def test_registered_worktree_on_disk_is_ok_and_unregistered_dir_is_flagged(roots, monkeypatch, tmp_path):
    present = tmp_path / "code" / "cora" / ".claude" / "worktrees" / "lexicon-flywheel"
    present.mkdir(parents=True)
    base = _fake_repo(tmp_path, registered={"lexicon-flywheel": present})
    (present / ".git").write_text(f"gitdir: {base / '.git' / 'worktrees' / 'lexicon-flywheel'}\n", encoding="utf-8")
    orphan = base / ".claude" / "worktrees" / "dw-rebase"           # on disk, registration pruned
    orphan.mkdir(parents=True)
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    assert m._worktree_status(m._path_to_slug(present)) == ("ok", present)
    status, path = m._worktree_status(m._path_to_slug(orphan))
    assert status == "unregistered" and path == orphan
    _codemem(roots["code_projects"], m._path_to_slug(present), "MEMORY.md", "ok")
    _codemem(roots["code_projects"], m._path_to_slug(orphan), "MEMORY.md", "orphan")
    plan = m.build_plan(m.load_config(), "code_memory")
    assert not any("NOT FOUND" in w for w in plan.warns)
    assert sum("not registered" in w for w in plan.warns) == 1


def test_two_cora_slugs_with_memory_md_do_not_collide_on_dest(roots, monkeypatch, tmp_path):
    base = _fake_repo(tmp_path, registered={})
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    _codemem(roots["code_projects"], m._path_to_slug(base), "MEMORY.md", "checkout memory")
    _codemem(roots["code_projects"], "G--My-Drive-HJR-Founder-OS--shared-projects-cora", "MEMORY.md", "drive-cwd memory")
    plan = m.build_plan(m.load_config(), "code_memory")
    mem = [w for w in plan.writes if w.dest.name == "cora-mirror-MEMORY.md"]
    assert len(mem) == 2 and len({w.dest for w in mem}) == 2       # two files, two dests
    assert not any("dest collision" in w for w in plan.warns)
    assert all(w.zone == "X" for w in mem)


def test_dest_collision_guard_keeps_both_and_uniquifies_the_second(roots):
    dest = roots["zx"] / "code-memory" / "cora" / "cora-mirror-MEMORY.md"
    plan = m.Plan()
    a = m.Planned(dest=dest, zone="X", text="a", source="~/.claude/projects/A/memory/MEMORY.md", cls="code_memory")
    b = m.Planned(dest=dest, zone="X", text="b", source="~/.claude/projects/B/memory/MEMORY.md", cls="code_memory")
    plan.writes = [a, b]
    m._uniquify_dest_collisions(plan)
    assert plan.writes == [a, b] and a.dest == dest and b.dest != dest    # nothing dropped
    assert b.dest.parent == dest.parent and b.dest.suffix == ".md"
    assert b.dest.name.startswith("cora-mirror-MEMORY-")                   # belt prefix kept
    assert any("dest collision" in w and "projects/B" in w for w in plan.warns)
    assert plan.counts["code_memory"]["dest_uniquified"] == 1
    # deterministic run to run
    plan2 = m.Plan()
    a2 = m.Planned(dest=dest, zone="X", text="a", source=a.source, cls="code_memory")
    b2 = m.Planned(dest=dest, zone="X", text="b", source=b.source, cls="code_memory")
    plan2.writes = [a2, b2]
    m._uniquify_dest_collisions(plan2)
    assert b2.dest == b.dest
    # a case-variant dest is the same file on Windows and is treated as a collision too
    plan3 = m.Plan()
    c1 = m.Planned(dest=dest, zone="X", text="a", source="s1", cls="code_memory")
    c2 = m.Planned(dest=dest.with_name("CORA-MIRROR-memory.md"), zone="X", text="b", source="s2", cls="code_memory")
    plan3.writes = [c1, c2]
    m._uniquify_dest_collisions(plan3)
    if os.path.normcase("A") == os.path.normcase("a"):
        assert c2.dest.name != "CORA-MIRROR-memory.md"
    else:
        assert c2.dest.name == "CORA-MIRROR-memory.md"


def test_dest_collision_guard_is_wired_through_build_plan(roots, monkeypatch, tmp_path):
    # MED-3 (tests lens): prove the guard RUNS from build_plan, not just in isolation.
    base = _fake_repo(tmp_path, registered={})
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    monkeypatch.setattr(m, "slug_repo_zone", lambda slug: ("same-label", "X"))
    _codemem(roots["code_projects"], "A--one", "MEMORY.md", "one")
    _codemem(roots["code_projects"], "B--two", "MEMORY.md", "two")
    plan = m.build_plan(m.load_config(), "code_memory")
    mem = [w for w in plan.writes if w.cls == "code_memory"]
    assert len(mem) == 2 and len({os.path.normcase(str(w.dest)) for w in mem}) == 2
    assert sum("dest collision" in w for w in plan.warns) == 1
    assert plan.counts["code_memory"]["dest_uniquified"] == 1
    assert all(w.dest.name.startswith("cora-mirror-") for w in mem)      # belt shape kept
    assert plan.counts["code_memory"]["mirrored"] == 2                    # counts stay truthful


def test_relative_gitdir_paths_resolve_against_the_dot_git_file(monkeypatch, tmp_path):
    # git >= 2.48 worktree.useRelativePaths: both the worktree's .git file and the
    # registration's gitdir may be RELATIVE. Neither may collapse the prefix or
    # report a healthy worktree as unregistered.
    base = tmp_path / "cora"
    reg = base / ".git" / "worktrees" / "wt-rel"
    reg.mkdir(parents=True)
    wt = tmp_path / "cora-wt-rel"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../cora/.git/worktrees/wt-rel\n", encoding="utf-8")
    (reg / "gitdir").write_text("../../../../cora-wt-rel/.git\n", encoding="utf-8")
    monkeypatch.setattr(m, "CORA_REPO_ROOT", wt)
    assert m._cora_base_repo_root() == base.resolve()
    assert m._cora_slug_prefix() == m._path_to_slug(base.resolve()).lower()
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    status, path = m._worktree_status(m._path_to_slug(wt.resolve()))
    assert status == "ok" and path == wt.resolve()


def test_subdirectory_session_slug_is_the_same_checkout_not_a_missing_worktree(roots, monkeypatch, tmp_path):
    base = _fake_repo(tmp_path, registered={})
    (base / "scripts").mkdir()
    (base / "src" / "cora").mkdir(parents=True)
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    slug = m._path_to_slug(base / "scripts")
    assert m._worktree_status(slug) == ("ok", base / "scripts")
    assert m.slug_repo_zone(slug) == ("cora-scripts", "X")               # still ZONE-X, its own label
    assert m._worktree_status(m._path_to_slug(base / "src" / "cora"))[0] == "ok"
    _codemem(roots["code_projects"], slug, "MEMORY.md", "subdir session memory")
    plan = m.build_plan(m.load_config(), "code_memory")
    assert not any("NOT FOUND" in w for w in plan.warns)


def test_prunable_means_the_gitfile_is_gone_and_locked_is_not_prunable(roots, monkeypatch, tmp_path):
    kept_dir = tmp_path / "code" / "cora" / ".claude" / "worktrees" / "dir-kept-gitfile-gone"
    kept_dir.mkdir(parents=True)                       # dir present, NO .git file -> git: prunable
    locked_gone = tmp_path / "code" / "cora-locked"    # registered + locked marker, dir absent -> git: NOT prunable
    base = _fake_repo(tmp_path, registered={"dir-kept": kept_dir, "cora-locked": locked_gone})
    (base / ".git" / "worktrees" / "cora-locked" / "locked").write_text("", encoding="utf-8")
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    assert m._worktree_status(m._path_to_slug(kept_dir))[0] == "missing"
    assert m._worktree_status(m._path_to_slug(locked_gone))[0] == "locked"
    _codemem(roots["code_projects"], m._path_to_slug(kept_dir), "MEMORY.md", "a")
    _codemem(roots["code_projects"], m._path_to_slug(locked_gone), "MEMORY.md", "b")
    plan = m.build_plan(m.load_config(), "code_memory")
    assert sum("worktree NOT FOUND" in w for w in plan.warns) == 1       # the prunable one
    assert sum("LOCKED" in w for w in plan.warns) == 1                   # the locked one, not NOT FOUND
    assert plan.counts["code_memory"]["worktree_not_found"] == 1
    assert plan.counts["code_memory"]["worktree_locked"] == 1
    assert len([w for w in plan.writes if w.cls == "code_memory"]) == 2  # both still mirrored


def test_parity_table_renders_every_recorded_count_key():
    import re as _re
    src = Path(m.__file__).read_text(encoding="utf-8")
    recorded = set(_re.findall(r'_record\(plan, [^,]+, "([a-z_]+)"\)', src))
    assert recorded, "no _record() calls found -- regex drifted"
    missing = recorded - set(m._ALL_COUNT_KEYS)
    assert not missing, f"count keys recorded but not rendered in the parity table: {sorted(missing)}"


def test_unreadable_memory_file_is_a_warn_not_a_silent_skip(roots, monkeypatch, tmp_path):
    base = _fake_repo(tmp_path, registered={})
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    _codemem(roots["code_projects"], m._path_to_slug(base), "MEMORY.md", "ok")
    real_read = Path.read_text

    def boom(self, *a, **k):
        if self.name == "MEMORY.md":
            raise OSError("locked")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    plan = m.build_plan(m.load_config(), "code_memory")
    assert any("could not read" in w for w in plan.warns)
    assert plan.counts["code_memory"].get("unreadable") == 1


def test_colliding_opted_in_zone_k_key_is_quarantined_not_released_under_a_derived_name(roots, monkeypatch, tmp_path):
    # D-051 second pass LOW-3: allow_files is keyed on the dest; two sources sharing
    # that dest were opted in as ONE reviewed key. The second must not reach the
    # KB-ingested zone under a name nobody reviewed.
    base = _fake_repo(tmp_path, registered={})
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    monkeypatch.setattr(m, "slug_repo_zone", lambda slug: ("foo", "K"))
    _codemem(roots["code_projects"], "A--one", "MEMORY.md", "Lexington client billing note one")
    _codemem(roots["code_projects"], "B--two", "MEMORY.md", "Lexington client billing note two")
    cfg = m.load_config()
    cfg.allow_files = {"code-memory/foo/memory.md"}                 # the reviewed key (lowercased)
    plan = m.build_plan(cfg, "code_memory")
    writes = [w for w in plan.writes if w.cls == "code_memory"]
    assert len(writes) == 1 and writes[0].dest.name == "MEMORY.md" and writes[0].allow_override
    assert any(k == "code-memory/foo/MEMORY.md" and "dest collision on an opted-in key" in r
               for k, r in plan.quarantined)
    assert sum("QUARANTINED" in w for w in plan.warns) == 1
    assert plan.counts["code_memory"]["mirrored"] == 1
    assert plan.counts["code_memory"]["quarantined"] == 1
    assert "dest_uniquified" not in plan.counts["code_memory"]
    # a CLEAN ZONE-K collision is still uniquified (both kept)
    plan2 = m.Plan()
    d = roots["zk"] / "code-memory" / "foo" / "MEMORY.md"
    x = m.Planned(dest=d, zone="K", text="a", source="s1", cls="code_memory")
    y = m.Planned(dest=d, zone="K", text="b", source="s2", cls="code_memory")
    plan2.writes = [x, y]
    m._uniquify_dest_collisions(plan2)
    assert len(plan2.writes) == 2 and y.dest != d and not plan2.quarantined


def test_sibling_checkout_wins_over_the_subdirectory_heuristic(monkeypatch, tmp_path):
    # D-051 second pass LOW-4: a sibling checkout named like a subdir (cora-scripts)
    # must not read "ok" just because <base>/scripts exists.
    base = _fake_repo(tmp_path, registered={})
    (base / "scripts").mkdir()
    monkeypatch.setattr(m, "CORA_REPO_ROOT", base)
    slug = m._path_to_slug(base / "scripts")                       # == slug of sibling <parent>/cora-scripts
    assert m._worktree_status(slug) == ("ok", base / "scripts")     # no sibling -> the subdir session
    sibling = base.parent / "cora-scripts"
    sibling.mkdir()
    assert m._worktree_status(slug) == ("unregistered", sibling)   # sibling exists -> it is the checkout
