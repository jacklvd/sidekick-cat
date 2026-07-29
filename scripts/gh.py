"""GitHub operations as the bot (PyGithub, authenticated with the App token).

Env: GH_TOKEN (App installation token), GITHUB_REPOSITORY (owner/repo, auto-set
in Actions), PR_NUMBER.
"""

import json
import logging
import os
import urllib.request

from github import Auth, Github

from scripts.config import DEFAULT_LABEL_COLOR, LABEL_COLORS

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


_THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100){nodes{id isResolved "
    "comments(first:1){nodes{databaseId}}}}}}}"
)
_RESOLVE_MUT = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
_UNRESOLVE_MUT = "mutation($id:ID!){unresolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


def review_thread_state(repo, pr_number, token):
    """{root comment databaseId -> (thread node id, is_resolved)} for every review
    thread on the PR. databaseId joins to a REST comment's `.id`, so reconcile can find
    a root's GraphQL thread — resolution is GraphQL-only, REST comments don't carry it.

    Best-effort: returns {} if the read fails, so reconcile degrades to edit/create
    without resolving (never deletes)."""
    owner, name = repo.full_name.split("/", 1)
    try:
        data = graphql(token, _THREADS_QUERY, {"owner": owner, "name": name, "number": pr_number})
        nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except Exception:
        log.warning("review_thread_state read failed for PR #%s", pr_number, exc_info=True)
        return {}
    state = {}
    for n in nodes:
        roots = (n.get("comments") or {}).get("nodes") or []
        if roots and roots[0].get("databaseId") is not None:
            state[roots[0]["databaseId"]] = (n["id"], bool(n["isResolved"]))
    return state


def resolve_thread(token, thread_id):
    """Mark a review thread resolved (GraphQL). Best-effort — logs and returns on
    failure so one thread not resolving doesn't abort the reconcile.

    dev-note: needs the App's `pull_requests: write` (it authors these threads). If it
    ever 403s, the reconcile still adds the "no longer flags this" note; the thread just
    stays open — no worse than the pre-resolve behavior, and no data lost."""
    try:
        graphql(token, _RESOLVE_MUT, {"id": thread_id})
    except Exception:
        log.warning("resolve_thread failed for %s", thread_id, exc_info=True)


def unresolve_thread(token, thread_id):
    """Mark a review thread unresolved (GraphQL) — a fixed issue that recurred, so the
    merge gate must count it again. Best-effort, same rationale as resolve_thread."""
    try:
        graphql(token, _UNRESOLVE_MUT, {"id": thread_id})
    except Exception:
        log.warning("unresolve_thread failed for %s", thread_id, exc_info=True)


def get_check_run(repo, head_sha, name):
    """The check run named `name` on `head_sha`, or None. `get_check_runs` filters
    server-side by name, so the first result is the one we own (only this App creates
    a check by this name)."""
    for run in repo.get_commit(head_sha).get_check_runs(check_name=name):
        return run
    return None


def upsert_check_run(repo, head_sha, name, status, conclusion, title, summary):
    """Write the check idempotently: edit the run of this name on the head if it
    exists, else create it. `create_check_run` is NOT idempotent — the same name on
    the same head yields a duplicate row — so two racing dispatches would otherwise
    show two Sidekick checks. Name is the natural key, same principle as upsert_comment.

    `conclusion` is None for an in_progress check; omit it rather than send null."""
    kwargs = {"status": status, "output": {"title": title, "summary": summary}}
    if conclusion is not None:
        kwargs["conclusion"] = conclusion
    existing = get_check_run(repo, head_sha, name)
    if existing is not None:
        existing.edit(**kwargs)
    else:
        repo.create_check_run(name=name, head_sha=head_sha, **kwargs)


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


def get_file_text(repo, path, ref=None):
    """Contents of `path` as text, or None if absent. Best-effort — used to feed the
    target repo's CLAUDE.md to the reviewer (no local checkout on Cloud Run).

    `ref` defaults to the default branch. Pass the PR head sha when reading a file the
    PR itself touches: a helper the PR adds doesn't exist on the default branch yet, so
    reading without a ref would report it missing — which is the exact mistake we're
    giving the reviewer this data to stop making."""
    try:
        kwargs = {"ref": ref} if ref else {}
        contents = repo.get_contents(path, **kwargs)
        return contents.decoded_content.decode("utf-8", errors="replace")
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


def get_review_comments(repo, pr_number):
    """Every review (inline) comment on the PR — thread roots and replies alike.
    One API round-trip; callers that need only roots filter on in_reply_to_id."""
    return list(repo.get_pull(pr_number).get_review_comments())


def get_inline_comments(repo, pr_number, marker):
    """The bot's inline thread ROOTS carrying `marker`. Roots only (in_reply_to_id
    is None): a reply — the bot's own answer, or a human's — must never be keyed as
    an existing comment, or reconcile_inline would collide it with its own thread."""
    tag = f"<!-- {marker} -->"
    return [
        c for c in get_review_comments(repo, pr_number)
        if tag in (c.body or "") and c.in_reply_to_id is None
    ]


def get_review_comment_thread(repo, pr_number, comment_id):
    """(root, [root, ...replies]) for the thread CONTAINING `comment_id`, in id order
    (≈ chronological). (None, []) if the comment is gone. Feeds reply_thread with the
    conversation so far.

    `comment_id` may be any comment in the thread, not just the root: we walk
    `in_reply_to_id` up to the root (the comment with none). A webhook delivers the id
    of the comment a reply answers, and while GitHub review threads are flat today (so
    that id is already the root), resolving explicitly means the flow can't silently
    no-op if that ever stops holding — a webhook path is miserable to debug live."""
    comments = get_review_comments(repo, pr_number)
    by_id = {c.id: c for c in comments}
    node = by_id.get(comment_id)
    while node is not None and node.in_reply_to_id is not None:
        node = by_id.get(node.in_reply_to_id)
    if node is None:
        return None, []
    thread = [node] + [c for c in comments if c.in_reply_to_id == node.id]
    thread.sort(key=lambda c: c.id)
    return node, thread


def create_review_comment_reply(repo, pr_number, root_id, body):
    """Post `body` as a reply in the thread rooted at `root_id`."""
    repo.get_pull(pr_number).create_review_comment_reply(root_id, body)


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
    for name in desired:  # create any label that doesn't exist yet, so setup is zero-config
        color = LABEL_COLORS.get(name, DEFAULT_LABEL_COLOR)
        try:
            label = repo.get_label(name)
        except Exception:
            repo.create_label(name=name, color=color)
            continue
        # Repaint only labels still wearing the old default grey: that means the bot made
        # them and nobody has recolored them since, so every repo self-heals on its next
        # PR. A color a human picked is left alone.
        if label.color == DEFAULT_LABEL_COLOR != color:
            label.edit(name=name, color=color)
    if add:
        issue.add_to_labels(*add)
    for name in remove:
        issue.remove_from_labels(name)


def get_tree(repo) -> list[tuple[str, int]]:
    """All blob (file) (path, size-in-bytes) pairs in the repo's default branch,
    recursive. Sizes ride along free in the same API response — repo_context uses
    them to mark heavyweight files and pick which heads to fetch. Best-effort —
    an empty/unreadable repo yields an empty list rather than raising."""
    try:
        tree = repo.get_git_tree(repo.default_branch, recursive=True)
        return [(e.path, e.size or 0) for e in tree.tree if e.type == "blob"]
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
