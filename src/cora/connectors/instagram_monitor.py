"""Instagram Graph API — brand mention monitoring for F3 Energy influencer compliance.

Polls F3 brand Instagram accounts (F3 Energy, F3 Mood, F3 Pure) for content where
sponsored athletes have tagged the brand, as required by their contracts. Detections
are matched against the influencer handle registry and posted to Slack for Alex to
confirm as deliverable completions.

Architecture:
- Polling (not webhooks) so Cora stays desktop-hosted with no HTTPS endpoint needed.
- Two detection paths per brand account:
    1. /{ig-user-id}/tags — media where the brand was directly tagged in the photo/video.
    2. Hashtag search — finds posts using campaign hashtags (#F3Energy, #F3Mood, #F3Pure)
       even when the brand isn't tagged in the media object itself.
- Detection dedup via influencer_client.detection_log (platform + post_id unique key).
- Scan watermarks stored per brand account so each run only processes new content.

Authentication — Long-Lived User Access Tokens (LLAT):
- Meta issues LLATs valid for 60 days. They MUST be refreshed before expiry.
- Token refresh is handled automatically: if a token is within 10 days of expiry,
  this module refreshes it and overwrites the .env file entry.
- Required .env keys per brand account (example for F3 Energy):
    INSTAGRAM_F3E_USER_ID=<IG Business Account numeric ID>
    INSTAGRAM_F3E_ACCESS_TOKEN=<long-lived user access token>
  See _shared/projects/cora/META_SETUP_GUIDE.md for how to generate these.

Required Instagram app permissions:
    instagram_basic
    instagram_manage_insights
    pages_show_list
    pages_read_engagement

Rate limits:
- Graph API: 200 calls / hour / user token (per brand account token).
- Hashtag API: 30 unique hashtags per App per 7 days; ~500 results per hashtag.
  The scan only queries each hashtag once per run, so a 2-hour cadence = 12 queries/day —
  well within the 30-unique-hashtag/week limit for 3 brand hashtags.

TikTok note: TikTok monitoring is scaffolded in tiktok_monitor.py but requires
TikTok Research API approval (apply at developers.tiktok.com). Until approved,
TikTok deliverables are logged manually via the influencer_log_deliverable tool.
"""

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.facebook.com/v19.0"
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Watermark file: tracks the last-scanned timestamp per (platform, brand_handle)
# so each scan only fetches new content.
_WATERMARK_PATH = _REPO_ROOT / "data" / "influencer_scan_watermarks.json"


