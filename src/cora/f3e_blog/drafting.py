"""LLM drafting for the F3E blog lane -- the ONLY place a model is involved.

The model writes prose. It does not decide whether the prose is publishable (that
is `preflight`, deterministic), it does not stage (that is `pipeline`), and it
cannot publish (`shopify_client.publish_article` is called from the tap handler
alone). FAIL-CLOSED throughout: any API, parse, or shape error returns None and
the run reports a skip rather than staging something half-built.

Facts come from two cleared sources only, both fetched at runtime and pasted into
the prompt: the live FAQ (published under the D-108 claims clearance) and the
canonical product-lineup file. The model is told to use nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import re

from ..model_router import MODEL_SONNET
from ..connectors import shopify_client
from . import operating_files, preflight

log = logging.getLogger(__name__)

# Sonnet 5 / Opus 5 run adaptive thinking BY DEFAULT when `thinking` is omitted,
# and max_tokens is a HARD cap on thinking + visible output COMBINED -- so an
# omitted `thinking` would spend the article's token budget on reasoning and
# return a truncated body. Disabled explicitly (D-091 class).
_THINKING_DISABLED = {"type": "disabled"}
_MAX_TOKENS = 8000

_PROMPT = """You are drafting one article for f3energy.com. Write it as the F3 Energy
team would: direct, confident, community-rooted; not bro-y, not corporate.

## The assignment
Working title: {title}
Lane / pillar: {lane_pillar}
Discovery target: {target_prompt}
Editorial notes for THIS post (follow them exactly): {notes}

## The template you must follow
{template}

## The ONLY facts you may use about F3 products
These two sources are the cleared record. Do not add any product fact, number,
certification, or ingredient that is not in them. If you need a fact you do not
have, leave that point out entirely rather than estimating it.

### Live FAQ
{faq}

### Canonical product lineup
{lineup}

## Hard copy rules (a draft breaking any of these is discarded, so do not)
- No em-dashes anywhere. Use a comma, a colon, or a full stop.
- No prices, no MSRP, no cost figures of any kind.
- "clean", "cleaner", "clean-label", "clean-sweetened", "natural" may be used ONLY
  about F3 Pure, and never in the SAME SENTENCE as F3 Energy or F3 Mood, even when
  the sentence is contrasting them and even when the clean word plainly attaches
  to Pure. This one is measured, not theoretical: a real draft was rejected for
  "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure".
  Split it: "F3 Energy carries the full stack. F3 Pure is the clean-sweetened
  version." Two sentences, one line each.
- F3 Mood is never a sleep aid and never makes anyone drowsy. Cleared framing is
  "calm and focus", "composure, not sedation".
- "NSF Certified for Sport" may be said of F3 Energy only. Name F3 Energy
  EXPLICITLY in the same sentence every single time. Never "it is NSF Certified
  for Sport" after a sentence about Energy: write "F3 Energy is NSF Certified for
  Sport." A pronoun there is rejected, because a reader who lands mid-page cannot
  tell which line the certification belongs to.
- No health, medical, or disease claims. Never name a condition (anxiety, ADHD,
  insomnia, and so on) as something F3 affects.
- F3 was founded in 2023 in Mesa, Arizona. Never 2022.
- No vegan, dairy-free, gluten-free, non-GMO, or organic PRODUCT claims.
  "organic cane sugar" as an ingredient descriptor is fine.
- F3 is a functional beverage, never a dietary supplement, on this site.
- Quotes must be real and attributed. If you have no cleared quote, use none.
- Leave no placeholders. Every bracket, link, and number must be final.

## Output
Reply with exactly these four sections, each introduced by its marker on its own
line, and nothing else. No preamble, no code fence, no JSON.

===TITLE===
the phrase a reader would actually search, max 65 characters
===SUMMARY===
one or two sentences, the excerpt shown in listings
===TAGS===
two to five short tags, comma separated
===BODY===
the full post as HTML using <p>, <h2>, <ul>, <li>, <strong>, <a>, including the
JSON-LD BlogPosting script block the template calls for

Everything after the ===BODY=== marker is the body, verbatim to the end of your
reply. Write normal HTML with normal double-quoted attributes and normal
quotation marks in the prose. Nothing needs escaping.
"""


def fetch_faq_text(limit: int = 14000) -> str:
    """The live FAQ as plain text. Empty string if it cannot be fetched.

    Fetched rather than cached in the repo on purpose: the FAQ is the cleared
    record, and a stale copy here would let a claims correction on the site fail
    to reach the drafts.
    """
    code, html = shopify_client.fetch_public_page(operating_files.FAQ_URL)
    if code != 200 or not html:
        log.error("f3e_blog: FAQ fetch failed (HTTP %s) -- cannot draft without it", code)
        return ""
    return preflight.html_to_text(html)[:limit]


_REVISION_NOTE = """

## YOUR PREVIOUS DRAFT WAS REJECTED -- fix exactly this and change nothing else
An automated claims check rejected the last attempt. It is not a matter of
opinion: the sentences below break a rail, so rewrite them and keep the rest of
the piece as it was.

{trips}

