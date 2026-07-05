"""Rate-limit / dedup / breaker seams for the Cloud Run service.

Daily LLM caps (allow_llm_call), last-reviewed-head tracking (reviewed_head /
record_reviewed_head — dedup + the base for incremental re-review), webhook
delivery dedup (delivery_seen), and a circuit breaker (breaker_open/record_*).

Backend: set LIMITS_BACKEND=firestore to share caps + dedup across instances
(Cloud Run scales to zero and runs several instances, so that state must live
outside the process). Without it — or if a Firestore RPC fails — the functions
fall back to the in-memory bodies below, which are per-instance but enough at
personal scale. The circuit breaker is always in-memory: per-instance backoff is
correct and avoids a Firestore round-trip on every LLM call.

dev-note: in-memory state is PER-INSTANCE — with Firestore off and --max-instances>1
the effective caps are up to that multiple, and a delivery/SHA could slip through on
a second instance. Acceptable because LLMs are structurally free and
comment-marker upserts make any reprocess idempotent.
"""

import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from scripts.config import (
    BREAKER_COOLDOWN_S,
    BREAKER_FAILS,
    BREAKER_WINDOW_S,
    GLOBAL_DAILY_MAX,
    PR_DAILY_MAX,
    REPO_DAILY_MAX,
)

log = logging.getLogger("sidekick-cat.limits")

# Firestore is optional: the package may be absent (local/test) and is only used
# when LIMITS_BACKEND=firestore. Guard the import so the module loads either way.
try:
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import firestore
except Exception:  # pragma: no cover - exercised only where the dep is installed
    AlreadyExists = None
    firestore = None

_USE_FS = os.environ.get("LIMITS_BACKEND") == "firestore" and firestore is not None
_DOC_TTL = timedelta(days=2)  # GC handled by a Firestore TTL policy on `exp`
_db = None


def _fs():
    """Lazily-built Firestore client, or None when the in-memory backend is active."""
    global _db
    if not _USE_FS:
        return None
    if _db is None:
        _db = firestore.Client()  # infers project from the Cloud Run metadata server
    return _db


def _docid(s: str) -> str:
    # Firestore doc ids can't contain '/'. Repo slugs do; '_' is collision-safe enough
    # for one account's repos. dev-note: hash the key if cross-org collisions matter.
    return s.replace("/", "_")


def _expiry():
    return datetime.now(timezone.utc) + _DOC_TTL


# --- in-memory backend (fallback + the breaker) -----------------------------

_MAX = 2048
_lock = threading.Lock()  # dev-note: per-instance lock; Firestore txns when FS is on.
_now = time.monotonic  # dev-note: indirection so tests can inject a fake clock.

_DELIVERIES: "OrderedDict[str, None]" = OrderedDict()
_HEADS: "OrderedDict[str, str]" = OrderedDict()  # repo#pr -> last reviewed head sha
_counts: "dict[str, int]" = {}
_counts_day = ""
_fails: "list[float]" = []
_open_until = 0.0


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _checks(repo, pr):
    """The three date-keyed cap checks: (doc-key, cap, blocked-reason)."""
    return [
        ("g", GLOBAL_DAILY_MAX, "global daily limit reached"),
        (f"r:{repo}", REPO_DAILY_MAX, "daily limit reached for this repo"),
        (f"p:{repo}#{pr}", PR_DAILY_MAX, "daily limit reached for this PR"),
    ]


def _mem_delivery_seen(delivery_id: str) -> bool:
    if not delivery_id:
        return False
    with _lock:
        if delivery_id in _DELIVERIES:
            return True
        _DELIVERIES[delivery_id] = None
        if len(_DELIVERIES) > _MAX:
            _DELIVERIES.popitem(last=False)
    return False


def _mem_allow_llm_call(repo: str, pr) -> "tuple[bool, str]":
    global _counts, _counts_day
    day = _today()
    with _lock:
        if day != _counts_day:  # new UTC day → wipe yesterday's counters
            _counts = {}
            _counts_day = day
        for key, cap, reason in _checks(repo, pr):
            if _counts.get(key, 0) >= cap:
                return False, reason
        for key, _, _ in _checks(repo, pr):
            _counts[key] = _counts.get(key, 0) + 1
    return True, ""


def _mem_reviewed_head(repo: str, pr) -> "str | None":
    with _lock:
        return _HEADS.get(f"{repo}#{pr}")


def _mem_record_reviewed_head(repo: str, pr, head_sha: str) -> None:
    with _lock:
        _HEADS[f"{repo}#{pr}"] = head_sha
        if len(_HEADS) > _MAX:
            _HEADS.popitem(last=False)


# --- Firestore backend ------------------------------------------------------


