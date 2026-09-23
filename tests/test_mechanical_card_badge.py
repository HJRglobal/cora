"""Code #14 S7 (cq-17fe76f5ab91) -- the `?` on mechanical review cards.

The kickoff read the `?` as an Asana PRIORITY badge; it is the ENTITY slot of
knowledge_review.format_mechanical_dm, which read payload.entity only and fell back
to a literal '?'. Pass-5 asana_task, task_close and hubspot_note rows carry no
payload entity, so ~283 of 303 PENDING mechanical rows rendered '*[Asana task]* `?`'
(clarity checks 9/15, 9/16, 9/18). The badge now comes from
review_lanes.resolve_entity and is OMITTED when unresolved; raw <U...> speaker
tokens in the description are resolved like the decision card's.

All fixtures are synthetic (no real vendor/person content; no LEX content).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cora import knowledge_review as kr
from cora import review_lanes as rl


def _mech(utype="asana_task", desc="[HJRP] Drive doc suggests missing task: Pay vendor invoice",
          payload=None):
    return {"update_id": "m-1", "update_type": utype, "description": desc,
            "payload": {} if payload is None else payload}


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0]


@pytest.fixture
def roster(monkeypatch):
    from cora.tools import user_identity
    monkeypatch.setattr(
        user_identity, "display_name",
        lambda sid: {"U0B2RM2JYJ1": "Harrison Rogers"}.get(sid, sid),
    )
    monkeypatch.delenv("CORA_SLACK_USER_ID", raising=False)


def test_mechanical_card_omits_badge_when_entity_unresolved():
    text = kr.format_mechanical_dm(_mech(
        "task_close", 'Possible task completion: "Reinstate something" -- slack says: "done"'))
    assert _first_line(text) == "*[Close task]*"
    assert "`?`" not in text


@pytest.mark.parametrize("payload", [None, "junk", 7, ["entity", "F3E"]])
def test_mechanical_card_badge_tolerates_missing_or_non_dict_payload(payload):
    u = {"update_id": "m-2", "update_type": "hubspot_note",
         "description": "Deal note with no entity code", "payload": payload}
    text = kr.format_mechanical_dm(u)
    assert _first_line(text) == "*[HubSpot note]*"
    assert "?" not in _first_line(text)


def test_mechanical_card_badge_resolves_from_description():
    """The exact 9/19 clarity-check shape (synthetic body): the entity is in the
    description's bracket, not in the payload."""
    text = kr.format_mechanical_dm(_mech())
    assert _first_line(text) == "*[Asana task]* `HJRP`"


def test_mechanical_card_badge_uses_payload_entity():
    text = kr.format_mechanical_dm(_mech(desc="no code here", payload={"entity": "f3e"}))
    assert _first_line(text) == "*[Asana task]* `F3E`"


def test_mechanical_card_ambiguous_codes_omit_badge():
    text = kr.format_mechanical_dm(_mech(desc="[F3E] shared task also in (OSN) scope"))
    assert _first_line(text) == "*[Asana task]*"


def test_mechanical_card_badge_resolves_from_task_url_gid(monkeypatch):
    monkeypatch.setattr(rl, "_asana_gid_entity_map", lambda: {"1200000000000001": "BDM"})
    text = kr.format_mechanical_dm(_mech(
        "task_close", "Possible task completion: \"x\"",
        payload={"task_url": "https://app.asana.com/1/1/project/1200000000000001/task/9"}))
    assert _first_line(text) == "*[Close task]* `BDM`"


def test_mechanical_card_resolver_error_omits_badge(monkeypatch):
    def _boom(update):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(rl, "resolve_entity", _boom)
    text = kr.format_mechanical_dm(_mech())
    assert _first_line(text) == "*[Asana task]*"


def test_mechanical_card_resolves_raw_slack_ids(roster):
    text = kr.format_mechanical_dm(_mech(desc=(
        'Action commitment in slack (F3E) with no matching Asana task: '
        '"[2026-09-01 10:00 UTC] <U0B2RM2JYJ1>: I will send it <@U0B44MDGC5R> '
        'and <U0ZZZZ99999>"')))
    assert "@Harrison Rogers: I will send it @Cora and @unknown user" in text
    assert "<U" not in text and "<@U" not in text
    assert _first_line(text) == "*[Asana task]* `F3E`"


def test_mechanical_blocks_never_show_a_question_mark_badge():
    safe, blocks = kr.build_mechanical_blocks(_mech(
        "task_close", 'Possible task completion: "x" -- slack says: "done"'))
    section = blocks[0]["text"]["text"]
    assert _first_line(section) == "*[Close task]*"
    assert "`?`" not in section and "`?`" not in safe


def test_batch_card_unresolved_row_renders_no_question_mark(monkeypatch):
    import scripts.run_knowledge_review as rkr
    monkeypatch.setattr(rl, "content_screen_excludes", lambda u: (False, ""))
    now = datetime.now(timezone.utc)
    rows = [{
        "update_id": "b-1", "update_type": "task_close", "state": "PENDING",
        "description": 'Possible task completion: "Unfiled thing"',
        "payload": {}, "proposed_at": "2026-09-01T00:00:00+00:00",
        "dm_message_ts": "", "resolved_at": None,
    }]
    text = rkr._build_mechanical_batch_card(rows, now, expired_this_run=0, expired_7d=0)
    row_line = next(line for line in text.splitlines() if "Unfiled thing" in line)
    assert " ? " not in row_line
    assert row_line.startswith("  1. [task_close] ") and "d -- " in row_line
