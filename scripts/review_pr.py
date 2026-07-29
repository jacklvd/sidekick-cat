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
import re
from pathlib import Path

from scripts import gh, limits, repo_context
from scripts.config import (
    AUTO_APPROVE,
    CONTEXT_MAX_TREE_CHARS,
    MAX_DIFF_CHARS,
    MODEL_INPUT_CHARS,
    MODELS,
    REVIEW_FILE_MAX_CHARS,
    REVIEW_LARGE_DIFF_CHARS,
    REVIEW_SYMBOL_FILES,
    REVIEW_SYMBOLS_PER_FILE,
)
from scripts.diff_anchors import anchors, block_path, file_blocks, number_diff, strip_noise
from scripts.llm_client import complete, truncate_diff

_INLINE_MARKER = "bot:review-inline"

# Friendly display names for review models — config's ids are ugly for user copy.
_MODEL_LABELS = {
    "z-ai/glm-5.2": "GLM-5.2",
    "nvidia/nemotron-3-ultra-550b-a55b": "Nemotron-3-Ultra",
    "deepseek-ai/deepseek-v4-pro": "DeepSeek-V4-Pro",
    "minimaxai/minimax-m2.7": "MiniMax-M2.7",
    "mistralai/mistral-medium-3.5-128b": "Mistral-Medium-3.5",
    "qwen/qwen3.5-122b-a10b": "Qwen3.5-122B",
    "qwen/qwen3.6-27b": "Qwen3.6-27B",
    "openai/gpt-oss-120b": "GPT-OSS-120B",
    "openai/gpt-4.1": "GPT-4.1",
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


# Top-level declarations across the languages these repos actually use. Anchored at
# column 0 on purpose: a nested def isn't the file's surface, it's noise. `const`/`let`
# catch the JS/TS `export const foo = () => {}` idiom, which is a definition in practice.
# dev-note: regex, not a parser — it misses Go methods (`func (r *T) Name()`, whose
# receiver breaks the identifier match) and anything exotic. That's fine: an extra symbol
# costs a few tokens and a missed one just restores today's behavior. Reach for a real
# parser (tree-sitter) only if reviewers start citing symbols this doesn't see.
_SYMBOL_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|pub\s+)?"
    r"(?:static\s+)?(?:async\s+)?"
    r"(?:def|class|func|fn|function|type|struct|interface|trait|enum|const|let|var)\s+"
    r"([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def file_symbols(text: str, limit: int = REVIEW_SYMBOLS_PER_FILE) -> list[str]:
    """`name (line N)` for each top-level declaration in a source file.

    The reviewer only ever sees the diff, so anything defined outside the changed hunks
    is invisible to it — and it reads that absence as a defect. A real review of this bot
    flagged `clean_space` as "not imported or defined" when the helper sat 120 lines above
    the hunk, unchanged and therefore absent from the diff. This is the cheap fix: names
    and line numbers only, no bodies, so a whole file costs a few dozen tokens.
    """
    out = []
    for match in _SYMBOL_RE.finditer(text or ""):
        line = text.count("\n", 0, match.start()) + 1
        out.append(f"{match.group(1)} (line {line})")
        if len(out) >= limit:
            break
    return out


def changed_paths(diff: str, limit: int = REVIEW_SYMBOL_FILES) -> list[str]:
    """Paths the diff touches, in order, deduped and capped."""
    seen: list[str] = []
    for block in file_blocks(diff):
        path = block_path(block)
        if path and path not in seen:
            seen.append(path)
        if len(seen) >= limit:
            break
    return seen


def build_symbol_outline(files: dict[str, str]) -> str:
    """Render `path -> top-level symbols` for the files the diff touches."""
    parts = []
    for path, text in files.items():
        symbols = file_symbols(text)
        if symbols:
            parts.append(f"{path}: " + ", ".join(symbols))
    return "\n".join(parts)


def build_file_contents(
    files: dict[str, str], max_chars: int, per_file_max: int = REVIEW_FILE_MAX_CHARS
) -> str:
    """Full, line-numbered text of the changed files, in diff order — what the symbol
    outline can only name. For large-context models only (see run.prompt_for): the
    reviewer sees callers, error paths, and surrounding conventions, not just the hunk.

    Files that don't fit are dropped whole, never sliced: a half-file is worse than an
    absent one (the reviewer reads a cut-off body as a defect). A file over per_file_max
    is skipped outright — it's almost certainly generated. Returns "" when nothing fits."""
    parts, size = [], 0
    for path, text in files.items():
        if not text or len(text) > per_file_max:
            continue
        numbered = "\n".join(
            f"{i}| {line}" for i, line in enumerate(text.splitlines(), 1)
        )
        block = f"{path}:\n{numbered}"
        if size + len(block) > max_chars:
            break
        parts.append(block)
        size += len(block)
    return "\n\n".join(parts)


