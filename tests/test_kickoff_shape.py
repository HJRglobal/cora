"""Code #15 S6 (cq-3a29e7dc3953) -- ONE fixed shape for every auto-generated kickoff.

Measured on the 12 kickoffs staged 2026-09-21 (and the 96 since 8/05): the model
wrapped 92 of 96 in a ```markdown fence it was never asked for, 15 of them
UNCLOSED -- every unclosed one a max_tokens cut (output == 2000 on the llm_usage
line at its staged time). No file carried a STATUS line, so the Cowork Friday
unfired-work sweep had nothing to read; 4 of 12 had no H1, the rest were model
paraphrases, and the skeleton's H1 was the SLUG.

Contract under test:
  * code owns the header: line 1 = ``STATUS: STAGED <ISO AZ> · via <door> ·
    fire-owner: Harrison · fire-or-park: <next Fri 16:00 AZ>``; exactly one H1 =
    the LEX-safe item title (a bundle: theme + count); the banner; a Queue line;
  * the model's OUTER wrapper is stripped (never an inner block), its preamble is
    dropped, a STATUS-looking model line is neutralized;
  * a truncated reply is never hidden: stop_reason == max_tokens OR an unclosed
    wrapper -> a TRUNCATED trailer in the file + ``truncated: true`` on the ledger;
  * a pre-write shape gate (off-shape model body -> the skeleton) and a post-write
    read-back of line 1 (mismatch -> None, never a raise, no `staged` event);
  * every `staged` event in code_queue.py names its door (C13-09).

Fixtures (tests/fixtures/kickoffs_2026_09_21/): stamp-stripped EXCERPTS of the 11
non-LEX 9/21 files (line 1 dropped only when it was a Cowork ``<!--`` stamp; the
head through the first ``## `` heading, then the last 3 lines), read ONCE from
the Founder-OS _notes folder by the S6 build. The LEX file (1849b9) is a SYNTHETIC
body -- no content of it is committed. index.json carries {cq_id, via, truncated,
title (LEX-safe)} from the live ledger + the output=2000 usage lines. This test
never reads G:.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import code_queue as cq  # noqa: E402

from test_code_queue import qenv  # noqa: E402,F401  -- shared isolation fixture

HARRISON = "U0B2RM2JYJ1"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "kickoffs_2026_09_21"
INDEX = json.loads((FIXTURE_DIR / "index.json").read_text(encoding="utf-8"))
# 2026-09-21 08:20:51 AZ -- the 68c8e8 staged time (a Monday).
MON_0820_AZ = datetime(2026, 9, 21, 15, 20, 51, tzinfo=timezone.utc)
FENCE = "`" * 3


def _pin_now(monkeypatch, when=MON_0820_AZ):
    monkeypatch.setattr(cq, "_now", lambda: when)


def _fake_anthropic(monkeypatch, text, *, stop_reason="end_turn", calls=None, boom=None):
    """The test_batch_adoption fake-client pattern, plus stop_reason."""
    import anthropic

    class _Messages:
        def create(self, **kw):
            if calls is not None:
                calls.append(kw)
            if boom is not None:
                raise boom
            return SimpleNamespace(
                model="claude-sonnet-5", stop_reason=stop_reason,
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=10, cache_creation_input_tokens=0,
                                      cache_read_input_tokens=0, output_tokens=5))

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _seed(title="Widget renders wrong", severity="P2", entity="F3E"):
    return cq.seed_item(kind="bug", severity=severity, title=title, summary="the body",
                        entity=entity, signal="explicit", status="APPROVED")


def _replay(cq_id, title, entity="FNDR"):
    """A captured row with a chosen id + title (the fixture's live-ledger identity)."""
    cq._append_event({"event": "captured", "id": cq_id, "ts": "2026-09-21T15:00:00+00:00",
                      "status": "APPROVED", "kind": "bug", "severity": "P2", "title": title,
                      "summary": "fixture body", "entity": entity, "signal": "explicit",
                      "count": 1, "reporter": HARRISON, "seeded": True,
                      "evidence": [{"channel_id": "", "ts": "", "note": "fixture body"}]})
    return cq_id


def _staged_events(cid):
    return [e for e in cq._read_jsonl(cq._EVENT_LEDGER)
            if e.get("event") == "staged" and e.get("id") == cid]


MODEL_BODY = "\n".join([
    "## 0. Evidence", "- copied evidence", "",
    "## 1. Deliverables", "- Slice 1: do it", "",
    "## 2. Guardrails", "- D-011", "",
    "## 3. Tests", "- tests", "",
    "## 4. Live acceptance (Harrison)", "- smoke", "",
    "## 5. Notes", "- restart: yes",
])


def _wrapped(body=MODEL_BODY, *, close=True, preamble=True):
    lines = [FENCE + "markdown"]
    if preamble:
        lines += ["# A model paraphrase of the title", "",
                  "**Model:** Opus-tier · STANDING OPERATING LOOP applies",
                  "**AUTO-GENERATED DRAFT -- VERIFY-FIRST everything**",
                  "Suggested branch: `claude/some-slug`", "", "---", ""]
    lines += body.splitlines()
    if close:
        lines.append(FENCE)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 1. _strip_model_fences -- outer wrapper only; an unclosed wrapper is flagged
# ─────────────────────────────────────────────────────────────────────────────
class TestStripModelFences:
    def test_leading_only_is_flagged_unclosed(self):
        body, unclosed = cq._strip_model_fences(FENCE + "markdown\n## 0\nx")
        assert body == "## 0\nx" and unclosed is True

    def test_leading_and_trailing(self):
        body, unclosed = cq._strip_model_fences(FENCE + "markdown\n## 0\nx\n" + FENCE)
        assert body == "## 0\nx" and unclosed is False

    def test_no_fence_is_unchanged(self):
        text = "## 0\nx\n"
        assert cq._strip_model_fences(text) == (text, False)

    def test_unclosed_mid_word(self):
        body, unclosed = cq._strip_model_fences(FENCE + "markdown\n## 0\n- Un")
        assert body.endswith("- Un") and unclosed is True

    def test_trailing_only_fence_closes_an_inner_block_and_is_untouched(self):
        text = "## 0\n" + FENCE + "python\nx = 1\n" + FENCE
        assert cq._strip_model_fences(text) == (text, False)

    def test_inner_block_survives_a_stripped_wrapper(self):
        text = FENCE + "markdown\n## 0\n" + FENCE + "python\nx = 1\n" + FENCE + "\n## 1\ny\n" + FENCE
        body, unclosed = cq._strip_model_fences(text)
        assert unclosed is False
        assert body == "## 0\n" + FENCE + "python\nx = 1\n" + FENCE + "\n## 1\ny"

    @pytest.mark.parametrize("opener", [FENCE + "md", FENCE, FENCE + "Markdown", "  " + FENCE + " markdown  "])
    def test_md_and_bare_openers(self, opener):
        body, unclosed = cq._strip_model_fences(f"{opener}\n## 0\nx\n{FENCE}")
        assert body == "## 0\nx" and unclosed is False

    def test_leading_blank_lines(self):
        body, unclosed = cq._strip_model_fences("\n\n  \n" + FENCE + "markdown\n## 0\nx\n" + FENCE + "\n\n")
        assert body == "## 0\nx" and unclosed is False

    def test_final_fence_closing_an_inner_block_leaves_the_wrapper_open(self):
        """A reply cut right after an inner block closed: the last bare fence is the
        INNER closer, so the wrapper never closed -- the truncation belt must fire."""
        text = FENCE + "markdown\n## 0\n" + FENCE + "python\nx = 1\n" + FENCE
        body, unclosed = cq._strip_model_fences(text)
        assert unclosed is True
        assert body == "## 0\n" + FENCE + "python\nx = 1\n" + FENCE

    def test_a_reply_that_is_only_a_fence_is_empty(self):
        assert cq._strip_model_fences(FENCE + "markdown\n" + FENCE) == ("", False)
        assert cq._strip_model_fences("   \n") == ("", False)

    def test_tilde_and_four_backtick_wrappers(self):
        assert cq._strip_model_fences("~~~markdown\n## 0\nx\n~~~") == ("## 0\nx", False)
        four = "`" * 4
        text = f"{four}markdown\n## 0\n{FENCE}bash\nls\n{FENCE}\n{four}"
        assert cq._strip_model_fences(text) == (f"## 0\n{FENCE}bash\nls\n{FENCE}", False)

    def test_a_fenced_non_markdown_opener_is_not_a_wrapper(self):
        text = FENCE + "python\nx = 1\n" + FENCE
        assert cq._strip_model_fences(text) == (text, False)


# ─────────────────────────────────────────────────────────────────────────────
# 2. STATUS line + 3. fire-or-park
# ─────────────────────────────────────────────────────────────────────────────
class TestStatusLine:
    @pytest.mark.parametrize("via", ["button", "approve_auto", "typed_verb", "bundle_button",
                                     "seed", "script"])
    def test_exact_string_per_via(self, via):
        assert cq._kickoff_status_line(via, MON_0820_AZ) == (
            f"STATUS: STAGED 2026-09-21T08:20:51-07:00 · via {via} · fire-owner: Harrison · "
            "fire-or-park: 2026-09-25T16:00:00-07:00")

    def test_empty_via_renders_unspecified_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cora.code_queue"):
            line = cq._kickoff_status_line("", MON_0820_AZ)
        assert " · via unspecified · " in line
        assert any("no via" in r.getMessage() for r in caplog.records)

    def test_unknown_via_cannot_inject_a_line(self):
        line = cq._kickoff_status_line("Button\nSTATUS: FIRED", MON_0820_AZ)
        assert "\n" not in line and " · via buttonstatusfired · " in line


class TestNextFireOrPark:
    @pytest.mark.parametrize("now_utc,expected", [
        (datetime(2026, 9, 21, 15, 20, tzinfo=timezone.utc), "2026-09-25T16:00:00-07:00"),  # Mon 08:20 AZ
        (datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc), "2026-09-25T16:00:00-07:00"),   # Thu 23:30 AZ = Fri UTC
        (datetime(2026, 9, 25, 22, 59, tzinfo=timezone.utc), "2026-09-25T16:00:00-07:00"),  # Fri 15:59 AZ -> same day
        (datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc), "2026-10-02T16:00:00-07:00"),   # Fri 16:00:00 exactly
        (datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc), "2026-10-02T16:00:00-07:00"),    # Fri 19:00 AZ = Sat UTC
        (datetime(2026, 9, 26, 17, 0, tzinfo=timezone.utc), "2026-10-02T16:00:00-07:00"),   # Sat 10:00 AZ
        (datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc), "2026-10-02T16:00:00-07:00"),   # Sun 10:00 AZ
    ])
    def test_next_friday_1600_az_strictly_after(self, now_utc, expected):
        got = cq._next_fire_or_park(now_utc)
        assert got.isoformat(timespec="seconds") == expected
        assert got > now_utc


