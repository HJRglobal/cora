"""Code #16 C1 -- the dead-channel archive lane's mode / demotion / acting tier.

The ladder registry's ``channel_archive_mode`` probe imports ``policy`` from script
processes (the nightly health check, the Monday digest), so the module must stay
stdlib-only at import; the tier it reports must never widen on a typo, and a
demotion -- even an unreadable one -- pins the lane at T0.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from cora.channel_archive import policy

_SRC = Path(__file__).resolve().parents[1] / "src" / "cora" / "channel_archive" / "policy.py"


@pytest.fixture
def dem(monkeypatch, tmp_path):
    p = tmp_path / "dem.json"
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_DEMOTION_PATH", str(p))
    return p


def test_default_mode_is_propose_and_only_exact_words_count(monkeypatch):
    monkeypatch.delenv("CORA_CHANNEL_ARCHIVE", raising=False)
    assert policy.mode() == "propose"
    cases = {"act": "act", " ACT ": "act", "off": "off", "Off": "off", "propose": "propose",
             "": "propose", "actt": "propose", "on": "propose", "live": "propose", "1": "propose",
             "act now": "propose"}
    for raw, want in cases.items():
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", raw)
        assert policy.mode() == want, raw


def test_acting_tier_is_t1_only_for_act_and_not_demoted(monkeypatch, dem):
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "propose")
    assert policy.acting_tier() == "T0"
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "off")
    assert policy.acting_tier() == "T0"
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")
    assert policy.acting_tier() == "T1"
    dem.write_text('{"demoted": true, "reason": "unattributed archive C0TEST"}', encoding="utf-8")
    assert policy.is_demoted() and policy.acting_tier() == "T0"
    assert policy.demotion_state()["reason"].startswith("unattributed")


@pytest.mark.parametrize("body", ["{not json", "[1, 2]", '"a string"', ""])
def test_an_unreadable_demotion_file_is_still_a_demotion(monkeypatch, dem, body):
    dem.write_text(body, encoding="utf-8")
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")
    st = policy.demotion_state()
    assert st is not None and st.get("demoted") is True
    assert policy.acting_tier() == "T0"


def test_no_demotion_file_means_not_demoted(dem):
    assert not dem.exists()
    assert policy.demotion_state() is None and policy.is_demoted() is False


def test_default_demotion_path_is_repo_data_state(monkeypatch):
    monkeypatch.delenv("CORA_CHANNEL_ARCHIVE_DEMOTION_PATH", raising=False)
    p = policy.demotion_path()
    assert p.name == "channel-archive-demotion.json"
    assert p.parent.name == "state" and p.parent.parent.name == "data"
    assert (p.parent.parent.parent / "src" / "cora").is_dir()


def test_policy_imports_only_the_stdlib():
    """The probe runs inside the nightly health check / Monday digest processes."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    assert mods <= stdlib, mods - stdlib