class InstagramMonitorError(Exception):
    """Raised on Graph API errors or configuration problems."""


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _refresh_long_lived_token(token: str) -> tuple[str, int]:
    """Refresh a long-lived User Access Token. Returns (new_token, expires_in_seconds).

    Meta allows refreshing any time while the token is valid. Raises InstagramMonitorError
    on API failure.
    """
    resp = requests.get(
        f"{_GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": token,
        },
        timeout=15,
    )
    if not resp.ok:
        raise InstagramMonitorError(
            f"Token refresh failed HTTP {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    new_token = data.get("access_token")
    expires_in = int(data.get("expires_in", 5184000))  # default 60 days in seconds
    if not new_token:
        raise InstagramMonitorError(f"Token refresh response missing access_token: {data}")
    return new_token, expires_in


def check_and_refresh_token(token: str, env_key: str) -> str:
    """Check token expiry via debug endpoint; refresh if within 10 days of expiry.

    Returns the (possibly refreshed) token. If refreshed, logs a warning so Harrison
    knows to update .env with the new value.
    """
    try:
        resp = requests.get(
            f"{_GRAPH_BASE}/debug_token",
            params={
                "input_token": token,
                "access_token": token,
            },
            timeout=15,
        )
        if not resp.ok:
            log.warning("instagram_monitor: token debug check failed for %s — skipping refresh", env_key)
            return token

        debug_data = resp.json().get("data", {})
        expires_at = debug_data.get("expires_at", 0)
        if expires_at:
            expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
            days_left = (expires_dt - datetime.now(tz=timezone.utc)).days
            if days_left <= 10:
                log.warning(
                    "instagram_monitor: token for %s expires in %d days — refreshing now",
                    env_key, days_left,
                )
                new_token, _ = _refresh_long_lived_token(token)
                log.warning(
                    "instagram_monitor: REFRESHED token for %s. "
                    "UPDATE your .env: %s=<new_token> (new token logged at DEBUG level)",
                    env_key, env_key,
                )
                log.debug("instagram_monitor: new token for %s = %s", env_key, new_token)
                return new_token
    except Exception as exc:
        log.warning("instagram_monitor: token refresh check failed for %s: %s", env_key, exc)
    return token


# ---------------------------------------------------------------------------
# Watermark helpers
# ---------------------------------------------------------------------------

def _load_watermarks() -> dict[str, str]:
    """Load the scan watermarks dict {'{platform}:{brand_handle}': ISO_timestamp}."""
    if _WATERMARK_PATH.exists():
        try:
            return json.loads(_WATERMARK_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_watermarks(marks: dict[str, str]) -> None:
    _WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WATERMARK_PATH.write_text(json.dumps(marks, indent=2), encoding="utf-8")


def get_watermark(platform: str, brand_handle: str) -> str | None:
    """Return the ISO timestamp of the last successful scan for this brand account."""
    key = f"{platform}:{brand_handle}"
    return _load_watermarks().get(key)


def set_watermark(platform: str, brand_handle: str, timestamp: str) -> None:
    """Persist the scan watermark for this brand account."""
    marks = _load_watermarks()
    marks[f"{platform}:{brand_handle}"] = timestamp
    _save_watermarks(marks)


# ---------------------------------------------------------------------------
# Graph API helpers
# ---------------------------------------------------------------------------

def _graph_get(path: str, params: dict[str, Any], token: str) -> dict[str, Any]:
    """GET from Graph API. Raises InstagramMonitorError on HTTP or API error."""
    params = {**params, "access_token": token}
    resp = requests.get(f"{_GRAPH_BASE}/{path.lstrip('/')}", params=params, timeout=20)
    if not resp.ok:
        raise InstagramMonitorError(
            f"Graph API {path} returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise InstagramMonitorError(
            f"Graph API error {err.get('code')} ({err.get('type')}): {err.get('message')}"
        )
    return data


# ---------------------------------------------------------------------------
# Core scan functions
# ---------------------------------------------------------------------------

def scan_tagged_media(
    ig_user_id: str,
    access_token: str,
    brand_handle: str,
    since_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch media objects where the brand account was tagged (in the photo/video).

    Returns a list of normalised media dicts:
        {post_id, media_type, timestamp, permalink, caption, username, tagged_username}

    since_timestamp: ISO 8601 string. If provided, only returns posts after this time.
    Handles one page of pagination (up to 50 results) — sufficient for 2-hour cadence.
    """
    fields = "id,media_type,timestamp,permalink,caption,username"
    try:
        data = _graph_get(
            f"{ig_user_id}/tags",
            {"fields": fields, "limit": 50},
            access_token,
        )
    except InstagramMonitorError as exc:
        log.warning("scan_tagged_media brand=%s: %s", brand_handle, exc)
        return []

    items = data.get("data") or []
    results = []
    for item in items:
        ts = item.get("timestamp", "")
        if since_timestamp and ts and ts <= since_timestamp:
            continue  # already processed
        results.append({
            "post_id": item.get("id", ""),
            "media_type": (item.get("media_type") or "").lower(),
            "timestamp": ts,
            "permalink": item.get("permalink", ""),
            "caption": (item.get("caption") or "")[:500],
            "username": item.get("username", ""),
            "detection_method": "tagged_media",
            "brand_handle": brand_handle,
        })

    log.info(
        "scan_tagged_media brand=%s since=%s found=%d new",
        brand_handle, since_timestamp or "none", len(results),
    )
    return results


def scan_hashtag(
    ig_user_id: str,
    access_token: str,
    hashtag: str,
    brand_handle: str,
    since_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Search for recent media using a campaign hashtag.

    Returns normalised media dicts (same shape as scan_tagged_media).
    Note: Graph API hashtag search does NOT return the username who posted — this is a
    Meta privacy restriction. We include the post_id and permalink so Alex can verify
    manually, or Alex registers the athlete's handle so we can cross-reference captions.

    Meta limit: 30 unique hashtags per App per 7 days.
    """
    clean_hashtag = hashtag.lstrip("#")
    # Step 1: get hashtag ID (cached by Meta per IG user)
    try:
        ht_data = _graph_get(
            "ig_hashtag_search",
            {"user_id": ig_user_id, "q": clean_hashtag},
            access_token,
        )
    except InstagramMonitorError as exc:
        log.warning("scan_hashtag hashtag=%s brand=%s: hashtag search failed: %s", hashtag, brand_handle, exc)
        return []

    hashtag_id = (ht_data.get("data") or [{}])[0].get("id")
    if not hashtag_id:
        log.warning("scan_hashtag: no hashtag_id returned for #%s", clean_hashtag)
        return []

    # Step 2: get recent media for the hashtag
    fields = "id,media_type,timestamp,permalink,caption"
    try:
        media_data = _graph_get(
            f"{hashtag_id}/recent_media",
            {"user_id": ig_user_id, "fields": fields, "limit": 50},
            access_token,
        )
    except InstagramMonitorError as exc:
        log.warning("scan_hashtag hashtag=%s brand=%s: recent_media failed: %s", hashtag, brand_handle, exc)
        return []

    items = media_data.get("data") or []
    results = []
    for item in items:
        ts = item.get("timestamp", "")
        if since_timestamp and ts and ts <= since_timestamp:
            continue
        results.append({
            "post_id": item.get("id", ""),
            "media_type": (item.get("media_type") or "").lower(),
            "timestamp": ts,
            "permalink": item.get("permalink", ""),
            "caption": (item.get("caption") or "")[:500],
            "username": "",  # not available via hashtag search (Meta privacy restriction)
            "detection_method": f"hashtag:#{clean_hashtag}",
            "brand_handle": brand_handle,
        })

    log.info(
        "scan_hashtag hashtag=#%s brand=%s since=%s found=%d new",
        clean_hashtag, brand_handle, since_timestamp or "none", len(results),
    )
    return results


# ---------------------------------------------------------------------------
# Slack notification formatter
# ---------------------------------------------------------------------------

_MEDIA_TYPE_LABELS = {
    "image": "Photo",
    "video": "Video",
    "reel": "Reel",
    "carousel_album": "Carousel",
    "story": "Story",
}


def format_detection_for_slack(
    detection: dict[str, Any],
    *,
    athlete_name: str | None,
    brand_display_name: str,
) -> str:
    """Format a detection event as a Slack mrkdwn notification block.

    Designed to be posted to the influencer ops channel so Alex can confirm
    or dismiss with a quick reply.
    """
    media_label = _MEDIA_TYPE_LABELS.get(detection.get("media_type", ""), "Post")
    ts_raw = detection.get("timestamp", "")
    try:
        ts_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        ts_str = ts_dt.strftime("%b %-d, %Y %-I:%M %p UTC")
    except Exception:
        ts_str = ts_raw or "unknown time"

    permalink = detection.get("permalink", "")
    link_text = f"<{permalink}|View on Instagram>" if permalink else "(no link)"

    caption = detection.get("caption", "")
    caption_snippet = f'"{caption[:120]}…"' if len(caption) > 120 else (f'"{caption}"' if caption else "(no caption)")

    if athlete_name:
        who = f"*{athlete_name}*"
        handle_hint = f" (@{detection.get('username', '?')})" if detection.get("username") else ""
        athlete_line = f"*Athlete:* {who}{handle_hint} ✅ _registered_"
    elif detection.get("username"):
        athlete_line = f"*Handle:* @{detection['username']} ⚠️ _not in handle registry — add with `@Cora add handle`_"
    else:
        athlete_line = f"*Detection:* via {detection.get('detection_method', 'unknown')} ⚠️ _username unavailable (hashtag scan)_"

    return (
        f"🎯 *New Deliverable Detected* — {brand_display_name} tagged on Instagram\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{athlete_line}\n"
        f"*Type:* {media_label}  |  *Posted:* {ts_str}\n"
        f"*Caption:* {caption_snippet}\n"
        f"*Link:* {link_text}\n"
        f"\n"
        f"Reply `@Cora mark deliverable #<ID> complete link <url>` to log this, "
        f"or `@Cora show influencer status` to find the right ID."
    )
