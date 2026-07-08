"""Full AI code review on /review — inline review threads + idempotent summary.

The model returns one JSON object {verdict, summary, issues[]}. Issues whose
(path, line) is a valid RIGHT-side diff anchor become inline review comments,
reconciled against prior runs (keep matches, delete stale, add new); the rest
fall back into the summary comment so nothing is lost. The summary stays the
upserted `<!-- bot:review -->` comment.

Diff comes from a file (the workflow runs `gh pr diff`). Inference uses
NVIDIA_API_KEY / GROQ_API_KEY / MODELS_PAT (see llm_client); posting uses
GH_TOKEN. PR number = the triggering comment's issue.
"""

import json
import os
from pathlib import Path

from scripts import gh, limits, repo_context
from scripts.config import (
    AUTO_APPROVE,
    CONTEXT_MAX_TREE_CHARS,
    MAX_DIFF_CHARS,
    MODEL_INPUT_CHARS,
    MODELS,
    REVIEW_LARGE_DIFF_CHARS,
)
from scripts.diff_anchors import anchors, number_diff, strip_noise
from scripts.llm_client import complete, truncate_diff

_INLINE_MARKER = "bot:review-inline"

# Friendly display names for review models — config's ids are ugly for user copy.
_MODEL_LABELS = {
    "z-ai/glm-5.2": "GLM-5.2",
    "minimaxai/minimax-m2.7": "MiniMax-M2.7",
    "meta-llama/llama-4-scout-17b-16e-instruct": "Llama-4 Scout",
    "groq/compound": "Groq Compound",
}


def _large_note(model: str) -> str:
    """Big-PR disclaimer that names the model which actually answered — the large
    tier is NVIDIA-first (GLM-5.2) now, so hardcoding a fallback would misreport it.
    Falls back to the tier's primary label when the responder is unknown."""
    label = _MODEL_LABELS.get(model, model)
    return (
        f"> 🐱 Big PR — I reviewed the whole diff in one pass with **{label}**. "
        "Treat it as a wide first sweep; split the PR and `/review` again for a "
        "closer look.\n\n"
    )


_SYSTEM = (
    "You are a meticulous senior software engineer code reviewer. Review only the changes in the diff, "
    "but judge them against everything you're given: flag a change that violates the repo conventions, "
    "contradicts the PR's own description, or plausibly breaks a caller/consumer the project context "
    "shows exists outside the diff.\n"
    "Respond with ONLY a single JSON object (no prose, no markdown fence) shaped as:\n"
    '{"verdict": "approve|comment|request_changes", "summary": "<markdown>", '
    '"issues": [{"path": "<file>", "line": <int>, '
    '"severity": "blocker|major|minor|nit", "body": "<what is wrong -> the fix>"}]}\n'
    "verdict: request_changes if there is any bug, security, or correctness problem; "
    "approve only when you found nothing to fix.\n"
    "summary: a one-sentence overall assessment, then a short markdown checklist table "
    "covering correctness, tests, docs, and security (✅ / ⚠️ / ❌ / N/A per row).\n"
    "issues: one entry per concrete problem. Diff lines that can carry a comment are "
    "prefixed with their line number as `N| ` — set `line` by copying that N exactly; "
    "never count lines yourself and never flag an unprefixed line. `body` is GitHub-flavored "
    "markdown: a one-sentence explanation of the problem and fix, then — when you "
    "show corrected code — put it in a fenced code block on its own, tagged with "
    "the file's language (```python, ```yaml, …). Never write code inline as plain "
    "prose. Keep snippets short. Use an empty list when there are no issues.\n"
    "Report at most 6 issues — the ones a human reviewer would actually block or "
    "comment on. Skip style/formatting nits a linter or formatter would already "
    "enforce; do not pad the list to look thorough."
)

_CONVENTIONS = Path("CLAUDE.md")  # repo house rules, fed to the reviewer when present


