"""Tests for the Deposco write path (Phase 2/3 order push).

Organized around the SONNET-HANDOFF step 3 pin requirements: exactly one
mutating verb literal, exactly one route, and a prod gate that is
structurally impossible to bypass from a script or a REPL.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from cora.connectors import deposco_client as dc
from cora.connectors import deposco_push as dp

_SOURCE = Path(dp.__file__).read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """The module's EXECUTABLE code, with comments and docstrings removed --
    see test_deposco_client.py for why a raw-source grep is the wrong tool
    here (the module docstring quite reasonably *names* the verbs it forbids)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


_CODE = _code_only(_SOURCE)

USER = "prod-user"
PASSWORD = "s3cr3t-prod-pass"


class FakeResponse:
    def __init__(self, status_code=201, text="", content_type="application/json"):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}


class FakeTransport:
    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers, body):
        self.calls.append({"url": url, "headers": headers, "body": body})
        if not self._queue:
            return FakeResponse(201, '{"order": [{"status": "201 Created"}]}')
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def prod_env(monkeypatch):
    monkeypatch.setenv("DEPOSCO_PROD_USER", USER)
    monkeypatch.setenv("DEPOSCO_PROD_PASS", PASSWORD)
    monkeypatch.setenv("DEPOSCO_TENANT", "ESM")
    monkeypatch.setenv("DEPOSCO_BU", "F3E")
    monkeypatch.delenv("CORA_DEPOSCO_PUSH_CHANNELS", raising=False)


@pytest.fixture
def ua_env(monkeypatch):
    monkeypatch.setenv("DEPOSCO_UA_USER", "F3 API Integration")
    monkeypatch.setenv("DEPOSCO_UA_PASS", "ua-pass")
    monkeypatch.setenv("DEPOSCO_TENANT", "ESM")
    monkeypatch.setenv("DEPOSCO_BU", "F3E")


def push_client(env="prod", transport=None):
    return dp.DeposcoPushClient(env=env, transport=transport, pace_seconds=0)


PAYLOAD = {"order": [{"number": "TEST-GOTHAM-003", "type": "Sales Order"}]}


# ── Write-surface pins ────────────────────────────────────────────────────────


class TestWriteSurfacePins:
    def test_exactly_one_post_literal(self):
        found = re.findall(r"""['"](POST)['"]""", _CODE)
        assert len(found) == 1, f"expected exactly one POST literal, found {found}"

    def test_no_other_mutating_verb_literal(self):
        found = set(re.findall(r"""['"](PUT|PATCH|DELETE)['"]""", _CODE))
        assert not found, f"mutating HTTP verb string present: {found}"

    def test_no_get_verb_literal_in_this_modules_own_code(self):
        """This module never issues its own GET -- reads belong to
        deposco_client, imported, never reimplemented here."""
        assert not re.findall(r"""['"]GET['"]""", _CODE)

    def test_exactly_one_route_literal(self):
        # ast.unparse's quote-style choice is an implementation detail (it
        # rendered single-quoted in practice) -- match either, same idiom
        # test_deposco_client.py uses for its own verb-literal pins.
        assert len(re.findall(r"""['"]/orders['"]""", _CODE)) == 1

    def test_no_order_type_subpath_route_ever_appears(self):
        """Guards against a future edit reintroducing the dead, space-broken
        direct routes (deposco_client finding 3)."""
        assert "/orders/Sales" not in _CODE
        assert "/orders/{" not in _CODE

    def test_client_exposes_no_other_mutating_method(self):
        for verb in ("put", "patch", "delete", "get"):
            assert not hasattr(dp.DeposcoPushClient, verb), \
                f"DeposcoPushClient.{verb} must not exist"

    def test_only_one_request_primitive(self):
        assert _CODE.count("client.request(") == 1
        assert "httpx.Client(" in _CODE

    def test_credentials_are_never_reimplemented_here(self):
        """This module must reuse deposco_client's credential handling, never
        its own base64/Basic-auth construction."""
        assert "base64" not in _CODE
        assert "Basic " not in _CODE


# ── Prod gate: structurally impossible to bypass ─────────────────────────────


