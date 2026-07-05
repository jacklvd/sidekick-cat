"""Shared constants for the flow scripts and the server."""

# GitHub Models (OpenAI-compatible) endpoint. Used by the legacy models_client smoke.
MODELS_BASE_URL = "https://models.github.ai/inference"

# Publisher-prefixed model ids. Light model for summaries, stronger for full reviews.
SUMMARY_MODEL = "openai/gpt-4o-mini"
REVIEW_MODEL = "openai/gpt-4.1"

# --- provider-abstracted LLM client (llm_client.py) ---
# Both backends are OpenAI-compatible. Groq is free (no card); GitHub Models is the
# managed fallback. `complete()` walks the task's list in order, crossing providers
# freely, so precedence can interleave them (Groq → GitHub → Groq) — which a
# provider-then-model nesting couldn't express.
GROQ_BASE = "https://api.groq.com/openai/v1"
GH_MODELS_BASE = MODELS_BASE_URL
# task -> ordered list of (provider, model). Tried first-to-last; first success wins.
# Review is size-routed (see review_pr.run):
#   "review"       — small PRs: smartest first (qwen → gpt-4.1 → gpt-oss).
#   "review_large" — big PRs: highest-TPM first so the whole diff fits ONE pass
#                    (Llama-4 Scout 30K TPM → groq/compound 70K → gpt-4.1). The
#                    smart models above cap at 6-8K TPM and would truncate a big diff.
MODELS = {
    "summary": [("groq", "llama-3.1-8b-instant"), ("github", SUMMARY_MODEL)],
    "context": [("groq", "llama-3.1-8b-instant"), ("github", SUMMARY_MODEL)],
    "review": [
        ("groq", "qwen/qwen3-32b"),
        ("github", REVIEW_MODEL),
        ("groq", "openai/gpt-oss-120b"),
    ],
    "review_large": [
        ("groq", "meta-llama/llama-4-scout-17b-16e-instruct"),
        ("groq", "groq/compound"),
        ("github", REVIEW_MODEL),
    ],
}

# Cap diff size before sending so big PRs don't blow the free-tier token budget.
# GitHub Models' free tier caps the *whole request* at 8000 tokens for gpt-4.1.
# Budget: system prompt + CLAUDE.md conventions + diff must fit, so keep the diff
# well under that (~3-4 chars/token for code). dev-note: raise if the cap lifts.
MAX_DIFF_CHARS = 12000
# Generated/vendored file patterns stripped from the diff before review — they
# waste the small models' token budget and can flip a PR onto the large-model
# path for no reviewable content. fnmatch globs; `*` crosses `/` (as LABEL_RULES).
NOISE_GLOBS = [
    "*.lock",
    "package-lock.json",
    "*.min.js",
    "*.min.css",
    "*.svg",
    "*.map",
    "*.snap",
    "node_modules/*",
    "vendor/*",
    "dist/*",
    "build/*",
]

# Diffs over MAX_DIFF_CHARS route to the "review_large" model tier (Scout's 30K TPM
# holds far more than qwen's 6K), so the large path may send a bigger diff before
# truncating. ~4 chars/token → ~14k tokens, comfortably under 30K TPM.
REVIEW_LARGE_DIFF_CHARS = 56000

# Deterministic PR-open.
# Sections the PR description must contain (matched as line-leading headings,
# case-insensitive). "TL;DR" (no trailing colon) so both "## TL;DR" and
# "## TL;DR:" match — validate_pr does a startswith check.
REQUIRED_SECTIONS = ["TL;DR", "What", "Why", "Test"]

# Changed-path glob -> label. fnmatch globs; `*` also crosses `/`.
LABEL_RULES = {
    "*.py": "python",
    "*.md": "documentation",
    "docs/*": "documentation",
    ".github/*": "github-actions",
}

# /review command.
TRIGGER_REVIEW = "/review"
# /merge command. Deterministic, no AI.
TRIGGER_MERGE = "/merge"
# Let the bot APPROVE when the review finds no blockers. Off by default: a bot/App
# approval only counts toward branch protection if the repo is configured to allow it.
AUTO_APPROVE = False

# Repo project context for /review. Cached as a GitHub Issue (bot:context),
# refreshed manually (/context) or lazily by /review when missing/stale.
TRIGGER_CONTEXT = "/context"
CONTEXT_REFRESH_DAYS = 30
CONTEXT_MAX_TREE_CHARS = 6000
CONTEXT_KEY_FILES = [
    "README.md", "CLAUDE.md", "pyproject.toml", "package.json",
    "go.mod", "Cargo.toml", "requirements.txt",
]

# --- cost safeguards. In-memory per instance, or shared via Firestore when
# LIMITS_BACKEND=firestore. Defaults sit well under Groq's ~1000/day free cap,
# so we stop ourselves long before the provider does. ---
GLOBAL_DAILY_MAX = 400  # max LLM calls/day across everything
REPO_DAILY_MAX = 100  # max LLM calls/day per repo
PR_DAILY_MAX = 5  # max reviews+summaries/day per PR
BREAKER_FAILS = 5  # consecutive provider/API failures...
BREAKER_WINDOW_S = 300  # ...within this window...
BREAKER_COOLDOWN_S = 900  # ...opens the breaker for this long
