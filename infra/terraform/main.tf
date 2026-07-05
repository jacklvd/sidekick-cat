# Terraform mirror of the live Sidekick infra — built as a "destroy button", not
# as the source of truth. Everything here was originally created with gcloud; this
# config IMPORTS those resources into state so `terraform destroy` can remove them
# in one shot (the gcloud equivalent is the setup wizard's Teardown tab, Option B).
#
# Safety by design:
#   - `ignore_changes = all` on every resource: plan/apply only ever IMPORT, never
#     mutate the live service. So `apply` here is safe — it won't reconcile drift.
#   - secret VALUES are never managed (only the containers), so they never land in
#     terraform.tfstate. Deleting a container deletes its versions anyway.
#
# Usage (nothing is destroyed by these):
#   terraform init
#   terraform plan         # shows "N to import, 0 to add/change/destroy"
#   terraform apply        # performs the imports (no infra change)
# Then, only when you actually want it gone:
#   terraform destroy      # tears the whole bot down

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

variable "project" {
  type    = string
  default = "your-gcp-project-id"
}

variable "region" {
  type    = string
  default = "us-west1"
}

variable "runtime_sa" {
  type    = string
  default = "PROJECT_NUMBER-compute@developer.gserviceaccount.com"
}

# --- Cloud Run (the only thing that can cost money) ---------------------------
resource "google_cloud_run_v2_service" "sidekick_cat" {
  name     = "sidekick-cat"
  location = var.region

  template {
    containers {
      image = "placeholder" # ignored; the live image is set by gcloud --source deploys
    }
  }

  lifecycle { ignore_changes = all }
}

# --- Firestore (rate-limit counters / dedup) ---------------------------------
# dev-note: `terraform destroy` ABANDONS this one — the provider's deletion_policy
# defaults to ABANDON (state-only removal, DB left running), and setting it to
# DELETE here wouldn't stick because ignore_changes=all keeps the imported default.
# After destroy, finish Firestore with the wizard Teardown step 3 (gcloud databases delete).
resource "google_firestore_database" "default" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  lifecycle { ignore_changes = all }
}

# --- Secret containers (values NOT managed → never in tfstate) ----------------
# Explicit blocks (not for_each): TF 1.5 import blocks can't target instance keys.
resource "google_secret_manager_secret" "app_key" {
  secret_id = "APP_KEY"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = all
  }
}

resource "google_secret_manager_secret" "webhook_secret" {
  secret_id = "WEBHOOK_SECRET"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = all
  }
}

resource "google_secret_manager_secret" "groq_api_key" {
  secret_id = "GROQ_API_KEY"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = all
  }
}

resource "google_secret_manager_secret" "models_pat" {
  secret_id = "MODELS_PAT"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = all
  }
}

# --- Artifact Registry (source-deploy images) --------------------------------
resource "google_artifact_registry_repository" "source_deploy" {
  location      = var.region
  repository_id = "cloud-run-source-deploy"
  format        = "DOCKER"

  lifecycle { ignore_changes = all }
}

# --- IAM: runtime SA → Firestore. (secretAccessor was granted per-secret and
#     disappears when the secrets are deleted, so it isn't tracked here.) -------
resource "google_project_iam_member" "datastore_user" {
  project = var.project
  role    = "roles/datastore.user"
  member  = "serviceAccount:${var.runtime_sa}"

  lifecycle { ignore_changes = all }
}

# dev-note: NOT tracked here on purpose — billing budget (needs billing-account id;
# console-only delete), enabled APIs (disabling is the nuclear last step), the GCS
# source bucket `run-sources-<project>-<region>` (recreated by every --source
# deploy; deleting a non-empty bucket via TF needs force_destroy, which
# ignore_changes=all would strip), and the GitHub App / external LLM keys (live
# outside GCP). Those stay manual — see the setup wizard's Teardown tab.
