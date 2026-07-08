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
from scripts.config import GH_MODELS_BASE, GROQ_BASE, MAX_DIFF_CHARS, MODELS, NVIDIA_BASE
from scripts.diff_anchors import block_path, file_blocks

_QUOTA_MSG = "⚠️ AI quota reached, try again later."
_EMPTY_MSG = "⚠️ Model returned an empty response."
_TOO_LARGE_MSG = (
    "⚠️ This PR is too large for the model's request cap — the diff was truncated "
    "and still didn't fit. Review skipped for now."
)

# provider -> (base_url, env var holding its API key)
_PROVIDERS = {
    "nvidia": (NVIDIA_BASE, "NVIDIA_API_KEY"),
    "groq": (GROQ_BASE, "GROQ_API_KEY"),
    "github": (GH_MODELS_BASE, "MODELS_PAT"),
}


def _call(provider: str, system: str, user: str, model: str, json_mode: bool = False) -> str:
    """One chat completion against a single provider. Raises on any API error.
    json_mode requests a guaranteed-JSON body; a 400 on that request means the
    model doesn't support it, so retry the same model plain rather than skip it."""
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
    if json_mode:
        try:
            resp = client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
            return (resp.choices[0].message.content or "").strip()
        except APIStatusError as e:
            if e.status_code != 400:
                raise
    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def complete(system: str, user, task: str, json_mode: bool = False, used: list | None = None) -> str:
    """Try primary then fallback. `task` is a key into config.MODELS ('summary'|'review').

    `user` is the prompt (str), or a callable `(model_id) -> str` invoked per attempt
    so each model gets a prompt sized to its own input budget.
    Returns a friendly message (never raises) so a quota/outage degrades gracefully.
    If `used` is given, the (provider, model) that actually answered is appended to
    it — lets a caller name the responder (e.g. the big-PR note) instead of guessing.
    """
    if limits.breaker_open():
        return _QUOTA_MSG  # a provider/API is failing → don't hammer it
    too_large = False
    dead: set[str] = set()  # providers to skip for the rest of this call
    for provider, model in MODELS[task]:
        if provider in dead:
            continue  # host unreachable or key absent — don't retry its later models
        try:
            prompt = user(model) if callable(user) else user
            text = _call(provider, system, prompt, model, json_mode)
            limits.record_success()
            if used is not None:
                used.append((provider, model))
            return text or _EMPTY_MSG
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
        except KeyError:
            dead.add(provider)  # provider key not configured — not an API fault
    return _TOO_LARGE_MSG if too_large else _QUOTA_MSG


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
