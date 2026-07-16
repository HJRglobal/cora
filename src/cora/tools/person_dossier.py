"""Per-person involvement dossier -- the multi-source pull / PHI-wall / synthesize /
write-back engine behind the `cora_person_dossier` tool and the weekly refresh.

North Star pillar 4 ("knows each individual"). On-demand: Harrison checks in on a
teammate ("what has Tommy been working on lately?") or a teammate self-profiles
("what have I been working on?"). The tool_dispatch handler runs the deterministic
founder-or-self ACCESS GATE (see resolve_access) and then calls build_dossier, which:

  1. Pulls work-involvement from each source FAIL-SOFT (one dead source never kills
     the dossier): Gmail (DWD per mailbox), Fireflies meetings (deduped), Asana tasks,
     HubSpot deals (stage GID -> label), Calendar (this/next week), Drive (v1: pending).
  2. PHI WALL -- mirrors drive_materializer._phi_wall: LEX-staff (and any LEX-domain
     mailbox) content is scrubbed PRE-synthesis (scrub_lex_phi + redact_cue_adjacent_names,
     staff roster preserved) so raw PHI never reaches the LLM; the synthesized OUTPUT is
     then re-checked and DROPPED if clinical / named-billing PHI survives. Non-LEX targets
     get the clinical backstop too (catches a cross-entity controller's incidental LEX
     activity, e.g. Justin's @lexingtonservices mailbox).
  3. Synthesize a tight "Recent involvements" section via Sonnet (multi-source composite --
     Haiku misnarrates degraded/empty sources, the plate-tool lesson). Fail-soft: on LLM
     error the raw (scrubbed) signals are returned and NOTHING is written back.
  4. Write-back (decision 10.2 = ON): replace the dossier's "Recent involvements" block,
     keep "Durable notes", normalize "auto-refreshed by Tag" -> "by Cora".

INVARIANTS (build spec section 2):
  - Founder-or-self only (resolve_access). Peer -> refused with NO target leak.
  - Peer-walled: a dossier is for Harrison + that one person; never a channel post.
  - Work-involvement only; never personal life.
  - Hard exclusions: Demi personal mailbox (structurally empty + flag), Alina Maricopa
    meetings (dropped at the Fireflies pull), Jason external (work-relevant/limited).
  - Decision-SUPPORT not decision-MAKER; source-opaque activity labels (no platform names).
  - D-011 untouched: a dossier is founder-oversight over company-owned data, not canon.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .. import drive_io, model_router, org_roles, phi_guard, slack_egress
from ..connectors import gmail_reader
from ..person_identity import PersonIdentity
from ..person_identity import resolve as _resolve_identity
from ..person_identity import resolve_by_name as _resolve_identity_by_name
from . import asana_client, calendar_client, hubspot_client

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Lexington email domains: a mailbox on one of these carries LEX content (so its
# Gmail/Calendar block is PHI-scrubbed pre-synthesis even for a non-lex_staff target,
# e.g. justin@lexingtonservices.com).
_LEX_MAIL_DOMAINS: tuple[str, ...] = (
    "lexingtonservices.com", "lexingtonbhs.com", "lexingtontherapyservices.com",
)

# Maricopa-class signal (Alina): drop any meeting whose attendees include a Maricopa
# County address or whose title names the probation/budget class (third-party PII /
# no-AI-bots, build spec section 5).
_MARICOPA_DOMAIN_RE = re.compile(r"@([\w.-]+\.)?maricopa\.gov$", re.IGNORECASE)
_MARICOPA_TITLE_RE = re.compile(r"\b(maricopa|probation|budget\s+class)\b", re.IGNORECASE)

# Lexington/Medicaid PROGRAM cue for the NON-LEX named-billing backstop now lives in
# phi_guard (is_lex_program_context; centralized 2026-07-05, W2-01). Ordinary commercial
# "client billing/invoice" is not PHI, so the non-LEX drop only fires when a care-program
# cue is ALSO present. One shared regex across all four PHI-wall consumers -> no drift.
# LBHS / 42-CFR-Part-2 hard signal -- if it survives into a LEX target's synthesis, drop.
_LBHS_SIGNAL_RE = re.compile(
    r"\b(LBHS|BHRF|COPA|Behavioral Health Services|Jared Harker)\b", re.IGNORECASE
)

_GMAIL_MAX = 18            # recent messages per mailbox
_FF_MAX = 12               # meetings synthesized
_ASANA_MAX = 25            # tasks listed (recent + upcoming)
_HUBSPOT_MAX = 25          # deals listed
_SONNET_MAX_TOKENS = 1500
_SYNTH_MODEL = model_router.MODEL_SONNET   # spec: force Sonnet for the composite


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class DossierResult:
    target_slug: str
    reply: str
    body: Optional[str] = None        # synthesized "Recent involvements" body, or None
    written: bool = False
    coverage: dict[str, str] = field(default_factory=dict)
    phi_dropped: bool = False


# ── paths ──────────────────────────────────────────────────────────────────────

def _people_dir() -> Path:
    return Path(
        os.environ.get("BRAIN_PEOPLE_DIR")
        or r"G:\My Drive\HJR-Founder-OS\_brain\people"
    )


# ── PHI helpers (mirror drive_materializer / fae) ───────────────────────────────

def _staff_names() -> set[str]:
    """Org-roles names to PRESERVE during scrubbing (client names redact, staff don't).
    Fail-soft to empty (empty = over-redact, the safe direction)."""
    try:
        return {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
    except Exception:  # noqa: BLE001
        return set()


def _scrub_lex_block(text: str) -> str:
    """Scrub LEX PHI from a source block, preserving staff names. FAIL-SAFE: on a
    scrubber error, drop the block entirely ("" -- the safe direction) rather than
    risk leaking unscrubbed PHI into the LLM."""
    if not text:
        return text
    try:
        staff = _staff_names()
        out = phi_guard.scrub_lex_phi(text, allowed_names=staff)
        return phi_guard.redact_cue_adjacent_names(out, allowed_names=staff)
    except Exception as exc:  # noqa: BLE001 -- fail-safe: drop rather than leak
        log.warning("person_dossier: LEX scrub error -- dropping block: %s", exc)
        return ""


def _is_lex_mailbox(email: str) -> bool:
    e = (email or "").strip().lower()
    return any(e.endswith("@" + d) for d in _LEX_MAIL_DOMAINS)


def _phi_wall(p: PersonIdentity, body: str) -> Optional[str]:
    """Return the safe synthesized body, or None to DROP it (don't write / don't surface).

    LEX-staff: the source text was already scrubbed pre-synthesis, so placeholders pass;
    this is the fail-safe -- DROP if an LBHS/Part-2 signal, clinical PHI, or named-billing
    PHI survived. Non-LEX: drop on clinical PHI always; drop on named-billing PHI only when
    a Lexington/Medicaid program cue is ALSO present (so ordinary commercial billing language
    is not over-dropped) -- this catches a cross-entity controller's incidental LEX content.
    """
    if not body:
        return body
    if p.lex_staff:
        if _LBHS_SIGNAL_RE.search(body):
            log.warning("person_dossier: %s dossier carries an LBHS/Part-2 signal -- DROPPED", p.slug)
            return None
        if phi_guard.is_clinical_phi(body) or phi_guard.is_lex_billing_status_phi(body):
            log.warning("person_dossier: %s dossier still trips PHI after scrub -- DROPPED", p.slug)
            return None
        return body
    if phi_guard.is_clinical_phi(body):
        log.warning("person_dossier: %s dossier contains clinical PHI (mis-tag?) -- DROPPED", p.slug)
        return None
    if phi_guard.is_lex_billing_status_phi(body) and phi_guard.is_lex_program_context(body):
        log.warning("person_dossier: %s dossier ties care-recipient billing to a LEX context -- DROPPED", p.slug)
        return None
    return body


def _maybe_scrub(p: PersonIdentity, text: str, *, source_email: str | None = None) -> str:
    """Scrub a source block when the TARGET is LEX-staff OR the source mailbox is a
    Lexington domain. Otherwise pass through (mojibake still repaired by the caller)."""
    if not text:
        return text
    if p.lex_staff or (source_email and _is_lex_mailbox(source_email)):
        return _scrub_lex_block(text)
    return text


# ── source pulls (each returns (status, text); module-level for test monkeypatch) ──

def _gmail_block(p: PersonIdentity, days: int) -> tuple[str, str]:
    """Recent work email across the target's DWD-eligible mailboxes (subjects +
    correspondents + dates -- metadata only). Skips entirely when the personal
    mailbox is excluded (Demi) or there is no mailbox to impersonate."""
    if p.exclude_personal_mailbox or not p.mailboxes:
        return ("skipped", "")
    query = f"newer_than:{max(1, days)}d -in:spam -in:trash -in:chats"
    blocks: list[str] = []
    any_ok = False
    for mbox in p.mailboxes:
        try:
            msgs = gmail_reader.get_inbox_summary(mbox, query=query, max_results=_GMAIL_MAX)
            any_ok = True
        except gmail_reader.GmailReaderError as exc:
            log.warning("person_dossier gmail %s: %s", mbox, exc)
            continue
        except Exception as exc:  # noqa: BLE001 -- one mailbox must not kill the block
            log.warning("person_dossier gmail unexpected %s: %s", mbox, exc)
            continue
        mbox_lines: list[str] = []
        for m in msgs:
            subj = (m.get("subject") or "(no subject)").strip()
            frm = re.sub(r"\s*<[^>]+>", "", (m.get("from") or "")).strip()
            to = re.sub(r"\s*<[^>]+>", "", (m.get("to") or "")).strip()
            corr = frm or to
            day = ""
            ts = m.get("date_ts")
            if ts:
                try:
                    day = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                except Exception:  # noqa: BLE001
                    day = ""
            mbox_lines.append(f"- {subj} (with {corr[:60]}{', ' + day if day else ''})")
        if not mbox_lines:
            continue
        # Scrub THIS mailbox's lines by ITS OWN domain. A LEX mailbox
        # (e.g. justin@lexingtonservices.com) is scrubbed even when the TARGET
        # is not lex_staff and the mailbox isn't first in the list -- the bug a
        # single scrub-by-mailboxes[0] would leak; a non-LEX mailbox is left
        # un-over-redacted. _maybe_scrub fail-closes to "" on a scrub error.
        block = _maybe_scrub(
            p, slack_egress.repair_mojibake("\n".join(mbox_lines)), source_email=mbox
        )
        if block.strip():
            blocks.append(block)
    if not any_ok:
        return ("error", "")
    if not blocks:
        return ("empty", "")
    return ("ok", "\n".join(blocks))


def _fireflies_block(p: PersonIdentity, days: int) -> tuple[str, str]:
    """Meetings the target attended in the window (deduped). LEX meetings are
    PHI-scrubbed; LBHS / clinical LEX meetings are dropped; for Alina the Maricopa
    class meetings are dropped (third-party PII)."""
    if not p.all_emails:
        return ("skipped", "")
    try:
        from . import meeting_actions as ma  # reuse the D-052 fetch + dedup helpers
        transcripts = ma._recent_transcripts(set(p.all_emails))
        transcripts = ma._dedup_meetings(transcripts)
    except Exception as exc:  # noqa: BLE001 -- fail-soft
        log.warning("person_dossier fireflies pull failed for %s: %s", p.slug, exc)
        return ("error", "")
    lines: list[str] = []
    for t in transcripts[: _FF_MAX]:
        title = (t.get("title") or "").strip()
        # Alina: drop the Maricopa probation/budget class meetings.
        if p.exclude_maricopa and _is_maricopa_meeting(t, title):
            continue
        try:
            meeting_entity, is_lex = ma._classify_meeting(t)
        except Exception:  # noqa: BLE001
            meeting_entity, is_lex = ("FNDR", False)
        # Treat a meeting as LEX-touching if the shared classifier says so OR any
        # attendee is on a Lexington domain -- so the gist is scrubbed even when a
        # generically-titled LEX meeting slips the title/name-based classifier (the
        # live pull has no prior KB filter, unlike drive_materializer).
        lex_touch = is_lex or _meeting_touches_lex_domain(t)
        if lex_touch:
            # Drop LBHS / 42-CFR-Part-2 (by signal OR @lexingtonbhs.com attendee) and
            # clinically-titled meetings outright.
            if (_meeting_touches_lbhs(t) or _LBHS_SIGNAL_RE.search(title)
                    or phi_guard.is_clinical_phi(title)):
                continue
        day = ma._meeting_date_str(t)
        summary = t.get("summary") or {}
        gist = (summary.get("short_summary") or summary.get("overview")
                or (summary.get("action_items") or "")[:300] or "").strip()
        title_s = _scrub_lex_block(title) if lex_touch else title
        gist_s = _scrub_lex_block(gist) if lex_touch else gist
        seg = f"- {title_s} ({day})"
        if gist_s:
            seg += f": {gist_s[:300]}"
        lines.append(seg)
    if not lines:
        return ("empty", "")
    return ("ok", _maybe_scrub(p, slack_egress.repair_mojibake("\n".join(lines))))


def _meeting_emails(transcript: dict) -> list[str]:
    out: list[str] = []
    for a in (transcript.get("meeting_attendees") or []):
        if isinstance(a, dict) and a.get("email"):
            out.append(str(a["email"]).strip().lower())
    out += [str(pp).strip().lower() for pp in (transcript.get("participants") or []) if isinstance(pp, str)]
    return [e for e in out if e]


def _meeting_touches_lex_domain(transcript: dict) -> bool:
    """True if any attendee/participant email is on a Lexington domain -- a scrub
    backstop independent of the title/name-based classifier."""
    return any(_is_lex_mailbox(e) for e in _meeting_emails(transcript))


def _meeting_touches_lbhs(transcript: dict) -> bool:
    """True if any attendee/participant is on the LBHS domain (42 CFR Part 2 -> drop)."""
    return any(e.endswith("@lexingtonbhs.com") for e in _meeting_emails(transcript))


def _is_maricopa_meeting(transcript: dict, title: str) -> bool:
    if _MARICOPA_TITLE_RE.search(title or ""):
        return True
    for a in (transcript.get("meeting_attendees") or []):
        if isinstance(a, dict) and _MARICOPA_DOMAIN_RE.search((a.get("email") or "")):
            return True
    for pp in (transcript.get("participants") or []):
        if isinstance(pp, str) and _MARICOPA_DOMAIN_RE.search(pp):
            return True
    return False


def _asana_block(p: PersonIdentity) -> tuple[str, str]:
    """Open Asana tasks assigned to the target (recent + upcoming), with project names."""
    if not p.asana_gid:
        return ("skipped", "")
    try:
        tasks = asana_client.get_user_tasks(p.asana_gid)
    except asana_client.AsanaClientError as exc:
        log.warning("person_dossier asana %s: %s", p.slug, exc)
        return ("error", "")
    except Exception as exc:  # noqa: BLE001
        log.warning("person_dossier asana unexpected %s: %s", p.slug, exc)
        return ("error", "")
    if not tasks:
        return ("empty", "")
    shown = asana_client.sort_tasks_due_first(tasks)[: _ASANA_MAX]
    lines: list[str] = []
    for t in shown:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        proj = ""
        for m in (t.get("memberships") or []):
            pj = ((m.get("project") or {}).get("name") or "").strip()
            if pj:
                proj = pj
                break
        due = (t.get("due_on") or "").strip()
        seg = f"- {name}"
        meta = "; ".join(x for x in [f"project: {proj}" if proj else "", f"due {due}" if due else ""] if x)
        if meta:
            seg += f" ({meta})"
        lines.append(seg)
    if not lines:
        return ("empty", "")
    return ("ok", _maybe_scrub(p, slack_egress.repair_mojibake("\n".join(lines))))


def _hubspot_block(p: PersonIdentity) -> tuple[str, str]:
    """Open deals owned by the target, stage GIDs resolved to labels. None owner -> skip."""
    if not p.hubspot_owner_id:
        return ("skipped", "")
    try:
        from .tool_dispatch import HUBSPOT_PIPELINE_BY_ENTITY  # local import: avoid cycle
        pipeline_id = HUBSPOT_PIPELINE_BY_ENTITY.get((p.entity or "").upper())
    except Exception:  # noqa: BLE001
        pipeline_id = None
    try:
        deals = hubspot_client.get_owner_deals(p.hubspot_owner_id, pipeline_id=pipeline_id)
    except hubspot_client.HubSpotClientError as exc:
        log.warning("person_dossier hubspot %s: %s", p.slug, exc)
        return ("error", "")
    except Exception as exc:  # noqa: BLE001
        log.warning("person_dossier hubspot unexpected %s: %s", p.slug, exc)
        return ("error", "")
    if not deals:
        return ("empty", "")
    # format_deals_for_llm resolves stage_id -> label via the warmed _STAGE_NAME_CACHE.
    text = hubspot_client.format_deals_for_llm(deals[: _HUBSPOT_MAX])
    if not isinstance(text, str) or not text.strip():
        return ("empty", "")
    return ("ok", slack_egress.repair_mojibake(text))


def _calendar_block(p: PersonIdentity) -> tuple[str, str]:
    """This-week + next-week calendar events for the target (forward-looking;
    get_user_events is forward-only -- Fireflies covers past meetings).

    Impersonates a DWD-ELIGIBLE mailbox (primary_email when it's DWD, else the first
    mailbox). A target with only a non-DWD address (e.g. Jason's personal Gmail) or
    no mailbox is skipped -- never a failing impersonation call."""
    if p.exclude_personal_mailbox or not p.mailboxes:
        return ("skipped", "")
    cal_email = p.primary_email if p.primary_email in set(p.mailboxes) else p.mailboxes[0]
    parts: list[str] = []
    any_ok = False
    for when in ("this_week", "next_week"):
        try:
            events, label = calendar_client.get_user_events(cal_email, when=when)
            any_ok = True
        except calendar_client.CalendarClientError as exc:
            log.warning("person_dossier calendar %s %s: %s", p.slug, when, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("person_dossier calendar unexpected %s %s: %s", p.slug, when, exc)
            continue
        rendered = calendar_client.format_events_for_llm(events, label)
        if rendered and rendered.strip():
            parts.append(rendered.strip())
    if not any_ok:
        return ("error", "")
    if not parts:
        return ("empty", "")
    text = "\n".join(parts)
    return ("ok", _maybe_scrub(p, slack_egress.repair_mojibake(text),
                               source_email=cal_email))


def _drive_block(p: PersonIdentity) -> tuple[str, str]:
    """Recent Drive files the target modified. DEFERRED to a follow-up (spec 4.6:
    optional in v1 -- mark pending). Honestly labeled so the dossier never reads as
    complete when Drive wasn't pulled."""
    return ("pending", "")


# ── source assembly ─────────────────────────────────────────────────────────────

# (label, block-function-name, takes_days). The function is resolved by NAME from
# module globals at call time so tests can patch.object(person_dossier, "_gmail_block").
_SOURCE_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("Email", "_gmail_block", True),
    ("Meetings", "_fireflies_block", True),
    ("Tasks", "_asana_block", False),
    ("Deals", "_hubspot_block", False),
    ("Calendar", "_calendar_block", False),
    ("Docs", "_drive_block", False),
)


