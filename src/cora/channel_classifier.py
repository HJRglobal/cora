"""Channel function classification and financial-access tier assignment."""

KNOWN_FUNCTIONS = {"leadership", "finance", "hr", "sales", "ops", "clients", "founder", "build"}

_SPECIAL_CHANNELS = {
    "cora-build": "build",
    "fndr": "founder",
    "fndr-general": "founder",
}

TIER_1_FUNCTIONS = {"leadership", "finance", "founder", "build"}


def classify_function(channel_name: str) -> str:
    """Return the channel's function segment, or 'unknown'."""
    name = channel_name.lower()
    if name in _SPECIAL_CHANNELS:
        return _SPECIAL_CHANNELS[name]
    if "-" not in name:
        return "unknown"
    _prefix, _, rest = name.partition("-")
    if rest in KNOWN_FUNCTIONS:
        return rest
    first_segment = rest.partition("-")[0]
    if first_segment in KNOWN_FUNCTIONS:
        return first_segment
    return "unknown"


def is_tier_1(entity: str, function: str) -> bool:
    if entity == "HJRG":
        return True
    return function in TIER_1_FUNCTIONS


def tier_label(entity: str, function: str) -> str:
    return "TIER_1" if is_tier_1(entity, function) else "TIER_3"
