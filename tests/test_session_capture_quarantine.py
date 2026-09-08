"""Ingest-integrity bundle I2 (cq-bc5e5b7512bd + cq-e4b0d20a313f folded), 2026-09-08.

Leak #2: the harvester forced ``entity = "LEX"`` whenever the INGESTION-grade
``phi_guard.is_phi_risk`` tripped on a transcript -- and on the real transcripts
that regex trips on ``diagnosis`` (root-cause), ``arc``, ``assessment``,
``discharge``, ``patient`` ... 142 non-LEX sessions since 2026-06-11 were filed
into 08-Lexington-Services under a false PHI stamp.

Locked behaviour:
  * a NON-LEX distill is NEVER re-homed to LEX;
  * a non-LEX distill that trips the new VALUE-shaped prose screen
    (``is_prose_phi_risk``) is QUARANTINED into the KB-excluded founder-only
    quarantine folder, never KB-ingested, ledgered ``quarantined: true``;
  * a LEX distill keeps the strict ingestion posture (PHI line as before);
  * ``is_prose_phi_risk`` ignores topic words (diagnosis / arc / assessment /
    approved / pending / claims / the literal PHI regex vocabulary a code session
    quotes) and fires on values and individuals (a DOB value, an ICD-10 code,
    "diagnosed with", a programme id number, a care-noun-governed name next to a
    diagnosis or medication);
  * the mirror's ZONE-K screen uses the SAME prose screen (the cq-e4b0d20a313f
    fold): "status APPROVED" / "restock pending" / "CLAIMS CHECK" no longer
    quarantine an F3 skill or memory; LEX tokens and real PHI still do.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import phi_guard  # noqa: E402
from cora import session_capture as scap  # noqa: E402
from cora import kb_exclusions  # noqa: E402


# ── fixtures shared with tests/test_session_capture.py (kept local: independence) ──
class _FakeClient:
    def __init__(self, body: dict):
        self._text = json.dumps(body)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)])


def _body(entity="F3E", topic="did a thing"):
    return {"entity": entity, "topic": topic, "decisions": ["locked X"], "facts": ["Y"],
            "action_items": [], "open_questions": []}


def _session_file(projects: Path, sid: str, text_extra: str = "", cwd: str = r"C:\Users\Harri\code\cora") -> Path:
    proj = projects / "C--Users-Harri-code-cora"
    proj.mkdir(parents=True, exist_ok=True)
    lines = [
        {"sessionId": sid, "cwd": cwd, "timestamp": "2026-09-08T01:00:00.000Z",
         "message": {"role": "user", "content": f"please do the thing {text_extra}"}},
        {"sessionId": sid, "timestamp": "2026-09-08T01:05:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "done."}]}},
    ]
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    old = scap._now_epoch() - 3600
    os.utime(f, (old, old))
    return f


# ── the prose screen ──────────────────────────────────────────────────────────
TOPIC_WORD_TEXTS = [
    # the measured Leak #2 trip tokens, in the ops/founder senses they actually carried
    "Root-cause diagnosis: the WSH wholesale checkout failed because the discount arc reset.",
    "The knowledge-parity arc: assessment of the mirror build, discharge the old watermark, be patient.",
    "Kickoff status APPROVED; restock pending; CLAIMS CHECK on the Mood can copy (F3 marketing claims).",
    "Tommy's claims about the Sprouts appeal were approved and are pending review by Emily.",
    # a Cora code session quoting the PHI regex vocabulary itself
    "Edited phi_guard.py: the pattern lists ssn, date of birth, member id, npi, ddd client, intake form, care plan.",
    "Test fixture in the session: 'AHCCCS' and 'Medicaid' as bare topic words must not trip.",
    "Lexington Services website rebuild: intake form for careers, Book-a-Tour picker, brand kit v1.",
    "Larry's deck is pending; Justin Moran's invoice was approved; the Arc chapter sponsorship.",
    "",
]

VALUE_SHAPED_PHI = [
    "Client Marcus Johnson was diagnosed with autism at intake.",
    "DOB 03/04/2012 recorded on the member file.",
    "The claim references F84.0 for the service month.",
    "AHCCCS ID 84213365 pending re-authorization.",
    "Medicaid number 900123 -- billing hold for client Kayla Reed.",
    "Client Jalen is on risperidone; guardian Rita Hill signed the consent.",
    "Member Sofia Alvarez's authorization for HCBS units at Lexington is pending.",
]


class TestProsePhiRisk:
    @pytest.mark.parametrize("text", TOPIC_WORD_TEXTS)
    def test_topic_words_are_not_phi(self, text):
        assert phi_guard.is_prose_phi_risk(text) is False, text

    @pytest.mark.parametrize("text", VALUE_SHAPED_PHI)
    def test_value_shaped_phi_trips(self, text):
        assert phi_guard.is_prose_phi_risk(text) is True, text

    def test_strict_ingestion_predicate_unchanged(self):
        # the strict screen still trips on the topic words -- ingestion surfaces keep recall
        assert phi_guard.is_phi_risk("Root-cause diagnosis of the checkout arc.") is True
        assert phi_guard.is_phi_risk("the pattern lists ssn and date of birth") is True

    def test_diagnosis_of_ops_prose_is_not_diagnosed_with(self):
        # the shared _DIAGNOSED_WITH_RE matches "diagnosis of/with <anything>" (correct on
        # a LEX chunk); the prose screen uses the strict past-participle framing
        assert phi_guard.is_prose_phi_risk("diagnosis of that 10000 custom order failure") is False
        assert phi_guard.is_prose_phi_risk("diagnosis with reversible watermark") is False
        assert phi_guard.is_prose_phi_risk("she was diagnosed with ADHD last spring") is True

    def test_legs_diagnostic_names_only(self):
        legs = phi_guard.prose_phi_legs("Client Marcus Johnson was diagnosed with autism. DOB 03/04/2012.")
        assert "diagnosed_with" in legs and "dob" in legs and "dx_individual" in legs
        assert phi_guard.prose_phi_legs("plain business prose") == []

    def test_staff_names_are_not_care_recipients(self):
        # a possessive of a rostered staff member next to a dx term is not an individual leak
        assert phi_guard.is_prose_phi_risk(
            "Shaun Hawkins's note mentions autism services capacity", allowed_names={"Shaun Hawkins"}
        ) is False

    def test_never_raises_on_odd_input(self):
        assert phi_guard.is_prose_phi_risk("") is False
        assert phi_guard.is_prose_phi_risk("x" * 200_000) is False


# ── harvester routing ─────────────────────────────────────────────────────────
class TestRouting:
    def test_non_lex_topic_words_file_by_distilled_entity(self, tmp_path):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        # "care plan" + "diagnosis" trip the STRICT regex (the old override) but not the prose screen
        _session_file(projects, "sess-0001-aaaa", text_extra="review the care plan; root-cause diagnosis done")
        res = scap.harvest(lookback_hours=24, dry_run=False, projects_root=projects, founder_os_root=fos,
                           ledger_path=ledger, anthropic_client=_FakeClient(_body("F3E", "shipped")))
        assert len(res) == 1
        r = res[0]
        assert r.entity == "F3E" and r.phi is False and r.quarantined is False
        assert "02-F3-Energy" in str(r.note_path) and "08-Lexington-Services" not in str(r.note_path)
        text = r.note_path.read_text(encoding="utf-8")
        assert "- PHI: yes" not in text and "— F3E —" in text.split("\n", 1)[0]
        row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert row["entity"] == "F3E" and row["phi"] is False and row["quarantined"] is False

    def test_non_lex_value_phi_is_quarantined_never_lex(self, tmp_path):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _session_file(projects, "sess-0002-bbbb", text_extra="client Marcus Johnson was diagnosed with autism")
        kb = SimpleNamespace(upsert_documents=lambda docs: (_ for _ in ()).throw(AssertionError("KB ingest must not run")))
        res = scap.harvest(lookback_hours=24, dry_run=False, projects_root=projects, founder_os_root=fos,
                           ledger_path=ledger, anthropic_client=_FakeClient(_body("F3E", "phi-ish")),
                           with_kb=True, kb=kb)
        r = res[0]
        assert r.quarantined is True and r.phi is True
        assert r.entity == "F3E"                                   # never re-homed
        assert r.note_path is not None and r.note_path.exists()
        assert "08-Lexington-Services" not in str(r.note_path)
        # the quarantine folder lives under the KB-excluded Cora workspace and the
        # filename trips the drive_sweep title belt by construction
        rel = r.note_path.relative_to(fos)
        assert rel.parts[:4] == ("_shared", "projects", "cora", scap.QUARANTINE_DIRNAME)
        assert r.note_path.name.startswith(scap.QUARANTINE_PREFIX)
        assert kb_exclusions.is_cora_internal_path(r.note_path)
        assert kb_exclusions.is_cora_internal_title(r.note_path.name)
        assert kb_exclusions.is_cora_internal_title(r.note_path.name, broad=True)
        text = r.note_path.read_text(encoding="utf-8")
        assert "QUARANTINED" in text and "- PHI: yes (LEX-scoped" not in text
        row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert row["quarantined"] is True and row["entity"] == "F3E" and row["phi"] is True
        assert scap.load_captured_ids(ledger) == {"sess-0002-bbbb"}   # dedup still holds

    def test_lex_distill_keeps_strict_posture(self, tmp_path):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _session_file(projects, "sess-0003-cccc", text_extra="review the care plan")
        res = scap.harvest(lookback_hours=24, dry_run=False, projects_root=projects, founder_os_root=fos,
                           ledger_path=ledger, anthropic_client=_FakeClient(_body("LEX", "lex work")))
        r = res[0]
        assert r.entity == "LEX" and r.phi is True and r.quarantined is False
        assert "08-Lexington-Services" in str(r.note_path)
        assert "- PHI: yes (LEX-scoped, access-controlled)" in r.note_path.read_text(encoding="utf-8")

    def test_lex_sub_entity_distill_stays_lex_folder(self, tmp_path):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _session_file(projects, "sess-0004-dddd", text_extra="nothing clinical here")
        res = scap.harvest(lookback_hours=24, dry_run=False, projects_root=projects, founder_os_root=fos,
                           ledger_path=ledger, anthropic_client=_FakeClient(_body("LEX-LLC", "llc work")))
        r = res[0]
        assert r.entity == "LEX-LLC" and r.phi is False and r.quarantined is False
        assert "08-Lexington-Services" in str(r.note_path)

    def test_route_capture_matrix(self):
        prose_phi = "client Marcus Johnson was diagnosed with autism"
        topic = "root-cause diagnosis of the arc; assessment pending"
        assert scap.route_capture("F3E", topic) == ("F3E", False, False)
        assert scap.route_capture("F3E", prose_phi) == ("F3E", True, True)
        assert scap.route_capture("FNDR", "review the care plan") == ("FNDR", False, False)
        assert scap.route_capture("LEX", "review the care plan") == ("LEX", True, False)
        assert scap.route_capture("LEX-LTS", "nothing clinical") == ("LEX-LTS", False, False)

    def test_dry_run_quarantine_writes_nothing(self, tmp_path):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _session_file(projects, "sess-0005-eeee", text_extra="client Marcus Johnson was diagnosed with autism")
        res = scap.harvest(lookback_hours=24, dry_run=True, projects_root=projects, founder_os_root=fos,
                           ledger_path=ledger, anthropic_client=_FakeClient(_body("F3E")))
        assert res[0].quarantined is True
        assert not fos.exists() or not any(fos.rglob("*.md"))
        assert scap.load_captured_ids(ledger) == set()

    def test_quarantine_path_shape(self, tmp_path):
        from datetime import datetime, timezone
        p = scap.quarantine_note_path_for("F3E", datetime(2026, 9, 8, tzinfo=timezone.utc),
                                          "local_abcdef12-rest", root=tmp_path, surface=scap.SURFACE_COWORK)
        assert p == (tmp_path / "_shared" / "projects" / "cora" / scap.QUARANTINE_DIRNAME / "2026-09"
                     / f"{scap.QUARANTINE_PREFIX}2026-09-08_cowork-session_abcdef12.md")

    def test_source_pin_no_lex_override_left(self):
        src = (_REPO_ROOT / "src" / "cora" / "session_capture.py").read_text(encoding="utf-8")
        assert 'entity = "LEX" if not entity.startswith("LEX") else entity' not in src


# ── the mirror screen fold (cq-e4b0d20a313f) ─────────────────────────────────
class TestMirrorScreenFold:
    @pytest.fixture(scope="class")
    def mirror(self):
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import mirror_claude_workspace as mw  # noqa: PLC0415
        return mw

    def _cfg(self, mirror):
        return SimpleNamespace(personal_families=["capital-raise", "oneamerica"])

    @pytest.mark.parametrize("text", [
        "Skill: cascade. Status APPROVED at the 9/3 lock; promote as written.",
        "f3-rangeme memory: restock pending for the Mood 12-pack; wholesale price approved.",
        "CLAIMS CHECK: the sleep bans hold; Emily is out of claims review.",
        "project-social-ig-fb-tiktok-optimization: pending creator approvals.",
    ])
    def test_business_vocabulary_no_longer_quarantines(self, mirror, text):
        assert mirror.screen_reason(text, self._cfg(mirror)) is None

    def test_real_phi_still_quarantines(self, mirror):
        r = mirror.screen_reason("client Marcus Johnson was diagnosed with autism", self._cfg(mirror))
        assert r is not None and r.startswith("phi")

    def test_lex_token_and_personal_still_quarantine(self, mirror):
        assert mirror.screen_reason("The LEX-LTS census update.", self._cfg(mirror)) == "lex-token"
        assert mirror.screen_reason("capital-raise deck v3", self._cfg(mirror)) == "personal (capital-raise)"


# ── runner + health surfaces ──────────────────────────────────────────────────
class TestQuarantineSurfaces:
    def test_health_check_warns_on_recent_quarantine(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import nightly_health_check as hc  # noqa: PLC0415
        from datetime import datetime, timezone, timedelta
        ledger = tmp_path / "session-captures.jsonl"
        now = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
        rows = [
            {"session_id": "a", "entity": "F3E", "phi": True, "quarantined": True,
             "note_path": "x", "captured_at": (now - timedelta(hours=3)).isoformat()},
            {"session_id": "b", "entity": "FNDR", "phi": False, "quarantined": False,
             "note_path": "y", "captured_at": (now - timedelta(hours=2)).isoformat()},
            {"session_id": "c", "entity": "OSN", "phi": True, "quarantined": True,
             "note_path": "z", "captured_at": (now - timedelta(days=4)).isoformat()},   # outside 26h
        ]
        ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        res = hc.check_session_capture_quarantine(now=now, ledger_path=ledger)
        assert res.status == "warn" and "1 " in res.detail and "quarantine" in res.detail.lower()

    def test_health_check_ok_when_none_recent(self, tmp_path):
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import nightly_health_check as hc  # noqa: PLC0415
        from datetime import datetime, timezone
        ledger = tmp_path / "session-captures.jsonl"
        ledger.write_text(json.dumps({"session_id": "b", "entity": "FNDR", "phi": False,
                                      "note_path": "y", "captured_at": "2026-09-01T00:00:00+00:00"}) + "\n",
                          encoding="utf-8")
        res = hc.check_session_capture_quarantine(now=datetime(2026, 9, 8, tzinfo=timezone.utc), ledger_path=ledger)
        assert res.status == "ok"
        # a missing ledger is an OK, never a crash
        res2 = hc.check_session_capture_quarantine(now=datetime(2026, 9, 8, tzinfo=timezone.utc),
                                                   ledger_path=tmp_path / "nope.jsonl")
        assert res2.status == "ok"

    def test_health_check_is_registered_in_main(self):
        src = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
        assert "all_results.append(check_session_capture_quarantine())" in src

    def test_runner_writes_marker_and_warns(self):
        src = (_REPO_ROOT / "scripts" / "run_session_capture.py").read_text(encoding="utf-8")
        assert "run_marker.write(" in src and "cowork-cora-session-capture" in src
        assert "QUARANTINED" in src

    def test_run_marker_registry_row_present(self):
        import yaml
        data = yaml.safe_load((_REPO_ROOT / "data" / "maps" / "scheduled-task-state.yaml").read_text(encoding="utf-8"))
        names = {r.get("name") for r in (data.get("run_markers") or [])}
        assert "cowork-cora-session-capture" in names