Rewrite so none of those rails can trip again, then return the corrected article
in full, in the same four marker sections.
"""


def build_prompt(row, *, template: str, faq: str, lineup: str,
                 revision_trips: str = "") -> str:
    prompt = _PROMPT.format(
        title=row.title,
        lane_pillar=row.lane_pillar,
        target_prompt=row.target_prompt,
        notes=row.notes or "(none)",
        template=template[:9000],
        faq=faq,
        lineup=lineup[:9000],
    )
    if revision_trips:
        prompt += _REVISION_NOTE.format(trips=revision_trips[:3000])
    return prompt


_SECTION_ORDER = ("TITLE", "SUMMARY", "TAGS", "BODY")
_MARKER_RE = re.compile(r"^===(TITLE|SUMMARY|TAGS|BODY)===\s*$", re.MULTILINE)


def _extract_sections(raw: str) -> dict | None:
    """Parse the marker-delimited reply. None if the markers are not all there.

    WHY THIS REPLACED A JSON ENVELOPE, measured rather than supposed. Asking for
    `body_html` inside a JSON object means every quotation mark in ordinary prose
    has to be escaped, and a real draft failed on exactly that: the model wrote

        reading past the "0 sugar" claim on the front of a can

    inside the JSON string, with `stop_reason: end_turn` -- a complete, sensible
    answer that no tolerant parser can repair, because an unescaped quote is
    indistinguishable from the end of the string. Quotation marks in prose are
    not an edge case; they are how people write.

    A marker envelope has no escaping rules at all: everything after ===BODY===
    is the body, bytes included. The JSON path is kept as a fallback for a model
    that answers in JSON anyway.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n")
                         if not l.startswith("```")).strip()
    marks = list(_MARKER_RE.finditer(text))
    if not marks:
        return None
    found: dict[str, str] = {}
    for i, m in enumerate(marks):
        name = m.group(1)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        found[name] = text[m.end():end].strip()
    if not all(k in found for k in _SECTION_ORDER):
        return None
    tags = [t.strip() for t in found["TAGS"].replace("\n", ",").split(",")]
    return {
        "title": found["TITLE"],
        "summary": found["SUMMARY"],
        "body_html": found["BODY"],
        "tags": [t for t in tags if t],
    }


def _extract_json(raw: str) -> dict | None:
    """Parse the model's JSON object, tolerantly.

    `strict=False` is load-bearing, not defensive noise: `body_html` is a long
    multi-line string, and a raw newline inside a JSON string literal is invalid
    under strict JSON. The first live revision pass failed for exactly that reason
    while its output was well under the token cap, so the "unparseable" verdict
    was about newline escaping, not about a truncated or confused answer.
    """
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = "\n".join(l for l in txt.split("\n") if not l.startswith("```")).strip()
    start, end = txt.find("{"), txt.rfind("}")
    if start == -1 or end <= start:
        return None
    blob = txt[start:end + 1]
    for kwargs in ({}, {"strict": False}):
        try:
            out = json.loads(blob, **kwargs)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(out, dict):
            return out
    return None


def draft_article(row, *, template: str, faq: str, lineup: str,
                  revision_trips: str = "") -> dict | None:
    """Draft one article. Returns {title, summary, body_html, tags} or None.

    FAIL-CLOSED: a missing key, a non-string field, or an empty body returns None.
    A half-formed draft must never reach the preflight, because a preflight that
    passes an empty body would stage an empty article.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("f3e_blog: ANTHROPIC_API_KEY not set -- cannot draft")
        return None
    if not faq:
        # The FAQ is the cleared source for every product fact. Drafting without
        # it would mean drafting product copy from the model's own memory.
        log.error("f3e_blog: no FAQ text -- refusing to draft product copy")
        return None

    prompt = build_prompt(row, template=template, faq=faq, lineup=lineup,
                          revision_trips=revision_trips)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=_MAX_TOKENS,
            thinking=_THINKING_DISABLED,
            messages=[{"role": "user", "content": prompt}],
        )
        from ..llm_usage import log_usage
        log_usage(response, caller="f3e_blog_drafting", model=MODEL_SONNET)
        raw = "".join(
            getattr(b, "text", "") for b in response.content
            if getattr(b, "type", "") == "text"
        )
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.error("f3e_blog: draft call failed for row %s: %s", row.number, exc)
        return None

    parsed = _extract_sections(raw) or _extract_json(raw)
    if not parsed:
        # Log the head of what actually came back. "not parseable" with no
        # evidence sends the next reader guessing at token limits when the real
        # cause is in the first line.
        log.error("f3e_blog: draft for row %s was not parseable JSON; raw begins: %r",
                  row.number, (raw or "")[:300])
        return None

    title = str(parsed.get("title") or "").strip()
    summary = str(parsed.get("summary") or "").strip()
    body = str(parsed.get("body_html") or "").strip()
    tags = parsed.get("tags") or []
    if not title or not body:
        log.error("f3e_blog: draft for row %s missing title or body", row.number)
        return None
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip() for t in tags if str(t).strip()][:5]

    return {
        "title": title[:120],
        "summary": summary,
        "body_html": body,
        "tags": tags,
    }
