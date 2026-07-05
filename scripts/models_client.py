"""Thin GitHub Models client (OpenAI SDK pointed at the GH inference endpoint).

Auth: MODELS_TOKEN env var = the built-in GITHUB_TOKEN (needs `models: read`),
or the MODELS_PAT fallback if the built-in token is denied model access.

Run as a smoke test:  python -m scripts.models_client
"""

import os

from openai import APIStatusError, OpenAI, RateLimitError

from scripts.config import MAX_DIFF_CHARS, MODELS_BASE_URL, SUMMARY_MODEL

_QUOTA_MSG = "⚠️ AI quota reached, try again later."
_EMPTY_MSG = "⚠️ Model returned an empty response."
_TOO_LARGE_MSG = (
    "⚠️ This PR is too large for the free model tier (8000-token request cap) — "
    "the diff was truncated and still didn't fit. Review skipped for now."
)


def complete(system: str, user: str, model: str) -> str:
    """One-shot chat completion. Returns a friendly message on 429/413/empty instead of raising."""
    client = OpenAI(base_url=MODELS_BASE_URL, api_key=os.environ["MODELS_TOKEN"])
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
    except RateLimitError:
        return _QUOTA_MSG
    except APIStatusError as e:  # RateLimitError is caught above; this covers 413 etc.
        if e.status_code == 413:
            return _TOO_LARGE_MSG
        return f"⚠️ Model request failed (HTTP {e.status_code})."
    text = (resp.choices[0].message.content or "").strip()
    return text or _EMPTY_MSG


def truncate_diff(diff: str, max_chars: int = MAX_DIFF_CHARS) -> str:
    """Cap a diff and mark where it was cut, so the model knows it's partial."""
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + f"\n\n…[diff truncated to {max_chars} chars]…"


if __name__ == "__main__":
    # Smoke: proves free inference is reachable. This is what smoke-models.yml runs.
    print(
        complete(
            system="You are a smoke test. Reply with one short sentence, nothing else.",
            user="Say: GitHub Models inference is working.",
            model=SUMMARY_MODEL,
        )
    )
