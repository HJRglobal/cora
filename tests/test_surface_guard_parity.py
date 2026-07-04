"""App-surface guard parity (W7-01, CRITICAL) + coverage guardrail (W7-06, MED).

WHY THIS EXISTS
---------------
Before D-068, every pre-LLM invariant (entity authorization via
user_access.check_access, the D-064 finance-topic deflection, the LEX PHI block,
the sibling / cross-entity firewalls) was tested ONLY in module isolation. No
test drove a real Bolt handler end-to-end through the guard chain, so the exact
D-068 defect class -- a gate that lives in a module but a handler simply forgets
to call it (or calls it out of order) before egress -- was structurally invisible
for the primary surfaces. handle_message_event Path 2 skipped check_access for
weeks and the green suite never noticed; only human review caught it.

WHAT THIS LOCKS
---------------
Every Q&A egress surface is driven end-to-end through the REAL guard chain,
mocking ONLY the LLM/egress (_dispatch_qa) + infra (rate limiter, channel-name
resolution, thread history, roster lookups). Two layers:

  * TestGatePresenceAndOrdering (guards MOCKED) -- pins that each surface calls
    check_access -> sibling -> cross in that order, refuses on any block, and
    reaches _dispatch_qa only when all three pass. If a handler drops or reorders
    a guard, this goes RED for that surface.

  * TestRealGuardChain (guards REAL) -- pins the behavioral invariants end-to-end
    per the W7-01 charter: (a) a financials-blocked role's company-P&L question
    deflects with no entity-code leak, (b) a cross-entity question redirects,
    (c) an unknown user fails closed (channel surfaces), (d) an authorized
    commercial question reaches _dispatch_qa.

The four surfaces (matching their enclosing app.py function names, which the
W7-06 guardrail below cross-checks against the live _dispatch_qa call-sites):

  handle_mention        -- @mention                 (say(...) refusals)
  handle_cora_ask       -- /cora-ask slash command  (client.chat_postEphemeral)
  _handle_dm_qa         -- plain-DM Q&A             (client.chat_postMessage)
  handle_message_event  -- Path 2 active-thread     (client.chat_postMessage)

Path 2 also has its own dedicated D-068 regression in
test_thread_followup_access.py; it is included here too so the coverage manifest
(COVERED_SURFACES) is complete and a future 5th surface cannot slip in ungated.

W7-06 (bottom of file) is the structural half: it parses app.py and FAILS if a
new _dispatch_qa call-site appears without a preceding check_access (anti-D-068),
or if a gated surface has no driver here (anti-regression coverage).
"""

import ast
import collections
import contextlib
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cora.app as app_module
import cora.entity_router as entity_router


# Real roster: F3E sales role, 'financials' + 'hr' blocked, authorized for F3E.
TOMMY = "U0B3RU5Q55G"
UNKNOWN = "U_UNKNOWN_USER_XYZ"
CHAN = "C123CHAN"                # not a blocked channel, not #info-for-cora
CHANNEL_NAME = "f3e-sales"       # non-silent, routes F3E, classifies TIER_3
ENTITY = "F3E"


# ── surface registry (also the W7-06 coverage manifest) ──────────────────────
SURFACES = [
    SimpleNamespace(name="handle_mention", is_dm=False),
    SimpleNamespace(name="handle_cora_ask", is_dm=False),
    SimpleNamespace(name="_handle_dm_qa", is_dm=True),
    SimpleNamespace(name="handle_message_event", is_dm=False),  # Path 2
]
SURFACE_NAMES = [s.name for s in SURFACES]
IS_DM = {s.name: s.is_dm for s in SURFACES}
COVERED_SURFACES = {s.name for s in SURFACES}

# Channel surfaces resolve the entity from the channel route (F3E), so an unknown
# user is refused. The DM surface resolves the entity from org-roles and falls
# back to FNDR, which unknown users MAY access -- handled separately below.
CHANNEL_SURFACES = [s.name for s in SURFACES if not s.is_dm]


