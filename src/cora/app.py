"""Bolt app and event handlers."""

import logging

from slack_bolt import App

from .config import config

log = logging.getLogger(__name__)

app = App(token=config.slack_bot_token, signing_secret=config.slack_signing_secret)


@app.event("app_mention")
def handle_mention(event: dict, say: callable) -> None:
    channel_id = event.get("channel")
    user_id = event.get("user")
    thread_ts = event.get("ts")

    log.info("app_mention received channel=%s user=%s", channel_id, user_id)

    say(
        text="👋 Cora here — Socket Mode online. Day 1 midday smoke test.",
        thread_ts=thread_ts,
    )
