#!/usr/bin/env python3
r"""RIDER B items 6+7 (cq-59c5048d0891): propose an ARCHIVE manifest for the
HOLD-partition + HOLD-lane rows of the folder-audit holds file of record.

WHAT IT DOES
------------
Reads the holds file of record (``_archive\dedup-2026-09\_planning\
manifest-holds-remaining-2026-09.csv``), groups every row by sha256 (a group =
every ``source_relpath`` plus its one ``keep_relpath``), and decides each group
that contains a HOLD-partition or HOLD-lane row AS A WHOLE: either every
non-keep member is archived against one canonical keep, or nothing in the group
moves. A group is never half-archived.

DEFAULT = DRY-RUN. It computes and prints a counts-only summary and writes
NOTHING. ``--write --out-dir <dir>`` writes four files, create-only (it refuses
to run if any of them already exists, never mkdirs the out-dir, and refuses an
out-dir inside ``--root``):

  manifest-riderb-holds-archive-2026-09.csv     ARCHIVE rows only -- passes the
                                                apply.ps1 gates (no HOLD/KEEP, no
                                                08- path in any column, no DELETE)
  manifest-riderb-holds-still-held-2026-09.csv  the in-scope rows that stay held,
                                                reason_code = HOLD-rb-<reason>
  riderb-holds-misses-2026-09.csv               item 7: members missing on disk
  riderb-holds-summary-2026-09.md               counts only

Nothing here moves a file. Harrison reviews the proposal, copies it to
``_planning`` and runs the Drive ``apply.ps1`` by hand (dry-run first).

THE CONSERVATIVE DEFAULTS (Code #15 kickoff decisions, 2026-09-24)
------------------------------------------------------------------
  * RI lane = only the receipts@ / payables@ localpart copies in
    ``01-HJR-Global\accounting\Receipts & Invoices Inbox`` (``--ri-scope broad``
    = the whole Inbox). Both RI numbers are printed either way.
  * same-partition ENT duplicates HOLD ``not-in-ruling-samepart``
    (``--include-samepart`` to archive them).
  * binder groups HOLD (Justin's workstream); LEX HOLD; a group touching any
    pinned container HOLDs (a file is never moved out of a pinned container).
  * any missing member -> HOLD + listed in the misses file (never guess).
  * every member of an eligible group is re-verified against the disk NOW: size
    must equal the manifest bytes and the mtime must equal the 9/21 hashing
    baseline inventory's, else HOLD (apply.ps1 checks neither the keep's
    existence nor byte identity before Move-Item -- this is that check, run as
    close to apply as Harrison runs this script).

Read-only against the Founder-OS tree; the only writes are the four files above.
No .env is needed (no credential is read).
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import drive_io  # noqa: E402
from cora.hygiene_drive_report import (  # noqa: E402
    MANIFEST_COLUMNS,
    MANIFEST_HEADER_BYTES,
    is_lex_partition,
    pinned_container,
    render_manifest_csv,
)

DEFAULT_ROOT = r"G:\My Drive\HJR-Founder-OS"
DEFAULT_HOLDS = DEFAULT_ROOT + r"\_archive\dedup-2026-09\_planning\manifest-holds-remaining-2026-09.csv"
DEFAULT_SNAPSHOT = r"C:\Users\Harri\Downloads\hjr-folder-audit\manifest-holds-remaining-2026-09.csv"
DEFAULT_SCHEMA_REF = DEFAULT_ROOT + r"\_archive\dedup-2026-09\_planning\manifest-dedup-2026-09.csv"
DEFAULT_BASELINE_INVENTORY = r"C:\Users\Harri\Downloads\hjr-folder-audit\inventory-files-20260921-1948.csv"

SCOPE_REASONS = frozenset({"HOLD-partition", "HOLD-lane"})
ARCHIVE_PREFIX = "_archive\\dedup-2026-09\\"
MAX_PATH = 260

OUT_ARCHIVE = "manifest-riderb-holds-archive-2026-09.csv"
OUT_HELD = "manifest-riderb-holds-still-held-2026-09.csv"
OUT_MISSES = "riderb-holds-misses-2026-09.csv"
OUT_SUMMARY = "riderb-holds-summary-2026-09.md"
OUT_NAMES = (OUT_ARCHIVE, OUT_HELD, OUT_MISSES, OUT_SUMMARY)

# Archived-copy class -> reason code (Harrison can strike a whole class in review).
ARCHIVE_REASON = {
    "ri": "RB6-lane-ri",
    "ea": "RB6-lane-ea",
    "inv": "RB6-lane-inv",
    "ent": "RB6-samepart",
}

# E7 (2026-09-22) rewrote the 5 keeps that pointed into these un-refiled dumps.
# Kept as a guard that currently matches nothing (brief item 7).
E7_OLD_PREFIXES = (
    "05-hjr-productions\\podcast\\ai-agent-hub\\old items",
    "06-hjr-properties\\ai-agent-hub\\old items",
)

_RI_PREFIX = "01-hjr-global\\accounting\\receipts & invoices inbox\\"
_EA_PREFIX = "_shared\\email-attachments\\"
_INV_RE = re.compile(r"^0[0-9]-[^\\]+\\invoices\\", re.IGNORECASE)
_ENT_TOP_RE = re.compile(r"^0[0-9]-[^\\]+$")
# finance_receipts._canonical_filename: "{YYYY-MM-DD}_{localpart}_{safe-original}"
_RI_NARROW_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(receipts|payables)_", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"( \(\d+\)|_\d+)(?=\.[^.\\]+$)|^Copy of |conflicted copy", re.IGNORECASE)
_OTHER_LANE_PREFIXES = ("_brain\\", "_shared\\meetings\\", "_shared\\claude-workspace-mirror\\",
                        "_session-captures\\")


# ── classification ───────────────────────────────────────────────────────────

def top_of(rel: str) -> str:
    return rel.split("\\", 1)[0]


def classify(rel: str, *, ri_scope: str) -> tuple[str, str]:
    """(zone, detail) for one member path; first match wins.

    zone: LEX | PIN | BINDER | LANE (detail ea/ri/inv) | RI-OUTSIDE | OLANE | ENT | OTHER
    """
    p = rel.lower()
    if is_lex_partition(rel):
        return "LEX", ""
    pin = pinned_container(rel)
    if pin:
        return "PIN", pin
    if "\\visibility-binder\\" in p or p.startswith("visibility-binder\\"):
        return "BINDER", ""
    if p.startswith(_EA_PREFIX):
        return "LANE", "ea"
    if p.startswith(_RI_PREFIX):
        name = rel.rsplit("\\", 1)[-1]
        if ri_scope == "broad" or _RI_NARROW_RE.match(name):
            return "LANE", "ri"
        return "RI-OUTSIDE", ""
    if _INV_RE.match(rel):
        return "LANE", "inv"
    if p.startswith(_OTHER_LANE_PREFIXES) or "\\_session-captures\\" in p:
        return "OLANE", ""
    if "\\" in rel and _ENT_TOP_RE.match(top_of(rel)):
        return "ENT", ""
    return "OTHER", ""


def has_drive_suffix(rel: str) -> bool:
    return bool(_SUFFIX_RE.search(rel.rsplit("\\", 1)[-1]))


def pick_keep(cands: list[str], current_keep: str) -> str:
    """Current keep if it qualifies; else no Drive suffix, then the shortest
    name, then path order (case-insensitive)."""
    if current_keep in cands:
        return current_keep

    def score(p: str):
        name = p.rsplit("\\", 1)[-1]
        return (1 if has_drive_suffix(p) else 0, len(name), p.lower())
    return sorted(cands, key=score)[0]


# ── inputs ───────────────────────────────────────────────────────────────────

def md5_of(path: str | Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_rows(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if rows and tuple(rows[0].keys()) != MANIFEST_COLUMNS:
        raise ValueError("holds file does not carry the 9-column manifest schema")
    return rows


def load_baseline(path: str | Path) -> dict[str, tuple[int, str]]:
    """relpath(lower) -> (bytes, ModifiedUtc) from the 9/21 hashing inventory."""
    out: dict[str, tuple[int, str]] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                out[r["RelPath"].lower()] = (int(r["Bytes"]), r["ModifiedUtc"])
            except (KeyError, ValueError):
                continue
    return out


def _utc_second(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the decision ─────────────────────────────────────────────────────────────

@dataclass
class Options:
    root: str
    ri_scope: str = "narrow"
    include_samepart: bool = False
    baseline: dict[str, tuple[int, str]] | None = None   # None = mtime check skipped
    # The root apply.ps1 will move under (its -Root default). The MAX_PATH check
    # measures dest paths against THIS, not against --root (which only locates
    # the files for the existence/size checks).
    apply_root: str = DEFAULT_ROOT


@dataclass
class Group:
    sha: str
    rows: list[dict]
    keep: str
    members: list[str]
    decision: str = ""          # "ARCHIVE" or "HOLD"
    reason: str = ""            # hold reason, or the eligible keep kind
    new_keep: str = ""
    archived: list[tuple[str, str]] = field(default_factory=list)   # (member, class)
    missing: list[tuple[str, str]] = field(default_factory=list)    # (role, relpath)

    @property
    def scope_rows(self) -> list[dict]:
        return [r for r in self.rows if r["reason_code"] in SCOPE_REASONS]


@dataclass
class Result:
    groups: list[Group]
    loose_rows: list[dict]      # in-scope rows that belong to no group (empty sha256)
    rows_total: int
    rows_in_scope: int


class _Stat:
    """Bounded, memoised stat of Founder-OS relpaths (DriveUnavailable propagates)."""

    def __init__(self, root: str):
        self.root = root
        self._cache: dict[str, tuple[float, int] | None] = {}

    def info(self, rel: str) -> tuple[float, int] | None:
        if rel not in self._cache:
            self._cache[rel] = drive_io.stat_info(os.path.join(self.root, rel), retry_seconds=0)
        return self._cache[rel]

    def exists(self, rel: str) -> bool:
        return self.info(rel) is not None


def build_groups(rows: list[dict]) -> tuple[list[Group], list[dict]]:
    by_sha: dict[str, list[dict]] = collections.defaultdict(list)
    loose: list[dict] = []
    for r in rows:
        if not r.get("sha256"):
            if r["reason_code"] in SCOPE_REASONS:
                loose.append(r)
            continue
        by_sha[r["sha256"]].append(r)
    groups = []
    for sha, grs in by_sha.items():
        if not any(r["reason_code"] in SCOPE_REASONS for r in grs):
            continue
        keeps = {r["keep_relpath"] for r in grs}
        keep = sorted(keeps)[0]
        members = list(dict.fromkeys([r["source_relpath"] for r in grs] + sorted(keeps)))
        g = Group(sha, grs, keep, members)
        if len(keeps) > 1:
            g.decision, g.reason = "HOLD", "multi-keep"
        groups.append(g)
    return groups, loose


def decide(groups: list[Group], opts: Options, stat: _Stat, all_rows: list[dict]) -> None:
    # A path that is a member of two sha groups cannot be two contents.
    seen: dict[str, set[str]] = collections.defaultdict(set)
    for r in all_rows:
        if r.get("sha256"):
            seen[r["source_relpath"].lower()].add(r["sha256"])
            seen[r["keep_relpath"].lower()].add(r["sha256"])
    for g in groups:
        if g.decision:
            continue
        _decide_one(g, opts, stat, seen)


def _hold(g: Group, reason: str) -> None:
    g.decision, g.reason = "HOLD", reason
    g.archived = []


def _decide_one(g: Group, opts: Options, stat: _Stat, seen: dict[str, set[str]]) -> None:
    zones = {m: classify(m, ri_scope=opts.ri_scope) for m in g.members}
    kinds = {z for z, _ in zones.values()}
    if any(r["source_relpath"] == r["keep_relpath"] for r in g.rows):
        return _hold(g, "self-keep")
    if any(len(seen[m.lower()]) > 1 for m in g.members):
        return _hold(g, "path-in-two-groups")
    # LEX before any disk touch: a LEX path is never stat'd, listed or written.
    if "LEX" in kinds:
        return _hold(g, "lex")
    for r in g.rows:
        if not stat.exists(r["source_relpath"]):
            g.missing.append(("source", r["source_relpath"]))
    if not stat.exists(g.keep):
        g.missing.append(("keep", g.keep))
    if g.missing:
        return _hold(g, "member-missing")
    pins = {d for z, d in zones.values() if z == "PIN"}
    if pins:
        if len(pins) == 1 and kinds == {"PIN"}:
            return _hold(g, "pinned-internal")
        return _hold(g, "straddles-pinned")
    if "BINDER" in kinds:
        return _hold(g, "binder")
    if any(r["reason_code"] not in SCOPE_REASONS for r in g.rows):
        return _hold(g, "other-hold-in-group")
    if kinds & {"OLANE", "OTHER"}:
        return _hold(g, "not-in-ruling")
    if "RI-OUTSIDE" in kinds:
        return _hold(g, "ri-outside-narrow")
    if any(m.lower().startswith(E7_OLD_PREFIXES) for m in g.members):
        return _hold(g, "e7-old-items")
    ents = [m for m in g.members if zones[m][0] == "ENT"]
    lanes = [m for m in g.members if zones[m][0] == "LANE"]
    invs = [m for m in lanes if zones[m][1] == "inv"]
    ent_tops = {top_of(m) for m in ents}
    inv_tops = {top_of(m) for m in invs}
    if len(ent_tops) >= 2:
        return _hold(g, "cross-entity")
    if (ent_tops and inv_tops and inv_tops != ent_tops) or (not ent_tops and len(inv_tops) >= 2):
        return _hold(g, "cross-entity-invoice-lane")
    if len(ents) >= 2 and not opts.include_samepart:
        return _hold(g, "not-in-ruling-samepart")
    if ents:
        cands, kind = ents, "keep-ent"
    elif invs:
        cands, kind = invs, "keep-inv"
    else:
        cands, kind = lanes, "keep-lane"
    if not cands:
        return _hold(g, "no-candidate")
    new_keep = pick_keep(cands, g.keep)
    archived = []
    for m in g.members:
        if m == new_keep:
            continue
        z, d = zones[m]
        archived.append((m, d if z == "LANE" else "ent"))
    # Re-verify EVERY member against the disk now (size) and the hashing baseline
    # (mtime): the 9/21 sha256 is only as good as the file being unchanged since.
    try:
        want_bytes = int(g.rows[0]["bytes"])
    except (KeyError, ValueError):
        return _hold(g, "changed-since-hash")
    for m in g.members:
        info = stat.info(m)
        if info is None:
            g.missing.append(("member", m))
            return _hold(g, "member-missing")
        mtime, size = info
        if size != want_bytes:
            return _hold(g, "changed-since-hash")
        if opts.baseline is not None:
            base = opts.baseline.get(m.lower())
            if base is None:
                return _hold(g, "not-in-hash-baseline")
            if base[0] != want_bytes or base[1] != _utc_second(mtime):
                return _hold(g, "changed-since-hash")
    for m, _cls in archived:
        dest = ARCHIVE_PREFIX + m
        if m.lower().endswith(".md"):
            return _hold(g, "md-member")
        if len(opts.apply_root.rstrip("\\") + "\\" + dest) >= MAX_PATH:
            return _hold(g, "dest-path-too-long")
        if stat.exists(dest):
            return _hold(g, "dest-exists")
    g.decision, g.reason, g.new_keep, g.archived = "ARCHIVE", kind, new_keep, archived


def propose(rows: list[dict], opts: Options, stat: _Stat | None = None) -> Result:
    stat = stat or _Stat(opts.root)
    groups, loose = build_groups(rows)
    decide(groups, opts, stat, rows)
    in_scope = sum(1 for r in rows if r["reason_code"] in SCOPE_REASONS)
    return Result(groups, loose, len(rows), in_scope)


# ── outputs ──────────────────────────────────────────────────────────────────

def archive_rows(res: Result) -> list[dict]:
    out = []
    for g in res.groups:
        if g.decision != "ARCHIVE":
            continue
        by_src = {r["source_relpath"]: r for r in g.rows}
        for m, cls in g.archived:
            src_row = by_src.get(m)
            out.append({
                "action": "ARCHIVE",
                "source_relpath": m,
                "dest_relpath": ARCHIVE_PREFIX + m,
                "reason_code": ARCHIVE_REASON[cls],
                "sha256": g.sha,
                "bytes": g.rows[0]["bytes"],
                "keep_relpath": g.new_keep,
                "is_md": src_row["is_md"] if src_row else ("True" if m.lower().endswith(".md") else "False"),
                "top_level": src_row["top_level"] if src_row else (top_of(m) if "\\" in m else "<root>"),
            })
    return out


def held_rows(res: Result) -> tuple[list[dict], int]:
    """In-scope rows that stay held, reason HOLD-rb-<reason>. LEX-path rows are
    counted, never written (they stay held in the file of record)."""
    out, lex = [], 0
    for g in res.groups:
        if g.decision == "ARCHIVE":
            continue
        for r in g.scope_rows:
            if any(is_lex_partition(r.get(c, "")) for c in ("source_relpath", "dest_relpath", "keep_relpath")):
                lex += 1
                continue
            out.append({**r, "action": "HOLD", "reason_code": "HOLD-rb-" + g.reason})
    for r in res.loose_rows:
        if any(is_lex_partition(r.get(c, "")) for c in ("source_relpath", "dest_relpath", "keep_relpath")):
            lex += 1
            continue
        out.append({**r, "action": "HOLD", "reason_code": "HOLD-rb-no-sha256"})
    return out, lex


def misses_csv(res: Result) -> tuple[bytes, int]:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["sha256", "role", "missing_relpath", "group_in_scope_rows"])
    n = 0
    for g in res.groups:
        for role, rel in dict.fromkeys(g.missing):
            if is_lex_partition(rel):
                continue
            w.writerow([g.sha, role, rel, len(g.scope_rows)])
            n += 1
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"), n


class GateError(RuntimeError):
    pass


def check_gates(arch: list[dict], held: list[dict], res: Result) -> None:
    """Mirror apply.ps1's refusal gates + this proposal's own invariants. Raises
    GateError; nothing is written when it does."""
    srcs = [r["source_relpath"] for r in arch]
    if len(srcs) != len(set(s.lower() for s in srcs)):
        raise GateError("duplicate source in the archive manifest")
    keeps = {r["keep_relpath"].lower() for r in arch}
    for r in arch:
        if r["action"] != "ARCHIVE":
            raise GateError("non-ARCHIVE row in the archive manifest")
        for c in MANIFEST_COLUMNS:
            v = str(r.get(c, ""))
            if v.lower().startswith("08-") or (c.endswith("relpath") and is_lex_partition(v)):
                raise GateError("08- / LEX path in the archive manifest")
        if r["dest_relpath"] != ARCHIVE_PREFIX + r["source_relpath"]:
            raise GateError("dest is not _archive\\dedup-2026-09\\ + source")
        if r["source_relpath"].lower() in keeps:
            raise GateError("a keep is also archived")
        if pinned_container(r["source_relpath"]) or pinned_container(r["keep_relpath"]):
            raise GateError("a pinned-container member in the archive manifest")
    for g in res.groups:
        if g.decision != "ARCHIVE":
            continue
        got = {m for m, _ in g.archived} | {g.new_keep}
        if got != set(g.members):
            raise GateError("a group would be half-archived")
    for r in held:
        if r["action"] != "HOLD" or not r["reason_code"].startswith("HOLD-rb-"):
            raise GateError("still-held row without a HOLD-rb- reason")
        if any(str(r.get(c, "")).lower().startswith("08-") for c in MANIFEST_COLUMNS):
            raise GateError("08- path in the still-held file")


# ── summary (counts only) ────────────────────────────────────────────────────

def _ri_count(res: Result, *, localpart_only: bool = False) -> int:
    return sum(1 for g in res.groups if g.decision == "ARCHIVE" for m, c in g.archived
               if c == "ri" and (not localpart_only or _RI_NARROW_RE.match(m.rsplit("\\", 1)[-1])))


def summarize(res: Result, *, opts: Options, record_md5: str, snapshot_md5: str | None,
              other_ri: tuple[str, Result] | None, wrote: bool, n_misses: int, held_lex: int) -> str:
    arch = [g for g in res.groups if g.decision == "ARCHIVE"]
    held = [g for g in res.groups if g.decision == "HOLD"]
    files = sum(len(g.archived) for g in arch)
    total_bytes = sum(int(g.rows[0]["bytes"] or 0) * len(g.archived) for g in arch)
    by_cls = collections.Counter(c for g in arch for _, c in g.archived)
    by_top = collections.Counter(top_of(m) for g in arch for m, _ in g.archived)
    flips = collections.Counter()
    for g in arch:
        if g.new_keep != g.keep:
            old = classify(g.keep, ri_scope="broad")
            new = classify(g.new_keep, ri_scope="broad")
            flips[f"{old[1] or old[0].lower()}->{new[1] or new[0].lower()}"] += 1
    suffixed_keeps = sum(1 for g in arch if has_drive_suffix(g.new_keep))
    max_dest = max((len(opts.apply_root.rstrip("\\") + "\\" + ARCHIVE_PREFIX + m)
                    for g in arch for m, _ in g.archived), default=0)
    reasons_g = collections.Counter(g.reason for g in held)
    reasons_r = collections.Counter()
    for g in held:
        reasons_r[g.reason] += len(g.scope_rows)
    src_missing = sum(1 for g in res.groups for role, _ in dict.fromkeys(g.missing) if role == "source")
    keep_missing_rows = sum(len(g.scope_rows) for g in res.groups if any(role == "keep" for role, _ in g.missing))
    keep_missing_distinct = len({rel for g in res.groups for role, rel in g.missing if role == "keep"})
    snap = ("missing" if snapshot_md5 is None else
            ("match" if snapshot_md5 == record_md5 else f"DIFFERS ({snapshot_md5}) -- the file of record wins"))
    L = ["<!-- generated-by: scripts/propose_rider_b_holds_manifest.py (RIDER B items 6+7) -->", "",
         "# RIDER B holds proposal (items 6+7) -- counts only", "",
         f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
         f"- File of record md5: `{record_md5}` ({res.rows_total} rows); snapshot: {snap}",
         f"- Options: ri-scope={opts.ri_scope}, include-samepart={opts.include_samepart}, "
         f"mtime-check={'on (9/21 hashing baseline)' if opts.baseline is not None else 'OFF (size only)'}",
         f"- In scope (HOLD-partition + HOLD-lane): {res.rows_in_scope} rows in {len(res.groups)} sha256 groups"
         f" (+{len(res.loose_rows)} rows with no sha256)", "",
         "## ARCHIVE proposal", "",
         f"- {len(arch)} groups -> {files} files, {total_bytes / 1_000_000:.1f} MB",
         f"- keep flips: {sum(flips.values())} ({', '.join(f'{k} {v}' for k, v in sorted(flips.items())) or 'none'})",
         f"- new keeps carrying a Drive suffix: {suffixed_keeps}",
         f"- max full dest path length: {max_dest} (limit {MAX_PATH})", "",
         "| Archived class (reason code) | Files |", "|---|---:|"]
    for cls, code in ARCHIVE_REASON.items():
        L.append(f"| {code} | {by_cls.get(cls, 0)} |")
    L += ["", "| Archived sources by top-level | Files |", "|---|---:|"]
    for t, n in sorted(by_top.items(), key=lambda kv: (-kv[1], kv[0])):
        L.append(f"| {t} | {n} |")
    L += ["", "## Still held (in-scope rows)", "", "| Reason (HOLD-rb-...) | Groups | Rows |", "|---|---:|---:|"]
    for reason, n in sorted(reasons_g.items(), key=lambda kv: (-reasons_r[kv[0]], kv[0])):
        L.append(f"| {reason} | {n} | {reasons_r[reason]} |")
    if res.loose_rows:
        L.append(f"| no-sha256 | - | {len(res.loose_rows)} |")
    if held_lex:
        L.append(f"| (LEX rows counted, not written) | - | {held_lex} |")
    L += ["", "## RI reading (receipts|payables) -- both numbers", "",
          "Narrow is decided per GROUP: a group holding any non-receipts@/payables@ Inbox copy "
          "stays held whole, so narrow archives fewer receipts@/payables@ copies than the broad "
          "proposal contains.", "",
          "| ri-scope | RI copies archived | of which receipts@/payables@ | Files archived | Groups |",
          "|---|---:|---:|---:|---:|"]
    pairs = [(opts.ri_scope, res)] + ([other_ri] if other_ri else [])
    for scope, r in sorted(pairs, key=lambda p: p[0] != "narrow"):
        a = [g for g in r.groups if g.decision == "ARCHIVE"]
        L.append(f"| {scope}{' (this proposal)' if r is res else ''} | {_ri_count(r)} | "
                 f"{_ri_count(r, localpart_only=True)} | {sum(len(g.archived) for g in a)} | {len(a)} |")
    L += ["", "## Item 7 -- path drift", "",
          f"- sources missing on disk: {src_missing}",
          f"- keep missing on disk: {keep_missing_distinct} distinct keeps ({keep_missing_rows} in-scope rows)",
          f"- misses listed: {n_misses} (LEX never listed)",
          "- E7 Old-Items keeps: guarded (reason e7-old-items)", "",
          "## Outputs", ""]
    if wrote:
        L += [f"- `{OUT_ARCHIVE}` (ARCHIVE only; apply.ps1 gates mirrored)",
              f"- `{OUT_HELD}`", f"- `{OUT_MISSES}`", f"- `{OUT_SUMMARY}`"]
    else:
        L.append("- DRY-RUN: nothing written. Re-run with `--write --out-dir <dir>`.")
    return "\n".join(L) + "\n"


# ── main ─────────────────────────────────────────────────────────────────────

def _inside(child: str, parent: str) -> bool:
    c = os.path.normcase(os.path.abspath(child)).rstrip("\\/")
    p = os.path.normcase(os.path.abspath(parent)).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--holds", default=DEFAULT_HOLDS, help="the holds file of record (_planning)")
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT, help="the Downloads snapshot (md5-compared only)")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--baseline-inventory", default=DEFAULT_BASELINE_INVENTORY,
                    help="the 9/21 hashing inventory (mtime re-verification)")
    ap.add_argument("--skip-mtime-check", action="store_true",
                    help="verify size only (the summary says so)")
    ap.add_argument("--schema-ref", default=DEFAULT_SCHEMA_REF,
                    help="manifest-dedup-2026-09.csv, whose header bytes the output must match")
    ap.add_argument("--ri-scope", choices=("narrow", "broad"), default="narrow")
    ap.add_argument("--include-samepart", action="store_true")
    ap.add_argument("--write", action="store_true", help="write the four outputs (default: dry-run)")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--apply-root", default=DEFAULT_ROOT,
                    help="the -Root apply.ps1 will run with (dest path-length check)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _parse(argv)
    if a.write and not a.out_dir:
        print("REFUSE: --write needs --out-dir")
        return 2
    if a.write:
        if not os.path.isdir(a.out_dir):
            print("REFUSE: --out-dir does not exist (this script never creates it)")
            return 2
        if _inside(a.out_dir, a.root) or _inside(a.out_dir, a.apply_root):
            print("REFUSE: --out-dir is inside the Founder-OS root; write to Downloads and copy by hand")
            return 2
        clobber = [n for n in OUT_NAMES if os.path.exists(os.path.join(a.out_dir, n))]
        if clobber:
            print(f"REFUSE: {len(clobber)} output file(s) already exist in --out-dir (create-only)")
            return 2
    if not os.path.isfile(a.holds):
        print("REFUSE: the holds file of record is not readable")
        return 2
    record_md5 = md5_of(a.holds)
    snapshot_md5 = md5_of(a.snapshot) if a.snapshot and os.path.isfile(a.snapshot) else None
    if snapshot_md5 is not None and snapshot_md5 != record_md5:
        print("WARN: the Downloads snapshot differs from the file of record -- the _planning file wins")
    if a.schema_ref and os.path.isfile(a.schema_ref):
        with open(a.schema_ref, "rb") as fh:
            ref_head = fh.readline()
        if ref_head.rstrip(b"\r\n") + b"\n" != MANIFEST_HEADER_BYTES:
            print("REFUSE: the reference manifest header no longer matches this script's schema")
            return 3
    else:
        print("WARN: schema reference not readable; using the pinned header constant")
    rows = load_rows(a.holds)
    baseline = None
    if not a.skip_mtime_check:
        if not os.path.isfile(a.baseline_inventory):
            print("REFUSE: the hashing baseline inventory is not readable (pass --skip-mtime-check to verify size only)")
            return 2
        baseline = load_baseline(a.baseline_inventory)
    opts = Options(root=a.root, ri_scope=a.ri_scope, include_samepart=a.include_samepart,
                   baseline=baseline, apply_root=a.apply_root)
    stat = _Stat(a.root)
    try:
        res = propose(rows, opts, stat)
        other_scope = "broad" if a.ri_scope == "narrow" else "narrow"
        other = propose(rows, Options(root=a.root, ri_scope=other_scope, include_samepart=a.include_samepart,
                                      baseline=baseline, apply_root=a.apply_root), stat)
    except drive_io.DriveUnavailable:
        print("REFUSE: the G: mount is unavailable -- no proposal")
        return 2
    arch = archive_rows(res)
    held, held_lex = held_rows(res)
    misses_bytes, n_misses = misses_csv(res)
    try:
        check_gates(arch, held, res)
    except GateError as exc:
        print(f"REFUSE: gate failed -- {exc}; nothing written")
        return 3
    summary = summarize(res, opts=opts, record_md5=record_md5, snapshot_md5=snapshot_md5,
                        other_ri=(other_scope, other), wrote=a.write, n_misses=n_misses, held_lex=held_lex)
    if a.write:
        payloads = {
            OUT_ARCHIVE: render_manifest_csv(arch),
            OUT_HELD: render_manifest_csv(held),
            OUT_MISSES: misses_bytes,
            OUT_SUMMARY: summary.encode("utf-8"),
        }
        for name, data in payloads.items():
            with open(os.path.join(a.out_dir, name), "xb") as fh:   # create-only
                fh.write(data)
    sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
