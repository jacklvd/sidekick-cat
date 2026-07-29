"""Map a webhook (event type + payload) to the flow to run — replaces the
workflow `if:` guards. Pure function, so it self-checks offline.

`classify` returns an intent dict with a `kind`:
  - "ignore"       : drop (bot loop guard, irrelevant event, unauthorized commenter)
  - "pr_open"      : run the PR-open flow
  - "pr_update"    : run the PR-update flow
  - "command"      : run a slash command ("review" | "merge" | "context")
  - "thread_reply" : answer an author's reply in a bot review thread

`pr_open` / `pr_update` also carry `review: bool` — whether this event earns an AI
review. The whole decision lives here so it stays pure and testable: a review costs a
budget unit, so only a code change (`synchronize` / `ready_for_review`) on a non-draft
qualifies. `draft` is in the webhook payload, so no API call is needed to know.
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
            # A draft is WIP by definition — the phase with the most pushes and the
            # least finished code — so it gets the deterministic checks but no review.
            # `/review` still works inside one for an early look.
            "review": not pr.get("draft"),
        }

    # Re-run the cheap deterministic checks when the PR changes after open: `edited`
    # (description fixed → revalidate the required sections), `synchronize` (new commits
    # → relabel from the new file mix), and `ready_for_review` (the draft is done). No
    # welcome — that fires once on open.
    if event == "pull_request" and action in {"edited", "synchronize", "ready_for_review"}:
        pr = body.get("pull_request", {})
        return {
            "kind": "pr_update", "owner": owner, "repo": repo,
            "number": pr.get("number"), "installation_id": installation_id,
            # Only a CODE change on a non-draft earns a review. `edited` is a
            # description/title change — reviewing it would re-read an unchanged diff
            # and spend a budget unit to say the same thing twice.
            "review": action in {"synchronize", "ready_for_review"} and not pr.get("draft"),
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

    # An author replying inside a review thread → let Sidekick answer there. Only
    # replies (in_reply_to_id set): a brand-new inline thread the human starts isn't
    # ours to answer. The bot's own replies are already dropped by the loop guard
    # above. Whether the thread is actually bot-owned needs an API call, so that
    # check lives in the flow (reply_thread.run), keeping this classifier pure.
    if event == "pull_request_review_comment" and action == "created":
        comment = body.get("comment", {})
        if comment.get("author_association") not in AUTHORIZED:
            return {"kind": "ignore", "reason": "unauthorized commenter"}
        if comment.get("in_reply_to_id") is None:
            return {"kind": "ignore", "reason": "new thread, not a reply"}
        pr = body.get("pull_request", {})
        return {
            "kind": "thread_reply", "owner": owner, "repo": repo,
            "number": pr.get("number"), "comment_id": comment.get("id"),
            "in_reply_to_id": comment.get("in_reply_to_id"),
            "installation_id": installation_id,
        }

    return {"kind": "ignore", "reason": f"unhandled {event}/{action}"}
