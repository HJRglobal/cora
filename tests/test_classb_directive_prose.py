"""Class-B kinds must never render MODEL-FACING directive prose on a HUMAN surface.

cq-288edaba659d. A tool returns a payload written for the MODEL to interpret --
a sentinel line, a "no preamble, no meta-commentary" instruction, a "Surface
this to the user:" lead-in -- and that text reaches a person verbatim.

TWO human surfaces post an executor's return with no model in between, and both
go through the SAME seam (tool_dispatch._strip_write_sentinel):
  * the confirm-card terminal text on a button tap
    (resolve_and_claim_stash / _resolve_meeting_item_tap)
  * the deterministic typed-confirm interceptor reply
    (try_confirm_pending_write -> app.py say(text=...) VERBATIM)

The seed named "3 Class-B kinds". Measured against the roster, SIX of the eight
leak, because every kind's FAILURE return is sentinel-free by construction --
see the cascade report's S1 table.

ROSTER-DRIVEN BY CONSTRUCTION: test_every_roster_kind_has_a_scenario fails when
a kind is added to _CLASSB_KINDS without an outcome scenario here, so a future
kind cannot join arbitration without being covered (D-220).
"""

from unittest.mock import patch

import pytest

import cora.tools.gmail_client as gc
import cora.tools.hubspot_client as hc
import cora.tools.influencer_client as ic
import cora.tools.tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"

# Every token that means "this text was written for the model, not the reader".
DIRECTIVE_TOKENS = (
    "tell the user",
    "surface this to the user",
    "surface these to the user",
    "no preamble",
    "no meta-commentary",
    "as your entire response",
    "preserve the <url|name> syntax",
    "post the following",
)
SENTINEL_TOKENS = ("WRITE_CONFIRMED", "WRITE_BLOCKED")


def _assert_human_clean(text, *, label):
    """The invariant: outbound human text carries no sentinel and no directive."""
    assert text is not None, "%s: None reached a human surface" % label
    for tok in SENTINEL_TOKENS:
        assert tok not in text, "%s: sentinel %r reached Slack text:\n%s" % (label, tok, text)
    low = text.lower()
    for tok in DIRECTIVE_TOKENS:
        assert tok not in low, (
            "%s: model-facing directive %r reached Slack text:\n%s" % (label, tok, text))


# --------------------------------------------------------------------------
# Per-kind outcome scenarios. Each returns the RAW executor payload, which the
# test then pushes through the real shared seam -- so these exercise the live
# executors, not a transcription of them.
# --------------------------------------------------------------------------

def _gmail_success():
    with patch.object(gc, "create_draft", return_value={"id": "draft_abc123"}):
        return td._execute_claimed_gmail_draft(
            {"sender_email": "harrison@hjrglobal.com", "to": "a@b.com",
             "subject": "Wholesale terms", "body": "Body"}, HARRISON)


def _gmail_success_guard_notes():
    with patch.object(gc, "create_draft", return_value={"id": "draft_abc123"}):
        return td._execute_claimed_gmail_draft(
            {"sender_email": "harrison@hjrglobal.com", "to": "a@b.com",
             "subject": "Wholesale terms", "body": "Body",
             "guard_notes": ["no greeting", "no sign-off"]}, HARRISON)


def _gmail_failure():
    with patch.object(gc, "create_draft",
                      side_effect=gc.GmailClientError("bad recipient")):
        return td._execute_claimed_gmail_draft(
            {"sender_email": "harrison@hjrglobal.com", "to": "a@b",
             "subject": "S", "body": "B"}, HARRISON)


_HS_STAGE = {"entity": "F3E", "deal_id": "1", "stage_id": "s2", "deal_name": "Acme",
             "current_stage_name": "Proposal", "new_stage_name": "Closed Won"}


def _hubspot_stage_success():
    with patch.object(hc, "update_deal_stage", return_value=None), \
         patch.object(hc, "_deal_url", return_value="https://app.hubspot.com/deal/1"):
        return td._execute_claimed_hubspot_stage(dict(_HS_STAGE), HARRISON)


def _hubspot_stage_failure():
    with patch.object(hc, "update_deal_stage",
                      side_effect=hc.HubSpotClientError("403 forbidden")):
        return td._execute_claimed_hubspot_stage(dict(_HS_STAGE), HARRISON)


