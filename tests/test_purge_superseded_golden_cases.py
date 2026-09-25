"""Code #15 rider (c): scripts/purge_superseded_golden_cases_2026-09-24.py.

Every test runs the script against a TMP COPY. The real
data/evals/golden-set-auto.yaml is never read for writing, and this file never
calls the script's main() against DEFAULT_PATH (it is monkeypatched to tmp).

Pinned: dry run writes nothing; --apply removes exactly the two target ids and
nothing else (other cases byte-identical, header kept, CRLF kept, backup made);
an absent id is a no-op; a content-key mismatch refuses that id; a second apply
is clean; the re-parse safety check refuses a result that is not "original minus
the dropped cases"; a bot rewrite landing after the read is REFUSED, never
replaced away (D-051 r2 rider#r2-0).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "purge_superseded_golden_cases_2026-09-24.py"
_spec = importlib.util.spec_from_file_location("purge_superseded_golden_cases", _SCRIPT)
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

# Synthetic corpus in the bot writer's own layout (yaml.safe_dump under the
# three-line header). The two target cases carry their real ids and content
# keys; every other case is invented. One target sits mid-file, one is last.
_HEADER = (
    "# AUTO-GROWN eval cases -- appended by the knowledge-review executor on\n"
    "# Harrison-approved knowledge writes (WS-3). Merged with golden-set.yaml\n"
    "# by scripts/run_kb_evals.py. Do not hand-edit ids; prune freely.\n"
)
_CASES = [
    {"id": "auto-ka-20260101000000000001", "entity": "F3E",
     "question": "Which warehouse ships the northwind samples?",
     "expect_substring": "The northwind samples ship from the Tempe dock",
     "source": "known_answer_approval"},
    {"id": "auto-note-94fd91c1dcdb", "entity": "F3E",
     "question": "F3 Pure's MSRP was raised to $39.99 on August 4, with the wholesale tier "
                 "dollar prices unchanged.",
     "expect_substring": "F3 Pure's MSRP was raised to $39.99 on August 4, with the wholesale tier dollar",
     "source": "contributed_note_approval"},
    {"id": "auto-note-cdef5ccb8289", "entity": "F3E",
     "question": "F3 Pure retail price is $36.99 everywhere -- locked 2026-07-08 and reconciled "
                 "across every channel. The earlier idea was not adopted.",
     "expect_substring": "F3 Pure retail price is $36.99 everywhere -- locked 2026-07-08 and reconciled acr",
     "source": "contributed_note_approval"},
    {"id": "auto-note-0123456789ab", "entity": "FNDR",
     "question": "Our vendor invoices are Net 30 unless the contract says otherwise",
     "expect_substring": "Our vendor invoices are Net 30 unless the contract says otherwise",
     "source": "contributed_note_approval"},
    {"id": "auto-ka-1", "entity": "FNDR", "question": "What are the payment terms?",
     "expect_substring": "Payment terms are Net 30.", "source": "known_answer_approval"},
]
_TARGET_IDS = {"auto-note-cdef5ccb8289", "auto-ka-1"}


def _corpus(cases=None, *, crlf=True) -> str:
    text = _HEADER + yaml.safe_dump({"version": 1, "cases": list(cases or _CASES)},
                                    allow_unicode=True, sort_keys=False)
    return text.replace("\n", "\r\n") if crlf else text


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "data" / "evals" / "golden-set-auto.yaml"
    p.parent.mkdir(parents=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _siblings(path: Path) -> set[str]:
    return {p.name for p in path.parent.iterdir()}


def test_dry_run_reports_both_ids_and_writes_nothing(tmp_path):
    p = _write(tmp_path, _corpus())
    before = p.read_bytes()
    count, notes = pg.purge(p, apply=False)
    assert count == 2
    assert any(n.startswith("drop auto-note-cdef5ccb8289") for n in notes)
    assert any(n.startswith("drop auto-ka-1") for n in notes)
    assert p.read_bytes() == before
    assert _siblings(p) == {"golden-set-auto.yaml"}      # no .bak, no temp file


def test_apply_removes_exactly_the_two_ids_and_nothing_else(tmp_path):
    raw = _corpus()
    p = _write(tmp_path, raw)
    count, notes = pg.purge(p, apply=True)
    assert count == 2
    out = p.read_bytes().decode("utf-8")
    doc = yaml.safe_load(out)
    assert doc["version"] == 1
    assert doc["cases"] == [c for c in _CASES if c["id"] not in _TARGET_IDS]
    # every surviving byte is the original's: the result equals a corpus built
    # WITHOUT the two cases, header and CRLF included
    assert out == _corpus([c for c in _CASES if c["id"] not in _TARGET_IDS])
    assert out.startswith(_HEADER.replace("\n", "\r\n"))
    assert "\n" not in out.replace("\r\n", "")          # no lone LF introduced
    # the real "Net 30" case (different id) survives -- never a text purge
    assert "Our vendor invoices are Net 30" in out
    backup = p.with_name(p.name + ".bak-2026-09-24")
    assert backup.read_bytes() == raw.encode("utf-8")
    assert _siblings(p) == {"golden-set-auto.yaml", backup.name}   # temp file replaced away


def test_lf_file_stays_lf(tmp_path):
    p = _write(tmp_path, _corpus(crlf=False))
    assert pg.purge(p, apply=True)[0] == 2
    out = p.read_bytes().decode("utf-8")
    assert "\r" not in out and out.endswith("\n")
    assert out == _corpus([c for c in _CASES if c["id"] not in _TARGET_IDS], crlf=False)


def test_absent_ids_are_a_no_op(tmp_path):
    others = [c for c in _CASES if c["id"] not in _TARGET_IDS]
    p = _write(tmp_path, _corpus(others))
    before = p.read_bytes()
    for apply in (False, True):
        count, notes = pg.purge(p, apply=apply)
        assert count == 0
        assert "clean: auto-note-cdef5ccb8289 not present" in notes
        assert "clean: auto-ka-1 not present" in notes
    assert p.read_bytes() == before
    assert _siblings(p) == {"golden-set-auto.yaml"}      # no backup for a no-op


def test_second_apply_is_clean_and_keeps_the_first_backup(tmp_path):
    raw = _corpus()
    p = _write(tmp_path, raw)
    pg.purge(p, apply=True)
    after_first = p.read_bytes()
    count, notes = pg.purge(p, apply=True)
    assert count == 0 and p.read_bytes() == after_first
    assert p.with_name(p.name + ".bak-2026-09-24").read_bytes() == raw.encode("utf-8")


def test_a_content_key_mismatch_refuses_that_id_only(tmp_path):
    # an id collision with a DIFFERENT case must never delete it
    cases = [dict(c) for c in _CASES]
    for c in cases:
        if c["id"] == "auto-ka-1":
            c["expect_substring"] = "The dock opens at 7am."
    p = _write(tmp_path, _corpus(cases))
    count, notes = pg.purge(p, apply=True)
    assert count == 1
    assert any(n.startswith("REFUSED auto-ka-1") for n in notes)
    ids = [c["id"] for c in yaml.safe_load(p.read_text(encoding="utf-8"))["cases"]]
    assert "auto-ka-1" in ids and "auto-note-cdef5ccb8289" not in ids


def test_the_reparse_safety_net_refuses_every_wrong_result():
    raw = _corpus()
    before = yaml.safe_load(raw)
    new_text, dropped, _ = pg.plan(raw)
    assert set(dropped) == _TARGET_IDS
    pg.verify(before, new_text, dropped)                 # the real cut passes
    # a cut that also took a neighbouring REAL case
    over_cut = new_text.replace("- id: auto-note-0123456789ab", "- id: auto-note-XXXXXXXXXXXX", 1)
    with pytest.raises(pg.Refused, match="minus exactly"):
        pg.verify(before, over_cut, dropped)
    # a cut that left a target in place (under-cut) is equally refused
    with pytest.raises(pg.Refused, match="minus exactly"):
        pg.verify(before, raw, dropped)
    # a changed top-level key
    with pytest.raises(pg.Refused, match="top-level key"):
        pg.verify(before, new_text.replace("version: 1", "version: 2"), dropped)
    # an unparseable result
    with pytest.raises(pg.Refused, match="does not parse"):
        pg.verify(before, "cases: [\n", dropped)
    # and plan() itself refuses input it cannot reason about
    with pytest.raises(pg.Refused):
        pg.plan("cases: not-a-list\n")
    with pytest.raises(pg.Refused):
        pg.plan("cases: [\n")


_RACED = {"id": "auto-note-fedcba987654", "entity": "OSN",
          "question": "Which dock receives the pallet returns?",
          "expect_substring": "Pallet returns go to the Mesa dock",
          "source": "contributed_note_approval"}


def _race_after_plan(monkeypatch, p: Path, rewrite) -> None:
    """The bot's _append_case replacing the file AFTER purge() read and planned it,
    before purge()'s own os.replace (D-051 r2 rider#r2-0)."""
    real_plan = pg.plan

    def racing_plan(raw, targets=None):
        out = real_plan(raw, targets)
        rewrite(p)
        return out
    monkeypatch.setattr(pg, "plan", racing_plan)


