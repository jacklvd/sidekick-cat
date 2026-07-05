"""Light AI summary of a PR, upserted as a `<!-- bot:summary -->` comment.

Diff comes from a file (the workflow runs `gh pr diff` and writes it), so we
avoid the paginated file API. Inference uses GROQ_API_KEY / MODELS_PAT (see
llm_client); posting uses GH_TOKEN.
"""

import os
from pathlib import Path

from scripts import gh, limits
from scripts.llm_client import complete, truncate_diff

_SYSTEM = (
    "You are a concise code reviewer. Summarize a pull request diff for a busy "
    "maintainer in 3-5 short bullet points: what changed and why it matters. "
    "No preamble, no restating the file list, just the bullets in Markdown."
)


def summarize(diff: str) -> str:
    if not diff.strip():
        return "_No diff to summarize (empty or binary-only changes)._"
    return complete(_SYSTEM, truncate_diff(diff), "summary")


def run(repo, pr_number, diff):
    """Summarize a PR, gated by the daily LLM caps. Host-agnostic core.

    A non-empty diff is the only path that reaches the model, so only that path
    is metered — an empty diff posts its note for free. When capped, skip the
    model and leave a single `<!-- bot:ratelimit -->` note instead.
    """
    if diff.strip():
        ok, reason = limits.allow_llm_call(repo.full_name, pr_number)
        if not ok:
            gh.upsert_comment(
                repo, pr_number, "bot:ratelimit",
                f"🐱 Sidekick is taking a breather — {reason}. Try again later.",
            )
            return
    body = "### 🐱 Sidekick's summary\n" + summarize(diff)
    gh.upsert_comment(repo, pr_number, "bot:summary", body)


def main():
    pr_number = int(os.environ["PR_NUMBER"])
    diff = Path(os.environ["PR_DIFF_FILE"]).read_text(
        encoding="utf-8", errors="replace"
    )
    run(gh.get_repo(), pr_number, diff)


if __name__ == "__main__":
    main()