def _hubspot_stage_lex_blocked():
    return td._execute_claimed_hubspot_stage(dict(_HS_STAGE, entity="LEX"), HARRISON)


_HS_NOTE = {"entity": "F3E", "deal_id": "1", "deal_name": "Acme",
            "note_body": "Called them"}


def _hubspot_note_success():
    with patch.object(hc, "create_note", return_value="n-1"), \
         patch.object(hc, "_deal_url", return_value="https://app.hubspot.com/deal/1"):
        return td._execute_claimed_hubspot_note(dict(_HS_NOTE), HARRISON)


def _hubspot_note_failure():
    with patch.object(hc, "create_note",
                      side_effect=hc.HubSpotClientError("timeout")):
        return td._execute_claimed_hubspot_note(dict(_HS_NOTE), HARRISON)


_DM = {"entity": "F3E", "recipient_id": "U0B3VGWJTMJ", "display_name": "Tommy",
       "message": "Quick question about the retail deck."}


class _FakeSlackOK:
    def __init__(self, *a, **k):
        pass

    def conversations_open(self, users):
        return {"channel": {"id": "D1"}}

    def chat_postMessage(self, channel, text):
        return {"ts": "1.0"}


class _FakeSlackTimeout(_FakeSlackOK):
    def chat_postMessage(self, channel, text):
        raise TimeoutError("read timed out")


class _FakeSlackRejects(_FakeSlackOK):
    """Slack REJECTED the post -- distinct from the transport-timeout case, and a
    separate return branch, so it needs its own scenario or the branch ships
    unmeasured (it was the 13th leaking path)."""

    def chat_postMessage(self, channel, text):
        from slack_sdk.errors import SlackApiError
        raise SlackApiError("channel_not_found",
                            response={"error": "channel_not_found"})


def _slack_dm_success():
    with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}), \
         patch("slack_sdk.WebClient", _FakeSlackOK):
        return td._execute_claimed_slack_dm(dict(_DM), HARRISON)


def _slack_dm_indeterminate():
    """Transport error AFTER Slack may have accepted -- the D-101 unknown case."""
    with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}), \
         patch("slack_sdk.WebClient", _FakeSlackTimeout):
        return td._execute_claimed_slack_dm(dict(_DM), HARRISON)


def _slack_dm_rejected_by_slack():
    """Slack said no -- "not sent" is accurate here, unlike the timeout case."""
    with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}), \
         patch("slack_sdk.WebClient", _FakeSlackRejects):
        return td._execute_claimed_slack_dm(dict(_DM), HARRISON)


def _slack_dm_malformed_recipient():
    with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
        return td._execute_claimed_slack_dm(dict(_DM, recipient_id="U1,U2"), HARRISON)


def _slack_dm_lex_blocked():
    return td._execute_claimed_slack_dm(dict(_DM, entity="LEX"), HARRISON)


_HANDLE = {"athlete_name": "Jenna Williams", "platform": "instagram",
           "handle": "@jennaw", "row_entity": "F3E"}


def _influencer_handle_success():
    with patch.object(ic, "register_handle", return_value={"handle": "jennaw"}):
        return td._execute_claimed_influencer_handle(dict(_HANDLE), HARRISON)


def _influencer_handle_failure():
    with patch.object(ic, "register_handle",
                      side_effect=ic.InfluencerClientError("duplicate handle")):
        return td._execute_claimed_influencer_handle(dict(_HANDLE), HARRISON)


_ROW = {"id": 5, "athlete_name": "Jenna Williams", "platform": "instagram",
        "deliverable_type": "post", "due_date": "2026-08-31", "entity": "F3E",
        "status": "pending"}


def _influencer_deliverable_add():
    with patch.object(ic, "add_deliverable", return_value=dict(_ROW)):
        return td._execute_claimed_influencer_deliverable(
            {"action": "add", "athlete_name": "Jenna Williams",
             "platform": "instagram", "deliverable_type": "post",
             "row_entity": "F3E", "actor_display": "Alex"}, HARRISON)


def _influencer_deliverable_complete():
    with patch.object(ic, "mark_complete", return_value=dict(_ROW)):
        return td._execute_claimed_influencer_deliverable(
            {"action": "complete", "deliverable_id": 5,
             "actor_display": "Alex"}, HARRISON)


