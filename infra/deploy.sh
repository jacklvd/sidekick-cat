#!/usr/bin/env bash
# Deploy sidekick-cat to Cloud Run from source. Gate: all offline suites green.
#
# Source-only on purpose: env vars, secrets, and scaling persist across
# revisions, so the running service keeps its config. A FIRST-TIME deploy
# needs the full command in README.md "Deploy" (env vars + --set-secrets).
#
# dev-note: infra/terraform is the destroy button (ignore_changes=all), not a
# deploy path — Terraform can't build the image and by design never mutates
# the live service. Deploys stay on gcloud --source.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m scripts.tests.test_pr_logic
uv run python -m scripts.tests.test_limits
uv run python -m scripts.tests.test_server

exec gcloud run deploy sidekick-cat \
  --project "${GCP_PROJECT:?set GCP_PROJECT to your gcloud project id}" \
  --region us-west1 \
  --source . \
  --quiet
