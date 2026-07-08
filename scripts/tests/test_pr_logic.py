"""Offline self-checks for the pure logic across milestones (no network, no token).

Run: python -m scripts.tests.test_pr_logic
"""

from scripts.diff_anchors import anchors, number_diff, strip_noise
from scripts.label_pr import labels_for
from scripts.merge_pr import blockers, unresolved_count
from scripts.review_pr import parse_response, partition
from scripts.summarize_pr import summarize
from scripts.validate_pr import missing_sections


def test_missing_sections():
    req = ["Summary", "How to Validate"]
    assert missing_sections("## Summary\n## How to Validate\ndetails", req) == []
    assert missing_sections("**Summary**\n- How to Validate: run it", req) == []  # bold/list markup
    # prose mention must NOT count as the section being present
    assert missing_sections("## Summary\n(omitting the How to Validate section)", req) == ["How to Validate"]
    assert missing_sections(None, req) == req  # empty body -> all missing


def test_labels_for():
    rules = {"*.py": "python", "*.md": "documentation", "docs/*": "documentation"}
    assert labels_for(["scripts/config.py"], rules) == ["python"]          # * crosses /
    assert labels_for(["docs/PLAN.md"], rules) == ["documentation"]        # deduped across rules
    assert labels_for(["a.py", "b.md"], rules) == ["documentation", "python"]
    assert labels_for(["LICENSE"], rules) == []


def test_summarize_empty():
    # empty/whitespace diff short-circuits (no network call to the model)
    assert "No diff" in summarize("   \n  ")


def test_summarize_gating():
    # run() must meter LLM calls — within budget posts a summary, over the
    # per-PR cap posts the ratelimit note and never touches the model.
    import scripts.summarize_pr as sp
    from scripts import limits
    from scripts.config import PR_DAILY_MAX

    limits._reset()
    posted, modeled = [], []
    orig_up, orig_sum = sp.gh.upsert_comment, sp.summarize
    sp.gh.upsert_comment = lambda repo, n, marker, body: posted.append(marker)
    sp.summarize = lambda diff: (modeled.append(1), "MODEL")[1]

    class FakeRepo:
        full_name = "o/r"

    try:
        for _ in range(PR_DAILY_MAX):  # spend the per-PR budget
            sp.run(FakeRepo(), 1, "a diff")
        assert posted == ["bot:summary"] * PR_DAILY_MAX
        assert len(modeled) == PR_DAILY_MAX
        sp.run(FakeRepo(), 1, "a diff")  # one over → capped
        assert posted[-1] == "bot:ratelimit"
        assert len(modeled) == PR_DAILY_MAX  # model NOT called when capped
    finally:
        sp.gh.upsert_comment, sp.summarize = orig_up, orig_sum


def test_review_gating():
    # run() must (a) skip a head SHA already reviewed (free re-/review) and
    # (b) post the ratelimit note instead of calling the model once the per-PR cap
    # is spent.
    import scripts.review_pr as rp
    from scripts import limits
    from scripts.config import PR_DAILY_MAX

    posted, modeled = [], []
    sha = {"v": "h0"}

    class FakeRepo:
        full_name = "o/r"

        def get_pull(self, n):
            return type("P", (), {"head": type("H", (), {"sha": sha["v"]}), "title": "t", "body": ""})()

    orig = (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text, rp.reconcile_inline,
            rp.repo_context.ensure_fresh)
    rp.complete = lambda s, u, t, **kw: (modeled.append(1), '{"verdict":"approve","summary":"ok","issues":[]}')[1]
    rp.gh.upsert_comment = lambda repo, n, marker, body: posted.append(marker)
    rp.gh.get_file_text = lambda repo, path: ""
    rp.reconcile_inline = lambda *a, **k: None
    rp.repo_context.ensure_fresh = lambda repo: ""
    try:
        limits._reset()
        rp.run(FakeRepo(), 1, "a diff")              # first review of h0
        rp.run(FakeRepo(), 1, "a diff")              # same head → seen → skipped, free
        assert posted == ["bot:review"] and len(modeled) == 1

        # distinct heads exhaust the per-PR cap, then the next is rate-limited.
        limits._reset()
        posted.clear(); modeled.clear()
        for i in range(PR_DAILY_MAX):
            sha["v"] = f"s{i}"
            rp.run(FakeRepo(), 1, "a diff")
        assert len(modeled) == PR_DAILY_MAX
        sha["v"] = "over"
        rp.run(FakeRepo(), 1, "a diff")              # cap spent → no model call
        assert posted[-1] == "bot:ratelimit" and len(modeled) == PR_DAILY_MAX
    finally:
        (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text, rp.reconcile_inline,
         rp.repo_context.ensure_fresh) = orig


