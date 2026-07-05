"""Offline self-check for limits.py: daily caps, SHA dedup, circuit breaker.

No network, no clock dependence (the breaker clock is faked).
Run: python -m scripts.tests.test_limits
"""

from scripts import limits
from scripts.config import (
    BREAKER_COOLDOWN_S,
    BREAKER_FAILS,
    BREAKER_WINDOW_S,
    PR_DAILY_MAX,
)


def test_per_pr_cap_blocks_and_isolates():
    limits._reset()
    for _ in range(PR_DAILY_MAX):  # spend the per-PR budget
        ok, _ = limits.allow_llm_call("o/r", 1)
        assert ok
    blocked, reason = limits.allow_llm_call("o/r", 1)
    assert not blocked and "PR" in reason  # one over → blocked
    ok2, _ = limits.allow_llm_call("o/r", 2)  # a different PR is independent
    assert ok2


def test_reviewed_head():
    limits._reset()
    assert limits.reviewed_head("o/r", 1) is None  # never reviewed
    limits.record_reviewed_head("o/r", 1, "abc")
    assert limits.reviewed_head("o/r", 1) == "abc"
    limits.record_reviewed_head("o/r", 1, "def")  # newer head overwrites
    assert limits.reviewed_head("o/r", 1) == "def"
    assert limits.reviewed_head("o/r", 2) is None  # keyed per PR


def test_breaker_opens_then_cools_down():
    limits._reset()
    clock = {"t": 1000.0}
    orig, limits._now = limits._now, lambda: clock["t"]
    try:
        for _ in range(BREAKER_FAILS):
            assert limits.breaker_open() is False
            limits.record_failure()
        assert limits.breaker_open() is True  # tripped after N fails in-window
        clock["t"] += BREAKER_COOLDOWN_S + 1
        assert limits.breaker_open() is False  # cooldown elapsed → reopened
    finally:
        limits._now = orig


def test_breaker_window_prunes_old_failures():
    limits._reset()
    clock = {"t": 0.0}
    orig, limits._now = limits._now, lambda: clock["t"]
    try:
        # failures spaced a full window apart never accumulate to the cap
        for _ in range(BREAKER_FAILS + 2):
            limits.record_failure()
            clock["t"] += BREAKER_WINDOW_S
        assert limits.breaker_open() is False
    finally:
        limits._now = orig


def test_record_success_clears_streak():
    limits._reset()
    for _ in range(BREAKER_FAILS - 1):  # one short of tripping
        limits.record_failure()
    limits.record_success()  # streak reset
    limits.record_failure()  # a lone failure can't trip it now
    assert limits.breaker_open() is False


if __name__ == "__main__":
    test_per_pr_cap_blocks_and_isolates()
    test_reviewed_head()
    test_breaker_opens_then_cools_down()
    test_breaker_window_prunes_old_failures()
    test_record_success_clears_streak()
    print("ok")
