"""Autonomy-ladder REGISTRY (Code #13 slice 7, cq-6afa86210ba0; charter v1 section 1
R-A schema, roadmap v2 WS-B).

THE DEFECT. The ladder had rows and no registry: five bespoke copies of one schema
in four files (the Code #12 report section 4, the ingest-integrity report section
4, `_mirror/cora-mirror-LADDER-ROW.md` = a string constant in the mirror script,
the DR/VM kickoff's task-estate row, the blog one-tap cap as charter prose). The
9/8 honesty-rail amendment requires tier narration to be REGISTRY-derived, which
needs a registry.

THE FILE. ``data/ladder-registry.yaml`` -- one row per lane:

    lane                 stable id (kebab-case)
    title                human name + the code seam it names
    tier                 T0 | T1 | T2 | T3 | CAP-T1   (CAP-T1 = a deliberate permanent
                         T1 cap; charter line 23)
    status               live | dark | unbuilt | cowork-estate  (a lifecycle, NOT a
                         tier: 'revops DARK' and the unbuilt task-estate manifest
                         cannot be expressed honestly with the tier enum alone)
    promotion_criteria   what would earn the next tier
    demotion_triggers    what drops it a tier AUTOMATICALLY (charter line 21)
    evidence_monitor     {description, failing_capable: bool} -- a lane whose
                         monitor cannot FAIL cannot hold above T1 (charter line 22;
                         enforced here as a schema violation)
    audit_surface        where a human reads what the lane did
    authority            who may promote (Harrison)
    confirmed_by         'pending-Harrison' until his batch confirm flips it
    acting_probe         (optional) name of a LIVE-MODE probe in PROBES whose
                         observed tier the health check compares to `tier`
    capability_terms     (optional) natural-language names of what the lane lets
                         Cora DO -- read by cora.capability_set for the honesty rail
    ask_hint             (optional) the `Try:` hint the honesty rail prints
    events[]             {ts, event: seeded|promoted|demoted|confirmed|note, by,
                         evidence, tier?} -- append-only history; `tier` = the tier
                         the lane holds AFTER the event (required on promoted /
                         demoted, carried on every seeded event); a tier change
                         without an event is a schema violation, ENFORCED: the
                         last tier-bearing event must equal `tier`

RULES (all deterministic; NO LLM reads or writes this file):
  * every tier change is an `events[]` entry with evidence; no lane skips a tier
    (a `promoted` event is exactly +1 rank; a `demoted` event is strictly lower;
    the last tier-bearing event == `tier`) -- see _tier_history_problems;
  * a lane acting ABOVE its registered tier (live probe) is a health WARN;
  * a lane in KNOWN_LANES with no row is a health WARN, and a row not in KNOWN_LANES
    is a schema WARN (the two lists are pinned to each other by tests);
  * the file is written ONLY by a Code/Cowork commit (Harrison's batch confirm flips
    `confirmed_by`); the bot never writes it. The rendered markdown rides ZONE-X via
    the claude-workspace mirror (KB-excluded by the `_shared/projects/cora` folder
    pin AND the `cora-mirror-` title belt).

Readers: scripts/nightly_health_check.py (drift WARNs), scripts/cora_health_report.py
(Monday digest table), scripts/mirror_claude_workspace.py (rendered
cora-mirror-LADDER-REGISTRY.md), cora.capability_set (capability_terms). Every
reader is call-time and fail-soft; a missing or malformed file is a WARN, never a
crash and never a silent OK (the file is a Code-13 deliverable, so absence IS drift).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _REPO_ROOT / "data" / "ladder-registry.yaml"

TIERS: tuple[str, ...] = ("T0", "T1", "T2", "T3", "CAP-T1")
STATUSES: tuple[str, ...] = ("live", "dark", "unbuilt", "cowork-estate")
EVENT_KINDS: tuple[str, ...] = ("seeded", "promoted", "demoted", "confirmed", "note")
REQUIRED_KEYS: tuple[str, ...] = (
    "lane", "title", "tier", "status", "promotion_criteria", "demotion_triggers",
    "evidence_monitor", "audit_surface", "authority", "confirmed_by", "events",
)
#: Tier ORDER for drift comparison. CAP-T1 sits at T1 (a cap, not a higher rung).
TIER_RANK: dict[str, int] = {"T0": 0, "T1": 1, "CAP-T1": 1, "T2": 2, "T3": 3}

#: Every lane the registry MUST row (pinned both ways by tests). A new lane ships
#: with its row from birth (kickoff section 3), so this tuple grows with the lanes.
KNOWN_LANES: tuple[str, ...] = (
    "s2-phantom-write-screen",
    "c2-mechanical-batch-expiry",
    "claude-workspace-mirror",
    "cora-self-inventory",
    "task-estate-manifest",
    "f3e-blog-one-tap",
    "inventory-adjust-g1",
    "delegated-work-phase1",
    "email-triage-tier1",
    "email-triage-tier2",
    "revops-send",
    "one-cora-ensure",
    "honesty-rail-capability-screen",
    "founder-dm-queue-verbs",
    "nightly-catchup",
    "one-cora-rsvp-accept",
    "meeting-recap-card",
    "drive-sweep-allowlist",
    "repeat-signal-escalation",
    "expected-invoice-owner-nudge",
    "cowork-run-marker-contract",
)


def registry_path() -> Path:
    """Overridable so tests never read the repo file by accident."""
    return Path(os.environ.get("CORA_LADDER_REGISTRY_PATH", "") or _DEFAULT_PATH)


# ── load ─────────────────────────────────────────────────────────────────────
def load(path: Path | None = None) -> dict[str, Any]:
    """The parsed registry, or ``{"available": False, "reason": ...}`` when the file
    is missing or unparseable (call-time, never import-time, so tests redirect)."""
    p = Path(path) if path is not None else registry_path()
    try:
        if not p.exists():
            return {"available": False, "reason": f"registry file missing: {p}", "lanes": []}
        import yaml  # lazy: keep the module importable without PyYAML at import time
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 -- unparseable = unavailable, said out loud
        return {"available": False, "reason": f"registry unreadable: {exc}", "lanes": []}
    if not isinstance(data, dict):
        return {"available": False, "reason": "registry root is not a mapping", "lanes": []}
    lanes = data.get("lanes")
    data["lanes"] = [x for x in (lanes or []) if isinstance(x, dict)] if isinstance(lanes, list) else []
    data["available"] = True
    data["path"] = str(p)
    return data


def lanes(reg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    reg = reg if reg is not None else load()
    return list(reg.get("lanes") or [])


def row_for(lane: str, reg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    for row in lanes(reg):
        if str(row.get("lane") or "") == lane:
            return row
    return None


def tier_of(lane: str, reg: dict[str, Any] | None = None) -> str | None:
    row = row_for(lane, reg)
    return str(row.get("tier")) if row and row.get("tier") is not None else None


# ── validate ─────────────────────────────────────────────────────────────────
def _monitor_failing_capable(row: dict[str, Any]) -> bool | None:
    m = row.get("evidence_monitor")
    if isinstance(m, dict):
        v = m.get("failing_capable")
        return bool(v) if isinstance(v, bool) else None
    return None


#: Event kinds that MAY carry `tier` (the tier the lane holds AFTER the event).
#: `promoted` and `demoted` MUST carry it: a tier change that does not say where
#: it landed cannot be checked against `tier`, which is the whole point.
TIER_BEARING_EVENTS: tuple[str, ...] = ("seeded", "promoted", "demoted")


def _tier_history_problems(lane: str, tier: str, events: list[Any]) -> list[str]:
    """The docstring's rules, ENFORCED (D-051 EF-8): before this, events carried no
    tier, so a hand-edited `tier: T3` with no event, a T0->T2 promotion that skipped
    T1, and a `demoted` event with the tier left high all validated clean -- and a
    raised registered tier silenced the acting-drift WARN the registry exists to
    produce. Rules over the tier-bearing events in order:

      (a) when ANY event carries a tier, the LAST tier-bearing event's tier must
          equal the row's `tier` (a tier change without an event is a violation);
      (b) a `promoted` event lands exactly ONE rank above the previous tier-bearing
          event (no lane skips a tier);
      (c) a `demoted` event lands strictly BELOW the previous tier-bearing event;
      (d) `promoted` / `demoted` without a `tier`, or a `tier` outside TIERS, is
          malformed.
    A row whose events carry no tier at all (a pre-EF-8 hand-written registry) is
    left to the evidence rule above -- the shipped file carries a tier on every
    seeded event so rule (a) is live from day one.
    """
    problems: list[str] = []
    prev: str | None = None
    last: str | None = None
    for e in events:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("event") or "")
        has_tier = "tier" in e and e.get("tier") not in (None, "")
        if kind not in TIER_BEARING_EVENTS:
            if has_tier:
                problems.append(f"{lane}: `{kind}` event must not carry `tier` ({e.get('tier')!r})")
            continue
        if not has_tier:
            if kind in ("promoted", "demoted"):
                problems.append(f"{lane}: `{kind}` event without a `tier` (where did it land?)")
            continue
        et = str(e.get("tier"))
        if et not in TIERS:
            problems.append(f"{lane}: event tier {et!r} not in {list(TIERS)}")
            continue
        if kind == "promoted":
            if prev is None:
                problems.append(f"{lane}: `promoted` to {et} with no earlier tier-bearing event")
            elif TIER_RANK[et] != TIER_RANK[prev] + 1:
                problems.append(f"{lane}: `promoted` {prev} -> {et} is not exactly one rank "
                                "(no lane skips a tier)")
        elif kind == "demoted":
            if prev is None:
                problems.append(f"{lane}: `demoted` to {et} with no earlier tier-bearing event")
            elif TIER_RANK[et] >= TIER_RANK[prev]:
                problems.append(f"{lane}: `demoted` {prev} -> {et} does not lower the tier")
        prev = et
        last = et
    if last is not None and last != tier:
        problems.append(f"{lane}: tier {tier} but the last tier-bearing event says {last} "
                        "(a tier change without an event is a schema violation)")
    return problems


def validate(reg: dict[str, Any]) -> list[str]:
    """Schema problems as human sentences (empty = clean). Never raises."""
    problems: list[str] = []
    if not reg.get("available", True):
        return [str(reg.get("reason") or "registry unavailable")]
    seen: set[str] = set()
    for i, row in enumerate(lanes(reg)):
        lane = str(row.get("lane") or f"<row {i}>")
        for k in REQUIRED_KEYS:
            if k not in row or row.get(k) in (None, ""):
                problems.append(f"{lane}: missing `{k}`")
        if lane in seen:
            problems.append(f"{lane}: duplicate lane id")
        seen.add(lane)
        tier = str(row.get("tier") or "")
        if tier not in TIERS:
            problems.append(f"{lane}: tier {tier!r} not in {list(TIERS)}")
        status = str(row.get("status") or "")
        if status not in STATUSES:
            problems.append(f"{lane}: status {status!r} not in {list(STATUSES)}")
        fc = _monitor_failing_capable(row)
        if fc is None:
            problems.append(f"{lane}: evidence_monitor needs `failing_capable: true|false`")
        elif TIER_RANK.get(tier, 0) > 1 and not fc:
            problems.append(f"{lane}: tier {tier} without a failing-capable evidence_monitor "
                            "(charter section 1: no failing-capable monitor = no promotion past T1)")
        if TIER_RANK.get(tier, 0) > 1 and not str(row.get("audit_surface") or "").strip():
            problems.append(f"{lane}: tier {tier} without an audit_surface (silent T2 = schema violation)")
        events = row.get("events")
        if not isinstance(events, list) or not events:
            problems.append(f"{lane}: `events` must be a non-empty list")
        else:
            for e in events:
                if not isinstance(e, dict) or not e.get("ts") or str(e.get("event") or "") not in EVENT_KINDS:
                    problems.append(f"{lane}: malformed event {e!r}")
            # a tier above T0 needs a promotion/seeding event that names evidence
            if TIER_RANK.get(tier, 0) > 0 and not any(
                    isinstance(e, dict) and str(e.get("event")) in ("seeded", "promoted", "confirmed")
                    and str(e.get("evidence") or "").strip() for e in events):
                problems.append(f"{lane}: tier {tier} with no seeded/promoted/confirmed event carrying evidence")
            problems.extend(_tier_history_problems(lane, tier, events))
        if lane not in KNOWN_LANES:
            problems.append(f"{lane}: not in KNOWN_LANES (add the lane to the tuple with its row)")
        terms = row.get("capability_terms")
        if terms is not None and (not isinstance(terms, list) or not all(isinstance(t, str) for t in terms)):
            problems.append(f"{lane}: capability_terms must be a list of strings")
        probe = row.get("acting_probe")
        if probe is not None and str(probe) not in PROBES:
            problems.append(f"{lane}: acting_probe {probe!r} is not a known probe {sorted(PROBES)}")
    for lane in KNOWN_LANES:
        if lane not in seen:
            problems.append(f"{lane}: KNOWN lane has no registry row")
    return problems


def pending_confirmation(reg: dict[str, Any] | None = None) -> list[str]:
    return [str(r.get("lane")) for r in lanes(reg)
            if str(r.get("confirmed_by") or "").startswith("pending")]


# ── live acting probes ───────────────────────────────────────────────────────
# Each probe returns the tier the lane is OBSERVED to act at right now, read from the
# live flag the lane itself reads, or None when it cannot be read. Never imports a
# bot-only module eagerly; every probe is wrapped by acting_drift.
def _probe_sentinel() -> str | None:
    from cora import egress_rails  # script-safe, stdlib-only module
    return "T2" if egress_rails.sentinel_mode() == "enforce" else "T0"


def _probe_revops() -> str | None:
    from cora.revops import send_trust
    return "T1" if send_trust.send_live_mode() == "tier1" else "T0"


def _probe_delegated() -> str | None:
    from cora import delegated_work
    lvl = delegated_work.delegated_level()
    return "T1" if lvl == "live" else "T0"


def _probe_ensure() -> str | None:
    from cora import meeting_capture
    m = meeting_capture.ensure_mode()
    return "T2" if m == "live" else "T0"


PROBES: dict[str, Callable[[], str | None]] = {
    "sentinel_mode": _probe_sentinel,
    "send_live_mode": _probe_revops,
    "delegated_level": _probe_delegated,
    "ensure_mode": _probe_ensure,
}


def acting_drift(reg: dict[str, Any], probes: dict[str, Callable[[], str | None]] | None = None) -> list[str]:
    """Lanes whose LIVE mode reads above their registered tier, as sentences; a probe
    that cannot be read is reported too (blind never renders clean)."""
    probes = probes if probes is not None else PROBES
    out: list[str] = []
    for row in lanes(reg):
        name = row.get("acting_probe")
        if not name:
            continue
        fn = probes.get(str(name))
        if fn is None:
            out.append(f"{row.get('lane')}: probe {name!r} unknown -- cannot read acting tier")
            continue
        try:
            observed = fn()
        except Exception as exc:  # noqa: BLE001
            out.append(f"{row.get('lane')}: probe {name} failed ({type(exc).__name__}) -- cannot read acting tier")
            continue
        if observed is None:
            out.append(f"{row.get('lane')}: probe {name} returned nothing -- cannot read acting tier")
            continue
        registered = str(row.get("tier") or "T0")
        if TIER_RANK.get(observed, 0) > TIER_RANK.get(registered, 0):
            out.append(f"{row.get('lane')}: ACTING at {observed} but registered {registered} "
                       f"(probe {name}) -- promote by Harrison's tap with an event, or flip the lane back")
    return out


# ── render ───────────────────────────────────────────────────────────────────
def render_markdown(reg: dict[str, Any], *, source_note: str = "") -> str:
    """Deterministic markdown (sorted by lane, fixed columns) for the ZONE-X mirror
    file and the Monday digest attachment. Byte-stable for equal input."""
    lines = [
        "<!-- READ-ONLY MIRROR -- working knowledge, not canon. Rendered from "
        "data/ladder-registry.yaml by cora.ladder_registry.render_markdown; edit the YAML, "
        "never this file. -->",
        "",
        "# Cora autonomy-ladder registry",
        "",
    ]
    if source_note:
        lines += [f"_{source_note}_", ""]
    if not reg.get("available", True):
        lines += [f"REGISTRY UNAVAILABLE: {reg.get('reason')}", ""]
        return "\n".join(lines) + "\n"
    rows = sorted(lanes(reg), key=lambda r: str(r.get("lane") or ""))
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("tier"))] = counts.get(str(r.get("tier")), 0) + 1
    pend = pending_confirmation(reg)
    lines.append(f"{len(rows)} lanes | " + " / ".join(f"{t} {counts.get(t, 0)}" for t in TIERS)
                 + f" | pending Harrison confirm: {len(pend)}")
    lines.append("")
    lines.append("| lane | tier | status | promotion_criteria | demotion_triggers | evidence_monitor "
                 "(failing-capable) | audit_surface | authority | confirmed_by | last event |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")

    def _cell(v: Any) -> str:
        return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ").strip()

    for r in rows:
        m = r.get("evidence_monitor") if isinstance(r.get("evidence_monitor"), dict) else {}
        ev = r.get("events") if isinstance(r.get("events"), list) else []
        last = ev[-1] if ev and isinstance(ev[-1], dict) else {}
        last_txt = f"{last.get('ts', '')} {last.get('event', '')} ({last.get('by', '')})".strip()
        lines.append("| " + " | ".join(_cell(x) for x in (
            r.get("lane"), r.get("tier"), r.get("status"), r.get("promotion_criteria"),
            r.get("demotion_triggers"),
            f"{m.get('description', '')} ({'yes' if m.get('failing_capable') else 'NO'})",
            r.get("audit_surface"), r.get("authority"), r.get("confirmed_by"), last_txt,
        )) + " |")
    lines.append("")
    problems = validate(reg)
    lines.append("Schema: " + ("clean" if not problems else f"{len(problems)} problem(s) -- " + "; ".join(problems)))
    lines.append("")
    return "\n".join(lines) + "\n"


def summary(reg: dict[str, Any], probes: dict[str, Callable[[], str | None]] | None = None) -> dict[str, Any]:
    """The one structure both health surfaces render: counts, pending, schema
    problems, acting drift."""
    if not reg.get("available", True):
        return {"available": False, "reason": reg.get("reason"), "lanes": 0}
    rows = lanes(reg)
    by_tier: dict[str, int] = {t: 0 for t in TIERS}
    by_status: dict[str, int] = {s: 0 for s in STATUSES}
    for r in rows:
        by_tier[str(r.get("tier"))] = by_tier.get(str(r.get("tier")), 0) + 1
        by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1
    return {
        "available": True,
        "path": reg.get("path", ""),
        "lanes": len(rows),
        "by_tier": by_tier,
        "by_status": by_status,
        "pending_confirmation": pending_confirmation(reg),
        "schema_problems": validate(reg),
        "acting_drift": acting_drift(reg, probes),
    }
