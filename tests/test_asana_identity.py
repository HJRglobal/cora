"""Asana identity resolver (Code #13 RIDER 1 / S-B, bundle code-identity-v1).

Pins the token-selection matrix, that EVERY Asana seam (three _pat() functions +
the scripts) routes through cora.asana_identity, that the default (flag unset)
is byte-identical to the pre-S-B read of ASANA_PAT, that the health check is
identity-aware, that no error message ever carries a token value, and the
<=1 adjacency (the self-inventory Asana family line names the active identity).

Fixture tokens are synthetic strings that match no real secret shape.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import asana_identity as ai  # noqa: E402
from cora.connectors import asana_connector  # noqa: E402
from cora.tools import asana_client, lex_client  # noqa: E402

_H = "harrison-token-fixture"
_C = "cora-token-fixture"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ai.IDENTITY_ENV, raising=False)
    monkeypatch.setenv(ai.HARRISON_PAT_ENV, _H)
    monkeypatch.setenv(ai.CORA_PAT_ENV, _C)


# --- token selection matrix ---

class TestTokenSelection:
    def test_unset_flag_selects_harrison_pat(self):
        assert ai.active_identity() == "harrison"
        assert ai.resolve_pat() == (_H, "harrison")
        assert ai.active_pat_key() == "ASANA_PAT"

    def test_harrison_flag_selects_harrison_pat(self, monkeypatch):
        monkeypatch.setenv(ai.IDENTITY_ENV, "harrison")
        assert ai.resolve_pat() == (_H, "harrison")

    def test_cora_flag_selects_cora_pat(self, monkeypatch):
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        assert ai.resolve_pat() == (_C, "cora")
        assert ai.active_pat_key() == "ASANA_PAT_CORA"

    @pytest.mark.parametrize("raw", ["Cora", " cora ", "CORA"])
    def test_flag_is_case_and_whitespace_tolerant(self, monkeypatch, raw):
        monkeypatch.setenv(ai.IDENTITY_ENV, raw)
        assert ai.active_identity() == "cora"

    def test_cora_with_key_missing_hard_raises_never_falls_back(self, monkeypatch):
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        monkeypatch.delenv(ai.CORA_PAT_ENV, raising=False)
        with pytest.raises(ai.AsanaIdentityError, match="ASANA_PAT_CORA") as ei:
            ai.resolve_pat()
        # The privileged token is present and must NOT have been returned or named.
        assert _H not in str(ei.value)

    def test_cora_with_key_blank_hard_raises(self, monkeypatch):
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        monkeypatch.setenv(ai.CORA_PAT_ENV, "   ")
        with pytest.raises(ai.AsanaIdentityError, match="ASANA_PAT_CORA"):
            ai.resolve_pat()

    def test_harrison_with_key_missing_raises_naming_asana_pat(self, monkeypatch):
        monkeypatch.delenv(ai.HARRISON_PAT_ENV, raising=False)
        with pytest.raises(ai.AsanaIdentityError, match="ASANA_PAT") as ei:
            ai.resolve_pat()
        assert "ASANA_PAT_CORA" not in str(ei.value)
        assert _C not in str(ei.value)

    @pytest.mark.parametrize("raw", ["bot", "both", "1", "true", "harrison,cora"])
    def test_unknown_flag_value_raises(self, monkeypatch, raw):
        monkeypatch.setenv(ai.IDENTITY_ENV, raw)
        with pytest.raises(ai.AsanaIdentityError, match="CORA_ASANA_IDENTITY"):
            ai.active_identity()
        with pytest.raises(ai.AsanaIdentityError):
            ai.resolve_pat()

    def test_unknown_flag_value_is_never_echoed(self, monkeypatch):
        # A secret pasted into the wrong .env line must not surface via the error.
        monkeypatch.setenv(ai.IDENTITY_ENV, "pasted-secret-shaped-value-fixture")
        with pytest.raises(ai.AsanaIdentityError) as ei:
            ai.active_identity()
        assert "pasted-secret" not in str(ei.value)

    def test_resolve_pat_for_explicit_identity(self, monkeypatch):
        assert ai.resolve_pat_for("harrison") == _H
        assert ai.resolve_pat_for("cora") == _C
        monkeypatch.delenv(ai.CORA_PAT_ENV, raising=False)
        with pytest.raises(ai.AsanaIdentityError, match="ASANA_PAT_CORA"):
            ai.resolve_pat_for("cora")
        with pytest.raises(ai.AsanaIdentityError):
            ai.resolve_pat_for("nobody")

    def test_pat_key_for_is_a_pure_map(self):
        assert ai.pat_key_for("harrison") == "ASANA_PAT"
        assert ai.pat_key_for("cora") == "ASANA_PAT_CORA"
        with pytest.raises(ai.AsanaIdentityError):
            ai.pat_key_for("admin")


# --- every seam routed; default byte-identical ---

_SEAMS = [
    (asana_client, asana_client.AsanaClientError),
    (asana_connector, asana_connector.AsanaConnectorError),
    (lex_client, lex_client.LexClientError),
]


class TestSeamsRouted:
    @pytest.mark.parametrize("mod,_err", _SEAMS, ids=[m.__name__ for m, _ in _SEAMS])
    def test_default_is_byte_identical_to_the_pre_sb_read(self, mod, _err):
        # Flag unset -> exactly os.environ["ASANA_PAT"], the pre-S-B behaviour.
        assert mod._pat() == os.environ["ASANA_PAT"] == _H

    @pytest.mark.parametrize("mod,_err", _SEAMS, ids=[m.__name__ for m, _ in _SEAMS])
    def test_cora_identity_reaches_every_seam(self, monkeypatch, mod, _err):
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        assert mod._pat() == _C

    @pytest.mark.parametrize("mod,err", _SEAMS, ids=[m.__name__ for m, _ in _SEAMS])
    def test_cora_missing_key_raises_the_seams_own_error_type(self, monkeypatch, mod, err):
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        monkeypatch.delenv(ai.CORA_PAT_ENV, raising=False)
        with pytest.raises(err, match="ASANA_PAT_CORA") as ei:
            mod._pat()
        assert _H not in str(ei.value)  # never the fallback token, never its value

    @pytest.mark.parametrize("mod,err", _SEAMS, ids=[m.__name__ for m, _ in _SEAMS])
    def test_harrison_missing_key_keeps_the_legacy_error_contract(self, monkeypatch, mod, err):
        # tests/test_lex_tools.py pins match="ASANA_PAT" on this path -- preserved.
        monkeypatch.delenv(ai.HARRISON_PAT_ENV, raising=False)
        with pytest.raises(err, match="ASANA_PAT"):
            mod._pat()

    def test_asana_client_header_carries_the_cora_token(self, monkeypatch):
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        resp = MagicMock(status_code=200, text="")
        resp.json.return_value = {"data": [], "next_page": None}
        client = MagicMock()
        client.get.return_value = resp
        cm = MagicMock()
        cm.__enter__.return_value = client
        cm.__exit__.return_value = False
        with patch.object(asana_client.httpx, "Client", MagicMock(return_value=cm)):
            asana_client.get_user_tasks("123")
        assert client.get.call_args.kwargs["headers"]["Authorization"] == f"Bearer {_C}"


_DIRECT_READ_RE = re.compile(
    r"""(?:environ\.get|getenv)\s*\(\s*["']ASANA_PAT(?:_CORA)?["']|environ\s*\[\s*["']ASANA_PAT(?:_CORA)?["']\]""")


class TestNoDirectReadsRemain:
    """Source pin: the resolver is the ONLY reader of either PAT key in src/ AND
    scripts/. A new direct read would re-open the silent-Harrison-fallback door."""

    def test_no_direct_asana_pat_read_outside_the_resolver(self):
        offenders = []
        for base in (_REPO_ROOT / "src" / "cora", _REPO_ROOT / "scripts"):
            for path in base.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                rel = path.relative_to(_REPO_ROOT).as_posix()
                if rel == "src/cora/asana_identity.py":
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if _DIRECT_READ_RE.search(text):
                    offenders.append(rel)
        assert not offenders, (
            "direct ASANA_PAT / ASANA_PAT_CORA env read(s) outside cora.asana_identity "
            "-- route through resolve_pat(): " + ", ".join(sorted(offenders)))

    def test_config_py_keeps_only_its_validation_load(self):
        # config.py's get("ASANA_PAT") is the key-shape validator, not a consumer;
        # the comment there must say so and nothing may read config.asana_pat.
        src = (_REPO_ROOT / "src" / "cora").rglob("*.py")
        readers = [p.relative_to(_REPO_ROOT).as_posix() for p in src
                   if p.name != "config.py" and ".asana_pat" in p.read_text(encoding="utf-8", errors="ignore")]
        assert readers == []

    def test_the_seams_name_the_resolver(self):
        for mod in (asana_client, asana_connector, lex_client):
            text = Path(mod.__file__).read_text(encoding="utf-8")
            assert "asana_identity" in text, mod.__name__


# --- health check identity-aware ---

import nightly_health_check as hc  # noqa: E402

_ALL_OTHER_REQUIRED = list(hc._REQUIRED_ENV_VARS)


@pytest.fixture()
def _all_required_present(monkeypatch):
    for k in _ALL_OTHER_REQUIRED:
        monkeypatch.setenv(k, "present-fixture")


class TestHealthCheckIdentityAware:
    def test_static_list_no_longer_hardcodes_asana_pat(self):
        assert "ASANA_PAT" not in hc._REQUIRED_ENV_VARS
        assert "ASANA_PAT_CORA" not in hc._REQUIRED_ENV_VARS

    def test_required_vars_follow_the_active_identity(self, monkeypatch):
        assert "ASANA_PAT" in hc._required_env_vars()
        assert "ASANA_PAT_CORA" not in hc._required_env_vars()
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        assert "ASANA_PAT_CORA" in hc._required_env_vars()
        assert "ASANA_PAT" not in hc._required_env_vars()

    def test_ok_detail_names_the_identity_and_key(self, _all_required_present):
        r = hc.check_env_vars()
        assert r.status == "ok"
        assert "harrison" in r.detail and "ASANA_PAT" in r.detail

    def test_cora_identity_missing_key_is_critical_naming_the_key(self, monkeypatch, _all_required_present):
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        monkeypatch.delenv(ai.CORA_PAT_ENV, raising=False)
        r = hc.check_env_vars()
        assert r.status == "critical"
        assert "ASANA_PAT_CORA" in r.detail and "cora" in r.detail
        assert _H not in r.detail

    def test_cora_identity_does_not_demand_harrisons_key(self, monkeypatch, _all_required_present):
        # Day-14: Harrison removes ASANA_PAT; the check must stay green under cora.
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        monkeypatch.delenv(ai.HARRISON_PAT_ENV, raising=False)
        r = hc.check_env_vars()
        assert r.status == "ok" and "cora" in r.detail

    def test_invalid_flag_is_critical_naming_the_flag(self, monkeypatch, _all_required_present):
        monkeypatch.setenv(ai.IDENTITY_ENV, "bot")
        r = hc.check_env_vars()
        assert r.status == "critical"
        assert "CORA_ASANA_IDENTITY" in r.detail
        assert "bot" not in r.detail  # the raw flag value is never echoed

    def test_api_connectivity_uses_the_active_token_and_names_identity(self, monkeypatch):
        import httpx
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "nonexistent-fixture.json")
        seen: dict[str, str] = {}

        def fake_get(url, headers=None, timeout=None, **_kw):
            r = MagicMock(status_code=200)
            if "asana.com" in url:
                seen["auth"] = (headers or {}).get("Authorization", "")
                r.json.return_value = {"data": {"name": "Cora Bot", "gid": "1"}}
            else:
                r.json.return_value = {"ok": True, "user": "x", "data": []}
            return r

        monkeypatch.setattr(httpx, "get", fake_get)
        results = hc.check_api_connectivity()
        asana = [r for r in results if r.name == "Asana API"]
        assert len(asana) == 1
        assert asana[0].status == "ok"
        assert "identity: cora" in asana[0].detail and "Cora Bot" in asana[0].detail
        assert seen["auth"] == f"Bearer {_C}"

    def test_api_connectivity_cora_missing_key_warns_without_a_call(self, monkeypatch):
        import httpx
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        monkeypatch.delenv(ai.CORA_PAT_ENV, raising=False)
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "nonexistent-fixture.json")
        calls: list[str] = []

        def fake_get(url, **_kw):
            calls.append(url)
            r = MagicMock(status_code=200)
            r.json.return_value = {"ok": True, "user": "x", "data": []}
            return r

        monkeypatch.setattr(httpx, "get", fake_get)
        results = hc.check_api_connectivity()
        asana = [r for r in results if r.name == "Asana API"]
        assert len(asana) == 1 and asana[0].status == "warn"
        assert "ASANA_PAT_CORA" in asana[0].detail
        assert not any("asana.com" in u for u in calls)  # no call with a fallback token