def _influencer_deliverable_waive():
    with patch.object(ic, "mark_waived", return_value=dict(_ROW)):
        return td._execute_claimed_influencer_deliverable(
            {"action": "waive", "deliverable_id": 5,
             "actor_display": "Alex"}, HARRISON)


def _influencer_deliverable_failure():
    with patch.object(ic, "mark_complete",
                      side_effect=ic.InfluencerClientError("no such id")):
        return td._execute_claimed_influencer_deliverable(
            {"action": "complete", "deliverable_id": 5,
             "actor_display": "Alex"}, HARRISON)


def _decision_close_success():
    from cora import decision_alerts
    with patch.object(decision_alerts, "mark_state", return_value=True):
        return td._execute_claimed_decision_close(
            {"alert_key": "k1", "answer": "Go with option A"}, HARRISON)


def _decision_close_expired():
    from cora import decision_alerts
    with patch.object(decision_alerts, "mark_state", return_value=False):
        return td._execute_claimed_decision_close(
            {"alert_key": "k1", "answer": "Go with option A"}, HARRISON)


def _decision_close_empty():
    return td._execute_claimed_decision_close({"alert_key": "", "answer": ""}, HARRISON)


def _meeting_item_no_transcript():
    return td._execute_claimed_meeting_item({}, HARRISON)


def _meeting_item_unmapped_asker():
    from cora.tools import meeting_actions as ma
    with patch.object(ma, "_asker_emails", return_value=[]):
        return td._execute_claimed_meeting_item({"transcript_id": "t1"}, HARRISON)


def _meeting_item_success():
    from cora.tools import meeting_actions as ma
    created = (
        "WRITE_CONFIRMED -- post the following as your entire response "
        "(no preamble, no meta-commentary):\n\n"
        "Done -- created 1 task in Asana, assigned to you:\n"
        "- Ship the thing <https://app.asana.com/1|open>"
    )
    with patch.object(ma, "_asker_emails", return_value=["h@hjrglobal.com"]), \
         patch.object(ma, "_fetch_transcript_by_id",
                      return_value={"title": "F3E sync", "id": "t1"}), \
         patch.object(ma, "_asker_attended", return_value=True), \
         patch.object(ma, "_classify_meeting", return_value=("F3E", False)), \
         patch.object(ma, "_scope_ok", return_value=(True, "")), \
         patch.object(ma, "_lex_gate", return_value=(True, "", "F3E")), \
         patch.object(ma, "_create_selected", return_value=created):
        return td._execute_claimed_meeting_item(
            {"transcript_id": "t1", "item": "Ship the thing", "entity": "F3E"}, HARRISON)


# kind -> {scenario label: callable returning the raw executor payload}
SCENARIOS = {
    "gmail_draft": {
        "success": _gmail_success,
        "success_with_guard_notes": _gmail_success_guard_notes,
        "failure": _gmail_failure,
    },
    "hubspot_stage": {
        "success": _hubspot_stage_success,
        "failure": _hubspot_stage_failure,
        "lex_blocked": _hubspot_stage_lex_blocked,
    },
    "hubspot_note": {
        "success": _hubspot_note_success,
        "failure": _hubspot_note_failure,
    },
    "slack_dm": {
        "success": _slack_dm_success,
        "rejected_by_slack": _slack_dm_rejected_by_slack,
        "indeterminate": _slack_dm_indeterminate,
        "malformed_recipient": _slack_dm_malformed_recipient,
        "lex_blocked": _slack_dm_lex_blocked,
    },
    "influencer_handle": {
        "success": _influencer_handle_success,
        "failure": _influencer_handle_failure,
    },
    "influencer_deliverable": {
        "add": _influencer_deliverable_add,
        "complete": _influencer_deliverable_complete,
        "waive": _influencer_deliverable_waive,
        "failure": _influencer_deliverable_failure,
    },
    "meeting_item": {
        "success": _meeting_item_success,
        "no_transcript_id": _meeting_item_no_transcript,
        "unmapped_asker": _meeting_item_unmapped_asker,
    },
    "decision_close": {
        "success": _decision_close_success,
        "expired": _decision_close_expired,
        "empty_answer": _decision_close_empty,
    },
}


