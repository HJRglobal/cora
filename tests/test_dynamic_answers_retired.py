"""Regression anchors for the retirement of the manual dynamic-answer seeds (D-085).

The four never-refreshed manual dynamic-answer seeds (FNDR cash position, F3E sales
pipeline, F3 Pure launch, OSN monthly financials) were retired 2026-07-18 -- they had
no auto-refresh writer and served the stale yaml fallback for ~60 days. They are
SUPERSEDED by live tools (financial_get_cashflow / f3e_hubspot_pipeline_summary /
osn_financial_pulse). Decision: retire, do not rebuild (Harrison, 2026-07-18; D-085,
closing the D-084 FNDR context-budget arc).

These tests pin the two invariants the retirement must not break:
  1. The live-tool answer path still exists + is exposed to the right channels --
     retiring static context must not remove the ONLY way Cora answers cash /
     pipeline questions, nor weaken finance gating.
  2. The dynamic_answers MECHANISM stays intact + dormant -- a future properly-fed
     seed still renders -- so the retirement is reversible.
"""

import pytest

import cora.context_loader as cl
import cora.dynamic_answers as da
import cora.tools.tool_dispatch as td


@pytest.fixture(autouse=True)
def _reset_dynamic_cache():
    da._cache.clear()
    yield
    da._cache.clear()


def _names(entity: str, cross_entity: bool = False) -> set[str]:
    return {t["name"] for t in td.tools_for_entity(entity, cross_entity)}


# ---------------------------------------------------------------------------
# 1. The live-tool answer path survives the retirement
# ---------------------------------------------------------------------------

def test_finance_pipeline_tools_still_defined():
    """The tools that superseded the retired seeds must remain defined."""
    defined = {t["name"] for t in td.TOOL_DEFINITIONS}
    assert "financial_get_cashflow" in defined
    assert "f3e_hubspot_pipeline_summary" in defined
    assert "osn_financial_pulse" in defined


def test_founder_cash_and_pipeline_answer_path_intact(monkeypatch):
    """FNDR/HJRG (the retired seeds' audience) still reach the live cash + pipeline
    tools; F3E/OSN still reach their own. This is the answer path the retired static
    snapshots were pre-tool scaffolding for."""
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    for agg in ("FNDR", "HJRG"):
        n = _names(agg)
        assert "financial_get_cashflow" in n, agg
        assert "f3e_hubspot_pipeline_summary" in n, agg
        assert "osn_financial_pulse" in n, agg
    f3e = _names("F3E")
    assert "financial_get_cashflow" in f3e
    assert "f3e_hubspot_pipeline_summary" in f3e
    osn = _names("OSN")
    assert "financial_get_cashflow" in osn
    assert "osn_financial_pulse" in osn


def test_finance_gating_language_intact():
    """Retiring static context must not open a finance path: the cash tool stays
    TIER_1/FNDR-scoped and refuses TIER_3."""
    spec = next(t for t in td.TOOL_DEFINITIONS if t["name"] == "financial_get_cashflow")
    desc = spec["description"]
    assert "TIER_1" in desc and "FNDR" in desc
    assert "TIER_3" in desc  # "NEVER call this tool in TIER_3 ... financial guardrail applies."


# ---------------------------------------------------------------------------
# 2. The mechanism is dormant but reversible
# ---------------------------------------------------------------------------

def test_scoped_dynamic_answers_empty_with_no_seeds(monkeypatch, tmp_path):
    """With existing-but-empty dynamic dirs (the post-retirement state), the context
    loader returns no dynamic block -- no throw, no leak -- for both the FNDR
    aggregator and a single entity."""
    root = tmp_path / "design" / "known-answers" / "dynamic"
    for e in ("FNDR", "F3E", "OSN"):
        (root / e).mkdir(parents=True)
    monkeypatch.setattr(da, "_DYNAMIC_DIR", root)
    monkeypatch.setattr(da, "_REPO_ROOT", tmp_path)
    assert cl._load_scoped_dynamic_answers("FNDR") == ""
    assert cl._load_scoped_dynamic_answers("F3E") == ""


def test_mechanism_reversible_synthetic_seed_renders(monkeypatch, tmp_path):
    """Dropping a synthetic, fresh seed still renders through the context-loader
    path -- proving the dynamic_answers mechanism is intact and the retirement is
    reversible (a future, properly-fed seed just works)."""
    root = tmp_path / "design" / "known-answers" / "dynamic"
    (root / "F3E").mkdir(parents=True)
    snap = tmp_path / "data" / "snapshots" / "f3e" / "live.yaml"
    snap.parent.mkdir(parents=True)
    snap.write_text("status: healthy\nowner: Tommy\n", encoding="utf-8")
    (root / "F3E" / "live.yaml").write_text(
        "topic: T\n"
        'template: "Pipeline is {status}, owned by {owner}."\n'
        'fallback: "unavailable"\n'
        "snapshot_path: data/snapshots/f3e/live.yaml\n"
        "source:\n  staleness_threshold_hours: 336\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(da, "_DYNAMIC_DIR", root)
    monkeypatch.setattr(da, "_REPO_ROOT", tmp_path)
    out = cl._load_scoped_dynamic_answers("F3E")
    assert "Pipeline is healthy, owned by Tommy." in out
    # And the entity-scope firewall still holds: OSN sees nothing of F3E's seed.
    assert cl._load_scoped_dynamic_answers("OSN") == ""
