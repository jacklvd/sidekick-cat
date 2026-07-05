"""GitHub operations as the bot (PyGithub, authenticated with the App token).

Env: GH_TOKEN (App installation token), GITHUB_REPOSITORY (owner/repo, auto-set
in Actions), PR_NUMBER.
"""

import json
import logging
import os
import urllib.request

from github import Auth, Github

_API = "https://api.github.com"
log = logging.getLogger("sidekick-cat.gh")


def graphql(token, query, variables):
    """POST a GraphQL query with an installation token (no `gh` CLI on Cloud Run).
    Returns the parsed response; the caller reads `data`/`errors`."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        f"{_API}/graphql", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "sidekick-cat",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_pr_diff(full_name, number, token):
    """Raw unified diff for a PR via REST — the Cloud Run analog of `gh pr diff`
    (no `gh` CLI in the container).

    dev-note: GitHub returns the diff inline (200) for normal PRs; very large
    diffs may 302 to a signed URL. urllib follows the redirect, and we truncate
    downstream anyway, so the size ceiling lives in MAX_DIFF_CHARS, not here.
    """
    req = urllib.request.Request(
        f"{_API}/repos/{full_name}/pulls/{number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sidekick-cat",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def unified_from_files(files) -> str:
    """Rebuild a unified diff from compare-API file entries so the downstream
    diff tooling (anchors, strip_noise, number_diff) works unchanged. `files` is
    [(filename, previous_filename, status, patch)]; a None patch (binary,
    too large) is skipped — nothing reviewable in it."""
    parts = []
    for name, prev, status, patch in files:
        if not patch:
            continue
        old = prev or name
        minus = "/dev/null" if status == "added" else f"a/{old}"
        plus = "/dev/null" if status == "removed" else f"b/{name}"
        parts.append(f"diff --git a/{old} b/{name}\n--- {minus}\n+++ {plus}\n{patch}\n")
    return "".join(parts)


def compare_diff(repo, base_sha, head_sha) -> str:
    """Diff of base..head via the compare API — the incremental re-review source.
    Patch line numbers are against the head-side files, so inline anchors stay
    valid. Raises if base is unreachable (force-push); caller falls back to full."""
    cmp = repo.compare(base_sha, head_sha)
    return unified_from_files(
        [(f.filename, f.previous_filename, f.status, f.patch) for f in cmp.files]
    )


def get_repo():
    """Repository handle from GH_TOKEN + GITHUB_REPOSITORY."""
    return Github(os.environ["GH_TOKEN"]).get_repo(os.environ["GITHUB_REPOSITORY"])


def get_file_text(repo, path):
    """Default-branch contents of `path` as text, or None if absent. Best-effort —
    used to feed the target repo's CLAUDE.md to the reviewer (no local checkout on
    Cloud Run)."""
    try:
        return repo.get_contents(path).decoded_content.decode("utf-8", errors="replace")
    except Exception:
        return None  # dev-note: missing file / dir / binary → just review without it


def repo_from_token(token, full_name):
    """Repo handle from an explicit installation token (Cloud Run path; no env)."""
    return Github(auth=Auth.Token(token)).get_repo(full_name)


def upsert_comment(repo, pr_number, marker, body):
    """Edit the bot's existing `<!-- marker -->` comment, else create one. Idempotent on re-runs."""
    issue = repo.get_issue(pr_number)
    tag = f"<!-- {marker} -->"
    full = f"{tag}\n{body}"
    for c in issue.get_comments():
        if tag in (c.body or ""):
            c.edit(full)
            return c
    return issue.create_comment(full)


def react(repo, issue_number, comment_id, content="eyes"):
    """React to the triggering issue comment (👀 by default) as an immediate ack.
    Goes through Issue.get_comment — PyGithub's Repository has no comment-by-id
    getter. Best-effort: a missing comment or perms hiccup must not block the
    command, but log it — a silent pass hid a hard AttributeError here for days."""
    try:
        repo.get_issue(issue_number).get_comment(comment_id).create_reaction(content)
    except Exception:
        log.warning("react on comment %s failed", comment_id, exc_info=True)