@contextlib.contextmanager
def surface_ctx(surface, mode):
    """Patch infra shared by every surface; in ``mode='mock'`` also patch the four
    guards so gate PRESENCE/ORDERING can be asserted; in ``mode='real'`` leave the
    guards live so the behavioral invariants run end-to-end.

    Yields a namespace with ``.dispatch`` (mocked _dispatch_qa) and, in mock mode,
    ``.guards`` (access / sibling / cross / phi mocks).
    """
    with contextlib.ExitStack() as stack:
        p = stack.enter_context
        # infra shared by all surfaces
        p(patch.object(app_module.rate_limiter, "check", return_value=(True, None)))
        p(patch.object(app_module, "_resolve_channel_name", return_value=CHANNEL_NAME))
        p(patch.object(app_module, "_resolve_bot_user_id", return_value="UBOT"))
        p(patch.object(app_module, "route", return_value=ENTITY))
        p(patch.object(entity_router, "is_silent_channel", return_value=False))
        p(patch.object(app_module, "_fetch_thread_history", return_value=[]))
        p(patch.object(app_module, "_fetch_dm_history", return_value=[]))
        p(patch.object(app_module.help_responder, "is_help_intent", return_value=False))
        dispatch = p(patch.object(app_module, "_dispatch_qa"))

        # surface-specific infra
        if surface == "handle_mention":
            p(patch.object(app_module.team_learning, "parse_note", return_value=None))
        elif surface == "handle_message_event":
            p(patch.object(app_module.team_learning, "get_pending_confirm", return_value=None))
            p(patch.object(app_module.team_learning, "is_correction", return_value=False))
            p(patch.object(app_module.active_thread_store, "is_active", return_value=True))
            p(patch.object(app_module.active_thread_store, "touch"))
        elif surface == "_handle_dm_qa":
            def _role(uid):
                if uid == TOMMY:
                    return SimpleNamespace(primary_entity=ENTITY, name="Tommy Anderson")
                return None  # unknown -> entity falls back to FNDR
            p(patch.object(app_module.org_roles, "get_role", side_effect=_role))

        guards = SimpleNamespace()
        if mode == "mock":
            guards.phi = p(patch.object(app_module.lex_phi_access, "phi_allowed", return_value=False))
            guards.access = p(patch.object(app_module.user_access, "check_access", return_value=None))
            guards.sibling = p(patch.object(app_module.sibling_guard, "check_redirect", return_value=None))
            guards.cross = p(patch.object(app_module.cross_entity_guard, "check_cross_entity", return_value=None))
        yield SimpleNamespace(dispatch=dispatch, guards=guards)


def _invoke(surface, client, text, user):
    """Drive the real handler for ``surface``. Returns the ``say`` mock for the
    @mention surface (its refusals go there, not to ``client``); None otherwise."""
    if surface == "handle_mention":
        say = MagicMock()
        event = {"channel": CHAN, "user": user, "ts": "10.1", "text": f"<@UBOT> {text}"}
        app_module.handle_mention(event, say, client)
        return say
    if surface == "handle_cora_ask":
        ack = MagicMock()
        body = {"channel_id": CHAN, "user_id": user, "text": text}
        app_module.handle_cora_ask(ack, body, client)
        return None
    if surface == "_handle_dm_qa":
        event = {"channel": "D123DM", "ts": "5.1"}  # no thread_ts -> top-level DM
        app_module._handle_dm_qa(event, client, user, text)
        return None
    if surface == "handle_message_event":  # Path 2 active-thread follow-up
        event = {"user": user, "text": text, "channel": CHAN, "ts": "200.2",
                 "thread_ts": "200.1", "channel_type": "channel"}
        app_module.handle_message_event(event, client)
        return None
    raise AssertionError(f"unknown surface {surface!r}")


def _refusal_call(surface, client, say):
    """The last refusal *call* this surface posted (via its refusal sink), or None.

    Every non-guard exit is mocked away (rate allowed, help off, note off,
    _dispatch_qa stubbed), so any post carrying `text` on the surface's refusal
    sink is a guard refusal.
    """
    if surface == "handle_mention":
        sink = say
    elif surface == "handle_cora_ask":
        sink = client.chat_postEphemeral
    else:  # _handle_dm_qa, handle_message_event
        sink = client.chat_postMessage
    posts = [c for c in sink.call_args_list if c.kwargs.get("text")]
    return posts[-1] if posts else None


