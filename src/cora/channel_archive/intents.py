"""Founder-only text intents of the dead-channel lane (Code #16 C1, A21) and the
deterministic replies they get. Every branch here returns BEFORE the model and
before code_queue's signal capture, so an archive request can never be narrated by
a zero-tool model turn ("Archived 12 channels" trips no rail: `archived` is not in
the ruled phantom-write lexicon) and can never re-seed a build ask (the 9/20
three-captures incident).

  * ``looks_like_archive_ask`` -- the start-anchored grammar: "archive the dead
    channels" (+ a greeting / affirmative, a vocative with any punctuation, a polite
    modal, a determiner, a deadness adjective, "slack", a short tail incl. ", please"
    and emoji). -> the scan.
  * ``looks_like_archive_attempt`` -- an ``archive`` verb whose DIRECT object is a
    channel ("channel(s)" or a ``<#C...>`` token, not the sales sense, not inside a
    prepositional phrase) that FAILED the grammar ("archive #old-promo", "archive
    the dead channels except #x"). -> a refusal naming the one thing that works.
  * ``looks_like_archive_status`` -- an interrogative about the LANE's state ("did
    you archive <channels>", "<channels> got archived", "the archive card"), never a
    how-to / policy / decision question, someone else's archive ("archived by alex")
    or a time frame older than the lane ("in june", the sprawl). -> a store/ledger-
    backed status line,
    never a model turn, never a forced tool (lesson 83: a forced read is itself a
    tool_use).
  * ``looks_like_live_followup`` -- while a card is live in this DM, any text
    opening with the archive verb whose object is not plainly some other thing
    ("archive them / the rest / everything", "yes, archive them"), or a bare yes /
    ok / go ahead / do it within 30 minutes of the card. -> "that reply archived
    nothing".

Every predicate first normalizes (whitespace collapsed, channel tokens masked to
``#chan``, emphasis / leading punctuation stripped) and caps the input (a real ask is
short), so no pattern can backtrack on a 40k-space message (each is also timed in
tests, including the capped worst cases of the filler / tail patterns).
"""
from __future__ import annotations

import html
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .deliver import MISSING_PARTS_LEAD

MAX_CHARS = 300
FOLLOWUP_WINDOW_S = 30 * 60
#: A card ts is SLACK's clock and ``now`` is the host's (and a .6f stamp rounds up
#: about half the time): a card up to this far in the FUTURE is clock skew, not a
#: stale card (harness-isolation#0).
FOLLOWUP_SKEW_S = 120

_MENTION_TOKEN_RE = re.compile(r"<@[A-Za-z0-9_]{2,24}(?:\|[^>\n]{0,40})?>")
_CHANNEL_TOKEN_RE = re.compile(r"<#C[A-Z0-9]{6,24}(?:\|[^>\n]{0,80})?>")
_LIST_MARKER_RE = re.compile(r"\A[-*•>]{1,3} ")
#: Italic / strikethrough wrappers (``_x_`` / ``~x~``): an underscore or tilde NOT
#: between two alphanumerics, so ``:thumbs_up:`` and ``snake_case`` survive.
_EMPHASIS_RE = re.compile(r"(?<![A-Za-z0-9])[_~]+|[_~]+(?![A-Za-z0-9])")
_PUNCT = "[,:;!.\u2014\u2013-]"
#: The punctuation a stripped mention leaves at the front ("<@BOT>, archive ...").
_LEAD_PUNCT_RE = re.compile(r"\A" + _PUNCT + r"+ ?")
#: A channel TOKEN becomes this placeholder in the normalized view, so a channel's
#: NAME ("hjr-archive-2024") can never supply a predicate's keyword.
CHANNEL_PLACEHOLDER = "#chan"

# ── grammar pieces (every input is whitespace-collapsed to single spaces first) ──
#: an optional greeting / affirmative lead-in ("hey", "yes,", "ok", "sounds good,")
_LEAD = ("(?:(?:hey|hi|hello|yo|yes|yep|yeah|ya|ok|okay|k|sure|alright|all right|"
         "sounds good|great|perfect|cool)(?: ?" + _PUNCT + "+)? )?")
