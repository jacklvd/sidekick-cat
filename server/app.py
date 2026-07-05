"""FastAPI webhook service for sidekick-cat on Cloud Run.

The single public endpoint every installed repo's GitHub App events hit.
See README.md#architecture. Env: WEBHOOK_SECRET (HMAC verify); APP_ID/APP_KEY
and the LLM keys are read by lower layers.

/webhook verifies HMAC on the raw body, drops bot/irrelevant events fast,
dedupes deliveries, acks (202), and hands the real work to a background task.
The background flows: pr_open runs the deterministic flow (welcome+assign,
validate, label) plus the gated AI summary; /review runs the gated AI review;
/merge runs the merge gate; /context (re)generates the cached project-context
doc. All reuse the scripts' host-agnostic run() cores.
"""

import logging
import os

from fastapi import BackgroundTasks, FastAPI, Request, Response

from scripts import gh, label_pr, merge_pr, repo_context, review_pr, summarize_pr, validate_pr, welcome
from scripts.gh import get_pr_diff, repo_from_token
from scripts.limits import delivery_seen
from server.gh_app_auth import installation_token
from server.router import classify
from server.security import verify

log = logging.getLogger("sidekick-cat")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="sidekick-cat")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


# dev-note: NOT "/healthz" — Google's GFE reserves that exact path and answers it
# itself before the container sees it (404 HTML page). "/health" passes through.
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def dispatch(intent: dict) -> None:
    """Run the flow for a classified event. Runs AFTER the 202 ack.

    dev-note: in-process background work — requires Cloud Run CPU-always-allocated
    (--no-cpu-throttling) so it isn't frozen after the 202. A Cloud Tasks hand-off
    could replace this in-process seam if durability/retries ever matter.
    """
    kind = intent.get("kind")
    number = intent.get("number")
    log.info("dispatch %s repo=%s/%s pr=%s", kind,
             intent.get("owner"), intent.get("repo"), number)
    try:
        full_name = f'{intent["owner"]}/{intent["repo"]}'
        token = installation_token(intent["installation_id"])
        repo = repo_from_token(token, full_name)
        if kind == "pr_open":
            # Each step is independent GitHub I/O; isolate so one failure (e.g. a
            # fork PR that blocks assign or labels) doesn't skip the rest.
            _safe("welcome", welcome.run, repo, number, intent.get("author"))
            _safe("validate", validate_pr.run, repo, number)
            _safe("label", label_pr.run, repo, number)
            _safe("summary", _summary, repo, full_name, number, token)
        elif kind == "pr_update":
            # PR changed after open: recheck the description and relabel. Both are
            # idempotent upserts, so the ❌/✅ flag and labels track the latest state.
            _safe("validate", validate_pr.run, repo, number)
            _safe("label", label_pr.run, repo, number)
        elif kind == "command" and intent.get("command") == "review":
            gh.react(repo, number, intent["comment_id"])  # 👀 immediate ack on the comment
            review_pr.run(repo, number, get_pr_diff(full_name, number, token))
        elif kind == "command" and intent.get("command") == "merge":
            gh.react(repo, number, intent["comment_id"])  # 👀 immediate ack on the comment
            merge_pr.run(repo, number, token)
        elif kind == "command" and intent.get("command") == "context":
            gh.react(repo, number, intent["comment_id"])  # 👀 immediate ack on the comment
            body = repo_context.run(repo)
            msg = (
                "🐱 Project context refreshed." if body
                else "🐱 Sidekick is taking a breather — try `/context` again later."
            )
            gh.upsert_comment(repo, number, "bot:context-ack", msg)
        else:
            log.info("dispatch %s: no handler, ignoring", kind)
    except Exception:
        # Already acked 202; log so a failed flow doesn't crash the worker silently.
        log.exception("dispatch failed for %s", kind)


def _summary(repo, full_name: str, number, token: str) -> None:
    """Fetch the diff (REST, no `gh` CLI) and run the gated AI summary."""
    summarize_pr.run(repo, number, get_pr_diff(full_name, number, token))


def _safe(name: str, fn, *args) -> None:
    """Run one flow step, logging (not raising) on failure so siblings still run."""
    try:
        fn(*args)
    except Exception:
        log.exception("pr_open step %s failed", name)


@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks) -> Response:
    raw = await request.body()
    if not verify(request.headers.get("X-Hub-Signature-256"), raw, WEBHOOK_SECRET):
        return Response(status_code=401)

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return Response(status_code=200, content="pong")

    if delivery_seen(request.headers.get("X-GitHub-Delivery", "")):
        return Response(status_code=200, content="duplicate")

    intent = classify(event, await request.json())
    if intent["kind"] == "ignore":
        log.info("ignore %s/%s: %s", event, request.headers.get("X-GitHub-Event"),
                 intent.get("reason"))
        return Response(status_code=204)

    bg.add_task(dispatch, intent)
    return Response(status_code=202, content="accepted")
