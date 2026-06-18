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

# The trailing wildcard in channel-routing.yaml: any channel matching ONLY this
# pattern has no dedicated route (it falls through to founder-level FNDR scope).
_CATCHALL_PATTERN = "*"


def route(channel_name: str) -> str:
    """Return entity code for the given channel name (first fnmatch match wins)."""
    for entry in _ROUTES:
        if fnmatch.fnmatch(channel_name, entry["pattern"]):
            return entry["entity"]
    return "FNDR"


def matched_pattern(channel_name: str) -> str | None:
    """Return the route PATTERN that claims this channel (first fnmatch match wins,
    same order as route()), or None if nothing matches.

    Useful when a caller needs to know *which* rule fired -- e.g. whether a channel
    was claimed by a real entity route or only by the trailing "*" catch-all -- not
    just the resolved entity. Reuses the same _ROUTES + first-match semantics as
    route(); it does not reimplement routing.
    """
    for entry in _ROUTES:
        if fnmatch.fnmatch(channel_name, entry["pattern"]):
            return entry["pattern"]
    return None


def is_mapped(channel_name: str) -> bool:
    """Return True iff an explicit (non-catch-all) route claims this channel.

    route() returns "FNDR" both for channels explicitly routed to FNDR/HJRG
    (e.g. #hjrg-leadership, #fndr, the silent feed channels) AND for any channel
    that matches only the trailing "*" catch-all. Callers that must tell those
    apart -- e.g. the channel-health monitor flagging channels with no dedicated
    route -- use this. Because it shares route()'s first-match loop, is_mapped()
    can never diverge from route(): it is True exactly when route()'s winning
    pattern is something other than the catch-all.
    """
    pattern = matched_pattern(channel_name)
    return pattern is not None and pattern != _CATCHALL_PATTERN


def is_silent_channel(channel_name: str) -> bool:
    """Return True if this channel is marked silent (Cora should not respond).

    Silent channels are automated feed channels where Cora is present for
    monitoring but should not reply to @mentions.
    """
    for entry in _ROUTES:
        if fnmatch.fnmatch(channel_name, entry["pattern"]):
            return bool(entry.get("silent", False))
    return False