def _run_source(label: str, fname: str, takes_days: bool, p: PersonIdentity, days: int) -> tuple[str, str]:
    """Run one source block fail-soft. Resolves the fn by NAME at call time so tests'
    monkeypatches are picked up. Returns (status, text)."""
    fn = globals().get(fname)
    try:
        return fn(p, days) if takes_days else fn(p)
    except Exception as exc:  # noqa: BLE001 -- a source raising must never kill the dossier
        log.warning("person_dossier: source %s crashed for %s: %s", label, p.slug, exc)
        return ("error", "")


def _assemble_sources(p: PersonIdentity, days: int) -> tuple[str, dict[str, str]]:
    """Run every source CONCURRENTLY (independent I/O) + fail-soft. Returns
    (labeled_source_text, coverage map), blocks in canonical _SOURCE_SPECS order.

    Parallel because the sequential pull (~38s for a 5-connector target) blew the
    25s dispatch tool-timeout in the 2026-06-30 live smoke; the slowest single source
    (multi-mailbox Gmail) now overlaps the others instead of summing. The dispatch
    wrapper already runs the whole tool in a worker thread, so this nested pool just
    fans the I/O out within that thread."""
    import concurrent.futures as _cf

    results: dict[str, tuple[str, str]] = {}
    with _cf.ThreadPoolExecutor(max_workers=len(_SOURCE_SPECS)) as ex:
        futs = {
            ex.submit(_run_source, label, fname, takes_days, p, days): label
            for label, fname, takes_days in _SOURCE_SPECS
        }
        for fut in _cf.as_completed(futs):
            label = futs[fut]
            try:
                results[label] = fut.result()
            except Exception as exc:  # noqa: BLE001 -- belt; _run_source already catches
                log.warning("person_dossier: source %s future failed for %s: %s", label, p.slug, exc)
                results[label] = ("error", "")

    blocks: list[str] = []
    coverage: dict[str, str] = {}
    for label, _fname, _td in _SOURCE_SPECS:  # canonical order, not completion order
        status, text = results.get(label, ("error", ""))
        coverage[label] = status
        if status == "ok" and text.strip():
            blocks.append(f"### {label}\n{text.strip()}")
    return ("\n\n".join(blocks), coverage)