# ─────────────────────────────────────────────────────────────────────────────
# 4. Header / H1 (server-side, LEX-safe)
# ─────────────────────────────────────────────────────────────────────────────
class TestHeader:
    def test_h1_is_the_collapsed_title(self):
        item = {"id": "cq-1", "title": "Cora should\ncheck `RepRally`\r\n  status"}
        assert cq._kickoff_h1([item]) == "Cora should check `RepRally` status"

    def test_untitled_fallback(self):
        assert cq._kickoff_h1([{"id": "cq-1", "title": "  "}]) == "(untitled)"

    def test_lex_item_renders_the_redaction_even_from_a_raw_record(self, qenv):  # noqa: F811
        raw = {"id": "cq-lex1", "title": "raw LEX client text", "entity": "LEX-LLC"}
        assert cq._kickoff_h1([raw]) == cq._LEX_REDACTED_TITLE
        cid = _seed(title="some lex build ask", entity="LEX")
        assert cq._kickoff_h1([cq.get_item(cid)]) == cq._LEX_REDACTED_TITLE

    def test_bundle_h1_names_theme_never_a_member_title(self):
        items = [{"id": f"cq-{i}", "title": f"secret member title {i}", "subsystem_guess": "shopify",
                  "entity": "F3E"} for i in range(3)]
        h1 = cq._kickoff_h1(items)
        assert h1 == "Bundle: shopify (3 items)"
        assert "member title" not in h1

    def test_header_block_order(self):
        item = {"id": "cq-1", "title": "Widget", "severity": "P1", "entity": "F3E", "kind": "bug"}
        hdr = cq._kickoff_header([item], "widget", "button", MON_0820_AZ)
        assert hdr[0].startswith("STATUS: STAGED 2026-09-21T08:20:51-07:00 · via button")
        assert hdr[1] == "" and hdr[2] == "# Widget"
        assert "AUTO-GENERATED DRAFT -- VERIFY-FIRST everything" in hdr[4]
        assert "`claude/widget` off `main`" in hdr[4]
        assert hdr[6] == "Queue: `cq-1` (P1, F3E, bug)"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Shape invariant
