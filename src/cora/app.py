"""Bolt app and event handlers."""

import logging

from slack_bolt import App

from .config import config
from .entity_router import route

log = logging.getLogger(__name__)

app = App(token=config.slack_bot_token, signing_secret=config.slack_signing_secret)


def _resolve_channel_name(client, channel_id: str) -> str:
    try:
        info = client.conversations_info(channel=channel_id)
        return info["channel"]["name"]
    except Exception as exc:
        log.warning("Could not resolve channel name for %s: %s", channel_id, exc)
        return channel_id


@app.event("app_mention")
def handle_mention(event: dict, say: callable, client) -> None:
    channel_id = event.get("channel", "")
    user_id = event.get("user")
    thread_ts = event.get("ts")

    channel_name = _resolve_channel_name(client, channel_id)
    entity = route(channel_name)
    log.info("app_mention routed channel=#%s user=%s → entity=%s", channel_name, user_id, entity)

    say(
        text="👋 Cora here — Socket Mode online. Day 1 midday smoke test.",
        thread_ts=thread_ts,
    )
