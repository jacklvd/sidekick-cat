"""Provider-abstracted LLM client: NVIDIA NIM (primary) → Groq → GitHub Models.

All backends are OpenAI-compatible, so one OpenAI() client serves them — only
the base URL, API key, and model id differ per provider. `complete(system, user,
task)` tries the task's tier in order, falls back on rate-limit/transient/auth
errors, and returns a friendly message if all fail — so callers never crash on
quota. `user` may be a callable of the model id, so large-context models get a
bigger prompt than the fallbacks (see config.MODEL_INPUT_CHARS).

Env: NVIDIA_API_KEY (NVIDIA NIM), GROQ_API_KEY (Groq), MODELS_PAT (GitHub Models;
needs the `models` scope). A missing key just skips that provider (KeyError → dead).
Replaces models_client.py (which spoke only to GitHub Models via the built-in token).

Smoke test:  python -m scripts.llm_client
"""

import os

from openai import APIConnectionError, APIStatusError, OpenAI

from scripts import limits
from scripts.config import (
    GH_MODELS_BASE,
    GROQ_BASE,
    MAX_DIFF_CHARS,
    MODELS,
    NVIDIA_BASE,
    PROVIDER_MAX_TOKENS,
)
from scripts.diff_anchors import block_path, file_blocks

_QUOTA_MSG = "⚠️ AI quota reached, try again later."
_EMPTY_MSG = "⚠️ Model returned an empty response."
_TOO_LARGE_MSG = (
    "⚠️ This PR is too large for the model's request cap — the diff was truncated "
    "and still didn't fit. Review skipped for now."
)
_SENTINELS = frozenset({_QUOTA_MSG, _EMPTY_MSG, _TOO_LARGE_MSG})


def failed(text: str) -> bool:
    """True when `text` is one of complete()'s sentinels rather than a model's answer.

    complete() never raises — a quota, an outage, an empty body, or an oversized prompt
    all come back as a friendly string — so by shape alone a caller cannot tell a failure
    from an answer. This is the seam that says "no model answered", and callers must not
    treat a sentinel as content: don't record it as reviewed, don't post it in a thread.

    Exact match, not a "⚠️" prefix test: a model may legitimately open a reply or a review
    with a warning sign, and dropping that would silently lose real content.
    """
    return text.strip() in _SENTINELS

# provider -> (base_url, env var holding its API key)
_PROVIDERS = {
    "nvidia": (NVIDIA_BASE, "NVIDIA_API_KEY"),
    "groq": (GROQ_BASE, "GROQ_API_KEY"),
    "github": (GH_MODELS_BASE, "MODELS_PAT"),
}


class NoReply(Exception):
    """A 200 that carries no answer — a broken attempt, not an empty answer."""


def _content(resp) -> str:
    """Pull the reply text, refusing a 200 that said nothing. NIM returns both empty
    shapes (verified live, 2026-07-13): minimax-m3 sends `choices: []` on every call,
    and qwen3.5-122b sends a choice with empty content (`completion_tokens: 0`,
    finish_reason 'stop') on roughly 1 call in 8.

    Neither is an answer, and neither is fatal — a sibling model will take the prompt
    happily. So raise rather than return: an unguarded `choices[0]` raises IndexError,
    which complete() does not catch (killing the whole review), and returning "" makes
    complete()'s `text or _EMPTY_MSG` post a warning and stop, skipping every healthy
    rung below it. Raising NoReply falls through to the next model — the entire point
    of having a tier."""
    if not resp.choices:
        raise NoReply(f"no choices from {resp.model or 'model'}")
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise NoReply(f"empty content from {resp.model or 'model'}")
    return text


