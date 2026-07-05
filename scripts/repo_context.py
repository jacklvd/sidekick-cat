"""Per-repo project context for /review — a summary, folder structure, and
per-file highlights, cached as a hidden-marker GitHub Issue so /review can judge
a diff against more than the diff itself.

Generated from the repo's file tree + a handful of key manifest/doc files, one
LLM call (task "context", same light tier as "summary"). Refreshed manually via
/context or lazily by /review when missing or older than CONTEXT_REFRESH_DAYS —
staleness reads the issue's own `updated_at`, no separate bookkeeping needed.
"""

from datetime import datetime, timezone
from fnmatch import fnmatch

from scripts import gh, limits
from scripts.config import (
    CONTEXT_KEY_FILES,
    CONTEXT_MAX_TREE_CHARS,
    CONTEXT_REFRESH_DAYS,
    NOISE_GLOBS,
)
from scripts.llm_client import complete

_MARKER = "bot:context"
_TITLE = "🐱 Sidekick project context (auto-generated, do not edit)"

_SYSTEM = (
    "You are a senior engineer writing an onboarding brief for another reviewer "
    "who has never seen this repository. Given its file tree and a few key "
    "files, write a concise Markdown brief: a 2-4 sentence project summary, the "
    "top-level folder structure with a one-line purpose per entry, and any "
    "notable relationships between parts (what calls what, what owns what "
    "state). No preamble, no restating the file list verbatim — synthesize."
)


def render_tree(paths: list[str], globs=NOISE_GLOBS, max_chars: int = CONTEXT_MAX_TREE_CHARS) -> str:
    """Sorted, noise-filtered file listing, capped like `llm_client.truncate_diff`
    — whole lines kept up to the char budget, the rest counted, not dropped silently."""
    kept = sorted(p for p in paths if not any(fnmatch(p, g) for g in globs))
    lines, size, omitted = [], 0, 0
    for p in kept:
        line = p + "\n"
        if size + len(line) > max_chars:
            omitted += 1
            continue
        lines.append(line)
        size += len(line)
    out = "".join(lines)
    if omitted:
        out += f"\n…[{omitted} more file(s) omitted to fit the size cap]…"
    return out


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
    paths = gh.get_tree(repo)
    key_files = {
        name: text for name in CONTEXT_KEY_FILES if (text := gh.get_file_text(repo, name))
    }
    prompt = build_context_prompt(render_tree(paths), key_files)
    body = complete(_SYSTEM, prompt, "context")
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