#: the vocative, with any punctuation ("cora,", "cora!", "cora —", "@cora")
_VOC = "(?:@?cora(?: ?" + _PUNCT + "+)? )?"
#: a polite modal and up to two softeners ("can we", "could you please go ahead and")
_POLITE = ("(?:(?:can|could|would|will) (?:you|u|we) )?"
           "(?:(?:please|pls|plz|just|now|then|so|go ahead and|time to|let's|lets|let us) ){0,2}")
_PREFIX = _LEAD + _VOC + _POLITE
_ASK_VERB = "(?:archive|(?:do (?:you|u) )?mind archiving)"
_TAIL_WORD = ("(?:now|please|pls|plz|for me|for us|in slack|in our slack|thanks|thank you|thx|ty|"
              "today|asap|again|already|when you can|when you get a chance|real quick|quickly)")
_THANKS = "(?:thanks|thank you|thx|ty)"
#: a trailing vocative ("..., cora" / "... please cora") -- D-051 r2 c1-intents-copy#1
_VOC_END = "(?:,? @?cora)?"
_EMOJI = ("(?::[a-z0-9_+'-]{1,40}:|"
          "[\u2600-\u27bf\U0001f300-\U0001faff\ufe0f\u200d]{1,8})")
#: ", please" / " pls" / " thanks" / " again" (max three) and a trailing "cora", then
#: one sentence-final thanks (". thanks!" / " \u2014 thanks"), then emoji or :shortcodes:
#: (max three)
_TAIL = ("(?:,? " + _TAIL_WORD + "){0,3}" + _VOC_END + "[?.!]*"
         "(?:(?: [\u2014\u2013-]+| ?[,;])? " + _THANKS + _VOC_END + "[?.!]*)?"
         "(?: ?" + _EMOJI + "[?.!]*){0,3}")
_DEAD_ADJ = "(?:dead|inactive|stale|unused|quiet|old|abandoned|idle|silent)"
#: one deadness adjective, or two -- adjacent or joined by and / or / '/' / ',' / '+'
_DEAD_ADJS = _DEAD_ADJ + "(?:(?: ?[,/+&] ?| (?:and|or|and/or) | )" + _DEAD_ADJ + ")?"
_ASK_RE = re.compile(
    r"\A" + _PREFIX + _ASK_VERB +
    r" (?:(?:all of the|all of our|all of my|all our|all my|all the|all|any|our|my|the|those|"
    r"these|every) )?" + _DEAD_ADJS + r" (?:slack )?channels?" + _TAIL + r"\Z")

#: a determiner run at the head of the verb's object
_DET = "(?:the|these|those|this|that|my|our|all|any|some|every|each|both|a|an|of)"
#: a word that ends the object noun phrase (a preposition / conjunction / clause
#: opener) -- "channel" AFTER one of these is not what is being archived
_STOP = ("(?:about|from|for|in|on|with|of|to|by|re|regarding|and|or|then|but|except|"
         "after|before|that|which|who|when|where|if|unless|into|at|via|without|than|"
         "so|because|as)")
_FILLER = "(?!" + _STOP + r"\b)[a-z0-9#'_-]+"
#: things that are archived but are not Slack channels
_OBJ_NOUNS = ("(?:e-?mails?|mails?|inbox(?:es)?|threads?|messages?|dms?|conversations?|"
              "chats?|deals?|tasks?|projects?|docs?|documents?|files?|folders?|reports?|"
              "notes?|sheets?|spreadsheets?|pdfs?|decks?|contacts?|tickets?|invoices?|"
              "orders?|transcripts?|recordings?|meetings?|events?|pages?|posts?|boards?|"
              "repos?|issues?|photos?|images?|videos?|attachments?|drafts?|"
              "histor(?:y|ies)|logs?)")
#: a word right AFTER "channel" that makes it the sales / media sense
_SALES_NOUNS = ("(?:partners?|partnerships?|sales|strateg(?:y|ies|ic)|launch(?:es)?|mix|"
                "managers?|teams?|revenue|pricing|prices?|data|metrics?|performance|"
                "inventory|accounts?|budgets?|spend|campaigns?|content|leads?|pipelines?|"
                "forecasts?|plans?|planning|summar(?:y|ies)|analysis|reviews?|numbers|"
                "results|kpis?|goals?|targets?|sync|calls?|contracts?|agreements?|"
                "audits?|growth|expansion|marketing|ads?|conflicts?|checks?|excel)")