def test_review_routes_by_size():
    # Small diff -> "review" tier, no disclaimer. Diff over MAX_DIFF_CHARS -> the
    # high-TPM "review_large" tier with a big-PR note that NAMES the model that
    # actually answered (dynamic — not a hardcoded fallback like Scout).
    import scripts.review_pr as rp
    from scripts import limits
    from scripts.config import MAX_DIFF_CHARS

    tasks, bodies = [], []

    class FakeRepo:
        full_name = "o/r"

        def get_pull(self, n):
            return type("P", (), {"head": type("H", (), {"sha": "h"}), "title": "t", "body": ""})()

    def fake_complete(s, u, t, **kw):
        tasks.append(t)
        if kw.get("used") is not None:  # simulate MiniMax answering (not the primary)
            kw["used"].append(("nvidia", "minimaxai/minimax-m2.7"))
        return '{"verdict":"comment","summary":"ok","issues":[]}'

    orig = (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text, rp.reconcile_inline,
            rp.repo_context.ensure_fresh)
    rp.complete = fake_complete
    rp.gh.upsert_comment = lambda repo, n, marker, body: bodies.append(body)
    rp.gh.get_file_text = lambda repo, path: ""
    rp.reconcile_inline = lambda *a, **k: None
    rp.repo_context.ensure_fresh = lambda repo: ""
    try:
        limits._reset()
        rp.run(FakeRepo(), 1, "x")                          # tiny -> small tier
        limits._reset()
        rp.run(FakeRepo(), 2, "y" * (MAX_DIFF_CHARS + 1))   # huge -> large tier
        assert tasks == ["review", "review_large"]
        # note only when large, and it names the responder (MiniMax), not a default
        assert "MiniMax-M2.7" not in bodies[0] and "MiniMax-M2.7" in bodies[1]
    finally:
        (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text, rp.reconcile_inline,
         rp.repo_context.ensure_fresh) = orig


def test_render_tree():
    from scripts.repo_context import render_tree

    entries = [("app.py", 120), ("scripts/gh.py", 4096), ("uv.lock", 90000), ("static/app.min.js", 5)]
    out = render_tree(entries, globs=["*.lock", "*.min.js"], max_chars=1000)
    assert "app.py (120B)" in out and "scripts/gh.py (4K)" in out  # sizes shown
    assert "uv.lock" not in out and "app.min.js" not in out  # noise filtered
    # tight cap -> some entries omitted, with a note
    tight = render_tree([("a.py", 1), ("b.py", 1), ("c.py", 1)], globs=[], max_chars=6)
    assert "omitted" in tight


def test_pick_head_files():
    from scripts.repo_context import pick_head_files

    entries = [
        ("big.py", 9000), ("small.py", 10), ("mid.py", 500),
        ("dist/bundle.js", 99999),      # noise-filtered despite its size
        ("README.md", 8000),            # not a source file
    ]
    # largest source files first, noise and non-code excluded
    assert pick_head_files(entries, n=2) == ["big.py", "mid.py"]
    assert pick_head_files(entries, n=10) == ["big.py", "mid.py", "small.py"]


def test_build_context_prompt():
    from scripts.repo_context import build_context_prompt

    p = build_context_prompt("a.py\nb.py\n", {"README.md": "# Hi"})
    assert "a.py" in p and "README.md" in p and "# Hi" in p
    assert build_context_prompt("a.py\n", {}) == "Repository file tree:\na.py\n"


def test_is_stale():
    from datetime import datetime, timedelta, timezone

    from scripts.repo_context import is_stale

    class FakeIssue:
        def __init__(self, age_days):
            self.updated_at = datetime.now(timezone.utc) - timedelta(days=age_days)

    assert is_stale(None) is True
    assert is_stale(FakeIssue(1)) is False
    assert is_stale(FakeIssue(31)) is True


