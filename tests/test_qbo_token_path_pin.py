"""Code #14 R14-7b: the QBO token file's path, pinned in the three places it is named.

The LIVE token file is ``.credentials/qbo-tokens.json`` (``qbo_oauth._TOKEN_FILE``),
already gitignored by the ``.credentials/`` rule. The runbook's identity inventory
named ``data/qbo-tokens.json`` -- a path that has never existed or been tracked --
which is what made the ruled ``.gitignore`` belt (4b.5) look like a leak fix. It is
not one: the belt only keeps a future mis-pathed write uncommittable.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _gitignore_lines() -> list[str]:
    return [ln.strip() for ln in (_REPO / ".gitignore").read_text(encoding="utf-8").splitlines()]


def test_live_token_file_is_under_credentials():
    from cora.connectors import qbo_oauth
    assert qbo_oauth._TOKEN_FILE.parent.name == ".credentials"
    assert qbo_oauth._TOKEN_FILE.name == "qbo-tokens.json"


def test_gitignore_covers_the_live_path_and_carries_the_belt():
    lines = _gitignore_lines()
    assert ".credentials/" in lines, "the rule that actually protects the live token file"
    assert "data/qbo-tokens.json" in lines, "the 4b.5 belt"


def test_runbook_names_the_live_token_path_not_the_phantom_one():
    text = (_REPO / "deployment" / "runbook.md").read_text(encoding="utf-8")
    assert "data/qbo-tokens.json" not in text
    assert "`.credentials/qbo-tokens.json`" in text
