"""The weekly Learn drafting run (S2), plus the checklist-drift gate.

Order of operations, and why:

  1. checklist drift gate   -- the code mirrors a human-owned file; if the file
                               changed, the mirror may no longer be its mirror, so
                               nothing is staged until Harrison acknowledges it
  2. read the backlog       -- top QUEUED row; a DRAFTED row is skipped by
                               construction, which is what makes double-running
                               alongside the interim Cowork task harmless
  3. draft (LLM)            -- fail-closed
  4. preflight (code)       -- fail-closed; a trip means NOTHING is staged
  5. stage unpublished      -- read back (D-110)
  6. backlog row -> DRAFTED -- only after the read-back succeeded
  7. pipeline log + card    -- the record, then the tap

Every step reports what it did. A run that stages nothing says why; there is no
path on which this returns a cheerful summary for work it did not do.

`publish_article` is not imported here and must never be. The publish tap is the
only caller, and a test pins that this module's source does not name it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..connectors import shopify_client
from . import (drafting, news_lane, operating_files, preflight, publish_cards,
               refill)

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def state_path() -> Path:
    return Path(os.environ.get(
        "CORA_F3E_BLOG_STATE_PATH",
        str(_REPO_ROOT / "data" / "state" / "f3e-blog-pipeline-state.json"),
    ))


def _read_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


@dataclass
class RunReport:
    lines: list[str] = field(default_factory=list)
    staged_gid: str = ""
    staged_title: str = ""
    blocked_rails: list[str] = field(default_factory=list)
    proposed: int = 0
    drift_blocked: bool = False

    def say(self, line: str) -> None:
        self.lines.append(line)
        log.info("f3e_blog: %s", line)

    def render(self) -> str:
        return "\n".join(self.lines) if self.lines else "Nothing to report."


# ---------------------------------------------------------------------------
# Checklist drift gate
# ---------------------------------------------------------------------------


def check_checklist_drift(report: RunReport) -> tuple[bool, str]:
    """(ok_to_stage, fingerprint).

    The checklist file is the human-readable source of truth and this package's
    `preflight` is its code mirror. If a human edits a rail, the mirror is stale
    until somebody re-derives it, so staging stops rather than continuing to
    enforce yesterday's rules while claiming to enforce the file.

    First run adopts the current fingerprint instead of alerting: there is no
    "previous run" to have drifted from, and blocking run one on a change that
    never happened would be a false alarm.
    """
    try:
        text = operating_files.read_checklist()
    except Exception as exc:  # noqa: BLE001
        report.say("Could not read the claims checklist (%s). Nothing staged -- the "
                   "preflight mirrors that file and I will not stage without it."
                   % exc)
        return False, ""

    fingerprint = preflight.fingerprint_checklist(text)
    state = _read_state()
    acked = state.get("checklist_acked_fingerprint")

    if not acked:
        state["checklist_acked_fingerprint"] = fingerprint
        state["checklist_mirror_version"] = preflight.CHECKLIST_MIRROR_VERSION
        _write_state(state)
        report.say("Claims checklist fingerprint recorded (%s); first run, so "
                   "nothing to compare against." % fingerprint)
        return True, fingerprint

    if acked != fingerprint:
        report.drift_blocked = True
        report.say(
            "The claims checklist file CHANGED (%s -> %s) and my code mirror of it "
            "has not been re-checked, so I staged nothing. Someone needs to compare "
            "the file against the preflight rails, then acknowledge with: "
            "run_f3e_blog_pipeline.py --ack-checklist"
            % (acked, fingerprint)
        )
        return False, fingerprint

    return True, fingerprint


def ack_checklist() -> str:
    """Record the current checklist fingerprint as acknowledged."""
    text = operating_files.read_checklist()
    fingerprint = preflight.fingerprint_checklist(text)
    state = _read_state()
    state["checklist_acked_fingerprint"] = fingerprint
    state["checklist_mirror_version"] = preflight.CHECKLIST_MIRROR_VERSION
    _write_state(state)
    return fingerprint


# ---------------------------------------------------------------------------
# Published-article recheck (D-110 rule 2: read back on a LATER day)
# ---------------------------------------------------------------------------


def recheck_published(report: RunReport) -> list[str]:
    """Confirm articles this lane published are STILL live, a week later.

    The tap verifies the public page immediately. Rule 2 of D-110 asks for a
    read-back on a later day, because "it worked when I clicked" is the exact
    evidence that has been wrong here before.
    """
    problems: list[str] = []
    for rec in publish_cards._read_all().values():  # noqa: SLF001 -- same package
        if rec.get("state") != publish_cards.STATE_PUBLISHED:
            continue
        url = rec.get("public_url") or ""
        if not url:
            continue
        code, text = shopify_client.fetch_public_page(url)
        title = (rec.get("title") or "")[:40]
        if code == 200 and (not title or title in text):
            continue
        problems.append("%s (%s) -- %s" % (rec.get("title"), url,
                                          "HTTP %s" % code if code else "unreachable"))
    if problems:
        report.say("Previously published posts that did NOT read back clean: "
                   + "; ".join(problems))
    return problems


# ---------------------------------------------------------------------------
# Draft-and-check, with ONE bounded revision
# ---------------------------------------------------------------------------

# One retry, not a loop. Enough to fix a phrasing slip, bounded so a model that
# cannot satisfy a rail costs two calls a week rather than an unbounded spend.
MAX_DRAFT_ATTEMPTS = 2


def draft_until_clean(row, *, template: str, faq: str, lineup: str, lane: str,
                      report: RunReport):
    """(draft, preflight_result). draft is None if no usable draft came back.

    Why a revision pass exists at all: the claims rails are deliberately strict
    on a claims surface -- rail 2 trips on "clean" sharing a SENTENCE with the
    Energy line even when the word plainly attaches to Pure. That is the right
    direction for a fail-closed guard, but the first live draft tripped exactly
    that construction ("the full stack in F3 Energy or the clean-sweetened
    version in F3 Pure"), which a model writes naturally whenever an article
    links both lines. Left alone, the lane would jam on the same sentence shape
    every week.

    So the guard stays strict and the LOOP gets smarter: the tripped rail and the
    offending sentence go back to the model once. Loosening the rail instead would
    have traded a productivity problem for a claims hole.
    """
    revision = ""
    draft = None
    result = None
    for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
        draft = drafting.draft_article(
            row, template=template, faq=faq, lineup=lineup, revision_trips=revision)
        if not draft:
            report.say("Attempt %d: the draft did not come back usable, so nothing "
                       "was staged." % attempt)
            return None, None
        result = preflight.run_preflight(
            title=draft["title"], summary=draft["summary"],
            body_html=draft["body_html"], lane=lane,
        )
        if result.passed:
            if attempt > 1:
                report.say("Attempt %d passed the claims preflight after a "
                           "revision." % attempt)
            else:
                report.say(result.render())
            return draft, result
        report.say("Attempt %d: %s" % (attempt, result.render()))
        if attempt < MAX_DRAFT_ATTEMPTS:
            revision = "\n".join(t.render() for t in result.trips[:6])
            report.say("Sending the tripped rail(s) back for one revision.")
    return draft, result


# ---------------------------------------------------------------------------
# The weekly Learn run
# ---------------------------------------------------------------------------


def run_learn(*, dry_run: bool = False, client_factory=None) -> RunReport:
    report = RunReport()

    ok, _fingerprint = check_checklist_drift(report)
    if not ok:
        return report

    try:
        backlog_text, rows = operating_files.read_backlog()
    except Exception as exc:  # noqa: BLE001
        report.say("Could not read the editorial backlog (%s). Nothing staged." % exc)
        return report

    row = operating_files.next_queued(rows)
    if row is None:
        report.say("No QUEUED backlog row, so no Learn draft this week. "
                   "%d row(s) are PROPOSED and waiting to be queued."
                   % sum(1 for r in rows if r.status == operating_files.STATUS_PROPOSED))
        _maybe_refill(report, rows, dry_run=dry_run)
        return report

    report.say("Drafting backlog row %s: %s" % (row.number, row.title))

    try:
        template = operating_files.read_templates()
        lineup = operating_files.read_lineup()
    except Exception as exc:  # noqa: BLE001
        report.say("Could not read the templates or the canonical lineup (%s). "
                   "Nothing staged." % exc)
        return report

    faq = drafting.fetch_faq_text()
    if not faq:
        report.say("The live FAQ did not load, and it is the cleared source for "
                   "every product fact, so I did not draft anything.")
        return report

    draft, result = draft_until_clean(
        row, template=template, faq=faq, lineup=lineup, lane="learn", report=report)
    if draft is None:
        report.say("Row %s is still QUEUED and will be retried next run."
                   % row.number)
        return report
    if not result.passed:
        report.blocked_rails = result.tripped_rail_ids
        report.say("Row %s stays QUEUED. Nothing was written to Shopify."
                   % row.number)
        return report

    if dry_run:
        report.say("DRY RUN: would stage %r unpublished in /blogs/learn and card it."
                   % draft["title"])
        report.staged_title = draft["title"]
        _maybe_refill(report, rows, dry_run=True)
        return report

    try:
        article = shopify_client.create_article(
            blog_id=operating_files.BLOG_LEARN,
            title=draft["title"],
            body_html=draft["body_html"],
            summary=draft["summary"],
            tags=list(draft["tags"]) + ["Learn"],
        )
    except Exception as exc:  # noqa: BLE001
        report.say("Staging FAILED and nothing is live: %s. Row %s stays QUEUED."
                   % (exc, row.number))
        return report

    report.staged_gid = article.get("id", "")
    report.staged_title = article.get("title", "")
    report.say("Staged UNPUBLISHED and read back: %r -> %s"
               % (report.staged_title,
                  shopify_client.article_admin_url(report.staged_gid)))

    # The backlog row is only advanced AFTER a verified staging, so a failed
    # staging leaves the row QUEUED and the topic gets retried rather than lost.
    try:
        new_text = operating_files.set_row_status(
            backlog_text, row,
            operating_files.drafted_status_cell(report.staged_gid))
        operating_files.write_backlog(new_text)
        report.say("Backlog row %s marked DRAFTED." % row.number)
        backlog_text = new_text
        rows = operating_files.parse_backlog(new_text)
    except Exception as exc:  # noqa: BLE001
        # The article IS staged; say so plainly rather than reporting a failure.
        report.say("The draft is staged, but I could not update backlog row %s "
                   "(%s) -- it still reads QUEUED, so next week could draft the "
                   "same topic. Worth a look." % (row.number, exc))

    rec = publish_cards.record_for_article(
        article=article, lane="learn", excerpt=draft["summary"],
        backlog_row=row.number, rails_passed=len(result.rails_checked),
    )
    publish_cards.stage_card(rec, client_factory=client_factory)
    report.say("Publish card sent to Harrison.")

    operating_files.append_pipeline_log(_log_entry(report, row, article))
    _maybe_refill(report, rows, dry_run=dry_run)
    return report


def run_news(*, dry_run: bool = False, client_factory=None) -> RunReport:
    """The weekly News sweep, behind the same checklist-drift gate as Learn."""
    report = RunReport()
    ok, _fp = check_checklist_drift(report)
    if not ok:
        return report
    state = _read_state()
    state = news_lane.sweep(report, state, dry_run=dry_run,
                            client_factory=client_factory)
    if not dry_run:
        _write_state(state)
    return report


def run_weekly(*, dry_run: bool = False, client_factory=None) -> RunReport:
    """Both lanes plus the later-day read-back of what this lane published.

    Combined rather than two scheduled tasks: they share the drift gate, the FAQ
    fetch and the operating files, and one report is one thing to read.
    """
    report = run_learn(dry_run=dry_run, client_factory=client_factory)
    if report.drift_blocked:
        return report

    state = _read_state()
    state = news_lane.sweep(report, state, dry_run=dry_run,
                            client_factory=client_factory)
    if not dry_run:
        _write_state(state)

    recheck_published(report)
    return report


def _maybe_refill(report: RunReport, rows: list, *, dry_run: bool) -> None:
    queued = operating_files.count_queued(rows)
    if queued >= refill.REFILL_THRESHOLD:
        report.say("Backlog depth: %d QUEUED. No refill needed." % queued)
        return
    proposals = refill.build_proposals(
        exclude_titles={r.title for r in rows})
    if not proposals:
        report.say("Backlog is down to %d QUEUED, but I have no measured gaps to "
                   "propose from the latest visibility scan." % queued)
        return
    if dry_run:
        report.say("DRY RUN: would propose %d topic(s): %s"
                   % (len(proposals), "; ".join(p["title"][:60] for p in proposals)))
        report.proposed = len(proposals)
        return
    try:
        text, current = operating_files.read_backlog()
        operating_files.write_backlog(
            operating_files.append_proposed_rows(text, proposals))
        report.proposed = len(proposals)
        report.say("Backlog is down to %d QUEUED, so I appended %d PROPOSED "
                   "topic(s) from the latest visibility scan. They will not be "
                   "drafted until you flip one to QUEUED." % (queued, len(proposals)))
    except Exception as exc:  # noqa: BLE001
        report.say("Could not append refill proposals (%s)." % exc)


def _log_entry(report: RunReport, row, article: dict) -> str:
    admin = shopify_client.article_admin_url(article.get("id", ""))
    lines = [
        "## %s (Cora weekly Learn run)" % operating_files.az_today(),
        "- Learn draft staged UNPUBLISHED: %r (backlog row %s) -> %s -- read back OK "
        "(title match, isPublished:false)." % (article.get("title"), row.number, admin),
        "- Claims preflight passed %d mechanical rails (mirror v%s). Not machine "
        "checked: %s." % (len(preflight.RAILS_CHECKED),
                          preflight.CHECKLIST_MIRROR_VERSION,
                          ", ".join(preflight.UNENFORCED_RAILS)),
        "- Publish card DM'd to Harrison; nothing goes live until he taps Publish.",
    ]
    if report.proposed:
        lines.append("- Backlog refill: %d PROPOSED row(s) appended from the latest "
                     "AI-visibility scan gaps." % report.proposed)
    return "\n".join(lines)