# ─────────────────────────────────────────────────────────────────────────────
def _valid(body_lines=None, h1="Widget"):
    hdr = cq._kickoff_header([{"id": "cq-1", "title": h1, "severity": "P2", "entity": "F3E",
                               "kind": "bug"}], "widget", "button", MON_0820_AZ)
    return "\n".join(hdr + (body_lines if body_lines is not None else MODEL_BODY.splitlines()))


class TestShapeInvariant:
    def test_valid_passes(self):
        assert cq._kickoff_shape_errors(_valid(), "Widget") == []

    def test_missing_status_fails(self):
        text = _valid().split("\n", 1)[1]
        assert any("STATUS" in e for e in cq._kickoff_shape_errors(text, "Widget"))

    def test_wrapper_at_the_top_fails(self):
        text = _valid([FENCE + "markdown"] + MODEL_BODY.splitlines() + [FENCE])
        assert any("wraps the body" in e for e in cq._kickoff_shape_errors(text, "Widget"))

    def test_unbalanced_fence_fails(self):
        text = _valid(MODEL_BODY.splitlines() + [FENCE + "python", "x = 1"])
        assert any("unbalanced" in e for e in cq._kickoff_shape_errors(text, "Widget"))

    def test_a_balanced_inner_code_block_passes(self):
        """The deliberate departure from the spec's "no line == ```" rule."""
        text = _valid(MODEL_BODY.splitlines() + [FENCE + "bash", "# a comment, not an H1",
                                                 "pytest -q", FENCE])
        assert cq._kickoff_shape_errors(text, "Widget") == []

    def test_a_second_h1_fails(self):
        text = _valid(MODEL_BODY.splitlines() + ["# A second title"])
        assert any("2 H1" in e for e in cq._kickoff_shape_errors(text, "Widget"))

    def test_an_h1_that_is_not_the_title_fails(self):
        assert cq._kickoff_shape_errors(_valid(), "Something else")

    def test_normalize_drops_preamble_and_neutralizes_status(self):
        body, info = cq._normalize_model_body(
            _wrapped(MODEL_BODY + "\n**STATUS:** FIRED 2026-09-25\nstatus : parked"))
        assert body.startswith("## 0. Evidence")
        assert "# A model paraphrase" not in body and "Suggested branch" not in body
        assert info["preamble_dropped"] > 0 and info["status_neutralized"] == 2
        assert not any(cq._STATUS_LIKE_RE.match(ln) for ln in body.splitlines())
        assert "Status -- FIRED 2026-09-25" in body

    def test_normalize_keeps_everything_without_a_section_heading(self):
        body, info = cq._normalize_model_body("just prose\nmore prose")
        assert body == "just prose\nmore prose" and info["preamble_dropped"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 6. The generator end to end (qenv: FOUNDER_OS_ROOT is NOT globally redirected)
# ─────────────────────────────────────────────────────────────────────────────
class TestGenerator:
    def test_wrapped_reply_becomes_the_fixed_shape(self, qenv, monkeypatch):  # noqa: F811
        _pin_now(monkeypatch)
        calls: list[dict] = []
        _fake_anthropic(monkeypatch, _wrapped(), calls=calls)
        cid = _seed(title="Widget renders wrong")
        meta: dict = {}
        path = cq.generate_kickoff_prompt([cq.get_item(cid)], meta_out=meta, via="button")
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0].startswith("STATUS: STAGED 2026-09-21T08:20:51-07:00 · via button · "
                                   "fire-owner: Harrison · fire-or-park: 2026-09-25T16:00:00-07:00")
        assert [ln for ln in lines if ln.startswith("# ")] == ["# Widget renders wrong"]
        assert not any(cq._WRAPPER_OPEN_RE.match(ln) for ln in lines)
        assert lines.count("## 0. Evidence") == 1
        assert "# A model paraphrase of the title" not in text
        assert cq._kickoff_shape_errors(text, "Widget renders wrong") == []
        assert "truncated" not in meta and "shape_fallback" not in meta
        # the model is asked for the BODY only, with room to finish
        kw = calls[0]
        assert kw["max_tokens"] == 4096
        assert "Slug:" not in kw["messages"][0]["content"]
        system = " ".join(kw["system"].split())
        assert "Do NOT wrap your reply in a code fence" in system
        assert "Do NOT write a title" in system and "STATUS line" in system

    def test_max_tokens_marks_the_file_and_the_ledger(self, qenv, monkeypatch):  # noqa: F811
        _pin_now(monkeypatch)
        _fake_anthropic(monkeypatch, _wrapped(close=True), stop_reason="max_tokens")
        cid = _seed(title="Cut off kickoff")
        outcome, path = cq.ensure_kickoff_staged(cid, via="approve_auto")
        assert outcome == "staged"
        text = Path(path).read_text(encoding="utf-8")
        assert text.rstrip().splitlines()[-1] == cq._KICKOFF_TRUNCATED_TRAILER
        ev = _staged_events(cid)[-1]
        assert ev["truncated"] is True and ev["via"] == "approve_auto"

    def test_unclosed_wrapper_is_the_truncation_belt(self, qenv, monkeypatch):  # noqa: F811
        """stop_reason says end_turn but the wrapper never closed: still marked."""
        _pin_now(monkeypatch)
        _fake_anthropic(monkeypatch, _wrapped(close=False), stop_reason="end_turn")
        meta: dict = {}
        path = cq.generate_kickoff_prompt([cq.get_item(_seed(title="Belt"))], meta_out=meta,
                                          via="button")
        assert meta.get("truncated") is True
        assert cq._KICKOFF_TRUNCATED_TRAILER in Path(path).read_text(encoding="utf-8")

    def test_cut_inside_a_code_block_closes_it_before_the_trailer(self, qenv, monkeypatch):  # noqa: F811
        _pin_now(monkeypatch)
        body = MODEL_BODY + "\n" + FENCE + "python\nx = 1"
        _fake_anthropic(monkeypatch, _wrapped(body, close=False), stop_reason="max_tokens")
        meta: dict = {}
        path = cq.generate_kickoff_prompt([cq.get_item(_seed(title="Mid block"))], meta_out=meta,
                                          via="button")
        text = Path(path).read_text(encoding="utf-8")
        assert meta.get("truncated") is True and "shape_fallback" not in meta
        assert cq._kickoff_shape_errors(text, "Mid block") == []
        tail = text.rstrip().splitlines()
        assert tail[-1] == cq._KICKOFF_TRUNCATED_TRAILER and tail[-3] == FENCE

    def test_off_shape_model_body_falls_back_to_the_skeleton(self, qenv, monkeypatch, caplog):  # noqa: F811
        _pin_now(monkeypatch)
        _fake_anthropic(monkeypatch, _wrapped(MODEL_BODY + "\n# A stray second title"))
        meta: dict = {}
        with caplog.at_level(logging.WARNING, logger="cora.code_queue"):
            path = cq.generate_kickoff_prompt([cq.get_item(_seed(title="Stray"))], meta_out=meta,
                                              via="button")
        text = Path(path).read_text(encoding="utf-8")
        assert meta["shape_fallback"] and "stray second title" not in text.lower()
        assert "## 5. Notes" in text and cq._kickoff_shape_errors(text, "Stray") == []
        assert any("off-shape" in r.getMessage() for r in caplog.records)

    def test_model_status_line_is_neutralized(self, qenv, monkeypatch):  # noqa: F811
        _pin_now(monkeypatch)
        _fake_anthropic(monkeypatch, _wrapped("## 0. Evidence\nSTATUS: FIRED 2026-09-25\n" + MODEL_BODY))
        path = cq.generate_kickoff_prompt([cq.get_item(_seed(title="No fake status"))], via="seed")
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        assert [ln for ln in lines if cq._STATUS_LIKE_RE.match(ln)] == [lines[0]]

    def test_model_crash_writes_the_skeleton_with_the_same_header(self, qenv, monkeypatch):  # noqa: F811
        _pin_now(monkeypatch)
        _fake_anthropic(monkeypatch, "", boom=RuntimeError("sonnet down"))
        path = cq.generate_kickoff_prompt([cq.get_item(_seed(title="Skeleton"))], via="typed_verb")
        text = Path(path).read_text(encoding="utf-8")
        assert text.startswith("STATUS: STAGED 2026-09-21T08:20:51-07:00 · via typed_verb")
        assert cq._kickoff_shape_errors(text, "Skeleton") == []

    def test_skeleton_survives_multiline_item_text(self, qenv):  # noqa: F811
        """A typed ask's title/summary can hold newlines; a continuation that starts
        with '# ' or a fence must not break the skeleton's own gate (-> no stage)."""
        cid = cq.seed_item(kind="bug", severity="P2", title="Multi line ask",
                           summary="first\n# looks like a heading\n" + FENCE + "\nSTATUS: FIRED",
                           entity="F3E", signal="explicit", status="APPROVED")
        outcome, path = cq.ensure_kickoff_staged(cid, via="button")
        assert outcome == "staged"
        text = Path(path).read_text(encoding="utf-8")
        assert cq._kickoff_shape_errors(text, "Multi line ask") == []

    def test_read_back_mismatch_is_an_error_with_no_staged_event(self, qenv, monkeypatch, caplog):  # noqa: F811
        _pin_now(monkeypatch)
        monkeypatch.setattr(cq.drive_io, "read_text", lambda *a, **k: "something else\n")
        cid = _seed(title="Read back me")
        with caplog.at_level(logging.ERROR, logger="cora.code_queue"):
            outcome, detail = cq.ensure_kickoff_staged(cid, via="button")
        assert outcome == "error" and "no file" in detail and "line 1" in detail
        assert _staged_events(cid) == []
        assert cq.get_item(cid)["status"] == "APPROVED"
        assert cid not in cq._STAGING_INFLIGHT  # the reservation was released
        # the orphan is findable from the log by its id suffix, and the log carries
        # no title-derived text (D-082): not the title, not the filename slug
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "read-back FAILED" in logged and cid in logged
        suffix = cq._id_suffix([{"id": cid}])
        assert f"*-{suffix}.md" in logged
        assert "Read back me" not in logged and "read-back-me" not in logged

    def test_read_back_exception_is_an_error_never_a_raise(self, qenv, monkeypatch):  # noqa: F811
        def _boom(*a, **k):
            raise cq.drive_io.DriveUnavailable("mount gone after write")
        monkeypatch.setattr(cq.drive_io, "read_text", _boom)
        cid = _seed(title="Unreadable")
        outcome, detail = cq.process_queue_action(cq.ACTION_STAGE, cid, HARRISON)
        assert outcome == "error" and "nothing staged" in detail and "read-back failed" in detail
        assert _staged_events(cid) == []

    def test_mis_homed_path_is_read_back_from_the_repo_notes(self, qenv, monkeypatch):  # noqa: F811
        def _raise(*a, **k):
            raise cq.drive_io.DriveUnavailable("mount gone")

        def _never(*a, **k):
            raise AssertionError("a mis-homed prompt must be read back from the repo path")
        monkeypatch.setattr(cq.drive_io, "write_text_atomic", _raise)
        monkeypatch.setattr(cq.drive_io, "read_text", _never)
        cid = _seed(title="Mis-homed read back")
        outcome, path = cq.ensure_kickoff_staged(cid, via="button")
        assert outcome == "staged"
        assert _staged_events(cid)[-1].get("mis_homed") is True
        assert Path(path).read_text(encoding="utf-8").startswith("STATUS: STAGED ")

    def test_mis_homed_read_back_mismatch_is_caught(self, qenv, monkeypatch):  # noqa: F811
        def _bad_write(body, fname):
            p = cq._NOTES_DIR / fname
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# not what was generated\n", encoding="utf-8")
            return str(p), True
        monkeypatch.setattr(cq, "_write_prompt_file", _bad_write)
        meta: dict = {}
        assert cq.generate_kickoff_prompt([cq.get_item(_seed(title="Bad"))], meta_out=meta,
                                          via="button") is None
        assert "line 1" in meta["error"]

    def test_total_write_failure_names_its_reason(self, qenv, monkeypatch):  # noqa: F811
        monkeypatch.setattr(cq, "_write_prompt_file", lambda body, fname: (None, False))
        cid = _seed(title="Nowhere to write")
        outcome, detail = cq.ensure_kickoff_staged(cid, via="button")
        assert outcome == "error" and "no file" in detail and "both targets" in detail

    def test_no_via_is_recorded_as_unspecified(self, qenv, monkeypatch):  # noqa: F811
        cid = _seed(title="Door unknown")
        outcome, path = cq.ensure_kickoff_staged(cid)
        assert outcome == "staged"
        assert _staged_events(cid)[-1]["via"] == "unspecified"
        assert " · via unspecified · " in Path(path).read_text(encoding="utf-8").splitlines()[0]


