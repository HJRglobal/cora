#!/usr/bin/env python
"""Drop two canon-violating cases from data/evals/golden-set-auto.yaml (Code #15 rider (c)).

DRY-RUN BY DEFAULT: prints the case ids it WOULD drop and writes nothing.
Pass --apply to write. Harrison runs it, from the PRIMARY checkout:

    .venv\\Scripts\\python.exe scripts\\purge_superseded_golden_cases_2026-09-24.py
    .venv\\Scripts\\python.exe scripts\\purge_superseded_golden_cases_2026-09-24.py --apply
    .venv\\Scripts\\python.exe scripts\\purge_superseded_golden_cases_2026-09-24.py   (must say clean)

It edits the golden-set-auto.yaml of the repo it RUNS IN (the file next to this
script's parent), so running a worktree copy touches only that worktree.

TARGETS (exact case id; each also guarded by a content key)
  auto-note-cdef5ccb8289  expect_substring "F3 Pure retail price is $36.99 everywhere"
      The 2026-07-08 F3 Pure price, SUPERSEDED 2026-09-23 ($39.99 since Aug 4). The
      case still PASSES because the old text survives inside the live known-answer's
      SUPERSEDED stamp -- so the weekly eval CERTIFIES a superseded fact -- and it
      contradicts its sibling auto-note-94fd91c1dcdb ($39.99).
  auto-ka-1  expect_substring "Payment terms are Net 30."
      The Code #11 S2 test fixture. The 8/30 purge removed it from the primary
      WORKING TREE only; committed HEAD still carries it, so any fresh checkout,
      worktree or DR rebuild brings it back until Harrison commits the live file.

WHY A STAGED SCRIPT AND NOT A BRANCH EDIT
    The file is tracked AND bot-written (cora.golden_set._write_auto on every
    approved knowledge write), and the primary working tree carries uncommitted
    REAL cases. A branch commit that touches it makes Harrison's --ff-only merge
    refuse ("local changes would be overwritten"). So the fix runs against the
    live tree, in place, and never restores anything from a git blob.

WHY ID + CONTENT KEY, NEVER TEXT ALONE
    A case is located by its exact `- id:` line. Its expect_substring must then
    start with the content key above, or the drop is REFUSED -- an id collision
    with a different case can never delete a real one. "Net 30" and "$36.99"
    are never searched for on their own: both occur in real cases and canon.

SAFETY
  * Before anything is written, the result is re-parsed (yaml.safe_load) and must
    equal the original document with EXACTLY the dropped ids removed -- same other
    cases, same order, same top-level keys. Otherwise the whole run is REFUSED and
    nothing is written.
  * Atomic temp + os.replace; a one-time backup `<file>.bak-2026-09-24` holding the
    exact bytes the plan was verified against (an existing backup is never
    overwritten, so a second --apply keeps the first original).
  * Every other byte is preserved: header comments, the other cases' formatting,
    and the file's own line terminator (the live file is CRLF).
  * An absent id is a no-op, reported as clean.

WHAT IS DELIBERATELY NOT TOUCHED
  * The superseded known-answer text itself (live _brain/known-answers/f3e.md):
    canon, D-011 -- not this script's.
  * Committed HEAD's copy of the file: auto-ka-1 disappears from git only when
    Harrison commits the live file.
  * Any other case, including the "for Harrison's word" candidates the Code #15
    scout listed (stale portfolio-cash figure, one-off calculation, a fragment).

CONCURRENCY
    The bot rewrites the WHOLE file on each approved append (read, append, temp +
    replace), so an append can race --apply two ways; neither corrupts the file.
  * The bot read BEFORE --apply and replaced AFTER it: its rewrite RESURRECTS a
    dropped case. That is why the last step above is a second dry run that must
    say clean.
  * The bot replaced the file AFTER --apply read it: writing the plan would silently
    drop the bot's new case, and the second dry run could not see that. So --apply
    re-reads the file immediately before its os.replace and REFUSES -- nothing
    written, no backup, no temp left -- unless it is byte-identical to what was
    planned and verified. Re-run the same command. What is left is the
    sub-millisecond gap between that re-read and the os.replace itself.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
DEFAULT_PATH = _REPO / "data" / "evals" / "golden-set-auto.yaml"
BACKUP_SUFFIX = ".bak-2026-09-24"

#: exact case id -> the prefix its expect_substring must carry (whitespace-normalised)
TARGETS: dict[str, str] = {
    "auto-note-cdef5ccb8289": "F3 Pure retail price is $36.99 everywhere",
    "auto-ka-1": "Payment terms are Net 30.",
}

_ID_LINE = re.compile(r"""^- id: ['"]?(?P<id>[A-Za-z0-9_.-]+)['"]?\s*$""")


class Refused(Exception):
    """The planned edit failed a safety check; nothing is written."""


def _norm(text: object) -> str:
    return " ".join(str(text or "").split())


def _safe_write(path: Path, text: str, original: bytes) -> Path | None:
    """Atomic write + one-time backup of *original* -- the bytes the plan was built
    and verified on, never a second read. Returns the backup path when one was made.

    Raises Refused, leaving nothing behind (no write, no new backup, no temp), when
    the file no longer holds *original* at the moment before the replace: the bot
    rewrote it after it was read, and writing the plan would drop that rewrite.
    """
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    tmp = path.with_name(path.name + ".tmp-purge")
    made = None
    try:
        if not backup.exists():
            backup.write_bytes(original)
            made = backup
        with io.open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        try:
            current = path.read_bytes()
        except OSError:
            current = None
        if current != original:
            raise Refused("the file changed after it was read and verified (the bot "
                          "rewrote it -- an approved append?) -- nothing written; re-run")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        if made is not None:
            made.unlink(missing_ok=True)
        raise
    return made