class TestProdGate:
    def test_prod_push_with_no_pending_id_is_refused_before_any_network_call(self, prod_env):
        t = FakeTransport()
        c = push_client(env="prod", transport=t)
        with pytest.raises(dp.DeposcoPushRefused, match="claimed pending"):
            c.push_order(PAYLOAD, channel="wholesale", claimed_pending_id=None)
        assert t.calls == [], "no request should ever be sent"

    def test_prod_push_with_empty_string_pending_id_is_also_refused(self, prod_env):
        t = FakeTransport()
        c = push_client(env="prod", transport=t)
        with pytest.raises(dp.DeposcoPushRefused):
            c.push_order(PAYLOAD, channel="wholesale", claimed_pending_id="   ")
        assert t.calls == []

    def test_prod_push_with_a_pending_id_but_no_channel_allowlist_is_refused(self, prod_env, monkeypatch):
        monkeypatch.delenv("CORA_DEPOSCO_PUSH_CHANNELS", raising=False)
        t = FakeTransport()
        c = push_client(env="prod", transport=t)
        with pytest.raises(dp.DeposcoPushRefused, match="wholesale"):
            c.push_order(PAYLOAD, channel="wholesale", claimed_pending_id="pend-1")
        assert t.calls == []

    def test_prod_push_refused_when_channel_not_in_the_allowlist(self, prod_env, monkeypatch):
        monkeypatch.setenv("CORA_DEPOSCO_PUSH_CHANNELS", "fba")
        t = FakeTransport()
        c = push_client(env="prod", transport=t)
        with pytest.raises(dp.DeposcoPushRefused):
            c.push_order(PAYLOAD, channel="wholesale", claimed_pending_id="pend-1")
        assert t.calls == []

    def test_prod_push_allowed_when_both_conditions_are_met(self, prod_env, monkeypatch):
        monkeypatch.setenv("CORA_DEPOSCO_PUSH_CHANNELS", "wholesale,fba")
        t = FakeTransport(FakeResponse(201, "201 Created"))
        c = push_client(env="prod", transport=t)
        outcome = c.push_order(PAYLOAD, channel="wholesale", claimed_pending_id="pend-1")
        assert len(t.calls) == 1
        assert outcome.status == 201

    def test_allowlist_is_comma_separated_and_trims_whitespace(self, prod_env, monkeypatch):
        monkeypatch.setenv("CORA_DEPOSCO_PUSH_CHANNELS", " wholesale , fba ")
        assert dp._push_channels_allowlist() == {"wholesale", "fba"}

    def test_ua_push_is_never_gated(self, ua_env):
        """The whole point of Phase 2 is rehearsing the write path somewhere
        it cannot cost anything -- UA carries no channel/id restriction."""
        t = FakeTransport(FakeResponse(201, "201 Created"))
        c = push_client(env="ua", transport=t)
        outcome = c.push_order(PAYLOAD, channel="wholesale", claimed_pending_id=None)
        assert len(t.calls) == 1
        assert outcome.status == 201


# ── Outcome facts (classification is one layer up, in deposco_orders.handler) ─


class TestMultistatusBodyParsing:
    """LIVE FINDING 2026-09-23 (UA): Deposco wraps every /orders response in a
    multistatus envelope -- outer HTTP status observed 207 for BOTH a real
    create and a genuine duplicate, with the true result as TEXT inside the
    body. `created`/`updated`/`conflict` are the derived facts; `is_2xx` alone
    is never a create/update/conflict signal for this route."""

    CREATE_BODY = (
        '<ns2:multistatus><response><status>HTTP/1.1 201 Created</status>'
        '<entity>F3E-W-GOTHAM-TEST003</entity></response></ns2:multistatus>'
    )
    CONFLICT_BODY = (
        '<ns2:multistatus><response><status>HTTP/1.1 409 Conflict</status>'
        "<description>Duplicate entry '181-Sales Order-F3E-W-GOTHAM-TEST003' "
        "for key 'ORDER_HEADER.ux_oh_bu_type_number'</description>"
        '</response></ns2:multistatus>'
    )
    UPDATE_BODY = (
        '<ns2:multistatus><response><status>HTTP/1.1 200 Updated</status>'
        '</response></ns2:multistatus>'
    )

    def test_a_real_create_is_recognized_via_the_body_not_the_outer_status(self, ua_env):
        """The outer status was observed 207, NOT 201 -- a check against
        outer `status == 201` alone would mis-classify every real create."""
        t = FakeTransport(FakeResponse(207, self.CREATE_BODY))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.status == 207
        assert outcome.created is True
        assert outcome.updated is False
        assert outcome.conflict is False

    def test_a_genuine_duplicate_is_409_conflict_in_the_body_not_a_silent_200(self, ua_env):
        """The design's D-182 premise (a 200 silently updates) was drawn from
        Deposco's documentation and never empirically tested before this
        build; live, a duplicate returned 409 Conflict, not 200."""
        t = FakeTransport(FakeResponse(207, self.CONFLICT_BODY))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.created is False
        assert outcome.updated is False
        assert outcome.conflict is True

    def test_an_update_body_is_still_recognized_defensively(self, ua_env):
        """D-182's own doctrine cuts both ways: an unobserved case is not a
        disproven one. The check costs nothing to keep."""
        t = FakeTransport(FakeResponse(207, self.UPDATE_BODY))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.updated is True
        assert outcome.created is False

    def test_created_and_updated_are_mutually_exclusive(self, ua_env):
        """A body that happens to carry both substrings must not fire both --
        created takes priority (it is checked first)."""
        both = self.CREATE_BODY + " Updated"
        t = FakeTransport(FakeResponse(207, both))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.created is True
        assert outcome.updated is False

    def test_blank_after_retries_is_none_of_the_three(self, ua_env):
        t = FakeTransport(*[FakeResponse(200, "   ")] * dc.BLANK_200_RETRIES)
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.created is False
        assert outcome.updated is False
        assert outcome.conflict is False

    def test_is_2xx_true_for_207_does_not_distinguish_create_from_conflict(self, ua_env):
        """207 is numerically within [200,300), so `is_2xx` is True for BOTH
        a create and a conflict -- it is a raw HTTP-layer fact, not an order
        outcome. `created`/`updated`/`conflict` are the ones that distinguish
        them; a caller must never use `is_2xx` for that."""
        create = push_client(env="ua", transport=FakeTransport(
            FakeResponse(207, self.CREATE_BODY))).push_order(PAYLOAD, channel="wholesale")
        conflict = push_client(env="ua", transport=FakeTransport(
            FakeResponse(207, self.CONFLICT_BODY))).push_order(PAYLOAD, channel="wholesale")
        assert create.is_2xx is True and create.created is True
        assert conflict.is_2xx is True and conflict.conflict is True


