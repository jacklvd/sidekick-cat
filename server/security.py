"""Webhook signature verification — the only trust boundary for the public /webhook.

Reads no env directly; the secret is passed in (from WEBHOOK_SECRET).
"""

import hashlib
import hmac


def verify(signature_header: str | None, raw_body: bytes, secret: str) -> bool:
    """True iff `X-Hub-Signature-256` matches HMAC-SHA256 of the raw body.

    Verify against the RAW bytes (before any JSON parse) and compare in constant
    time so the signature can't be probed byte-by-byte.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + expected, signature_header)
