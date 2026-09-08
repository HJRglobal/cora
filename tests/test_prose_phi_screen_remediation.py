"""D-051 lens-A remediation of the value-shaped prose PHI screen (ingest-integrity
I2, 2026-09-08): the legs the review found missing, the default staff roster,
the governed-name stoplist, the harvester's entity aliases, the batch exclusion
on BOTH screens, the mirror screen's clinical leg, and the quarantine-folder
isolation probe.

Every positive here is synthetic; the negatives are the shapes a Cora / ops
session actually writes (staff possessives, Cora's own product copy, Title-case
common nouns after a care noun).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import phi_guard  # noqa: E402
from cora import session_capture as scap  # noqa: E402
from cora.knowledge_base import schema  # noqa: E402

STAFF = {"Justin Moran", "Harrison Rogers", "Tommy Tucker", "Shaun Riley"}


class TestNewLegs:
    @pytest.mark.parametrize("text,leg", [
        ("Intake sheet: SSN 123-45-6789 on file", "ssn"),
        ("social security number: 123456789 verified", "ssn"),
        ("the field held 987-65-4321 in clear text", "ssn"),
        ("Member ID 84213365 approved for 20 hrs/wk", "person_id"),
        ("client #: 90012345 placement pending", "person_id"),
        ("Client: Marcus Johnson has autism per the intake", "dx_individual"),            # the colon shape
        ("Client:Marcus Johnson -- risperidone 2mg", "dx_individual"),
        ("Marcus is on risperidone since March", "dx_individual"),                        # first-name subject
        ("Sofia was diagnosed with ADHD in 2024", "dx_individual"),
        ("Jalen takes melatonin at bedtime per the BSP", "dx_individual"),
        ("Bob Smith's authorization is pending with DDD; Lexington billing hold", "billing_individual"),  # non-staff possessive
    ])
    def test_positive(self, text, leg):
        legs = phi_guard.prose_phi_legs(text, STAFF)
        assert leg in legs, (text, legs)
        assert phi_guard.is_prose_phi_risk(text, STAFF) is True

    @pytest.mark.parametrize("text", [
        # staff possessives on the billing leg are spared
        "Justin Moran's authorization for the Lexington billing run is pending review",
        "Harrison's approval on the DDD claims batch came through; Lexington is paid",
        # Cora's own product copy: the roster spares her (default roster path below)
        "Cora's melatonin recommendation for the F3E Recovery SKU: 3mg per can",
        # Title-case common nouns after a care noun are not names
        "Client Portal rollout; Member Services team; Parent Company memo; Individual Support Plan template",
        "Participant Directed care model; Client Onboarding form; Member Experience survey",
        "recipient Custodian on the Chase wire (EBITDA activity)",
        # sentence-openers / pronouns with a clinical predicate are not names
        "She is on the waitlist; This was diagnosed as a checkout bug; Cora is on the Sonnet tier",
        # transcript possessives that are contractions / brands / verb-led fragments never trip the billing leg
        "It's Lexington billing week; Here's Walmart's claims sheet; On Harrison's authorization we proceed; Today's eligibility run",
        "Review Trent's authorization draft for the Lexington claims process; Paste Trent's notes",
        # a bare topic word never trips
        "root-cause diagnosis of the arc; the assessment is pending; be patient with the discharge queue",
        "AHCCCS rate changes for 2026; DDD manual section 1240; SSN column added to the schema (no values)",
        # phone / order shapes are not SSNs
        "call 480-555-1234; order 123-456-7890; invoice 2026-09-08",
        "",
    ])
    def test_negative(self, text):
        assert phi_guard.prose_phi_legs(text, STAFF) == [], text

    def test_default_roster_comes_from_org_roles_plus_cora(self, monkeypatch):
        from cora import org_roles
        phi_guard._STAFF_ROSTER_CACHE.update({"at": 0.0, "names": frozenset()})
        monkeypatch.setattr(org_roles, "all_roles", lambda: [SimpleNamespace(name="Marcus Johnson"), SimpleNamespace(name="Tommy Tucker")])
        names = phi_guard._default_allowed_names()
        assert {"Marcus Johnson", "Tommy Tucker", "Cora", "Claude", "Harrison Rogers"} <= names
        # with the default roster, a ROSTERED Marcus is spared ...
        assert phi_guard.prose_phi_legs("Client: Marcus Johnson has autism per the intake") == []
        # ... and Cora's own med mention never reads as a care recipient
        assert phi_guard.prose_phi_legs("Cora's melatonin recommendation for the F3E Recovery SKU") == []
        phi_guard._STAFF_ROSTER_CACHE.update({"at": 0.0, "names": frozenset()})
        # fail-soft: a broken registry still spares the fixed names and quarantines the rest
        monkeypatch.setattr(org_roles, "all_roles", lambda: (_ for _ in ()).throw(RuntimeError("yaml")))
        names = phi_guard._default_allowed_names()
        assert names == set(phi_guard._ALWAYS_ALLOWED_NAMES)
        assert "dx_individual" in phi_guard.prose_phi_legs("Client: Marcus Johnson has autism per the intake")
        phi_guard._STAFF_ROSTER_CACHE.update({"at": 0.0, "names": frozenset()})

    def test_live_roster_spares_real_staff(self):
        phi_guard._STAFF_ROSTER_CACHE.update({"at": 0.0, "names": frozenset()})
        names = phi_guard._default_allowed_names()
        assert "Cora" in names and len(names) >= 4          # org_roles.yaml + the fixed names
        assert phi_guard.is_prose_phi_risk("Justin Moran's authorization for the Lexington billing run is pending") is False

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(phi_guard, "_SSN_VALUE_RE", SimpleNamespace(search=lambda t: (_ for _ in ()).throw(RuntimeError("x"))))
        assert phi_guard.prose_phi_legs("anything") == ["error"]
        assert phi_guard.is_prose_phi_risk("anything") is True     # fail closed


# ── the harvester ─────────────────────────────────────────────────────────────
class TestHarvester:
    @pytest.mark.parametrize("raw,expect", [
        ("LEXINGTON", "LEX"), ("Lexington Services", "LEX"), ("F3 Energy", "F3E"), ("f3", "F3E"),
        ("Founder", "FNDR"), ("HJR Global", "HJRG"), ("osn", "OSN"), ("LEX-LLC", "LEX-LLC"), ("MARS", "MARS"),
    ])
    def test_normalize_entity(self, raw, expect):
        assert scap.normalize_entity(raw) == expect

    def test_parse_distilled_folds_aliases_and_falls_back(self):
        body = {"topic": "t", "decisions": [], "facts": [], "action_items": [], "open_questions": []}
        assert scap._parse_distilled(json.dumps({"entity": "LEXINGTON", **body}), "FNDR")["entity"] == "LEX"
        assert scap._parse_distilled(json.dumps({"entity": "F3 Energy", **body}), "FNDR")["entity"] == "F3E"
        assert scap._parse_distilled(json.dumps({"entity": "MARS", **body}), "FNDR")["entity"] == "FNDR"

    def test_batch_excludes_both_screens(self, monkeypatch):
        from cora import batch_client
        seen: dict = {}
        monkeypatch.setattr(batch_client, "batch_enabled", lambda flag: True)

        def fake_generate(requests, caller, deadline_s):
            seen["n"] = len(requests)
            return {}
        monkeypatch.setattr(batch_client, "batch_generate", fake_generate)
        mk = lambda text: SimpleNamespace(text=text, cwd=r"C:\Users\Harri\code\cora", session_id="s")  # noqa: E731
        pending = [
            (mk("ordinary launch notes about the Sprouts appeal"), "code", "k1"),
            (mk("review the care plan and the diagnosis notes"), "code", "k2"),          # strict screen
            (mk("Client: Marcus Johnson has autism per the intake"), "code", "k3"),      # prose screen only
        ]
        out = scap._batch_distill(pending)
        assert seen["n"] == 1                       # only the clean transcript rode the batch
        assert out == {"k1": None}


# ── the mirror screen ─────────────────────────────────────────────────────────
def _load_mirror():
    path = _REPO_ROOT / "scripts" / "mirror_claude_workspace.py"
    spec = importlib.util.spec_from_file_location("mirror_claude_workspace_a", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_mirror_screen_takes_the_clinical_leg_too():
    m = _load_mirror()
    cfg = SimpleNamespace(personal_families=[])
    assert m.screen_reason("the member has autism and takes risperidone", cfg) == "phi (clinical-term)"
    assert m.screen_reason("Client: Marcus Johnson has autism", cfg).startswith("phi (dx_individual")
    assert m.screen_reason("status APPROVED; restock pending; CLAIMS CHECK", cfg) is None     # the cq-e4b0d20a313f fold holds
    assert m.screen_reason("root-cause diagnosis of the arc", cfg) is None                    # the ops sense stays clean
    assert m.screen_reason("F3 Recovery: 3mg melatonin per can, launch copy", cfg) is None    # product copy is not clinical


# ── the quarantine-folder isolation probe ────────────────────────────────────
def _load_health():
    path = _REPO_ROOT / "scripts" / "nightly_health_check.py"
    spec = importlib.util.spec_from_file_location("nightly_health_check_a", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_quarantine_folder_isolation_probe(tmp_path):
    h = _load_health()
    db = tmp_path / "kb.db"
    conn = schema.connect(db)
    schema.init_schema(conn)
    conn.execute("INSERT INTO knowledge_chunks (chunk_id, source, source_id, entity, title, content, ingested_at) "
                 "VALUES ('a', 'static_md', '02-F3-Energy/_session-captures/2026-09/x.md', 'F3E', 'ok', 'c', 1)")
    conn.commit()
    conn.close()
    res = h.check_quarantine_folder_isolation(kb_path=db, founder_os_root=tmp_path)
    assert res.status == "ok" and "path-excluded" in res.detail
    conn = schema.connect(db)
    conn.execute("INSERT INTO knowledge_chunks (chunk_id, source, source_id, entity, title, content, ingested_at) "
                 "VALUES ('b', 'static_md', '_shared/projects/cora/_session-capture-quarantine/2026-09/cora-quarantine-x.md', "
                 "'F3E', 'Session capture', 'c', 1)")
    conn.commit()
    conn.close()
    res = h.check_quarantine_folder_isolation(kb_path=db, founder_os_root=tmp_path)
    assert res.status == "warn" and "1 KB row(s) carry a quarantine path" in res.detail
    # registered in the nightly run
    src = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    assert "all_results.append(check_quarantine_folder_isolation())" in src
