"""Mode, demotion and acting tier for the dead-channel archive lane (Code #16 C1).

STDLIB-ONLY AT IMPORT. ``ladder_registry``'s ``channel_archive_mode`` probe imports
this module from the nightly health check and the Monday digest (script
processes), so nothing here may pull a bot-only module.

THE LADDER (ruled 2026-09-21, kickoff section 9.1):
  * T0 = a proposal card in Harrison's DM. A tap RECORDS his agreement (the T1
    promotion evidence); nothing is archived.
  * T1 = approve-then-act: a tap on a card staged at T1 posts a notice into the
    channel, then archives it, and ledgers the archive.

The bot never writes the registry. Promotion is a registry ``promoted`` event (a
Code/Cowork commit) plus ``CORA_CHANNEL_ARCHIVE=act`` plus one restart; acting at
T1 while the registry says T0 is a nightly acting-drift WARN (D-326 in code).

DEMOTION IS AUTOMATIC and one-way: the monitor writes the demotion file when it
finds an archive by the bot that the ledger cannot attribute to a tap. While the
file exists the lane acts at T0 whatever the flag says. Only Harrison clears it.
An unreadable or corrupt demotion file reads as DEMOTED (fail closed).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]

MODE_ENV = "CORA_CHANNEL_ARCHIVE"
DEMOTION_ENV = "CORA_CHANNEL_ARCHIVE_DEMOTION_PATH"

MODES: tuple[str, ...] = ("off", "propose", "act")
DEFAULT_MODE = "propose"


def mode() -> str:
    """``off`` | ``propose`` | ``act``, read per call.

    Unset or any unrecognised value reads as ``propose`` (T0). Only the exact word
    ``act`` arms the T1 executor, and only ``off`` disables the lane: an authority
    boundary never widens on a typo.
    """
    raw = str(os.environ.get(MODE_ENV, "") or "").strip().lower()
    return raw if raw in MODES else DEFAULT_MODE


def demotion_path() -> Path:
    """Per call, so tests redirect it (tests/conftest.py)."""
    raw = str(os.environ.get(DEMOTION_ENV, "") or "").strip()
    return Path(raw) if raw else _REPO_ROOT / "data" / "state" / "channel-archive-demotion.json"


def demotion_state(path: Path | None = None) -> dict[str, Any] | None:
    """The demotion record, or None when the lane is not demoted.

    A file that exists but cannot be read or parsed is returned as a synthetic
    record: a demotion we cannot read is still a demotion.
    """
    p = Path(path) if path is not None else demotion_path()
    try:
        if not p.exists():
            return None
    except OSError as exc:
        return {"demoted": True, "reason": f"demotion file unreadable ({type(exc).__name__})",
                "unreadable": True}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- corrupt = demoted, said out loud
        return {"demoted": True, "reason": f"demotion file unreadable ({type(exc).__name__})",
                "unreadable": True}
    if not isinstance(data, dict):
        return {"demoted": True, "reason": "demotion file is not a mapping", "unreadable": True}
    data.setdefault("demoted", True)
    return data


def is_demoted(path: Path | None = None) -> bool:
    return demotion_state(path) is not None


def acting_tier() -> str:
    """The tier the lane acts at RIGHT NOW: ``T1`` only when the flag says ``act``
    and the lane is not demoted; otherwise ``T0``."""
    if mode() != "act":
        return "T0"
    return "T0" if is_demoted() else "T1"