def assign(repo, pr_number, login):
    """Assign a user to the PR. Best-effort: a no-op if they can't be assigned."""
    try:
        repo.get_issue(pr_number).add_to_assignees(login)
    except Exception:
        pass  # dev-note: author may lack assignable access (e.g. fork PR); skip silently


def submit_review(repo, pr_number, body, event):
    """Submit a PR review. event in {COMMENT, APPROVE, REQUEST_CHANGES}."""
    repo.get_pull(pr_number).create_review(body=body, event=event)


def get_inline_comments(repo, pr_number, marker):
    """The bot's existing inline review comments (those carrying the marker)."""
    tag = f"<!-- {marker} -->"
    return [c for c in repo.get_pull(pr_number).get_review_comments() if tag in (c.body or "")]


def create_review_comment(repo, pr_number, head_sha, path, line, body):
    """Post a single inline comment on the RIGHT side, starting an unresolved thread."""
    pr = repo.get_pull(pr_number)
    pr.create_review_comment(
        body=body, commit=repo.get_commit(head_sha), path=path, line=line, side="RIGHT"
    )


def set_managed_labels(repo, pr_number, desired, managed):
    """Reconcile the bot-managed labels to exactly `desired`. `managed` is the full
    universe the bot owns (LABEL_RULES values); labels outside it (human-added) are
    never touched. Removing stale managed labels keeps a relabeled PR consistent
    when its file mix changes — additive-only labeling drifts."""
    issue = repo.get_issue(pr_number)
    current = {lbl.name for lbl in issue.get_labels()}
    add = [n for n in desired if n not in current]
    remove = [n for n in managed if n in current and n not in desired]
    for name in add:  # create any label that doesn't exist yet, so setup is zero-config
        try:
            repo.get_label(name)
        except Exception:
            repo.create_label(name=name, color="ededed")
    if add:
        issue.add_to_labels(*add)
    for name in remove:
        issue.remove_from_labels(name)


def get_tree(repo) -> list[str]:
    """All blob (file) paths in the repo's default branch, recursive. Best-effort
    — an empty/unreadable repo yields an empty list rather than raising."""
    try:
        tree = repo.get_git_tree(repo.default_branch, recursive=True)
        return [e.path for e in tree.tree if e.type == "blob"]
    except Exception:
        return []


def get_context_issue(repo, marker):
    """The bot's existing hidden-marker issue (open or closed), or None. Requires
    the issue to be bot-authored — marker text alone isn't authorization, since
    an unrelated user could plant it in an issue they created (same idea as
    server.router._is_bot's loop guard: a human account can't be flagged type
    Bot / end in "[bot]").
    dev-note: linear scan over all issues — fine at personal-repo scale (same
    tradeoff as upsert_comment's comment scan); switch to the Search API if a
    target repo's issue count ever makes this slow."""
    tag = f"<!-- {marker} -->"
    for issue in repo.get_issues(state="all"):
        author = issue.user
        is_bot = author is not None and (
            getattr(author, "type", None) == "Bot" or (author.login or "").endswith("[bot]")
        )
        if is_bot and tag in (issue.body or ""):
            return issue
    return None


def upsert_issue(repo, marker, title, body):
    """Edit the bot's existing marker issue, else create one — then close it (it's
    metadata, not actionable, so it shouldn't sit in the open Issues list)."""
    tag = f"<!-- {marker} -->"
    full = f"{tag}\n{body}"
    issue = get_context_issue(repo, marker)
    if issue is not None:
        issue.edit(body=full, state="closed")
        return issue
    issue = repo.create_issue(title=title, body=full)
    issue.edit(state="closed")
    return issue
