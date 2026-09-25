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


# ── Code #15 D-051 r1 (s2#1 / s2#3 / s2#4 / s2#5 / lens-d082#1) ──────────────

HANNAH = "U0B3AEQS0NB"


def _p1(uid: str, subject: str, *, days_ago: float = 3.0, desc_tail: str = "",
        entity: str = "F3E", **extra) -> dict:
    """A pass-1 (slack) asana_task row: payload.entity set, so it is delegable --
    and it has NO ledger `proposed` row (only pass-5 is seeded/recorded)."""
    r = {"update_id": f"missing_asana_task:slack_C1_1.2:{uid}", "update_type": "asana_task",
         "description": f"[{entity}] Slack thread suggests a missing task: {subject}{desc_tail}",
         "payload": {"entity": entity, "suggested_task_name": subject, "source": "slack"},
         "source_evidence": "", "confidence": "HIGH", "state": "PENDING",
         "proposed_at": _iso(days_ago), "dm_message_ts": ""}
    r.update(extra)
    return r


def _p1_state(env, uid: str) -> tuple[str, str]:
    for ln in env["live"].read_text(encoding="utf-8").splitlines():
        e = json.loads(ln)
        if e["update_id"].endswith(f":{uid}"):
            return e["state"], e.get("resolved_reason") or ""
    raise AssertionError("row missing")


def _delegate_to_hannah(monkeypatch):
    from cora import review_lanes
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "on")
    monkeypatch.setattr(review_lanes, "_load_mechanical_approvers", lambda: (HARRISON, HANNAH))
    review_lanes.reset_cache()


def _capture_cards(monkeypatch) -> dict:
    sent: dict[str, list[str]] = {}

    def _send(batch, tok, cf, block_builder=None, recipient_id=None):
        for u in batch:
            sent.setdefault(recipient_id, []).append(block_builder(u)[0])
        return {u["update_id"]: f"ts-{i}" for i, u in enumerate(batch)}
    monkeypatch.setattr(rkr, "send_individual_dms", _send)
    monkeypatch.setattr(rkr, "_send_dm_to_user", lambda *a, **k: "hdr")
    monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")
    return sent


def test_a_delegated_card_lists_only_near_dups_its_recipient_may_approve(env, monkeypatch):
    """D-051 r1 s2#1: _attach_mechanical_plans was never told the recipient, so a
    sibling can_approve REFUSES Hannah (content-screened: a LEX token) had its
    title printed on her card, and so did a ledger-only pass-5 row."""
    from cora import review_lanes
    _delegate_to_hannah(monkeypatch)
    card = _p1("aaaa0001", "Ship Brightwell retail sample kit with COA packet")
    hidden = _p1("bbbb0002", "Ship Brightwell retail sample kit with COA packet today",
                 desc_tail=' -- fireflies says: "discussed on the (LEX) call"', days_ago=5)
    clean = _p1("cccc0003", "Ship Brightwell retail sample kit with COA packet to the lab",
                days_ago=4)
    _write_rows(env, card, hidden, clean)
    gtd.record_proposal(gap_id="pass5:drive:dddd0004", entity="F3E",
                        subject="Ship Brightwell retail sample kit with COA packet (Zqpass5)")
    assert review_lanes.can_approve(card, HANNAH) is True
    assert review_lanes.can_approve(hidden, HANNAH) is False
    sent = _capture_cards(monkeypatch)
    rkr._send_mechanical_review_dms([card], "xoxb-test", logging.getLogger("t"))
    (text,) = sent[HANNAH]
    assert "COA packet today" not in text          # screened sibling: withheld
    assert "Zqpass5" not in text                   # ledger-only pass-5 row: withheld
    assert "COA packet to the lab" in text         # a sibling she may approve: listed
    rows = {json.loads(ln)["update_id"]: json.loads(ln)
            for ln in env["live"].read_text(encoding="utf-8").splitlines()}
    assert rows[card["update_id"]]["near_dups_shown"] == [clean["update_id"]]


def test_a_content_screened_near_dup_reaches_no_card_at_all(env):
    """Even Harrison's card: `near_duplicates` promises a LEX title never reaches a
    card, and its entity check alone did not keep that for an HJRG/F3E row whose
    subject carries a LEX token."""
    gtd.record_created(update_id="pass5:drive:e0000lex", entity="OSN",
                       subject="Clarify meter coverage of the Lexington suite",
                       gid="1218000000000077", url="https://app.asana.com/x/77")
    u = _row("e0000009", "OSN", "Clarify meter coverage of the suite")
    assert gtd.match_tier(gtd.subject_of(u), "Clarify meter coverage of the Lexington suite") == "B"
    rkr._attach_mechanical_plans([u], logging.getLogger("t"))
    assert "Lexington" not in _card(u)
    assert u["_near_dups"] == []