def test_repo_context_run_and_ensure_fresh():
    import scripts.repo_context as rc
    from scripts import limits

    upserted = []

    class FakeRepo:
        full_name = "o/r"

    orig = (rc.gh.get_tree, rc.gh.get_file_text, rc.gh.upsert_issue,
            rc.gh.get_context_issue, rc.complete)
    rc.gh.get_tree = lambda repo: [("a.py", 10)]
    rc.gh.get_file_text = lambda repo, name: "readme" if name == "README.md" else None
    rc.gh.upsert_issue = lambda repo, marker, title, body: upserted.append(body)
    rc.gh.get_context_issue = lambda repo, marker: None
    rc.complete = lambda system, user, task: "GENERATED CONTEXT"
    try:
        limits._reset()
        body = rc.run(FakeRepo())
        assert body == "GENERATED CONTEXT" and upserted == ["GENERATED CONTEXT"]

        # a quota/warning response must not get cached as if it were real context
        upserted.clear()
        rc.complete = lambda s, u, t: "⚠️ AI quota reached, try again later."
        assert rc.run(FakeRepo()) is None and upserted == []

        # ensure_fresh: no issue yet -> generates
        rc.complete = lambda s, u, t: "FRESH"
        rc.gh.get_context_issue = lambda repo, marker: None
        assert rc.ensure_fresh(FakeRepo()) == "FRESH"

        # ensure_fresh: fresh issue present -> reused, model not called
        class FreshIssue:
            from datetime import datetime, timezone
            body = "<!-- bot:context -->\nCACHED"
            updated_at = datetime.now(timezone.utc)

        rc.gh.get_context_issue = lambda repo, marker: FreshIssue()
        rc.complete = lambda s, u, t: (_ for _ in ()).throw(AssertionError("must not call model"))
        assert rc.ensure_fresh(FakeRepo()) == "CACHED"

        # ensure_fresh: stale issue + generation blocked by cap -> falls back to stale text
        class StaleIssue:
            from datetime import datetime, timedelta, timezone
            body = "<!-- bot:context -->\nOLD BUT PRESENT"
            updated_at = datetime.now(timezone.utc) - timedelta(days=60)

        rc.gh.get_context_issue = lambda repo, marker: StaleIssue()
        from scripts.config import PR_DAILY_MAX
        for _ in range(PR_DAILY_MAX):  # spend the "context" pseudo-PR's cap so the next call is blocked
            limits.allow_llm_call("o/r", "context")
        assert rc.ensure_fresh(FakeRepo()) == "OLD BUT PRESENT"
    finally:
        rc.gh.get_tree, rc.gh.get_file_text, rc.gh.upsert_issue, rc.gh.get_context_issue, rc.complete = orig
        limits._reset()


def test_anchors():
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n"
        "@@ -1,2 +1,3 @@\n a\n-b\n+B\n+C\n"          # foo.py RIGHT lines: 1,2,3
        "--- a/bar.py\n+++ b/bar.py\n"
        "@@ -10,1 +10,1 @@\n-x\n+y\n"                # bar.py RIGHT line: 10
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-z\n"  # deletion: no anchors
    )
    a = anchors(diff)
    assert a == {"foo.py": {1, 2, 3}, "bar.py": {10}}, a


