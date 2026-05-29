"""F3 LinkedIn Spy — weekly retail buyer & executive prospect scanner.

Searches Apollo.io for retail buyers and head executives, deduplicates against
previously seen prospects, uses Claude to assign F3 brand fit and generate a
personalized LinkedIn connection note, then posts a ranked weekly report to
the #f3-sales Slack channel for Tommy Anderson's outreach queue.

Usage (called by Windows Task Scheduler weekly on Mondays):
    uv run python scripts/run_linkedin_spy.py

Environment variables required (add to .env):
    APOLLO_API_KEY          Apollo.io master API key (from Settings → API Keys)
    ANTHROPIC_API_KEY       Already set (powers Cora)
    SLACK_BOT_TOKEN         Already set (powers Cora)
    LINKEDIN_SPY_CHANNEL    Slack channel without # (default: f3-sales)

Search config: data/maps/linkedin-spy-search-config.yaml
Prospect DB:   data/linkedin_spy.db

Credit note: Apollo Search API calls are FREE — no credit deduction. Credits
are only spent when revealing email/phone, which this scanner never does.
The weekly report shows title + company + LinkedIn URL only; Tommy can decide
which prospects to reveal/reach out to manually.

Apollo endpoint: POST https://api.apollo.io/v1/mixed_people/api_search
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

from cora.connectors.apollo_client import ApolloClientError, iter_people_pages
from cora.tools.linkedin_spy_client import (
    get_pending_report_prospects,
    get_total_seen,
    is_already_seen,
    log_prospect,
    mark_slack_notified,
)

log = logging.getLogger(__name__)

_CONFIG_PATH = _REPO_ROOT / "data" / "maps" / "linkedin-spy-search-config.yaml"
_NOTIFY_CHANNEL = os.environ.get("LINKEDIN_SPY_CHANNEL", "f3e-sales")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        log.error("linkedin_spy: config not found at %s", _CONFIG_PATH)
        return {}
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Brand fit — match company name against channel_fit config
# ---------------------------------------------------------------------------

def _detect_brand_fit(company: str, channel_fit: list[dict]) -> dict:
    """Return the first channel_fit entry whose keywords match the company name."""
    company_lower = company.lower()
    for entry in channel_fit:
        for kw in entry.get("keywords") or []:
            if kw and kw.lower() in company_lower:
                return entry
    # Return the last entry (default/fallback) if nothing matched
    return channel_fit[-1] if channel_fit else {
        "brand": "F3 Energy",
        "tagline": "Fuel. Focus. Finish.",
        "angle": "general retail",
    }


# ---------------------------------------------------------------------------
# Claude — batch scoring + message generation
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a B2B outreach strategist for F3 Energy, a premium functional energy drink brand with three sub-brands:

• F3 Pure — clean-label natural channel. Avatar: Lauren (25-35, Pilates-mom / Sprouts-regular). Tagline: "Real energy for real life."
• F3 Energy — performance energy for MMA-adjacent athletes. Avatar: Alex (22-42, trains regularly, knows his nootropics). Tagline: "Fuel. Focus. Finish."
• F3 Mood — anti-anxiety + focus for high-cognitive-load professionals. Avatar: Marcus (35-50, ER doctor / trial attorney). Tagline: "Calm the Noise."

You will receive a JSON array of retail buyer prospects. For each prospect, output a JSON array with one object per prospect in the same order. Each object must have:
  "apollo_id": (string — copy from input, unchanged)
  "brand_fit": (string — which F3 sub-brand is the best fit and why, ≤ 15 words)
  "message_draft": (string — a LinkedIn connection request note, ≤ 280 characters, first-person from Tommy Anderson at F3, warm and specific, never generic. If the contact name is known, use it. Otherwise address by title.)

Rules for message_draft:
- Must be ≤ 280 characters (LinkedIn connection note limit)
- Reference their specific role or company to show it's not mass-sent
- Lead with F3's fit for their channel (natural, convenience, sports, mass grocery)
- No emojis. Professional but warm. No "I came across your profile."
- Sign off implied — do NOT add "Best, Tommy" or any sign-off
- If company name is blank or title is vague, write a general but credible note

Return ONLY the JSON array — no markdown, no explanation, no wrapper text.
"""