def build_prompt(
    diff: str,
    conventions: str | None = None,
    max_chars: int = MAX_DIFF_CHARS,
    pr_text: str | None = None,
    project_context: str | None = None,
) -> str:
    """Compose the review prompt. `conventions` is the target repo's CLAUDE.md text
    (Cloud Run fetches it via API); when None, fall back to a local file (Actions).
    `project_context` is the cached repo-context doc (scripts.repo_context) — what
    the rest of the project looks like, so the reviewer isn't judging the diff in a
    vacuum. `pr_text` is the PR title+body — what the change CLAIMS to do, so the
    reviewer can flag code that contradicts its own description. `max_chars` caps
    the diff — larger on the high-TPM large-PR path."""
    if conventions is None and _CONVENTIONS.exists():
        conventions = _CONVENTIONS.read_text(encoding="utf-8")
    parts = []
    if conventions:
        parts.append("Repo conventions (CLAUDE.md):\n" + conventions)
    if project_context:
        parts.append("Project context:\n" + project_context[:CONTEXT_MAX_TREE_CHARS])
    if pr_text:
        parts.append("PR title and description (what the author says it does):\n" + pr_text)
    parts.append("PR diff:\n" + truncate_diff(diff, max_chars))
    return "\n\n".join(parts)


def parse_response(text: str):
    """Extract the JSON object from the model reply, tolerating an outer ```json
    fence, leading prose, and code fences *inside* string values. We slice from the
    first { to the last } — the outer fence's backticks sit outside the braces, so
    no fence-stripping is needed (and stripping would wrongly grab an inner ```lang
    block from a body). Returns the dict, or None if it isn't a JSON object."""
    if not text or not text.strip():
        return None
    s = text.strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


_SNAP = 3  # max distance to pull a near-miss line onto a real anchor


def partition(issues, anchor_map):
    """Split issues into (anchorable, unanchorable) by (path, line) validity.
    A line within _SNAP of a real anchor snaps to the nearest one — models are
    often off by a line or two, and losing the issue to the summary is worse
    than anchoring it one line away. Anchorable lines normalized to int."""
    ok, no = [], []
    for it in issues:
        path = it.get("path")
        try:
            line = int(it.get("line"))
        except (TypeError, ValueError):
            no.append(it)
            continue
        valid = anchor_map.get(path, ())
        if line in valid:
            ok.append({**it, "line": line})
            continue
        near = min(valid, key=lambda a: (abs(a - line), a), default=None)
        if near is not None and abs(near - line) <= _SNAP:
            ok.append({**it, "line": near})
        else:
            no.append(it)
    return ok, no


def _inline_body(issue) -> str:
    sev = str(issue.get("severity", "")).strip()
    prefix = f"**[{sev}]** " if sev else ""
    return f"{prefix}{str(issue.get('body', '')).strip()}\n<!-- {_INLINE_MARKER} -->"


def reconcile_inline(repo, pr_number, head_sha, anchorable, scope=None):
    """Keep/edit matching comments, delete stale ones, create new ones. Keyed on
    (path, line). Editing keeps the thread + its resolution state intact.
    `scope` is the set of file paths this review actually looked at (incremental
    runs); stale comments OUTSIDE it are kept — the model never re-judged them."""
    existing = {
        (c.path, c.line): c
        for c in gh.get_inline_comments(repo, pr_number, _INLINE_MARKER)
    }
    desired = {(it["path"], it["line"]): it for it in anchorable}
    for key, it in desired.items():
        body = _inline_body(it)
        c = existing.get(key)
        if c is None:
            gh.create_review_comment(
                repo, pr_number, head_sha, it["path"], it["line"], body
            )
        elif (c.body or "") != body:
            c.edit(body)
    for key, c in existing.items():
        if key not in desired and (scope is None or key[0] in scope):
            c.delete()  # issue no longer reported


def _unanchorable_md(issues) -> str:
    if not issues:
        return ""
    lines = ["", "### Other notes (couldn't anchor to a diff line)"]
    for it in issues:
        loc = f"`{it.get('path', '?')}:{it.get('line', '?')}`"
        sev = str(it.get("severity", "")).strip()
        sev = f" **[{sev}]**" if sev else ""
        lines.append(f"- {loc}{sev} {str(it.get('body', '')).strip()}")
    return "\n".join(lines)