def _fs_create_once(db, collection: str, doc_id: str) -> bool:
    """True if the doc already existed (→ 'seen'); creates it atomically if not."""
    ref = db.collection(collection).document(_docid(doc_id))
    try:
        ref.create({"exp": _expiry()})  # create() fails iff the doc exists
        return False
    except AlreadyExists:
        return True


def _fs_allow_llm_call(db, repo: str, pr) -> "tuple[bool, str]":
    checks = _checks(repo, pr)
    day = _today()
    refs = [db.collection("llm_counts").document(_docid(f"{k}:{day}")) for k, _, _ in checks]
    return _allow_txn(db.transaction(), refs, checks)


_transactional = firestore.transactional if firestore else (lambda f: f)


@_transactional
def _allow_txn(transaction, refs, checks):
    """Read all three counters, then increment them iff every cap has room. Reads
    must precede writes inside a Firestore transaction — hence the two passes."""
    snaps = [ref.get(transaction=transaction) for ref in refs]
    counts = [(s.to_dict() or {}).get("count", 0) if s.exists else 0 for s in snaps]
    for (key, cap, reason), cur in zip(checks, counts):
        if cur >= cap:
            return False, reason
    exp = _expiry()
    for ref, cur in zip(refs, counts):
        transaction.set(ref, {"count": cur + 1, "exp": exp})
    return True, ""


# --- public API (dispatches FS → in-memory, degrading on FS error) ----------


def delivery_seen(delivery_id: str) -> bool:
    """True if this X-GitHub-Delivery id was seen before (and records it if not)."""
    db = _fs()
    if db is not None:
        try:
            if not delivery_id:
                return False
            return _fs_create_once(db, "deliveries", delivery_id)
        except Exception:
            log.warning("firestore delivery_seen failed, using in-memory", exc_info=True)
    return _mem_delivery_seen(delivery_id)


def allow_llm_call(repo: str, pr) -> "tuple[bool, str]":
    """Check the three daily caps and increment them atomically iff all pass.
    Returns (ok, reason); counters are untouched on a blocked call."""
    db = _fs()
    if db is not None:
        try:
            return _fs_allow_llm_call(db, repo, pr)
        except Exception:
            log.warning("firestore allow_llm_call failed, using in-memory", exc_info=True)
    return _mem_allow_llm_call(repo, pr)


# dev-note: reviewed heads reuse the "reviewed_shas" collection (its `exp` TTL
# policy already exists); the doc id changed from repo#pr@sha to repo#pr, so old
# per-sha docs just age out. TTL expiry of a head record only means the next
# /review is a full one instead of incremental — safe.


def reviewed_head(repo: str, pr) -> "str | None":
    """The head sha last reviewed for this PR, or None if never/expired."""
    db = _fs()
    if db is not None:
        try:
            snap = db.collection("reviewed_shas").document(_docid(f"{repo}#{pr}")).get()
            return (snap.to_dict() or {}).get("sha") if snap.exists else None
        except Exception:
            log.warning("firestore reviewed_head failed, using in-memory", exc_info=True)
    return _mem_reviewed_head(repo, pr)


def record_reviewed_head(repo: str, pr, head_sha: str) -> None:
    """Record `head_sha` as this PR's last reviewed head (overwrites the previous)."""
    db = _fs()
    if db is not None:
        try:
            db.collection("reviewed_shas").document(_docid(f"{repo}#{pr}")).set(
                {"sha": head_sha, "exp": _expiry()}
            )
            return
        except Exception:
            log.warning("firestore record_reviewed_head failed, using in-memory", exc_info=True)
    _mem_record_reviewed_head(repo, pr, head_sha)


# --- circuit breaker (always in-memory; per-instance backoff is correct) -----


def breaker_open() -> bool:
    """True while the breaker is in its cooldown — skip provider calls."""
    with _lock:
        return _now() < _open_until


def record_failure() -> None:
    """Record a provider/API failure; open the breaker if too many in the window."""
    global _open_until
    t = _now()
    with _lock:
        _fails[:] = [f for f in _fails if t - f < BREAKER_WINDOW_S]
        _fails.append(t)
        if len(_fails) >= BREAKER_FAILS:
            _open_until = t + BREAKER_COOLDOWN_S
            _fails.clear()


def record_success() -> None:
    """Clear the recent-failure streak after a good call."""
    with _lock:
        _fails.clear()


def _reset() -> None:
    """Test seam: wipe all in-memory state. dev-note: tests only (FS is off in tests)."""
    global _counts, _counts_day, _open_until
    with _lock:
        _DELIVERIES.clear()
        _HEADS.clear()
        _counts = {}
        _counts_day = ""
        _fails.clear()
        _open_until = 0.0