def _refusal_text(surface, client, say):
    """The last refusal text this surface posted, or None."""
    call = _refusal_call(surface, client, say)
    return call.kwargs["text"] if call is not None else None


def _assert_refusal_routed(surface, call):
    """A security refusal must land where the asker is: right channel, and — for
    threaded/ephemeral surfaces — the right thread / the asking user. (finding 2:
    _refusal_text alone proved text was emitted, not that it was routed correctly.)"""
    kw = call.kwargs
    if surface == "handle_mention":
        assert kw.get("thread_ts") == "10.1"                 # in-thread reply to the @mention
    elif surface == "handle_cora_ask":
        assert kw.get("channel") == CHAN and "user" in kw    # ephemeral, to the asker
    elif surface == "_handle_dm_qa":
        assert kw.get("channel") == "D123DM"                 # the asker's DM
    else:  # handle_message_event Path 2
        assert kw.get("channel") == CHAN and kw.get("thread_ts") == "200.1"


# ─────────────────────────────────────────────────────────────────────────────
# W7-01 layer 1 — gate PRESENCE + ORDERING (guards mocked)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("surface", SURFACE_NAMES)
class TestGatePresenceAndOrdering:
    def test_check_access_block_refuses_and_stops(self, surface):
        with surface_ctx(surface, "mock") as ctx:
            ctx.guards.access.return_value = "You're not authorized for that topic."
            client = MagicMock()
            say = _invoke(surface, client, "what's the plan", TOMMY)
            assert _refusal_text(surface, client, say) == "You're not authorized for that topic."
            ctx.dispatch.assert_not_called()
            # check_access is FIRST — a blocked question never reaches the others.
            ctx.guards.sibling.assert_not_called()
            ctx.guards.cross.assert_not_called()

    def test_all_guards_pass_reaches_dispatch(self, surface):
        with surface_ctx(surface, "mock") as ctx:
            client = MagicMock()
            say = _invoke(surface, client, "what's the plan", TOMMY)
            ctx.guards.access.assert_called_once()
            ctx.dispatch.assert_called_once()
            assert _refusal_text(surface, client, say) is None

    def test_sibling_redirect_refuses_before_cross(self, surface):
        with surface_ctx(surface, "mock") as ctx:
            ctx.guards.sibling.return_value = "That belongs in the LLC channel."
            client = MagicMock()
            say = _invoke(surface, client, "what's the plan", TOMMY)
            assert _refusal_text(surface, client, say) == "That belongs in the LLC channel."
            ctx.dispatch.assert_not_called()
            ctx.guards.cross.assert_not_called()  # sibling runs before cross

    def test_cross_redirect_refuses(self, surface):
        with surface_ctx(surface, "mock") as ctx:
            ctx.guards.cross.return_value = "Ask in the UFL channel."
            client = MagicMock()
            say = _invoke(surface, client, "what's the plan", TOMMY)
            assert _refusal_text(surface, client, say) == "Ask in the UFL channel."
            ctx.dispatch.assert_not_called()

    def test_check_access_params_mirror_across_surfaces(self, surface):
        with surface_ctx(surface, "mock") as ctx:
            client = MagicMock()
            _invoke(surface, client, "whats the plan", TOMMY)
            # f3e-sales -> TIER_3 for the channel surfaces; the DM pins TIER_3.
            ctx.guards.access.assert_called_once_with(
                TOMMY, ENTITY, "whats the plan", phi_custodian=False, tier="TIER_3",
            )

    def test_phi_custodian_flag_flows_to_check_access(self, surface):
        with surface_ctx(surface, "mock") as ctx:
            ctx.guards.phi.return_value = True
            client = MagicMock()
            _invoke(surface, client, "what's the plan", TOMMY)
            # phi_allowed is called with the surface's is_dm and its result is
            # threaded verbatim into check_access.
            assert ctx.guards.phi.call_args.kwargs["is_dm"] is IS_DM[surface]
            # Positional (user_id, entity) pinned too, so a wrong-entity / over-grant
            # call into phi_allowed is caught — not just the is_dm kwarg (finding 9).
            assert ctx.guards.phi.call_args.args == (TOMMY, ENTITY)
            assert ctx.guards.access.call_args.kwargs["phi_custodian"] is True

    def test_refusal_is_routed_to_asker(self, surface):
        with surface_ctx(surface, "mock") as ctx:
            ctx.guards.access.return_value = "blocked."
            client = MagicMock()
            say = _invoke(surface, client, "what's the plan", TOMMY)
            call = _refusal_call(surface, client, say)
            assert call is not None
            _assert_refusal_routed(surface, call)


