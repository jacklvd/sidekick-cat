"""Per-repo project context for /review — a summary, folder structure, and
per-file highlights, cached as a hidden-marker GitHub Issue so /review can judge
a diff against more than the diff itself.

Generated from the repo's file tree (with blob sizes), a handful of key
manifest/doc files, and — for large-budget models — the heads of its biggest
source files (docstrings + imports show what calls what). One LLM call (task
"context", NVIDIA GLM-first — the brief is folded into every
review prompt, so it gets the strong tier; large-context models also see a much
bigger tree, see config.MODEL_INPUT_CHARS). Refreshed manually via /context or
lazily by /review when missing or older than CONTEXT_REFRESH_DAYS — staleness
reads the issue's own `updated_at`, no separate bookkeeping needed.
"""

from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import PurePosixPath

from scripts import gh, limits
from scripts.config import (
    CONTEXT_HEAD_FILES,
    CONTEXT_HEAD_LINES,
    CONTEXT_KEY_FILES,
    CONTEXT_MAX_TREE_CHARS,
    CONTEXT_REFRESH_DAYS,
    MODEL_INPUT_CHARS,
    NOISE_GLOBS,
)
from scripts.llm_client import complete

_MARKER = "bot:context"
_TITLE = "🐱 Sidekick project context (auto-generated, do not edit)"

# dev-note: kept under ~500 words because review_pr.build_prompt slices the brief
# at CONTEXT_MAX_TREE_CHARS — a longer brief would be cut mid-sentence downstream.
_SYSTEM = (
    "You are a senior engineer writing an onboarding brief for a code reviewer "
    "who has never seen this repository. Given its file tree and a few key "
    "files, write a Markdown brief with exactly these sections:\n"
    "**What this is** — 2-4 sentences: purpose, stack, how it runs.\n"
    "**Layout** — top-level folders/modules, one line of purpose each, plus "
    "notable relationships (what calls what, what owns what state).\n"
    "**Conventions** — coding/testing/dependency rules a reviewer should "
    "enforce, inferred from the docs and manifests; skip generic advice.\n"
    "**Review watch-fors** — the riskiest spots: trust boundaries, invariants, "
    "easy-to-break couplings between the parts above.\n"
    "Under 500 words total. No preamble, no restating the file list verbatim — "
    "synthesize."
)


def _fmt_size(n: int) -> str:
    return f"{n / 1024:.0f}K" if n >= 1024 else f"{n}B"


def render_tree(entries: list[tuple[str, int]], globs=NOISE_GLOBS, max_chars: int = CONTEXT_MAX_TREE_CHARS) -> str:
    """Sorted, noise-filtered `path (size)` listing — sizes tell the model which
    files carry weight and which are stubs. Capped like `llm_client.truncate_diff`
    — whole lines kept up to the char budget, the rest counted, not dropped silently."""
    kept = sorted((p, s) for p, s in entries if not any(fnmatch(p, g) for g in globs))
    lines, size, omitted = [], 0, 0
    for p, s in kept:
        line = f"{p} ({_fmt_size(s)})\n"
        if size + len(line) > max_chars:
            omitted += 1
            continue
        lines.append(line)
        size += len(line)
    out = "".join(lines)
    if omitted:
        out += f"\n…[{omitted} more file(s) omitted to fit the size cap]…"
    return out


# Source extensions whose heads are worth reading (docstring/imports up top).
_CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java",
              ".kt", ".swift", ".c", ".h", ".cpp", ".cs", ".php"}


def pick_head_files(entries: list[tuple[str, int]], globs=NOISE_GLOBS, n: int = CONTEXT_HEAD_FILES) -> list[str]:
    """The n largest noise-filtered source files. dev-note: size is a crude
    'most architecture per byte' proxy — swap for a path-depth/name heuristic if
    briefs keep fixating on one giant file."""
    code = [
        (p, s) for p, s in entries
        if PurePosixPath(p).suffix in _CODE_EXTS
        and not any(fnmatch(p, g) for g in globs)
    ]
    return [p for p, _ in sorted(code, key=lambda e: (-e[1], e[0]))[:n]]


def build_context_prompt(tree_text: str, key_files: dict[str, str]) -> str:
    """Compose the tree + key-file contents into the context-generation prompt."""
    parts = ["Repository file tree:\n" + tree_text]
    for name, text in key_files.items():
        parts.append(f"{name}:\n{text}")
    return "\n\n".join(parts)


def is_stale(issue) -> bool:
    """True if there's no issue yet, or it hasn't been refreshed in CONTEXT_REFRESH_DAYS."""
    if issue is None:
        return True
    age = datetime.now(timezone.utc) - issue.updated_at
    return age.days >= CONTEXT_REFRESH_DAYS


def _strip_marker(body: str) -> str:
    tag = f"<!-- {_MARKER} -->\n"
    return body[len(tag):] if body.startswith(tag) else body


def run(repo) -> "str | None":
    """Generate + cache the project-context doc. Returns the body, or None if
    rate-limited or the model didn't return usable content. Host-agnostic core.
    Gated on a repo-scoped pseudo-PR key ("context"), not the triggering PR's own
    bucket — this is a once-a-month repo-level refresh, not part of any single
    PR's review budget, so it shouldn't silently halve that PR's daily cap."""
    ok, _ = limits.allow_llm_call(repo.full_name, "context")
    if not ok:
        return None
    entries = gh.get_tree(repo)
    key_files = {
        name: text for name in CONTEXT_KEY_FILES if (text := gh.get_file_text(repo, name))
    }
    # Heads of the biggest source files: docstrings + imports are the cheapest
    # "what calls what" signal — a path listing alone can't show relationships.
    heads = {
        f"{p} (first {CONTEXT_HEAD_LINES} lines)": "\n".join(text.splitlines()[:CONTEXT_HEAD_LINES])
        for p in pick_head_files(entries)
        if (text := gh.get_file_text(repo, p))
    }

    def prompt_for(model: str) -> str:
        # Large-context models (NVIDIA) see the whole tree plus source-file heads;
        # the small fallbacks keep the tight tree+manifests prompt they can fit.
        cap = MODEL_INPUT_CHARS.get(model, CONTEXT_MAX_TREE_CHARS)
        files = {**key_files, **heads} if model in MODEL_INPUT_CHARS else key_files
        return build_context_prompt(render_tree(entries, max_chars=cap), files)

    body = complete(_SYSTEM, prompt_for, "context")
    if body.strip().startswith("⚠️"):
        return None  # quota/empty/too-large — don't cache a warning as "the context"
    gh.upsert_issue(repo, _MARKER, _TITLE, body)
    return body


def ensure_fresh(repo) -> str:
    """Context text for /review to include: reuse if fresh, else refresh (falling
    back to a stale-but-present doc, then "" if generation fails). Never raises."""
    issue = gh.get_context_issue(repo, _MARKER)
    if issue is not None and not is_stale(issue):
        return _strip_marker(issue.body or "")
    fresh = run(repo)
    if fresh is not None:
        return fresh
    return _strip_marker(issue.body or "") if issue is not None else ""