def _coverage_footer(coverage: dict[str, str]) -> str:
    """Deterministic honesty line: what was actually pulled (doctrine -- label partials)."""
    mark = {"ok": "✓", "empty": "none", "skipped": "n/a", "error": "unavailable", "pending": "pending"}
    return "_Sources: " + ", ".join(
        f"{label} {mark.get(coverage.get(label, ''), coverage.get(label, ''))}"
        for label, _, _ in _SOURCE_SPECS
    ) + "._"


# ── synthesis (Sonnet, fail-soft) ───────────────────────────────────────────────

_SYNTH_PROMPT = """You are writing the "Recent involvements" section of {name}'s internal work-involvement dossier (role: {role}, business unit: {entity}). This is for the founder (Harrison) and {name} only -- work activity, never personal life.

The signals below were pulled from the last {days} days across their email, meetings, tasks, deals, and calendar (labeled by activity type, not platform).

Write GitHub-flavored markdown in EXACTLY this shape:

Headline: one sentence -- the through-line of what {name} has been driving.

- One bullet per source/theme that has signal. Lead with the work, include concrete amounts/dates/counterparties where present. Keep each bullet tight.

Themes: a one-line list of the 2-4 threads running through the period.

Rules:
- Distill HARD. Signal only. No raw email bodies, no transcripts, no long quotes.
- Be concrete (staff/vendor names, amounts, dates) for ordinary business activity.
- Do NOT invent anything not in the signals. If a source is empty, simply omit it -- do not speculate.
- Source-opaque: refer to activity types ("email", "meetings", "deals"), never platform/system names.
- PHI (NON-NEGOTIABLE -- this unit may touch Lexington, a care provider): NEVER include any care-recipient's name (client/patient/member), diagnosis, medication, or their billing/authorization/eligibility/coverage status. Staff and vendor names are fine. When in doubt, leave it out.

Signals:
---
{signals}
"""