def test_every_roster_kind_has_a_scenario():
    """A new Class-B kind cannot join arbitration without an outcome scenario.

    _CLASSB_KINDS is the authoritative roster (the factory comment says a kind
    joins by being added to it), so keying the coverage check on the roster --
    not a hand-written list -- is what makes a ninth kind covered by
    construction rather than by someone remembering."""
    assert set(td._CLASSB_KINDS) == set(SCENARIOS), (
        "Class-B roster and directive-prose scenarios disagree. Missing: %s; stale: %s"
        % (sorted(set(td._CLASSB_KINDS) - set(SCENARIOS)),
           sorted(set(SCENARIOS) - set(td._CLASSB_KINDS))))


_CASES = [(k, label, fn) for k, scen in SCENARIOS.items() for label, fn in scen.items()]
_IDS = ["%s-%s" % (k, label) for k, label, _ in _CASES]


@pytest.mark.parametrize("kind,label,fn", _CASES, ids=_IDS)
def test_no_directive_prose_on_the_human_confirm_surface(kind, label, fn):
    """The confirm card's terminal text, and the typed interceptor's reply --
    both are this exact string, posted with no model in between."""
    raw = fn()
    _assert_human_clean(td._strip_write_sentinel(raw), label="%s/%s" % (kind, label))


@pytest.mark.parametrize("kind,label,fn", _CASES, ids=_IDS)
def test_outcome_text_is_never_empty(kind, label, fn):
    """A strip that removes the directive must not leave the user with nothing.

    D-217, the over-removal direction: several kinds' whole payload IS a lead
    sentence plus a directive, so a filter one notch too greedy renders an empty
    Slack message -- which reads as a silent failure on an action that really
    happened."""
    out = td._strip_write_sentinel(fn())
    assert out and out.strip(), "%s/%s: stripped to empty" % (kind, label)


def test_the_seam_covers_every_human_surface():
    """All three verbatim-posting call sites route through the SAME seam, so one
    fix covers them all (D-220). Pinned so a future refactor cannot quietly give
    one surface its own copy."""
    import inspect
    for fn, what in (
        (td.resolve_and_claim_stash, "button-tap"),
        (td._resolve_meeting_item_tap, "meeting-item tap"),
        (td.try_confirm_pending_write, "typed-confirm interceptor"),
    ):
        assert "_strip_write_sentinel" in inspect.getsource(fn), (
            "%s path stopped using the shared seam" % what)


def _scan_fn_literals(fn, offenders, where):
    """Flag directive-bearing string literals in one function body.

    ALLOWED: a literal that OPENS with a contract sentinel. That is the
    model-facing half of the WRITE_CONFIRMED contract, and the seam splits it off
    at the blank line before any human sees it -- removing it would break the
    narration net, which is the "do not fix the working prompts" trap.

    EXCLUDED: the docstring. `_execute_claimed_calendar`'s docstring legitimately
    quotes the phrase in order to warn future editors off it, and flagging that
    would be a false positive on a comment explaining the rule -- the same trap
    as a source pin that greps a token and matches the comment about it.
    """
    import ast
    doc = set()
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        doc = {id(fn.body[0].value)}

    def _check(text, lineno):
        # A contract literal is NOT waved through wholesale. Only the half ABOVE
        # the blank line is model-facing; the half BELOW it is what the seam posts
        # to a human, so it is held to the same standard as a bare payload.
        # D-051 lens-2 measured four live directives sitting in that user half,
        # invisible to both the belt (which never runs on the sentinel branch)
        # and to the first cut of this scan (which skipped the whole literal).
        if text.startswith(SENTINEL_TOKENS):
            halves = text.split("\n\n", 1)
            if len(halves) < 2:
                return
            text = halves[1]
        low = text.lower()
        for tok in DIRECTIVE_TOKENS:
            if tok in low:
                offenders.append("%s %s (line %d): %r in %r"
                                 % (where, fn.name, lineno, tok, text[:110]))
                return

    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in doc:
                _check(node.value, node.lineno)
        elif isinstance(node, ast.JoinedStr):
            # f-strings: the literal segments are separate Constants, so a
            # directive SPLIT BY an interpolation ("Tell the {who} the stage was
            # not changed.") shows up as two fragments that each carry no token.
            # Rejoin them with a placeholder so the token is visible again. The
            # runtime belt is blind to this shape too -- documented as a residual
            # in the cascade report rather than left to look covered.
            rebuilt = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                else "{}"
                for v in node.values
            )
            _check(rebuilt, node.lineno)