_CHANREF = "(?:channels?|#[a-z0-9][a-z0-9_-]*)"
#: "archive" as a NOUN at the front ("archive of the old site is in drive", "archive
#: folder", "archive: ...") is not an archive request (lesson 45)
_NOUN_SENSE = ("(?:of|for|is|was|were|has|had|looks|seems|should|will|would|can|could|"
               "must|might|folders?|files?|links?|page|policy|policies|drive|box|bin|"
               "logs?|section|tab|view|index|copy|copies|versions?|site|url|access|"
               "search|button|feature|settings?|status)")
_ARCHIVE_V = r"archive(?! " + _NOUN_SENSE + r"\b)(?![:;])"
#: a Slack-channel object: "channel(s)" or a channel ref, NOT "channel's ..." and
#: NOT followed by a sales / other-object noun ("the retail channel deals")
_CHAN_OBJ = (_CHANREF + r"\b(?!['\u2019]s?\b)(?! (?:" + _OBJ_NOUNS + "|" + _SALES_NOUNS
             + r")\b)")
_OBJECT_NP = "(?:" + _DET + " ){0,3}(?:" + _FILLER + " ){0,3}?"
#: ... or a COORDINATED modifier list right before the channel noun ("the promo and
#: launch channels", "promo/event channels", "the promo, event and launch channels"):
#: its first word is not another object ("the email and the channel" is two things)
#: and nothing stands between the list and the noun ("archive it and tell the
#: channel" is a second clause) -- D-051 r2 c1-intents-copy#1 (a).
_COORD = "(?: ?[,/+&] ?| (?:and|or|and/or) )"
_OBJECT_NP_COORD = ("(?:" + _DET + " ){0,3}(?:" + _FILLER + " ){0,2}?(?!" + _OBJ_NOUNS + r"\b)"
                    + _FILLER + "(?:" + _COORD + _FILLER + "){1,2} ")
#: A21(b), tightened (c1-intents-copy#6): the archive verb's DIRECT object is a
#: channel -- "archive the old promo channels", "archive #x" -- never "archive the
#: email from the channel" or "archive the retail channel deals".
_ATTEMPT_RE = re.compile(r"\A" + _PREFIX + _ARCHIVE_V + " (?:" + _OBJECT_NP + "|" + _OBJECT_NP_COORD
                         + ")" + _CHAN_OBJ)

_STATUS_START_RE = re.compile(
    r"\A" + _LEAD + _VOC +
    r"(?:did|do|does|has|have|had|were|was|is|are|which|what|what's|whats|how|when|why|"
    r"where|who|any|show me|list|status)\b")
_STATUS_ARCH_RE = re.compile(r"\barchiv")
#: how-to / policy / decision framings are questions ABOUT archiving, not about the
#: lane's state (integration#5) -- they go to the model.
_STATUS_BAIL_RE = re.compile(
    r"\b(?:how (?:do|does|can|could|should|would|to)|should|shall|ought|polic(?:y|ies)|"
    r"rules?|decide[ds]?|deciding|decision|guidelines?|best practices?|safe to|ok to|"
    r"okay to|allowed|access|permissions?|what happens|supposed to|mean)\b")
_AGENT = "(?:you|cora|u|we)"
_ADV = ("(?:already|ever|actually|really|just|finally|go ahead and|end up|able to|get to|"
        "try to|manage to)")
_PASSIVE_AUX = ("(?:all|already|ever|actually|really|now|yet|finally|just|been|being|get|"
                "got|gotten|were|was|are|is|be|have|has|had)")
#: A21(c), tightened to lane-status OBJECTS (c1-intents-copy#0): "did you archive
#: <channels>", "<channels> got archived", "<channels> (did) you archive(d)", or the
#: lane's own nouns ("the archive card / proposal", "the dead channels archive").
_STATUS_LANE_RE = re.compile(
    r"\b(?:did|have|has|had|were|was) " + _AGENT + " (?:" + _ADV + " ){0,2}"
    r"(?:archive[ds]?|archiving) " + _OBJECT_NP + _CHAN_OBJ +
    "|" + _CHAN_OBJ + "(?: " + _PASSIVE_AUX + r"){0,3} archived\b"
    r"(?! (?:[a-z0-9-]+ ){0,2}" + _OBJ_NOUNS + r"\b)" +   # not "archived excel reports"
    "|" + _CHANREF + r"\b (?:(?:did|have|has|had|that) )?" + _AGENT
    + r"(?:'ve|\u2019ve)? (?:(?:have|has|had) )?(?:" + _ADV + r" ){0,2}archive[ds]?\b" +
    r"|\b(?:archive|archiving|dead[ -]channels?|inactive[ -]channels?|"
    r"channel[ -]archiv(?:e|ing)) (?:cards?|proposals?|lane|scans?)\b"
    r"|\b(?:dead|inactive|stale)[ -]channels? archiv(?:e|al|ing)\b")