def _block(path, body="+x\n"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


def test_strip_noise():
    code, lock, minjs = _block("app.py"), _block("package-lock.json"), _block("static/app.min.js")
    assert strip_noise(code + lock + minjs) == code
    assert strip_noise(code) == code                       # nothing noisy -> unchanged
    # a DELETED lock file (+++ /dev/null) is still noise — match on the old path
    gone = "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    assert strip_noise(code + gone) == code
    assert strip_noise("") == ""


def test_truncate_diff_per_file():
    from scripts.llm_client import truncate_diff

    a, b, c = _block("a.py", "+aaa\n"), _block("b.py", "+bbb\n"), _block("c.py", "+ccc\n")
    out = truncate_diff(a + b + c, max_chars=len(a) + len(b))
    assert a in out and b in out          # whole blocks kept
    assert "+ccc" not in out              # dropped block never half-included
    assert "c.py" in out.split("omitted")[1]  # note names what was cut
    # under the cap -> untouched, no note
    assert truncate_diff(a + b, max_chars=len(a) + len(b)) == a + b
    # one block alone over the cap -> fall back to the plain slice
    huge = _block("big.py", "+" + "z" * 500 + "\n")
    out = truncate_diff(huge, max_chars=100)
    assert len(out) < len(huge) and "truncated" in out


def test_complete_json_mode():
    # json_mode=True must request response_format json_object; a 400 on that call
    # (model doesn't support it) must retry the same model without it.
    import os
    from types import SimpleNamespace as NS

    import httpx

    import scripts.llm_client as lc
    from scripts import limits

    calls, reject_json = [], {"v": False}

    def fake_create(**kw):
        calls.append(kw)
        if "response_format" in kw and reject_json["v"]:
            resp = httpx.Response(400, request=httpx.Request("POST", "http://t"))
            raise lc.APIStatusError("no json mode", response=resp, body=None)
        return NS(choices=[NS(message=NS(content='{"ok": 1}'))])

    orig_openai, orig_key = lc.OpenAI, os.environ.get("GROQ_API_KEY")
    lc.OpenAI = lambda **kw: NS(chat=NS(completions=NS(create=fake_create)))
    os.environ["GROQ_API_KEY"] = "dummy"
    try:
        limits._reset()
        assert lc.complete("s", "u", "review", json_mode=True) == '{"ok": 1}'
        assert calls[-1]["response_format"] == {"type": "json_object"}

        calls.clear()
        reject_json["v"] = True  # provider 400s on json mode -> plain retry succeeds
        assert lc.complete("s", "u", "review", json_mode=True) == '{"ok": 1}'
        assert len(calls) == 2 and "response_format" not in calls[-1]

        calls.clear()
        assert lc.complete("s", "u", "summary") == '{"ok": 1}'  # default: no json mode
        assert "response_format" not in calls[-1]
    finally:
        lc.OpenAI = orig_openai
        if orig_key is None:
            del os.environ["GROQ_API_KEY"]
        else:
            os.environ["GROQ_API_KEY"] = orig_key
        limits._reset()


def test_build_prompt_includes_pr_text():
    from scripts.review_pr import build_prompt

    p = build_prompt("DIFF", conventions="rules", pr_text="Add caching\n\nWhy: speed")
    assert "Add caching" in p and "Why: speed" in p
    assert p.index("Add caching") < p.index("DIFF")  # intent before implementation
    assert "Add caching" not in build_prompt("DIFF", conventions="rules")  # optional


def test_build_prompt_includes_project_context():
    from scripts.review_pr import build_prompt

    p = build_prompt("DIFF", conventions="rules", project_context="This repo does X.")
    assert "This repo does X." in p
    assert p.index("This repo does X.") < p.index("DIFF")  # context before the diff
    assert "Project context" not in build_prompt("DIFF", conventions="")  # optional, omitted when absent


def test_run_feeds_pr_text_to_model():
    # run() must pass the PR title+body so the reviewer can check intent vs code.
    import scripts.review_pr as rp
    from scripts import limits

    prompts = []

    class FakeRepo:
        full_name = "o/r"

        def get_pull(self, n):
            return type("P", (), {
                "head": type("H", (), {"sha": "h"}),
                "title": "Fix the frobnicator",
                "body": "## Why\nit was broken",
            })()

    orig = (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text, rp.reconcile_inline,
            rp.repo_context.ensure_fresh)
    # `u` is a per-model prompt builder now — resolve it the way complete() would.
    rp.complete = lambda s, u, t, **kw: (prompts.append(u("m") if callable(u) else u), '{"verdict":"comment","summary":"ok","issues":[]}')[1]
    rp.gh.upsert_comment = lambda repo, n, marker, body: None
    rp.gh.get_file_text = lambda repo, path: ""
    rp.reconcile_inline = lambda *a, **k: None
    rp.repo_context.ensure_fresh = lambda repo: ""
    try:
        limits._reset()
        rp.run(FakeRepo(), 1, "a diff")
        assert "Fix the frobnicator" in prompts[0] and "it was broken" in prompts[0]
    finally:
        (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text, rp.reconcile_inline,
         rp.repo_context.ensure_fresh) = orig
        limits._reset()


def test_run_feeds_project_context_to_model():
    # run() must call repo_context.ensure_fresh and pass its result into the prompt.
    import scripts.review_pr as rp
    from scripts import limits

    prompts = []

    class FakeRepo:
        full_name = "o/r"

        def get_pull(self, n):
            return type("P", (), {"head": type("H", (), {"sha": "h"}), "title": "t", "body": ""})()

    orig = (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text, rp.reconcile_inline,
            rp.repo_context.ensure_fresh)
    # `u` is a per-model prompt builder now — resolve it the way complete() would.
    rp.complete = lambda s, u, t, **kw: (prompts.append(u("m") if callable(u) else u), '{"verdict":"comment","summary":"ok","issues":[]}')[1]
    rp.gh.upsert_comment = lambda repo, n, marker, body: None
    rp.gh.get_file_text = lambda repo, path: ""
    rp.reconcile_inline = lambda *a, **k: None
    rp.repo_context.ensure_fresh = lambda repo: "PROJECT DOES Y"
    try:
        limits._reset()
        rp.run(FakeRepo(), 9, "a diff")
        assert "PROJECT DOES Y" in prompts[0]
    finally:
        (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text, rp.reconcile_inline,
         rp.repo_context.ensure_fresh) = orig
        limits._reset()


def test_react_wiring():
    # react() must reach the comment via Issue.get_comment (Repository has no
    # get_issue_comment in PyGithub — the old path raised AttributeError on every
    # call, silently) and must swallow failures without raising.
    from scripts import gh

    reacted = []

    class FakeComment:
        def create_reaction(self, content):
            reacted.append(content)

    class FakeIssue:
        def get_comment(self, cid):
            assert cid == 7
            return FakeComment()

    class FakeRepo:
        def get_issue(self, n):
            assert n == 1
            return FakeIssue()

    gh.react(FakeRepo(), 1, 7)
    assert reacted == ["eyes"]

    class BoomRepo:
        def get_issue(self, n):
            raise RuntimeError("nope")

    gh.react(BoomRepo(), 1, 7)  # best-effort: must not raise


def test_get_tree():
    from scripts import gh

    class Elem:
        def __init__(self, path, type_, size=None):
            self.path, self.type, self.size = path, type_, size

    class Tree:
        # dirs have no size (None) — get_tree must coerce, not crash
        tree = [Elem("app.py", "blob", 120), Elem("scripts", "tree"), Elem("uv.lock", "blob", 9000)]

    class FakeRepo:
        default_branch = "main"

        def get_git_tree(self, sha, recursive=False):
            assert sha == "main" and recursive is True
            return Tree()

    assert gh.get_tree(FakeRepo()) == [("app.py", 120), ("uv.lock", 9000)]  # only blobs, dirs excluded

    class BoomRepo:
        default_branch = "main"

        def get_git_tree(self, sha, recursive=False):
            raise RuntimeError("no repo")

    assert gh.get_tree(BoomRepo()) == []  # best-effort: never raises


def test_context_issue_upsert():
    from scripts import gh

    class FakeUser:
        def __init__(self, login, type_="Bot"):
            self.login, self.type = login, type_

    class FakeIssue:
        def __init__(self, body, user=None):
            self.body = body
            self.edits = []
            self.user = user or FakeUser("sidekick-cat[bot]")

        def edit(self, **kw):
            self.edits.append(kw)
            if "body" in kw:
                self.body = kw["body"]

    class FakeRepo:
        def __init__(self):
            self.issues = []
            self.created = []

        def get_issues(self, state=None):
            assert state == "all"
            return self.issues

        def create_issue(self, title, body):
            issue = FakeIssue(body)
            issue.title = title
            self.created.append(issue)
            self.issues.append(issue)
            return issue

    repo = FakeRepo()
    assert gh.get_context_issue(repo, "bot:context") is None  # nothing yet

    # a human-authored issue that happens to contain the marker text must NOT
    # be treated as the bot's context cache (would let anyone hijack/close it)
    planted = FakeIssue("<!-- bot:context -->\nplanted by a human", user=FakeUser("attacker", type_="User"))
    repo.issues.append(planted)
    assert gh.get_context_issue(repo, "bot:context") is None

    issue = gh.upsert_issue(repo, "bot:context", "Title", "hello")
    assert len(repo.created) == 1
    assert issue.body == "<!-- bot:context -->\nhello"
    assert issue.edits[-1] == {"state": "closed"}  # closed after create

    found = gh.get_context_issue(repo, "bot:context")
    assert found is issue  # marker match, bot-authored

    gh.upsert_issue(repo, "bot:context", "Title", "updated")
    assert len(repo.created) == 1  # no second issue created
    assert issue.body == "<!-- bot:context -->\nupdated"
    assert issue.edits[-1] == {"body": "<!-- bot:context -->\nupdated", "state": "closed"}


def test_unified_from_files():
    from scripts.gh import unified_from_files

    files = [
        ("mod.py", None, "modified", "@@ -1 +1 @@\n-a\n+b"),
        ("new.py", None, "added", "@@ -0,0 +1 @@\n+n"),
        ("gone.py", None, "removed", "@@ -1 +0,0 @@\n-g"),
        ("moved.py", "old.py", "renamed", "@@ -1 +1 @@\n-x\n+y"),
        ("bin.png", None, "modified", None),  # binary: no patch, skipped
    ]
    out = unified_from_files(files)
    a = anchors(out)  # the rebuilt diff must anchor like a real one
    assert a["mod.py"] == {1} and a["new.py"] == {1} and a["moved.py"] == {1}
    assert "gone.py" not in a and "bin.png" not in out


def test_reconcile_scope():
    # scope limits stale-deletion to the re-reviewed files; None means everything.
    import scripts.review_pr as rp

    deleted = []

    class C:
        def __init__(self, path, line):
            self.path, self.line, self.body = path, line, "b"

        def delete(self):
            deleted.append((self.path, self.line))

        def edit(self, body):
            pass

    orig = (rp.gh.get_inline_comments, rp.gh.create_review_comment)
    rp.gh.get_inline_comments = lambda *a: [C("a.py", 1), C("b.py", 2)]
    rp.gh.create_review_comment = lambda *a: None
    try:
        rp.reconcile_inline(None, 1, "sha", [], scope={"a.py"})
        assert deleted == [("a.py", 1)]  # b.py untouched by this review -> kept
        deleted.clear()
        rp.reconcile_inline(None, 1, "sha", [])
        assert sorted(deleted) == [("a.py", 1), ("b.py", 2)]  # full review -> all stale
    finally:
        rp.gh.get_inline_comments, rp.gh.create_review_comment = orig


def test_review_incremental():
    # A previously reviewed PR with a new head reviews only compare(prev, head),
    # scopes reconciliation to those files, and says so; if compare fails
    # (force-push), fall back to the full diff.
    import scripts.review_pr as rp
    from scripts import limits

    prompts, scopes, bodies, sha = [], [], [], {"v": "h1"}

    class FakeRepo:
        full_name = "o/r"

        def get_pull(self, n):
            return type("P", (), {"head": type("H", (), {"sha": sha["v"]}), "title": "t", "body": ""})()

    full = _block("full.py", "+base\n") + _block("changed.py", "+base\n")
    orig = (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text,
            rp.reconcile_inline, rp.gh.compare_diff, rp.repo_context.ensure_fresh)
    # `u` is a per-model prompt builder now — resolve it the way complete() would.
    rp.complete = lambda s, u, t, **kw: (prompts.append(u("m") if callable(u) else u), '{"verdict":"comment","summary":"ok","issues":[]}')[1]
    rp.gh.upsert_comment = lambda repo, n, m, body: bodies.append(body)
    rp.gh.get_file_text = lambda repo, path: ""
    rp.reconcile_inline = lambda repo, n, s, anch, scope=None: scopes.append(scope)
    rp.gh.compare_diff = lambda repo, base, head: _block("changed.py", "+delta\n")
    rp.repo_context.ensure_fresh = lambda repo: ""
    try:
        limits._reset()
        rp.run(FakeRepo(), 1, full)
        assert "full.py" in prompts[0] and scopes[0] is None  # first review: full

        sha["v"] = "h2"
        rp.run(FakeRepo(), 1, full)
        assert "delta" in prompts[1] and "full.py" not in prompts[1]
        assert scopes[1] == {"changed.py"}
        assert "h1"[:7] in bodies[-1]  # summary names the incremental base

        sha["v"] = "h2"
        rp.run(FakeRepo(), 1, full)  # unchanged head -> free skip
        assert len(prompts) == 2

        sha["v"] = "h3"
        rp.gh.compare_diff = lambda *a: (_ for _ in ()).throw(RuntimeError("force-push"))
        rp.run(FakeRepo(), 1, full)
        assert "full.py" in prompts[2] and scopes[2] is None  # fallback: full review
    finally:
        (rp.complete, rp.gh.upsert_comment, rp.gh.get_file_text,
         rp.reconcile_inline, rp.gh.compare_diff, rp.repo_context.ensure_fresh) = orig
        limits._reset()


def test_number_diff():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n"
        "@@ -1,2 +1,3 @@\n a\n-b\n+B\n+C\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-z\n"
    )
    out = number_diff(diff).splitlines()
    # RIGHT-side (context/added) lines carry their new-file number; the rest don't
    assert "1|  a" in out and "2| +B" in out and "3| +C" in out
    assert "-b" in out and "@@ -1,2 +1,3 @@" in out  # unnumbered, untouched
    assert not any(l.endswith("|-z") or "| -z" in l for l in out)  # deletion: no numbers
    assert number_diff("") == ""


