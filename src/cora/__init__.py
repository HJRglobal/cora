"""Cora — entity-aware Slack Q&A bot for the HJR workspace."""

# Single sanitizing egress boundary (Phase 2.1 / B1). Installing here means every
# process that imports any cora module -- the bot AND every scheduled script --
# routes its Slack sends through the formatter/redaction wrapper, with no
# per-call-site edits. Idempotent; safe at package-import time (slack_egress only
# pulls in the pure reply_formatter, and slack_sdk is imported lazily inside the
# installer).
from .slack_egress import install_egress_sanitizer as _install_egress_sanitizer

_install_egress_sanitizer()