# ─────────────────────────────────────────────────────────────────────────────
# W7-01 layer 2 — behavioral invariants end-to-end (guards REAL)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("surface", SURFACE_NAMES)
class TestRealGuardChain:
    def test_company_finance_deflected(self, surface):
        """(a) A financials-blocked sales role's company-P&L question is deflected
        pre-LLM by the REAL check_access (D-064). NOTE: the no-entity-code-leak
        invariant is asserted in test_unknown_user_fails_closed_on_channel_surfaces
        below — the financials deflection returns a fixed constant with no entity
        interpolation, so an entity-leak assertion here would be a tautology
        (finding 3). The real leak surface is the entity-authorization refusal."""
        with surface_ctx(surface, "real") as ctx:
            client = MagicMock()
            say = _invoke(surface, client, "what's our p&l this quarter?", TOMMY)
            ctx.dispatch.assert_not_called()
            assert _refusal_text(surface, client, say)   # a standard refusal was posted

    def test_cross_entity_question_redirects(self, surface):
        """(b) A cross-entity question passes check_access but is redirected by the
        REAL cross_entity_guard before any dispatch."""
        with surface_ctx(surface, "real") as ctx:
            client = MagicMock()
            say = _invoke(surface, client, "how is the UFL sponsorship pipeline?", TOMMY)
            ctx.dispatch.assert_not_called()
            assert _refusal_text(surface, client, say)

    def test_authorized_commercial_question_reaches_dispatch(self, surface):
        """(d) An authorized, deal-scoped commercial question passes the full real
        chain and reaches _dispatch_qa."""
        with surface_ctx(surface, "real") as ctx:
            client = MagicMock()
            say = _invoke(surface, client, "what's the price on the Reliant order?", TOMMY)
            ctx.dispatch.assert_called_once()
            assert _refusal_text(surface, client, say) is None


@pytest.mark.parametrize("surface", CHANNEL_SURFACES)
def test_unknown_user_fails_closed_on_channel_surfaces(surface):
    """(c) On channel-routed surfaces the entity is F3E, so an unknown user is
    refused by the REAL check_access before any dispatch. This is the entity-
    AUTHORIZATION refusal branch — the one that historically leaked an internal
    entity code (the #f3-events incident) — so it also pins no-code-leak here."""
    with surface_ctx(surface, "real") as ctx:
        client = MagicMock()
        say = _invoke(surface, client, "how are sales going?", UNKNOWN)
        ctx.dispatch.assert_not_called()
        text = _refusal_text(surface, client, say)
        assert text
        assert ENTITY not in text          # no internal entity-code leak (finding 3)


def test_dm_unknown_user_falls_back_to_fndr_and_passes():
    """DM entity resolves via org-roles; an unknown user resolves to FNDR, which
    unknown users MAY access (catch-all posture). Documents the by-design
    asymmetry vs the channel surfaces above so it can't silently change."""
    with surface_ctx("_handle_dm_qa", "real") as ctx:
        client = MagicMock()
        _invoke("_handle_dm_qa", client, "how are sales going?", UNKNOWN)
        ctx.dispatch.assert_called_once()