def test_partition_snaps_near_misses():
    amap = {"foo.py": {10, 11, 12, 30}}
    issues = [
        {"path": "foo.py", "line": 13, "body": "off by one"},   # snaps to 12
        {"path": "foo.py", "line": 27, "body": "off by three"}, # snaps to 30
        {"path": "foo.py", "line": 20, "body": "too far"},      # >3 away -> unanchorable
        {"path": "nope.py", "line": 10, "body": "bad path"},    # no anchors -> unanchorable
    ]
    ok, no = partition(issues, amap)
    assert [i["line"] for i in ok] == [12, 30]
    assert len(no) == 2


def test_parse_response():
    assert parse_response('{"verdict":"approve","issues":[]}') == {"verdict": "approve", "issues": []}
    assert parse_response('```json\n{"a":1}\n```') == {"a": 1}          # fenced
    assert parse_response('Sure! Here:\n```\n{"a":2}\n```') == {"a": 2}  # prose + fence
    assert parse_response("here: {\"a\": 3} done") == {"a": 3}           # bare, embedded
    assert parse_response("not json at all") is None
    assert parse_response("") is None
    assert parse_response("[1,2,3]") is None  # a list is not an issue object
    # a ```lang fence *inside* a body must not derail parsing (first { .. last })
    fenced = '{"issues":[{"body":"fix it\\n```python\\nx == 1\\n```"}]}'
    assert parse_response(fenced) == {"issues": [{"body": "fix it\n```python\nx == 1\n```"}]}


