"""Tests for the org-roles-driven daily briefing (Org Synthesis Phase 2, deliverable 2).

Coverage:
  - roster: registry-driven parity (every active registry user with a Slack ID
    gets a briefing built), externals + registry-only excluded, fail-closed
  - section composition: reuses the plate builders from tool_dispatch; LEX
    users never get a pipeline section; stalled decisions are Harrison-only;
    sections fail soft to stub lines
  - review-driven rollout (DEFAULT, Harrison 2026-06-11): one review DM PER
    USER to Harrison; his :+1: on a message enables that user's delivery at
    the next run; :-1: drops the user from review; nobody else's reactions
    count; no unsolicited DMs before a thumbs-up
  - send mode: --send-users force-delivers to all active registry users
  - retirement of role-briefing-config.yaml (old config path is gone)
  - sub-entity canonicalization in the SHARED plate task builder (LEX-LLC
    scopes to LEX, never unfiltered)

Doctrine: direct `sys.path + import mod` (NOT spec_from_file_location) so
patch.object(rdb, ...) intercepts module-global lookups.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import org_roles  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

import run_daily_briefing as rdb  # noqa: E402

# ---------------------------------------------------------------------------
# Registry + state fixtures
# ---------------------------------------------------------------------------

_HARRISON = "U0B2RM2JYJ1"  # must match tool_dispatch._HARRISON_SLACK_ID

_SAMPLE_REGISTRY = """
users:
  - slack_id: U0B2RM2JYJ1
    name: Harrison Rogers
    role: Founder / CEO
    entity: FNDR
    responsibilities:
      - Portfolio strategy
  - slack_id: U101
    name: Tara Sales
    role: F3E Sales Lead
    entity: F3E
    responsibilities:
      - Retail pipeline
  - slack_id: U102
    name: Lana Lex
    role: LLC Office Manager
    entity: LEX-LLC
  - slack_id: UEXT
    name: Gene Guest
    role: Outside Consultant
    entity: F3E
    external: true
  - name: Reggie RegistryOnly
    role: Part-time EA
    entity: HJRG