def test_no_executor_on_the_shared_seam_emits_directive_prose():
    """Static invariant over EVERY executor, and over the formatters they return.

    The seam is shared by all 17 stash kinds, so the roster-driven runtime cases
    above still leave nine kinds uncovered. This walks the AST of every
    `_execute_*` function and fails on any string literal carrying a directive --
    so a tenth kind, or a new branch inside an existing one, is caught without
    anyone writing a mock for it.

    D-051 lens-2 measured the first cut of this invariant blind to all three of
    its own HIGH findings, for two reasons, both fixed here:
      * it filtered `_execute_claimed_*`, so `_execute_asana_create` -- whose
        failure text reaches both surfaces -- was not a candidate at all;
      * it only parsed `tool_dispatch`, so a `return some_client.format_*(...)`
        was scanned as a Call node with no literals in sight. The whole calendar
        leak lived one module over.

    The second hole is closed by DERIVING the extra scan targets from the
    executors' own `return` statements rather than listing them: a new formatter
    on the seam is picked up because it is returned, not because someone
    remembered to add it. Scoped to what is actually returned, which is why the
    model-facing PREVIEW formatters (`format_slot_proposals_for_llm`,
    `format_tasks_for_llm`) are correctly out of scope -- they are narrated by
    the model, never posted verbatim.
    """
    import ast
    import importlib
    import inspect

    td_tree = ast.parse(inspect.getsource(td))
    offenders: list[str] = []
    returned: set[tuple[str, str]] = set()

    for fn in ast.walk(td_tree):
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("_execute_"):
            continue
        _scan_fn_literals(fn, offenders, "tool_dispatch")
        # Aliases bound by a FUNCTION-LOCAL import. Measured: without this,
        # `_execute_claimed_meeting_item`'s `from cora.tools import
        # meeting_actions as ma` left `ma` unresolvable, so the one roster kind
        # whose outcome text is produced outside tool_dispatch was silently not
        # scanned -- a derivation that looks exhaustive and quietly is not.
        local: dict[str, str] = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    local[a.asname or a.name] = "%s.%s" % (node.module, a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    local[a.asname or a.name] = a.name
        # `return <alias>.<func>(...)` -- one level deep, which is how every
        # formatter on this seam is actually reached.
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                continue
            f = node.value.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                returned.add((local.get(f.value.id, f.value.id), f.attr))

    assert returned, "derivation found no returned formatters -- the scan went inert"

    resolved_any = False
    for alias, func_name in sorted(returned):
        target = None
        if "." in alias:                      # a dotted path from a local import
            try:
                target = importlib.import_module(alias)
            except ImportError:               # pragma: no cover
                target = None
        else:
            target = getattr(td, alias, None)
        if target is None or not inspect.ismodule(target):
            continue
        try:
            mod_tree = ast.parse(inspect.getsource(target))
        except (OSError, TypeError):  # pragma: no cover -- builtins/extensions
            continue
        for fn in ast.walk(mod_tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == func_name:
                _scan_fn_literals(fn, offenders, target.__name__)
                resolved_any = True

    assert resolved_any, (
        "no returned formatter resolved to a real module -- the cross-module half "
        "of this invariant went inert, which is how the whole calendar_client leak "
        "stayed green through two reviews")

    assert not offenders, (
        "string literals on the shared seam carry model-facing directive prose; "
        "they are posted VERBATIM on the confirm card, the slot-picker card and "
        "the typed-confirm reply:\n  " + "\n  ".join(offenders))


@pytest.mark.parametrize("code,expected", [
    # A plain literal -- the control. If this stops being caught the scan is dead.
    ('def f():\n    return "Tell the user it was not created."\n', True),
    # Value interpolated, directive intact -- the shape every real emitter uses.
    ('def f(e):\n    return f"Asana error: {e}. Tell the user it was not created."\n', True),
    # Directive SPLIT AROUND an interpolation: the literal segments are separate
    # Constants, so this is only caught because JoinedStr values are rejoined.
    ('def f(n):\n    return f"Tell the user the task {n} was not created."\n', True),
    ('def f():\n    return "Done. " + "Tell the user it worked."\n', True),
    ('def f(x):\n    return "Tell the user {} failed.".format(x)\n', True),
    # The USER HALF of a contract is held to the same standard as a bare payload.
    ('def f():\n    return "WRITE_CONFIRMED -- post it:\\n\\nDone. Tell the user it worked."\n', True),
    # ...while the MODEL-FACING half above the blank line is left alone.
    ('def f():\n    return "WRITE_CONFIRMED -- post the following, no preamble:\\n\\nDone."\n', False),
    # DOCUMENTED RESIDUAL, pinned as a known gap rather than left to look
    # covered: interpolating the OBJECT NOUN itself defeats token matching, and
    # the runtime belt is equally blind to the rendered form ("Tell the manager
    # ..."). Catching it would mean matching bare "tell the", which false-
    # positives on ordinary English ("tell the difference"). No emitter does it.
    ('def f(p):\n    return f"Tell the {p} the stage was not changed."\n', False),
], ids=["plain", "f-value", "f-split", "concat", "format",
        "contract-user-half", "contract-model-half", "residual-object-interp"])
def test_static_scan_sees_the_shapes_that_actually_occur(code, expected):
    """The first cut of the static invariant was blind to all three of its own
    HIGH findings. These pin the shapes it must see, and the one it provably
    cannot -- so the gap is a recorded decision, not a surprise."""
    import ast
    offenders = []
    _scan_fn_literals(ast.parse(code).body[0], offenders, "synthetic")
    assert bool(offenders) is expected, offenders


# --------------------------------------------------------------------------
# The belt itself, measured in BOTH directions (D-217).
# --------------------------------------------------------------------------

LEAKY_PAYLOADS = [
    "Gmail draft CREATED in h@x.com's Drafts folder. Surface this to the user:\n"
    "- To: a@b.com\n- Subject: Terms\n"
    "- Open in Gmail: <https://mail.google.com/mail/u/0/#drafts|Drafts>\n\n"
    "Tell the user the draft is ready to review + send from their Gmail Drafts. "
    "Format the Drafts link as a Slack hyperlink (preserve the <url|name> syntax).",
    "Handle REGISTERED. Surface this to the user:\n- *Jenna* -> Instagram @j [F3E]",
    "Draft created.\n\nEMAIL GUARD NOTES (surface these to the user so they fix "
    "the draft before sending):\n- no greeting",
    "HubSpot update failed: 403. Tell the user the stage was not changed.",
]

# Legitimate outbound text that must survive BYTE-IDENTICAL. Includes the
# shortest possible inputs and the adversarial near-misses: "Tell Harrison." is
# an instruction to the READER and is the user's only route to a broken
# recipient map, and a user-supplied subject line may contain anything at all.
LEGITIMATE_PAYLOADS = [
    "Done.",
    "I couldn't complete that.",
    "Nothing was sent.",
    "Updated <https://app.hubspot.com/deal/1|Acme> stage: Proposal -> *Closed Won*.",
    "slack_send_dm: that recipient's Slack id in the user map is malformed, so I "
    "didn't send anything. Tell Harrison.",
    "slack_send_dm: SLACK_BOT_TOKEN not configured. Tell Harrison.",
    "I lost track of which meeting that was. Ask me for it again.",
    "calendar_schedule_meeting: that option is no longer one of the times I "
    "offered. Ask me to find a time again.",
    "The user map is out of date, so I skipped it.",
    "Let me know if that looks wrong.",
    "Report generated for 3 users.",
    "Subject: Tell the user about pricing",
    # Bullet lines are skipped WHOLESALE and that is deliberate: a bullet is
    # rendered content (a subject, an athlete name, a task title). The
    # consequence, stated rather than discovered later: a future emitter that
    # puts its directive on a bullet is NOT caught by the runtime belt -- the
    # static invariant is what catches that shape, and it does.
    "- Subject: Tell the user about pricing.",
    "- Tell the user about pricing.",
    "1. Tell the user about the new rate.",
    "- To: tell.the.user@example.com",
    # D-051 lens-1/lens-4 measured every one of these DESTROYED by the first
    # cut. The plural/possessive/compound-noun trio is why "the user" cannot be
    # a bare substring; the Label: forms are why a colon-terminated part
    # protects the value after it.
    "Ask the users what they think of the new can.",
    "Report the user's crash to engineering.",
    "Show the user list to Justin.",
    "Show the user roster to Justin.",
    "Remind the user's broker in October.",
    "Subject: Tell the user about pricing.",
    "Note: Ask the user for approval.",
    "Task: Report the user counts to finance.",
    "Draft body: Let the user know we shipped.",
    "Deal renamed: Show the user portal migration.",
    "Lease renewal (remind the user's broker in Oct) is due.",
    # A user-authored calendar title echoed into a confirmation. Two lenses
    # found this independently; it is now contract-wrapped at the emitter, and
    # the belt must ALSO leave it alone if it ever arrives bare.
    "Cancelled 'Retro. Show the users the new dashboard.'. Google notified any attendees.",
    "Cancelled 'Kroger intro (ask the user's rep to join)'. Google notified any attendees.",
    "Done -- created 2 tasks in Asana, assigned to you:\n"
    "- Ship the thing <https://app.asana.com/1|open>\n- Other thing",
]


_LEAKY_IDS = ["gmail-success", "handle-success", "gmail-guard-notes", "hubspot-failure"]


@pytest.mark.parametrize("raw", LEAKY_PAYLOADS, ids=_LEAKY_IDS)
def test_belt_removes_the_directive_it_exists_to_stop(raw):
    _assert_human_clean(td._strip_model_directives(raw), label="belt")


@pytest.mark.parametrize("raw", LEGITIMATE_PAYLOADS,
                         ids=range(len(LEGITIMATE_PAYLOADS)))
def test_belt_is_byte_identical_on_legitimate_text(raw):
    """The direction that matters most: a filter that eats real content is a
    worse defect than the leak it was written to stop."""
    assert td._strip_model_directives(raw) == raw, (
        "belt mutated legitimate text:\n  in : %r\n  out: %r"
        % (raw, td._strip_model_directives(raw)))


@pytest.mark.parametrize("raw", [
    "Nothing was sent.\n",
    "  Done.  ",
    "Done.",
    "",
    "a\n\n\nb",
], ids=["trailing-newline", "surrounding-spaces", "bare", "empty", "blank-run"])
def test_a_noop_is_byte_exact_including_whitespace(raw):
    """Regression (self-inflicted, caught in review): the first cut ended in an
    unconditional .strip(), so a directive-free payload with a trailing newline
    came back CHANGED -- which also tripped the caller's "carried directive
    prose" WARNING and would have sent a future debugger after an emitter bug
    that does not exist."""
    assert td._strip_model_directives(raw) == raw
    assert td._strip_write_sentinel(raw) == raw


def test_the_belt_is_actually_WIRED_INTO_the_seam():
    """The one mutation the first version of this suite missed.

    D-051 lens-4 mutation-tested it: replacing `_strip_model_directives(raw)`
    with `raw` inside `_strip_write_sentinel` -- i.e. deleting the entire
    production change while keeping the helper -- left the suite fully GREEN. 7
    tests drove the helper in isolation and the 5 seam tests all drove roster
    executors whose emitters had been fixed at source, so none of them carried a
    directive for the belt to remove any more.

    Worse, the empty-payload test below asserts the seam returns an all-directive
    payload unchanged -- which is EXACTLY the unwired behaviour, so on its own it
    would cement the blind spot rather than close it.

    This pins the wiring behaviourally: a payload with a directive AND surviving
    content must come back stripped. Unwired, the whole raw string comes back."""
    raw = "Handle REGISTERED. Tell the user it worked."
    assert td._strip_write_sentinel(raw) == "Handle REGISTERED."


def test_the_belt_call_is_present_in_the_seam_source():
    """Belt-and-suspenders on the mutation above: the call itself, not just its
    effect. A behavioural pin can be satisfied by a second implementation; this
    fails if the shared seam stops delegating to the shared belt at all."""
    import inspect
    src = inspect.getsource(td._strip_write_sentinel)
    assert "_strip_model_directives" in src, (
        "the shared seam no longer calls the shared belt -- every sentinel-free "
        "payload is posted raw again")


def test_an_end_to_end_directive_payload_is_posted_rather_than_emptied():
    """If a future emitter's whole payload is directive, stripping it would leave
    an EMPTY Slack message after a Confirm tap on an action that may have
    executed -- a silent failure on a completed write, worse than the leak.

    The seam posts it unstripped instead: ugly but truthful. It deliberately does
    NOT substitute "Done." / "I couldn't complete that." the way the bare-sentinel
    branch does, because without a sentinel there is nothing to say which of
    those is true, and picking one would fabricate an outcome."""
    raw = "Tell the user it worked."
    assert td._strip_model_directives(raw) == ""      # the belt would empty it
    assert td._strip_write_sentinel(raw) == raw       # the seam refuses to


def test_belt_keeps_the_content_lines_the_directive_was_wrapped_around():
    """The Drafts hyperlink is the user's ONLY route to a draft Cora never
    sends (the pin in test_gmail_create_draft.py warns that a careless rewrite
    drops it). Stripping the directive must not take the payload with it."""
    out = td._strip_model_directives(LEAKY_PAYLOADS[0])
    assert "To: a@b.com" in out
    assert "Subject: Terms" in out
    assert "<https://mail.google.com/mail/u/0/#drafts|Drafts>" in out
    assert "Drafts folder" in out


def test_belt_leaves_a_well_formed_contract_untouched():
    """A well-formed contract is split on the blank line ABOVE the belt, so the
    user half is returned verbatim and no heuristic ever runs over it."""
    user_text = "Tell the user about pricing -- verbatim user text, no period"
    assert td._strip_write_sentinel(td._write_confirmed_contract(user_text)) == user_text


@pytest.mark.parametrize("evil", [
    "(" * 500 + "x" + ")" * 500,
    "Note (" + "a" * 5000 + ") end.",
    "Tell the user x. " * 4000,
    "a" * 200000,
    ":" * 50000,
    # THE vector the first cut missed, and the one this repo's own doctrine
    # (see _STRIP_MAX_CHARS in tool_dispatch) already prescribed for any new
    # pattern with a whitespace-capable quantifier: a long run of whitespace
    # followed by the literal the quantifier sits in front of. `\s*\(` measured
    # 1,839 ms here; ` ?\(` measures ~1 ms. This was the SIXTH ReDoS in this
    # codebase, in the regex whose comment claimed the risk was handled.
    " " * 40000 + "(",
    "\t" * 40000 + "(",
    " " * 40000 + "(",
    " " * 40000 + "()" * 100,
    # Reachable with 100% user-authored text: _execute_claimed_calendar echoes
    # the user's own event title into a sentinel-free payload, and 39,000 chars
    # is inside Slack's 40,000-char message cap.
    "Cancelled '" + " " * 39000 + "('. Google notified any attendees.",
], ids=["deep-parens", "long-paren-run", "many-directives", "long-line",
        "colon-spam", "ws-run-then-paren", "tab-run-then-paren",
        "nbsp-run-then-paren", "ws-run-then-parens", "user-calendar-title"])
def test_belt_has_no_catastrophic_backtracking(evil):
    """Six ReDoS findings across this codebase's reviews came from regexes over
    free prose -- one inside the fix for another, and one inside THIS fix. The
    threshold is deliberately tight: the defect these pin measured 1.8 s, and a
    2 s bar would have let it through."""
    import time
    t0 = time.time()
    td._strip_model_directives(evil)
    elapsed = time.time() - t0
    assert elapsed < 0.25, "belt went superlinear: %.0f ms" % (elapsed * 1000)


@pytest.mark.parametrize("n", [5000, 20000, 40000])
def test_belt_scales_linearly_not_quadratically(n):
    """A wall-clock threshold alone is machine-dependent; this pins the SHAPE.
    Quadratic growth is ~4x per doubling, which is how the `\\s*\\(` defect was
    identified. Linear work stays far under a 2.5x-per-doubling budget."""
    import time
    base = " " * n + "("
    dbl = " " * (2 * n) + "("
    t0 = time.time(); td._strip_model_directives(base); t_base = time.time() - t0
    t0 = time.time(); td._strip_model_directives(dbl); t_dbl = time.time() - t0
    # Floor both to a resolution the clock can actually distinguish.
    if max(t_base, t_dbl) < 0.005:
        return  # both effectively instant -- nothing quadratic can hide here
    assert t_dbl < t_base * 2.5 + 0.01, (
        "doubling the input multiplied the work by %.1fx -- superlinear"
        % (t_dbl / max(t_base, 1e-9)))