def _get_client(client: Any = None) -> Any:
    if client is not None:
        return client
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("person_dossier: ANTHROPIC_API_KEY not set -- cannot synthesize")
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("person_dossier: anthropic client init failed: %s", exc)
        return None


def _synthesize(p: PersonIdentity, signals: str, days: int, client: Any) -> Optional[str]:
    """Sonnet synthesis. Returns the markdown body, or None on LLM failure (fail-soft)."""
    if not signals.strip():
        return None
    prompt = _SYNTH_PROMPT.format(
        name=p.name, role=p.role or "team member", entity=p.entity or "the company",
        days=days, signals=signals,
    )
    try:
        resp = client.messages.create(
            model=_SYNTH_MODEL,
            max_tokens=_SONNET_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001 -- fail-soft (do NOT write back on failure)
        log.warning("person_dossier: synthesis failed for %s: %s", p.slug, exc)
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    return raw or None


# ── write-back (Drive _brain/people/{slug}.md) ──────────────────────────────────

_RECENT_HEADER_RE = re.compile(r"^##\s+Recent involvements.*$", re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^##\s+", re.MULTILINE)
_CANONICAL_RECENT_HEADER = "## Recent involvements (auto-refreshed by Cora)"
_DEFAULT_PREAMBLE = (
    "_Populated when Harrison checks in on {name}, or by the weekly involvement "
    "refresh -- pulls their email, meetings, tasks, deals, calendar, and docs._"
)


def _render_recent_section(name: str, as_of: str, body: str, existing_preamble: str | None) -> str:
    """The replacement "Recent involvements" section: normalized header, the
    preamble (existing if present), and the synthesized body stamped with the date."""
    preamble = (existing_preamble or _DEFAULT_PREAMBLE.format(name=name)).strip()
    return (
        f"{_CANONICAL_RECENT_HEADER}\n"
        f"{preamble}\n\n"
        f"**As of {as_of}**\n\n"
        f"{body.strip()}\n"
    )


def write_back(p: PersonIdentity, body: str, *, as_of: str | None = None) -> bool:
    """Replace the dossier's "Recent involvements" section, preserving everything
    before it and "## Durable notes" onward; normalize "by Tag" -> "by Cora".

    Only writes an EXISTING dossier file (the 22 are seeded); a missing file is
    skipped + logged rather than silently created. Atomic temp + replace. Returns
    True on a successful write.
    """
    if not body or not body.strip():
        return False
    path = _people_dir() / p.dossier_filename
    # The dossier files live on the G: mount. A transient unmount must never hang this
    # tool (it runs in the request path); route every G: touch through drive_io and
    # skip the write-back (returning the synthesized reply anyway) if the mount is gone.
    # Short retry so the tool never blocks the user on a flaky mount.
    try:
        present = drive_io.exists(path, timeout=5.0, retry_seconds=2.0)
    except drive_io.DriveUnavailable:
        log.warning("person_dossier: G: mount unavailable -- skipping dossier write-back for %s", p.slug)
        return False
    if not present:
        log.warning("person_dossier: no dossier file at %s -- skipping write-back", path)
        return False
    as_of = as_of or datetime.now().date().isoformat()
    try:
        original = drive_io.read_text(path, timeout=5.0, retry_seconds=2.0)
    except drive_io.DriveUnavailable:
        log.warning("person_dossier: G: mount unavailable reading %s -- skipping write-back", path)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("person_dossier: could not read %s: %s", path, exc)
        return False

    hdr = _RECENT_HEADER_RE.search(original)
    if not hdr:
        log.warning("person_dossier: %s has no 'Recent involvements' header -- not writing", path)
        return False
    # Preserve the existing italic preamble line (the line after the header), if present.
    after_hdr = original[hdr.end():]
    preamble_m = re.match(r"\s*\n(_[^\n]*_)\s*", after_hdr)
    existing_preamble = preamble_m.group(1) if preamble_m else None

    # The section runs from the header to the next "## " heading (e.g. Durable notes).
    nxt = _NEXT_H2_RE.search(original, hdr.end())
    tail = original[nxt.start():] if nxt else ""
    new_section = _render_recent_section(p.name, as_of, body, existing_preamble)
    new_doc = original[: hdr.start()] + new_section + ("\n" + tail if tail else "")
    # Belt: normalize any residual "by Tag" anywhere (older seeds).
    new_doc = new_doc.replace("auto-refreshed by Tag", "auto-refreshed by Cora")

    try:
        drive_io.write_text_atomic(path, new_doc, timeout=5.0, retry_seconds=2.0)
    except drive_io.DriveUnavailable:
        log.warning("person_dossier: G: mount unavailable writing %s -- write-back skipped", path)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("person_dossier: write-back failed for %s: %s", path, exc)
        return False
    log.info("person_dossier: wrote involvements for %s (%s)", p.slug, as_of)
    return True


# ── access gate (founder-or-self; NO target leak) ───────────────────────────────

def resolve_access(
    asker_slack_id: str, person_arg: str, founder_slack_id: str | None
) -> tuple[Optional[PersonIdentity], Optional[str]]:
    """Deterministic founder-or-self gate. Returns (target_identity, refusal).

    - No `person` arg -> SELF profile (target = asker). Unknown asker -> graceful refuse.
    - `person` arg + asker is NOT the founder -> REFUSE as a peer-surveillance request,
      WITHOUT resolving the name (no existence/metadata leak about the target).
    - `person` arg + asker IS the founder -> resolve the named teammate.
    """
    person_arg = (person_arg or "").strip()
    if not person_arg:
        target = _resolve_identity(asker_slack_id)
        if target is None:
            return None, (
                "I can only pull your own work involvement, and I don't have you in "
                "my people map yet -- ask Harrison to add you."
            )
        return target, None

    # A teammate is named.
    if not founder_slack_id or asker_slack_id != founder_slack_id:
        # NO name resolution here -- never confirm/deny the target exists to a peer.
        return None, (
            "I can only pull your own work involvement for you. Checking in on a "
            "teammate's involvement is Harrison's call -- ask him if you need it."
        )
    target = _resolve_identity_by_name(person_arg)
    if target is None:
        return None, (
            f"I couldn't match \"{person_arg}\" to anyone in the people map. Tell me "
            "the name as it appears in the roster, or ask Harrison to add them."
        )
    return target, None


# ── build (the orchestrator) ────────────────────────────────────────────────────

def build_dossier(
    p: PersonIdentity,
    *,
    days: int = 14,
    client: Any = None,
    write_back_enabled: bool = True,
    dry_run: bool = False,
) -> DossierResult:
    """Pull -> scrub -> synthesize -> (write-back) for one person. Never raises.

    write_back_enabled gates the Drive write (decision 10.2 = ON for both the founder
    check-in and the weekly refresh). dry_run synthesizes but writes nothing.
    """
    days = max(1, min(int(days or 14), 30))

    # External consultant: work-relevant + limited. We still pull what's reachable
    # (Fireflies/Asana/Calendar if mapped), but most internal keys are absent, so the
    # dossier naturally narrows to their engagement scope.
    signals, coverage = _assemble_sources(p, days)
    footer = _coverage_footer(coverage)

    if not signals.strip():
        reply = (
            f"I don't have any reachable work-involvement signals for {p.name} in the "
            f"last {days} days right now.\n\n{footer}"
        )
        return DossierResult(p.slug, reply, body=None, written=False, coverage=coverage)

    llm = _get_client(client)
    if llm is None:
        # No LLM: return the raw (already-scrubbed) signals, write nothing.
        reply = (
            f"Couldn't synthesize {p.name}'s involvement right now (model unavailable). "
            f"Raw signals from the last {days} days:\n\n{signals}\n\n{footer}"
        )
        return DossierResult(p.slug, reply, body=None, written=False, coverage=coverage)

    body = _synthesize(p, signals, days, llm)
    if not body:
        reply = (
            f"Couldn't synthesize {p.name}'s involvement right now -- please try again "
            f"shortly.\n\n{footer}"
        )
        return DossierResult(p.slug, reply, body=None, written=False, coverage=coverage)

    safe = _phi_wall(p, body)
    if safe is None:
        # PHI survived synthesis -> never write, never surface the body.
        reply = (
            f"I pulled {p.name}'s recent work activity but it touched protected "
            f"Lexington information I can't surface, so I'm not showing or saving an "
            f"involvement summary for this period.\n\n{footer}"
        )
        return DossierResult(p.slug, reply, body=None, written=False, coverage=coverage,
                             phi_dropped=True)

    written = False
    if write_back_enabled and not dry_run:
        written = write_back(p, safe)

    reply = (
        f"*{p.name} -- recent involvement (last {days} days)*\n\n{safe.strip()}\n\n{footer}"
    )
    if written:
        reply += f"\n_Saved to {p.name}'s dossier._"
    return DossierResult(p.slug, reply, body=safe, written=written, coverage=coverage)
