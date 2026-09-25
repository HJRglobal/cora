"""Code #15 S2 (cq-22b84598aee8) -- the knowledge-review gap executor's asana_task
branch, and the mechanical card's plan + possible-duplicate lines.

THE INCIDENT. `create_task(name, notes)` with no project and no assignee: 48
creates, 32 still open on 2026-09-24, every one projects=[] / assignee=null.
These tests pin: a real project + a real assignee or a VISIBLE refusal; tier-A
repeats refused at EXECUTION time (most live cards were rendered before any of
this and will never be re-rendered); apply-first-then-resolve with a named reason;
a transient Asana failure left PENDING; zero writes on a dry run.
"""
from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.run_knowledge_review as rkr
from cora import gap_task_dedup as gtd
from cora import knowledge_review as kr

HARRISON = "U0B2RM2JYJ1"
OWNER_SLACK = {"HJRP": "U0HANNAH01", "OSN": "U0MATT0001", "F3E": "U0TOMMY001"}
OWNER_ASANA = {"U0HANNAH01": "1209060959783860", "U0MATT0001": "1211215949326709",
               "U0TOMMY001": "1213638047870465"}
PROJECT = {"HJRP": "1216928758714643", "OSN": "1215470834881074", "F3E": "1215470928454227"}

CASH = "Monitor cash position and assess inflow stabilization"
CASH_VARIANT = "Monitor cash position and assess inflow stabilization ($33,487 as of 2026-08-27)"


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated ledgers + deterministic project/owner config + captured posts."""
    live = tmp_path / "proposed.jsonl"
    arch = tmp_path / "proposed.archive.jsonl"
    live.write_text("", encoding="utf-8")
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", live)
    monkeypatch.setattr(kr, "_ARCHIVE_PATH", arch)
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
    kr._SEEN_IDS_CACHE = None
    kr._ARCHIVE_IDS_CACHE = None
    led = tmp_path / "gap-ledger.jsonl"
    monkeypatch.setenv("GAP_TASK_FP_PATH", str(led))

    from cora.tools import project_resolver as pr
    from cora import gap_autofill
    from cora.tools import user_identity
    monkeypatch.setattr(pr, "resolve_project", lambda entity, task_text="", **k: PROJECT.get(entity))
    monkeypatch.setattr(pr, "entity_catch_all", lambda entity: PROJECT.get(entity))
    monkeypatch.setattr(pr, "is_blocked_project", lambda gid: False)
    monkeypatch.setattr(gap_autofill, "resolve_owner", lambda entity: OWNER_SLACK.get(entity) or HARRISON)
    monkeypatch.setattr(user_identity, "asana_gid", lambda sid: OWNER_ASANA.get(sid))
    monkeypatch.setattr(user_identity, "display_name", lambda sid: f"Name-{sid[-4:]}")
    monkeypatch.setattr(gtd, "_owner_source", lambda entity: "owner")

    from cora.tools import asana_client
    created: list[dict] = []

    def _create(**kw):
        created.append(kw)
        gid = f"12190000000{len(created):05d}"
        return {"gid": gid, "permalink_url": f"https://app.asana.com/1/682743441507584/task/{gid}",
                "projects": [{"gid": kw.get("project_gid"), "name": "[X] Operations - General"}],
                "assignee": {"gid": kw.get("assignee_gid"), "name": "Owner Person"}}
    monkeypatch.setattr(asana_client, "create_task", _create)
    scan = MagicMock(return_value=[])
    monkeypatch.setattr(asana_client, "get_project_tasks", scan)
    posts: list[str] = []
    monkeypatch.setattr(rkr, "_post_to_slack", lambda tok, ch, text: posts.append(text))
    return {"live": live, "ledger": led, "created": created, "posts": posts, "scan": scan,
            "tmp": tmp_path}


def _row(uid: str, ent: str, subject: str, *, days_ago: float = 1.0, payload=None,
         **extra) -> dict:
    r = {"update_id": f"pass5:drive:{uid}", "update_type": "asana_task",
         "description": f"[{ent}] Drive doc suggests missing task: {subject}",
         "payload": payload if payload is not None else {},
         "source_evidence": "Source: digest", "confidence": "HIGH", "state": "PENDING",
         "proposed_at": _iso(days_ago), "dm_message_ts": f"1.{uid}"}
    r.update(extra)
    return r


def _write_rows(env, *rows):
    env["live"].write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    kr._SEEN_IDS_CACHE = None


def _state(env, uid: str) -> tuple[str, str]:
    for ln in env["live"].read_text(encoding="utf-8").splitlines():
        e = json.loads(ln)
        if e["update_id"] == f"pass5:drive:{uid}":
            return e["state"], e.get("resolved_reason") or ""
    raise AssertionError("row missing")


def _exec(update, state=None):
    return rkr._execute_approved_update(update, "xoxb-test", logging.getLogger("t"),
                                        run_state=state)


# ── Part 1: a real project + a real assignee, or a visible refusal ───────────

def test_a_resolvable_entity_creates_with_project_AND_assignee(env):
    u = _row("a0000001", "HJRP", CASH)
    _write_rows(env, u)
    assert _exec(u) is True
    (kw,) = env["created"]
    assert kw["project_gid"] == PROJECT["HJRP"]
    assert kw["assignee_gid"] == OWNER_ASANA["U0HANNAH01"]
    assert kw["name"] == f"[HJRP] {CASH}"           # the ugly legacy prefix is gone
    assert "pass5:drive:a0000001" in kw["notes"]
    st, why = _state(env, "a0000001")
    assert st == "APPROVED" and why.startswith("executed:12190000000")
    assert "[X] Operations - General" in env["posts"][-1]
    assert "entity owner" in env["posts"][-1]
    # the ledger now proves the task exists
    rows = gtd.ledger_rows(persist=False)
    assert any(r["kind"] == "created" and r["ref"] == u["update_id"] for r in rows)


def test_bdm_has_no_project_and_is_REFUSED_visibly(env):
    u = _row("b0000001", "BDM", "Finalize the shot list for events")
    _write_rows(env, u)
    assert _exec(u) is False
    assert env["created"] == []
    assert _state(env, "b0000001") == ("DISMISSED", "refused:no_project:BDM")
    assert ":no_entry_sign:" in env["posts"][-1] and "no BDM Asana project" in env["posts"][-1]


def test_bdm_is_refused_against_the_REAL_project_map():
    """D-052: BDM has no Asana project config. Pinned against the live map, not a
    stub -- a hard-coded fallback project would silently re-open the orphan class.
    (No `env` fixture: the real project_resolver and map, read-only.)"""
    from cora.tools import project_resolver as pr
    pr.reload_map()
    plan = gtd.plan_create(_row("b0000002", "BDM", "Any BDM task"))
    assert plan.refusal == "no_project:BDM"
    assert plan.project_gid == ""


def test_lex_is_refused_before_anything_else(env):
    u = _row("c0000001", "LEX-LLC", "Anything")
    _write_rows(env, u)
    assert _exec(u) is False
    assert env["created"] == []
    assert _state(env, "c0000001") == ("DISMISSED", "refused:lex_out_of_scope")
    env["scan"].assert_not_called()


def test_an_unresolvable_entity_is_refused(env):
    u = _row("d0000001", "HJRP", "x")
    u["description"] = "Drive doc suggests missing task: no code at all"
    _write_rows(env, u)
    assert _exec(u) is False
    assert _state(env, "d0000001") == ("DISMISSED", "refused:entity_unresolved")


def test_no_mapped_assignee_is_refused(env, monkeypatch):
    from cora.tools import user_identity
    monkeypatch.setattr(user_identity, "asana_gid", lambda sid: None)
    u = _row("e0000001", "OSN", "Clarify meter coverage")
    _write_rows(env, u)
    assert _exec(u) is False
    assert env["created"] == []
    assert _state(env, "e0000001") == ("DISMISSED", "refused:no_assignee:OSN")


def test_the_default_owner_fallback_is_recorded_and_shown(env, monkeypatch):
    monkeypatch.setattr(gtd, "_owner_source", lambda entity: "default")
    u = _row("e0000002", "OSN", "Clarify meter coverage")
    _write_rows(env, u)
    assert _exec(u) is True
    assert "default owner" in env["posts"][-1]


# ── Part 2: tier-A repeats refused at execution ──────────────────────────────

def test_fixture_pair_the_orphan_create_is_now_a_refusal(env):
    """9/15: 1218516997344812 was created for the figure-drift variant of
    1218344765000648 (open at the time). Synthetic -- 1218344765000648 is
    COMPLETED live now, so an open-only check could not reproduce it."""
    gtd.record_created(update_id="pass5:drive:9d1b78bb", entity="HJRP", subject=CASH,
                       gid="1218344765000648",
                       url="https://app.asana.com/1/682743441507584/task/1218344765000648")
    u = _row("16975563", "HJRP", CASH_VARIANT)
    _write_rows(env, u)
    assert _exec(u) is False
    assert env["created"] == []                       # 1218516997344812 never exists
    assert _state(env, "16975563") == ("DISMISSED", "duplicate_of:1218344765000648")
    assert "task/1218344765000648" in env["posts"][-1]


def test_a_repeat_of_an_EARLIER_proposal_in_any_state_is_refused(env):
    """D-030 propose-once: an earlier proposal of the same task -- even one that
    was dismissed -- refuses the later approval."""
    early = _row("f0000001", "OSN", "Implement Whey Protein 20 lb pricing adjustment ($240 to $295)",
                 days_ago=10, state="DISMISSED")
    later = _row("f0000002", "OSN",
                 "Implement Whey Protein 20 lb pricing adjustment ($240 to $295) effective 8/28/26",
                 days_ago=2)
    _write_rows(env, early, later)
    assert gtd.ensure_bootstrapped() == 2
    assert _exec(later) is False
    assert env["created"] == []
    assert _state(env, "f0000002") == ("DISMISSED", "duplicate_of:pass5:drive:f0000001")


def test_the_EARLIER_of_two_pending_duplicates_still_creates(env):
    early = _row("f0000003", "OSN", "Set POS change date", days_ago=10)
    later = _row("f0000004", "OSN", "Set POS change date", days_ago=2)
    _write_rows(env, early, later)
    gtd.ensure_bootstrapped()
    assert _exec(early) is True                       # nothing EARLIER repeats it
    assert len(env["created"]) == 1
    assert _exec(later) is False                      # ... and now the task exists
    assert len(env["created"]) == 1


def test_two_sibling_approvals_in_one_run_create_ONE_task(env):
    a = _row("g0000001", "HJRP", CASH, days_ago=1)
    b = _row("g0000002", "HJRP", CASH_VARIANT, days_ago=1)
    _write_rows(env, a, b)
    state = rkr._new_gap_run_state()
    assert _exec(a, state) is True
    assert _exec(b, state) is False
    assert len(env["created"]) == 1
    assert _state(env, "g0000002")[1].startswith("duplicate_of:12190000000")


def test_in_run_set_holds_even_when_the_ledger_write_fails(env, monkeypatch):
    monkeypatch.setattr(gtd, "record_created", lambda **k: False)
    a = _row("g0000003", "HJRP", CASH)
    b = _row("g0000004", "HJRP", CASH_VARIANT)
    _write_rows(env, a, b)
    state = rkr._new_gap_run_state()
    _exec(a, state)
    _exec(b, state)
    assert len(env["created"]) == 1


def test_an_open_task_in_the_target_project_is_a_duplicate(env):
    env["scan"].return_value = [{"gid": "1218999999999999", "name": f"[HJRP] {CASH}",
                                 "permalink_url": "https://app.asana.com/0/0/1218999999999999"}]
    u = _row("h0000001", "HJRP", CASH_VARIANT)
    _write_rows(env, u)
    assert _exec(u) is False
    assert env["created"] == []
    assert _state(env, "h0000001") == ("DISMISSED", "duplicate_of:1218999999999999")
    env["scan"].assert_called_once_with(PROJECT["HJRP"], max_tasks=500)


def test_a_project_scan_failure_fails_OPEN_with_a_warning(env, caplog):
    from cora.tools.asana_client import AsanaClientError
    env["scan"].side_effect = AsanaClientError("Asana 403")
    u = _row("h0000002", "HJRP", CASH)
    _write_rows(env, u)
    caplog.set_level(logging.WARNING)
    assert _exec(u) is True
    assert len(env["created"]) == 1
    assert any("fail-open" in r.getMessage() for r in caplog.records)


def test_the_project_scan_runs_once_per_project_per_run(env):
    a = _row("h0000003", "HJRP", "Task one about invoices")
    b = _row("h0000004", "HJRP", "A totally different second chore")
    _write_rows(env, a, b)
    state = rkr._new_gap_run_state()
    _exec(a, state)
    _exec(b, state)
    assert env["scan"].call_count == 1


# ── apply-first-then-resolve ─────────────────────────────────────────────────

def test_a_transient_asana_error_leaves_the_row_PENDING(env, monkeypatch):
    from cora.tools import asana_client
    from cora.tools.asana_client import AsanaClientError

    def _boom(**kw):
        raise AsanaClientError("Asana 503 -- upstream error")
    monkeypatch.setattr(asana_client, "create_task", _boom)
    u = _row("i0000001", "HJRP", CASH)
    _write_rows(env, u)
    assert _exec(u) is False
    assert _state(env, "i0000001") == ("PENDING", "")
    assert "Left pending" in env["posts"][-1]
    # nothing was recorded as created, so the retry is not mistaken for a dup
    assert gtd.created_by(u["update_id"], gtd.ledger_rows(persist=False)) is None


def test_crash_recovery_a_row_that_already_created_is_resolved_not_recreated(env):
    """Asana answered, the resolve never landed (crash). The retry must not
    double-create -- and must not call its own task a duplicate."""
    u = _row("j0000001", "HJRP", CASH)
    _write_rows(env, u)
    gtd.record_created(update_id=u["update_id"], entity="HJRP", subject=CASH,
                       gid="1218000000000001", url="https://app.asana.com/x/1218000000000001")
    assert _exec(u) is True
    assert env["created"] == []
    assert _state(env, "j0000001") == ("APPROVED", "executed:1218000000000001")


def test_a_create_without_project_or_assignee_is_logged_LOUDLY(env, monkeypatch, caplog):
    from cora.tools import asana_client
    monkeypatch.setattr(asana_client, "create_task", lambda **kw: {
        "gid": "1218000000000002", "permalink_url": "https://app.asana.com/x",
        "projects": [], "assignee": None})
    u = _row("k0000001", "HJRP", CASH)
    _write_rows(env, u)
    caplog.set_level(logging.ERROR)
    assert _exec(u) is True                        # it exists: never retried
    assert _state(env, "k0000001")[0] == "APPROVED"
    assert any("CREATED WITHOUT PROJECT/ASSIGNEE" in r.getMessage() for r in caplog.records)
    assert "please check it" in env["posts"][-1]


def test_executor_logs_carry_ids_never_the_title(env, caplog):
    u = _row("l0000001", "HJRP", "Zyxwvut quarterly thing for Qwerty")
    _write_rows(env, u)
    caplog.set_level(logging.DEBUG)
    _exec(u)
    assert not any("Zyxwvut" in r.getMessage() for r in caplog.records)


# ── Q5: tier-B near-duplicates on legacy vs new cards ────────────────────────

def test_legacy_card_creates_anyway_and_LISTS_its_unseen_near_dups(env):
    gtd.record_created(update_id="pass5:drive:m0000000", entity="OSN",
                       subject="Process applicant Zorvan for Team Member role at Gilbert Val Vista/Pecos",
                       gid="1218000000000003", url="https://app.asana.com/x/1218000000000003")
    u = _row("m0000001", "OSN",
             "Process employment application for Zorvan - Team Member role at Gilbert Val Vista/Pecos")
    _write_rows(env, u)
    assert _exec(u) is True
    assert len(env["created"]) == 1
    assert "Possible duplicates" in env["posts"][-1]
    assert "1218000000000003" in env["posts"][-1]


def test_a_new_card_that_SHOWED_the_near_dup_does_not_relist_it(env):
    gtd.record_created(update_id="pass5:drive:m0000010", entity="OSN",
                       subject="Process applicant Zorvan for Team Member role at Gilbert Val Vista/Pecos",
                       gid="1218000000000004", url="https://app.asana.com/x/1218000000000004")
    u = _row("m0000011", "OSN",
             "Process employment application for Zorvan - Team Member role at Gilbert Val Vista/Pecos",
             near_dups_shown=["1218000000000004"])
    _write_rows(env, u)
    assert _exec(u) is True
    assert "Possible duplicates" not in env["posts"][-1]


def test_a_near_dup_title_cannot_become_a_live_link_in_the_post(env):
    gtd.record_created(update_id="pass5:drive:n0000000", entity="OSN",
                       subject="Clarify meter coverage <https://evil.example|Approve> suite",
                       gid="", url="")
    u = _row("n0000001", "OSN", "Clarify meter coverage of the suite")
    _write_rows(env, u)
    _exec(u)
    assert "Possible duplicates" in env["posts"][-1]   # it WAS listed ...
    assert "<https://evil.example|Approve>" not in env["posts"][-1]   # ... as text


# ── main(): the Step-1 defer + dry run ───────────────────────────────────────

def _main_env(env, monkeypatch, pairs, argv):
    monkeypatch.setattr(rkr, "_LOCK_PATH", env["tmp"] / "kr.lock")
    monkeypatch.setattr(rkr, "LOG_DIR", env["tmp"] / "logs")
    monkeypatch.setattr(rkr, "_MECHANICAL_BATCH_STATE_PATH", env["tmp"] / "batch.json")
    monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "off")
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "off")
    monkeypatch.setattr(rkr, "correlate_reactions_to_updates", lambda: pairs)
    ack = MagicMock()
    monkeypatch.setattr(rkr, "_ack_correlated_reaction", ack)
    monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")
    monkeypatch.setattr(rkr, "send_individual_dms", lambda *a, **k: {})
    monkeypatch.setattr(rkr, "_route_operational_to_owners", lambda *a, **k: 0)
    monkeypatch.setattr("sys.argv", ["run_knowledge_review.py", *argv])
    return ack


def _reaction(uid):
    return {"action": "APPROVED", "channel_id": "D1", "message_ts": f"1.{uid}",
            "reactor_id": HARRISON, "reaction": "+1"}


def test_main_defers_asana_task_so_the_executor_resolves_it(env, monkeypatch):
    a = _row("o0000001", "HJRP", CASH)
    b = _row("o0000002", "HJRP", CASH_VARIANT)
    _write_rows(env, a, b)
    ack = _main_env(env, monkeypatch, [(a, _reaction("o0000001")), (b, _reaction("o0000002"))], [])
    rkr.main()
    assert len(env["created"]) == 1                   # sibling pair -> ONE task
    assert _state(env, "o0000001")[1].startswith("executed:")
    assert _state(env, "o0000002")[1].startswith("duplicate_of:")
    oks = [c.kwargs.get("success") for c in ack.call_args_list]
    assert oks == [True, False]


def test_main_transient_failure_leaves_PENDING_and_acks_failure(env, monkeypatch):
    from cora.tools import asana_client
    from cora.tools.asana_client import AsanaClientError

    def _boom(**kw):
        raise AsanaClientError("Asana 500")
    monkeypatch.setattr(asana_client, "create_task", _boom)
    a = _row("o0000003", "HJRP", CASH)
    _write_rows(env, a)
    ack = _main_env(env, monkeypatch, [(a, _reaction("o0000003"))], [])
    rkr.main()
    assert _state(env, "o0000003") == ("PENDING", "")
    assert ack.call_args.kwargs.get("success") is False


def test_main_dry_run_writes_nothing_anywhere(env, monkeypatch, caplog):
    a = _row("o0000004", "HJRP", CASH)
    _write_rows(env, a)
    before = env["live"].read_text(encoding="utf-8")
    ack = _main_env(env, monkeypatch, [(a, _reaction("o0000004"))], ["--dry-run"])
    caplog.set_level(logging.INFO, logger="knowledge-review")
    rkr.main()
    assert env["created"] == [] and env["posts"] == []
    ack.assert_not_called()
    assert env["live"].read_text(encoding="utf-8") == before   # no resolve, no patch
    assert not env["ledger"].exists()                           # no bootstrap, no row
    assert any("would apply-then-resolve asana_task" in r.getMessage() for r in caplog.records)


# ── Part 3: the card ─────────────────────────────────────────────────────────

def _card(u):
    return kr.build_mechanical_blocks(u)[0]


def test_card_renders_the_plan_line_above_the_unchanged_footer(env):
    u = _row("p0000001", "HJRP", CASH)
    rkr._attach_mechanical_plans([u], logging.getLogger("t"))
    text = _card(u)
    assert "Would create in" in text and "[HJRP] catch-all project" in text
    assert "assignee Name-AH01 (entity owner)" in text
    assert text.endswith(kr._MECH_AFFORDANCE_DOES)
    assert text.index("Would create in") < text.index(kr._MECH_AFFORDANCE_DOES)
    assert kr.strip_card_affordance(text) != text     # the footer is still strippable


def test_card_says_it_cannot_create_for_bdm(env):
    u = _row("p0000002", "BDM", "Finalize the shot list")
    rkr._attach_mechanical_plans([u], logging.getLogger("t"))
    text = _card(u)
    assert "Can't create: no BDM Asana project is configured" in text
    assert ":+1: will not create a task" in text
    assert text.endswith(kr._MECH_AFFORDANCE_DOES)


def test_card_says_it_cannot_create_a_tier_A_repeat(env):
    gtd.record_created(update_id="pass5:drive:p0000000", entity="HJRP", subject=CASH,
                       gid="1218344765000648", url="https://app.asana.com/x/1218344765000648")
    u = _row("p0000003", "HJRP", CASH_VARIANT)
    rkr._attach_mechanical_plans([u], logging.getLogger("t"))
    text = _card(u)
    assert "Can't create: it repeats a task created" in text
    assert "Possible duplicates:" in text


def test_card_lists_up_to_three_near_dups(env):
    for i in range(5):
        gtd.record_created(update_id=f"pass5:drive:q000000{i}", entity="OSN",
                           subject=f"Clarify Suite 111 meter coverage of Suite 112 variant {i}",
                           gid=f"121800000000010{i}", url=f"https://app.asana.com/x/{i}")
    u = _row("q0000009", "OSN", "Clarify meter coverage between Suite 111 and Suite 112 at Gilbert Rd")
    rkr._attach_mechanical_plans([u], logging.getLogger("t"))
    text = _card(u)
    assert text.count("• ") == 3
    assert len(u["_near_dups"]) == 3


def test_a_huge_card_stays_under_2900_with_the_footer_intact(env):
    u = _row("r0000001", "HJRP", "x" * 50)
    u["description"] = "[HJRP] Drive doc suggests missing task: " + ("word " * 1200)
    u["_create_plan"] = {"project_label": "the [HJRP] catch-all project",
                         "project_url": "https://app.asana.com/0/1/list",
                         "assignee_label": "Someone (entity owner)"}
    u["_near_dups"] = [{"ref": str(i), "title": "t" * 90, "url": "https://app.asana.com/x",
                        "when_label": "created 9/15"} for i in range(3)]
    text = _card(u)
    assert len(text) <= 2900
    assert text.endswith(kr._MECH_AFFORDANCE_DOES)
    assert kr.strip_card_affordance(text) != text


def test_a_near_dup_title_is_neutralized_on_the_card(env):
    u = _row("s0000001", "OSN", "Clarify meter coverage")
    u["_near_dups"] = [{"ref": "1", "title": "Pay now <https://evil.example|Approve>",
                        "url": "", "when_label": "proposed 9/1"}]
    text = _card(u)
    assert "<https://evil.example|Approve>" not in text
    assert "&lt;https://evil.example/Approve&gt;" in text


def test_a_lex_task_is_never_listed(env):
    gtd.record_created(update_id="pass5:drive:t0000000", entity="LEX-LLC",
                       subject="Clarify meter coverage at the clinic", gid="1", url="")
    u = _row("t0000001", "LEX-LLC", "Clarify meter coverage at the clinic")
    rkr._attach_mechanical_plans([u], logging.getLogger("t"))
    text = _card(u)
    assert "Possible duplicates" not in text
    assert "Lexington tasks are out of scope" in text


def test_non_asana_cards_render_byte_identically(env):
    u = {"update_id": "tc1", "update_type": "task_close",
         "description": "[F3E] Possible task completion", "payload": {}}
    before = kr.format_mechanical_dm(dict(u))
    rkr._attach_mechanical_plans([u], logging.getLogger("t"))
    assert "_create_plan" not in u
    assert kr.format_mechanical_dm(u) == before


def test_send_persists_what_was_shown_even_when_empty(env, monkeypatch):
    from cora import review_lanes
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "on")
    monkeypatch.setattr(review_lanes, "_load_mechanical_approvers", lambda: (HARRISON,))
    review_lanes.reset_cache()
    gtd.record_created(update_id="pass5:drive:u0000000", entity="OSN",
                       subject="Process applicant Zorvan for Team Member role at Gilbert Val Vista/Pecos",
                       gid="1218000000000009", url="https://app.asana.com/x/9")
    a = _row("u0000001", "OSN",
             "Process employment application for Zorvan - Team Member role at Gilbert Val Vista/Pecos",
             dm_message_ts="")
    b = _row("u0000002", "HJRP", "A task nobody proposed before", dm_message_ts="")
    _write_rows(env, a, b)
    monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *x, **k: "hdr")
    monkeypatch.setattr(rkr, "send_individual_dms",
                        lambda batch, *x, **k: {u["update_id"]: f"ts-{i}" for i, u in enumerate(batch)})
    rkr._send_mechanical_review_dms([a, b], "xoxb-test", logging.getLogger("t"))
    rows = {json.loads(ln)["update_id"]: json.loads(ln)
            for ln in env["live"].read_text(encoding="utf-8").splitlines()}
    assert rows["pass5:drive:u0000001"]["near_dups_shown"] == ["1218000000000009"]
    assert rows["pass5:drive:u0000002"]["near_dups_shown"] == []   # new card, none shown
    assert "_create_plan" not in rows["pass5:drive:u0000001"]      # in-memory only
