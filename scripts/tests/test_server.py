"""Offline self-check for the Cloud Run adapter logic (no network, no token).
Run: uv run python -m scripts.tests.test_server
"""

import hashlib
import hmac

from scripts.limits import delivery_seen
from server.router import classify
from server.security import verify

SECRET = "topsecret"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify():
    body = b'{"hello":"world"}'
    assert verify(_sig(body), body, SECRET) is True
    assert verify(_sig(body, "wrong"), body, SECRET) is False   # wrong secret
    assert verify(_sig(body), body + b" ", SECRET) is False      # tampered body
    assert verify(None, body, SECRET) is False                   # missing header
    assert verify("md5=deadbeef", body, SECRET) is False         # wrong scheme


def test_classify():
    # Loop guard: bot sender always ignored.
    assert classify("pull_request", {"action": "opened", "sender": {"type": "Bot"}})["kind"] == "ignore"
    assert classify("issue_comment", {"action": "created", "sender": {"login": "sidekick-cat[bot]"}})["kind"] == "ignore"

    # PR open -> pr_open with extracted fields. review=True: a non-draft open earns one.
    i = classify("pull_request", {
        "action": "opened", "sender": {"login": "alice"},
        "repository": {"full_name": "alice/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc"}, "user": {"login": "bob"}},
        "installation": {"id": 99},
    })
    assert i == {"kind": "pr_open", "owner": "alice", "repo": "repo",
                 "number": 7, "head_sha": "abc", "author": "bob", "installation_id": 99,
                 "review": True}

    # A draft opens with review=False: WIP is the phase with the most pushes and the
    # least finished code. /review still works inside one.
    d = classify("pull_request", {
        "action": "opened", "sender": {"login": "alice"},
        "repository": {"full_name": "alice/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc"}, "user": {"login": "bob"},
                         "draft": True},
        "installation": {"id": 99},
    })
    assert d["kind"] == "pr_open" and d["review"] is False

    # PR changed after open -> pr_update (revalidate + relabel). An `edited` is a
    # description/title change, not code -> review=False.
    u = classify("pull_request", {
        "action": "edited", "sender": {"login": "alice"},
        "repository": {"full_name": "alice/repo"},
        "pull_request": {"number": 7}, "installation": {"id": 99},
    })
    assert u == {"kind": "pr_update", "owner": "alice", "repo": "repo",
                 "number": 7, "installation_id": 99, "review": False}

    # New commits on a non-draft -> review.
    s = classify("pull_request", {"action": "synchronize", "sender": {"login": "a"},
                                  "pull_request": {"number": 1}})
    assert s["kind"] == "pr_update" and s["review"] is True

    # ...but not while it's a draft.
    sd = classify("pull_request", {"action": "synchronize", "sender": {"login": "a"},
                                   "pull_request": {"number": 1, "draft": True}})
    assert sd["kind"] == "pr_update" and sd["review"] is False

    # Marking a draft ready is the moment it earns its first review. GitHub sends
    # draft=False with this action (the transition already happened).
    r = classify("pull_request", {"action": "ready_for_review", "sender": {"login": "a"},
                                  "pull_request": {"number": 1, "draft": False}})
    assert r["kind"] == "pr_update" and r["review"] is True

    # A truly unhandled PR action is still ignored.
    assert classify("pull_request", {"action": "labeled", "sender": {"login": "a"}})["kind"] == "ignore"

    base = {
        "action": "created", "sender": {"login": "alice"},
        "repository": {"full_name": "alice/repo"},
        "issue": {"number": 7, "pull_request": {"url": "..."}},
        "installation": {"id": 99},
    }
    i = classify("issue_comment", {**base, "comment": {"body": "please /review", "author_association": "OWNER", "id": 5}})
    assert i["kind"] == "command" and i["command"] == "review" and i["comment_id"] == 5
    assert i["number"] == 7 and i["installation_id"] == 99
    i = classify("issue_comment", {**base, "comment": {"body": "/merge", "author_association": "MEMBER", "id": 6}})
    assert i["kind"] == "command" and i["command"] == "merge"
    i = classify("issue_comment", {**base, "comment": {"body": "/context", "author_association": "OWNER", "id": 7}})
    assert i["kind"] == "command" and i["command"] == "context"
    # unauthorized commenter
    assert classify("issue_comment", {**base, "comment": {"body": "/review", "author_association": "NONE"}})["kind"] == "ignore"
    # comment on an issue, not a PR
    assert classify("issue_comment", {**base, "issue": {"number": 7}, "comment": {"body": "/review", "author_association": "OWNER"}})["kind"] == "ignore"
    # no command in the body
    assert classify("issue_comment", {**base, "comment": {"body": "hi", "author_association": "OWNER"}})["kind"] == "ignore"


def test_delivery_dedup():
    assert delivery_seen("d-unique-1") is False   # first time
    assert delivery_seen("d-unique-1") is True     # repeat
    assert delivery_seen("") is False              # empty never dedupes


if __name__ == "__main__":
    test_verify()
    test_classify()
    test_delivery_dedup()
    print("ok")
