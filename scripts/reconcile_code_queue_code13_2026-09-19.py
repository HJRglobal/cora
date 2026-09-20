"""Step 7.5 queue reconciliation for Code #13 -- honesty rail + nightly catch-up + One-Cora
(branch ``claude/code-13-honesty-rail-2026-09-19``, bundle_id ``code-13``).

Transitions ONLY the seeds this branch closed (kickoff section 7). Dry-run by default;
``--apply`` writes through the module's own writers -- ``process_queue_action`` (carrying
bundle_id + branch + commit on the shipped event: the C7 HARD GATE), ``dismiss_with_
evidence``, ``supersede_item`` -- never by hand-editing the jsonl or the backlog (loop
step 7.5; the 2026-07-31 stale-positive incident).

    SHIPPED (bundle_id code-13)
      cq-2a88e32a75ea  S1 honesty rail: phantom-capability-claim screen (OBSERVE; third rail key)
      cq-70d7b203f7ad  RIDER 2 (rides S1): founder-DM queue-verb normalizer + PARSE_REFUSED reply
      cq-2e02f1fd0f65  I4 self-inventory force route covers "can you access <connector>"
      cq-fb50c9e6c911  slice 2: missed-start catch-up for the nightly ingest set (own 08:30 task)
      cq-19b0298cf5be  slice 3: ensure lane RSVP-accepts as cora@ after every guest-add
      cq-8c4f2f4e73fc  slice 4: capture auditor UNCONVENED bucket (group-calendar organizer basis)
      cq-7a724ee43964  slice 5: propose-only meeting recap card to the organizer; internal DMs on tap
      cq-6afa86210ba0  slice 7: autonomy-ladder registry file + health check + mirror page
      cq-da5abb36df07  adjacency (D-301): default sensitive-topic blocks for EVERY unlisted user
      cq-eebf2408f252  adjacency: inventory tool checks membership BEFORE the F3E scope guard
      cq-c5ed06e1f0bf  slice 8: personal Drive sweep allowlist-by-folder mode           (if built)
      cq-21207e34a954  slice 9a: expected-invoice owner nudge (portal-only ads billing) (if built)
      cq-06045f418bd2  slice 9c: file-drop run-marker contract for the Cowork estate    (if built)
    LEFT (printed note; nothing written unless --apply, which records a `reconciled`
    provenance note on the ONE row whose ship gate closed)
      cq-85b35413b020  rail-2 loosening: the harness ruled SHIP: NO (two classes have no rail)
                       -- stays STAGED; escalation note to Harrison
      cq-7f3febc35710  did not ride (LOW; next slot)
      cq-9f686d94365e  cq-bbdc206a097c  cq-9a409c0612dc  cq-41540856f95b  cq-9f1f6e9b7121
      cq-1de87e0f2e08  cq-6ffdd65034fa  cq-8324caf8fa08 (-> ladder-registry row email-triage-tier2)
      cq-cdb2510b682c  cq-5f8dc77ff9fb  the weekly memory-consolidation run has NOT closed them
                       (no capture cites it; both eval ids are still in the golden set) -> leave
      RIDER bundle code-identity-v1 (cq-8d16f1a557e5 / cq-40baab26d7f3 / cq-c624f28c772a): its
                       OWN 7.5 per the identity kickoff; NOT touched here
    DISMISSED WITH EVIDENCE
      cq-64272c86f5e1  RULED D-300 (2026-09-10): keep `expired_low_risk`
    SUPERSEDED
      cq-8618a256a8ef  by cq-19b0298cf5be (the RSVP-accept ask rides slice 3)

Every transition is GUARDED on the fix being present on the checked-out tree
(behavioural / reachability checks, never a grep for a string a stub could carry).

    --probe-gate   proves the C7 gate on THIS tree against a throwaway ledger: a
                   MARK_SHIPPED with no bundle reference must be refused. Never touches
                   the real ledger.

Run (from the repo root, AFTER the FF-merge):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code13_2026-09-19.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code13_2026-09-19.py --probe-gate
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code13_2026-09-19.py --apply
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # D-266: every spawn windowless
BUNDLE_ID = "code-13"
BRANCH = "claude/code-13-honesty-rail-2026-09-19"

HARNESS_NOTE = ("rail-2 differential harness (Code #13 slice 6) ruled SHIP: NO -- the "
                "sugar-free-on-Pure and comparative-category classes have NO rail under either "
                "preflight, so the ruled 'ALL caught' condition is unreachable by a rail-2 change; "
                "attribution sibling shipped UNWIRED, drafting.py unchanged; measured FP set "
                "legacy 4/5 -> attribution 0/5; live corpus 6 -> 4 trips; escalation "
                "_notes/2026-09-19_fndr_ESCALATION-code-13-slice6-rail2.md")


# ── code-presence preconditions (each returns a blocker string, or None) ──────
def _capture_log(logger_name: str, fn: Callable[[], object]) -> tuple[object, list[str]]:
    hits: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):  # noqa: D401
            hits.append(record.getMessage())
    h = _H()
    lg = logging.getLogger(logger_name)
    lg.addHandler(h)
    prev = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        out = fn()
    finally:
        lg.removeHandler(h)
        lg.setLevel(prev)
    return out, hits


def _s1_present() -> str | None:
    from cora import slack_egress, egress_rails, app
    if not hasattr(slack_egress, "screen_capability_claims"):
        return "S1 screen_capability_claims missing on this tree"
    if "phantom-capability-claim" not in set(getattr(egress_rails, "RAIL_KEYS", ())):
        return "S1 third rail key not counted by egress_rails"
    src = inspect.getsource(app._dispatch_qa)
    if src.count("slack_egress.screen_capability_claims(") != 3:
        return "S1 screen not wired at the cached-serve path + both final-reply sites in _dispatch_qa"
    # BEHAVIOUR: a denial about a capability F3E HAS, zero tool_use -> one observe WARN,
    # byte-identical text (CORA_SENTINEL_ENFORCE is never flipped by this script).
    text = "I don't have access to HubSpot, so I can't see the pipeline."
    out, hits = _capture_log(slack_egress.__name__, lambda: slack_egress.screen_capability_claims(
        text, tool_use_count=0, channel_name="f3e-sales", user_id="U1", entity="F3E"))
    if out != text:
        return "S1 screen mutated text outside enforce mode"
    if not any("phantom-capability-claim kind=denial" in m for m in hits):
        return "S1 screen did not WARN kind=denial on the F3E denial shape"
    return None


def _rider2_present() -> str | None:
    for name in ("normalize_verb_text", "looks_like_queue_verb_attempt", "PARSE_REFUSED_REPLY"):
        if not hasattr(code_queue, name):
            return f"RIDER 2 {name} missing"
    if code_queue.match_queue_verb(code_queue.normalize_verb_text("- `stage cq-621dfad586aa`")) \
            != ("stage", "cq-621dfad586aa"):
        return "RIDER 2 normalizer does not fold a list marker + backticks to the bare verb"
    if not code_queue.looks_like_queue_verb_attempt("stage <cq-621dfad586aa>"):
        return "RIDER 2 attempt detector misses the angle-bracket shape"
    if code_queue.looks_like_queue_verb_attempt("Cora should retry the way cq-621dfad586aa describes"):
        return "RIDER 2 attempt detector fires on a citation sentence"
    from cora import app
    hm = inspect.getsource(app.handle_message_event)
    for needle in ("code_queue.normalize_verb_text(", "code_queue.looks_like_queue_verb_attempt(",
                   "code_queue.PARSE_REFUSED_REPLY"):
        if needle not in hm:
            return f"RIDER 2 {needle} not wired in app.handle_message_event"
    return None


def _i4_present() -> str | None:
    from cora import self_inventory as si
    if not si.is_self_inventory_question("can you access HubSpot?"):
        return "I4 'can you access <connector>' does not route to the self-inventory"
    if not si.is_self_inventory_question("are you able to access our QuickBooks?"):
        return "I4 'able to access <connector>' does not route to the self-inventory"
    if si.is_self_inventory_question("what is the F3E pipeline total?"):
        return "I4 intent matcher over-fires on an ordinary data question"
    return None


def _slice2_present() -> str | None:
    from cora import nightly_catchup as nc
    for name in ("load_set", "decide", "append_ledger", "read_ledger", "TASK_NAME"):
        if not hasattr(nc, name):
            return f"slice 2 nightly_catchup.{name} missing"
    _window, tasks = nc.load_set(_REPO_ROOT / "data" / "maps" / "nightly-catchup-set.yaml")
    if not any(getattr(t, "enabled", True) for t in tasks):
        return "slice 2 catch-up set has no enabled task"
    if not (_REPO_ROOT / "scripts" / "check_missed_nightly.py").exists():
        return "slice 2 scripts/check_missed_nightly.py missing"
    if not (_REPO_ROOT / "deployment" / "setup-missed-nightly-catchup-task.ps1").exists():
        return "slice 2 task registration PS1 missing"
    hc = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    if "all_results.append(check_missed_nightly_catchup())" not in hc:
        return "slice 2 check_missed_nightly_catchup not registered in the nightly check"
    return None


def _slice3_present() -> str | None:
    from cora.tools import calendar_client
    from cora import meeting_capture as mc
    if not hasattr(calendar_client, "set_own_response"):
        return "slice 3 calendar_client.set_own_response missing"
    if not hasattr(mc, "_plan_rsvp") or not hasattr(mc, "EnsureAction"):
        return "slice 3 ensure-lane RSVP plan step missing"
    names = {f.name for f in dataclasses.fields(mc.EnsureAction)}
    if not {"rsvp", "rsvp_planned", "rsvp_event_id", "rsvp_error"} <= names:
        return "slice 3 EnsureAction carries no rsvp fields"
    return None


def _slice4_present() -> str | None:
    from cora import meeting_capture as mc
    if getattr(mc, "UNCONVENED_BASIS_GROUP_CALENDAR", "") != "group-calendar-organizer":
        return "slice 4 UNCONVENED basis constant missing"
    if not hasattr(mc, "presumed_unconvened_basis"):
        return "slice 4 presumed_unconvened_basis missing"
    suffix = getattr(mc, "GROUP_CALENDAR_ORGANIZER_SUFFIX", "")
    if not suffix:
        return "slice 4 GROUP_CALENDAR_ORGANIZER_SUFFIX missing"
    if mc.presumed_unconvened_basis({"organizer": {"email": "team-cal" + suffix}}) \
            != "group-calendar-organizer":
        return "slice 4 a group-calendar organizer is not bucketed unconvened"
    if mc.presumed_unconvened_basis({"organizer": {"email": "person@hjrglobal.com"}}):
        return "slice 4 a person-organized meeting reads as unconvened"
    if "unconvened" not in {f.name for f in dataclasses.fields(mc.AuditReport)}:
        return "slice 4 AuditReport has no unconvened bucket"
    return None


def _slice5_present() -> str | None:
    from cora import meeting_recap as mr, app
    for name in ("prepare_card", "claim_for_tap", "process_share", "mark_dismissed", "already_carded"):
        if not hasattr(mr, name):
            return f"slice 5 meeting_recap.{name} missing"
    if "meeting_recap.claim_for_tap(" not in inspect.getsource(app):
        return "slice 5 recap tap handler not wired in app.py"
    rk = (_REPO_ROOT / "scripts" / "run_meeting_ask_capture.py").read_text(encoding="utf-8")
    if "def process_recap(" not in rk or "meeting_recap.prepare_card(" not in rk:
        return "slice 5 recap card not produced by the meeting-ask capture run"
    # BEHAVIOUR (D-051 review C-1): a live WebClient returns SlackResponse, which is
    # NOT a dict subclass. Every symbol above was present while the lane was dead in
    # prod because post_card read a non-dict response as "no channel id". The card
    # must POST and RECORD against a non-dict Mapping response. The probe redirects
    # both card write paths to a throwaway dir -- it never touches the real store.
    from collections.abc import Mapping

    class _NonDictResponse(Mapping):
        def __init__(self, data: dict):
            self._d = dict(data)

        def __getitem__(self, key):
            return self._d[key]

        def __iter__(self):
            return iter(self._d)

        def __len__(self):
            return len(self._d)

    class _ProbeClient:
        def conversations_open(self, users):
            return _NonDictResponse({"ok": True, "channel": {"id": "D-probe-slice5"}})

        def chat_postMessage(self, **kw):
            return _NonDictResponse({"ok": True, "ts": "1.1"})

    probe_id = "probe-slice5-recap"
    rec = {
        "recap_id": probe_id, "transcript_id": probe_id,
        "meeting_title": "probe", "meeting_date": "2026-01-01", "entity": "F3E",
        "is_lex": False, "overview": "probe overview", "action_items": "",
        "transcript_url": "", "recipients": ["U0PROBE00001"], "attendee_count": 2,
        "addressee_id": "U0PROBE00000", "routing_reason": "",
        "attribution_unreliable": False, "sent_to": [], "state": mr.STATE_PENDING,
    }
    keys = ("MEETING_RECAP_PENDING_PATH", "MEETING_RECAP_LEDGER_PATH")
    saved = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory() as td:
        os.environ["MEETING_RECAP_PENDING_PATH"] = str(Path(td) / "pending.jsonl")
        os.environ["MEETING_RECAP_LEDGER_PATH"] = str(Path(td) / "ledger.jsonl")
        try:
            posted = bool(mr.post_card(_ProbeClient(), rec))
            stored = mr.get_record(probe_id) or {}
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    if not posted or stored.get("dm_channel_id") != "D-probe-slice5" or stored.get("card_message_ts") != "1.1":
        return ("slice 5 post_card does not deliver against a non-dict (SlackResponse-shaped) "
                "client -- the dict type guard that killed the lane in prod is back")
    return None


def _slice7_present() -> str | None:
    from cora import ladder_registry as lr
    reg = lr.load(_REPO_ROOT / "data" / "ladder-registry.yaml")
    errs = lr.validate(reg)
    if errs:
        return f"slice 7 ladder registry invalid: {errs[:2]}"
    if len(lr.lanes(reg)) < 20:
        return "slice 7 ladder registry has fewer rows than the seeded 21"
    hc = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    if "all_results.append(check_ladder_registry())" not in hc:
        return "slice 7 check_ladder_registry not registered in the nightly check"
    mirror = (_REPO_ROOT / "scripts" / "mirror_claude_workspace.py").read_text(encoding="utf-8")
    if "def render_ladder_registry(" not in mirror:
        return "slice 7 mirror page renderer missing"
    return None


def _d301_present() -> str | None:
    from cora import user_access as ua
    topics = set(getattr(ua, "_UNLISTED_DEFAULT_BLOCKED_TOPICS", ()))
    if not {"financials", "hr", "legal", "phi", "cap_table"} <= topics:
        return "D-301 default block list incomplete"
    # BEHAVIOUR: an UNLISTED user on an aggregator entity (where the entity gate passes)
    # is blocked on an HR ask and allowed on an ordinary one -- no grant involved.
    if ua.check_access("UZZUNLISTED0000", "HJRG", "what is Justin's salary?") is None:
        return "D-301 an unlisted HJRG asker passes an HR question"
    if ua.check_access("UZZUNLISTED0000", "HJRG", "what time is the team lunch?") is not None:
        return "D-301 default blocks over-fire on an ordinary question"
    return None


def _ordering_present() -> str | None:
    from cora.tools import tool_dispatch as td
    s = inspect.getsource(td._shopify_set_inventory_impl)
    a = s.find("inventory_membership.allows(")
    b = s.find("DTC inventory updates are only available from F3E channels")
    if a < 0 or b < 0:
        return "adjacency: membership check or scope guard missing from the inventory tool"
    if a > b:
        return "adjacency: the scope guard still runs BEFORE the membership check"
    if not (_REPO_ROOT / "tests" / "test_inventory_guard_order.py").exists():
        return "adjacency: ordering pin test missing"
    return None


def _slice8_present() -> str | None:
    import yaml
    from cora.connectors import drive_sweep as ds
    from cora import self_inventory as si
    for name in ("resolve_sweep_mode", "_file_disposition_ex", "AGGREGATE_COUNTER_KEYS",
                 "ALLOWLIST_FOLDER_LABELS", "DRIVE_SWEEP_MODE_ALLOWLIST"):
        if not hasattr(ds, name):
            return f"slice 8 drive_sweep.{name} missing"
    if "skipped_outside_allowlist" not in ds.AGGREGATE_COUNTER_KEYS:
        return "slice 8 skipped_outside_allowlist not folded into the aggregate"
    # BEHAVIOUR: absent key = denylist; empty allowlist REFUSED; unknown mode REFUSED (never denylist)
    if ds.resolve_sweep_mode({})[0] != "denylist" or ds.resolve_sweep_mode({})[2] is not None:
        return "slice 8 absent drive_sweep_mode does not read as denylist"
    if ds.resolve_sweep_mode({"drive_sweep_mode": "allowlist", "drive_sweep_allowlist": []})[2] is None:
        return "slice 8 an EMPTY allowlist is not refused"
    if ds.resolve_sweep_mode({"drive_sweep_mode": "alowlist"})[2] is None:
        return "slice 8 an unknown mode falls open"
    # a parentless file in allowlist mode is outside; the same file in denylist mode ingests
    if ds._file_disposition(None, [], frozenset(), True, {}, allowlist=frozenset({"X"})) != "outside_allowlist":
        return "slice 8 parentless file not outside_allowlist in allowlist mode"
    if ds._file_disposition(None, [], frozenset(), True, {}) is not None:
        return "slice 8 denylist parentless disposition changed"
    roster = yaml.safe_load((_REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml")
                            .read_text(encoding="utf-8")) or {}
    rows = [r for r in roster.get("accounts") or [] if isinstance(r, dict) and r.get("drive_sweep_mode")]
    if len(rows) != 1 or rows[0].get("email") != "harrison@hjrglobal.com":
        return "slice 8 exactly ONE roster row (harrison@ primary) must carry drive_sweep_mode"
    if ds.FOUNDERS_OS_ROOT_ID not in set(rows[0].get("drive_sweep_allowlist") or []):
        return "slice 8 the v1 allowlist does not name the HJR-Founder-OS root"
    if not hasattr(si, "read_drive_sweep_modes"):
        return "slice 8 self_inventory.read_drive_sweep_modes missing"
    if not (_REPO_ROOT / "scripts" / "drive_sweep_allowlist_manifest.py").exists():
        return "slice 8 migration manifest script missing"
    return None


def _slice9ab_present() -> str | None:
    import json
    from cora import repeat_signal as rs, expected_invoices as ei, org_roles, decision_lane as dl
    for name in ("fire", "ack", "clear", "make_key", "active_keys", "tier2_signals"):
        if not hasattr(rs, name):
            return f"slice 9b repeat_signal.{name} missing"
    for name in ("nudge_candidates", "resolve_owner", "format_owner_nudge", "signal_key_for"):
        if not hasattr(ei, name):
            return f"slice 9a expected_invoices.{name} missing"
    if not hasattr(org_roles, "find_by_handle"):
        return "slice 9a org_roles.find_by_handle missing"
    if getattr(dl, "PING_SURFACE_PREFIX", "") != "ping:":
        return "slice 9b decision_lane.PING_SURFACE_PREFIX missing"
    # BEHAVIOUR (D-310): a ping row never counts as a delivery
    tmp = Path(tempfile.mkdtemp(prefix="cq-9b-"))
    try:
        led = tmp / "deliveries.jsonl"
        led.write_text(json.dumps({"ts": "2026-09-19T15:45:00+00:00", "key": "gate topic",
                                   "topic": "gate topic", "surface": "ping:health_check"}) + "\n",
                       encoding="utf-8")
        idx = dl.delivery_index(ledger=led)
        if "gate topic" in (idx or {}):
            return "slice 9b a ping: surface row counted as a delivery"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # an unresolvable owner fails closed to Harrison, never to nobody
    sid, _name, resolved = ei.resolve_owner("no-such-handle-zz")
    if resolved or sid != HARRISON_ID:
        return "slice 9a an unresolvable owner does not fall back to Harrison"
    hc = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    if 'decision_lane.PING_SURFACE_PREFIX + "health_check"' not in hc or "_gate_escalate(" not in hc:
        return "slice 9b the decision-gate check does not record a ping / route through repeat_signal"
    if "REPEAT_SIGNAL_WRITE_FAILING" not in hc:
        return "slice 9b ledger write failure is not a CRITICAL log pattern"
    inv = (_REPO_ROOT / "scripts" / "run_expected_invoice_check.py").read_text(encoding="utf-8")
    if "def plan_signals(" not in inv or "def send_nudges(" not in inv:
        return "slice 9a the invoice check has no nudge path"
    return None


def _slice9c_present() -> str | None:
    import yaml
    cad = _REPO_ROOT / "data" / "maps" / "cowork-run-cadence.yaml"
    if not cad.exists():
        return "slice 9c cadence map missing"
    rows = yaml.safe_load(cad.read_text(encoding="utf-8")) or {}
    rows = {k: v for k, v in rows.items() if isinstance(v, dict) and "cadence_hours" in v}
    if len(rows) < 4:
        return "slice 9c cadence map has fewer than the 4 transitional seeds"
    footer = _REPO_ROOT / "deployment" / "cowork-run-marker-footer.md"
    if not footer.exists() or not footer.read_text(encoding="utf-8").isascii():
        return "slice 9c footer/paste-block doc missing or non-ASCII (D-016)"
    mirror = (_REPO_ROOT / "scripts" / "mirror_claude_workspace.py").read_text(encoding="utf-8")
    for needle in ("def load_cadence(", "def read_run_marker(", "def evaluate_run_marker(",
                   "def run_marker_summary("):
        if needle not in mirror:
            return f"slice 9c mirror {needle} missing"
    hc = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    if "_RUN_MARKER_ALARM_KEYS" not in hc:
        return "slice 9c run-marker alarms not read by the nightly check"
    sync = (_REPO_ROOT / "scripts" / "incremental_sync_static.py").read_text(encoding="utf-8")
    if '== "_runs"' not in sync:
        return "slice 9c the _runs KB belt is missing from the static walk"
    return None


def _gate(fn: Callable[[], str | None]) -> str | None:
    """Run one precondition; a RAISE is a blocker, never an abort (D-051 lens E F10:
    an AttributeError from a renamed symbol mid --apply would have left the earlier
    transitions written and the later ones not)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"precondition raised {type(exc).__name__}: {_ascii(exc)}"