def plan(raw: str, targets: dict[str, str] | None = None) -> tuple[str, list[str], list[str]]:
    """Return (new_text, dropped_ids, notes) for *raw*. Pure: never touches disk.

    Raises Refused when the result would not be exactly the original minus the
    dropped cases.
    """
    import yaml

    targets = TARGETS if targets is None else targets
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)
    try:
        before = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise Refused(f"the file does not parse as YAML ({exc.__class__.__name__})") from None
    cases = before.get("cases") if isinstance(before, dict) else None
    if not isinstance(cases, list):
        raise Refused("no top-level `cases:` list")
    parsed_ids = [c.get("id") for c in cases if isinstance(c, dict)]

    notes: list[str] = []
    dropped: list[str] = []
    cut: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        m = _ID_LINE.match(lines[i])
        if not m or m.group("id") not in targets:
            i += 1
            continue
        start = i
        end = i + 1
        # a case block runs to the next column-0 line (the next `- id:` or a
        # top-level key); multi-line scalars are indented, so they stay inside
        while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
            end += 1
        while end - 1 > start and not lines[end - 1].strip():
            end -= 1          # leave blank separator / trailing lines where they are
        cid = m.group("id")
        block = yaml.safe_load(nl.join(lines[start:end]))
        case = block[0] if isinstance(block, list) and block and isinstance(block[0], dict) else {}
        want = _norm(targets[cid])
        got = _norm(case.get("expect_substring"))
        if case.get("id") != cid or not got.startswith(want):
            notes.append(f"REFUSED {cid}: its expect_substring does not start with the "
                         f"content key {targets[cid]!r} -- left in place")
        else:
            cut.append((start, end))
            dropped.append(cid)
            notes.append(f"drop {cid} (lines {start + 1}-{end}, entity {case.get('entity')}, "
                         f"source {case.get('source')})")
        i = end
    for cid in targets:
        if cid not in dropped and not any(n.startswith(f"REFUSED {cid}") for n in notes):
            if cid in parsed_ids:
                raise Refused(f"{cid} is in the parsed cases but has no `- id: {cid}` line "
                              "(unexpected layout) -- nothing written")
            notes.append(f"clean: {cid} not present")

    if not cut:
        return raw, dropped, notes
    keep = [ln for k, ln in enumerate(lines) if not any(s <= k < e for s, e in cut)]
    new_text = nl.join(keep)
    verify(before, new_text, dropped)
    return new_text, dropped, notes


def verify(before: dict, new_text: str, dropped: list[str]) -> None:
    """The safety net: *new_text* must parse to *before* minus exactly *dropped*.

    Same other cases in the same order, same top-level keys. Raises Refused.
    """
    import yaml

    try:
        after = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        raise Refused(f"re-parse check failed: the result does not parse "
                      f"({exc.__class__.__name__}) -- nothing written") from None
    cases = before.get("cases") or []
    expected = [c for c in cases if not (isinstance(c, dict) and c.get("id") in dropped)]
    if not isinstance(after, dict) or after.get("cases") != expected:
        raise Refused("re-parse check failed: the result is not the original minus exactly "
                      "the dropped cases -- nothing written")
    if {k: v for k, v in after.items() if k != "cases"} != \
            {k: v for k, v in before.items() if k != "cases"}:
        raise Refused("re-parse check failed: a top-level key changed -- nothing written")


def purge(path: Path, apply: bool) -> tuple[int, list[str]]:
    """Plan the drop for *path*; write it only when *apply*. Returns (dropped, notes)."""
    notes = [f"target: {path}"]
    if not path.exists():
        return 0, notes + ["SKIP: file not found"]
    original = path.read_bytes()
    raw = original.decode("utf-8")      # no newline translation: the bytes, as text
    try:
        new_text, dropped, plan_notes = plan(raw)
    except Refused as exc:
        return 0, notes + [f"REFUSED: {exc}"]
    notes += plan_notes
    if apply and dropped:
        try:
            backup = _safe_write(path, new_text, original)
        except Refused as exc:
            return 0, notes + [f"REFUSED: {exc}"]
        if backup is not None:
            notes.append(f"backup: {backup}")
    return len(dropped), notes


def _redirect_warning() -> str | None:
    """The bot honours GOLDEN_SET_AUTO_PATH; this script edits the repo file only."""
    val = os.environ.get("GOLDEN_SET_AUTO_PATH")
    if not val:
        try:
            from dotenv import dotenv_values
            val = (dotenv_values(_REPO / ".env") or {}).get("GOLDEN_SET_AUTO_PATH")
        except Exception:  # noqa: BLE001
            val = None
    if val and Path(val).resolve() != DEFAULT_PATH.resolve():
        return ("WARNING: GOLDEN_SET_AUTO_PATH is set and points elsewhere -- the bot "
                "writes THAT file, and this script edits only the repo copy.")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args(argv)

    print("=== superseded golden-case purge (%s) ===" % ("APPLY" if args.apply else "DRY RUN"))
    warn = _redirect_warning()
    if warn:
        print(warn)
    count, notes = purge(DEFAULT_PATH, args.apply)
    for n in notes:
        print("   " + n)
    print("%s %d case(s)" % ("removed" if args.apply else "would remove", count))
    if not args.apply:
        print("DRY RUN -- nothing written. Re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