def _generate_outreach(prospects: list[dict]) -> list[dict]:
    """Call Claude to score brand fit and draft LinkedIn messages for a batch of prospects.

    Returns a list of dicts with apollo_id, brand_fit, message_draft.
    Falls back to empty strings per prospect on any failure.
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("linkedin_spy: ANTHROPIC_API_KEY not set — skipping message generation")
        return [{"apollo_id": p["apollo_id"], "brand_fit": "", "message_draft": ""} for p in prospects]

    payload = [
        {
            "apollo_id": p["apollo_id"],
            "title": p["title"],
            "company": p["company"],
            "name": p.get("name"),
            "location": f"{p.get('city', '')}, {p.get('state', '')}".strip(", "),
            "suggested_brand": p.get("_brand_hint", {}).get("brand", ""),
            "channel_angle": p.get("_brand_hint", {}).get("angle", ""),
        }
        for p in prospects
    ]

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Generate brand fit labels and LinkedIn connection drafts for these {len(payload)} prospects:\n\n{json.dumps(payload, indent=2)}",
                }
            ],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        if isinstance(result, list):
            return result
        log.error("linkedin_spy: Claude returned non-list JSON — %r", raw[:200])
    except Exception as exc:
        log.error("linkedin_spy: Claude generation failed — %s", exc)

    return [{"apollo_id": p["apollo_id"], "brand_fit": "", "message_draft": ""} for p in prospects]


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

def _post_to_slack(text: str) -> bool:
    import requests

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("linkedin_spy: SLACK_BOT_TOKEN not set — cannot post Slack notification")
        return False

    channel = _NOTIFY_CHANNEL.lstrip("#")
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"channel": channel, "text": text, "mrkdwn": True},
        timeout=15,
    )
    data = resp.json() if resp.ok else {}
    if not data.get("ok"):
        log.warning(
            "linkedin_spy: Slack post failed channel=%s error=%s",
            channel, data.get("error", resp.status_code),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _format_report(prospects: list[dict], new_found: int, total_seen: int) -> str:
    week_str = datetime.now(tz=timezone.utc).strftime("%#d %b %Y")
    lines = [
        f"📋 *F3 LinkedIn Prospect Report — {week_str}*",
        f"Found *{new_found} new* retail buyers & executives this week. "
        f"Showing top {len(prospects)}. Total scanned to date: {total_seen}.",
        "",
    ]

    for i, p in enumerate(prospects, 1):
        title = p.get("title") or "Unknown Title"
        company = p.get("company") or "Unknown Company"
        location_parts = [x for x in (p.get("city"), p.get("state")) if x]
        location = ", ".join(location_parts) or "US"
        brand_fit = p.get("brand_fit") or "F3 Energy fit"
        message = p.get("message_draft") or "(draft unavailable)"
        li_url = p.get("linkedin_url") or ""
        name_display = p.get("name") or f"{title}"

        profile_link = f"<{li_url}|View Profile>" if li_url else "(no LinkedIn URL)"

        lines += [
            f"*{i}. {name_display}*",
            f"   🏢 {title} @ {company} · 📍 {location}",
            f"   🎯 {brand_fit}",
            f'   💬 _{message}_',
            f"   🔗 {profile_link}",
            "",
        ]

    lines += [
        "---",
        "_Apollo Search API — no credits used. React ✅ when outreach is sent. "
        "DM @Cora for a custom message on any prospect._",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def run_scan() -> None:
    config = _load_config()
    if not config:
        log.error("linkedin_spy: no config loaded — aborting")
        return

    target_titles = config.get("target_titles") or []
    keywords = config.get("search_keywords", "")
    person_locations = config.get("person_locations") or ["United States"]
    per_page = int(config.get("per_page", 100))
    max_pages = int(config.get("max_pages", 3))
    max_report = int(config.get("max_report_items", 10))
    channel_fit = config.get("channel_fit") or []

    if not target_titles:
        log.error("linkedin_spy: no target_titles in config — aborting")
        return

    log.info(
        "linkedin_spy: starting scan — %d titles, %d pages × %d per page, max_report=%d",
        len(target_titles), max_pages, per_page, max_report,
    )

    # Step 1: pull candidates from Apollo and filter to new ones
    new_prospects: list[dict] = []

    try:
        for page in iter_people_pages(
            person_titles=target_titles,
            keywords=keywords,
            person_locations=person_locations,
            per_page=per_page,
            max_pages=max_pages,
        ):
            for person in page:
                apollo_id = person.get("apollo_id", "")
                if not apollo_id:
                    continue
                if is_already_seen(apollo_id):
                    continue
                # Attach brand hint for Claude context
                person["_brand_hint"] = _detect_brand_fit(person.get("company", ""), channel_fit)
                new_prospects.append(person)
    except ApolloClientError as exc:
        log.error("linkedin_spy: Apollo scan failed — %s", exc)
        _post_to_slack(f"⚠️ *F3 LinkedIn Spy scan failed* — Apollo API error: {exc}")
        return

    total_new = len(new_prospects)
    log.info("linkedin_spy: %d new prospects found (not previously seen)", total_new)

    if not new_prospects:
        log.info("linkedin_spy: no new prospects — nothing to report")
        return

    # Step 2: log ALL new prospects to DB for dedup (before Claude, so we don't re-scan
    # them even if message generation fails)
    for p in new_prospects:
        log_prospect(
            apollo_id=p["apollo_id"],
            name=p.get("name"),
            title=p.get("title", ""),
            company=p.get("company", ""),
            linkedin_url=p.get("linkedin_url", ""),
            city=p.get("city", ""),
            state=p.get("state", ""),
            country=p.get("country", "US"),
        )

    # Step 3: generate Claude messages for the top N (for the Slack report)
    report_batch = new_prospects[:max_report]
    log.info("linkedin_spy: generating Claude outreach for %d prospects", len(report_batch))
    outreach = _generate_outreach(report_batch)

    # Index by apollo_id and write back brand_fit + message_draft to DB
    outreach_map = {o["apollo_id"]: o for o in outreach}
    for p in report_batch:
        aid = p["apollo_id"]
        o = outreach_map.get(aid, {})
        brand_hint = p.get("_brand_hint", {})
        # Update the DB row with generated content
        from cora.tools import linkedin_spy_client as _lsc
        with _lsc._get_conn() as conn:
            conn.execute(
                "UPDATE prospect_log SET brand_fit = ?, message_draft = ? WHERE apollo_id = ?",
                (
                    o.get("brand_fit") or brand_hint.get("brand", ""),
                    o.get("message_draft") or "",
                    aid,
                ),
            )
            conn.commit()

    # Step 4: pull final rows for the report (picks up the written brand_fit + message_draft)
    report_rows = get_pending_report_prospects(limit=max_report)
    total_seen = get_total_seen()

    if not report_rows:
        log.warning("linkedin_spy: no pending report rows after write — skipping Slack post")
        return

    # Step 5: post Slack report
    report_text = _format_report(report_rows, new_found=total_new, total_seen=total_seen)
    posted = _post_to_slack(report_text)

    if posted:
        for row in report_rows:
            mark_slack_notified(row["id"])
        log.info(
            "linkedin_spy: posted report to #%s — %d prospects surfaced",
            _NOTIFY_CHANNEL.lstrip("#"), len(report_rows),
        )
    else:
        log.error("linkedin_spy: Slack post failed — prospects NOT marked notified (will retry next run)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                _REPO_ROOT / "logs" / f"linkedin-spy-{datetime.now().strftime('%Y-%m-%d')}.log",
                encoding="utf-8",
            ),
        ],
    )
    run_scan()