PRECONDITIONS: dict[str, Callable[[], str | None]] = {
    "cq-2a88e32a75ea": _s1_present,
    "cq-70d7b203f7ad": _rider2_present,
    "cq-2e02f1fd0f65": _i4_present,
    "cq-fb50c9e6c911": _slice2_present,
    "cq-19b0298cf5be": _slice3_present,
    "cq-8c4f2f4e73fc": _slice4_present,
    "cq-7a724ee43964": _slice5_present,
    "cq-6afa86210ba0": _slice7_present,
    "cq-da5abb36df07": _d301_present,
    "cq-eebf2408f252": _ordering_present,
    "cq-c5ed06e1f0bf": _slice8_present,
    "cq-21207e34a954": _slice9ab_present,
    "cq-06045f418bd2": _slice9c_present,
    "cq-8618a256a8ef": _slice3_present,   # superseded only once the fix that supersedes it is real
}

SHIPPED: dict[str, str] = {
    "cq-2a88e32a75ea": "S1 honesty rail: phantom-capability-claim screen (OBSERVE)",
    "cq-70d7b203f7ad": "RIDER 2: founder-DM queue-verb normalizer + PARSE_REFUSED reply",
    "cq-2e02f1fd0f65": "I4 self-inventory covers 'can you access <connector>'",
    "cq-fb50c9e6c911": "slice 2: missed-start catch-up for the nightly ingest set",
    "cq-19b0298cf5be": "slice 3: ensure lane RSVP-accepts as cora@",
    "cq-8c4f2f4e73fc": "slice 4: capture auditor UNCONVENED bucket",
    "cq-7a724ee43964": "slice 5: propose-only meeting recap card",
    "cq-6afa86210ba0": "slice 7: autonomy-ladder registry file",
    "cq-da5abb36df07": "adjacency (D-301): default sensitive-topic blocks for every unlisted user",
    "cq-eebf2408f252": "adjacency: membership check ordered before the F3E scope guard",
    "cq-c5ed06e1f0bf": "slice 8: personal Drive sweep allowlist-by-folder mode (D-303)",
    "cq-21207e34a954": "slice 9a/9b: expected-invoice owner nudge + repeat-signal escalation (D-302/D-310)",
    "cq-06045f418bd2": "slice 9c: Cowork-estate file-drop run-marker contract",
}
LEFT_OPEN: dict[str, str] = {
    "cq-85b35413b020": HARNESS_NOTE,
    "cq-7f3febc35710": "did not ride Code #13 (LOW) -- next slot",
    "cq-9f686d94365e": "next consolidation slot (kickoff section 0 out-of-scope list)",
    "cq-bbdc206a097c": "consolidation; its standalone kickoff stays PARKED",
    "cq-9a409c0612dc": "consolidation",
    "cq-41540856f95b": "consolidation (the lane works since the 8/31 grant; code-half not built)",
    "cq-9f1f6e9b7121": "E2 WS-F bundle",
    "cq-1de87e0f2e08": "consolidation (no per-file exclusion landed at this 7.5)",
    "cq-6ffdd65034fa": "consolidation",
    "cq-8324caf8fa08": "-> ladder-registry row email-triage-tier2 (slice 7), not a slice",
    "cq-cdb2510b682c": ("the weekly memory-consolidation run has NOT closed it -- no capture cites "
                        "it and auto-ka-20260711223622146658 is still in golden-set-auto.yaml -> leave"),
    "cq-5f8dc77ff9fb": ("the weekly memory-consolidation run has NOT closed it -- no capture cites "
                        "it and auto-note-4affd4ab42e7 is still in golden-set-auto.yaml -> leave"),
}
NOT_TOUCHED_RIDER_BUNDLE: tuple[str, ...] = ("cq-8d16f1a557e5", "cq-40baab26d7f3", "cq-c624f28c772a")
DISMISSED: dict[str, str] = {
    "cq-64272c86f5e1": ("RULED D-300 (2026-09-10): never-carded low-risk mechanical rows keep the "
                        "`expired_low_risk` outcome; no build follows from this ruling"),
}
SUPERSEDED: dict[str, tuple[str, str]] = {
    "cq-8618a256a8ef": ("cq-19b0298cf5be", "the RSVP-accept ask rides slice 3"),
}
# Rows that carry a `reconciled` provenance note under --apply WITHOUT a status change.
NOTED: dict[str, str] = {
    "cq-85b35413b020": "LEFT STAGED -- " + HARNESS_NOTE,
}

