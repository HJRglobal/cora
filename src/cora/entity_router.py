"""Channel name → entity code routing, driven by design/channel-routing.yaml."""

import fnmatch
import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent.parent.parent / "design" / "channel-routing.yaml"


def _load_routes() -> list[dict]:
    with open(_YAML_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["routes"]


_ROUTES: list[dict] = _load_routes()


def route(channel_name: str) -> str:
    """Return entity code for the given channel name (first fnmatch match wins)."""
    for entry in _ROUTES:
        if fnmatch.fnmatch(channel_name, entry["pattern"]):
            return entry["entity"]
    return "FNDR"


def is_silent_channel(channel_name: str) -> bool:
    """Return True if this channel is marked silent (Cora should not respond).

    Silent channels are automated feed channels where Cora is present for
    monitoring but should not reply to @mentions.
    """
    for entry in _ROUTES:
        if fnmatch.fnmatch(channel_name, entry["pattern"]):
            return bool(entry.get("silent", False))
    return False