def test_a_phi_shaped_near_dup_is_withheld_from_the_leadership_post(env):
    """D-051 r1 lens-d082#1: a never-approved (DISMISSED) proposal's subject was
    posted verbatim to the multi-person #hjrg-leadership as a 'possible duplicate'
    with no phi_guard screen at that egress."""
    from cora import phi_guard
    phi_subj = "Schedule ZZMARK follow-up for diabetes treatment plan at Gilbert clinic"
    assert phi_guard.is_any_phi(phi_subj)
    assert gtd.record_proposal(gap_id="pass5:drive:dism0001", entity="HJRP", subject=phi_subj)
    clean = "Renew the Gilbert clinic parking lease"
    gtd.record_created(update_id="pass5:drive:okay0001", entity="HJRP", subject=clean,
                       gid="1218000000000088", url="https://app.asana.com/x/88")
    new = "Confirm Gilbert clinic lease renewal paperwork"
    assert gtd.match_tier(new, phi_subj) == "B" and gtd.match_tier(new, clean) == "B"
    assert not phi_guard.is_any_phi(new) and not phi_guard.is_any_phi(clean)
    u = _row("new00001", "HJRP", new, days_ago=0.1)
    _write_rows(env, u)
    assert _exec(u) is True
    post = env["posts"][-1]
    assert "Possible duplicates" in post
    assert "ZZMARK" not in post and "diabetes" not in post
    assert "1 more withheld from this channel" in post
    assert "x/88" in post                            # the clean one is still listed


def test_a_phi_shaped_task_name_is_withheld_from_the_leadership_post(env, monkeypatch):
    from cora import phi_guard
    subj = "Schedule ZZMARK follow-up for diabetes treatment plan"
    assert phi_guard.is_any_phi(subj)
    u = _row("new00002", "HJRP", subj, days_ago=0.1)
    _write_rows(env, u)
    assert _exec(u) is True
    post = env["posts"][-1]
    assert "ZZMARK" not in post and "diabetes" not in post
    assert "name withheld" in post
    # fail-closed: a screen that raises withholds too
    import cora.phi_guard as pg
    monkeypatch.setattr(pg, "is_any_phi", lambda t: (_ for _ in ()).throw(RuntimeError("x")))
    u2 = _row("new00003", "HJRP", "A harmless second chore entirely", days_ago=0.1)
    _write_rows(env, u2)
    assert _exec(u2) is True
    assert "harmless second chore" not in env["posts"][-1]


def test_a_timed_out_create_that_committed_holds_its_pass1_sibling(env, monkeypatch):
    """D-051 r1 s2#3 (narrowed by its verifier: pass-1 rows, which have no ledger
    `proposed` row to order them): Asana commits A's create, then the read times
    out (AsanaClientError). B, a tier-A sibling approved in the same run, used to
    pass every check -- empty ledger, empty in-run set, a scan cached BEFORE A's
    create -- and create a SECOND task."""
    from cora.tools import asana_client
    from cora.tools.asana_client import AsanaClientError
    server: list[dict] = []

    def _create(**kw):
        server.append({"gid": f"1219{len(server):012d}", "name": kw["name"],
                       "permalink_url": "https://app.asana.com/x"})
        if len(server) == 1:
            raise AsanaClientError("Asana network error: ReadTimeout")
        return {"gid": server[-1]["gid"], "permalink_url": "https://app.asana.com/x",
                "projects": [{"gid": kw["project_gid"], "name": "P"}],
                "assignee": {"gid": kw["assignee_gid"], "name": "A"}}
    monkeypatch.setattr(asana_client, "create_task", _create)
    env["scan"].side_effect = lambda gid, max_tasks=500: [dict(t) for t in server]
    a = _p1("tmo00001", "Reorder the F3 Pure shrink sleeves from Brightwell", days_ago=3)
    b = _p1("tmo00002", "Reorder the F3 Pure shrink sleeves from Brightwell ($4,100)",
            days_ago=1)
    _write_rows(env, a, b)
    st = rkr._new_gap_run_state()
    assert _exec(a, st) is False                       # left PENDING
    assert _exec(b, st) is False                       # HELD, not created
    assert len(server) == 1                            # ONE task in Asana
    assert _p1_state(env, "tmo00001") == ("PENDING", "")
    assert _p1_state(env, "tmo00002") == ("PENDING", "")
    assert "did not confirm this run" in env["posts"][-1]
    assert st["outcomes"][b["update_id"]]["kind"] == "pending"
    # the scan cached before A's create was dropped: the next run's scan sees it
    assert PROJECT["F3E"] not in st["project_scan"]