#: ... but the lane's line answers what the LANE did (D-051 r2 integration#3): an
#: archive BY someone else ("archived by alex / me / slack / the sprawl script") is
#: not its question. "by you / cora / the lane / a tap" and a deadline ("by now",
#: "by friday", "by 5pm") still are.
_BY_OTHER_RE = re.compile(
    r"\barchived by (?!(?:you|u|cora|yourself|the (?:lane|card|cards|bot|scan|proposal)|a tap|"
    r"my tap|taps?|now|then|today|tonight|tomorrow|yesterday|this|next|last|the end|end|eod|"
    r"eow|cob|noon|midnight|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b|\d)[a-z#]")
#: ... nor a named time frame that STARTS before the lane was born (monitor.LANE_EPOCH,
#: 2026-09-25): "in june", "last summer", "since june", "in 2025", "last year", the
#: June sprawl. A relative "last week" / "today" / "yet" is lane time. The month /
#: season is resolved against the CLOCK (its most recent occurrence), so "in june"
#: is pre-lane in 2026 and lane time in 2027.
_MONTH_NUM = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
              "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
              "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11,
              "nov": 11, "december": 12, "dec": 12}
_SEASON_START = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9, "winter": 12}
_PERIOD_RE = re.compile(
    r"\b(?:(in|during|over|throughout|back in|since|from|after|last|this past)(?: the)?"
    r"(?: (?:early|mid|late))?[ -](" + "|".join(sorted(_MONTH_NUM, key=len, reverse=True))
    + r")(?:,? ((?:19|20)\d\d))?"
    r"|(?:in|during|over|throughout|back in|since|from) ((?:19|20)\d\d)"
    r"|(last|this past|this|over the|during the|in the|since) (spring|summer|fall|autumn|winter)"
    r"|(last year)|(sprawl))\b")
_AZ = timezone(timedelta(hours=-7))      # Arizona, no DST -- monitor.LANE_EPOCH's zone


def _period_start(m: re.Match, today: date) -> date:
    prep, mon, yr, yr2, sprep, season, last_year, sprawl = m.groups()
    if sprawl:
        return date.min
    if last_year:
        return date(today.year - 1, 1, 1)
    if yr2:
        return date(int(yr2), 1, 1)
    if mon:
        mi = _MONTH_NUM[mon]
        if yr:
            return date(int(yr), mi, 1)
        y = today.year if mi <= today.month else today.year - 1
        if prep in ("last", "this past") and mi == today.month:
            y -= 1                                   # "last september" asked in September
        return date(y, mi, 1)
    sm = _SEASON_START[season]
    y = today.year if date(today.year, sm, 1) <= today else today.year - 1
    end_y, end_m = (y + 1, (sm + 3) - 12) if sm + 3 > 12 else (y, sm + 3)
    if sprep in ("last", "this past") and today < date(end_y, end_m, 1):
        y -= 1                                       # "last summer" asked in the summer
    return date(y, sm, 1)


def _names_pre_lane_period(t: str, now: float) -> bool:
    from .monitor import LANE_EPOCH  # noqa: PLC0415
    born = datetime.fromtimestamp(LANE_EPOCH, _AZ).date()
    today = datetime.fromtimestamp(now, _AZ).date()
    return any(_period_start(m, today) < born for m in _PERIOD_RE.finditer(t))

#: A21(d), widened (c1-intents-copy#1): while a card is live, ANY text opening with
#: the archive verb (after an optional yes / ok / sure / sounds good / just / now /
#: please / go ahead and) is a typed follow-up ...
_FOLLOWUP_VERB_RE = re.compile(r"\A" + _PREFIX + _ARCHIVE_V + r"\b")
#: ... unless its object is plainly some other thing ("archive this thread",
#: "archive the retail channel deals") -- that is not about the card.
_FOREIGN_OBJECT_RE = re.compile(
    " (?:" + _DET + " ){0,3}(?:" + _FILLER + " ){0,4}?(?:" + _OBJ_NOUNS
    + "|channels? " + _SALES_NOUNS + r")\b")
