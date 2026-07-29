"""The Sidekick check run: the review verdict as a GitHub check above the merge
button, so branch protection can enforce it. See
docs/superpowers/specs/2026-07-17-auto-review-design.md, "PR 2 — the check".

`conclusion_of` is pure (three inputs -> the five gate rows). `conclude` and
`refresh` are the two I/O entry points with different authority (Task 3).

Env: none directly — callers pass an App installation token for the GraphQL read
and a PyGithub repo handle for the check write (see gh / server.app).
"""

from scripts import gh
from scripts.config import REQUIRED_SECTIONS
from scripts.merge_pr import PR_QUERY, unresolved_count
from scripts.validate_pr import missing_sections

# The check's name is its branch-protection key and its idempotency key on a head.
CHECK_NAME = "Sidekick"

_PENDING_SUMMARY = "Sidekick is reviewing the latest changes to this pull request."
_FAILED_SUMMARY = (
    "The review didn't complete (a provider outage or rate limit). This check is "
    "neutral — it doesn't block merge, but the code wasn't reviewed. Re-push or "
    "comment `/review` to retry."
)
_CLEAN_SUMMARY = "All review threads are resolved and the description is complete."


def conclusion_of(review_state, n_unresolved, missing):
    """Map (review_state, unresolved-thread count, missing sections) to a check
    (status, conclusion, title, summary). Pure — no I/O. First gate that fires wins
    the title; the summary always lists every reason so gate 3's title can coexist
    with a gate-4 reason in the panel. `conclusion` is None only for "pending"."""
    if review_state == "pending":
        return ("in_progress", None, "🐱 Reviewing…", _PENDING_SUMMARY)
    if review_state == "failed":
        return (
            "completed", "neutral",
            "🐱 Sidekick couldn't review — merge at your own risk",
            _FAILED_SUMMARY,
        )

    # review_state == "done": evaluate gates 3, 4, 5 in order.
    lines = []
    if n_unresolved:
        lines.append(f"- {n_unresolved} unresolved review thread(s) to address.")
    if missing:
        lines.append("- Description missing: " + ", ".join(missing) + ".")

    if n_unresolved:
        title = f"🐱 {n_unresolved} unresolved review thread(s)"
        return ("completed", "failure", title, "\n".join(lines))
    if missing:
        title = "🐱 Description missing: " + ", ".join(missing)
        return ("completed", "failure", title, "\n".join(lines))
    return ("completed", "success", "🐱 No unresolved feedback", _CLEAN_SUMMARY)


def conclude(repo, pr_number, token, review_state):
    """Authoritative: always writes. dispatch calls it around the review — the only
    caller that knows the outcome — so it owns gates 1 ("pending") and 2 ("failed").

    dev-note: if a Cloud Run instance dies between the "pending" write and this one,
    the check is stranded in_progress (D1). The deferential refresh below won't clear
    it; the exits are a new push (new head -> new check) or /review (calls conclude).
    Upgrade path: a staleness timeout in refresh (N > the ~15min NIM 504 worst case)."""
    head_sha = repo.get_pull(pr_number).head.sha
    _write(repo, pr_number, head_sha, token, review_state)


def refresh(repo, pr_number, token):
    """Deferential: only recomputes a check already concluded success/failure. Bails on
    an absent check (never creates — a draft has none until it's marked ready), an
    in_progress check (a review owns it and will conclude it), or a neutral one (the
    review failed; thread state can't turn that green). This bail is the race fix — it
    stops a mid-review thread-resolve from flipping the check green before the review
    posts its threads."""
    head_sha = repo.get_pull(pr_number).head.sha
    current = gh.get_check_run(repo, head_sha, CHECK_NAME)
    if current is None:
        return
    if current.status != "completed" or current.conclusion == "neutral":
        return
    _write(repo, pr_number, head_sha, token, "done")


def _write(repo, pr_number, head_sha, token, review_state):
    """Compute the check for `review_state` and upsert it. Gates 1/2 need no live read;
    "done" reads threads + description from one PR_QUERY (never mergeStateStatus /
    reviewDecision — a required check reading those is a circular deadlock)."""
    if review_state in ("pending", "failed"):
        n, missing = 0, []
    else:
        owner, name = repo.full_name.split("/", 1)
        data = gh.graphql(token, PR_QUERY, {"owner": owner, "name": name, "number": pr_number})
        pr = (data.get("data") or {}).get("repository", {}).get("pullRequest", {}) or {}
        n = unresolved_count(data)
        missing = missing_sections(pr.get("body"), REQUIRED_SECTIONS)
    status, conclusion, title, summary = conclusion_of(review_state, n, missing)
    gh.upsert_check_run(repo, head_sha, CHECK_NAME, status, conclusion, title, summary)
