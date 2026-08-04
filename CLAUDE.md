# House rules for sidekick-cat

Conventions for this repo. `review_pr.py` feeds this file to the AI reviewer, so
keep it accurate — it doubles as the review rubric.

## What this is

A private GitHub review bot that posts under its own **sidekick-cat** GitHub App
identity. It runs as one Google Cloud Run webhook service (`server/`), installed
on all repos of the account, so onboarding a new repo needs zero per-repo setup.
AI parts use free-tier LLMs: NVIDIA NIM primary for reviews/context/replies, Groq
and GitHub Models as backstops (`scripts/llm_client.py`). Architecture: [`README.md`](README.md#architecture).
Shutting it down: the setup wizard's **Teardown** tab (`tools/setup_wizard.py`).

## Code style

- Python 3.13, managed with `uv`. Standard library first; the only runtime deps
  are `openai` and `PyGithub` for the flow logic, plus `fastapi`/`uvicorn`/
  `google-cloud-firestore` for the Cloud Run service. Don't add a dependency for
  what a few lines do.
- Keep each script a thin entry point: a pure, testable function for the logic
  (e.g. `blockers`, `verdict_of`, `missing_sections`) plus a small `main()` that
  does the I/O. Pure functions get an offline self-check in
  `scripts/tests/test_pr_logic.py` — no network, no token.
- Module docstring on every script saying what it does and which env vars it reads.
- Comments explain *why*, not *what*. Mark deliberate shortcuts with `dev-note:`.

## Bot behavior conventions

- **Token separation:** GitHub writes use the App installation token, minted by
  `server/gh_app_auth.py` from `APP_ID`/`APP_KEY` (Cloud Run) or read from
  `GH_TOKEN` (scripts invoked standalone, e.g. from a shell). Inference uses
  `NVIDIA_API_KEY` / `GROQ_API_KEY` / `MODELS_PAT` via `scripts/llm_client.py`.
  Never cross them.
- **Idempotent comments:** every bot comment carries a hidden `<!-- bot:* -->`
  marker and is written via `gh.upsert_comment`, so re-runs edit instead of spam.
- **Reviews run automatically.** `server/router.py` decides which PR events earn
  one (opened / synchronize on a non-draft, never an `edited`); `/review` is the
  manual override and the only way to review a draft. There is no separate AI
  summary comment — the review opens with one.
- **Inline review threads** carry `<!-- bot:review-inline -->` and are reconciled
  per run keyed on `(path, line)`: matches kept/edited (preserving the thread),
  stale ones (issue no longer flagged, in the reviewed scope) **resolved** with a
  note — a visible collapsed trail, not deleted — and new ones created. If a
  resolved thread's issue recurs at the same `(path, line)`, it is **un-resolved**
  so the gate re-catches it. A thread that has human replies is never deleted,
  only noted. Resolving is GraphQL (`gh.resolve_thread` / `unresolve_thread`, keyed
  on the thread node id from `gh.review_thread_state`), so `review_pr.run` takes a
  `token`; it degrades safe (note only, no resolve) if that read fails. `/merge`
  blocks while any review thread is unresolved (resolution is the merge gate), so
  the reviewer never submits a formal REQUEST_CHANGES that would outlive
  resolution. `/merge` also re-checks the live PR body against `REQUIRED_SECTIONS`
  — gate on fresh state, never on a previously posted bot comment (comments go
  stale).
- **Merge check:** a reviewed head carries a `Sidekick` check run, written
  idempotently by `gh.upsert_check_run` (keyed on the name, same anti-spam
  principle as `upsert_comment`). `check_run.conclude` (authoritative, on the
  review path) posts `in_progress` before the review and the verdict after;
  `check_run.refresh` (deferential, on thread-resolve and description `edited`
  events) recomputes an already-concluded check but **bails on
  `in_progress`/`neutral` and never creates one** — the fix for a check going green
  mid-review. Gate precedence: reviewing → review-failed (`neutral`, doesn't block)
  → unresolved threads → missing description → success. The check reuses
  `merge_pr.PR_QUERY` but **never reads `mergeStateStatus` / `reviewDecision`** — a
  required check reading those deadlocks. Requires the App's **Checks: write**
  permission and the **Pull request review thread** event.
- **Thread replies:** an authorized author replying inside a bot review thread
  (`bot:review-inline` root) gets an in-thread answer via `reply_thread.py`
  (webhook `pull_request_review_comment`, routed to `thread_reply`). Requires the
  **Pull request review comment** event on the GitHub App. Replies carry
  `<!-- bot:review-reply -->` and never re-trigger the bot (the loop guard drops
  bot senders), and `gh.get_inline_comments` returns thread roots only so the
  reconciler never mistakes a reply for a stale review comment.
- **Project context:** `repo_context.py` caches a per-repo summary (file tree
  + key files + biggest source-file heads, one LLM call) as a hidden-marker
  (`bot:context`) GitHub Issue — closed
  immediately since it's metadata, not actionable. Every review folds it into the
  prompt via `ensure_fresh()`, regenerating when missing, when the issue's
  `updated_at` is older than `CONTEXT_REFRESH_DAYS`, or when `CONTEXT_REFRESH_PRS`
  PRs have merged since it (`gh.merged_since`, counted live from GitHub against the
  issue's own `updated_at` — no counter to persist; degrades to age-only if that
  read fails). Age covers quiet repos, the PR count covers active ones where a
  shipped refactor makes the brief wrong long before the clock says so. `/context`
  forces a refresh.
  No database: GitHub Issues are the free, host-agnostic cache, same principle as
  `limits.py` using Firestore only for counters, never for content.
- **Review prompts carry more than the diff:** a symbol outline of each changed
  file (names + line numbers, `REVIEW_SYMBOL_FILES` cap) so a helper defined
  outside the hunks isn't reported as undefined, plus the **full text of the
  changed files** for large-context (NVIDIA) models — the outline names cross-file
  helpers, the bodies show callers and error paths around each hunk.
- **Least privilege:** the manual smoke workflows (`.github/workflows/`) set
  `permissions:` to only what they need — only `smoke-models.yml` gets
  `models: read`. The real pr_open/review/merge/context flows run on Cloud Run
  and get their scopes from the GitHub App's own permissions, not workflow YAML.
- **Slash commands** (`/review`, `/merge`, `/context`) are gated in
  `server/router.py`'s `classify()` on `author_association`
  (OWNER/MEMBER/COLLABORATOR) so the bot can't trigger itself and outsiders can't.
- The webhook payload carries the PR/issue number and installation id directly
  (`server/router.py`); fetch the diff via REST (`scripts.gh.get_pr_diff`), not
  the `gh` CLI — there's no checkout on Cloud Run.
- Respect the free rate limit: truncate diffs (`truncate_diff`) and handle 429
  gracefully (return a friendly message, don't crash).
- Bot-facing copy uses the "Sidekick" persona and its face: `config.ICON`
  (neutral/positive) or `config.ICON_HMM` (blockers, errors, waiting). Both are the
  🐱 emoji rather than an `<img>`, so they render the same on markdown comment
  bodies and on the plain-text surfaces that can't hold an image — check-run titles
  (`check_run.py`) and the context Issue title (`repo_context.py`). Interpolate the
  constants, don't hardcode the emoji.

## Workflows

- Pin every action to a stable tag.
- Only two GitHub Actions workflows remain: `smoke-bot.yml` and
  `smoke-models.yml`, both manual (`workflow_dispatch`) smoke tests. The actual
  bot flows (pr_open, the automatic review, `/review`, `/merge`, `/context`, thread
  replies) are dispatched by the Cloud Run webhook (`server/app.py`), not by
  workflow files — changes there take effect on deploy (`infra/deploy.sh`), not on
  merge to `main`.

## GitHub App setup

The App needs these beyond the original set, or the flows fail silently:

- **Permissions:** Checks: write (the `Sidekick` check run).
- **Webhook events:** Pull request review thread (resolve/unresolve → check
  recompute) and Pull request review comment (author replies → `reply_thread`).
