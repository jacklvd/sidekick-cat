"""Map a webhook (event type + payload) to the flow to run — replaces the
workflow `if:` guards. Pure function, so it self-checks offline.

`classify` returns an intent dict with a `kind`:
  - "ignore"  : drop (bot loop guard, irrelevant event, unauthorized commenter)
  - "pr_open" : run the PR-open flow
  - "command" : run a slash command ("review" | "merge" | "context")
This just routes + acks; the real flows run downstream in the background task.
"""

from scripts.config import TRIGGER_CONTEXT, TRIGGER_MERGE, TRIGGER_REVIEW

# Mirror the old review.yml guard: only these may trigger slash commands.
AUTHORIZED = {"OWNER", "MEMBER", "COLLABORATOR"}


def _is_bot(sender: dict) -> bool:
    """Loop guard: any bot/App sender. Broad on purpose — bots never trigger us."""
    login = (sender or {}).get("login", "")
    return (sender or {}).get("type") == "Bot" or login.endswith("[bot]")


def _repo_owner(body: dict) -> tuple[str, str]:
    full = body.get("repository", {}).get("full_name", "")
    owner, _, repo = full.partition("/")
    return owner, repo


def classify(event: str, body: dict) -> dict:
    """Decide what to do with one delivery. Never raises on a malformed payload."""
    body = body or {}
    if _is_bot(body.get("sender", {})):
        return {"kind": "ignore", "reason": "bot sender"}

    installation_id = body.get("installation", {}).get("id")
    owner, repo = _repo_owner(body)
    action = body.get("action")

    if event == "pull_request" and action in {"opened", "reopened"}:
        pr = body.get("pull_request", {})
        return {
            "kind": "pr_open", "owner": owner, "repo": repo,
            "number": pr.get("number"), "head_sha": pr.get("head", {}).get("sha"),
            "author": pr.get("user", {}).get("login"),
            "installation_id": installation_id,
        }

    # Re-run the cheap deterministic checks when the PR changes after open: `edited`
    # (description fixed → revalidate the required sections) and `synchronize` (new
    # commits → relabel from the new file mix). No welcome, no AI summary — those
    # fire once on open. This is what makes the description flag clear when fixed.
    if event == "pull_request" and action in {"edited", "synchronize"}:
        pr = body.get("pull_request", {})
        return {
            "kind": "pr_update", "owner": owner, "repo": repo,
            "number": pr.get("number"), "installation_id": installation_id,
        }

    if event == "issue_comment" and action == "created":
        issue = body.get("issue", {})
        comment = body.get("comment", {})
        if not issue.get("pull_request"):
            return {"kind": "ignore", "reason": "comment not on a PR"}
        if comment.get("author_association") not in AUTHORIZED:
            return {"kind": "ignore", "reason": "unauthorized commenter"}
        text = comment.get("body", "")
        command = (
            "review" if TRIGGER_REVIEW in text
            else "merge" if TRIGGER_MERGE in text
            else "context" if TRIGGER_CONTEXT in text
            else None
        )
        if not command:
            return {"kind": "ignore", "reason": "no command"}
        return {
            "kind": "command", "command": command, "owner": owner, "repo": repo,
            "number": issue.get("number"), "comment_id": comment.get("id"),
            "installation_id": installation_id,
        }

    return {"kind": "ignore", "reason": f"unhandled {event}/{action}"}
