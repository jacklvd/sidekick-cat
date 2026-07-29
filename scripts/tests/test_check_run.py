"""Offline self-checks for the Sidekick check run — pure gate logic and the two
I/O entry points with all GitHub calls faked. No network, no token.

Run: uv run python -m scripts.tests.test_check_run
"""

from scripts import gh


def test_upsert_check_run_edits_when_present_creates_when_absent():
    calls = {"created": [], "edited": []}

    class FakeRun:
        def __init__(self, name):
            self.name = name

        def edit(self, **kw):
            calls["edited"].append(kw)

    class FakeCommit:
        def __init__(self, runs):
            self._runs = runs

        def get_check_runs(self, check_name=None):
            # Mirror the REST filter: only runs whose name matches are returned.
            return [r for r in self._runs if r.name == check_name]

    class FakeRepo:
        def __init__(self, runs):
            self._runs = runs

        def get_commit(self, sha):
            return FakeCommit(self._runs)

        def create_check_run(self, **kw):
            calls["created"].append(kw)
            return FakeRun(kw["name"])

    # Absent -> create, with output folded and a real conclusion passed through.
    gh.upsert_check_run(FakeRepo([]), "h1", "Sidekick", "completed", "success", "T", "S")
    assert len(calls["created"]) == 1
    assert calls["created"][0]["name"] == "Sidekick"
    assert calls["created"][0]["head_sha"] == "h1"
    assert calls["created"][0]["status"] == "completed"
    assert calls["created"][0]["conclusion"] == "success"
    assert calls["created"][0]["output"] == {"title": "T", "summary": "S"}

    # Present -> edit the SAME run, no new create; a None conclusion (in_progress)
    # is omitted so GitHub isn't sent conclusion=null on an unfinished check.
    gh.upsert_check_run(
        FakeRepo([FakeRun("Sidekick")]), "h1", "Sidekick", "in_progress", None, "T2", "S2"
    )
    assert len(calls["created"]) == 1  # unchanged — no duplicate row
    assert len(calls["edited"]) == 1
    assert calls["edited"][0]["status"] == "in_progress"
    assert "conclusion" not in calls["edited"][0]
    assert calls["edited"][0]["output"] == {"title": "T2", "summary": "S2"}

    # A check of a DIFFERENT name on the head is not ours -> we still create.
    gh.upsert_check_run(
        FakeRepo([FakeRun("CI")]), "h1", "Sidekick", "completed", "failure", "T3", "S3"
    )
    assert len(calls["created"]) == 2


def test_conclusion_of_covers_every_gate_and_precedence():
    from scripts.check_run import conclusion_of

    # Gate 1: review in flight for this head.
    assert conclusion_of("pending", 0, []) == (
        "in_progress", None, "🐱 Reviewing…",
        "Sidekick is reviewing the latest changes to this pull request.",
    )

    # Gate 2: the review failed (outage / rate limit) -> neutral, does not block.
    status, conclusion, title, summary = conclusion_of("failed", 0, [])
    assert (status, conclusion) == ("completed", "neutral")
    assert title == "🐱 Sidekick couldn't review — merge at your own risk"
    assert "neutral" in summary and "/review" in summary

    # Gate 3: unresolved threads.
    assert conclusion_of("done", 2, []) == (
        "completed", "failure", "🐱 2 unresolved review thread(s)",
        "- 2 unresolved review thread(s) to address.",
    )

    # Gate 4: description missing sections (no unresolved threads).
    assert conclusion_of("done", 0, ["Test"]) == (
        "completed", "failure", "🐱 Description missing: Test",
        "- Description missing: Test.",
    )

    # Gate 5: clean.
    assert conclusion_of("done", 0, []) == (
        "completed", "success", "🐱 No unresolved feedback",
        "All review threads are resolved and the description is complete.",
    )

    # Precedence: threads AND a missing section -> gate 3 title, BOTH in the summary.
    status, conclusion, title, summary = conclusion_of("done", 3, ["Test"])
    assert (status, conclusion) == ("completed", "failure")
    assert title == "🐱 3 unresolved review thread(s)"
    assert "3 unresolved review thread(s)" in summary
    assert "Description missing: Test" in summary


