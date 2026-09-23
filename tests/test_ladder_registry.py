"""Autonomy-ladder registry (Code #13 slice 7, cq-6afa86210ba0).

Contract under test:
  * the REAL data/ladder-registry.yaml loads, validates clean, rows every
    KNOWN_LANES lane exactly once, every tier/status is in the enum, no row above
    T1 lacks a failing-capable monitor, every row is pending-Harrison at seed;
  * validate() names each schema violation on a tmp registry (missing key, bad
    tier, T2 without a failing-capable monitor, T2 without an audit surface, a lane
    not in KNOWN_LANES, a KNOWN lane with no row, malformed events, a tier above T0
    with no evidenced event);
  * acting_drift() reports a lane whose live probe reads ABOVE its registered tier,
    reports an unreadable probe (blind never renders clean), and stays quiet when
    the observed tier is at/below the registered one;
  * render_markdown() is byte-stable for equal input, sorted by lane, and says
    UNAVAILABLE for a missing file;
  * cora.capability_set reads explicit capability_terms from the registry;
  * a missing/unparseable file is {available: False, reason} -- never a crash and
    never an empty-but-OK registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cora import capability_set as cs
from cora import ladder_registry as lr

_REPO = Path(__file__).resolve().parents[1]
_REAL = _REPO / "data" / "ladder-registry.yaml"


def _row(**over):
    base = {
        "lane": "s2-phantom-write-screen", "title": "t", "tier": "T0", "status": "live",
        "promotion_criteria": "p", "demotion_triggers": "d",
        "evidence_monitor": {"description": "m", "failing_capable": True},
        "audit_surface": "a", "authority": "Harrison", "confirmed_by": "pending-Harrison",
        "events": [{"ts": "2026-09-19", "event": "seeded", "by": "code-13", "evidence": "e"}],
    }
    base.update(over)
    return base


def _write(tmp_path: Path, rows) -> Path:
    p = tmp_path / "ladder-registry.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "lanes": rows}, sort_keys=False), encoding="utf-8")
    return p


# ── the shipped file ──────────────────────────────────────────────────────────

class TestShippedRegistry:
    def test_loads_and_validates_clean(self):
        reg = lr.load(_REAL)
        assert reg["available"] is True
        assert lr.validate(reg) == []

    def test_every_known_lane_rowed_exactly_once_and_vice_versa(self):
        reg = lr.load(_REAL)
        ids = [r["lane"] for r in lr.lanes(reg)]
        assert sorted(ids) == sorted(set(ids))
        assert set(ids) == set(lr.KNOWN_LANES)

    def test_tiers_statuses_and_monitors_are_honest(self):
        reg = lr.load(_REAL)
        for r in lr.lanes(reg):
            assert r["tier"] in lr.TIERS, r["lane"]
            assert r["status"] in lr.STATUSES, r["lane"]
            if lr.TIER_RANK[r["tier"]] > 1:
                assert r["evidence_monitor"]["failing_capable"] is True, r["lane"]
                assert r["audit_surface"].strip(), r["lane"]

    def test_every_row_is_pending_or_carries_harrisons_confirm_event(self):
        """At seed every row was `pending-Harrison`; his ONE batch confirm (RULED
        2026-09-19, applied 2026-09-20) flips `confirmed_by` AND appends a `confirmed`
        event by Harrison. A row may be in either state -- never confirmed_by without
        the event, never the event without confirmed_by (the two must agree)."""
        reg = lr.load(_REAL)
        pending = set(lr.pending_confirmation(reg))
        for r in lr.lanes(reg):
            confirmed_events = [e for e in r["events"]
                                if e.get("event") == "confirmed" and str(e.get("by", "")).startswith("Harrison")]
            if r["lane"] in pending:
                assert not confirmed_events, r["lane"]
            else:
                assert str(r["confirmed_by"]).startswith("Harrison"), r["lane"]
                assert confirmed_events and all(e.get("evidence") for e in confirmed_events), r["lane"]

    def test_bespoke_rows_carried_verbatim(self):
        """The Code #12 / ingest-report / mirror rows keep their VALUES (the key
        `demotion` became `demotion_triggers`)."""
        reg = lr.load(_REAL)
        s2 = lr.row_for("s2-phantom-write-screen", reg)
        assert "any false-positive correction in enforce -> back to observe (flag)" == s2["demotion_triggers"]
        assert "tests/test_phantom_write_claim.py replays the incident" in s2["evidence_monitor"]["description"]
        mirror = lr.row_for("claude-workspace-mirror", reg)
        assert mirror["promotion_criteria"].startswith("none sought -- read-only sources")
        inv = lr.row_for("cora-self-inventory", reg)
        assert "no writes, no egress, no LLM" in inv["promotion_criteria"]

    def test_lifecycle_is_a_status_not_a_tier(self):
        reg = lr.load(_REAL)
        assert lr.row_for("revops-send", reg)["status"] == "dark"
        # BUILT 2026-09-23 (DR/VM step-1 M1): the lane flipped unbuilt -> live WITH a note event
        assert lr.row_for("task-estate-manifest", reg)["status"] == "live"
        assert lr.row_for("task-estate-manifest", reg)["evidence_monitor"]["failing_capable"] is True
        assert lr.row_for("email-triage-tier1", reg)["status"] == "cowork-estate"
        assert lr.row_for("f3e-blog-one-tap", reg)["tier"] == "CAP-T1"

    def test_probes_named_on_rows_exist(self):
        reg = lr.load(_REAL)
        for r in lr.lanes(reg):
            if r.get("acting_probe"):
                assert r["acting_probe"] in lr.PROBES, r["lane"]

    def test_capability_terms_flow_into_the_honesty_rail(self, monkeypatch):
        monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", _REAL)
        terms = cs.capability_terms("FNDR", cross_entity=True, founder=True)
        assert "recap card" in terms and "meeting recap" in terms
        assert "send recap" in terms["recap card"]


# ── validate() on tmp registries ──────────────────────────────────────────────

class TestValidate:
    def _all_rows(self):
        rows = []
        for lane in lr.KNOWN_LANES:
            rows.append(_row(lane=lane))
        return rows

    def test_clean_tmp_registry(self, tmp_path):
        reg = lr.load(_write(tmp_path, self._all_rows()))
        assert lr.validate(reg) == []

    def test_missing_key_and_bad_enums_are_named(self, tmp_path):
        rows = self._all_rows()
        rows[0].pop("audit_surface")
        rows[1]["tier"] = "T9"
        rows[2]["status"] = "sleeping"
        probs = lr.validate(lr.load(_write(tmp_path, rows)))
        assert any("missing `audit_surface`" in p for p in probs)
        assert any("tier 'T9'" in p for p in probs)
        assert any("status 'sleeping'" in p for p in probs)

    def test_t2_without_failing_capable_monitor_or_audit_surface_is_a_violation(self, tmp_path):
        rows = self._all_rows()
        rows[0]["tier"] = "T2"
        rows[0]["evidence_monitor"] = {"description": "m", "failing_capable": False}
        rows[1]["tier"] = "T2"
        rows[1]["audit_surface"] = ""
        probs = lr.validate(lr.load(_write(tmp_path, rows)))
        assert any("without a failing-capable evidence_monitor" in p for p in probs)
        assert any("without an audit_surface" in p or "missing `audit_surface`" in p for p in probs)

    def test_unknown_lane_and_missing_known_lane(self, tmp_path):
        rows = self._all_rows()
        rows[0]["lane"] = "mystery-lane"
        probs = lr.validate(lr.load(_write(tmp_path, rows)))
        assert any("mystery-lane: not in KNOWN_LANES" in p for p in probs)
        assert any(f"{lr.KNOWN_LANES[0]}: KNOWN lane has no registry row" in p for p in probs)

    def test_events_must_be_evidenced_for_a_tier_above_t0(self, tmp_path):
        rows = self._all_rows()
        rows[0]["tier"] = "T1"
        rows[0]["events"] = [{"ts": "2026-09-19", "event": "note", "by": "x", "evidence": ""}]
        rows[1]["events"] = []
        rows[2]["events"] = [{"event": "bogus"}]
        probs = lr.validate(lr.load(_write(tmp_path, rows)))
        assert any("no seeded/promoted/confirmed event carrying evidence" in p for p in probs)
        assert any("`events` must be a non-empty list" in p for p in probs)
        assert any("malformed event" in p for p in probs)

    def test_unknown_probe_and_bad_capability_terms(self, tmp_path):
        rows = self._all_rows()
        rows[0]["acting_probe"] = "nope"
        rows[1]["capability_terms"] = "recap card"
        probs = lr.validate(lr.load(_write(tmp_path, rows)))
        assert any("acting_probe 'nope'" in p for p in probs)
        assert any("capability_terms must be a list" in p for p in probs)

    def test_missing_and_unparseable_files_are_unavailable_not_empty_ok(self, tmp_path):
        reg = lr.load(tmp_path / "nope.yaml")
        assert reg["available"] is False and "missing" in reg["reason"]
        assert lr.validate(reg) == [reg["reason"]]
        bad = tmp_path / "bad.yaml"
        bad.write_text("lanes: [::", encoding="utf-8")
        reg2 = lr.load(bad)
        assert reg2["available"] is False and "unreadable" in reg2["reason"]
        assert lr.summary(reg2)["available"] is False

    # ── D-051 EF-8: the tier history is CHECKED against `tier` ──────────────

    def _seeded(self, lane, tier, **over):
        row = _row(lane=lane, tier=tier,
                   events=[{"ts": "2026-09-19", "event": "seeded", "tier": "T0",
                            "by": "code-13", "evidence": "e"}])
        if tier != "T0":
            row["evidence_monitor"] = {"description": "m", "failing_capable": True}
        row.update(over)
        return row

    def test_a_raised_tier_with_no_event_is_named(self, tmp_path):
        """The review's first failing input: s2-phantom-write-screen T0 -> T3 by
        hand, no new event -> validate() was []. And the consequence it hid: the
        enforce-mode acting drift on that lane disappeared."""
        rows = [self._seeded(l, "T0") for l in lr.KNOWN_LANES]
        rows[0]["tier"] = "T3"
        rows[0]["acting_probe"] = "sentinel_mode"
        reg = lr.load(_write(tmp_path, rows))
        probs = lr.validate(reg)
        assert any(f"{lr.KNOWN_LANES[0]}: tier T3 but the last tier-bearing event says T0" in p
                   for p in probs), probs
        # the drift WARN is silenced by the hand edit -- which is why validate must shout
        assert lr.acting_drift(reg, {"sentinel_mode": lambda: "T2"}) == []
        assert lr.summary(reg, probes={"sentinel_mode": lambda: "T2"})["schema_problems"]

    def test_a_promotion_that_skips_a_tier_is_named(self, tmp_path):
        rows = [self._seeded(l, "T0") for l in lr.KNOWN_LANES]
        rows[1]["tier"] = "T2"
        rows[1]["events"].append({"ts": "2026-09-20", "event": "promoted", "tier": "T2",
                                  "by": "Harrison", "evidence": "tap"})
        probs = lr.validate(lr.load(_write(tmp_path, rows)))
        assert any("`promoted` T0 -> T2 is not exactly one rank" in p for p in probs), probs

    def test_a_demotion_that_does_not_lower_or_says_nothing_is_named(self, tmp_path):
        rows = [self._seeded(l, "T0") for l in lr.KNOWN_LANES]
        rows[2]["tier"] = "T2"
        rows[2]["events"] = [{"ts": "2026-09-19", "event": "seeded", "tier": "T2",
                              "by": "code-13", "evidence": "e"},
                             {"ts": "2026-09-20", "event": "demoted", "tier": "T2",
                              "by": "auto", "evidence": "class WARN"}]
        rows[3]["tier"] = "T2"
        rows[3]["events"] = [{"ts": "2026-09-19", "event": "seeded", "tier": "T2",
                              "by": "code-13", "evidence": "e"},
                             {"ts": "2026-09-20", "event": "demoted", "by": "auto",
                              "evidence": "class WARN"}]       # the review's third input
        probs = lr.validate(lr.load(_write(tmp_path, rows)))
        assert any("`demoted` T2 -> T2 does not lower the tier" in p for p in probs), probs
        assert any("`demoted` event without a `tier`" in p for p in probs), probs

    def test_a_legal_promotion_and_demotion_history_is_clean(self, tmp_path):
        rows = [self._seeded(l, "T0") for l in lr.KNOWN_LANES]
        rows[0]["tier"] = "T1"
        rows[0]["events"].append({"ts": "2026-09-20", "event": "promoted", "tier": "T1",
                                  "by": "Harrison", "evidence": "7 clean days"})
        rows[0]["events"].append({"ts": "2026-09-21", "event": "confirmed", "by": "Harrison",
                                  "evidence": "batch"})
        rows[1]["tier"] = "T1"
        rows[1]["events"] = [{"ts": "2026-09-19", "event": "seeded", "tier": "T2",
                              "by": "code-13", "evidence": "e"},
                             {"ts": "2026-09-20", "event": "demoted", "tier": "T1",
                              "by": "auto", "evidence": "class WARN"}]
        rows[1]["evidence_monitor"] = {"description": "m", "failing_capable": True}
        rows[2]["tier"] = "CAP-T1"
        rows[2]["events"].append({"ts": "2026-09-20", "event": "promoted", "tier": "CAP-T1",
                                  "by": "Harrison", "evidence": "charter line 23"})
        assert lr.validate(lr.load(_write(tmp_path, rows))) == []

    def test_a_tier_on_a_note_or_confirmed_event_and_a_bad_event_tier_are_named(self, tmp_path):
        rows = [self._seeded(l, "T0") for l in lr.KNOWN_LANES]
        rows[0]["events"].append({"ts": "2026-09-20", "event": "note", "tier": "T1",
                                  "by": "x", "evidence": "e"})
        rows[1]["events"][0]["tier"] = "T9"
        probs = lr.validate(lr.load(_write(tmp_path, rows)))
        assert any("`note` event must not carry `tier`" in p for p in probs), probs
        assert any("event tier 'T9'" in p for p in probs), probs

    def test_the_shipped_seeded_events_carry_their_row_tier(self):
        """Rule (a) is live from day one: every seeded event in the real file
        says the tier its row holds, so a hand-edited `tier` now fails validate."""
        reg = lr.load(_REAL)
        for r in lr.lanes(reg):
            seeded = [e for e in r["events"] if e.get("event") == "seeded"]
            assert seeded and all(e.get("tier") for e in seeded), r["lane"]
            # the LAST tier-bearing event is the row's tier (a ruled demotion after the
            # seed -- nightly-catchup T2 -> T1, 2026-09-19 -- moves the row WITH an event)
            tiered = [e for e in r["events"] if e.get("tier")]
            assert tiered[-1]["tier"] == r["tier"], r["lane"]
            if len(tiered) == 1:
                assert seeded[0].get("tier") == r["tier"], r["lane"]
        import copy
        edited = copy.deepcopy(reg)
        lr.row_for("s2-phantom-write-screen", edited)["tier"] = "T3"
        assert any("s2-phantom-write-screen: tier T3 but the last tier-bearing event says T0" in p
                   for p in lr.validate(edited))

    def test_env_override_redirects_the_default_path(self, tmp_path, monkeypatch):
        p = _write(tmp_path, self._all_rows())
        monkeypatch.setenv("CORA_LADDER_REGISTRY_PATH", str(p))
        assert lr.registry_path() == p
        assert lr.load()["available"] is True


# ── acting drift ──────────────────────────────────────────────────────────────

class TestActingDrift:
    def test_probe_above_registered_tier_is_reported(self, tmp_path):
        rows = [_row(lane=l) for l in lr.KNOWN_LANES]
        rows[0]["acting_probe"] = "sentinel_mode"
        reg = lr.load(_write(tmp_path, rows))
        drift = lr.acting_drift(reg, {"sentinel_mode": lambda: "T2"})
        assert len(drift) == 1 and "ACTING at T2 but registered T0" in drift[0]

    def test_probe_at_or_below_tier_is_quiet(self, tmp_path):
        rows = [_row(lane=l) for l in lr.KNOWN_LANES]
        rows[0]["acting_probe"] = "sentinel_mode"
        rows[0]["tier"] = "T2"
        reg = lr.load(_write(tmp_path, rows))
        assert lr.acting_drift(reg, {"sentinel_mode": lambda: "T2"}) == []
        assert lr.acting_drift(reg, {"sentinel_mode": lambda: "T0"}) == []

    def test_unreadable_probe_is_reported_not_swallowed(self, tmp_path):
        rows = [_row(lane=l) for l in lr.KNOWN_LANES]
        rows[0]["acting_probe"] = "sentinel_mode"
        reg = lr.load(_write(tmp_path, rows))

        def boom():
            raise RuntimeError("no module")
        drift = lr.acting_drift(reg, {"sentinel_mode": boom})
        assert len(drift) == 1 and "cannot read acting tier" in drift[0]
        assert "cannot read" in lr.acting_drift(reg, {"sentinel_mode": lambda: None})[0]
        assert "unknown" in lr.acting_drift(reg, {})[0]

    def test_live_probes_read_the_lanes_own_flags(self, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        assert lr.PROBES["sentinel_mode"]() == "T0"
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        assert lr.PROBES["sentinel_mode"]() == "T2"
        monkeypatch.delenv("CORA_SEND_LIVE", raising=False)
        assert lr.PROBES["send_live_mode"]() == "T0"
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        assert lr.PROBES["ensure_mode"]() == "T2"
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "plan")
        assert lr.PROBES["ensure_mode"]() == "T0"

    def test_real_registry_drift_is_quiet_with_every_flag_at_default(self, monkeypatch):
        for k in ("CORA_SENTINEL_ENFORCE", "CORA_SEND_LIVE", "CORA_DELEGATED_WORK"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")   # the live flip; both ensure rows are T2
        assert lr.acting_drift(lr.load(_REAL)) == []

    def test_real_registry_flags_enforce_as_drift_on_the_t0_rails(self, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        drift = lr.acting_drift(lr.load(_REAL))
        lanes_hit = {d.split(":")[0] for d in drift}
        assert lanes_hit == {"s2-phantom-write-screen", "honesty-rail-capability-screen"}


# ── render ────────────────────────────────────────────────────────────────────

class TestRender:
    def test_deterministic_and_sorted(self, tmp_path):
        rows = [_row(lane=l) for l in lr.KNOWN_LANES]
        rows.reverse()
        reg = lr.load(_write(tmp_path, rows))
        a = lr.render_markdown(reg)
        b = lr.render_markdown(reg)
        assert a == b
        body = [ln for ln in a.splitlines() if ln.startswith("| ") and not ln.startswith("| lane")]
        lanes_in_order = [ln.split("|")[1].strip() for ln in body]
        assert lanes_in_order == sorted(lr.KNOWN_LANES)
        assert "READ-ONLY MIRROR" in a and "Schema: clean" in a

    def test_unavailable_registry_renders_the_reason(self, tmp_path):
        text = lr.render_markdown(lr.load(tmp_path / "nope.yaml"))
        assert "REGISTRY UNAVAILABLE" in text and "missing" in text

    def test_summary_shape(self):
        s = lr.summary(lr.load(_REAL), probes={})
        assert s["available"] and s["lanes"] == len(lr.KNOWN_LANES)
        assert s["schema_problems"] == []
        assert set(s["by_tier"]) == set(lr.TIERS)
        # 0 after the 2026-09-19 batch confirm (applied 2026-09-20). A NEW lane ships
        # pending-Harrison and is confirmed in its own commit -- which must update this pin.
        assert s["pending_confirmation"] == []


# ── the readers: nightly health check + Monday digest ─────────────────────────

import sys as _sys  # noqa: E402

_sys.path.insert(0, str(_REPO / "scripts"))
import nightly_health_check as nhc  # noqa: E402
import cora_health_report as chr_  # noqa: E402


@pytest.fixture
def quiet_flags(monkeypatch):
    for k in ("CORA_SENTINEL_ENFORCE", "CORA_SEND_LIVE", "CORA_DELEGATED_WORK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
    monkeypatch.delenv("CORA_LADDER_REGISTRY_PATH", raising=False)


class TestHealthCheck:
    def test_shipped_registry_reads_clean(self, quiet_flags):
        r = nhc.check_ladder_registry()
        assert r.status == "ok" and "schema clean, no acting drift" in r.detail
        assert f"{len(lr.KNOWN_LANES)} lanes" in r.detail and "pending Harrison confirm" in r.detail

    def test_missing_file_is_a_warn_never_a_quiet_ok(self, quiet_flags, tmp_path, monkeypatch):
        monkeypatch.setenv("CORA_LADDER_REGISTRY_PATH", str(tmp_path / "nope.yaml"))
        r = nhc.check_ladder_registry()
        assert r.status == "warn" and "UNAVAILABLE" in r.detail and "absence IS drift" in r.detail

    def test_enforce_flag_reads_as_acting_drift_on_the_t0_rails(self, quiet_flags, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        r = nhc.check_ladder_registry()
        assert r.status == "warn" and "ACTING at T2 but registered T0" in r.detail
        assert "s2-phantom-write-screen" in r.detail and "honesty-rail-capability-screen" in r.detail

    def test_schema_violation_is_a_warn(self, quiet_flags, tmp_path, monkeypatch):
        rows = [_row(lane=l) for l in lr.KNOWN_LANES]
        rows[0]["tier"] = "T2"
        rows[0]["evidence_monitor"] = {"description": "m", "failing_capable": False}
        monkeypatch.setenv("CORA_LADDER_REGISTRY_PATH", str(_write(tmp_path, rows)))
        r = nhc.check_ladder_registry()
        assert r.status == "warn" and "without a failing-capable evidence_monitor" in r.detail

    def test_registered_in_main_before_the_egress_rails(self):
        import inspect
        src = inspect.getsource(nhc)
        body = src[src.index("def main("):]
        assert "all_results.append(check_ladder_registry())" in body
        assert body.index("all_results.append(check_ladder_registry())") < body.index("all_results.append(check_egress_rails())")


class TestDigest:
    def test_section_alarm_and_slack_line(self, quiet_flags, monkeypatch):
        s = chr_.ladder_registry_section()
        assert s["available"] and s["lanes"] == len(lr.KNOWN_LANES) and s["tiers"] == list(lr.TIERS)
        assert chr_.threshold_alarms({"ladder_registry": s}) == []
        line = chr_.format_slack({"ladder_registry": s, "alarms": [], "token_method": "t"})
        assert "*Ladder registry:*" in line and f"{len(lr.KNOWN_LANES)} lanes" in line and "drift 0" in line
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        s2 = chr_.ladder_registry_section()
        alarms = chr_.threshold_alarms({"ladder_registry": s2})
        assert any("LADDER REGISTRY" in a and "ACTING at T2" in a for a in alarms)

    def test_unavailable_registry_alarms_rather_than_hiding(self, quiet_flags, tmp_path, monkeypatch):
        monkeypatch.setenv("CORA_LADDER_REGISTRY_PATH", str(tmp_path / "nope.yaml"))
        s = chr_.ladder_registry_section()
        assert s["available"] is False
        assert any("LADDER REGISTRY unavailable" in a for a in chr_.threshold_alarms({"ladder_registry": s}))
