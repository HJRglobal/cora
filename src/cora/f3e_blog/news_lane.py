"""News lane: amplify earned press when a tracked pitch flips to Published (S5).

VERIFY-FIRST outcome, 2026-08-26: the kickoff said Cora's press-tracker read
access was UNCONFIRMED and told this session not to assume it. It exists. The
Media Contacts press pipeline is already wired (`notion_client._PRESS_DB_ID`) and
a live probe returned 211 rows with a populated Status field, 2 of them Published.
So the real weekly sweep is built here rather than the typed-ask fallback the
kickoff described for the no-read-lane case.

The flow mirrors the Learn lane exactly -- draft, deterministic preflight, stage
UNPUBLISHED, card Harrison -- because a News post is the riskier of the two lanes,
not the safer one: it names counterparties and restates an outlet's words.

Two rails here are human judgment and CANNOT be code-checked (verified quotes,
counterparty-safe announcements). They are named on the card, where Harrison
decides, rather than implied away by a green preflight.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from ..connectors import shopify_client
from . import drafting, operating_files, preflight, publish_cards

log = logging.getLogger(__name__)

# Entity values on a press row that mean "this coverage is about F3". "Both"
# counts for F3 as well (a feature covering F3 and Lexington together).
_F3_ENTITIES = ("F3E", "Both")
_PUBLISHED = "Published"

_NEWS_PROMPT_EXTRA = """
## This is an earned-press amplification post
Outlet: {outlet}
Reporter: {reporter}
Published: {date}
Source article: {link}
Headline as printed: {headline}

Extra rules for this post, on top of everything above:
- Restate ONLY facts the outlet actually printed, and attribute them to the
  outlet by name ("the {outlet} reports...").
- Link the source article. Never reproduce more than a short snippet of it, and
  never use the outlet's photography.
- No raise size, valuation, individual stakes, or funding mechanics. A revenue
  figure the outlet already printed may be restated with attribution.