def _pr_graphql(body="TL;DR\nWhat\nWhy\nTest", n_unresolved=0):
    """A well-formed merge_pr.PR_QUERY response: body + reviewThreads nodes."""
    nodes = [{"isResolved": False}] * n_unresolved
    return {"data": {"repository": {"pullRequest": {
        "body": body, "reviewThreads": {"nodes": nodes}}}}}


class _FakeCheck:
    def __init__(self, status, conclusion):
        self.status = status
        self.conclusion = conclusion


class _FakeRepo:
    full_name = "o/r"

    def get_pull(self, n):
        return type("P", (), {"head": type("H", (), {"sha": "h9"})})()


def test_conclude_always_writes_even_over_in_progress():
    import scripts.check_run as cr

    writes = []
    orig = (cr.gh.graphql, cr.gh.upsert_check_run, cr.gh.get_check_run)
    cr.gh.graphql = lambda token, q, v: _pr_graphql(n_unresolved=1)
    cr.gh.upsert_check_run = lambda repo, sha, name, st, cc, t, s: writes.append((sha, st, cc, t))
    # Even with an in_progress check already on the head, conclude overwrites it.
    cr.gh.get_check_run = lambda repo, sha, name: _FakeCheck("in_progress", None)
    try:
        cr.conclude(_FakeRepo(), 1, "tok", "done")
        assert writes == [("h9", "completed", "failure", "🐱 1 unresolved review thread(s)")]

        # "pending" and "failed" write without reading thread/description state at all.
        writes.clear()
        cr.gh.graphql = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no read on pending/failed"))
        cr.conclude(_FakeRepo(), 1, "tok", "pending")
        cr.conclude(_FakeRepo(), 1, "tok", "failed")
        assert [w[1:3] for w in writes] == [("in_progress", None), ("completed", "neutral")]
    finally:
        (cr.gh.graphql, cr.gh.upsert_check_run, cr.gh.get_check_run) = orig


def test_refresh_is_deferential_the_race_test():
    import scripts.check_run as cr

    writes = []
    orig = (cr.gh.graphql, cr.gh.upsert_check_run, cr.gh.get_check_run)
    # Threads are all clean — a naive recompute would flip the check to success.
    cr.gh.graphql = lambda token, q, v: _pr_graphql(n_unresolved=0)
    cr.gh.upsert_check_run = lambda repo, sha, name, st, cc, t, s: writes.append((st, cc))
    try:
        # in_progress: a review owns this head. refresh must NOT write — this is the
        # race fix. If the guard were removed, refresh would recompute to success and
        # append to `writes`, and this assertion would fail. THAT is what it protects.
        cr.gh.get_check_run = lambda repo, sha, name: _FakeCheck("in_progress", None)
        cr.refresh(_FakeRepo(), 1, "tok")
        assert writes == []

        # neutral: the review failed; thread state is moot. No write.
        cr.gh.get_check_run = lambda repo, sha, name: _FakeCheck("completed", "neutral")
        cr.refresh(_FakeRepo(), 1, "tok")
        assert writes == []

        # absent: refresh NEVER mints a check (drafts stay checkless). No write.
        cr.gh.get_check_run = lambda repo, sha, name: None
        cr.refresh(_FakeRepo(), 1, "tok")
        assert writes == []

        # already concluded failure -> recompute from live (now-clean) state -> success.
        cr.gh.get_check_run = lambda repo, sha, name: _FakeCheck("completed", "failure")
        cr.refresh(_FakeRepo(), 1, "tok")
        assert writes == [("completed", "success")]
    finally:
        (cr.gh.graphql, cr.gh.upsert_check_run, cr.gh.get_check_run) = orig


if __name__ == "__main__":
    test_upsert_check_run_edits_when_present_creates_when_absent()
    test_conclusion_of_covers_every_gate_and_precedence()
    test_conclude_always_writes_even_over_in_progress()
    test_refresh_is_deferential_the_race_test()
    print("ok")
