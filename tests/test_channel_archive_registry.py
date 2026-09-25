"""Code #16 C1 -- the dead-channel lane's exemption SOURCES fail closed.

The registry parse must be COMPLETE (nine entity sections, the coverage sentinel,
an id floor that follows the last good parse) or the scan is blind (A9); the deny
policy is read strictly with no cache (A7); the sprawl keep-list duplicate cannot
drift from the script it copies (A2, AST pin); the LEX belt catches the non-prefixed
LEX channels (A6) -- and its membership leg is keyed on the PRIMARY entity, because
Harrison's own roster row lists LEX among `entities`.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from _chanarch_fakes import (HARRISON, LEXSTAFF, PERSON, REGISTRY_FIXTURE, FakeRoles)
from cora.channel_archive import registry as reg

REPO = Path(__file__).resolve().parents[1]
FIXTURE_TEXT = REGISTRY_FIXTURE.read_text(encoding="utf-8")


class TestRegistryParse:
    def test_the_synthetic_fixture_parses_complete(self):
        r = reg.parse_registry(FIXTURE_TEXT)
        assert r.ok, r.reason
        assert r.count >= 100
        assert "C0FXLXPLAIN1" in r.lex_ids and "fx-community-help" in r.lex_names
        assert "C0FXREGQ0001" in r.ids and "fx-registry-quiet" in r.names
        assert "C0FXPROSE01" not in r.ids            # prose, not a table row
        assert not (r.lex_ids & {"C0FXREGQ0001", "C0FXHG0001"})

    def test_decorated_cells_still_yield_the_name(self):
        r = reg.parse_registry(FIXTURE_TEXT)
        assert {"fx-lexsec-03", "fx-bdm-04", "fx-hjrg-02", "fx-osn-05"} <= r.names

    def test_the_default_path_resolves_under_founder_os_root(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CORA_CHANNEL_REGISTRY_PATH", raising=False)
        monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path))
        assert reg.registry_path() == tmp_path / "_shared" / "playbooks" / "slack-channel-registry.md"

    def test_conftest_points_every_test_at_the_fixture(self):
        assert reg.registry_path() == REGISTRY_FIXTURE

    @pytest.mark.parametrize("section", ["## Lexington Services", "## Founder", "## UFL", "## OSN"])
    def test_a_missing_entity_section_is_blind(self, section):
        text = "\n".join(ln for ln in FIXTURE_TEXT.splitlines() if not ln.startswith(section))
        r = reg.parse_registry(text)
        assert not r.ok and "section" in r.reason

    def test_a_missing_coverage_sentinel_is_blind(self):
        text = "\n".join(ln for ln in FIXTURE_TEXT.splitlines() if not ln.startswith("_Coverage"))
        r = reg.parse_registry(text)
        assert not r.ok and "sentinel" in r.reason

    def test_a_read_truncated_inside_the_lex_section_is_blind(self):
        cut = FIXTURE_TEXT.index("#fx-lexsec-07")
        r = reg.parse_registry(FIXTURE_TEXT[:cut])
        assert not r.ok

    def test_the_floor_follows_the_last_good_parse(self):
        assert reg.parse_registry(FIXTURE_TEXT, last_good_count=100).ok
        assert reg.parse_registry(FIXTURE_TEXT, last_good_count=118).ok     # 0.9 * 118 = 107
        bad = reg.parse_registry(FIXTURE_TEXT, last_good_count=200)
        assert not bad.ok and "floor 180" in bad.reason
        assert reg.registry_floor(0) == 100 and reg.registry_floor(140) == 126

    def test_fewer_than_100_ids_is_blind_even_with_every_section(self):
        lines = FIXTURE_TEXT.splitlines()
        kept, dropped = [], 0
        for ln in lines:
            if ln.startswith("| #fx-") and "`C0FX" in ln and dropped < 20:
                dropped += 1
                continue
            kept.append(ln)
        r = reg.parse_registry("\n".join(kept))
        assert not r.ok and "< floor 100" in r.reason

    def test_an_unreadable_file_is_blind(self, tmp_path):
        r = reg.load_registry(path=tmp_path / "missing.md")
        assert not r.ok and r.reason.startswith("registry_unreadable")
        r2 = reg.load_registry(reader=lambda p: (_ for _ in ()).throw(TimeoutError("drive")))
        assert not r2.ok and "TimeoutError" in r2.reason

    def test_load_reads_the_fixture_through_the_bounded_reader(self):
        assert reg.load_registry().ok

    @pytest.mark.parametrize("shape", [
        "|" + " " * 40000 + "x", "| `" + "C" * 40000, "| #" + "a" * 40000,
        "## " + " " * 40000 + "x", " " * 40000 + "x"],
        ids=["pipe-spaces", "tick-C", "hash-a", "header-spaces", "spaces"])
    def test_the_parse_is_linear_on_degenerate_input(self, shape):
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            reg.parse_registry(shape)
            reg._ROW_ID_RE.search(shape)
            reg._ROW_NAME_RE.search(shape)
            best = min(best, time.perf_counter() - t0)
        assert best < 0.05


class TestDenyPolicy:
    def test_the_repo_policy_loads_strictly(self):
        p = reg.load_deny_policy()
        assert p is not None and len(p.ids) >= reg.MIN_DENY_IDS
        assert reg.is_denied(p, "personal-tasks", "")
        assert reg.is_denied(p, "", "C0B7PMLQ26B")
        assert reg.is_denied(p, "lbhs-anything", "")          # glob
        assert not reg.is_denied(p, "fx-hjrg-01", "C0FXHG0001")

    @pytest.mark.parametrize("body", [
        "{not: [valid", "- a\n- b\n", "deny_by_id: []\ndeny_by_name: [x]\n",
        "deny_by_id: [C1, C2]\ndeny_by_name: [x]\n",           # fewer than 10 ids
        "deny_by_name: [x]\n", ""])
    def test_any_problem_reads_as_none(self, tmp_path, body):
        f = tmp_path / "policy.yaml"
        f.write_text(body, encoding="utf-8")
        assert reg.load_deny_policy(f) is None

    def test_a_corrupt_edit_after_a_good_load_is_not_masked_by_a_cache(self, tmp_path):
        f = tmp_path / "policy.yaml"
        f.write_text(reg.DENY_POLICY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        assert reg.load_deny_policy(f) is not None
        f.write_text("deny_by_id: [broken", encoding="utf-8")
        assert reg.load_deny_policy(f) is None

    def test_the_lane_never_uses_the_fail_open_cached_loader(self):
        for mod in ("registry", "classify", "scan", "deliver"):
            src = (REPO / "src" / "cora" / "channel_archive" / f"{mod}.py").read_text(encoding="utf-8")
            tree = ast.parse(src)
            names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            mods = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names}
            assert "slack_sweep_policy" not in mods and "slack_sweep_policy" not in names, mod


def _script_sets() -> dict:
    tree = ast.parse((REPO / "scripts" / "archive_sprawl_channels.py").read_text(encoding="utf-8"))
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("KEEP_IDS", "KEEP_FINANCE", "KEEP_EXPLICIT", "_OSN_STORE_PREFIXES"):
                out[name] = {elt.value for elt in node.value.elts}
        if isinstance(node, ast.FunctionDef) and node.name == "keep_reason":
            out["startswith"] = {c.args[0].value for c in ast.walk(node)
                                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                                 and c.func.attr == "startswith" and c.args
                                 and isinstance(c.args[0], ast.Constant)}
    return out


class TestKeepListDuplicate:
    def test_the_duplicate_cannot_drift_from_the_sprawl_script(self):
        s = _script_sets()
        assert s["KEEP_IDS"] == set(reg.KEEP_IDS)
        assert s["KEEP_FINANCE"] == set(reg.KEEP_FINANCE)
        assert s["KEEP_EXPLICIT"] == set(reg.KEEP_EXPLICIT)
        assert s["_OSN_STORE_PREFIXES"] == set(reg.OSN_STORE_PREFIXES)
        assert "cora-" in s["startswith"] and set(reg.KEEP_PREFIXES) == {"cora-"}

    def test_the_lane_never_imports_the_script(self):
        for f in (REPO / "src" / "cora" / "channel_archive").glob("*.py"):
            assert "archive_sprawl_channels" not in {
                a.name for n in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names} | {
                n.module for n in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                if isinstance(n, ast.ImportFrom) and n.module}, f.name

    @pytest.mark.parametrize("cid,name,hit", [
        ("C0BBUMAU4KG", "f3-bdm", True), ("C0X1", "founder-operations", True),
        ("C0X1", "cowork-daily-briefs", True), ("C0X1", "cora-anything", True),
        ("C0X1", "osngw-store-ops", True), ("C0X1", "fx-plain", False)])
    def test_keep_list_reasons(self, cid, name, hit):
        assert bool(reg.keep_list_reason(cid, name)) is hit


class TestHardcodedPins:
    def test_blocked_general_id_equals_the_app_constant(self):
        import cora.app as app_module
        assert reg.BLOCKED_CHANNEL_IDS == app_module._BLOCKED_CHANNEL_IDS

    def test_info_for_cora_id_equals_the_intake_constant(self):
        from cora import info_intake
        assert reg.INFO_FOR_CORA_CHANNEL_ID == info_intake.CHANNEL_ID

    @pytest.mark.parametrize("name,hit", [
        ("ops-finance", True), ("a-b-finance", True), ("f3e-leadership", True),
        ("founder-finance", True), ("finance-talk", False), ("leadership-offsite", False)])
    def test_leadership_finance_is_a_suffix_rule(self, name, hit):
        assert reg.is_leadership_or_finance(name) is hit


class TestLexBelt:
    R = reg.parse_registry(FIXTURE_TEXT)

    @pytest.mark.parametrize("name", [
        "lex-anything", "llc-ops", "lts", "lbhs-x", "lla-y", "lexington-monthly-check-in",
        "open-tucson-dta-location", "fx-ddd-contract", "hcbs_billing", "copa-notes",
        "cora-kq-lex"])
    def test_name_belts(self, name):
        assert reg.lex_by_name("C0NOTREG01", name, self.R)

    def test_the_registry_lex_section_catches_a_plain_name(self):
        assert reg.lex_by_name("C0FXLXPLAIN1", "fx-community-help", self.R)
        assert reg.lex_by_name("C0SOMEID01", "fx-community-help", self.R)

    @pytest.mark.parametrize("name", ["fx-plain-ops", "product-dev", "f3e-social", "copacabana"])
    def test_non_lex_names(self, name):
        assert not reg.lex_by_name("C0NOTREG01", name, self.R)

    def test_membership_belt_keys_on_the_primary_entity_only(self):
        roles = FakeRoles()
        # Harrison's `entities` include LEX in the live roster; primary is FNDR -> not LEX
        assert not reg.lex_by_members([HARRISON, PERSON], roles=roles)
        assert reg.lex_by_members([HARRISON, LEXSTAFF], roles=roles)
        assert reg.lex_by_members(None, roles=roles)            # unreadable -> LEX

    def test_the_real_roster_never_makes_harrisons_presence_lex(self):
        """The premise behind the primary-entity key (measured 2026-09-25: his roster row
        lists LEX among `entities`). Whatever that list says later, Harrison being in a
        channel must never make it LEX, or no channel could ever be proposed."""
        from cora import org_roles
        rec = org_roles.get_role(HARRISON)
        assert rec is not None
        assert not reg.lex_by_members([HARRISON])


def test_name_fp_is_stable_and_case_insensitive():
    assert reg.name_fp("Old-Promo") == reg.name_fp("old-promo") and len(reg.name_fp("x")) == 12