def test_partition():
    amap = {"foo.py": {1, 2, 3}, "bar.py": {10}}
    issues = [
        {"path": "foo.py", "line": 2, "body": "ok"},       # anchorable
        {"path": "foo.py", "line": "3", "body": "str ok"}, # coerced to int -> anchorable
        {"path": "foo.py", "line": 99, "body": "bad line"},# line not in diff
        {"path": "nope.py", "line": 1, "body": "bad path"},# path not in diff
        {"path": "bar.py", "body": "no line"},             # missing line
    ]
    ok, no = partition(issues, amap)
    assert [i["line"] for i in ok] == [2, 3]  # both normalized to int
    assert len(no) == 3


def test_unresolved_count():
    def resp(states):
        nodes = [{"isResolved": s} for s in states]
        return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
    assert unresolved_count(resp([False, True, False])) == 2
    assert unresolved_count(resp([True, True])) == 0
    assert unresolved_count(resp([])) == 0


_GOOD_BODY = "## TL;DR\nx\n## What\nx\n## Why\nx\n## Test\nx"


def test_blockers():
    clean = {"state": "OPEN", "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN",
             "body": _GOOD_BODY}
    assert blockers(clean) == []  # all gates green
    assert blockers({**clean, "mergeStateStatus": "HAS_HOOKS"}) == []  # also mergeable
    assert blockers({**clean, "state": "CLOSED"}) == ["PR is closed, not open."]
    # mergeStateStatus drives the conflict/checks/branch-protection/draft messages
    assert blockers({**clean, "mergeStateStatus": "DIRTY"}) == ["there are merge conflicts to resolve."]
    assert blockers({**clean, "mergeStateStatus": "BLOCKED"})[0].startswith("branch protection")
    assert blockers({**clean, "mergeStateStatus": "UNSTABLE"}) == ["one or more checks are failing or still running."]
    assert blockers({**clean, "mergeStateStatus": "DRAFT"}) == ["the PR is still a draft."]
    # CHANGES_REQUESTED blocks even when the merge state itself is clean
    assert blockers({**clean, "reviewDecision": "CHANGES_REQUESTED"}) == ["changes were requested in review."]
    # unknown enum value falls through to a generic message rather than crashing
    assert blockers({**clean, "mergeStateStatus": "WEIRD"}) == ["merge state is WEIRD."]
    # no review decision + clean == mergeable
    assert blockers({"state": "OPEN", "reviewDecision": "", "mergeStateStatus": "CLEAN",
                     "body": _GOOD_BODY}) == []
    # a description still missing template sections blocks the merge — otherwise the
    # ❌ validate comment goes stale and the PR bypasses the template check entirely
    bad = blockers({**clean, "body": "just prose, no sections"})
    assert len(bad) == 1 and "TL;DR" in bad[0] and "description" in bad[0]
    assert "description" in blockers({**clean, "body": None})[0]  # empty body too