# ─────────────────────────────────────────────────────────────────────────────
# 7. The 12 kickoffs staged 2026-09-21 (stamp-stripped excerpts; LEX synthetic)
# ─────────────────────────────────────────────────────────────────────────────
def test_fixture_index_covers_the_twelve():
    assert len(INDEX) == 12
    assert {s for s, v in INDEX.items() if v["truncated"]} == {"c30f51", "796adf", "23ce76"}
    assert INDEX["1849b9"]["title"] == cq._LEX_REDACTED_TITLE and INDEX["1849b9"].get("synthetic")
    for suffix in INDEX:
        assert (FIXTURE_DIR / f"{suffix}.md").is_file(), suffix
        # every excerpt is raw generator output shape: the wrapper on line 1
        first = (FIXTURE_DIR / f"{suffix}.md").read_text(encoding="utf-8").splitlines()[0]
        assert cq._WRAPPER_OPEN_RE.match(first), suffix


@pytest.mark.parametrize("belt_only", [False, True], ids=["stop_reason", "unclosed-belt"])
@pytest.mark.parametrize("suffix", sorted(INDEX))
def test_fixture_reply_renders_the_fixed_shape(qenv, monkeypatch, suffix, belt_only):  # noqa: F811
    row = INDEX[suffix]
    _pin_now(monkeypatch)
    raw = (FIXTURE_DIR / f"{suffix}.md").read_text(encoding="utf-8")
    stop = "max_tokens" if (row["truncated"] and not belt_only) else "end_turn"
    _fake_anthropic(monkeypatch, raw, stop_reason=stop)
    entity = "LEX" if row.get("synthetic") else "FNDR"
    cid = _replay(row["cq_id"], row["title"] if entity != "LEX" else "synthetic lex ask", entity)
    item = cq.get_item(cid)
    meta: dict = {}
    path = cq.generate_kickoff_prompt([item], meta_out=meta, via=row["via"])
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    expected_h1 = " ".join(row["title"].split())
    assert lines[0].startswith("STATUS: STAGED 2026-09-21T08:20:51-07:00 · via " + row["via"] + " · ")
    assert cq._kickoff_shape_errors(text, expected_h1) == [], suffix
    assert lines[2] == f"# {expected_h1}"
    assert not any(cq._WRAPPER_OPEN_RE.match(ln) for ln in lines)
    assert "shape_fallback" not in meta, meta
    # the model preamble (H1 paraphrase / byline / banner / branch) is gone: the
    # header's Queue line is followed directly by the model's Section 0
    assert lines[6] == cq._kickoff_queue_line([item]) and lines[8] == "## 0. Evidence"
    assert (cq._KICKOFF_TRUNCATED_TRAILER in text) is row["truncated"]
    assert bool(meta.get("truncated")) is row["truncated"]


