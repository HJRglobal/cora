"""The F3E blog lane's operating-file layer (backlog table / pipeline log).

The backlog is a file Harrison edits by hand, so the tests that matter are the
ones proving an edit is SURGICAL and that a file which changed under us is
refused rather than overwritten.
"""

from __future__ import annotations

import inspect

import pytest

from cora.f3e_blog import operating_files as of

# Trimmed from the real file, including its exact status-cell convention.
BACKLOG = """# F3E Learn Editorial Backlog -- v1 (12 weeks)

_Consumed by the weekly pipeline task._

| # | Status | Working title | Lane/pillar | Target prompt class | Notes |
|---|--------|---------------|-------------|---------------------|-------|
| 1 | PUBLISHED 2026-08-26 (gid 618441081152) | What Is a Functional Energy Drink? | Category education | definitional | Live |
| 2 | DRAFTED 2026-08-26 (gid 618441113920) | Clean Energy Drinks for Yoga | Use-case (Pure) | 0% prompt | staged |
| 3 | QUEUED | L-Theanine and Caffeine | Ingredient explainer | jitters class | CAUTION: no jitters promises |
| 4 | QUEUED | An Honest Sweetener Guide | Ingredient explainer (Pure-led) | sweetener | clean language Pure-only |

## Standing rules
- Refill discipline: fewer than 4 QUEUED rows triggers a proposal.
"""


def test_parse_reads_only_numbered_table_rows():
    rows = of.parse_backlog(BACKLOG)
    assert [r.number for r in rows] == ["1", "2", "3", "4"]
    assert rows[0].title == "What Is a Functional Energy Drink?"
    assert rows[3].notes == "clean language Pure-only"


def test_status_and_gid_are_read_off_the_cell():
    rows = of.parse_backlog(BACKLOG)
    assert rows[0].status == "PUBLISHED"
    assert rows[0].article_gid == "618441081152"
    assert rows[2].status == "QUEUED"
    assert rows[2].article_gid == ""


def test_next_queued_skips_drafted_and_published():
    """This is what stops the job double-staging a row the interim Cowork task
    drafted an hour earlier while both pipelines run."""
    rows = of.parse_backlog(BACKLOG)
    nxt = of.next_queued(rows)
    assert nxt is not None
    assert nxt.number == "3"
    assert of.count_queued(rows) == 2


def test_next_queued_is_none_when_the_backlog_is_drained():
    rows = of.parse_backlog(BACKLOG.replace("| QUEUED |", "| DRAFTED |"))
    assert of.next_queued(rows) is None


def test_proposed_rows_are_never_drafted():
    """PROPOSED is a suggestion, not an authorisation."""
    text = of.append_proposed_rows(BACKLOG, [{"title": "New idea"}])
    rows = of.parse_backlog(text)
    proposed = [r for r in rows if r.status == of.STATUS_PROPOSED]
    assert len(proposed) == 1
    # ...and the picker still returns the QUEUED row, not the PROPOSED one.
    assert of.next_queued(rows).number == "3"


def test_set_row_status_changes_exactly_one_line():
    rows = of.parse_backlog(BACKLOG)
    target = rows[2]
    out = of.set_row_status(BACKLOG, target,
                            of.drafted_status_cell("gid://shopify/Article/555", "2026-09-01"))
    before = BACKLOG.split("\n")
    after = out.split("\n")
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(changed) == 1
    assert "DRAFTED 2026-09-01 (gid 555)" in after[changed[0]]
    # The other cells on that row survive byte for byte.
    assert "L-Theanine and Caffeine" in after[changed[0]]
    assert "CAUTION: no jitters promises" in after[changed[0]]


def test_set_row_status_refuses_when_the_row_moved():
    """A file that changed under us must not get a status written onto whatever
    topic now sits at that line."""
    rows = of.parse_backlog(BACKLOG)
    target = rows[2]
    shifted = BACKLOG.replace(
        "| 3 | QUEUED | L-Theanine and Caffeine |",
        "| 3 | QUEUED | A COMPLETELY DIFFERENT TOPIC |")
    with pytest.raises(ValueError, match="changed under us"):
        of.set_row_status(shifted, target, "DRAFTED")


