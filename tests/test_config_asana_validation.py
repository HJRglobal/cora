"""Code #15 rider (R1-02): boot-time validation covers BOTH Asana keys.

Before this, config._load() ran get() -- and so the REPLACE_ME placeholder
check -- only on ASANA_PAT, which is the INACTIVE key once CORA_ASANA_IDENTITY
flips to ``cora``. A placeholder in ASANA_PAT_CORA (the key the ``cora``
identity actually uses) was discovered only as runtime 401s. The comment also
claimed a "REPLACE_ME/prefix validation" although _PREFIX_RULES has no Asana
entry; the comment now says what is true.

Every test here calls config._load() directly on a monkeypatched environment;
no real key value is read, compared or printed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cora import config as cfg

_SRC = Path(cfg.__file__).read_text(encoding="utf-8")
_FAKE_SUFFIX = "zz9-not-a-real-token-tail"


def _raises_for(monkeypatch, key: str, value: str) -> str:
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError) as exc:
        cfg._load()
    return str(exc.value)


def test_a_placeholder_in_asana_pat_cora_fails_boot(monkeypatch):
    msg = _raises_for(monkeypatch, "ASANA_PAT_CORA", "2/REPLACE_ME" + _FAKE_SUFFIX)
    assert "ASANA_PAT_CORA: still contains REPLACE_ME placeholder" in msg
    # key NAME only -- never the value, nor any piece of it
    assert _FAKE_SUFFIX not in msg


def test_a_placeholder_in_asana_pat_still_fails_boot(monkeypatch):
    msg = _raises_for(monkeypatch, "ASANA_PAT", "REPLACE_ME" + _FAKE_SUFFIX)
    assert "ASANA_PAT: still contains REPLACE_ME placeholder" in msg
    assert _FAKE_SUFFIX not in msg


def test_asana_pat_cora_stays_optional(monkeypatch):
    # absent / empty boots (identity harrison needs no Cora key); the `cora`
    # identity's hard raise on an empty key is asana_identity's, not config's
    monkeypatch.delenv("ASANA_PAT_CORA", raising=False)
    assert isinstance(cfg._load(), cfg.Config)
    monkeypatch.setenv("ASANA_PAT_CORA", "")
    assert isinstance(cfg._load(), cfg.Config)


def test_a_real_looking_asana_pat_cora_boots(monkeypatch):
    # no prefix rule: any non-placeholder value is accepted
    # (a dummy shape the repo secrets scan never matches -- conftest's own form)
    monkeypatch.setenv("ASANA_PAT_CORA", "0/dummy-asana-pat-cora-" + _FAKE_SUFFIX)
    assert isinstance(cfg._load(), cfg.Config)


def test_no_asana_prefix_rule_and_the_comment_does_not_claim_one():
    assert not any(k.startswith("ASANA") for k in cfg._PREFIX_RULES)
    assert "REPLACE_ME/prefix validation" not in _SRC
    assert "There is NO prefix check" in _SRC


def test_both_keys_are_loaded_through_get():
    assert 'get("ASANA_PAT", required=False, default="")' in _SRC
    assert 'get("ASANA_PAT_CORA", required=False, default="")' in _SRC