class TestPushOutcomeFacts:
    def test_201_is_2xx(self, ua_env):
        t = FakeTransport(FakeResponse(201, "201 Created"))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.status == 201
        assert outcome.is_2xx is True

    def test_200_is_2xx_but_never_asserted_as_create(self, ua_env):
        """D-182: a 200 here means an existing order was silently UPDATED.
        is_2xx alone must never be mistaken for CONFIRMED-create."""
        t = FakeTransport(FakeResponse(200, '{"order": [{"status": "Updated"}]}'))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.status == 200
        assert outcome.is_2xx is True

    def test_blank_200_retries_then_reports_blank_after_retries(self, ua_env):
        t = FakeTransport(*[FakeResponse(200, "   ")] * dc.BLANK_200_RETRIES)
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.blank_after_retries is True
        assert outcome.is_2xx is False
        assert len(t.calls) == dc.BLANK_200_RETRIES

    def test_blank_200_that_recovers_is_not_reported_as_blank(self, ua_env):
        t = FakeTransport(FakeResponse(200, ""), FakeResponse(201, "201 Created"))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.blank_after_retries is False
        assert outcome.status == 201

    def test_4xx_is_returned_not_raised(self, ua_env):
        """A 4xx is a per-order business outcome (FAILED), not a structural
        problem -- it must reach the caller as data, not an exception."""
        t = FakeTransport(FakeResponse(400, "missingItems: F3-BOGUS"))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.status == 400
        assert "missingItems" in outcome.text
        assert outcome.is_2xx is False

    def test_transient_5xx_is_retried_then_reports_network_exhausted(self, ua_env):
        t = FakeTransport(*[FakeResponse(503, "nope")] * (dc.TRANSIENT_RETRIES + 1))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.network_exhausted is True
        assert outcome.status == 503

    def test_network_error_exhaustion_leaves_status_none(self, ua_env):
        t = FakeTransport(*[dc.httpx.ConnectError("boom")] * (dc.TRANSIENT_RETRIES + 1))
        outcome = push_client(env="ua", transport=t).push_order(PAYLOAD, channel="wholesale")
        assert outcome.network_exhausted is True
        assert outcome.status is None

    def test_401_raises_auth_error_scrubbed_of_credentials(self, ua_env):
        t = FakeTransport(FakeResponse(401, "denied"))
        c = push_client(env="ua", transport=t)
        with pytest.raises(dc.DeposcoAuthError) as exc:
            c.push_order(PAYLOAD, channel="wholesale")
        assert "ua-pass" not in str(exc.value)


# ── Credential/environment reuse (composition, not reimplementation) ─────────


class TestSharedPlumbing:
    def test_missing_prod_credentials_raises_the_same_auth_error(self, monkeypatch):
        monkeypatch.delenv("DEPOSCO_PROD_USER", raising=False)
        monkeypatch.delenv("DEPOSCO_PROD_PASS", raising=False)
        with pytest.raises(dc.DeposcoAuthError):
            dp.DeposcoPushClient(env="prod", pace_seconds=0)

    def test_ua_and_prod_hit_different_hosts(self, ua_env, prod_env):
        assert "sandboxapi.deposco.com" in push_client("ua").base_url
        assert "api.deposco.com" in push_client("prod").base_url

    def test_repr_carries_no_credential(self, ua_env):
        assert "ua-pass" not in repr(push_client("ua"))
