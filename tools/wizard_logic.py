"""Pure generator logic behind the setup wizard (tools/setup_wizard.py). No
network, no streamlit import — kept separate so the offline self-check in
tools/tests/test_wizard_logic.py doesn't need the optional streamlit dep
installed to run.

Env vars referenced here mirror .env.example; GitHub App permissions mirror
the actual API calls in scripts/gh.py and scripts/merge_pr.py.
"""

import secrets

GCP_APIS = [
    "run.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "logging.googleapis.com",
]

# (permission, level, why) — traced from scripts/gh.py + scripts/merge_pr.py,
# not guessed: e.g. squash-merge + branch delete need Contents write; PR/issue
# comments, labels, and the bot:context issue go through the Issues API.
GITHUB_APP_PERMISSIONS = [
    ("Contents", "Read and write", "reads files/tree/diffs; squash-merges and deletes the branch"),
    ("Issues", "Read and write", "PR comments, labels, assignees, and the bot:context cache issue"),
    ("Pull requests", "Read and write", "reads the diff, posts inline review comments, submits reviews"),
    ("Metadata", "Read-only", "mandatory default for every GitHub App"),
]

GITHUB_APP_WEBHOOK_EVENTS = ["Pull request", "Issue comment"]

_ENV_KEYS = ["APP_ID", "APP_KEY", "WEBHOOK_SECRET", "NVIDIA_API_KEY", "GROQ_API_KEY", "MODELS_PAT"]


def render_enable_apis_cmd(project: str) -> str:
    """`gcloud services enable ...` for every API the bot needs."""
    return (
        f"gcloud config set project {project}\n"
        f"gcloud services enable {' '.join(GCP_APIS)}"
    )


def render_firestore_create_cmd(region: str) -> str:
    """Only needed if you want LIMITS_BACKEND=firestore (shared caps/dedup across instances)."""
    return f"gcloud firestore databases create --location={region} --type=firestore-native"


def generate_webhook_secret() -> str:
    """A random secret for both the GitHub App's webhook field and WEBHOOK_SECRET."""
    return secrets.token_hex(32)


def render_env_block(values: dict) -> str:
    """.env-formatted text for the keys in _ENV_KEYS, blank if not supplied,
    plus LIMITS_BACKEND when firestore is requested."""
    lines = [f"{k}={values.get(k, '')}" for k in _ENV_KEYS]
    if values.get("with_firestore"):
        lines.append("LIMITS_BACKEND=firestore")
    return "\n".join(lines)


def render_deploy_cmd(project: str, region: str, app_id: str, with_firestore: bool) -> str:
    """The first-time `gcloud run deploy` command, filled in from wizard inputs."""
    env_vars = f"APP_ID={app_id or '<app_id>'}"
    if with_firestore:
        env_vars += ",LIMITS_BACKEND=firestore"
    secrets_flag = (
        "APP_KEY=APP_KEY:latest,WEBHOOK_SECRET=WEBHOOK_SECRET:latest,"
        "NVIDIA_API_KEY=NVIDIA_API_KEY:latest,"
        "GROQ_API_KEY=GROQ_API_KEY:latest,MODELS_PAT=MODELS_PAT:latest"
    )
    return (
        f"gcloud run deploy sidekick-cat --project {project} --source . --region {region} \\\n"
        "  --allow-unauthenticated --min-instances=0 --max-instances=3 --timeout=300 \\\n"
        "  --no-cpu-throttling \\\n"
        f"  --set-env-vars {env_vars} \\\n"
        f"  --set-secrets {secrets_flag}"
    )