def test_real_guard_roster_preconditions():
    """The real-guard tests above assert fixed outcomes for TOMMY read through the
    live user-permissions roster + the in-code D-064 classifier. Pin those
    preconditions HERE so a future roster/classifier drift fails with a clear,
    self-explaining message instead of surfacing as a misleading 'dispatch was
    (not) called' failure deep in a parametrized guard test (finding 10)."""
    from cora import user_access

    assert user_access.check_access(
        TOMMY, ENTITY, "what's our p&l this quarter?", phi_custodian=False, tier="TIER_3"
    ), "roster drift: TOMMY must be F3E-authorized with 'financials' blocked (D-064)"
    assert user_access.check_access(
        TOMMY, ENTITY, "what's the price on the Reliant order?", phi_custodian=False, tier="TIER_3"
    ) is None, "D-064 classifier drift: a commercial deal question must pass for a sales role"
    assert user_access.check_access(
        UNKNOWN, ENTITY, "how are sales going?", phi_custodian=False, tier="TIER_3"
    ), "roster drift: an unknown user must fail closed for a scoped entity"


# ─────────────────────────────────────────────────────────────────────────────
# W7-06 — structural coverage guardrail
#
# Parse app.py and enforce, for every _dispatch_qa call-site, that a
# user_access.check_access call precedes it in the same function (anti-D-068),
# unless the site is on a documented by-design allowlist. Then enforce that every
# gated surface has an end-to-end driver in this module (anti-regression).
# ─────────────────────────────────────────────────────────────────────────────
_APP_TREE = ast.parse(pathlib.Path(app_module.__file__).read_text(encoding="utf-8"))

# Un-gated _dispatch_qa sites intentionally guarded by a DIFFERENT mechanism than
# user_access.check_access. Key = (enclosing_function, entity-kwarg literal).
_UNGATED_ALLOWLIST = {
    ("handle_message_event", "FNDR"): (
        "DM Tier-2 historical retrieval — access control is the internal "
        "historical_access gate inside _dispatch_qa (W1-03 / W2-04, by design)."
    ),
}