# --- the <=1 adjacency: self-inventory names the active identity ---

class TestSelfInventoryAdjacency:
    def test_asana_family_line_names_the_default_identity(self):
        from cora import self_inventory as si
        assert si._with_asana_identity("Asana (live tasks)") == "Asana (live tasks); acting as: harrison"

    def test_asana_family_line_follows_the_flip(self, monkeypatch):
        from cora import self_inventory as si
        monkeypatch.setenv(ai.IDENTITY_ENV, "cora")
        assert si._with_asana_identity("Asana (live tasks)") == "Asana (live tasks); acting as: cora"

    def test_invalid_flag_renders_unknown_not_a_crash(self, monkeypatch):
        from cora import self_inventory as si
        monkeypatch.setenv(ai.IDENTITY_ENV, "bot")
        assert si._with_asana_identity("Asana (live tasks)") == "Asana (live tasks); acting as: unknown"

    def test_other_family_labels_untouched(self):
        from cora import self_inventory as si
        assert si._with_asana_identity("HubSpot CRM (live deals / contacts)") == "HubSpot CRM (live deals / contacts)"

    def test_live_tool_families_carries_the_identity_suffix(self, monkeypatch):
        from cora import self_inventory as si
        from cora.tools import tool_dispatch as td
        monkeypatch.setattr(td, "tools_for_entity", lambda _e: [{"name": "asana_get_my_tasks"}])
        fams = si.live_tool_families("F3E")
        assert any(f.startswith("Asana (live tasks); acting as: harrison") for f in fams)
        # never a token value on the inventory surface
        assert not any(_H in f or _C in f for f in fams)


# --- docs-in-code pins ---

class TestDocsInCode:
    def test_env_example_documents_both_new_keys_next_to_asana_pat(self):
        text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        assert re.search(r"^#?\s*ASANA_PAT_CORA\s*=", text, re.M)
        assert re.search(r"^#?\s*CORA_ASANA_IDENTITY\s*=", text, re.M)
        block = text.split("ASANA_PAT=")[0][-3000:]
        assert "FLIP PROCEDURE" in block and "14" in block  # 14-day PAT retention

    def test_slack_to_asana_has_a_commented_bot_row_with_no_invented_gid(self):
        import yaml
        path = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
        text = path.read_text(encoding="utf-8")
        assert "cora bot row" in text
        data = yaml.safe_load(text)
        # commented placeholder only: no parsed row names cora@
        assert not any("cora@" in str(u.get("asana_email", "")) for u in data["users"])

    def test_pm_metrics_attribution_note_is_identity_aware(self):
        import cora.pm_metrics as pm
        doc = pm.__doc__ or ""
        assert "CORA_ASANA_IDENTITY" in doc and "single-PAT" not in doc
