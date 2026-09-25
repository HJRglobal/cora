"""Slack inactive-channel archive lane (Code #16 C1, cq-be90cea867c3).

Born T0 (ladder row ``slack-channel-archive``): a metadata-only scan proposes dead
channels on a card in Harrison's DM; nothing is archived at T0. The T1 executor
(tap -> in-channel notice -> conversations.archive -> ledger) is built but DARK
until Harrison promotes the lane (a registry ``promoted`` event + the
``CORA_CHANNEL_ARCHIVE=act`` flag). ``policy`` is the stdlib-only core the ladder
registry's acting probe imports from scripts.
"""
