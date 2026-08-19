"""Diarization-collapse canary for Fireflies transcripts (cq-e63feff3a0bf).

THE INCIDENT: a 77-minute multi-party meeting was ingested with 100% of its
sentences labelled to a single speaker, and nothing noticed. Everything
downstream trusts those labels -- the action extractor assigns owners from them,
`person_dossier` quotes people, retrieval hands the model "[Name] said X" -- so a
collapsed transcript does not read as missing data. It reads as one person having
said everything in the room, confidently, forever.

The signal is cheap and needs no second API call: Fireflies already returns
`sentences[].speaker_name`, and the ingest formatter already writes them as
`[Speaker] text` lines. So the same check runs two ways:

  * at INGEST, over the live transcript (`assess`), tagging the chunks; and
  * RETROSPECTIVELY, over stored KB content (`assess_rendered`), because the
    rendered `[Speaker]` prefixes carry the same fact -- no re-fetch, no
    re-embed.

WHAT IS DELIBERATELY *NOT* FLAGGED:
  * a genuinely single-speaker recording (a solo memo, a one-sided call) -- hence
    the multi-party precondition, taken from the attendee/participant lists
    rather than from the labels being checked;
  * a short exchange, where one person doing most of the talking is normal --
    hence the sentence floor.
Both exist so the flag stays worth reading. A canary that cries on solo memos
gets ignored, and an ignored canary is the state we are already in.

The flag is ADVISORY: it labels attribution as unreliable. It never drops a
transcript (the words were still said in that meeting) and never blocks ingest.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: Share of sentences on one speaker at or above which diarization is considered
#: collapsed. 0.95 rather than 1.0 because a partial collapse (a handful of
#: correctly-split lines in an otherwise single-labelled hour) is the same defect.
COLLAPSE_SHARE = 0.95

#: Below this many labelled sentences the share is not evidence of anything.
MIN_SENTENCES = 40

#: Speaker labels that carry no identity. The ingest formatter substitutes
#: "Speaker" for an empty speaker_name, so an entirely unlabelled transcript
#: renders as 100% "Speaker" -- the collapse signature, and a real one.
_PLACEHOLDER_SPEAKERS = {"", "speaker", "unknown", "unknown speaker", "n/a", "none"}

#: `[Name]` as the ingest formatter writes it, matched ANYWHERE rather than
#: line-anchored. Measured against the live KB: the chunker collapses the
#: formatter's newlines, so a stored chunk holds `[A] .. [A] .. [B] ..` all on
#: ONE line. A line-anchored pattern therefore saw exactly one label per chunk
#: and computed the share over first-labels only -- the right verdict for the
#: wrong reason. The name class excludes both brackets and newlines, and the
#: sentence body is taken by slicing to the next match rather than by a second
#: quantifier, so there is no backtracking surface at all (the _DRIVE_PATH_RE
#: ReDoS class).
#:
#: PRECEDED BY A SENTENCE BOUNDARY (D-051 lens-2, measured). Any `[...]` counted as
#: a speaker, so three mid-sentence asides -- `[inaudible]`, `[crosstalk]`,
#: `[laughter]` -- turned a genuine 100%-collapsed meeting into
#: "4 speakers, top share 93% -- healthy". A canary that a transcription artifact
#: can switch off is worse than none. Verified against the live corpus: of 542,474
#: real speaker labels the preceding non-space character is `.` (482,559), `?`
#: (41,317), start-of-text (17,106), newline (1,482) or `:` (10) -- 100% still
#: match, while an aside after "…point 7 " no longer does.
_RENDERED_TOKEN_RE = re.compile(r"(?:^|(?<=[.?!:\n]))\s{0,4}\[([^\[\]\n]{1,80})\]")

#: Structural labels the formatter itself writes, which are not speakers. Live
#: count: "Fireflies Meeting" appears exactly 735 times across 735 stored
#: meetings -- one header line each -- and it is the ONLY such label in the
#: corpus (278 distinct labels scanned; every other one is a speaker name, a
#: room, a phone number or a "Speaker N" placeholder).
_STRUCTURAL_LABELS = {"fireflies meeting"}

#: Fireflies' own placeholder for an unnamed voice.
_PLACEHOLDER_NUMBERED_RE = re.compile(r"^speaker\s*\d+$", re.IGNORECASE)


@dataclass
class DiarizationHealth:
    """What the canary saw. `collapsed` is the only actionable field."""

    sentences: int = 0
    speakers: int = 0
    top_speaker_share: float = 0.0
    expected_parties: int = 0
    collapsed: bool = False
    reason: str = ""
    speaker_counts: dict[str, int] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        """The subset that rides into chunk metadata.

        NO SPEAKER NAMES. `speaker_counts` and `reason` both carry them, and
        metadata is rendered on surfaces content is not -- dashboards, digests,
        debug dumps -- so a name that arrived as meeting content would be
        travelling on a channel that never PHI-scrubbed or entity-scoped it. The
        prose reason lives in the flag ledger instead, which is Harrison-only.

        The measurements ride along even when healthy: they are how a threshold
        change gets evaluated later without a re-ingest.
        """
        out: dict[str, Any] = {
            "diarization_speakers": self.speakers,
            "diarization_top_share": round(self.top_speaker_share, 3),
            "diarization_sentences": self.sentences,
        }
        if self.collapsed:
            # The one key retrieval and the digest both act on.
            out["attribution_unreliable"] = True
        return out


def _tally(labels: list[str]) -> Counter:
    counts: Counter = Counter()
    for raw in labels:
        name = str(raw or "").strip()
        counts[name or "Speaker"] += 1
    return counts


def _judge(counts: Counter, expected_parties: int) -> DiarizationHealth:
    total = sum(counts.values())
    health = DiarizationHealth(
        sentences=total,
        speakers=len(counts),
        expected_parties=expected_parties,
        speaker_counts=dict(counts),
    )
    if not total:
        health.reason = "no sentences"
        return health

    top_name, top_n = counts.most_common(1)[0]
    health.top_speaker_share = top_n / total

    # Order matters only for the reason string; every clause is a hard
    # precondition for flagging.
    if total < MIN_SENTENCES:
        health.reason = f"only {total} sentences -- below the {MIN_SENTENCES} floor"
        return health
    if expected_parties < 2:
        health.reason = (
            f"{expected_parties} known part{'y' if expected_parties == 1 else 'ies'} "
            "-- single-speaker is expected"
        )
        return health
    if health.top_speaker_share < COLLAPSE_SHARE:
        health.reason = (
            f"{health.speakers} speakers, top share "
            f"{health.top_speaker_share:.0%} -- healthy"
        )
        return health

    health.collapsed = True
    top_clean = str(top_name).strip()
    placeholder = (top_clean.lower() in _PLACEHOLDER_SPEAKERS
                   or bool(_PLACEHOLDER_NUMBERED_RE.match(top_clean)))
    who = "unlabelled" if placeholder else f"one speaker ({top_name})"
    health.reason = (
        f"{health.top_speaker_share:.0%} of {total} sentences attributed to {who} "
        f"across a meeting with {expected_parties} known parties"
    )
    return health


#: Addresses that are software, not people. The notetaker bot rides in BOTH the
#: attendee and participant lists on live transcripts, so counting it made a
#: genuine one-human recording look like a 2-party meeting -- and the multi-party
#: precondition is the whole reason a solo memo is "deliberately NOT flagged"
#: (D-051 lens-2). Measured current exposure: zero flagged meetings depend on it,
#: which is why this is a correctness fix rather than a bug report.
_NON_HUMAN_ADDRESSES = ("@fireflies.ai", "notetaker@", "noreply@", "no-reply@")


def _is_human_address(value: str) -> bool:
    low = value.lower()
    return bool(low) and not any(marker in low for marker in _NON_HUMAN_ADDRESSES)


def expected_parties(transcript: dict[str, Any]) -> int:
    """How many PEOPLE the meeting RECORD says were there.

    Read from the attendee/participant lists -- never from the speaker labels,
    which are the thing under test. Emails are lower-cased and de-duplicated
    because Fireflies returns the same person under both keys, and the notetaker
    bot is excluded because it is not a party to the conversation.
    """
    seen: set[str] = set()
    for attendee in transcript.get("meeting_attendees") or []:
        if isinstance(attendee, dict):
            email = str(attendee.get("email") or "").strip().lower()
            if email and _is_human_address(email):
                seen.add(email)
    participants = transcript.get("participants") or []
    # A STRING here would iterate per character and report ~10 parties from one
    # address. Live data is always a list, and fireflies_connector's own
    # participant handling already guards this way -- so this is parity, not
    # speculation (D-051 lens-4).
    if isinstance(participants, str):
        participants = [participants]
    for participant in participants:
        value = str(participant or "").strip().lower()
        if value and _is_human_address(value):
            seen.add(value)
    return len(seen)


def assess(transcript: dict[str, Any]) -> DiarizationHealth:
    """Canary over a LIVE Fireflies transcript dict."""
    sentences = transcript.get("sentences") or []
    labels = [
        s.get("speaker_name")
        for s in sentences
        if isinstance(s, dict) and str(s.get("text") or "").strip()
    ]
    return _judge(_tally(labels), expected_parties(transcript))


def assess_rendered(content: str, expected: int) -> DiarizationHealth:
    """Canary over ALREADY-STORED content -- the `[Speaker] text` lines the ingest
    formatter wrote. Lets a retro sweep run entirely offline: no Fireflies call,
    no re-embed, and it reads exactly what retrieval will hand the model.

    `expected` comes from the caller (stored attendee metadata), for the same
    reason as above: never infer the party count from the labels under test.
    """
    text = str(content or "")
    tokens = list(_RENDERED_TOKEN_RE.finditer(text))
    labels: list[str] = []
    for i, match in enumerate(tokens):
        end = tokens[i + 1].start() if i + 1 < len(tokens) else len(text)
        body = text[match.end():end].strip()
        name = match.group(1).strip()
        if not body or name.lower() in _STRUCTURAL_LABELS:
            continue
        labels.append(name)
    return _judge(_tally(labels), expected)
