"""Mint + cache GitHub App installation tokens in Python — the Cloud Run analog of
the old `actions/create-github-app-token` step (no `gh` CLI in the container).

Env: APP_ID (numeric App ID, JWT issuer), APP_KEY (PEM private key, RS256).
"""

import os
import time

from github import Auth, GithubIntegration

# installation_id -> (token, expiry_epoch). Tokens live ~60 min; refresh a bit early.
_CACHE: dict[int, tuple[str, float]] = {}
_TTL = 55 * 60


def installation_token(installation_id: int) -> str:
    """An installation access token for `installation_id`, cached ~55 min per id."""
    cached = _CACHE.get(installation_id)
    if cached and cached[1] > time.time():
        return cached[0]
    auth = Auth.AppAuth(os.environ["APP_ID"], os.environ["APP_KEY"])
    token = GithubIntegration(auth=auth).get_access_token(installation_id).token
    _CACHE[installation_id] = (token, time.time() + _TTL)
    return token
