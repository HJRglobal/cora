"""WS12 — shared Asana system-noise filter (cora.asana_filters)."""

from cora.asana_filters import SYSTEM_NOISE_SKIP_TERMS, is_system_noise_task


def test_goal_reminder_singular_matches():
    assert is_system_noise_task("It's time to update your goal") is True


def test_goal_reminder_plural_matches():
    # substring "...your goal" matches the plural "...your goals" title
    assert is_system_noise_task("It's time to update your goals for Q3") is True


def test_curly_apostrophe_still_matches():
    # D-051: Asana often renders the typographic apostrophe (U+2019); it must
    # normalize to ASCII or the filter fails open.
    assert is_system_noise_task("It’s time to update your goals") is True
    assert is_system_noise_task("It’s time to update your goal") is True


def test_case_insensitive():
    assert is_system_noise_task("IT'S TIME TO UPDATE YOUR GOAL") is True


def test_real_task_not_flagged():
    assert is_system_noise_task("Ship the F3E retail deck") is False
    assert is_system_noise_task("Review Q3 goal alignment with Larry") is False


def test_none_and_empty_are_false():
    assert is_system_noise_task(None) is False
    assert is_system_noise_task("") is False


def test_terms_are_lowercased_for_substring_match():
    # the matcher folds case, so terms must be stored lowercased
    assert all(t == t.lower() for t in SYSTEM_NOISE_SKIP_TERMS)