def render_teardown_guide(project: str = "", region: str = "") -> str:
    """The full pause/delete runbook, with the GCP project id and region filled
    in from wizard inputs (placeholders kept where the wizard has no value)."""
    proj = project or "<your-gcp-project>"
    reg = region or "us-west1"
    return f"""\
## Take Sidekick offline

Either **pause** it (reversible, seconds) or **fully delete** every service it
spun up. The bot spans GitHub, Google Cloud, and three external LLM providers.

**No surprise bills either way.** The LLM providers are free-tier (no card → they
can only `429`). The only thing that can cost money is Cloud Run, and it scales to
zero (`--min-instances=0`), so an idle/paused bot already costs ~$0. Full deletion
is about tidiness and revoking credentials, not stopping a bill.

### What exists

| Service | Identifier |
|---|---|
| GitHub App | **sidekick-cat** — webhook active, installed on all repos |
| Cloud Run service | `sidekick-cat` · project `{proj}` · region `{reg}` |
| Firestore | native DB `(default)` · `{reg}` · collections `llm_counts`, `deliveries`, `reviewed_shas` |
| Secret Manager | `APP_KEY`, `WEBHOOK_SECRET`, `NVIDIA_API_KEY`, `GROQ_API_KEY`, `MODELS_PAT` |
| Artifact Registry | `cloud-run-source-deploy` |
| Cloud Storage | `run-sources-{proj}-{reg}` (source tarballs from `--source` deploys) |
| Runtime service account | `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com` (roles: `secretmanager.secretAccessor`, `datastore.user`) |
| Billing budget | ~$5/mo with alerts |
| External accounts | NVIDIA build.nvidia.com (`NVIDIA_API_KEY`), Groq (`GROQ_API_KEY`), GitHub fine-grained PAT (`MODELS_PAT`, `models` scope) |

### Option A — Pause (reversible, recommended first)

1. **Turn off the webhook** (cleanest): GitHub → App **sidekick-cat** → settings →
   **Webhook → Active: OFF**. The service stays deployed but idle (scales to zero → ~$0).
2. **Or hard-stop compute:** `gcloud run services update sidekick-cat --region {reg} --max-instances=0`
   (restore with `--max-instances=3`). Prefer option 1 so GitHub isn't logging delivery failures.

### Option B — Full teardown (delete everything)

Do GitHub first (stop the source of events), then GCP, then revoke external keys.
Nothing here is recoverable — confirm as you go.

**Terraform alternative:** `infra/terraform/` mirrors the GCP half as a one-button
`terraform destroy`. Import the gcloud-made resources once first
(`terraform plan` → `terraform apply`, import-only), then `terraform destroy` replaces
GCP steps 2 and 4–6. Not step 3 (the provider ABANDONs Firestore on destroy) — run the
`databases delete` yourself. GitHub (1), source bucket (5), budget, APIs, and external
keys stay manual.

```bash
# 1. GitHub — stop the leftover manual smoke workflows, then uninstall/delete the App.
gh workflow disable smoke-bot.yml smoke-models.yml --repo <you>/sidekick-cat
#    App sidekick-cat → Install App → uninstall from every account/org.
#    Optionally delete the App (Advanced → Delete) to invalidate APP_ID/APP_KEY/WEBHOOK_SECRET.

# 2. Cloud Run (stops all compute)
gcloud run services delete sidekick-cat --region {reg} --quiet

# 3. Firestore (data + database)
gcloud firestore databases delete --database='(default)' --quiet
#    If unavailable, delete collections llm_counts/deliveries/reviewed_shas from the console.

# 4. Secret Manager
for s in APP_KEY WEBHOOK_SECRET NVIDIA_API_KEY GROQ_API_KEY MODELS_PAT; do gcloud secrets delete "$s" --quiet; done

# 5. Artifact Registry + source bucket
gcloud artifacts repositories delete cloud-run-source-deploy --location={reg} --quiet
gcloud storage rm -r gs://run-sources-{proj}-{reg} --quiet

# 6. IAM — revoke the runtime SA's datastore role (secretAccessor dies with the secrets)
SA=<PROJECT_NUMBER>-compute@developer.gserviceaccount.com
gcloud projects remove-iam-policy-binding {proj} \\
  --member="serviceAccount:$SA" --role="roles/datastore.user" --quiet

# 8. (Optional, LAST) disable APIs — forces dependent resources off
for api in run firestore secretmanager cloudbuild artifactregistry; do
  gcloud services disable "$api.googleapis.com" --force --quiet
done

# 10. (Nuclear) delete the whole project
gcloud projects delete {proj}
```

**7. Billing budget:** Cloud Billing → Budgets & alerts → delete the ~$5/mo budget (console only).

**9. Revoke external credentials:** build.nvidia.com → API keys → revoke the
`nvapi-...` key behind `NVIDIA_API_KEY`; Groq console → revoke the `GROQ_API_KEY`;
GitHub → Fine-grained tokens → revoke the `models`-scoped `MODELS_PAT`.

### Verify it's gone

```bash
gcloud run services list --region {reg}   # no sidekick-cat
gcloud secrets list                        # empty
curl -s https://<your-cloud-run-url>/health   # connection refused / 404
```

Then confirm **$0** activity on the Cloud Billing dashboard after a day.
"""