def test_merge_gating():
    # run() must refuse to merge while any gate fails (dirty state, unresolved
    # threads) and squash-merge only when all gates are clean.
    import scripts.merge_pr as mp

    posted, merged = [], []

    class Pull:
        head = type("H", (), {"ref": "feat"})()

        def merge(self, merge_method=None):
            merged.append(merge_method)

    class FakeRepo:
        full_name = "o/r"

        def get_pull(self, n):
            return Pull()

        def get_git_ref(self, ref):
            raise Exception("no branch")  # exercise best-effort delete

    def resp(state="OPEN", mss="CLEAN", review="APPROVED", threads=(), body=_GOOD_BODY):
        return {"data": {"repository": {"pullRequest": {
            "state": state, "reviewDecision": review, "mergeStateStatus": mss, "body": body,
            "reviewThreads": {"nodes": [{"isResolved": r} for r in threads]}}}}}

    orig = (mp.gh.graphql, mp.gh.upsert_comment)
    mp.gh.upsert_comment = lambda repo, n, marker, body: posted.append(body)
    try:
        mp.gh.graphql = lambda token, q, v: resp(mss="DIRTY")
        mp.run(FakeRepo(), 1, "tok")
        assert merged == [] and "can't merge" in posted[-1]  # dirty → blocked

        mp.gh.graphql = lambda token, q, v: resp(threads=[False, True])
        mp.run(FakeRepo(), 1, "tok")
        assert merged == [] and "unresolved" in posted[-1]  # open thread → blocked

        mp.gh.graphql = lambda token, q, v: resp(body="no template here")
        mp.run(FakeRepo(), 1, "tok")
        assert merged == [] and "description" in posted[-1]  # bad description → blocked

        mp.gh.graphql = lambda token, q, v: resp(threads=[True])
        mp.run(FakeRepo(), 1, "tok")
        assert merged == ["squash"] and "Shipped" in posted[-1]  # all clean → merge
    finally:
        mp.gh.graphql, mp.gh.upsert_comment = orig


if __name__ == "__main__":
    test_missing_sections()
    test_labels_for()
    test_summarize_empty()
    test_summarize_gating()
    test_review_gating()
    test_review_routes_by_size()
    test_render_tree()
    test_pick_head_files()
    test_build_context_prompt()
    test_is_stale()
    test_repo_context_run_and_ensure_fresh()
    test_anchors()
    test_strip_noise()
    test_truncate_diff_per_file()
    test_complete_json_mode()
    test_build_prompt_includes_pr_text()
    test_build_prompt_includes_project_context()
    test_run_feeds_pr_text_to_model()
    test_run_feeds_project_context_to_model()
    test_react_wiring()
    test_get_tree()
    test_context_issue_upsert()
    test_unified_from_files()
    test_reconcile_scope()
    test_review_incremental()
    test_number_diff()
    test_partition_snaps_near_misses()
    test_parse_response()
    test_partition()
    test_blockers()
    test_unresolved_count()
    test_merge_gating()
    print("ok")
