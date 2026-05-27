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
    # Asana PAT is optional -- bot boots without it, Asana tool-use becomes a no-op
    asana_pat = get("ASANA_PAT", required=False, default="")
    # HubSpot Private App token -- optional, HubSpot tool-use disabled if missing
    hubspot_token = get("HUBSPOT_PRIVATE_APP_TOKEN", required=False, default="")
    # Google Service Account JSON path -- optional, Calendar tool-use disabled if missing
    google_sa_json = get("GOOGLE_SERVICE_ACCOUNT_JSON", required=False, default="")
    # OpenAI API key -- for embeddings (Phase 3 KB). Optional; KB ingest/retrieval no-ops if missing.
    openai_api_key = get("OPENAI_API_KEY", required=False, default="")
    # QuickBooks Online OAuth (Phase 2 #10). All four optional; QBO tool-use disabled if any missing.
    # Single Intuit Developer app -- per-entity tokens stored in .credentials/qbo-tokens.json.
    qbo_client_id = get("QBO_CLIENT_ID", required=False, default="")
    qbo_client_secret = get("QBO_CLIENT_SECRET", required=False, default="")
    qbo_redirect_uri = get(
        "QBO_REDIRECT_URI", required=False, default="http://localhost:8765/qbo-oauth-callback"
    )
    qbo_environment = get("QBO_ENVIRONMENT", required=False, default="production")
    log_level = get("LOG_LEVEL", required=False, default="INFO")
    # PhotoRoom API (image generation orchestrator). All optional; PhotoRoom tool-use
    # disabled if PHOTOROOM_API_KEY is missing.
    photoroom_api_key = get("PHOTOROOM_API_KEY", required=False, default="")
    photoroom_base_url = get(
        "PHOTOROOM_BASE_URL", required=False, default="https://image-api.photoroom.com/v2"
    )
    photoroom_rate_limit_per_min = get("PHOTOROOM_RATE_LIMIT_PER_MIN", required=False, default="60")
    photoroom_use_sandbox = get("PHOTOROOM_USE_SANDBOX", required=False, default="false")
    photoroom_weekly_budget_usd = get("PHOTOROOM_WEEKLY_BUDGET_USD", required=False, default="50")
    photoroom_outputs_drive_folder_id = get("PHOTOROOM_OUTPUTS_DRIVE_FOLDER_ID", required=False, default="")
    # Make webhook URL for sales deck generation (Canva → Drive → Slack DM pipeline).
    # Optional; sales deck tool responds with a config error if missing.
    make_sales_deck_webhook_url = get("MAKE_SALES_DECK_WEBHOOK_URL", required=False, default="")

    if errors:
        raise RuntimeError("Cora config errors:\n" + "\n".join(errors))

    return Config(
        slack_bot_token=bot_token,
        slack_app_token=app_token,
        slack_signing_secret=signing_secret,
        anthropic_api_key=anthropic_key,
        asana_pat=asana_pat,
        hubspot_private_app_token=hubspot_token,
        google_service_account_json=google_sa_json,
        openai_api_key=openai_api_key,
        qbo_client_id=qbo_client_id,
        qbo_client_secret=qbo_client_secret,
        qbo_redirect_uri=qbo_redirect_uri,
        qbo_environment=qbo_environment,
        log_level=log_level,
        photoroom_api_key=photoroom_api_key,
        photoroom_base_url=photoroom_base_url,
        photoroom_rate_limit_per_min=int(photoroom_rate_limit_per_min or "60"),
        photoroom_use_sandbox=photoroom_use_sandbox.lower() in ("true", "1", "yes"),
        photoroom_weekly_budget_usd=float(photoroom_weekly_budget_usd or "50"),
        photoroom_outputs_drive_folder_id=photoroom_outputs_drive_folder_id,
        make_sales_deck_webhook_url=make_sales_deck_webhook_url,
    )


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: str
    anthropic_api_key: str
    asana_pat: str
    hubspot_private_app_token: str
    google_service_account_json: str
    openai_api_key: str
    qbo_client_id: str
    qbo_client_secret: str
    qbo_redirect_uri: str
    qbo_environment: str
    log_level: str
    photoroom_api_key: str
    photoroom_base_url: str
    photoroom_rate_limit_per_min: int
    photoroom_use_sandbox: bool
    photoroom_weekly_budget_usd: float
    photoroom_outputs_drive_folder_id: str  # Drive folder ID for review uploads
    make_sales_deck_webhook_url: str       # Make webhook — sales deck Canva pipeline


config = _load()