def test_fixture_8b5b6c_h1_is_the_title_not_the_slug(qenv, monkeypatch):  # noqa: F811
    row = INDEX["8b5b6c"]
    _fake_anthropic(monkeypatch, (FIXTURE_DIR / "8b5b6c.md").read_text(encoding="utf-8"))
    cid = _replay(row["cq_id"], row["title"])
    path = cq.generate_kickoff_prompt([cq.get_item(cid)], via=row["via"])
    h1 = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.startswith("# ")]
    assert h1 == ["# " + row["title"]]
    assert "slack-asana-task-cards-render-a-literal-badge-in" not in h1[0]


def test_fixture_lex_file_never_egresses_a_raw_title(qenv, monkeypatch):  # noqa: F811
    row = INDEX["1849b9"]
    _fake_anthropic(monkeypatch, (FIXTURE_DIR / "1849b9.md").read_text(encoding="utf-8"))
    # a LEGACY raw LEX title at rest: the H1 still reads the redaction
    cq._append_event({"event": "captured", "id": row["cq_id"], "ts": "2026-09-21T15:00:00+00:00",
                      "status": "APPROVED", "kind": "feature", "severity": "P2",
                      "title": "raw legacy lex client words", "summary": "raw legacy lex client words",
                      "entity": "LEX", "signal": "explicit", "count": 1,
                      "evidence": [{"channel_id": "C0LEX", "ts": ""}]})
    path = cq.generate_kickoff_prompt([cq.get_item(row["cq_id"])], via=row["via"])
    text = Path(path).read_text(encoding="utf-8")
    assert "raw legacy lex client words" not in text
    assert text.splitlines()[2] == f"# {cq._LEX_REDACTED_TITLE}"