def _func_ranges():
    ranges = []
    for node in ast.walk(_APP_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ranges.append((node.name, node.lineno, node.end_lineno))
    return ranges


def _enclosing_func(lineno, ranges):
    """Innermost function whose line range contains ``lineno`` (nested defs win)."""
    best = None
    for name, start, end in ranges:
        if start <= lineno <= end and (best is None or (end - start) < (best[2] - best[1])):
            best = (name, start, end)
    return best[0] if best else None


def _entity_literal(call):
    for kw in call.keywords:
        if kw.arg == "entity":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            return "<dynamic>"
    return "<dynamic>"


def _collect_sites():
    ranges = _func_ranges()
    dispatches = []           # (func, lineno, entity_literal)
    access_by_func = {}       # func -> [linenos of check_access calls]
    for node in ast.walk(_APP_TREE):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == "_dispatch_qa":
            dispatches.append((_enclosing_func(node.lineno, ranges), node.lineno, _entity_literal(node)))
        elif isinstance(f, ast.Attribute) and f.attr == "check_access":
            access_by_func.setdefault(_enclosing_func(node.lineno, ranges), []).append(node.lineno)
    return dispatches, access_by_func


def _is_gated(func, line, access_by_func):
    # COARSE line-positional proxy: a check_access earlier in the same function.
    # Sound for the current handlers (each check_access is a straight-line prologue
    # statement, so everything after it on that function body is genuinely gated),
    # but it is NOT full control-flow dominance. The robust backstops behind it are
    # test_dispatch_site_inventory_is_pinned (any NEW/moved/duplicated dispatch trips
    # a manifest update -> conscious re-verification) and the per-surface drivers
    # above (the real guarantee per surface). See findings 1/4/6/7/11.
    return any(a < line for a in access_by_func.get(func, []))


# ── dispatch-site inventory (the robust anti-D-068 backstop) ─────────────────
# A pinned multiset of every _dispatch_qa call-site as (enclosing_function,
# entity-literal). ANY added / removed / moved / DUPLICATED dispatch changes this
# and forces a conscious update here — closing the two future-regression holes the
# coarse line-positional/allowlist-keyed checks can't: (A) a new ungated dispatch
# added downstream of an existing check_access, and (B) a SECOND ungated
# entity='FNDR' dispatch the (func,entity) allowlist key would otherwise excuse.
_EXPECTED_DISPATCH_SITES = collections.Counter({
    ("handle_cora_ask", "<dynamic>"): 1,
    ("handle_mention", "<dynamic>"): 1,
    ("_handle_dm_qa", "<dynamic>"): 1,
    ("handle_message_event", "FNDR"): 1,        # DM Tier-2 historical retrieval (allowlisted)
    ("handle_message_event", "<dynamic>"): 1,   # Path 2 active-thread follow-up (gated)
})


def test_dispatch_site_inventory_is_pinned():
    dispatches, _ = _collect_sites()
    live = collections.Counter((func, ent) for func, _line, ent in dispatches)
    assert live == _EXPECTED_DISPATCH_SITES, (
        "The set of _dispatch_qa call-sites in app.py changed.\n"
        f"  added:   {dict(live - _EXPECTED_DISPATCH_SITES)}\n"
        f"  removed: {dict(_EXPECTED_DISPATCH_SITES - live)}\n"
        "Update _EXPECTED_DISPATCH_SITES — AND for any NEW site confirm it runs "
        "check_access before dispatch (or add a documented _UNGATED_ALLOWLIST entry) "
        "and give its surface a driver in SURFACES (W7-01/W7-06 anti-D-068)."
    )


def test_ungated_sites_match_allowlist_one_to_one():
    """Cluster-B: the allowlist key is (func, entity), so a SECOND ungated dispatch
    sharing an allowlisted key would be silently excused by membership alone. Pin
    the COUNT: the multiset of ungated sites must equal the allowlist 1:1, so a
    duplicated ungated FNDR dispatch (count 2 vs 1) goes RED (findings 5/8)."""
    dispatches, access_by_func = _collect_sites()
    ungated = collections.Counter(
        (func, ent) for func, line, ent in dispatches
        if not _is_gated(func, line, access_by_func)
    )
    expected = collections.Counter({k: 1 for k in _UNGATED_ALLOWLIST})
    assert ungated == expected, (
        f"ungated dispatch sites {dict(ungated)} do not match the allowlist "
        f"{dict(expected)} 1:1. A new/duplicate ungated dispatch must earn its own "
        "documented _UNGATED_ALLOWLIST entry (and be re-reviewed as by-design)."
    )


def test_every_dispatch_site_is_gated_or_allowlisted():
    """Anti-D-068: a _dispatch_qa call with no preceding check_access must be an
    explicit, documented by-design exception — otherwise it is a missing gate."""
    dispatches, access_by_func = _collect_sites()
    assert dispatches, "no _dispatch_qa call-sites found — AST anchor broke"
    for func, line, ent in dispatches:
        if _is_gated(func, line, access_by_func):
            continue
        assert (func, ent) in _UNGATED_ALLOWLIST, (
            f"_dispatch_qa at {func}:{line} (entity={ent}) has NO preceding "
            f"user_access.check_access and is not on the by-design allowlist. "
            f"Every Q&A surface must run check_access before dispatch (D-068). "
            f"If this is deliberately guarded another way, add it to "
            f"_UNGATED_ALLOWLIST with a reason."
        )


def test_every_gated_surface_has_an_integration_driver():
    """Anti-regression: a new gated Q&A surface must gain a driver here (i.e. be in
    COVERED_SURFACES / SURFACES) so its guard chain is pinned end-to-end."""
    dispatches, access_by_func = _collect_sites()
    for func, line, _ent in dispatches:
        if not _is_gated(func, line, access_by_func):
            continue
        assert func in COVERED_SURFACES, (
            f"gated Q&A surface {func!r} (dispatch at line {line}) has no "
            f"end-to-end driver in this module. Add it to SURFACES so its "
            f"check_access -> sibling -> cross chain is regression-pinned (W7-01/W7-06)."
        )


def test_allowlist_has_no_stale_entries():
    """Guard the guardrail: every allowlisted site must still exist as a live
    un-gated dispatch, so the allowlist can't quietly excuse a future new gap."""
    dispatches, access_by_func = _collect_sites()
    ungated = {
        (func, ent) for func, line, ent in dispatches
        if not _is_gated(func, line, access_by_func)
    }
    stale = set(_UNGATED_ALLOWLIST) - ungated
    assert not stale, f"stale _UNGATED_ALLOWLIST entries (no matching un-gated site): {stale}"
