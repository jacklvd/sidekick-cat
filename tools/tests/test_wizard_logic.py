"""Offline self-check for tools/wizard_logic.py (no network, no streamlit).

Run: python -m tools.tests.test_wizard_logic
"""

from tools.wizard_logic import (
    GITHUB_APP_PERMISSIONS,
    GITHUB_APP_WEBHOOK_EVENTS,
    generate_webhook_secret,
    render_deploy_cmd,
    render_enable_apis_cmd,
    render_env_block,
    render_firestore_create_cmd,
    render_teardown_guide,
)


def test_enable_apis_cmd():
    assert render_enable_apis_cmd("my-proj") == (
        "gcloud config set project my-proj\n"
        "gcloud services enable run.googleapis.com firestore.googleapis.com "
        "secretmanager.googleapis.com cloudbuild.googleapis.com "
        "artifactregistry.googleapis.com logging.googleapis.com"
    )


def test_firestore_create_cmd():
    assert render_firestore_create_cmd("us-west1") == (
        "gcloud firestore databases create --location=us-west1 --type=firestore-native"
    )


def test_webhook_secret_is_random():
    secret = generate_webhook_secret()
    assert len(secret) == 64 and generate_webhook_secret() != secret  # random, not reused


def test_env_block():
    env = render_env_block({"APP_ID": "123", "GROQ_API_KEY": "g", "with_firestore": True})
    assert "APP_ID=123" in env and "APP_KEY=\n" in env and "LIMITS_BACKEND=firestore" in env
    assert "LIMITS_BACKEND" not in render_env_block({})  # no firestore -> no line at all


def test_deploy_cmd():
    cmd = render_deploy_cmd("my-proj", "us-west1", "42", with_firestore=True)
    assert "--project my-proj" in cmd and "--region us-west1" in cmd
    assert "APP_ID=42,LIMITS_BACKEND=firestore" in cmd
    assert "--set-secrets APP_KEY=APP_KEY:latest" in cmd
    no_fs_cmd = render_deploy_cmd("my-proj", "us-west1", "", with_firestore=False)
    assert "APP_ID=<app_id> \\" in no_fs_cmd and "LIMITS_BACKEND" not in no_fs_cmd


def test_teardown_guide():
    guide = render_teardown_guide("my-proj", "us-east1")
    # project + region interpolated into the delete commands, not left as placeholders
    assert "gcloud run services delete sidekick-cat --region us-east1" in guide
    assert "gs://run-sources-my-proj-us-east1" in guide
    assert "gcloud projects delete my-proj" in guide
    assert "<your-gcp-project>" not in guide
    # empty inputs fall back to safe placeholders (guide still renders)
    empty = render_teardown_guide()
    assert "<your-gcp-project>" in empty and "--region us-west1" in empty


def test_constants():
    assert len(GITHUB_APP_PERMISSIONS) == 4
    assert GITHUB_APP_WEBHOOK_EVENTS == ["Pull request", "Issue comment"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