def test_an_append_landing_after_the_read_is_refused_not_dropped(tmp_path, monkeypatch):
    # D-051 r2 rider#r2-0: --apply used to write its stale plan over the bot's
    # rewrite -- the new case vanished and the closing dry run still said clean.
    p = _write(tmp_path, _corpus())
    appended = _corpus(_CASES + [_RACED]).encode("utf-8")
    _race_after_plan(monkeypatch, p, lambda q: q.write_bytes(appended))
    count, notes = pg.purge(p, apply=True)
    assert count == 0
    assert any(n.startswith("REFUSED:") and "changed after it was read" in n for n in notes)
    assert p.read_bytes() == appended                      # the bot's case is kept
    assert _siblings(p) == {"golden-set-auto.yaml"}        # no backup, no temp left
    # the operator re-runs: the drop now plans on the file WITH the bot's case,
    # and the backup is exactly the bytes that plan was verified against
    monkeypatch.undo()
    assert pg.purge(p, apply=True)[0] == 2
    ids = [c["id"] for c in yaml.safe_load(p.read_text(encoding="utf-8"))["cases"]]
    assert _RACED["id"] in ids and _TARGET_IDS.isdisjoint(ids)
    assert p.with_name(p.name + ".bak-2026-09-24").read_bytes() == appended


