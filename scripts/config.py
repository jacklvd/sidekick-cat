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
# so it leads the review tiers for quality. Every model here honors json_object mode cleanly
# and keeps any reasoning out of message.content (verified live) — required, since /review
# calls with json_mode=True.
# dev-note: smoke any new NVIDIA model in *json* mode, on the *real* review prompt, before
# adding it. Not just plain, and not on a toy prompt — a NIM model can fail json while passing
# plain, and it fails at 200 (not 400), so _call's retry-plain never fires. Rejected this way,
# all verified live on 2026-07-13:
#   kimi-k2.6            — returns garbage in json mode.
#   minimaxai/minimax-m3 — 200 with `choices: []`, every time, json *and* plain. Empty body.
#   deepseek-v4-flash    — prefixes the JSON with a literal "We" (`We{"verdict": ...}`), 3/3,
#                          so json.loads dies in review_pr.parse_response. Same trap as kimi.
#                          Note this is the *flash* model; deepseek-v4-pro is clean and wired.
#
# NVIDIA rungs are ordered by measured behavior on the real review prompt, not by parameter
# count (6 runs each, 2026-07-13 — median latency / issues found on a diff with a known bug):
#   GLM-5.2       17.1s / 2.5   flagship coding model; still the best reviewer here
#   Nemotron      9.5s  / 2.3   fastest *and* nearly GLM's hit rate — the standout
#   DeepSeek-Pro  20.4s / 1.3
#   MiniMax-M2.7  incumbent
#   Mistral       23.3s / 1.2   slowest; 139s on a 200K-char prompt
#   Qwen3.5       30-140s       last: the only NVIDIA model that returns an empty body
#                               (~1 call in 8), and the slowest. Kept because it's a fine
#                               reviewer when it answers, and NoReply now absorbs the flake.
# All six hold a 200K-char prompt (~50K tokens), which is the point of leading with NIM.
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_GLM = "z-ai/glm-5.2"  # flagship coding/agentic; review primary, both size tiers
NVIDIA_NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b"  # fastest large-context rung
NVIDIA_DEEPSEEK = "deepseek-ai/deepseek-v4-pro"  # NB: the *pro* model; -flash is broken, see above
NVIDIA_MINIMAX = "minimaxai/minimax-m2.7"  # reasoning model; review fallback
NVIDIA_MISTRAL = "mistralai/mistral-medium-3.5-128b"
NVIDIA_QWEN = "qwen/qwen3.5-122b-a10b"  # sparse MoE; flaky (see ordering note), so it goes last