- Do not invent a quote from the reporter, from Harrison, or from anyone else.
"""


@dataclass(frozen=True)
class PressFlip:
    page_id: str
    reporter: str
    outlet: str
    date: str
    link: str
    headline: str = ""

    @property
    def key(self) -> str:
        return self.page_id


def published_f3_rows() -> list[PressFlip]:
    """Published F3 coverage rows from the press tracker.

    Fail-soft: returns [] and logs on any read failure. A press-tracker outage
    must not fail the whole weekly run, which also has a Learn draft to stage.
    """
    try:
        from ..tools import notion_client as nc
        pages = nc._paginate(db_id=nc._PRESS_DB_ID)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        log.warning("f3e_blog news: press tracker unreadable: %s", exc)
        return []

    out: list[PressFlip] = []
    for page in pages:
        props = page.get("properties", {})
        try:
            if nc._select(props, "Status") != _PUBLISHED:  # noqa: SLF001
                continue
            if nc._select(props, "Entity") not in _F3_ENTITIES:  # noqa: SLF001
                continue
            out.append(PressFlip(
                page_id=page.get("id", ""),
                reporter=nc._title(props, "Reporter"),  # noqa: SLF001
                outlet=nc._rich_text(props, "Outlet"),  # noqa: SLF001
                date=nc._date_start(props, "Date Pitched") or "",  # noqa: SLF001
                link=nc._url(props, "Coverage Link") or "",  # noqa: SLF001
            ))
        except Exception as exc:  # noqa: BLE001 -- one bad row must not lose the rest
            log.warning("f3e_blog news: skipping a press row: %s", exc)
    return out


_TITLE_RE = re.compile(r"<title[^>]{0,200}>(.{0,300}?)</title>",
                       re.IGNORECASE | re.DOTALL)


def fetch_headline(url: str) -> str:
    """The outlet's printed headline, read off the page. '' if unavailable.

    Read rather than guessed: the News template requires the headline verbatim,
    and a model asked to recall a headline will produce a plausible one.
    """
    if not url:
        return ""
    code, html = shopify_client.fetch_public_page(url)
    if code != 200 or not html:
        return ""
    m = _TITLE_RE.search(html)
    if not m:
        return ""
    return " ".join(m.group(1).split())[:200]


def new_flips(flips: list[PressFlip], seen: list[str]) -> list[PressFlip]:
    return [f for f in flips if f.key and f.key not in set(seen or [])]


def draft_and_stage_flip(
    flip: PressFlip,
    *,
    template: str,
    faq: str,
    lineup: str,
    report,
    dry_run: bool = False,
    client_factory=None,
) -> tuple[bool, str]:
    """(staged, human-readable line). Never raises."""
    headline = fetch_headline(flip.link)
    row = _PressRow(flip, headline)

    # Same one-bounded-revision path the Learn lane uses, so a phrasing slip on a
    # press post is not an automatic week-long skip either.
    from .pipeline import draft_until_clean
    draft, result = draft_until_clean(
        row, template=template, faq=faq, lineup=lineup, lane="news", report=report)
    if not draft:
        return False, ("Press flip %r (%s): the draft did not come back usable, so "
                       "nothing was staged." % (flip.reporter, flip.outlet))

    if not result.passed:
        return False, ("Press flip %r (%s): claims preflight blocked the draft (%s). "
                       "Nothing was staged."
                       % (flip.reporter, flip.outlet,
                          ", ".join(result.tripped_rail_ids)))

    if dry_run:
        return False, ("DRY RUN: would stage a News post for %r (%s): %r"
                       % (flip.reporter, flip.outlet, draft["title"]))

    try:
        article = shopify_client.create_article(
            blog_id=operating_files.BLOG_NEWS,
            title=draft["title"], body_html=draft["body_html"],
            summary=draft["summary"], tags=list(draft["tags"]) + ["News", "Press"],
        )
    except Exception as exc:  # noqa: BLE001
        return False, ("Press flip %r: staging FAILED and nothing is live: %s"
                       % (flip.reporter, exc))

    rec = publish_cards.record_for_article(
        article=article, lane="news", excerpt=draft["summary"],
        backlog_row=None, rails_passed=len(result.rails_checked),
    )
    publish_cards.stage_card(rec, client_factory=client_factory)
    return True, ("Staged a News amplification for %r (%s) UNPUBLISHED and carded "
                  "it: %r -> %s" % (flip.reporter, flip.outlet, article.get("title"),
                                    shopify_client.article_admin_url(article["id"])))


class _PressRow:
    """Adapts a press flip to the shape `drafting.draft_article` expects.

    The News lane has no backlog row, so the assignment fields are synthesised
    here instead of being read from the editorial table.
    """

    def __init__(self, flip: PressFlip, headline: str):
        self.number = "news"
        self.title = headline or ("F3 Energy featured in %s" % (flip.outlet or "the press"))
        self.lane_pillar = "News (earned press amplification)"
        self.target_prompt = "branded press discovery"
        self.notes = _NEWS_PROMPT_EXTRA.format(
            outlet=flip.outlet or "the outlet",
            reporter=flip.reporter or "the reporter",
            date=flip.date or "(date not recorded)",
            link=flip.link or "(no link recorded)",
            headline=headline or "(headline not available)",
        )


def sweep(report, state: dict, *, dry_run: bool = False, client_factory=None) -> dict:
    """Weekly News sweep. Mutates and returns `state`.

    First run ADOPTS the current Published set as the baseline rather than
    drafting for every historical flip: the two rows already Published on
    2026-08-26 were amplified by hand that same day, and re-amplifying them would
    be a duplicate post, not a catch-up.
    """
    flips = published_f3_rows()
    if not flips:
        report.say("Press tracker: no Published F3 coverage rows readable this run.")
        return state

    seen = state.get("news_seen_page_ids")
    if seen is None:
        state["news_seen_page_ids"] = [f.key for f in flips]
        report.say("Press tracker baseline recorded: %d row(s) already Published, "
                   "left alone (they were amplified by hand)." % len(flips))
        return state

    fresh = new_flips(flips, seen)
    if not fresh:
        report.say("Press tracker: no new Published flips since the last run.")
        return state

    try:
        template = operating_files.read_templates()
        lineup = operating_files.read_lineup()
    except Exception as exc:  # noqa: BLE001
        report.say("Press tracker found %d new flip(s) but the templates or lineup "
                   "did not load (%s), so nothing was drafted." % (len(fresh), exc))
        return state

    faq = drafting.fetch_faq_text()
    if not faq:
        report.say("Press tracker found %d new flip(s) but the live FAQ did not "
                   "load, so nothing was drafted." % len(fresh))
        return state

    # One post per run: a batch of amplifications on the same day reads as
    # scaled content, and the whole cadence ruling was depth over volume.
    flip = fresh[0]
    staged, line = draft_and_stage_flip(
        flip, template=template, faq=faq, lineup=lineup, report=report,
        dry_run=dry_run, client_factory=client_factory)
    report.say(line)
    if len(fresh) > 1:
        report.say("%d other new flip(s) are waiting and will be offered on later "
                   "runs, one per week." % (len(fresh) - 1))
    if staged:
        # Only mark it consumed once something was really staged, so a failed
        # draft is retried next week instead of being silently dropped.
        state.setdefault("news_seen_page_ids", []).append(flip.key)
    return state
