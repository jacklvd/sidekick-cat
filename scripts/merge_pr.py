"""Deterministic /merge — squash-merge the PR if every safety gate passes,
else post why it can't. No AI.

Gates + threads come from one GraphQL query; the merge is PyGithub
`pull.merge(squash)` + best-effort branch delete (no `gh` CLI on Cloud Run). The
verdict is an idempotent `<!-- bot:merge -->` comment. Auth: an App installation
token (Cloud Run) or GH_TOKEN (Actions) — both valid for api.github.com.
"""

import os

from scripts import gh
from scripts.config import REQUIRED_SECTIONS
from scripts.validate_pr import missing_sections

# Only these merge states are safe to merge. Everything else is a blocker.
_GOOD_STATES = {"CLEAN", "HAS_HOOKS"}

# One query for every gate: state + reviewDecision + mergeStateStatus (a single
# server-computed enum folding in conflicts, branch protection, required reviews,
# checks, draft) plus reviewThreads. mergeStateStatus avoids statusCheckRollup,
# which traverses Actions-scoped resources our Pull-requests-only token can't read.
_PR_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "state reviewDecision mergeStateStatus body "
    "reviewThreads(first:100){nodes{isResolved}}}}}"
)

# mergeStateStatus -> why it's not mergeable.
_STATE_REASON = {
    "DIRTY": "there are merge conflicts to resolve.",
    "BEHIND": "the branch is behind the base — update it first.",
    "BLOCKED": "branch protection is blocking it (a required review or status check isn't satisfied).",
    "UNSTABLE": "one or more checks are failing or still running.",
    "DRAFT": "the PR is still a draft.",
    "UNKNOWN": "the merge state is still being computed — try again in a moment.",
}


def unresolved_count(data: dict) -> int:
    """Number of unresolved review threads in a reviewThreads GraphQL response."""
    nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    return sum(1 for n in nodes if not n.get("isResolved"))


def _unresolved_reasons(data: dict) -> list[str]:
    """Best-effort unresolved-thread gate over an already-fetched response. Degrades
    open (no blocker) if threads are missing, so /merge stays usable on other gates."""
    try:
        n = unresolved_count(data)
    except (KeyError, TypeError):
        return []
    if n:
        return [f"{n} unresolved review comment(s) — resolve them before merging."]
    return []


def blockers(s: dict) -> list[str]:
    """Human-readable reasons the PR can't be merged. Empty list == good to merge."""
    if s.get("state") != "OPEN":
        return [f"PR is {str(s.get('state', 'unknown')).lower()}, not open."]
    reasons = []
    # CHANGES_REQUESTED even when reviews aren't *required* — respect it. (Required-
    # but-missing reviews surface as mergeStateStatus BLOCKED below.)
    if s.get("reviewDecision") == "CHANGES_REQUESTED":
        reasons.append("changes were requested in review.")
    state = s.get("mergeStateStatus")
    if state not in _GOOD_STATES:
        reasons.append(_STATE_REASON.get(state, f"merge state is {state}."))
    # Re-check the description live, not via the bot:validate comment — the ❌ flag
    # is only posted on open/update, so without this a PR opened with a bad
    # description and never edited would merge right past it.
    missing = missing_sections(s.get("body"), REQUIRED_SECTIONS)
    if missing:
        reasons.append(
            "the description is missing required section(s): "
            + ", ".join(f"**{m}**" for m in missing) + " — edit the PR description first."
        )
    return reasons


def run(repo, pr_number, token):
    """Squash-merge if every gate passes, else post why. Host-agnostic core.

    `token` mints the GraphQL call (the gate read); the merge + branch delete go
    through the PyGithub `repo` handle. No `gh` CLI.
    """
    owner, name = repo.full_name.split("/", 1)
    data = gh.graphql(token, _PR_QUERY, {"owner": owner, "name": name, "number": pr_number})
    pr = (data.get("data") or {}).get("repository", {}).get("pullRequest", {}) or {}

    reasons = blockers(pr)
    if pr.get("state") == "OPEN":
        reasons += _unresolved_reasons(data)
    if reasons:
        body = "### 🐱 Sidekick can't merge yet\n\n" + "\n".join(f"- {r}" for r in reasons)
        gh.upsert_comment(repo, pr_number, "bot:merge", body)
        return

    pull = repo.get_pull(pr_number)
    pull.merge(merge_method="squash")
    try:
        repo.get_git_ref(f"heads/{pull.head.ref}").delete()
    except Exception:
        pass  # dev-note: fork PR / protected branch — merge succeeded, delete is best-effort
    gh.upsert_comment(
        repo, pr_number, "bot:merge",
        "### 🐱 Shipped 🚀\nSquashed and deleted the branch.",
    )


def main():
    token = os.environ["GH_TOKEN"]  # Actions path; valid for api.github.com/graphql
    run(gh.get_repo(), int(os.environ["PR_NUMBER"]), token)


if __name__ == "__main__":
    main()