_TERMINAL_NOT_SHIPPABLE = frozenset({"DISMISSED", "SUPERSEDED"})


def _ascii(s: object) -> str:
    """process_queue_action's messages carry emoji; a redirected stdout on this host
    is cp1252 and would raise AFTER the ledger write."""
    return str(s).encode("ascii", "replace").decode("ascii")


def _head_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
            cwd=str(_REPO_ROOT), timeout=10, creationflags=_NO_WINDOW,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _provenance(cq_id: str, transition: str, commit: str) -> str | None:
    """A `reconciled` record for the non-shipped transitions (supersede / dismiss /
    left-with-note), so those rows answer "which bundle did this" too. The shipped
    event carries its own fields (C7). Reported, never mislabelled as a failed
    transition."""
    try:
        code_queue._append_event({
            "event": "reconciled", "ts": code_queue._now_iso(), "id": cq_id,
            "transition": transition, "bundle_id": BUNDLE_ID, "branch": BRANCH, "commit": commit,
        })
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {_ascii(exc)}"


def _shared_prompt_sharers(cq_id: str) -> list[str]:
    rec = code_queue.get_item(cq_id) or {}
    p = str(rec.get("prompt_path") or "")
    if not p:
        return []
    return sorted(it["id"] for it in code_queue.load_items()
                  if str(it.get("prompt_path") or "") == p and it["id"] != cq_id)