"""


@pytest.fixture
def registry(tmp_path):
    p = tmp_path / "org-roles.yaml"
    p.write_text(_SAMPLE_REGISTRY, encoding="utf-8")
    with patch.object(org_roles, "_ROLES_PATH", p):
        org_roles.invalidate_cache()
        yield p
    org_roles.invalidate_cache()


@pytest.fixture
def empty_registry(tmp_path):
    p = tmp_path / "org-roles.yaml"
    p.write_text("users: []\n", encoding="utf-8")
    with patch.object(org_roles, "_ROLES_PATH", p):
        org_roles.invalidate_cache()
        yield p
    org_roles.invalidate_cache()


@pytest.fixture
def state_path(tmp_path):
    p = tmp_path / "briefing-delivery.json"
    with patch.object(rdb, "_DELIVERY_STATE_PATH", p):
        yield p


@pytest.fixture(autouse=True)
def _isolate_run_lock(tmp_path):
    """Every test gets its own daily-briefing lock path -- the real
    data/state/daily-briefing.lock is never touched and tests never contend."""
    with patch.object(rdb, "_RUN_LOCK_PATH", tmp_path / "daily-briefing.lock"):
        yield


def _read_state(p: Path) -> dict:
    if not p.exists():
        return {"enabled": {}, "declined": {}, "pending_reviews": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _seed_state(p: Path, **kw) -> None:
    state = {"enabled": {}, "declined": {}, "pending_reviews": []}
    state.update(kw)
    p.write_text(json.dumps(state), encoding="utf-8")


def _mock_slack(reactions=None):
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "DM-CHAN"}}
    client.chat_postMessage.return_value = {"ok": True, "ts": "1700000000.000100"}
    client.reactions_get.return_value = {"message": {"reactions": reactions or []}}
    return client


_ENV = {"SLACK_BOT_TOKEN": "xoxb-test", "ANTHROPIC_API_KEY": "key"}


def _run_main(argv, client):
    with patch.dict("os.environ", _ENV), \
         patch.object(rdb, "SlackWebClient", return_value=client), \
         patch.object(rdb, "build_user_briefing",
                      side_effect=lambda rec, **k: f"BRIEF::{rec.name}"), \
         patch.object(rdb, "_write_audit", return_value=None), \
         patch.object(rdb.time, "sleep", return_value=None):
        return rdb.main(argv)


def _posts(client) -> list[str]:
    return [c.kwargs["text"] for c in client.chat_postMessage.call_args_list]


# ---------------------------------------------------------------------------
# Roster (registry-driven config parity)
# ---------------------------------------------------------------------------

class TestRoster:
    def test_every_active_registry_user_with_slack_id_included(self, registry):
        roster, _ = rdb._load_briefing_roster()
        names = {r.name for r in roster}
        assert names == {"Harrison Rogers", "Tara Sales", "Lana Lex"}

    def test_external_excluded_from_delivery(self, registry):
        roster, excluded = rdb._load_briefing_roster()
        assert all(r.name != "Gene Guest" for r in roster)
        assert any("Gene Guest" in n and "external" in n for n in excluded)

    def test_registry_only_excluded_from_delivery(self, registry):
        roster, excluded = rdb._load_briefing_roster()
        assert all(r.name != "Reggie RegistryOnly" for r in roster)
        assert any("Reggie RegistryOnly" in n and "registry-only" in n for n in excluded)

    def test_empty_registry_yields_no_recipients(self, empty_registry):
        roster, excluded = rdb._load_briefing_roster()
        assert roster == []
        assert excluded == []

    def test_unknown_users_skipped_by_construction(self, registry):
        # The registry IS the roster -- there is no merge with any other map,
        # so a user absent from org-roles.yaml can never receive a briefing.
        roster, _ = rdb._load_briefing_roster()
        assert all(r.slack_id for r in roster)
        assert {r.slack_id for r in roster} <= {_HARRISON, "U101", "U102"}


# ---------------------------------------------------------------------------
# Section composition (shared plate builders)
# ---------------------------------------------------------------------------

class TestComposeSections:
    def test_role_header_and_lanes(self, registry):
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None):
            out = rdb._compose_sections(rec)
        assert "ROLE" in out
        assert "Tara Sales -- F3E Sales Lead (F3E)" in out
        assert "Lanes: Retail pipeline" in out
        assert "OPEN TASKS\nT" in out
        assert "CALENDAR\nC" in out

    def test_harrison_gets_stalled_decisions(self, registry):
        rec = org_roles.get_role(_HARRISON)
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None), \
             patch.object(rdb, "_tool_fndr_open_decisions", return_value="DEC") as dec:
            out = rdb._compose_sections(rec)
        assert "STALLED DECISIONS\nDEC" in out
        dec.assert_called_once()

    def test_non_harrison_never_gets_stalled_decisions(self, registry):
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None), \
             patch.object(rdb, "_tool_fndr_open_decisions") as dec:
            out = rdb._compose_sections(rec)
        assert "STALLED DECISIONS" not in out
        dec.assert_not_called()

    def test_lex_user_never_gets_pipeline_section(self, registry):
        # Uses the REAL shared _plate_hubspot_section: LEX scope (incl.
        # sub-entities) returns None before any map/network access.
        rec = org_roles.get_role("U102")
        assert rec.entity == "LEX-LLC"
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"):
            out = rdb._compose_sections(rec)
        assert "DEAL PIPELINE" not in out

    def test_pipeline_section_present_for_owner(self, registry):
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value="DEALS"):
            out = rdb._compose_sections(rec)
        assert "DEAL PIPELINE\nDEALS" in out

    def test_section_crash_degrades_to_stub(self, registry):
        # Real _safe_plate_section wraps the patched (crashing) builder.
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", side_effect=RuntimeError("boom")), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None):
            out = rdb._compose_sections(rec)
        assert "(Open tasks section unavailable right now.)" in out

    def test_pipeline_crash_degrades_to_stub(self, registry):
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T"), \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", side_effect=RuntimeError("boom")):
            out = rdb._compose_sections(rec)
        assert "DEAL PIPELINE\n(Deal pipeline section unavailable right now.)" in out

    def test_brief_opts_into_stale_task_filter(self, registry):
        # N7 / Harrison #1: the brief passes its stale-overdue threshold to the
        # SHARED task builder so abandoned 2025 goal tasks stop surfacing daily.
        rec = org_roles.get_role("U101")
        with patch.object(rdb, "_plate_asana_section", return_value="T") as asana, \
             patch.object(rdb, "_plate_calendar_section", return_value="C"), \
             patch.object(rdb, "_plate_hubspot_section", return_value=None):
            rdb._compose_sections(rec)
        assert asana.call_args.args[-1] == rdb._BRIEFING_STALE_OVERDUE_DAYS


# ---------------------------------------------------------------------------
# Review mode (DEFAULT -- one DM per user to Harrison)
# ---------------------------------------------------------------------------

class TestReviewMode:
    def test_default_sends_one_message_per_user_to_harrison_only(self, registry, state_path):
        client = _mock_slack()
        rc = _run_main([], client)
        assert rc == 0
        # ONE DM conversation opened -- Harrison's (no user deliveries yet)
        client.conversations_open.assert_called_once_with(users=[_HARRISON])
        posts = _posts(client)
        # header + one message per active registry user
        assert len(posts) == 1 + 3
        assert "DAILY BRIEFING REVIEW" in posts[0]
        per_user = "\n".join(posts[1:])
        for name in ("Harrison Rogers", "Tara Sales", "Lana Lex"):
            assert f"WOULD-BE BRIEFING -- {name}" in per_user
            assert f"BRIEF::{name}" in per_user

    def test_review_messages_carry_reaction_instructions(self, registry, state_path):
        client = _mock_slack()
        _run_main([], client)
        for msg in _posts(client)[1:]:
            assert ":+1:" in msg
            assert ":-1:" in msg

    def test_header_carries_rollout_state(self, registry, state_path):
        client = _mock_slack()
        _run_main([], client)
        header = _posts(client)[0]
        assert "nobody yet" in header
        assert "Gene Guest" in header          # external, named as excluded
        assert "Reggie RegistryOnly" in header  # registry-only, named as excluded

    def test_each_review_message_tracked_for_reactions(self, registry, state_path):
        client = _mock_slack()
        _run_main([], client)
        state = _read_state(state_path)
        sids = {p["sid"] for p in state["pending_reviews"]}
        assert sids == {_HARRISON, "U101", "U102"}
        assert all(p["ts"] for p in state["pending_reviews"])

    def test_rerun_replaces_pending_not_accumulates(self, registry, state_path):
        client = _mock_slack()
        _run_main([], client)
        _run_main([], client)
        state = _read_state(state_path)
        assert len(state["pending_reviews"]) == 3  # one per user, latest only

    def test_digest_only_flag_forces_review_for_everyone(self, registry, state_path):
        _seed_state(state_path, enabled={"U101": {"name": "Tara Sales"}})
        client = _mock_slack()
        rc = _run_main(["--digest-only"], client)
        assert rc == 0
        # Even the enabled user goes to review; no user DMs opened
        client.conversations_open.assert_called_once_with(users=[_HARRISON])
        assert len(_posts(client)) == 1 + 3

    def test_build_failure_lands_in_review_and_returns_2(self, registry, state_path):
        client = _mock_slack()

        def _build(rec, **k):
            if rec.name == "Lana Lex":
                raise RuntimeError("asana down")
            return f"BRIEF::{rec.name}"

        with patch.dict("os.environ", _ENV), \
             patch.object(rdb, "SlackWebClient", return_value=client), \
             patch.object(rdb, "build_user_briefing", side_effect=_build), \
             patch.object(rdb, "_write_audit", return_value=None), \
             patch.object(rdb.time, "sleep", return_value=None):
            rc = rdb.main([])
        assert rc == 2
        joined = "\n".join(_posts(client))
        assert "(briefing could not be built" in joined
        assert "BRIEF::Tara Sales" in joined  # other users still present

    def test_empty_registry_returns_0_sends_nothing(self, empty_registry, state_path):
        client = _mock_slack()
        rc = _run_main([], client)
        assert rc == 0
        client.chat_postMessage.assert_not_called()


# ---------------------------------------------------------------------------
# Reaction-driven enablement (Harrison's :+1: / :-1:)
# ---------------------------------------------------------------------------

class TestReactionEnablement:
    def _pending(self, sid, name):
        return [{"sid": sid, "name": name, "channel": "DM-CHAN",
                 "ts": "1.1", "sent_at": 1_700_000_000.0}]

    def test_thumbs_up_enables_and_delivers_on_this_run(self, registry, state_path):
        _seed_state(state_path, pending_reviews=self._pending("U101", "Tara Sales"))
        client = _mock_slack(reactions=[{"name": "+1", "users": [_HARRISON]}])
        rc = _run_main([], client)
        assert rc == 0
        state = _read_state(state_path)
        assert "U101" in state["enabled"]
        # Tara got her OWN DM; the other two went to Harrison for review
        opened = [c.kwargs["users"][0] for c in client.conversations_open.call_args_list]
        assert "U101" in opened and _HARRISON in opened
        joined = "\n".join(_posts(client))
        assert "WOULD-BE BRIEFING -- Tara Sales" not in joined  # not in review anymore
        assert "BRIEF::Tara Sales" in joined                    # delivered directly
        assert "Delivered live this run: Tara Sales" in _posts(client)[1]  # header after her DM

    def test_thumbs_down_declines_and_excludes(self, registry, state_path):
        _seed_state(state_path, pending_reviews=self._pending("U102", "Lana Lex"))
        client = _mock_slack(reactions=[{"name": "-1", "users": [_HARRISON]}])
        builds = []
        with patch.dict("os.environ", _ENV), \
             patch.object(rdb, "SlackWebClient", return_value=client), \
             patch.object(rdb, "build_user_briefing",
                          side_effect=lambda rec, **k: builds.append(rec.name) or f"B::{rec.name}"), \
             patch.object(rdb, "_write_audit", return_value=None), \
             patch.object(rdb.time, "sleep", return_value=None):
            rc = rdb.main([])
        assert rc == 0
        state = _read_state(state_path)
        assert "U102" in state["declined"]
        assert "Lana Lex" not in builds  # declined users are not even built
        header = next(p for p in _posts(client) if "DAILY BRIEFING REVIEW" in p)
        assert "Declined" in header and "Lana Lex" in header

    def test_other_users_reactions_are_ignored(self, registry, state_path):
        _seed_state(state_path, pending_reviews=self._pending("U101", "Tara Sales"))
        client = _mock_slack(reactions=[{"name": "+1", "users": ["U_SOMEONE_ELSE"]}])
        _run_main([], client)
        state = _read_state(state_path)
        assert state["enabled"] == {}
        # Still pending (re-tracked from this run's fresh review message)
        assert any(p["sid"] == "U101" for p in state["pending_reviews"])

    def test_thumbs_up_wins_over_thumbs_down(self, registry, state_path):
        client = _mock_slack(reactions=[
            {"name": "-1", "users": [_HARRISON]},
            {"name": "+1", "users": [_HARRISON]},
        ])
        assert rdb._harrison_verdict(client, "DM-CHAN", "1.1") == "up"

    def test_verdict_none_when_no_reactions(self):
        client = _mock_slack(reactions=[])
        assert rdb._harrison_verdict(client, "DM-CHAN", "1.1") is None

    def test_verdict_fail_soft_on_api_error(self):
        from slack_sdk.errors import SlackApiError
        client = MagicMock()
        client.reactions_get.side_effect = SlackApiError("boom", {"error": "x"})
        assert rdb._harrison_verdict(client, "DM-CHAN", "1.1") is None

    def test_enabled_user_skips_review_on_subsequent_runs(self, registry, state_path):
        _seed_state(state_path, enabled={"U101": {"name": "Tara Sales"}})
        client = _mock_slack()
        _run_main([], client)
        posts = _posts(client)
        review = "\n".join(p for p in posts if "WOULD-BE BRIEFING" in p)
        assert "Tara Sales" not in review
        opened = [c.kwargs["users"][0] for c in client.conversations_open.call_args_list]
        assert "U101" in opened


# ---------------------------------------------------------------------------
# Send mode (force-deliver to all)
# ---------------------------------------------------------------------------

class TestSendMode:
    def test_send_users_dms_each_active_registry_user(self, registry, state_path):
        client = _mock_slack()
        rc = _run_main(["--send-users"], client)
        assert rc == 0
        opened = sorted(c.kwargs["users"][0] for c in client.conversations_open.call_args_list)
        assert opened == sorted([_HARRISON, "U101", "U102"])
        # 3 user DMs, no review header
        assert len(_posts(client)) == 3
        assert all("DAILY BRIEFING REVIEW" not in p for p in _posts(client))

    def test_user_filter_limits_processing(self, registry, state_path):
        client = _mock_slack()
        rc = _run_main(["--send-users", "--user", "tara"], client)
        assert rc == 0
        client.conversations_open.assert_called_once_with(users=["U101"])

    def test_flags_mutually_exclusive(self, registry, state_path):
        with pytest.raises(SystemExit):
            rdb.main(["--digest-only", "--send-users"])


# ---------------------------------------------------------------------------
# Dry run + env guards
# ---------------------------------------------------------------------------

class TestDryRunAndEnv:
    def test_dry_run_sends_nothing_and_keeps_state(self, registry, state_path, capsys):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "", "ANTHROPIC_API_KEY": "key"}), \
             patch.object(rdb, "SlackWebClient") as slack_cls, \
             patch.object(rdb, "build_user_briefing",
                          side_effect=lambda rec, **k: f"BRIEF::{rec.name}"):
            rc = rdb.main(["--dry-run"])
        assert rc == 0
        slack_cls.assert_not_called()
        assert not state_path.exists()  # no state mutation on dry runs
        out = capsys.readouterr().out
        assert "BRIEF::Tara Sales" in out

    def test_missing_anthropic_key_returns_1(self, registry, state_path):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "ANTHROPIC_API_KEY": ""}):
            assert rdb.main([]) == 1

    def test_missing_slack_token_returns_1_when_sending(self, registry, state_path):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "", "ANTHROPIC_API_KEY": "key"}):
            assert rdb.main([]) == 1


# ---------------------------------------------------------------------------
# Retirement of role-briefing-config.yaml (D-044 item 5 consolidation point)
# ---------------------------------------------------------------------------

class TestOldConfigRetired:
    def test_module_has_no_role_config_path(self):
        assert not hasattr(rdb, "_ROLE_CONFIG")
        assert not hasattr(rdb, "_load_role_config")

    def test_source_never_references_old_config(self):
        src = Path(rdb.__file__).read_text(encoding="utf-8")
        assert "role-briefing-config" not in src

    def test_old_config_file_deleted_from_repo(self):
        assert not (_REPO_ROOT / "data" / "maps" / "role-briefing-config.yaml").exists()

    def test_roster_reads_org_roles_registry(self, registry):
        # The roster is served by org_roles (60s TTL live-reload) -- edit the
        # registry, invalidate, and the roster follows with no other config.
        roster, _ = rdb._load_briefing_roster()
        assert {r.slack_id for r in roster} == {_HARRISON, "U101", "U102"}


# ---------------------------------------------------------------------------
# Sub-entity canonicalization in the SHARED plate task builder
# ---------------------------------------------------------------------------

class TestSharedBuilderSubEntityScope:
    def _tasks(self):
        return [
            {
                "name": "LEX task A",
                "permalink_url": "https://app.asana.com/1",
                "memberships": [{"project": {"name": "[LEX] Operations"}}],
                "projects": [],
            },
            {
                "name": "OSN task B",
                "permalink_url": "https://app.asana.com/2",
                "memberships": [{"project": {"name": "[OSN] Store Ops"}}],
                "projects": [],
            },
        ]

    def test_lex_llc_scopes_to_lex_not_unfiltered(self):
        with patch.object(td, "_load_slack_asana_map",
                          return_value={"U_X": {"asana_user_gid": "111"}}), \
             patch.object(td.asana_client, "get_user_tasks", return_value=self._tasks()):
            out = td._plate_asana_section("U_X", "LEX-LLC")
        assert "LEX task A" in out
        assert "OSN task B" not in out

    def test_osn_substore_scopes_to_osn(self):
        with patch.object(td, "_load_slack_asana_map",
                          return_value={"U_X": {"asana_user_gid": "111"}}), \
             patch.object(td.asana_client, "get_user_tasks", return_value=self._tasks()):
            out = td._plate_asana_section("U_X", "OSNGW")
        assert "OSN task B" in out
        assert "LEX task A" not in out


# ---------------------------------------------------------------------------
# Time budget + run-audit visibility (2026-06-12 morning-failure fixes)
# ---------------------------------------------------------------------------

class TestTimeBudgetAndRunAudit:
    """The 6/12 incident: the task was killed at its ExecutionTimeLimit
    mid-run (recon KB contention) with zero users finished and NO trace in the
    audit log. Now: a start-of-run line makes terminations visible (run_start
    with no run_end), and a build-loop budget degrades gracefully instead of
    being killed."""

    def _run_with_audit(self, argv, client):
        audit_entries: list[dict] = []
        with patch.dict("os.environ", _ENV), \
             patch.object(rdb, "SlackWebClient", return_value=client), \
             patch.object(rdb, "build_user_briefing",
                          side_effect=lambda rec, **k: f"BRIEF::{rec.name}") as build, \
             patch.object(rdb, "_write_audit",
                          side_effect=lambda entries: audit_entries.extend(entries)), \
             patch.object(rdb.time, "sleep", return_value=None):
            rc = rdb.main(argv)
        return rc, build, audit_entries

    def test_zero_budget_skips_all_users_gracefully(self, registry):
        client = _mock_slack()
        rc, build, audit = self._run_with_audit(["--time-budget-min=-1"], client)
        assert rc == 2
        build.assert_not_called()
        skips = [e for e in audit
                 if e.get("error") == "skipped: build time budget exhausted"]
        assert len(skips) == 3  # the whole roster, each visible in the audit log

    def test_run_start_line_written_before_any_build(self, registry):
        client = _mock_slack()
        _, _, audit = self._run_with_audit(["--time-budget-min=-1"], client)
        events = [e.get("event") for e in audit if e.get("event")]
        assert events[0] == "run_start"
        assert events[-1] == "run_end"

    def test_normal_run_has_start_and_end_with_elapsed(self, registry):
        client = _mock_slack()
        rc, build, audit = self._run_with_audit([], client)
        assert rc == 0
        assert build.call_count == 3
        start = [e for e in audit if e.get("event") == "run_start"]
        end = [e for e in audit if e.get("event") == "run_end"]
        assert len(start) == 1 and len(end) == 1
        assert "elapsed_s" in end[0]
        assert end[0]["roster"] == 3

    def test_dry_run_also_writes_run_end(self, registry):
        client = _mock_slack()
        rc, _, audit = self._run_with_audit(["--dry-run"], client)
        assert rc == 0
        end = [e for e in audit if e.get("event") == "run_end"]
        assert len(end) == 1 and end[0]["dry_run"] is True


# ---------------------------------------------------------------------------
# Double-fire single-instance lock (N7)
# ---------------------------------------------------------------------------

class TestDoubleFireLock:
    """The brief was seen firing twice minutes apart. The automated delivery
    paths take a single-instance lock; a concurrent invocation is a clean
    no-op. --dry-run / --user / --force bypass the lock."""

    def _hold_lock(self):
        rdb._RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        rdb._RUN_LOCK_PATH.write_text("99999 held", encoding="utf-8")

    def test_concurrent_run_is_noop(self, registry, state_path):
        self._hold_lock()
        client = _mock_slack()
        rc = _run_main([], client)
        assert rc == 0
        client.chat_postMessage.assert_not_called()
        client.conversations_open.assert_not_called()

    def test_locked_noop_writes_audit_line(self, registry, state_path):
        self._hold_lock()
        audit: list[dict] = []
        client = _mock_slack()
        with patch.dict("os.environ", _ENV), \
             patch.object(rdb, "SlackWebClient", return_value=client), \
             patch.object(rdb, "_write_audit", side_effect=lambda e: audit.extend(e)):
            rc = rdb.main([])
        assert rc == 0
        assert any(x.get("event") == "run_skipped_locked" for x in audit)

    def test_lock_released_after_normal_run(self, registry, state_path):
        client = _mock_slack()
        _run_main([], client)
        assert not rdb._RUN_LOCK_PATH.exists()

    def test_force_bypasses_lock(self, registry, state_path):
        self._hold_lock()
        client = _mock_slack()
        rc = _run_main(["--force"], client)
        assert rc == 0
        assert client.chat_postMessage.call_count >= 1  # ran despite the held lock

    def test_dry_run_ignores_lock(self, registry, state_path):
        self._hold_lock()
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "", "ANTHROPIC_API_KEY": "key"}), \
             patch.object(rdb, "build_user_briefing", side_effect=lambda rec, **k: f"B::{rec.name}"):
            rc = rdb.main(["--dry-run"])
        assert rc == 0  # dry-run never takes the lock

    def test_acquire_then_block_then_release(self):
        assert rdb._acquire_run_lock() is True
        assert rdb._RUN_LOCK_PATH.exists()
        assert rdb._acquire_run_lock() is False   # concurrent invocation blocked
        rdb._release_run_lock()
        assert not rdb._RUN_LOCK_PATH.exists()
        assert rdb._acquire_run_lock() is True     # next run reacquires
        rdb._release_run_lock()

    def test_stale_lock_is_reclaimed(self):
        import os as _os
        import time as _time
        rdb._RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        assert rdb._acquire_run_lock() is True
        old = _time.time() - (rdb._RUN_LOCK_STALE_SECONDS + 60)
        _os.utime(rdb._RUN_LOCK_PATH, (old, old))
        assert rdb._acquire_run_lock() is True     # stale lock cleared + reacquired
        rdb._release_run_lock()


# ---------------------------------------------------------------------------
# Time-budget alignment + pipeline-snapshot framing (1.7c)
# ---------------------------------------------------------------------------

class TestBudgetAndPrompt:
    def test_default_time_budget_fits_task_limit(self):
        # The default self-budget MUST be under the live 10-min task
        # ExecutionTimeLimit so the script self-bounds before a SIGKILL.
        assert rdb._parse_args([]).time_budget_min < 10.0

    def test_setup_script_keeps_budget_under_task_limit(self):
        # When the task is re-registered, the passed budget (18) must stay
        # under the bumped ExecutionTimeLimit (20).
        src = (_REPO_ROOT / "deployment" / "setup-daily-briefing-task.ps1").read_text(
            encoding="utf-8"
        )
        assert "--time-budget-min 18" in src
        assert "New-TimeSpan -Minutes 20" in src

    def test_prompt_frames_pipeline_as_snapshot(self, registry):
        rec = org_roles.get_role("U101")
        prompt = rdb._build_briefing_prompt(rec, "SECTIONS", "CONTEXT", "Monday")
        assert "open-pipeline snapshot" in prompt
        assert "gain or decline" in prompt
        # regression: existing guardrails survive the extraction
        assert "Do NOT add financial figures" in prompt
        assert "Good morning, Tara!" in prompt
