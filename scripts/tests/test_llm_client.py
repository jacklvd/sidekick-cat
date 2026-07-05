"""Offline self-check for llm_client: provider ordering, fallback, truncation.

Monkeypatches `_call` so nothing hits the network — no keys needed.
Run: python -m scripts.tests.test_llm_client
"""

import httpx
from openai import APIConnectionError, APIStatusError

from scripts import limits, llm_client
from scripts.config import MODELS


def test_truncate():
    assert llm_client.truncate_diff("abc", 10) == "abc"  # under cap → untouched
    out = llm_client.truncate_diff("x" * 100, 10)
    assert out.startswith("x" * 10)
    assert "truncated" in out


def test_review_order():
    # Review precedence is explicit and crosses providers: qwen → GitHub → gpt-oss.
    assert [p for p, _ in MODELS["review"]] == ["groq", "github", "groq"]
    assert MODELS["review"][0] == ("groq", "qwen/qwen3-32b")


def _patch(fake):
    """Swap _call for a fake, returning a restore callable. Resets breaker state
    so an accumulated failure streak from a prior test can't open it here."""
    limits._reset()
    orig = llm_client._call
    llm_client._call = fake
    return lambda: setattr(llm_client, "_call", orig)


def test_fallback_on_primary_error():
    providers = [p for p, _ in MODELS["summary"]]
    calls = []

    def fake(provider, system, user, model, json_mode=False):
        calls.append(provider)
        if provider == providers[0]:
            raise APIConnectionError(request=None)  # primary down → fall back
        return "fallback-ok"

    restore = _patch(fake)
    try:
        assert llm_client.complete("s", "u", "summary") == "fallback-ok"
        assert calls == providers  # primary tried first, then fallback
    finally:
        restore()


def test_both_fail_returns_quota_msg():
    def fake(provider, system, user, model, json_mode=False):
        raise APIConnectionError(request=None)

    restore = _patch(fake)
    try:
        assert llm_client.complete("s", "u", "review") == llm_client._QUOTA_MSG
    finally:
        restore()


def test_status_error_advances_to_next_entry():
    # A 429 on the first review model must advance to the next entry in the list,
    # crossing providers freely (qwen on Groq → gpt-4.1 on GitHub).
    (_, first), (_, second) = MODELS["review"][:2]
    tried = []

    def fake(provider, system, user, model, json_mode=False):
        tried.append(model)
        if model == first:
            resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
            raise APIStatusError("rate limited", response=resp, body=None)
        return "second-model-ok"

    restore = _patch(fake)
    try:
        assert llm_client.complete("s", "u", "review") == "second-model-ok"
        assert tried[:2] == [first, second]  # advanced to the next entry in order
    finally:
        restore()


if __name__ == "__main__":
    test_truncate()
    test_review_order()
    test_fallback_on_primary_error()
    test_both_fail_returns_quota_msg()
    test_status_error_advances_to_next_entry()
    print("ok")
