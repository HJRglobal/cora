"""Unit tests for dynamic_answers.load_dynamic_answers()."""

import logging
import os
import time
from textwrap import dedent

import pytest

import cora.dynamic_answers as da


def _clear_cache():
    da._cache.clear()


@pytest.fixture(autouse=True)
def _reset_cache():
    _clear_cache()
    yield
    _clear_cache()


@pytest.fixture
def dynamic_root(monkeypatch, tmp_path):
    """Monkeypatches _DYNAMIC_DIR and _REPO_ROOT to tmp_path, creates the dir."""
    root = tmp_path / "design" / "known-answers" / "dynamic"
    root.mkdir(parents=True)
    monkeypatch.setattr(da, "_DYNAMIC_DIR", root)
    monkeypatch.setattr(da, "_REPO_ROOT", tmp_path)
    return root


def _write_answer_yaml(entity_dir, name, *, topic, template, fallback, snapshot_path, threshold_hours=24):
    (entity_dir / name).write_text(
        dedent(f"""\
            topic: {topic}
            template: "{template}"
            fallback: "{fallback}"
            snapshot_path: {snapshot_path}
            source:
              staleness_threshold_hours: {threshold_hours}
        """),
        encoding="utf-8",
    )


def _make_snapshot(tmp_path, rel_path, content):
    snap = tmp_path / rel_path
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(content, encoding="utf-8")
    return snap


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_load_dynamic_answer_basic(dynamic_root, tmp_path):
    entity_dir = dynamic_root / "F3E"
    entity_dir.mkdir()

    _make_snapshot(tmp_path, "data/snapshots/f3e/status.yaml", "status: healthy\nowner: Alice")
    _write_answer_yaml(
        entity_dir, "status.yaml",
        topic="Pipeline Status",
        template="Pipeline is {status}, owned by {owner}.",
        fallback="Status unavailable.",
        snapshot_path="data/snapshots/f3e/status.yaml",
    )

    result = da.load_dynamic_answers("F3E")
    assert "Pipeline is healthy, owned by Alice." in result


def test_dynamic_block_is_capped(dynamic_root, tmp_path, caplog):
    # D-084: a pathologically large rendered block is truncated so it can never
    # balloon the cached static context toward the 200K input ceiling.
    entity_dir = dynamic_root / "F3E"
    entity_dir.mkdir()
    huge = "X" * (da._MAX_DYNAMIC_CHARS + 50_000)
    _make_snapshot(tmp_path, "data/snapshots/f3e/big.yaml", f"blob: {huge}")
    _write_answer_yaml(
        entity_dir, "big.yaml",
        topic="Big", template="Value: {blob}", fallback="none",
        snapshot_path="data/snapshots/f3e/big.yaml",
    )
    with caplog.at_level(logging.WARNING, logger="cora.dynamic_answers"):
        result = da.load_dynamic_answers("F3E")
    assert len(result) <= da._MAX_DYNAMIC_CHARS + 100  # cap + truncation note
    assert "truncated to fit the context budget" in result
    assert "truncating" in caplog.text


# ---------------------------------------------------------------------------
# Stale snapshot → fallback
# ---------------------------------------------------------------------------

def test_load_dynamic_answer_uses_fallback_on_stale(dynamic_root, tmp_path, caplog):
    entity_dir = dynamic_root / "F3E"
    entity_dir.mkdir()

    snap = _make_snapshot(tmp_path, "data/snapshots/f3e/status.yaml", "status: healthy")
    old_mtime = time.time() - 25 * 3600
    os.utime(snap, (old_mtime, old_mtime))

    _write_answer_yaml(
        entity_dir, "status.yaml",
        topic="Pipeline Status",
        template="Pipeline is {status}.",
        fallback="Status data is temporarily unavailable.",
        snapshot_path="data/snapshots/f3e/status.yaml",
        threshold_hours=24,
    )

    with caplog.at_level(logging.WARNING, logger="cora.dynamic_answers"):
        result = da.load_dynamic_answers("F3E")

    assert "Status data is temporarily unavailable." in result
    assert "stale" in caplog.text