def run(repo, pr_number, diff):
    """Full /review, gated by head-SHA dedup + daily caps. Host-agnostic core."""
    pr = repo.get_pull(pr_number)
    head_sha = pr.head.sha
    prev = limits.reviewed_head(repo.full_name, pr_number)
    if prev == head_sha:
        return  # unchanged head already reviewed — re-review is free + idempotent
    ok, reason = limits.allow_llm_call(repo.full_name, pr_number)
    if not ok:
        gh.upsert_comment(
            repo,
            pr_number,
            "bot:ratelimit",
            f"🐱 Sidekick is taking a breather — {reason}. Try again later.",
        )
        return
    # dev-note: recorded before the review runs (parity with the old check-and-record
    # seen_sha): if the LLM call dies mid-flight this head isn't retried until a new
    # commit moves the sha — rare, bounded by PR_DAILY_MAX, acceptable.
    limits.record_reviewed_head(repo.full_name, pr_number, head_sha)

    # Incremental: a previously reviewed PR only gets its NEW commits re-read —
    # cheaper, usually fits the smart tier, and untouched files keep their threads.
    incremental = False
    if prev:
        try:
            delta = gh.compare_diff(repo, prev, head_sha)
            if delta.strip():
                diff, incremental = delta, True
        except Exception:
            pass  # dev-note: base gone (force-push) or compare hiccup → full review

    # Strip generated/vendored files BEFORE size-routing: a lock-file bump must not
    # push an otherwise small PR onto the broad-sweep large-model path.
    diff = strip_noise(diff)
    # Size-route: small PRs go to the smart tier; big diffs to the high-TPM tier so
    # the whole thing fits one pass (and the large path may send a bigger diff). The
    # threshold is the small model's truncation cap — over it, qwen would truncate.
    large = len(diff) > MAX_DIFF_CHARS
    task = "review_large" if large else "review"
    max_chars = REVIEW_LARGE_DIFF_CHARS if large else MAX_DIFF_CHARS

    conventions = gh.get_file_text(repo, "CLAUDE.md") or ""  # target repo's rubric
    project_context = repo_context.ensure_fresh(repo)
    pr_text = f"{pr.title}\n\n{pr.body or ''}".strip()
    used: list = []  # complete() appends the (provider, model) that answered
    # Numbered BEFORE truncation so the prefixes always match the real file lines.
    numbered = number_diff(diff)

    def prompt_for(model: str) -> str:
        # Prompt sized per attempt: large-context models (NVIDIA) take the diff at
        # their own budget; the Groq/GitHub fallbacks keep the tier cap they were
        # TPM-tuned for. Same diff, different truncation point.
        cap = MODEL_INPUT_CHARS.get(model, max_chars)
        return build_prompt(numbered, conventions, cap, pr_text, project_context)

    raw = complete(_SYSTEM, prompt_for, task, json_mode=True, used=used)

    # Disclaimers built AFTER the call so the big-PR note names the model that
    # actually answered (falls back to the tier's primary if the call failed).
    note = _large_note(used[0][1] if used else MODELS[task][0][1]) if large else ""
    if incremental:
        note = (
            f"> 🐱 Incremental review — only the changes since `{prev[:7]}`; "
            "earlier threads on untouched files were left as-is.\n\n"
        ) + note

    data = parse_response(raw)
    if data is None:
        # Fallback: model didn't return parseable JSON (quota msg, malformed) —
        # post whatever it said as the summary, no inline comments (summary-only fallback).
        gh.upsert_comment(
            repo, pr_number, "bot:review", "### 🐱 Sidekick's code review\n" + note + raw
        )
        return

    verdict = str(data.get("verdict", "comment")).strip().lower()
    issues = [it for it in (data.get("issues") or []) if isinstance(it, dict)]
    anchor_map = anchors(diff)
    anchorable, unanchorable = partition(issues, anchor_map)

    reconcile_inline(
        repo, pr_number, head_sha, anchorable,
        scope=set(anchor_map) if incremental else None,
    )

    summary = (
        "### 🐱 Sidekick's code review\n"
        + note
        + f"VERDICT: {verdict}\n\n"
        + str(data.get("summary", "")).strip()
        + _unanchorable_md(unanchorable)
    )
    gh.upsert_comment(repo, pr_number, "bot:review", summary)

    if AUTO_APPROVE and verdict == "approve" and not issues:
        gh.submit_review(
            repo,
            pr_number,
            "Approved by Sidekick after comprehensive review.",
            "APPROVE",
        )


def main():
    pr_number = int(os.environ["PR_NUMBER"])
    diff = Path(os.environ["PR_DIFF_FILE"]).read_text(
        encoding="utf-8", errors="replace"
    )
    run(gh.get_repo(), pr_number, diff)


if __name__ == "__main__":
    main()