#: ... or whose place is some other system ("archive everything in the promo folder",
#: "archive them in my gmail").
_FOREIGN_PLACE_RE = re.compile(
    r"\b(?:in|from|inside|on|at) (?:(?:my|the|our|his|her|their) )?(?:[a-z0-9-]+ ){0,2}"
    r"(?:folders?|drives?|inbox(?:es)?|gmail|e-?mail|asana|hubspot|notion|dropbox|quickbooks|"
    r"qbo|shopify|deposco|airtable|trello|jira|github|calendar)\b")
#: A21(d) "a bare yes / ok / go ahead / do it", as people type it (D-051 r2
#: c1-intents-copy#0): a short run (max 4) of affirmatives and softeners joined by
#: punctuation / "and", an optional leading or trailing "cora", a thumbs-up. Any
#: other word (a condition, a stop, "thanks", a question's content) fails it.
_AFF = (r"(?:yes|yep|yeah|yup|ya|yea|y|ok|okay|k|kk|sure|go ahead|go for it|go|do it|"
        r"let's do it|lets do it|please do|sounds good|sounds great|confirm|confirmed|"
        # (normalize strips a message's LEADING ':' -- a bare ":+1:" arrives as "+1:")
        r"approve|approved|proceed|:?\+1:(?::skin-tone-[2-6]:)?|:?thumbsup:|:?thumbs_up:|"
        "\U0001f44d[\U0001f3fb-\U0001f3ff]?)")
_AFF_SOFT = "(?:please|pls|plz|just|now)"
_AFF_SEP = "(?: ?" + _PUNCT + "* (?:and )?)"
_BARE_YES_RE = re.compile(
    r"\A(?:@?cora ?" + _PUNCT + "* )?(?:" + _AFF_SOFT + " )?" + _AFF
    + "(?:" + _AFF_SEP + "(?:" + _AFF + "|" + _AFF_SOFT + ")){0,3}"
    r"(?:,? @?cora)?[?.!]*\Z")


def normalize(text: str, *, bot_user_id: str | None = None) -> str:
    """Lowercase, entity-unescaped, Slack mention tokens removed, channel tokens
    replaced by ``#chan`` (a channel's NAME never feeds a predicate), whitespace
    collapsed to single spaces, list marker / code / bold / italic / strike wrappers
    and any leading punctuation a stripped mention left behind removed, capped."""
    t = html.unescape(str(text or "")[: MAX_CHARS * 4])
    t = _MENTION_TOKEN_RE.sub(" ", t)
    t = _CHANNEL_TOKEN_RE.sub(" " + CHANNEL_PLACEHOLDER + " ", t)
    t = " ".join(t.split())
    t = _LIST_MARKER_RE.sub("", t)
    t = t.replace("`", "").replace("*", "")
    t = _EMPHASIS_RE.sub("", t)
    t = _LEAD_PUNCT_RE.sub("", t.strip())
    return " ".join(t.split()).lower()[:MAX_CHARS]


def _ok(t: str) -> bool:
    return bool(t) and len(t) < MAX_CHARS


def looks_like_archive_ask(text: str) -> bool:
    t = normalize(text)
    return _ok(t) and bool(_ASK_RE.match(t))


def looks_like_archive_attempt(text: str) -> bool:
    t = normalize(text)
    if not _ok(t) or _ASK_RE.match(t):
        return False
    return bool(_ATTEMPT_RE.match(t))


def looks_like_archive_status(text: str, *, now: float | None = None) -> bool:
    t = normalize(text)
    if not _ok(t) or not _STATUS_START_RE.match(t) or not _STATUS_ARCH_RE.search(t):
        return False
    if _STATUS_BAIL_RE.search(t) or not _STATUS_LANE_RE.search(t):
        return False
    if _BY_OTHER_RE.search(t):
        return False
    try:
        return not _names_pre_lane_period(t, time.time() if now is None else float(now))
    except Exception:  # noqa: BLE001 -- an unresolvable frame is not the lane's question
        return False