def build_prompt(
    diff: str,
    conventions: str | None = None,
    max_chars: int = MAX_DIFF_CHARS,
    pr_text: str | None = None,
    project_context: str | None = None,
    symbol_outline: str | None = None,
    file_contents: str | None = None,
    prior_issues: str | None = None,
) -> str:
    """Compose the review prompt. `conventions` is the target repo's CLAUDE.md text
    (Cloud Run fetches it via API); when None, fall back to a local file (Actions).
    `project_context` is the cached repo-context doc (scripts.repo_context) — what
    the rest of the project looks like, so the reviewer isn't judging the diff in a
    vacuum. `pr_text` is the PR title+body — what the change CLAIMS to do, so the
    reviewer can flag code that contradicts its own description. `symbol_outline` is
    what each changed file *already* defines outside the hunks, so the reviewer stops
    reporting existing helpers as undefined (see file_symbols). `file_contents` is the
    full, line-numbered text of changed files — for large-budget models only, so the
    reviewer sees callers, error paths, and surrounding conventions. `prior_issues` is
    the still-open issues from the previous review, so an incremental re-review can
    confirm fixes instead of re-discovering them. `max_chars` caps the diff — larger
    on the high-TPM large-PR path."""
    if conventions is None and _CONVENTIONS.exists():
        conventions = _CONVENTIONS.read_text(encoding="utf-8")
    parts = []
    if conventions:
        parts.append("Repo conventions (CLAUDE.md):\n" + conventions)
    if project_context:
        parts.append("Project context:\n" + project_context[:CONTEXT_MAX_TREE_CHARS])
    if pr_text:
        parts.append("PR title and description (what the author says it does):\n" + pr_text)
    if symbol_outline:
        parts.append(
            "Top-level symbols already defined in the changed files (name and line, at "
            "this PR's head). The diff shows you only the changed lines, so a helper "
            "listed here EXISTS even when you cannot see its definition — do not report "
            "it as missing, undefined, or unimported:\n" + symbol_outline
        )
    if file_contents:
        parts.append(
            "Full contents of the changed files at this PR's head, line-numbered. Use "
            "them to judge callers, error paths, and the conventions around each hunk — "
            "but only raise issues on lines the diff actually changed:\n" + file_contents
        )
    if prior_issues:
        parts.append(
            "Issues you flagged on the previous review that are still open. If the new "
            "changes fix one, do NOT report it again; if it is still present, report it "
            "at its current line. In your summary, say how many of these the new changes "
            "addressed:\n" + prior_issues
        )
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


def _clean_prior_body(body: str) -> str:
    """Strip the inline marker and a leading `**[severity]**` prefix from a stored
    comment body, leaving just the human-readable issue text for the prompt."""
    text = (body or "").split(f"<!-- {_INLINE_MARKER} -->")[0].strip()
    if text.startswith("**[") and "]**" in text:
        text = text.split("]**", 1)[1].strip()
    return " ".join(text.split())  # collapse newlines/whitespace to one line


def prior_issues_text(comments, scope) -> str:
    """The still-open inline issues from the previous review, so an incremental
    re-review confirms fixes instead of re-discovering them. Thread ROOTS only
    (a reply is not an issue), filtered to files in `scope` (the delta's files —
    the model never re-judged anything outside it, so its old threads must not
    appear as 'still open')."""
    lines = []
    for c in comments:
        if getattr(c, "in_reply_to_id", None) is not None:
            continue
        if c.path not in scope:
            continue
        lines.append(f"- {c.path}:{c.line} — {_clean_prior_body(c.body or '')}")
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
    # What the changed files already define outside the hunks. Read at head_sha, not the
    # default branch: a helper this PR itself adds doesn't exist on the default branch, and
    # reporting it missing is the very mistake the outline exists to prevent.
    # Fetched once at head_sha: the outline names these files' symbols, and (for
    # large-budget models) prompt_for folds in their full bodies. Same API calls,
    # the text is no longer thrown away.
    changed_files = {
        path: text
        for path in changed_paths(diff)
        if (text := gh.get_file_text(repo, path, ref=head_sha))
    }
    outline = build_symbol_outline(changed_files)
    used: list = []  # complete() appends the (provider, model) that answered
    # Numbered BEFORE truncation so the prefixes always match the real file lines.
    numbered = number_diff(diff)

    # On an incremental run, remind the model what it flagged last time so it confirms
    # fixes instead of re-discovering (or silently dropping) them. Scoped to the delta's
    # files — the model never re-judged anything outside them. set(anchors(diff)) is the
    # delta's file set; uncapped, unlike changed_paths.
    prior = ""
    if incremental:
        prior = prior_issues_text(
            gh.get_inline_comments(repo, pr_number, _INLINE_MARKER), set(anchors(diff))
        )

    def prompt_for(model: str) -> str:
        # Prompt sized per attempt: large-context models (NVIDIA) take the diff at
        # their own budget; the Groq/GitHub fallbacks keep the tier cap they were
        # TPM-tuned for. Same diff, different truncation point.
        cap = MODEL_INPUT_CHARS.get(model, max_chars)
        # Full changed-file bodies only for large-budget rungs, and only in whatever
        # cap the diff leaves free — a monster diff fills the budget and the outline
        # still carries the cross-file signal. Small rungs never get bodies (no room).
        files_text = None
        if model in MODEL_INPUT_CHARS:
            budget = max(0, cap - min(len(numbered), cap))
            files_text = build_file_contents(changed_files, budget) or None
        return build_prompt(
            numbered, conventions, cap, pr_text, project_context, outline,
            files_text, prior,
        )

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