# ─────────────────────────────────────────────────────────────────────────────
# C13-09 -- every `staged` event names its door
# ─────────────────────────────────────────────────────────────────────────────
def test_every_staged_event_literal_carries_via():
    tree = ast.parse(inspect.getsource(cq))
    staged = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value: v for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        ev = keys.get("event")
        if isinstance(ev, ast.Constant) and ev.value == "staged":
            staged.append(node)
            assert "via" in keys, f"a `staged` event literal at line {node.lineno} has no via"
    # ensure_kickoff_staged, record_staged, stage_bundle, apply_prompt_rehome
    assert len(staged) == 4


def test_record_staged_and_rehome_record_via_script(qenv, tmp_path, monkeypatch):  # noqa: F811
    cid = _seed(title="Hand written kickoff")
    assert cq.record_staged(cid, "/somewhere/prompt.md", HARRISON)[0] == "staged"
    assert _staged_events(cid)[-1]["via"] == "script"
    # rehome: a repo-_notes prompt moved to Founder-OS
    src = cq._NOTES_DIR / "2026-09-21_fndr_cora-code-prompt-x-abc123.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("STATUS: STAGED x\n", encoding="utf-8")
    cid2 = _seed(title="Rehome me")
    cq._append_event({"event": "staged", "ts": cq._now_iso(), "id": cid2, "prompt_path": str(src),
                      "via": "button"})
    done = cq.apply_prompt_rehome(cq.plan_prompt_rehome())
    assert done and all(d["ok"] for d in done)
    assert _staged_events(cid2)[-1]["via"] == "script" and _staged_events(cid2)[-1]["rehomed"] is True