def probe_gate() -> int:
    """Prove the C7 gate on THIS tree against a THROWAWAY ledger -- a MARK_SHIPPED with
    no bundle reference must be refused. Never touches the real ledger (the module
    constants are swapped for temp paths and restored)."""
    saved = (code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER)
    saved_sync = code_queue._SYNC
    tmpdir = Path(tempfile.mkdtemp(prefix="cq-probe-"))
    try:
        code_queue._EVENT_LEDGER = tmpdir / "ledger.jsonl"
        code_queue._FINGERPRINT_LEDGER = tmpdir / "fp.jsonl"
        code_queue._SIGNALS_LEDGER = tmpdir / "sig.jsonl"
        code_queue._SYNC = True
        cid = code_queue.seed_item(kind="bug", severity="P2", title="probe-gate fixture",
                                   summary="throwaway", entity="F3E", signal="explicit",
                                   status="APPROVED")
        if not cid:
            print("PROBE: could not seed the throwaway item"); return 1
        outcome, msg = code_queue.process_queue_action(code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID)
        if outcome != "refused":
            print(f"PROBE FAILED: MARK_SHIPPED without a bundle reference returned {outcome!r}: {_ascii(msg)}")
            return 1
        outcome2, msg2 = code_queue.process_queue_action(
            code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID, bundle_id=BUNDLE_ID, branch=BRANCH)
        if outcome2 != "shipped":
            print(f"PROBE FAILED: MARK_SHIPPED WITH a bundle reference returned {outcome2!r}: {_ascii(msg2)}")
            return 1
        print(f"PROBE OK: no-bundle MARK_SHIPPED refused ({_ascii(msg)[:90]}...); with bundle_id={BUNDLE_ID} -> shipped")
        return 0
    finally:
        code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER = saved
        code_queue._SYNC = saved_sync
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Perform the transitions. Omitted = report only.")
    ap.add_argument("--probe-gate", action="store_true",
                    help="Prove the C7 gate against a throwaway ledger; writes nothing real.")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    if args.probe_gate:
        return probe_gate()

    commit = _head_commit()
    print(f"Step 7.5 -- Code #13 ({'APPLY' if args.apply else 'DRY-RUN'}) "
          f"bundle_id={BUNDLE_ID} branch={BRANCH} commit={commit or '?'}\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        blocker = _gate(PRECONDITIONS.get(cq_id, lambda: None))
        if blocker:
            print(f"  BLOCKED  {cq_id}  {label}  -> {blocker}")
            rc = 1
            continue
        rec = code_queue.get_item(cq_id)
        if rec is None:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> missing id (no queue item)")
            rc = 1
            continue
        status = str(rec.get("status", ""))
        if status == "SHIPPED":
            print(f"  SKIP (already shipped)  {cq_id}  {label}")
            continue
        if status in _TERMINAL_NOT_SHIPPABLE:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> status is {status}; refusing to flip a terminal row")
            rc = 1
            continue
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status}; bundle_id={BUNDLE_ID})")
            continue
        try:
            outcome, msg = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID,
                bundle_id=BUNDLE_ID, branch=BRANCH, commit=commit)
            if outcome == "shipped":
                print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}")
            else:
                print(f"  NOT SHIPPED  {cq_id}  {label}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1

    for cq_id, note in LEFT_OPEN.items():
        rec = code_queue.get_item(cq_id)
        print(f"  LEFT OPEN  {cq_id}  ({rec.get('status') if rec else 'MISSING'})  -> {note}")
    for cq_id in NOT_TOUCHED_RIDER_BUNDLE:
        rec = code_queue.get_item(cq_id)
        print(f"  NOT TOUCHED  {cq_id}  ({rec.get('status') if rec else 'MISSING'})  -> code-identity-v1 has its own 7.5")

    for cq_id, note in NOTED.items():
        rec = code_queue.get_item(cq_id)
        if rec is None:
            print(f"  NOT NOTED  {cq_id}  -> missing id")
            rc = 1
            continue
        if not args.apply:
            print(f"  [dry-run] would record a provenance note on  {cq_id}  (status {rec.get('status')})")
            continue
        rec_err = _provenance(cq_id, note, commit)
        print(f"  NOTED  {cq_id}  (status {rec.get('status')})"
              + (f"  [provenance record FAILED: {rec_err}]" if rec_err else ""))
        if rec_err:
            rc = 1

    for cq_id, evidence in DISMISSED.items():
        rec = code_queue.get_item(cq_id)
        if rec is None:
            print(f"  NOT DISMISSED  {cq_id}  -> missing id")
            rc = 1
            continue
        status = str(rec.get("status", ""))
        if status == "DISMISSED":
            print(f"  SKIP (already dismissed)  {cq_id}")
            continue
        if status in ("SHIPPED", "SUPERSEDED"):
            print(f"  NOT DISMISSED  {cq_id}  -> status is {status}; refusing to flip a terminal row")
            rc = 1
            continue
        sharers = _shared_prompt_sharers(cq_id)
        share_note = (f"  [prompt file shared with {', '.join(sharers)} -- NOT struck]" if sharers else "")
        if not args.apply:
            print(f"  [dry-run] would DISMISS with evidence  {cq_id}  (status {status}){share_note}\n"
                  f"             evidence: {evidence}")
            continue
        try:
            outcome, msg = code_queue.dismiss_with_evidence(cq_id, HARRISON_ID, evidence)
            if outcome == "dismissed":
                rec_err = _provenance(cq_id, "DISMISSED (evidence-attached)", commit)
                print(f"  DISMISSED  {cq_id}  -> {_ascii(msg)[:120]}{share_note}"
                      + (f"  [provenance record FAILED: {rec_err}]" if rec_err else ""))
                if rec_err:
                    rc = 1
            else:
                print(f"  NOT DISMISSED  {cq_id}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {cq_id}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1

    for loser, (winner, label) in SUPERSEDED.items():
        blocker = _gate(PRECONDITIONS.get(loser, lambda: None))
        if blocker:
            print(f"  BLOCKED  {loser}  supersede by {winner}  -> {blocker}")
            rc = 1
            continue
        rec = code_queue.get_item(loser)
        if rec is None:
            print(f"  NOT SUPERSEDED  {loser}  -> missing id")
            rc = 1
            continue
        status = str(rec.get("status", ""))
        if status == "SUPERSEDED":
            print(f"  SKIP (already superseded)  {loser}  by {rec.get('superseded_by', '?')}")
            continue
        if status in ("SHIPPED", "DISMISSED"):
            print(f"  NOT SUPERSEDED  {loser}  -> status is {status}; refusing to flip a terminal row")
            rc = 1
            continue
        if not args.apply:
            print(f"  [dry-run] would mark SUPERSEDED  {loser}  by {winner}  ({label}; status {status})")
            continue
        try:
            if code_queue.supersede_item(loser, winner):
                rec_err = _provenance(loser, f"SUPERSEDED by {winner}", commit)
                print(f"  SUPERSEDED  {loser}  by {winner}  ({label})"
                      + (f"  [provenance record FAILED: {rec_err}]" if rec_err else ""))
                if rec_err:
                    rc = 1
            else:
                print(f"  NOT SUPERSEDED  {loser}  -> supersede_item returned False")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {loser}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