def test_set_row_status_refuses_when_the_line_is_no_longer_a_row():
    rows = of.parse_backlog(BACKLOG)
    target = rows[2]
    gutted = "\n".join(
        ("just prose now" if i == target.line_index else ln)
        for i, ln in enumerate(BACKLOG.split("\n")))
    with pytest.raises(ValueError):
        of.set_row_status(gutted, target, "DRAFTED")


def test_status_cell_helpers_match_the_human_convention():
    assert of.drafted_status_cell("gid://shopify/Article/7", "2026-08-26") == \
        "DRAFTED 2026-08-26 (gid 7)"
    assert of.published_status_cell("7", "2026-08-26") == "PUBLISHED 2026-08-26 (gid 7)"
    assert of.dismissed_status_cell("7", "2026-08-26") == "DISMISSED 2026-08-26 (gid 7)"


def test_appended_cells_escape_pipes_so_the_table_cannot_shift():
    """Topic text comes from an LLM refill proposal and from the frozen prompt
    basket; neither is pipe-free by contract, and one raw pipe shifts every later
    cell by one column."""
    text = of.append_proposed_rows(
        BACKLOG, [{"title": "Zero sugar | low sugar", "notes": "a | b"}])
    rows = of.parse_backlog(text)
    added = [r for r in rows if r.status == of.STATUS_PROPOSED][0]
    assert added.title.replace("\\", "") == "Zero sugar | low sugar"
    assert added.notes.replace("\\", "") == "a | b"


def test_appended_cells_collapse_newlines():
    text = of.append_proposed_rows(BACKLOG, [{"title": "line one\nline two"}])
    added = [r for r in of.parse_backlog(text) if r.status == of.STATUS_PROPOSED][0]
    assert added.title == "line one line two"


def test_append_refuses_a_backlog_with_no_table():
    with pytest.raises(ValueError, match="no parseable table"):
        of.append_proposed_rows("# just a heading\n", [{"title": "x"}])


def test_appending_nothing_is_a_byte_identical_noop():
    assert of.append_proposed_rows(BACKLOG, []) == BACKLOG


# ---------------------------------------------------------------------------
# Pipeline log
# ---------------------------------------------------------------------------

LOG = """# F3E News + Learn pipeline log

_Newest entry on top._

## 2026-08-26 (older entry)
- something happened
"""


def test_log_entry_goes_on_top_not_the_bottom():
    """The log's own header says newest-on-top, so appending would bury every
    Cora entry under the oldest human ones and read as stale."""
    out = of.prepend_log_entry(LOG, "## 2026-09-02 (new entry)\n- fresh")
    assert out.index("2026-09-02") < out.index("2026-08-26 (older entry)")
    # The intro header is preserved above it.
    assert out.startswith("# F3E News + Learn pipeline log")
    assert "_Newest entry on top._" in out.split("## 2026-09-02")[0]


def test_log_entry_survives_a_log_with_no_sections_yet():
    out = of.prepend_log_entry("# F3E pipeline log\n", "## new\n- x")
    assert "## new" in out and out.startswith("# F3E pipeline log")


def test_log_write_is_fail_soft(monkeypatch):
    """A record must never turn a real publish into a reported failure."""
    monkeypatch.setattr(of.drive_io, "read_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("mount gone")))
    of.append_pipeline_log("## x")  # must not raise


# ---------------------------------------------------------------------------
# Drive discipline
# ---------------------------------------------------------------------------


def test_every_drive_touch_goes_through_drive_io():
    """A raw pathlib read against a blipped G: mount can BLOCK the calling
    thread, and this module is imported by the always-on bot process."""
    src = inspect.getsource(of)
    for banned in (".read_text()", ".write_text(", "open(", "os.path.exists"):
        assert banned not in src, banned
    assert "drive_io.read_text" in src and "drive_io.write_text_atomic" in src


def test_drive_root_is_overridable():
    assert callable(of.drive_root)
    assert of.BLOG_LEARN.endswith("122516767040")
    assert of.BLOG_NEWS.endswith("97115373888")
