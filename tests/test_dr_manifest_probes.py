"""DR manifest probe runner (DR/VM step 1, slice M2; cq-a296aa8e0a2e; charter D1/D3).

Contract under test:
  * every manifest item carries a source of truth, a restore step, an owner from the
    allowed set and an RTO (estimate or UNMEASURED); every item has EITHER a probe OR a
    manual note -- zero items without one (kickoff section 4 #4);
  * a probe that raises becomes a FAIL row, never a crash of the run;
  * the KB restore paths are BOTH present, side by side, with RTO UNMEASURED and the
    one historical bound named; the cold-boot drill and the full RTO are MANUAL/UNMEASURED;
  * env_key_names / env_example_schema return KEY NAMES only -- no value ever appears in
    what they return; the rendered block carries the schema as names;
  * the rendered block is idempotent for equal input and slots into the DR-MANIFEST.md
    markers; the JSON twin has the schema and a summary that adds up;
  * the COMMITTED deployment/manifest/dr-manifest.json + DR-MANIFEST.md carry the probe
    baseline (0 FAIL on the office host at commit time) and no secret shape.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import dr_manifest_probes as dmp  # noqa: E402
import generate_task_estate_manifest as tem  # noqa: E402
import secrets_scan as ss  # noqa: E402

_OWNERS = {"Harrison", "Justin", "Cora-script", "Harrison + Justin", "Harrison (elevated) + Cora-script",
           "Cora-script (Harrison runs)", "Harrison + Cora-script"}


def _ctx(tmp_path: Path) -> dmp.Ctx:
    return dmp.Ctx(live_root=tmp_path, repo_root=REPO, backups_root=tmp_path / "backups",
                   now=datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc))


class TestItems:
    def test_every_item_is_complete(self):
        items = dmp.build_items()
        ids = [i.id for i in items]
        assert len(ids) == len(set(ids)) >= 20
        for it in items:
            assert it.source_of_truth.strip() and it.restore_step.strip(), it.id
            assert it.owner in _OWNERS, (it.id, it.owner)
            assert it.rto.strip(), it.id
            assert (it.probe is not None) or it.manual_note.strip(), f"{it.id} has neither a probe nor a manual step"

    def test_kb_paths_side_by_side_and_unmeasured(self):
        items = {i.id: i for i in dmp.build_items()}
        a, b = items["D16"], items["D17"]
        assert "snapshot" in a.item.lower() and "connector" in b.item.lower()
        assert a.rto.startswith("UNMEASURED") and b.rto.startswith("UNMEASURED")
        assert dmp.KB_SNAPSHOT_DATE in a.source_of_truth
        assert "2026-05-28..06-17" in dmp.KB_REBUILD_HISTORICAL_BOUND
        assert items["D23"].probe is None and items["D24"].rto == "UNMEASURED"

    def test_probe_exception_is_a_fail_row_not_a_crash(self, tmp_path):
        boom = dmp.Item("X1", "x", "s", "r", "Harrison", "n/a", probe=lambda c: (_ for _ in ()).throw(RuntimeError("kaboom")))
        out = dmp.run_probes([boom], _ctx(tmp_path))
        assert out[0].result["status"] == dmp.FAIL and "kaboom" in out[0].result["detail"]

    def test_d13_title_carries_the_committed_count_not_a_typed_number(self):
        items = {i.id: i for i in dmp.build_items()}
        committed = json.loads((REPO / "deployment" / "manifest" / "task-estate.json").read_text(encoding="utf-8"))
        assert f"({committed['count']} tasks)" in items["D13"].item
        assert "register-estate-from-manifest.ps1" in items["D13"].restore_step and "setup-*.ps1" not in items["D13"].restore_step.split("NOT")[0]

    def test_repo_probe_records_main_hash_and_flags_a_feature_branch(self, tmp_path, monkeypatch):
        answers = {"rev-parse --short HEAD": "e46bd61", "rev-parse --abbrev-ref HEAD": "claude/other-branch",
                   "remote get-url origin": "git@github.com:HJRglobal/cora.git", "rev-parse --short origin/main": "f65b7fc"}
        monkeypatch.setattr(dmp, "_run", lambda cmd, cwd=None, timeout=60: answers.get(" ".join(cmd[3:]), ""))
        status, detail = dmp.p_repo(_ctx(tmp_path))
        assert status == dmp.MANUAL and "origin/main f65b7fc" in detail and "claude/other-branch" in detail
        answers["rev-parse --abbrev-ref HEAD"] = "main"
        assert dmp.p_repo(_ctx(tmp_path))[0] == dmp.PASS
        answers["remote get-url origin"] = ""
        assert dmp.p_repo(_ctx(tmp_path))[0] == dmp.FAIL

    def test_xml_restore_set_probe_fails_when_a_live_task_has_no_xml(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.repo_root = tmp_path
        (tmp_path / "deployment" / "manifest" / "tasks").mkdir(parents=True)
        (tmp_path / "deployment" / "manifest" / "tasks" / "a-task.xml").write_text("<Task/>", encoding="utf-8")
        ctx.tasks = {"A": {"log_slug": "a-task"}, "B": {"log_slug": "b-task"}}
        status, detail = dmp.p_task_xml_backups(ctx)
        assert status == dmp.FAIL and "MISSING for 1" in detail and "b-task" in detail
        ctx.tasks = {"A": {"log_slug": "a-task"}}
        assert dmp.p_task_xml_backups(ctx)[0] == dmp.PASS

    def test_manual_and_unmeasured_items_take_their_status_from_the_rto(self, tmp_path):
        m = dmp.Item("X2", "x", "s", "r", "Harrison", "MANUAL (step 2)", None, "do it by hand")
        u = dmp.Item("X3", "x", "s", "r", "Harrison", "UNMEASURED", None, "measure at step 2")
        out = dmp.run_probes([m, u], _ctx(tmp_path))
        assert out[0].result == {"status": dmp.MANUAL, "detail": "do it by hand"}
        assert out[1].result == {"status": dmp.UNMEASURED, "detail": "measure at step 2"}


class TestEnvNamesOnly:
    def test_env_key_names_never_returns_values(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SLACK_BOT_TOKEN=xoxb-secret-value-1234567890\n# COMMENTED=x\nOPENAI_API_KEY='sk-secretsecretsecret'\n", encoding="utf-8")
        names = dmp.env_key_names(env)
        assert names == {"SLACK_BOT_TOKEN", "OPENAI_API_KEY"}
        assert not any("secret" in n.lower() for n in names)

    def test_env_example_schema_splits_active_and_commented(self, tmp_path):
        ex = tmp_path / ".env.example"
        ex.write_text("SLACK_BOT_TOKEN=<value>\n# SLACK_USER_TOKEN=<value>\n# just a comment\nOPENAI_API_KEY=<value>\n# OPENAI_API_KEY=<value>\n", encoding="utf-8")
        active, commented = dmp.env_example_schema(ex)
        assert active == ["OPENAI_API_KEY", "SLACK_BOT_TOKEN"] and commented == ["SLACK_USER_TOKEN"]

    def test_p_env_schema_flags_duplicate_keys_and_reports_names_only(self, tmp_path):
        live = tmp_path / ".env"
        live.write_text("HEALTH_PING_URL=https://hc-ping.com/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\nHEALTH_PING_URL=PASTE-UUID-HERE\nSLACK_BOT_TOKEN=xoxb-real-1234567890123\n", encoding="utf-8")
        ctx = _ctx(tmp_path)
        status, detail = dmp.p_env_schema(ctx)
        assert status == dmp.FAIL and "DUPLICATE keys: HEALTH_PING_URL" in detail
        assert "hc-ping.com" not in detail and "xoxb-real" not in detail


class TestRender:
    def test_block_is_idempotent_and_marker_gated(self, tmp_path):
        items = dmp.run_probes([dmp.Item("X2", "x", "s", "r", "Harrison", "MANUAL", None, "hand")], _ctx(tmp_path))
        ctx = _ctx(tmp_path)
        b1, b2 = dmp.render_block(items, ctx), dmp.render_block(items, ctx)
        assert b1 == b2 and "| X2 | x | s | r | **MANUAL** -- hand | Harrison | MANUAL |" in b1
        assert "`.env` schema (key NAMES" in b1
        doc = "# DR\n<!-- BEGIN GENERATED: dr-probe-baseline -->\nold\n<!-- END GENERATED: dr-probe-baseline -->\n"
        once = tem.replace_generated_block(doc, dmp.DR_BLOCK, b1)
        assert once == tem.replace_generated_block(once, dmp.DR_BLOCK, b1) and "old" not in once

    def test_json_twin_summary_adds_up(self, tmp_path):
        items = dmp.run_probes([
            dmp.Item("A", "a", "s", "r", "Harrison", "n/a", probe=lambda c: (dmp.PASS, "ok")),
            dmp.Item("B", "b", "s", "r", "Harrison", "n/a", probe=lambda c: (dmp.FAIL, "no")),
            dmp.Item("C", "c", "s", "r", "Harrison", "UNMEASURED", None, "later"),
        ], _ctx(tmp_path))
        j = dmp.to_json(items, _ctx(tmp_path))
        assert j["schema_version"] == 1 and sum(j["summary"].values()) == 3 == len(j["items"])
        assert j["summary"][dmp.PASS] == 1 and j["summary"][dmp.FAIL] == 1 and j["summary"][dmp.UNMEASURED] == 1
        assert j["kb_rto"] == {"a_snapshot": "UNMEASURED", "b_connector_rebuild": "UNMEASURED",
                               "b_historical_bound": dmp.KB_REBUILD_HISTORICAL_BOUND}


class TestCommittedBaseline:
    def test_committed_json_and_doc_carry_a_clean_baseline(self):
        jp = REPO / "deployment" / "manifest" / dmp.DR_JSON
        doc = REPO / "deployment" / "DR-MANIFEST.md"
        assert jp.exists() and doc.exists(), "M2 commits deployment/manifest/dr-manifest.json + deployment/DR-MANIFEST.md"
        j = json.loads(jp.read_text(encoding="utf-8"))
        assert j["summary"][dmp.FAIL] == 0, j["summary"]
        assert {i["id"] for i in j["items"]} == {i.id for i in dmp.build_items()}
        assert all(i["status"] in dmp.STATUSES for i in j["items"])
        text = doc.read_text(encoding="utf-8")
        begin, end = tem._markers(dmp.DR_BLOCK)  # noqa: SLF001
        assert text.count(begin) == 1 and text.count(end) == 1
        body = text.split(begin, 1)[1].split(end, 1)[0]
        for it in dmp.build_items():
            assert f"| {it.id} |" in body, it.id
        for section in ("Restore drill", "UNMEASURED", "snapshot", "connector rebuild", "Cowork estate", "D4"):
            assert section in text, section
        hits = ss.scan_paths([jp, doc], env_belt=True)
        assert hits == [], "\n".join(h.render() for h in hits)
        # the D13 row's counts are the committed task-estate count, not a typed number
        estate = json.loads((REPO / "deployment" / "manifest" / "task-estate.json").read_text(encoding="utf-8"))
        d13 = next(i for i in j["items"] if i["id"] == "D13")
        assert f"{estate['count']} live / {estate['count']} manifest" in d13["detail"], d13["detail"]
        assert f"({estate['count']} tasks)" in d13["item"]
        # names only: no live .env VALUE-shaped line in the doc
        assert not re.search(r"^\s*[A-Z][A-Z0-9_]+=(?!<)[^\s<]{8,}", text, re.MULTILINE)


@pytest.mark.skipif(sys.platform != "win32", reason="live probes need the Windows host")
def test_live_probes_run_end_to_end_read_only():
    items = dmp.build_items()
    tasks, te = dmp.load_tasks(dmp.LIVE_ROOT)
    ctx = dmp.Ctx(live_root=dmp.LIVE_ROOT, repo_root=REPO, backups_root=dmp.BACKUPS_ROOT,
                  now=datetime.now(timezone.utc), tasks=tasks, task_estate=te)
    out = dmp.run_probes(items, ctx)
    assert all(it.result.get("status") in dmp.STATUSES for it in out)
    # a probe row is never empty
    assert all(it.result.get("detail") for it in out)
