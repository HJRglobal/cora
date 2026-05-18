"""Unit tests for knowledge_gaps."""

import json
import threading

import pytest

import cora.knowledge_gaps as kg_module
from cora.knowledge_gaps import log_gap


@pytest.fixture(autouse=True)
def tmp_log_path(tmp_path, monkeypatch):
    log_file = tmp_path / "knowledge-gaps.jsonl"
    monkeypatch.setattr(kg_module, "_LOG_PATH", log_file)
    return log_file


def test_log_gap_appends_json_line(tmp_log_path):
    log_gap(
        entity="F3E",
        channel="f3e-leadership",
        user="U123",
        question="Who is the Sprouts buyer?",
        response_chars=200,
        gap="F3E Sprouts buyer specifics",
        latency_ms=1500,
    )
    lines = tmp_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["entity"] == "F3E"
    assert record["channel"] == "f3e-leadership"
    assert record["user"] == "U123"
    assert record["question"] == "Who is the Sprouts buyer?"
    assert record["response_chars"] == 200
    assert record["gap"] == "F3E Sprouts buyer specifics"
    assert record["latency_ms"] == 1500
    assert "ts" in record


def test_log_gap_with_special_chars(tmp_log_path):
    gap_text = 'Gap with "quotes", newline\nand unicode — em dash'
    log_gap(
        entity="LEX",
        channel="lex-ops",
        user="U999",
        question="What is the LBHS turnover?",
        response_chars=50,
        gap=gap_text,
        latency_ms=800,
    )
    lines = tmp_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["gap"] == gap_text


def test_concurrent_writes(tmp_log_path):
    errors = []

    def write(n):
        try:
            log_gap(
                entity="FNDR",
                channel="cora-build",
                user=f"U{n}",
                question=f"Question {n}",
                response_chars=100,
                gap=f"Gap {n}",
                latency_ms=n * 10,
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    lines = tmp_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20
    for line in lines:
        record = json.loads(line)
        assert record["entity"] == "FNDR"