def test_a_refused_apply_keeps_an_existing_backup(tmp_path, monkeypatch):
    raw = _corpus()
    p = _write(tmp_path, raw)
    assert pg.purge(p, apply=True)[0] == 2                 # first apply makes the backup
    backup = p.with_name(p.name + ".bak-2026-09-24")
    p.write_bytes(raw.encode("utf-8"))                      # a resurrecting rewrite
    appended = _corpus(_CASES + [_RACED]).encode("utf-8")
    _race_after_plan(monkeypatch, p, lambda q: q.write_bytes(appended))
    count, notes = pg.purge(p, apply=True)
    assert count == 0 and any(n.startswith("REFUSED:") for n in notes)
    assert p.read_bytes() == appended
    assert backup.read_bytes() == raw.encode("utf-8")       # the first original, untouched
    assert _siblings(p) == {"golden-set-auto.yaml", backup.name}


def test_a_file_that_vanished_after_the_read_is_not_recreated(tmp_path, monkeypatch):
    p = _write(tmp_path, _corpus())
    _race_after_plan(monkeypatch, p, lambda q: q.unlink())
    count, notes = pg.purge(p, apply=True)
    assert count == 0 and any(n.startswith("REFUSED:") for n in notes)
    assert not p.exists() and not any(p.parent.iterdir())


def test_an_unexpected_layout_is_refused_not_guessed(tmp_path):
    # the target id is present but NOT as a column-0 `- id:` line
    raw = "version: 1\ncases:\n- entity: FNDR\n  id: auto-ka-1\n  expect_substring: Payment terms are Net 30.\n"
    p = _write(tmp_path, raw)
    count, notes = pg.purge(p, apply=True)
    assert count == 0 and any(n.startswith("REFUSED:") for n in notes)
    assert p.read_text(encoding="utf-8") == raw


def test_main_is_dry_run_by_default(tmp_path, monkeypatch, capsys):
    p = _write(tmp_path, _corpus())
    before = p.read_bytes()
    monkeypatch.setattr(pg, "DEFAULT_PATH", p)
    monkeypatch.delenv("GOLDEN_SET_AUTO_PATH", raising=False)
    assert pg.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "would remove 2 case(s)" in out
    assert p.read_bytes() == before
    assert pg.main(["--apply"]) == 0
    assert "removed 2 case(s)" in capsys.readouterr().out
    assert {c["id"] for c in yaml.safe_load(p.read_text(encoding="utf-8"))["cases"]}.isdisjoint(_TARGET_IDS)


def test_the_script_targets_its_own_repo_and_never_imports_bot_code():
    assert pg.DEFAULT_PATH == _REPO / "data" / "evals" / "golden-set-auto.yaml"
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "from cora" not in src and "import cora" not in src
    assert "load_dotenv(" not in src           # reads .env only to WARN, via dotenv_values
    assert set(pg.TARGETS) == _TARGET_IDS
