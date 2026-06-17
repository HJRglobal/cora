"""Slack ingestion deny-list (Phase 1.4, audit F-1/F-2 + gate G-A).

Both Slack sweeps -- the KB-ingest sweep (scripts/incremental_sync_slack.py) and
the per-user synthesis sweep (connectors/channel_sweep.py) -- call should_ingest()
before reading a channel. A channel matching any deny rule is skipped entirely.

DENY-LIST model (NOT an allow-list): every channel not denied is ingested, public
or private. This deliberately does NOT block all private channels -- that would
drop the legit private leadership/finance channels. Only the sensitive set is
denied: personal/family, the NDA channel, all LBHS/LTS channels (removed from the
KB per G-A.2), and the workspace general-do-not-use.

Config: data/maps/slack-sweep-policy.yaml. Loaded once per process and cached;
the sweeps are fresh processes so an edit takes effect at the next run. Call
reset_cache() after editing in-process (tests).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml

_POLICY_PATH = Path(__file__).resolve().parents[2] / "data" / "maps" / "slack-sweep-policy.yaml"

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            _cache = {}
        except Exception:
            # Fail OPEN to {} (ingest normally) rather than crash the sweep -- a
            # broken policy file must not take the whole nightly sync down. The
            # deny-list is defense-in-depth; the entity firewall + PHI guards are
            # the load-bearing controls.
            _cache = {}
    return _cache


def reset_cache() -> None:
    """Force reload on next call (tests / after editing the YAML in-process)."""
    global _cache
    _cache = None


def is_denied(channel_name: str, channel_id: str | None = None) -> bool:
    """True if the channel matches any deny rule (id / exact name / glob)."""
    policy = _load()
    name = (channel_name or "").strip().lower()
    cid = (channel_id or "").strip()

    if cid and cid in {str(x).strip() for x in (policy.get("deny_by_id") or [])}:
        return True
    if name and name in {str(x).strip().lower() for x in (policy.get("deny_by_name") or [])}:
        return True
    if name:
        for glob in (policy.get("deny_by_glob") or []):
            if fnmatch.fnmatch(name, str(glob).strip().lower()):
                return True
    return False


def should_ingest(channel_name: str, channel_id: str | None = None, is_private: bool = False) -> bool:
    """Return True if this Slack channel should be ingested / swept.

    Deny-list first. Then the optional private allow-list: if it is present and
    NON-empty, a private channel must be on it to ingest; an empty/absent list
    means no private restriction (private channels ingest unless denied).
    """
    if is_denied(channel_name, channel_id):
        return False
    policy = _load()
    allowlist = policy.get("private_allowlist") or []
    if is_private and allowlist:
        name = (channel_name or "").strip().lower()
        cid = (channel_id or "").strip()
        allow_names = {str(x).strip().lower() for x in allowlist}
        allow_ids = {str(x).strip() for x in allowlist}
        if name not in allow_names and cid not in allow_ids:
            return False
    return True
