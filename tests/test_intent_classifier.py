"""Unit tests for intent_classifier — classify() and routing_hints()."""

import pytest

from cora.intent_classifier import Intent, RoutingHints, classify, routing_hints


# ── classify() — FINANCIAL intent ────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "what's our cash position?",
    "show me the P&L for OSN",
    "what's the weekly cash flow?",
    "give me a financial pulse",
    "how are we doing YTD?",
    "what's the net revenue this month?",
    "show me gross margin",
    "what's the cash balance?",
    "how much cash on hand do we have?",
    "financial status update",
    "profit and loss summary",
    "what's the breakeven for OSN?",
    "show me AR aging",
    "AP aging report",
    "year-to-date performance",
    "how is F3E tracking financially",
    "balance sheet",
    "quarterly results",
    "actuals vs forecast",
    "what's the forecast look like",
])
def test_classify_financial(msg):
    assert classify(msg) == Intent.FINANCIAL


# ── classify() — TASK_LOOKUP intent ──────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "what are my open tasks?",
    "what's on my plate?",
    "show me tasks assigned to me",
    "what's assigned to me?",
    "what tasks are due today?",
    "any overdue tasks?",
    "what do I have pending?",
    "show me action items",
    "what should I work on today?",
    "what's left this week?",
    "check Asana for my tasks",
    "pending work?",
    "open action items",
])
def test_classify_task_lookup(msg):
    assert classify(msg) == Intent.TASK_LOOKUP


# ── classify() — SIMPLE intent ────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "what's the F3 Pure tagline?",
    "what are the brand colors?",
    "who is Shaun Hawkins?",
    "what's the launch date?",
    "who owns OSN?",
    "what's the cap table for F3E?",
    "operating agreement?",
    "what's Tommy's phone number?",
    "what's Harrison's email address?",
    "when does F3 Pure launch?",
    "what are the hours?",
    "who is the owner of LBHS?",
])
def test_classify_simple(msg):
    assert classify(msg) == Intent.SIMPLE


# ── classify() — COMPLEX intent (default) ────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "what's the status of the Sprouts partnership?",
    "walk me through the Vine & Branches situation",
    "give me an update on Cora development",
    "what should we do about the CT Corp lien?",
    "explain the AHCCCS audit timeline",
    "summarize the Fireflies transcript from last Tuesday",
    "what happened in the F3 meeting?",
    "describe the OSN reconciliation process",
    "help me prepare for the BDM handoff meeting",
])
def test_classify_complex(msg):
    assert classify(msg) == Intent.COMPLEX


# ── classify() — FINANCIAL wins over TASK even if both match ─────────────────

def test_financial_beats_task():
    """FINANCIAL has higher priority than TASK_LOOKUP."""
    msg = "what tasks are related to our cash flow?"
    # "tasks" would match TASK_LOOKUP but "cash flow" matches FINANCIAL first
    assert classify(msg) == Intent.FINANCIAL


# ── classify() — entity argument is accepted (not used yet) ──────────────────

def test_classify_accepts_entity_arg():
    result = classify("what's the tagline?", entity="F3E")
    assert result == Intent.SIMPLE


# ── classify() — empty string → COMPLEX ──────────────────────────────────────

def test_classify_empty_string():
    assert classify("") == Intent.COMPLEX


# ── routing_hints() — FINANCIAL ───────────────────────────────────────────────

def test_hints_financial():
    hints = routing_hints(Intent.FINANCIAL)
    assert hints.intent == Intent.FINANCIAL
    assert hints.skip_kb is True
    assert hints.bypass_cache is True
    assert hints.cache_ttl == 0
    assert hints.kb_k_override is None


# ── routing_hints() — TASK_LOOKUP ─────────────────────────────────────────────

def test_hints_task_lookup():
    hints = routing_hints(Intent.TASK_LOOKUP)
    assert hints.intent == Intent.TASK_LOOKUP
    assert hints.skip_kb is False
    assert hints.bypass_cache is False
    assert hints.kb_k_override == 4
    assert hints.cache_ttl == 300


# ── routing_hints() — SIMPLE ──────────────────────────────────────────────────

def test_hints_simple():
    hints = routing_hints(Intent.SIMPLE)
    assert hints.intent == Intent.SIMPLE
    assert hints.skip_kb is False
    assert hints.bypass_cache is False
    assert hints.kb_k_override == 3
    assert hints.cache_ttl == 3600  # 1 hour


# ── routing_hints() — COMPLEX ─────────────────────────────────────────────────

def test_hints_complex():
    hints = routing_hints(Intent.COMPLEX)
    assert hints.intent == Intent.COMPLEX
    assert hints.skip_kb is False
    assert hints.bypass_cache is False
    assert hints.kb_k_override is None
    assert hints.cache_ttl == 1800  # 30 min


# ── RoutingHints is a dataclass ───────────────────────────────────────────────

def test_routing_hints_is_dataclass():
    hints = routing_hints(Intent.COMPLEX)
    assert isinstance(hints, RoutingHints)


# ── Intent string values ───────────────────────────────────────────────────────

def test_intent_string_values():
    assert Intent.FINANCIAL == "financial"
    assert Intent.TASK_LOOKUP == "task_lookup"
    assert Intent.SIMPLE == "simple"
    assert Intent.COMPLEX == "complex"


# ── Case-insensitive matching ──────────────────────────────────────────────────

def test_classify_case_insensitive_financial():
    assert classify("WHAT IS OUR CASH POSITION") == Intent.FINANCIAL


def test_classify_case_insensitive_simple():
    assert classify("WHAT IS THE TAGLINE") == Intent.SIMPLE


# ── Partial phrase matches ─────────────────────────────────────────────────────

def test_classify_ytd_acronym():
    assert classify("how are we doing YTD") == Intent.FINANCIAL


def test_classify_cap_table():
    assert classify("pull up the cap table") == Intent.SIMPLE


def test_classify_asana_keyword():
    assert classify("go check Asana") == Intent.TASK_LOOKUP