# ---------------------------------------------------------------------------
# Missing snapshot → fallback
# ---------------------------------------------------------------------------

def test_load_dynamic_answer_uses_fallback_on_missing_snapshot(dynamic_root, tmp_path, caplog):
    entity_dir = dynamic_root / "F3E"
    entity_dir.mkdir()

    # Intentionally do NOT create the snapshot file
    _write_answer_yaml(
        entity_dir, "status.yaml",
        topic="Pipeline Status",
        template="Pipeline is {status}.",
        fallback="Snapshot not found, using fallback.",
        snapshot_path="data/snapshots/f3e/missing.yaml",
    )

    with caplog.at_level(logging.WARNING, logger="cora.dynamic_answers"):
        result = da.load_dynamic_answers("F3E")

    assert "Snapshot not found, using fallback." in result
    assert "missing" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Malformed YAML → skip, error logged, no crash
# ---------------------------------------------------------------------------

def test_load_dynamic_answer_handles_malformed_yaml(dynamic_root, caplog):
    entity_dir = dynamic_root / "F3E"
    entity_dir.mkdir()

    (entity_dir / "broken.yaml").write_text(
        "topic: [unclosed bracket\n  bad: : :\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR, logger="cora.dynamic_answers"):
        result = da.load_dynamic_answers("F3E")

    assert result == ""
    assert "malformed" in caplog.text.lower() or "error" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Missing template field → fallback
# ---------------------------------------------------------------------------

def test_load_dynamic_answer_handles_missing_template_field(dynamic_root, tmp_path, caplog):
    entity_dir = dynamic_root / "F3E"
    entity_dir.mkdir()

    # Snapshot has 'status' but template references {nonexistent_field}
    _make_snapshot(tmp_path, "data/snapshots/f3e/status.yaml", "status: healthy")
    _write_answer_yaml(
        entity_dir, "status.yaml",
        topic="Pipeline Status",
        template="Value is {nonexistent_field}.",
        fallback="Field missing fallback text.",
        snapshot_path="data/snapshots/f3e/status.yaml",
    )

    with caplog.at_level(logging.WARNING, logger="cora.dynamic_answers"):
        result = da.load_dynamic_answers("F3E")

    assert "Field missing fallback text." in result
    assert "missing" in caplog.text.lower() or "nonexistent_field" in caplog.text


# ---------------------------------------------------------------------------
# Cross-entity isolation
# ---------------------------------------------------------------------------

def test_load_dynamic_answer_cross_entity_isolation(dynamic_root, tmp_path):
    for entity in ("F3E", "OSN"):
        (dynamic_root / entity).mkdir()

    _make_snapshot(tmp_path, "data/snapshots/f3e/status.yaml", "status: f3e-specific")
    _write_answer_yaml(
        dynamic_root / "F3E", "status.yaml",
        topic="F3E Status",
        template="F3E: {status}",
        fallback="unavailable",
        snapshot_path="data/snapshots/f3e/status.yaml",
    )
    # OSN dir is empty — no yaml files

    f3e_result = da.load_dynamic_answers("F3E")
    osn_result = da.load_dynamic_answers("OSN")

    assert "f3e-specific" in f3e_result
    assert osn_result == ""
    assert "f3e-specific" not in osn_result


# ---------------------------------------------------------------------------
# Empty entity directory → empty string
# ---------------------------------------------------------------------------

def test_load_dynamic_answer_empty_directory(dynamic_root):
    entity_dir = dynamic_root / "F3E"
    entity_dir.mkdir()
    # No YAML files placed

    result = da.load_dynamic_answers("F3E")
    assert result == ""


# ---------------------------------------------------------------------------
# Dynamic dir doesn't exist at all → empty string, no error
# ---------------------------------------------------------------------------

def test_no_dynamic_answers_dir(monkeypatch, tmp_path, caplog):
    _clear_cache()
    nonexistent = tmp_path / "no_such_dir"
    monkeypatch.setattr(da, "_DYNAMIC_DIR", nonexistent)

    with caplog.at_level(logging.ERROR, logger="cora.dynamic_answers"):
        result = da.load_dynamic_answers("F3E")

    assert result == ""
    assert caplog.text == ""
