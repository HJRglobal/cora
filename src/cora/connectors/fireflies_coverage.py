"""Fireflies DWD coverage classification — pure logic, no network in the classifier.

Goal: verify that every DWD user's meetings are actually being captured by Fireflies,
not just Harrison's (founder TOM: 16 invites pending since 2026-06-03).

Seat scope (2026-07-01): after the 6/22 Enterprise right-size the monitored
population is the 10 Fireflies seat-holders, marked `fireflies_seat: true` in
monitored-email-accounts.yaml — NOT the full DWD roster (removed employees stay
in that file for Gmail/Drive ingestion and must never be Fireflies-nudged).

Three statuses (MEMBER_NO_CALENDAR was dropped at CP-1: `integrations` provably does
NOT carry calendar state — Harrison has 566 transcripts yet no calendar in his
integrations list — so it would be undetectable / dead code):

    COVERED              — workspace member with >0 transcripts (recordings happening).
    MEMBER_NO_RECORDINGS — workspace member but no recordings (calendar likely not
                           connected, or just hasn't met). Nudge: "connect your calendar".
    NOT_A_MEMBER         — email/alias not in the Fireflies members list at all (invite
                           never accepted). Nudge: "accept the invite email".

CORRECTNESS LOCK (CP-1, from live evidence): membership is authoritative. The per-host
organizer probe reflects "someone with a connected calendar attended this meeting", NOT
"this person's calendar is connected" (Larry's 5/5 meeting was captured only because
Harrison was in the room, yet Larry is not a member). So the optional recency cross-check
ONLY refines people who are ALREADY members; it must NEVER promote a NOT_A_MEMBER.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ACCOUNTS_YAML = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"

# Status constants
COVERED = "COVERED"
MEMBER_NO_RECORDINGS = "MEMBER_NO_RECORDINGS"
NOT_A_MEMBER = "NOT_A_MEMBER"

# Shared-inbox local-parts (before @) that are dropped — these are not humans.
_SHARED_LOCALPARTS = frozenset({"payables", "receipts", "service"})

_ADMIN_LINK = "https://app.fireflies.ai/settings/team/members-and-groups"


# ── data shapes ───────────────────────────────────────────────────────────────


@dataclass
class DwdHuman:
    """One distinct human in the DWD roster (cross-domain aliases collapsed)."""

    name: str
    primary_email: str
    known_aliases: list[str] = field(default_factory=list)
    slack_user_id: str | None = None
    entity_default: str | None = None

    @property
    def all_emails(self) -> set[str]:
        return {self.primary_email, *self.known_aliases}


@dataclass
class PersonResult:
    human: DwdHuman
    status: str
    num_transcripts: int = 0
    is_member: bool = False
    # member with transcripts but no host meeting inside the recency window
    has_older_recordings: bool = False


@dataclass
class CoverageReport:
    results: list[PersonResult]
    enumerate_failed: bool = False

    @property
    def covered(self) -> list[PersonResult]:
        return [r for r in self.results if r.status == COVERED]

    @property
    def member_no_recordings(self) -> list[PersonResult]:
        return [r for r in self.results if r.status == MEMBER_NO_RECORDINGS]

    @property
    def not_a_member(self) -> list[PersonResult]:
        return [r for r in self.results if r.status == NOT_A_MEMBER]

    @property
    def summary_line(self) -> str:
        return (
            f"{len(self.covered)} covered, "
            f"{len(self.member_no_recordings)} member/no-recordings, "
            f"{len(self.not_a_member)} not-a-member "
            f"(of {len(self.results)} DWD users)"
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def _localpart(email: str) -> str:
    return (email or "").split("@", 1)[0].strip().lower()


def _norm_name(name: str) -> str:
    """Lowercase, strip a trailing parenthetical suffix, collapse whitespace.

    "Alex Cordova (UFL legacy)" -> "alex cordova". Used as the alias-collapse
    fallback when two cross-domain entries share neither slack_user_id nor an
    explicit alias link.
    """
    n = re.sub(r"\(.*?\)", "", name or "")
    return re.sub(r"\s+", " ", n).strip().lower()


def _is_shared_inbox(acct: dict) -> bool:
    email = (acct.get("email") or "").lower()
    if _localpart(email) in _SHARED_LOCALPARTS:
        return True
    if not acct.get("slack_user_id") and "inbox" in (acct.get("name") or "").lower():
        return True
    return False


def _account_emails(acct: dict) -> set[str]:
    emails = {(acct.get("email") or "").strip().lower()}
    for alias in acct.get("known_aliases") or []:
        if alias:
            emails.add(str(alias).strip().lower())
    emails.discard("")
    return emails


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def load_dwd_humans(path: Path | str | None = None) -> list[DwdHuman]:
    """Read monitored-email-accounts.yaml and return one DwdHuman per distinct human.

    - Keeps only entries with enabled && dwd_eligible.
    - Drops shared inboxes (payables@/receipts@/service@, or no-slack + "Inbox" name).
    - Collapses cross-domain aliases into one human via union-find over three edge
      types: shared slack_user_id, shared email (primary or alias), shared normalized
      name. Faithful to the spec ("collapse by slack_user_id, fall back to name").
    - Seat scope (2026-07-01 right-size): if ANY account in the file carries
      fireflies_seat: true, only humans with at least one flagged account in their
      collapsed component are returned — people removed from Fireflies stay in this
      file for Gmail/Drive ingestion but fall out of the coverage monitor's scope.
      A file with no flags keeps the full roster (backward-compatible).
    """
    yaml_path = Path(path) if path else _ACCOUNTS_YAML
    data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    raw_accounts = data.get("accounts") or []

    # Seat-scope mode is detected across the whole file (not just eligible entries);
    # the flag itself is evaluated per collapsed component below, because it may sit
    # on any one of a human's alias entries.
    seat_scope = any(
        isinstance(a, dict) and bool(a.get("fireflies_seat")) for a in raw_accounts
    )

    accounts = [
        a
        for a in raw_accounts
        if isinstance(a, dict)
        and a.get("enabled")
        and a.get("dwd_eligible")
        and not _is_shared_inbox(a)
    ]

    uf = _UnionFind()
    for i in range(len(accounts)):
        uf.find(i)

    # index by linking keys
    by_slack: dict[str, int] = {}
    by_email: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for i, acct in enumerate(accounts):
        sid = (acct.get("slack_user_id") or "").strip()
        if sid:
            if sid in by_slack:
                uf.union(by_slack[sid], i)
            else:
                by_slack[sid] = i
        for em in _account_emails(acct):
            if em in by_email:
                uf.union(by_email[em], i)
            else:
                by_email[em] = i
        nm = _norm_name(acct.get("name") or "")
        if nm:
            if nm in by_name:
                uf.union(by_name[nm], i)
            else:
                by_name[nm] = i

    # gather components
    components: dict[int, list[int]] = {}
    for i in range(len(accounts)):
        components.setdefault(uf.find(i), []).append(i)

    humans: list[DwdHuman] = []
    for member_idxs in components.values():
        members = [accounts[i] for i in member_idxs]
        if seat_scope and not any(m.get("fireflies_seat") for m in members):
            continue
        all_emails: set[str] = set()
        for m in members:
            all_emails |= _account_emails(m)

        # representative: prefer an entry with a slack_user_id, then an @hjrglobal.com
        # email, then deterministic by email — for stable name/primary selection.
        def _rep_key(m: dict) -> tuple:
            has_slack = 0 if m.get("slack_user_id") else 1
            email = (m.get("email") or "").lower()
            is_hjrg = 0 if email.endswith("@hjrglobal.com") else 1
            return (has_slack, is_hjrg, email)

        rep = sorted(members, key=_rep_key)[0]
        primary_email = (rep.get("email") or "").strip().lower()
        slack_user_id = next(
            (m.get("slack_user_id") for m in members if m.get("slack_user_id")), None
        )
        entity_default = rep.get("entity_default")
        # cleaned display name (drop "(BDM)" / "(UFL legacy)" style suffixes)
        display_name = re.sub(r"\s*\(.*?\)\s*", "", rep.get("name") or "").strip() or primary_email

        humans.append(
            DwdHuman(
                name=display_name,
                primary_email=primary_email,
                known_aliases=sorted(all_emails - {primary_email}),
                slack_user_id=slack_user_id,
                entity_default=entity_default,
            )
        )

    humans.sort(key=lambda h: h.name.lower())
    return humans


def classify(
    dwd_humans: list[DwdHuman],
    fireflies_members: list[dict],
    recent_host_emails: set[str] | None = None,
) -> CoverageReport:
    """Classify each DWD human into COVERED / MEMBER_NO_RECORDINGS / NOT_A_MEMBER.

    Matching is alias-aware and case-insensitive (any of a human's emails matching a
    Fireflies member email counts). `recent_host_emails`, when provided, refines ONLY
    members that have transcripts: a member with recordings but none in the window is
    bucketed MEMBER_NO_RECORDINGS with `has_older_recordings=True`. When None, COVERED
    is decided purely on transcript count (primary signal). A NOT_A_MEMBER is never
    promoted regardless of recency (correctness lock).
    """
    member_by_email: dict[str, dict] = {}
    for m in fireflies_members:
        email = (m.get("email") or "").strip().lower()
        if email:
            member_by_email[email] = m

    # None => refinement disabled (COVERED on transcript count alone).
    # A set (even empty) => refinement ran; empty means "nobody recent".
    recent = None if recent_host_emails is None else {e.strip().lower() for e in recent_host_emails}

    results: list[PersonResult] = []
    for human in dwd_humans:
        matched = None
        for email in human.all_emails:
            if email in member_by_email:
                matched = member_by_email[email]
                break

        if matched is None:
            results.append(PersonResult(human=human, status=NOT_A_MEMBER, is_member=False))
            continue

        n = int(matched.get("num_transcripts") or 0)
        if n <= 0:
            results.append(
                PersonResult(
                    human=human, status=MEMBER_NO_RECORDINGS, num_transcripts=0, is_member=True
                )
            )
            continue

        # member with recordings
        if recent is None or (human.all_emails & recent):
            results.append(
                PersonResult(human=human, status=COVERED, num_transcripts=n, is_member=True)
            )
        else:
            results.append(
                PersonResult(
                    human=human,
                    status=MEMBER_NO_RECORDINGS,
                    num_transcripts=n,
                    is_member=True,
                    has_older_recordings=True,
                )
            )

    return CoverageReport(results=results)


# ── formatting (Slack mrkdwn) ─────────────────────────────────────────────────


def _person_line(r: PersonResult, days: int) -> str:
    h = r.human
    if r.status == COVERED:
        plural = "" if r.num_transcripts == 1 else "s"
        return f"  - {h.name} ({r.num_transcripts} transcript{plural})"
    if r.status == MEMBER_NO_RECORDINGS:
        suffix = f" (has older recordings, none in last {days}d)" if r.has_older_recordings else ""
        return f"  - {h.name} <{h.primary_email}>{suffix}"
    return f"  - {h.name} <{h.primary_email}>"


def format_digest(report: CoverageReport, days: int = 30) -> str:
    """Build the Harrison digest (Slack mrkdwn). Always safe to send."""
    lines: list[str] = ["*Fireflies coverage check* (DWD users)"]

    if report.enumerate_failed:
        lines.append(
            "\n:warning: Could not enumerate Fireflies members this run "
            "(admin `users` query failed) -- coverage is UNKNOWN below. "
            "DWD roster for reference:"
        )
        for r in sorted(report.results, key=lambda x: x.human.name.lower()):
            lines.append(f"  - {r.human.name} <{r.human.primary_email}>")
        lines.append(f"\nVerify members: {_ADMIN_LINK}")
        return "\n".join(lines)

    lines.append(
        f"\n:white_check_mark: Covered: {len(report.covered)}  "
        f":warning: Member, no recordings: {len(report.member_no_recordings)}  "
        f":x: Not a member: {len(report.not_a_member)}"
    )

    if report.not_a_member:
        lines.append("\n:x: *Not a member* (invite not accepted -- need to accept the Fireflies invite email):")
        for r in sorted(report.not_a_member, key=lambda x: x.human.name.lower()):
            lines.append(_person_line(r, days))

    if report.member_no_recordings:
        lines.append("\n:warning: *Member, no recordings* (connect Google Calendar + enable auto-join):")
        for r in sorted(report.member_no_recordings, key=lambda x: x.human.name.lower()):
            lines.append(_person_line(r, days))

    if report.covered:
        lines.append("\n:white_check_mark: *Covered*:")
        for r in sorted(report.covered, key=lambda x: x.human.name.lower()):
            lines.append(_person_line(r, days))

    lines.append(f"\nVerify acceptances: {_ADMIN_LINK}")
    return "\n".join(lines)


def nudge_text(result: PersonResult) -> str:
    """Per-user nudge DM copy, branched on status (CP-1 ruling)."""
    if result.status == NOT_A_MEMBER:
        return (
            "Quick one -- your meetings aren't being captured by Fireflies yet. "
            "Please (1) accept the Fireflies invite email, then (2) connect your Google "
            "Calendar at app.fireflies.ai -> Settings -> Integrations and turn on auto-join. "
            "Takes ~2 minutes. Ping me if the invite didn't arrive."
        )
    # MEMBER_NO_RECORDINGS — already a member, just needs the calendar hooked up.
    return (
        "Quick one -- you're in our Fireflies workspace but no meetings are being captured "
        "yet. Please connect your Google Calendar at app.fireflies.ai -> Settings -> "
        "Integrations and turn on auto-join. Takes ~2 minutes. Ping me if you hit a snag."
    )
