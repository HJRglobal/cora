"""Tests for resolve_name_to_slack_user_id (audit N4 — assignment-gate resolver).

This resolver backs the interactive asana_create_task assignee gate; a wrong
resolution = a task assigned to the wrong person (the @Tommy -> Hannah misfire).
Callers previously mocked it, so it had zero direct coverage. These pin: bare
Slack ID + <@mention> syntax, leading-@ strip, exact, alias, word-anchored
substring, ambiguity-asks, and short-needle no-match (ask, don't guess).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.tools import tool_dispatch as td  # noqa: E402

# Realistic Slack IDs (alnum, no underscore) so the bare-ID / mention paths fire.
_MAP = {
    "U0TOMMY01": {"slack_user_id": "U0TOMMY01", "display_name": "Tommy Anderson", "asana_user_gid": "1"},
    "U0HANNAH1": {"slack_user_id": "U0HANNAH1", "display_name": "Hannah Grant", "asana_user_gid": "2"},
    "U0ALEX001": {"slack_user_id": "U0ALEX001", "display_name": "Alex Cordova", "asana_user_gid": "3"},
    "U0ALEXIS1": {"slack_user_id": "U0ALEXIS1", "display_name": "Alexis Stone", "asana_user_gid": "4"},
}
_ALIASES = {"aliases": {"Tommy Anderson": ["tommy", "t-bone"]}, "disambiguation_rules": []}


def _resolve(name, entity=None):
    with patch.object(td, "_load_slack_asana_map", return_value=_MAP), \
         patch.object(td, "_load_user_aliases", return_value=_ALIASES):
        return td.resolve_name_to_slack_user_id(name, channel_entity=entity)


def test_exact_display_name():
    assert _resolve("Tommy Anderson")[0] == "U0TOMMY01"


def test_alias_match():
    assert _resolve("t-bone")[0] == "U0TOMMY01"


def test_first_name_word_anchored():
    assert _resolve("Tommy")[0] == "U0TOMMY01"


def test_leading_at_is_stripped():
    # audit N4: "@Tommy" must resolve, not spuriously miss and let Cora guess
    assert _resolve("@Tommy")[0] == "U0TOMMY01"


def test_slack_mention_syntax():
    assert _resolve("<@U0TOMMY01>")[0] == "U0TOMMY01"
    assert _resolve("<@U0TOMMY01|tommy>")[0] == "U0TOMMY01"


def test_bare_slack_id():
    assert _resolve("U0TOMMY01")[0] == "U0TOMMY01"


def test_unknown_slack_id_does_not_guess():
    sid, _info = _resolve("U0NOTREAL")
    assert sid is None


def test_short_needle_does_not_misresolve():
    # "Al" (len 2) must NOT resolve to Alex/Alexis -- ask, don't guess.
    sid, _info = _resolve("Al")
    assert sid is None


def test_ambiguous_prefix_asks():
    # "alex" word-prefix-matches both "Alex Cordova" and "Alexis Stone".
    sid, info = _resolve("alex")
    assert sid is None
    assert info and "Multiple users match" in info


def test_no_match_returns_none():
    assert _resolve("Zorp Nonexistent") == (None, None)