def test_executor_posts_and_logs_name_the_row_by_its_hash_tail(env, caplog):
    """D-051 r1 s2#4: uid[:8] is the lane prefix -- 'pass5:dr' / 'missing_' for
    EVERY row -- so refusal / duplicate / left-pending lines named nothing."""
    caplog.set_level(logging.INFO)
    bdm = _row("tail0001", "BDM", "Finalize the shot list for events")
    p1 = _p1("tail0002", "Order the retail endcap signage", entity="BDM")
    _write_rows(env, bdm, p1)
    _exec(bdm)
    _exec(p1)
    posts = env["posts"][-2:]
    assert "`[tail0001]`" in posts[0] and "`[tail0002]`" in posts[1]
    assert not any("pass5:dr" in p or "missing_" in p for p in posts)
    msgs = [r.getMessage() for r in caplog.records if "gap-executor" in r.getMessage()]
    assert any("uid=tail0001" in m for m in msgs) and any("uid=tail0002" in m for m in msgs)
    assert not any("uid=pass5:dr" in m or "uid=missing_" in m for m in msgs)


def test_asana_ack_overrides_name_each_end(env):
    st = rkr._new_gap_run_state()
    u = {"update_id": "u1", "update_type": "asana_task"}
    assert rkr._asana_ack_overrides(st, u) == {}                   # no outcome: default
    rkr._gap_outcome(st, "u1", "refused", "no BDM Asana project is configured")
    kw = rkr._asana_ack_overrides(st, u)
    assert "no Asana task was created" in kw["text"] and "no BDM Asana project" in kw["text"]
    assert "didn't go through" not in kw["text"] and kw.get("retire", True) is True
    rkr._gap_outcome(st, "u1", "duplicate", "123")
    assert "Dismissed as a duplicate" in rkr._asana_ack_overrides(st, u)["text"]
    rkr._gap_outcome(st, "u1", "pending")
    kw = rkr._asana_ack_overrides(st, u)
    assert kw["retire"] is False and "retry it on the next review run" in kw["text"]
    rkr._gap_outcome(st, "u1", "created", "9")
    assert rkr._asana_ack_overrides(st, u) == {}
    assert rkr._asana_ack_overrides(st, {"update_id": "u1", "update_type": "task_close"}) == {}


def test_main_acks_a_duplicate_as_not_created_and_a_transient_as_pending(env, monkeypatch):
    """D-051 r1 s2#5: every False from the executor was acked ':warning: ... the
    automatic save didn't go through' and the card stamped Resolved -- a
    deliberate duplicate-dismiss read as a fault, and a still-PENDING row whose
    reaction IS retried next run was marked as no longer applying."""
    from cora.tools import asana_client
    from cora.tools.asana_client import AsanaClientError
    a = _row("o0000011", "HJRP", CASH)
    b = _row("o0000012", "HJRP", CASH_VARIANT)
    c = _row("o0000013", "OSN", "Clarify meter coverage")
    real = asana_client.create_task

    def _create(**kw):
        if kw.get("project_gid") == PROJECT["OSN"]:
            raise AsanaClientError("Asana 503")
        return real(**kw)
    monkeypatch.setattr(asana_client, "create_task", _create)
    _write_rows(env, a, b, c)
    ack = _main_env(env, monkeypatch, [(a, _reaction("o0000011")), (b, _reaction("o0000012")),
                                       (c, _reaction("o0000013"))], [])
    rkr.main()
    calls = {cl.args[2]["update_id"]: cl.kwargs for cl in ack.call_args_list}
    assert calls[a["update_id"]].get("success") is True and "text" not in calls[a["update_id"]]
    dup = calls[b["update_id"]]
    assert dup["success"] is False and "Dismissed as a duplicate" in dup["text"]
    assert dup.get("retire", True) is True             # DISMISSED: the card IS resolved
    pend = calls[c["update_id"]]
    assert pend["success"] is False and pend["retire"] is False
    assert "retry" in pend["text"] and _state(env, "o0000013") == ("PENDING", "")


def test_ack_with_retire_false_leaves_the_card_alone():
    client = MagicMock()
    reaction = {"action": "APPROVED", "channel_id": "D1", "message_ts": "1.2"}
    rkr._ack_correlated_reaction(reaction, "APPROVED", {"update_type": "asana_task"},
                                 "xoxb-test", logging.getLogger("t"),
                                 _client_factory=lambda: client, success=False,
                                 text="custom outcome", retire=False)
    assert client.chat_postMessage.call_args.kwargs["text"] == "custom outcome"
    client.chat_update.assert_not_called()
    client.conversations_history.assert_not_called()
    client.reactions_add.assert_not_called()


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
