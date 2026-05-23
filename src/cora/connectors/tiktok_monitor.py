"""TikTok monitoring — scaffold for when TikTok Research API is approved.

STATUS: NOT FUNCTIONAL — requires TikTok Research API access.

To apply: https://developers.tiktok.com/products/research-api/
Application takes 2-4 weeks for business accounts. Harrison or Alex should apply
using the F3 Energy TikTok Business account.

Once approved, this module will:
1. Poll hashtag search (#F3Energy, #F3Mood, #F3Pure) for recent videos
2. Match posting accounts against the influencer handle registry
3. Post Slack notifications to the influencer ops channel (same as instagram_monitor.py)

Until then: TikTok deliverables are logged manually via the influencer_log_deliverable
Cora tool ('mark deliverable #N complete link <url>').

--- What you get with TikTok Research API ---
Endpoint: POST /v2/research/video/query/
- Filter by hashtag, keyword, region, date range
- Returns: video_id, create_time, share_url, like_count, caption, author username
- Rate limit: 1000 requests/day

Authentication:
- Client credentials flow (no per-user OAuth needed for research queries)
- Required .env keys (once approved):
    TIKTOK_CLIENT_KEY=...
    TIKTOK_CLIENT_SECRET=...

--- Alternate approach (available now without Research API) ---
TikTok's public hashtag page is accessible without API:
  https://www.tiktok.com/tag/F3Energy
We intentionally do NOT scrape it here — TikTok's ToS prohibits automated scraping
and using the official Research API is the right long-term path.
"""

import logging

log = logging.getLogger(__name__)


class TikTokMonitorError(Exception):
    """Raised when TikTok monitoring is attempted before Research API is configured."""


def scan_hashtag(hashtag: str, brand_handle: str, since_timestamp: str | None = None) -> list:
    """Stub — requires TikTok Research API approval before implementation.

    Raises TikTokMonitorError with instructions.
    """
    raise TikTokMonitorError(
        "TikTok Research API not yet configured. "
        "Apply at https://developers.tiktok.com/products/research-api/ using the "
        "F3 Energy TikTok Business account. Once approved (2-4 weeks), set "
        "TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env and implement this function. "
        "Until then, TikTok deliverables are logged manually via @Cora in Slack."
    )
