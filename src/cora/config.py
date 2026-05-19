"""Load and validate environment config at import time."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

_PREFIX_RULES = {
    "SLACK_BOT_TOKEN": "xoxb-",
    "SLACK_APP_TOKEN": "xapp-1-",
    # Anthropic keys always start with sk-ant-; version suffix (api03 etc.) varies
    "ANTHROPIC_API_KEY": "sk-ant-",
}


def _load() -> "Config":
    errors: list[str] = []

    def get(name: str, required: bool = True, default: str = "") -> str:
        val = os.environ.get(name, default)
        if required and not val:
            errors.append(f"  {name}: missing")
            return ""
        if "REPLACE_ME" in val:
            errors.append(f"  {name}: still contains REPLACE_ME placeholder")
            return ""
        prefix = _PREFIX_RULES.get(name)
        if prefix and not val.startswith(prefix):
            errors.append(f"  {name}: must start with '{prefix}' (got '{val[:12]}...')")
            return ""
        return val

    bot_token = get("SLACK_BOT_TOKEN")
    app_token = get("SLACK_APP_TOKEN")
    signing_secret = get("SLACK_SIGNING_SECRET")
    anthropic_key = get("ANTHROPIC_API_KEY")
    # Asana PAT is optional — bot boots without it, Asana tool-use becomes a no-op
    asana_pat = get("ASANA_PAT", required=False, default="")
    # HubSpot Private App token — optional, HubSpot tool-use disabled if missing
    hubspot_token = get("HUBSPOT_PRIVATE_APP_TOKEN", required=False, default="")
    log_level = get("LOG_LEVEL", required=False, default="INFO")

    if errors:
        raise RuntimeError("Cora config errors:\n" + "\n".join(errors))

    return Config(
        slack_bot_token=bot_token,
        slack_app_token=app_token,
        slack_signing_secret=signing_secret,
        anthropic_api_key=anthropic_key,
        asana_pat=asana_pat,
        hubspot_private_app_token=hubspot_token,
        log_level=log_level,
    )


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: str
    anthropic_api_key: str
    asana_pat: str
    hubspot_private_app_token: str
    log_level: str


config = _load()
