"""Offline self-check for llm_client: provider ordering, fallback, truncation.

Monkeypatches `_call` so nothing hits the network — no keys needed.
Run: python -m scripts.tests.test_llm_client
"""

import itertools
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APIStatusError

from scripts import limits, llm_client
from scripts.config import MODEL_INPUT_CHARS, MODELS, REVIEW_MODEL


def test_truncate():
    assert llm_client.truncate_diff("abc", 10) == "abc"  # under cap → untouched
    out = llm_client.truncate_diff("x" * 100, 10)
    assert out.startswith("x" * 10)
    assert "truncated" in out


def test_review_order():
    # Both review tiers exhaust NVIDIA before leaving it: the NIM rungs are the only ones
    # with a large-context budget, so a non-NVIDIA rung jumping the queue would review a
    # truncated diff while a model that could hold the whole thing sat further down.
    for task in ("review", "review_large"):
        providers = [p for p, _ in MODELS[task]]
        nvidia_run = len(list(itertools.takewhile(lambda p: p == "nvidia", providers)))
        assert nvidia_run >= 3, f"{task}: expected NVIDIA to lead"
        assert "nvidia" not in providers[nvidia_run:], f"{task}: NVIDIA rungs must be contiguous"
        assert MODELS[task][0] == ("nvidia", "z-ai/glm-5.2")
    # Every NVIDIA rung must carry a large-context budget. Without one it silently falls back
    # to the caller's tier cap (12K chars on the small path) — so it would review a fraction
    # of the diff, find less, and never raise. The failure is invisible; the test isn't.
    for _, model in MODELS["review_large"]:
        if model.startswith(("z-ai/", "nvidia/", "deepseek-ai/", "minimaxai/", "mistralai/", "qwen/qwen3.5")):
            assert model in MODEL_INPUT_CHARS, f"{model} has no input budget"
    # Rungs that cannot answer. A tier naming one burns a fallback attempt for nothing:
    # the first two are decommissioned by Groq on 2026-07-17 (guaranteed 404 after that),
    # and groq/compound 413s on a prompt smaller than anything review_large even sends.
    retired = {
        "qwen/qwen3-32b",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "groq/compound",
    }
    assert not {m for tier in MODELS.values() for _, m in tier} & retired
    # review_large's fallbacks descend by the request size each will accept (NVIDIA →
    # gpt-4.1 → gpt-oss-120b). Reversing the last two would hand the biggest diffs to
    # the rung with the smallest request cap, which 413s on everything this tier sends.
    large = [m for _, m in MODELS["review_large"]]
    assert large.index(REVIEW_MODEL) < large.index("openai/gpt-oss-120b")
    # Every provider named in any task must be a registered _PROVIDERS entry, else
    # complete() would KeyError-skip it silently (a wrong provider name = dead tier).
    named = {p for tier in MODELS.values() for p, _ in tier}
    assert named <= set(llm_client._PROVIDERS)
    # Every per-model input budget must belong to a model some tier actually uses,
    # else it's dead config (a typo'd id silently drops the model to the tier cap).
    in_tiers = {m for tier in MODELS.values() for _, m in tier}
    assert set(MODEL_INPUT_CHARS) <= in_tiers


def test_callable_user_builds_prompt_per_model():
    # `user` as a callable is invoked with each attempt's model id, so a fallback
    # after a 429 gets a prompt rebuilt (re-truncated) for the model actually tried.
    (_, first), (_, second) = MODELS["review"][:2]
    seen = []

    def fake(provider, system, user, model, json_mode=False):
        seen.append((model, user))
        if model == first:
            resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
            raise APIStatusError("rate limited", response=resp, body=None)
        return "ok"

    restore = _patch(fake)
    try:
        assert llm_client.complete("s", lambda m: f"prompt-for-{m}", "review") == "ok"
        assert seen == [(first, f"prompt-for-{first}"), (second, f"prompt-for-{second}")]
    finally:
        restore()


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


def _resp(content):
    msg = SimpleNamespace(message=SimpleNamespace(content=content))
    return SimpleNamespace(choices=[msg], model="m")


def test_empty_reply_raises_not_returns():
    # Both empty shapes NIM actually serves must raise, so complete() can fall through:
    #   choices: []            — minimax-m3, every call
    #   choices with no content — qwen3.5-122b, ~1 call in 8 (completion_tokens: 0)
    # Returning "" instead would make complete() post _EMPTY_MSG and stop, skipping
    # every healthy rung below; indexing choices[0] blind would raise IndexError,
    # which complete() doesn't catch, killing the review outright.
    for broken in (SimpleNamespace(choices=[], model="m"), _resp(""), _resp(None), _resp("  ")):
        try:
            llm_client._content(broken)
            raise AssertionError(f"empty reply must raise NoReply: {broken}")
        except llm_client.NoReply:
            pass
    assert llm_client._content(_resp("  hi  ")) == "hi"  # a real body still reads through


def test_empty_reply_falls_through_to_next_model():
    (_, first), (_, second) = MODELS["review"][:2]
    tried = []

    def fake(provider, system, user, model, json_mode=False):
        tried.append(model)
        if model == first:
            raise llm_client.NoReply("empty content")
        return "next-model-ok"

    restore = _patch(fake)
    try:
        assert llm_client.complete("s", "u", "review") == "next-model-ok"
        assert tried[:2] == [first, second]  # broken model skipped, not fatal
    finally:
        restore()


def test_json_validate_failure_is_not_an_unsupported_param():
    # Two different 400s in json mode, needing opposite handling:
    #   json_validate_failed -> the model CAN do json but this generation produced none
    #     (a reasoning model burning its budget in <think>). Must NOT retry plain: plain
    #     hands back raw reasoning prose, which parse_response can't read, so the bot
    #     would post the model's <think> monologue as its review.
    #   any other 400 -> the model doesn't support response_format. Retrying plain is
    #     the whole reason that fallback exists; keep it.
    def err(body):
        resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
        return APIStatusError("bad request", response=resp, body=body)

    groq_shape = {"error": {"code": "json_validate_failed", "failed_generation": ""}}
    assert llm_client._json_generation_failed(err(groq_shape))
    assert llm_client._json_generation_failed(err({"code": "json_validate_failed"}))
    assert not llm_client._json_generation_failed(err({"error": {"code": "unsupported"}}))
    assert not llm_client._json_generation_failed(err(None))  # unparseable body -> retry plain


def test_all_empty_reports_empty_not_quota():
    # _EMPTY_MSG means "every rung said nothing", not "the first one did" — a quota
    # message here would send the user chasing a rate limit that never happened.
    def fake(provider, system, user, model, json_mode=False):
        raise llm_client.NoReply("empty content")

    restore = _patch(fake)
    try:
        assert llm_client.complete("s", "u", "review") == llm_client._EMPTY_MSG
    finally:
        restore()


if __name__ == "__main__":
    test_truncate()
    test_review_order()
    test_callable_user_builds_prompt_per_model()
    test_fallback_on_primary_error()
    test_both_fail_returns_quota_msg()
    test_status_error_advances_to_next_entry()
    test_empty_reply_raises_not_returns()
    test_empty_reply_falls_through_to_next_model()
    test_json_validate_failure_is_not_an_unsupported_param()
    test_all_empty_reports_empty_not_quota()
    print("ok")