# task -> ordered list of (provider, model). Tried first-to-last; first success wins.
# Review is size-routed (see review_pr.run): both tiers run the same six NVIDIA rungs first (see
# the ordering note above), then leave NIM for the Groq/GitHub backstops. The two tiers differ
# only in what happens *after* NVIDIA — and in how much diff each rung is handed
# (MODEL_INPUT_CHARS), which is the whole reason NVIDIA leads.
#   "review"       — small PRs: …NVIDIA… → qwen3.6-27b → gpt-4.1 → gpt-oss-120b.
#   "review_large" — big PRs:   …NVIDIA… → gpt-4.1 → gpt-oss-120b (no qwen3.6: its Groq TPM
#                    budget can't hold a large diff, so it would only ever 413 here).
# A long chain is cheap: a rung costs nothing unless the one above it fails.
# "context" also leads with GLM-5.2: the repo-context brief is folded into every review prompt,
# so a sharper brief pays off downstream. "summary" stays on Groq — a throwaway per-PR one-liner
# not worth NVIDIA's RPM budget.
#
# dev-note: Groq decommissions `qwen/qwen3-32b` (was the "review" rung) and
# `meta-llama/llama-4-scout-17b-16e-instruct` (was "review_large") on 2026-07-17, free/dev tier.
# Replaced with the models Groq itself names, both verified live in json mode on 2026-07-13:
# qwen3-32b → `qwen/qwen3.6-27b`, Scout → `openai/gpt-oss-120b`.
#
# `review_large` fallbacks are ordered by how big a request each will actually accept, because
# on a size-routed tier that is the only thing that decides whether a rung can answer at all
# (measured live, prompt chars before a 413): NVIDIA ~unbounded → gpt-4.1 dies past ~18K →
# gpt-oss-120b dies past ~11K. A 413 is handled, not fatal — complete() moves to the next model
# without tripping the breaker — so the small rungs still earn their place on the mid-size PRs
# that land just over MAX_DIFF_CHARS, while the NVIDIA rungs carry the monsters.
#
# dev-note: `groq/compound` was dropped from review_large, not replaced. It 413s on a 3.6K-char
# prompt — smaller than anything this tier sends — so it could never answer here; it only ever
# burned a fallback attempt. (Its 429 also names `meta-llama/llama-4-scout` as its backing
# model, so the 2026-07-17 decommission likely lands on it anyway.)
MODELS = {
    "summary": [("groq", "llama-3.1-8b-instant"), ("github", SUMMARY_MODEL)],
    "context": [("nvidia", NVIDIA_GLM), ("groq", "llama-3.1-8b-instant"), ("github", SUMMARY_MODEL)],
    "review": [
        ("nvidia", NVIDIA_GLM),
        ("nvidia", NVIDIA_NEMOTRON),
        ("nvidia", NVIDIA_DEEPSEEK),
        ("nvidia", NVIDIA_MINIMAX),
        ("nvidia", NVIDIA_MISTRAL),
        ("nvidia", NVIDIA_QWEN),
        ("groq", "qwen/qwen3.6-27b"),
        ("github", REVIEW_MODEL),
        ("groq", "openai/gpt-oss-120b"),
    ],
    "review_large": [
        ("nvidia", NVIDIA_GLM),
        ("nvidia", NVIDIA_NEMOTRON),
        ("nvidia", NVIDIA_DEEPSEEK),
        ("nvidia", NVIDIA_MINIMAX),
        ("nvidia", NVIDIA_MISTRAL),
        ("nvidia", NVIDIA_QWEN),
        ("github", REVIEW_MODEL),
        ("groq", "openai/gpt-oss-120b"),
    ],
    # Conversational reply in a review thread (plain prose, not JSON) — GLM leads for quality,
    # a cheap Groq backstop so a reply still lands if NIM is down.
    "reply": [
        ("nvidia", NVIDIA_GLM),
        ("nvidia", NVIDIA_NEMOTRON),
        ("groq", "llama-3.1-8b-instant"),
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
# Every NVIDIA rung gets the large budget — all six were verified live against a 203K-char
# prompt (~50K tokens). A model missing from this map silently drops to the caller's tier cap
# (12K chars on the small path), so it would review a fraction of the diff and never complain.
MODEL_INPUT_CHARS = {
    m: NVIDIA_INPUT_CHARS
    for m in (NVIDIA_GLM, NVIDIA_NEMOTRON, NVIDIA_DEEPSEEK, NVIDIA_MINIMAX, NVIDIA_MISTRAL, NVIDIA_QWEN)
}

# Per-provider output cap. Not tuning — both entries fix a real, reproduced failure, and
# the two providers fail in *opposite* directions, which is why one global number can't work.
#
# Reasoning models spend tokens thinking before they emit a single character of JSON, so the
# output cap has to cover think + answer. Too low and the model is cut off mid-thought:
#   nvidia — NIM defaults qwen3.5-122b to 128 completion tokens. A toy reply fits, so the
#     model smokes clean, but a real review's JSON is guillotined mid-string. It still comes
#     back 200, choices populated, text opening `{"verdict": ...` — so nothing raises and
#     parse_response silently returns None. The review is lost with no error anywhere.
#   groq — with no cap, qwen3.6-27b burns its default budget inside <think> and emits nothing,
#     so Groq itself rejects the call (400 json_validate_failed, failed_generation empty).
#     _call then retries plain and gets back raw reasoning prose that won't parse.
# But too high and Groq 413s: its free tier bills max_tokens against the per-request TPM
# budget *up front*, so 8000 is an instant "request too large" even on "say hello". 4000 both
# passes that check and leaves qwen3.6 room to finish (it used 3855 on a 2-issue review).
#
# GitHub is absent on purpose — its default already lets a review finish.
# dev-note: every number here verified live 2026-07-13. If a new model returns
# finish_reason='length', it needs more room, not a smaller diff.
PROVIDER_MAX_TOKENS = {"nvidia": 8000, "groq": 4000}

# Symbol outline: the top-level names each changed file already defines, fetched at the
# PR head and folded into the review prompt. The reviewer only ever sees the diff, so a
# helper defined outside the changed hunks is invisible to it — and it reports that
# absence as a defect (a real review of this bot called `clean_space` "not imported or
# defined" when it sat 120 lines above the hunk). Names + line numbers only, no bodies,
# so a file costs a few dozen tokens. Costs one contents-API call per changed file, capped
# here. dev-note: the cap means a PR touching more than REVIEW_SYMBOL_FILES files outlines
# only the first few — the long tail is where this matters least (a 40-file PR is already
# being skimmed), so it isn't worth the extra calls.
REVIEW_SYMBOL_FILES = 12
REVIEW_SYMBOLS_PER_FILE = 60

# Full changed-file contents folded into the review prompt for large-budget models
# (see review_pr.build_file_contents / run). The symbol outline gives names only; the
# large-context NVIDIA rungs can take the real bodies, so the reviewer sees callers,
# error paths, and the conventions around each hunk — not just the changed lines. A
# single file larger than this is skipped (almost always generated/minified — the
# NOISE_GLOBS strip catches most, this catches the rest without a second glob pass).
REVIEW_FILE_MAX_CHARS = 24000

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

# Conventional-commit type -> label, for the *kind* of change (the path rules above only
# say which language it touched). The title is the signal because it is the author stating
# their intent outright; the diff can only be read for it, and reads badly. A bugfix
# usually *adds* lines (a guard, a validation) and a cleanup usually removes them, so
# "mostly deletions" means "fix" far less often than it looks — this repo's own
# `fix: survive Groq's July 17 decommission` was net +180/-24.
# Names match GitHub's defaults where they exist, so most repos already have them
# (gh.set_managed_labels creates any that don't, colored from LABEL_COLORS).
KIND_LABELS = {
    "feat": "enhancement",
    "feature": "enhancement",
    "fix": "bug",
    "bugfix": "bug",
    "hotfix": "bug",
    "refactor": "refactor",
    "perf": "refactor",
    "style": "refactor",
    "docs": "documentation",
    "test": "tests",
    "tests": "tests",
    "chore": "chore",
    "build": "chore",
    "ci": "chore",
    "revert": "chore",
}

# Last resort when neither the title, the branch, nor the diff says what kind of change
# this is (typical of an outside collaborator: "Update the scraper filter" on `patch-1`).
# A human triaging is cheaper than a wrong guess, and the label says exactly that.
TRIAGE_LABEL = "needs-triage"

# Hex colors (no leading #) for labels the bot creates. GitHub's own defaults for the
# names it ships with, linguist's language color for `python`; the rest picked to keep
# the two axes visually distinct — path labels cool, kind labels warm. Anything without an
# entry gets DEFAULT_LABEL_COLOR, which also doubles as "the bot painted this, and nobody
# has repainted it" — gh.set_managed_labels only recolors labels still wearing it.
DEFAULT_LABEL_COLOR = "ededed"
LABEL_COLORS = {
    "python": "3572a5",
    "documentation": "0075ca",
    "github-actions": "2088ff",
    "enhancement": "a2eeef",
    "bug": "d73a4a",
    "refactor": "fbca04",
    "tests": "0e8a16",
    "chore": "cfd3d7",
    TRIAGE_LABEL: "e4e669",
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
