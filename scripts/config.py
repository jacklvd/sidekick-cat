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
# NVIDIA NIM — OpenAI-compatible free endpoint (build.nvidia.com). Auth: NVIDIA_API_KEY
# (an `nvapi-...` key). RPM-limited (not Groq's tokens-per-minute ceiling), large-context,
# so it leads the review tiers for quality. Both models honor json_object mode cleanly and
# keep any reasoning out of message.content (verified live) — required, since /review calls
# with json_mode=True. dev-note: Kimi-K2.6 was rejected — it returns garbage in json mode on
# NIM (200, not 400, so _call's retry-plain never fires); smoke any new NVIDIA model in json
# mode before adding it here, not just plain.
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_GLM = "z-ai/glm-5.2"  # flagship coding/agentic; review primary, both size tiers
NVIDIA_MINIMAX = "minimaxai/minimax-m2.7"  # reasoning model; review fallback

# task -> ordered list of (provider, model). Tried first-to-last; first success wins.
# Review is size-routed (see review_pr.run), NVIDIA-first for quality then Groq/GitHub fallback:
#   "review"       — small PRs: GLM-5.2 → MiniMax-M2.7 → qwen → gpt-4.1 → gpt-oss.
#   "review_large" — big PRs: GLM-5.2 (large ctx) → MiniMax-M2.7 → Scout 30K TPM → compound → gpt-4.1.
#                    The Groq "smart" models cap at 6-8K TPM and would truncate a big diff.
# "context" also leads with GLM-5.2: the repo-context brief is folded into every review prompt,
# so a sharper brief pays off downstream. "summary" stays on Groq — a throwaway per-PR one-liner
# not worth NVIDIA's RPM budget.
MODELS = {
    "summary": [("groq", "llama-3.1-8b-instant"), ("github", SUMMARY_MODEL)],
    "context": [("nvidia", NVIDIA_GLM), ("groq", "llama-3.1-8b-instant"), ("github", SUMMARY_MODEL)],
    "review": [
        ("nvidia", NVIDIA_GLM),
        ("nvidia", NVIDIA_MINIMAX),
        ("groq", "qwen/qwen3-32b"),
        ("github", REVIEW_MODEL),
        ("groq", "openai/gpt-oss-120b"),
    ],
    "review_large": [
        ("nvidia", NVIDIA_GLM),
        ("nvidia", NVIDIA_MINIMAX),
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

# Per-model bulk-payload budget: chars of diff (review) or file tree (context) one
# request may carry. The NVIDIA NIM models are large-context and RPM-limited (not
# token-billed), so they take far more than the Groq-TPM / GitHub-request-cap
# defaults; models not listed keep the caller's tier cap (MAX_DIFF_CHARS /
# REVIEW_LARGE_DIFF_CHARS / CONTEXT_MAX_TREE_CHARS). Callers pass complete() a
# prompt *builder* so each fallback attempt is sized for the model actually tried.
# dev-note: 200K chars ≈ 50K tokens — deliberately conservative for 128K-ctx
# models; raise after a live /review smoke on a monster PR.
NVIDIA_INPUT_CHARS = 200_000
MODEL_INPUT_CHARS = {
    NVIDIA_GLM: NVIDIA_INPUT_CHARS,
    NVIDIA_MINIMAX: NVIDIA_INPUT_CHARS,
}

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
# Heads (module docstring + imports) of the N largest source files, folded into
# the context prompt for large-budget models only (see repo_context) — paths
# alone can't show what calls what. Costs N extra contents-API calls per refresh,
# i.e. ~once per CONTEXT_REFRESH_DAYS — negligible.
CONTEXT_HEAD_FILES = 12
CONTEXT_HEAD_LINES = 40

# --- cost safeguards. In-memory per instance, or shared via Firestore when
# LIMITS_BACKEND=firestore. Defaults sit well under Groq's ~1000/day free cap,
# so we stop ourselves long before the provider does. ---
GLOBAL_DAILY_MAX = 400  # max LLM calls/day across everything
REPO_DAILY_MAX = 100  # max LLM calls/day per repo
PR_DAILY_MAX = 5  # max reviews+summaries/day per PR
BREAKER_FAILS = 5  # consecutive provider/API failures...
BREAKER_WINDOW_S = 300  # ...within this window...
BREAKER_COOLDOWN_S = 900  # ...opens the breaker for this long
