"""Unit tests for channel_classifier."""

from cora.channel_classifier import classify_function, is_tier_1, tier_label


def test_classify_function_basic():
    assert classify_function("f3e-leadership") == "leadership"
    assert classify_function("lex-finance") == "finance"
    assert classify_function("osn-ops") == "ops"
    assert classify_function("bdm-sales") == "sales"
    assert classify_function("hjrg-hr") == "hr"


def test_classify_function_special():
    assert classify_function("cora-build") == "build"
    assert classify_function("fndr") == "founder"
    assert classify_function("fndr-general") == "founder"


def test_classify_function_no_hyphen():
    assert classify_function("random") == "unknown"


def test_classify_function_unknown():
    assert classify_function("f3e-special-project") == "unknown"


def test_is_tier_1_by_function():
    assert is_tier_1("F3E", "leadership") is True
    assert is_tier_1("F3E", "finance") is True
    assert is_tier_1("F3E", "founder") is True
    assert is_tier_1("F3E", "build") is True
    assert is_tier_1("F3E", "ops") is False
    assert is_tier_1("F3E", "hr") is False
    assert is_tier_1("F3E", "clients") is False
    assert is_tier_1("F3E", "unknown") is False


def test_is_tier_1_by_entity():
    assert is_tier_1("HJRG", "ops") is True
    assert is_tier_1("HJRG", "unknown") is True
    assert is_tier_1("HJRG", "hr") is True


def test_tier_label_returns_string():
    assert tier_label("F3E", "leadership") == "TIER_1"
    assert tier_label("F3E", "ops") == "TIER_3"
    assert tier_label("HJRG", "ops") == "TIER_1"
