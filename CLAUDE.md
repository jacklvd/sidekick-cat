# House rules for sidekick-cat

Conventions for this repo. `review_pr.py` feeds this file to the AI reviewer, so
keep it accurate — it doubles as the review rubric.

## What this is

A private GitHub review bot that posts under its own **sidekick-cat** GitHub App
identity. It runs as one Google Cloud Run webhook service (`server/`), installed
on all repos of the account, so onboarding a new repo needs zero per-repo setup.
AI parts use free-tier LLMs: Groq primary, GitHub Models fallback
(`scripts/llm_client.py`). Architecture: [`README.md`](README.md#architecture).
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
  `GROQ_API_KEY` / `MODELS_PAT` via `scripts/llm_client.py`. Never cross them.
- **Idempotent comments:** every bot comment carries a hidden `<!-- bot:* -->`
  marker and is written via `gh.upsert_comment`, so re-runs edit instead of spam.
- **Inline review threads** (`/review`) carry `<!-- bot:review-inline -->` and are
  reconciled per run keyed on `(path, line)`: matches kept/edited (preserving the
  thread + its resolution), stale ones deleted, new ones created. `/merge` blocks
  while any review thread is unresolved (resolution is the merge gate), so the
  reviewer never submits a formal REQUEST_CHANGES that would outlive resolution.
  `/merge` also re-checks the live PR body against `REQUIRED_SECTIONS` — gate on
  fresh state, never on a previously posted bot comment (comments go stale).
- **Project context:** `repo_context.py` caches a per-repo summary (file tree + key
  files, one LLM call) as a hidden-marker (`bot:context`) GitHub Issue — closed
  immediately since it's metadata, not actionable. `/review` folds it into every
  prompt via `ensure_fresh()`, regenerating only when missing or when the issue's
  `updated_at` is older than `CONTEXT_REFRESH_DAYS`. `/context` forces a refresh.
  No database: GitHub Issues are the free, host-agnostic cache, same principle as
  `limits.py` using Firestore only for counters, never for content.
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
- Bot-facing copy uses the 🐱 emoji and the "Sidekick" persona.

## Workflows

- Pin every action to a stable tag.
- Only two GitHub Actions workflows remain: `smoke-bot.yml` and
  `smoke-models.yml`, both manual (`workflow_dispatch`) smoke tests. The actual
  bot flows (pr_open, `/review`, `/merge`, `/context`) are dispatched by the
  Cloud Run webhook (`server/app.py`), not by workflow files — changes there take
  effect on deploy (`infra/deploy.sh`), not on merge to `main`.