def followup_shape(text: str) -> str | None:
    """"imperative" (any text opening with the archive verb whose object is not
    plainly some other thing: "archive them / the rest / everything / all 12",
    "yes, archive them") | "affirmative" (a bare yes / ok / go ahead / do it) | None.
    Pure text: the caller reads the store only when this is not None."""
    t = normalize(text)
    if not _ok(t):
        return None
    m = _FOLLOWUP_VERB_RE.match(t)
    if m and not _FOREIGN_OBJECT_RE.match(t, m.end()) and not _FOREIGN_PLACE_RE.search(t, m.end()):
        return "imperative"
    if _BARE_YES_RE.match(t):
        return "affirmative"
    return None


def looks_like_live_followup(text: str, *, card_ts: float | None, now: float | None = None) -> bool:
    """Only meaningful while a proposal is live in this DM (the caller checks that and
    passes the newest card message's time). An imperative counts for the card's whole
    life; a bare affirmative only within 30 minutes of the card."""
    shape = followup_shape(text)
    if shape is None or card_ts is None:
        return False
    if shape == "imperative":
        return True
    now = time.time() if now is None else float(now)
    return -FOLLOWUP_SKEW_S <= now - float(card_ts) <= FOLLOWUP_WINDOW_S


# ── replies (deterministic, code-authored, tier-truthful) ────────────────────
#: "five to ten minutes" is pacing ARITHMETIC, not a measurement (no live scan has run
#: from a Code session): ~100 member channels read at >= 2 s per history call, plus
#: phase 2 + pins for the inactive ones -> roughly 6-8 minutes.
ACK_REPLY = ("Scanning the channels I belong to for 90+ days without a person posting — "
             "metadata only; nothing will be archived by this scan. The proposal card will "
             "follow here in about five to ten minutes.")
CHANNEL_ACK_REPLY = ("Scanning now — the proposal card will arrive in your DM; nothing will be "
                     "archived by this scan.")
SCAN_RUNNING_REPLY = "A scan is already running; its card will arrive here."
#: the channel @mention (and /cora-ask) variant: the card ALWAYS lands in Harrison's
#: DM (deliver_proposal), never in the channel the ask came from (c1-intents-copy#4)
SCAN_RUNNING_CHANNEL_REPLY = "A scan is already running; its card will arrive in your DM."
SCAN_FAILED_REPLY = ("The dead-channel scan stopped before a card was built — nothing was "
                     "archived. Ask again.")
OFF_REPLY = "The dead-channel lane is switched off (CORA_CHANNEL_ARCHIVE=off). Nothing was scanned."
ATTEMPT_REPLY = ("I only run the full dead-channel scan — nothing was archived. Say 'archive "
                 "the dead channels' here; exceptions are the Keep buttons.")
CATCHUP_DRAFT = "This was a dead-channel request — ask again live; nothing was archived."
EVAL_NOOP = ""
#: how every line this lane posts in the DM begins (the card's text= included, and
#: deliver's partial-delivery line -- its own constant, D-051 r2 c1-intents-copy#2):
#: a newer bot message that is one of THESE does not take a bare "yes" from the card
_LANE_REPLY_PREFIXES = ("That reply archived nothing", "Typed replies don't act",
                        "Dead-channel lane:", "Dead-channel proposal", "I only run the full dead-channel",
                        "Scanning the channels I belong to", "Scanning now — the proposal card",
                        "A scan is already running", "The dead-channel scan stopped",
                        "The dead-channel lane is switched off", "This was a dead-channel request",
                        MISSING_PARTS_LEAD)


def is_lane_reply(text: str) -> bool:
    t = str(text or "").strip()
    return bool(t) and t.startswith(_LANE_REPLY_PREFIXES)


#: Scoped to the TYPED turn (c1-authority-tier#4): true whatever the lane has done.
FOLLOWUP_REPLY_LEAD = "That reply archived nothing — only the card's buttons act."


def _lane_never_archived() -> bool:
    """True only when the ledger is readable and EMPTY and the lane is not demoted (a
    demotion means the monitor found an archive). Every ledger row is archive history:
    an intent / outcome, an ``unarchived_seen``, and the ``acknowledged`` rows
    Harrison's demotion clear writes for the archives it listed (D-051 r2
    c1-intents-copy#3) -- none of those may be followed by 'nothing has been archived'."""
    from . import policy  # noqa: PLC0415
    from . import store as st  # noqa: PLC0415
    try:
        if policy.is_demoted():
            return False
        ledger = st.read_ledger()
    except Exception:  # noqa: BLE001
        return False
    return ledger is not None and not ledger


