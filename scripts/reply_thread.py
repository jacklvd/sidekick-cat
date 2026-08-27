"""Reply in a bot review thread when an authorized author responds to an inline
comment (webhook: pull_request_review_comment/created, routed to "thread_reply").

The reply is grounded in the same material the review saw for that spot — the repo
conventions (CLAUDE.md), the changed file's text at the PR head, the diff hunk the
thread is anchored to, and the whole conversation so far — and answered in plain
prose (not JSON) on the MODELS["reply"] tier.

Only threads the bot started (root carries the bot:review-inline marker) get a reply;
inference uses the LLM keys, posting uses GH_TOKEN. Metered on its OWN per-PR bucket
("<pr>:reply"), not the review budget — over cap it stays silent (a rate-limit note
inside a code thread is noise, and the author can re-ask).

Env: PR_NUMBER, ROOT_COMMENT_ID (main()); LLM keys + GH_TOKEN (see llm_client / gh).
"""

import logging
import os

from scripts import gh, limits
from scripts.config import ICON
from scripts.llm_client import complete, failed

log = logging.getLogger("sidekick-cat.reply")

_INLINE_MARKER = "bot:review-inline"   # the marker a bot review-thread root carries
_REPLY_MARKER = "bot:review-reply"     # this flow's own replies

_SYSTEM = (
    "You are Sidekick, a senior engineer replying inside a code-review thread you "
    "started. The author has replied to your inline comment. Answer their question or "
    "respond to their point directly and briefly — a few sentences. If you show a code "
    "fix, put it in a fenced code block tagged with the file's language. Concede when "
    "they are right; hold your ground with a short reason when they are not. No "
    "preamble, no restating their message."
)


def render_thread(thread) -> str:
    """The conversation so far as `login: body` lines, oldest first — the order
    gh.get_review_comment_thread returns."""
    return "\n\n".join(
        f"{getattr(c.user, 'login', '?')}: {(c.body or '').strip()}" for c in thread
    )


def build_reply_prompt(conventions, file_text, path, diff_hunk, transcript) -> str:
    """Ground the reply in the same material the review saw for this spot."""
    parts = []
    if conventions:
        parts.append("Repo conventions (CLAUDE.md):\n" + conventions)
    parts.append(f"File under discussion: {path}")
    if diff_hunk:
        parts.append("The diff hunk this thread is anchored to:\n" + diff_hunk)
    if file_text:
        parts.append(f"Full contents of {path} at the PR head, line-numbered:\n" + file_text)
    parts.append("The review thread so far (reply to the last message):\n" + transcript)
    return "\n\n".join(parts)


def _numbered(text: str) -> str:
    return "\n".join(f"{i}| {ln}" for i, ln in enumerate((text or "").splitlines(), 1))


def _reply_body(answer: str) -> str:
    return f"{ICON} {answer.strip()}\n<!-- {_REPLY_MARKER} -->"


def run(repo, pr_number, reply_to_id) -> None:
    """Answer one reply in a bot review thread. Host-agnostic core, best-effort:
    a non-bot thread, a rate-limit, or a failed generation all end in silence.

    `reply_to_id` is the id a webhook says the human replied to; get_review_comment_thread
    resolves it up to the thread root, so we fetch AND post against that root — never the
    raw webhook id, which need not be the root."""
    root, thread = gh.get_review_comment_thread(repo, pr_number, reply_to_id)
    if root is None or _INLINE_MARKER not in (root.body or ""):
        return  # not a thread we started — don't answer
    # Replies bill to their own per-PR bucket: limits._checks interpolates the pr key, so
    # a synthetic one gets an independent PR_DAILY_MAX (same trick as repo_context's
    # "context"). Sharing the review bucket meant a conversation in the threads ate the
    # reviews of the very PR being discussed — and the author has no way to see why.
    ok, reason = limits.allow_llm_call(repo.full_name, f"{pr_number}:reply")
    if not ok:
        log.info("reply skipped, rate-limited: %s", reason)
        return
    conventions = gh.get_file_text(repo, "CLAUDE.md") or ""
    head_sha = repo.get_pull(pr_number).head.sha
    file_text = _numbered(gh.get_file_text(repo, root.path, ref=head_sha) or "")
    diff_hunk = getattr(root, "diff_hunk", "") or ""
    prompt = build_reply_prompt(conventions, file_text, root.path, diff_hunk, render_thread(thread))
    answer = complete(_SYSTEM, prompt, "reply")
    if failed(answer):
        return  # quota/empty/outage — silence beats posting a broken reply
    gh.create_review_comment_reply(repo, pr_number, root.id, _reply_body(answer))


def main():
    pr_number = int(os.environ["PR_NUMBER"])
    root_id = int(os.environ["ROOT_COMMENT_ID"])
    run(gh.get_repo(), pr_number, root_id)


if __name__ == "__main__":
    main()