def _json_generation_failed(e: APIStatusError) -> bool:
    """True when a 400 means "this model supports json mode but *this* generation didn't
    produce valid JSON" — not "this model doesn't support the response_format param".

    Groq flags the first as code `json_validate_failed`, and a reasoning model trips it by
    spending its whole token budget inside <think> and emitting nothing (qwen3.6-27b does,
    intermittently). The two 400s need opposite handling: an unsupported *param* is worth
    retrying plain on the same model, but a failed *generation* is not — retrying plain just
    returns the raw <think> prose, which parse_response can't read, so the bot would post
    the model's reasoning as its review. Treat it as a failed attempt and let the next
    model answer."""
    body = e.body if isinstance(getattr(e, "body", None), dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    return err.get("code") == "json_validate_failed" or "json_validate_failed" in str(e)


def _call(provider: str, system: str, user: str, model: str, json_mode: bool = False) -> str:
    """One chat completion against a single provider. Raises on any API error.
    json_mode requests a guaranteed-JSON body; a 400 on that request usually means the
    model doesn't support it, so retry the same model plain rather than skip it — except
    when the 400 says the generation itself failed (see _json_generation_failed)."""
    base, key_env = _PROVIDERS[provider]
    client = OpenAI(base_url=base, api_key=os.environ[key_env])
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    # Only where the provider's default would truncate a review — on Groq an explicit
    # cap is charged against the TPM budget up front and 413s. See config.
    if provider in PROVIDER_MAX_TOKENS:
        kwargs["max_tokens"] = PROVIDER_MAX_TOKENS[provider]
    if json_mode:
        try:
            resp = client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
            return _content(resp)
        except APIStatusError as e:
            # Anything but "this model can't do json mode" belongs to the caller: a failed
            # generation must fall through to the next model, not degrade to plain output.
            if e.status_code != 400 or _json_generation_failed(e):
                raise
    resp = client.chat.completions.create(**kwargs)
    return _content(resp)


def _rotate_head(rungs: list, key: int | None) -> list:
    """Pin the first rung (GLM-5.2 — the best reviewer here) and round-robin only the
    same-provider siblings *behind* it by `key` — a stateless, per-PR round-robin.

    GLM leads every review; when it 429s, `key` (a PR-identity hash) spreads which NVIDIA
    sibling is tried second, so concurrent fallbacks don't all pile onto the same rung. The
    head model itself never rotates out of first place. Only the interchangeable NVIDIA
    block rotates: the cross-provider tail (Groq/GitHub) is ordered by how big a request each
    will accept (see config), and reordering it would send a diff to a rung that 413s. `key`
    being any int is enough — its spread across PRs is all this needs, so a per-process hash
    seed is fine; cross-process determinism is not required.
    """
    if key is None:
        return rungs
    lead = rungs[0][0]
    n = 0
    while n < len(rungs) and rungs[n][0] == lead:
        n += 1
    if n <= 2:
        return rungs  # GLM + at most one sibling — nothing to spread behind the pinned head
    tail = rungs[1:n]
    off = key % len(tail)
    return [rungs[0]] + tail[off:] + tail[:off] + rungs[n:]


def complete(system: str, user, task: str, json_mode: bool = False, used: list | None = None,
             rotate_key: int | None = None) -> str:
    """Try primary then fallback. `task` is a key into config.MODELS ('summary'|'review').

    `user` is the prompt (str), or a callable `(model_id) -> str` invoked per attempt
    so each model gets a prompt sized to its own input budget.
    Returns a friendly message (never raises) so a quota/outage degrades gracefully.
    If `used` is given, the (provider, model) that actually answered is appended to
    it — lets a caller name the responder (e.g. the big-PR note) instead of guessing.
    `rotate_key` (a PR-identity hash) spreads which head model is tried first — see
    _rotate_head; omitted, the chain runs in its declared order.
    """
    if limits.breaker_open():
        return _QUOTA_MSG  # a provider/API is failing → don't hammer it
    too_large = False
    empty = False  # some rung answered with nothing (see _content)
    dead: set[str] = set()  # providers to skip for the rest of this call
    for provider, model in _rotate_head(MODELS[task], rotate_key):
        if provider in dead:
            continue  # host unreachable or key absent — don't retry its later models
        try:
            prompt = user(model) if callable(user) else user
            text = _call(provider, system, prompt, model, json_mode)
            limits.record_success()
            if used is not None:
                used.append((provider, model))
            return text  # _content guarantees this is non-empty
        except APIStatusError as e:
            # RateLimitError (429) is a subclass and lands here too. 413 = diff too
            # big for this model — deterministic, not a provider fault, so don't
            # trip the breaker; another model/provider may have room, keep going.
            if e.status_code == 413:
                too_large = True
            else:
                limits.record_failure()
        except APIConnectionError:
            limits.record_failure()
            dead.add(provider)  # host unreachable → skip this provider's later models
        except NoReply:
            # 200 with nothing in it. The model is broken *for this call* — not the
            # provider, so don't mark it dead: its siblings on the same host are fine.
            empty = True
            limits.record_failure()
        except KeyError:
            dead.add(provider)  # provider key not configured — not an API fault
    if too_large:
        return _TOO_LARGE_MSG
    return _EMPTY_MSG if empty else _QUOTA_MSG


def truncate_diff(diff: str, max_chars: int = MAX_DIFF_CHARS) -> str:
    """Cap a diff at whole-file-block granularity: keep every block that still
    fits, name the omitted files so the model knows what it didn't see. Slicing
    mid-hunk (the old behavior, kept as the fallback when even the first block
    is over the cap) leaves the model a dangling half-file it can't review."""
    if len(diff) <= max_chars:
        return diff
    kept, size, omitted = [], 0, []
    for block in file_blocks(diff):
        if size + len(block) <= max_chars:
            kept.append(block)
            size += len(block)
        else:
            omitted.append(block_path(block) or "?")
    if not kept:
        return diff[:max_chars] + f"\n\n…[diff truncated to {max_chars} chars]…"
    names = ", ".join(omitted[:10]) + (f", +{len(omitted) - 10} more" if len(omitted) > 10 else "")
    return "".join(kept) + f"\n\n…[{len(omitted)} file(s) omitted to fit the size cap: {names}]…"


if __name__ == "__main__":
    # Smoke: proves the primary→fallback path is reachable end to end.
    print(
        complete(
            system="You are a smoke test. Reply with one short sentence, nothing else.",
            user="Say: provider-abstracted inference is working.",
            task="summary",
        )
    )