def followup_reply() -> str:
    from . import gates, policy  # noqa: PLC0415
    tier = "T1" if (policy.acting_tier() == "T1" and gates.registry_allows_t1() is True) else "T0"
    if tier == "T1":
        return FOLLOWUP_REPLY_LEAD + " Nothing is archived without a tap on the card."
    # "nothing has been archived" is a claim about the LANE's history: only when the
    # ledger proves it. A lane back at T0 after real archives (a demotion, a flag
    # roll-back) states the T0 rule instead -- never a denial the ledger contradicts.
    if _lane_never_archived():
        return FOLLOWUP_REPLY_LEAD + " Lane at T0: nothing has been archived."
    return FOLLOWUP_REPLY_LEAD + " Lane at T0: a tap records your mark; nothing is archived at T0."


def status_reply(*, now: float | None = None) -> str:
    """A store/ledger-backed status line. Counts only -- never a channel name."""
    from . import gates, policy  # noqa: PLC0415
    from . import store as st  # noqa: PLC0415
    now = time.time() if now is None else float(now)
    acting_t1 = policy.acting_tier() == "T1" and gates.registry_allows_t1() is True
    lane = ("T1 (approve-then-act: a tap on the card posts a notice, then archives)" if acting_t1
            else "T0 (proposal cards only — nothing is archived at T0)")
    if policy.is_demoted():
        lane += "; DEMOTED — the nightly monitor found an archive it could not attribute to a tap"
    f = st.fold(now=now)
    ledger = st.read_ledger()
    if not f.ok or ledger is None:
        return (f"Dead-channel lane: {lane}. I couldn't read the proposal store or the archive "
                "ledger just now, so I can't give counts.")
    archived = sum(1 for r in ledger if r.get("event") == "outcome"
                   and str(r.get("outcome") or "").startswith("archived"))
    marked = kept = unknown = 0
    for p in f.proposals.values():
        for cid in p.rows_by_cid:
            s = p.state_of(cid)
            marked += s == st.AGREED
            kept += s == st.KEPT
            unknown += s == st.UNKNOWN
    last = f.latest()
    if last is None:
        card = "no proposal card yet"
    else:
        from .cards import _date  # noqa: PLC0415
        card = (f"last card {_date(last.created)} ({len(last.rows)} listed"
                + (", could not complete" if last.blind else "") + ")")
    return (f"Dead-channel lane: {lane}. {card[0].upper() + card[1:]}. Marked to archive {marked} · "
            f"kept {kept} · archived {archived} (ledger-backed) · outcome unknown {unknown}. "
            "Slack archives are reversible: anyone in the channel can unarchive it from the "
            "channel settings.")


def _live_cards(dm_channel: str, now: float) -> list:
    """EVERY live, delivered proposal in *dm_channel* (D-051 r2 integration#2): a newer
    partly delivered, blind or empty card supersedes nothing, so an older complete card
    stays live -- its buttons still act, and a typed reply in its thread is the card's."""
    from . import store as st  # noqa: PLC0415
    f = st.fold(now=now)
    return [p for pid, p in f.proposals.items()
            if p.delivered and f.is_live(pid, now)
            and (not dm_channel or p.dm_channel() == dm_channel)]


def live_card_message_ts(dm_channel: str, *, now: float | None = None) -> set[str]:
    """Every page message ts of every LIVE proposal delivered in *dm_channel* (a
    threaded follow-up counts only inside one of these threads)."""
    now = time.time() if now is None else float(now)
    try:
        cards_ = _live_cards(dm_channel, now)
    except Exception:  # noqa: BLE001
        return set()
    return {str(i.get("message_ts")) for p in cards_ for i in p.pages.values() if i.get("message_ts")}


def live_card_ts(dm_channel: str, *, now: float | None = None) -> float | None:
    """The newest card message time across the LIVE proposals delivered in *dm_channel*."""
    now = time.time() if now is None else float(now)
    try:
        cards_ = _live_cards(dm_channel, now)
    except Exception:  # noqa: BLE001
        return None
    if not cards_:
        return None
    stamps = []
    for p in cards_:
        for info in p.pages.values():
            try:
                stamps.append(float(info.get("message_ts") or 0))
            except (TypeError, ValueError):
                continue
    return max(stamps) if stamps else max(p.created for p in cards_)
