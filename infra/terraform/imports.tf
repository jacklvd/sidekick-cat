# Import blocks (Terraform 1.5+): they bind the existing gcloud-created resources
# to the definitions in main.tf. `terraform apply` runs the imports; with
# ignore_changes=all on every resource, that's the ONLY thing apply does.
# Delete this file after the import has been applied if you want it tidy.
#
# `id` must be a literal string (import blocks reject variable references), so
# replace "your-gcp-project-id" / "PROJECT_NUMBER" below with your own values —
# keep them in sync with var.project / var.runtime_sa in main.tf.

import {
  to = google_cloud_run_v2_service.sidekick_cat
  id = "projects/your-gcp-project-id/locations/us-west1/services/sidekick-cat"
}

import {
  to = google_firestore_database.default
  id = "projects/your-gcp-project-id/databases/(default)"
}

import {
  to = google_secret_manager_secret.app_key
  id = "projects/your-gcp-project-id/secrets/APP_KEY"
}

import {
  to = google_secret_manager_secret.webhook_secret
  id = "projects/your-gcp-project-id/secrets/WEBHOOK_SECRET"
}

import {
  to = google_secret_manager_secret.nvidia_api_key
  id = "projects/your-gcp-project-id/secrets/NVIDIA_API_KEY"
}

import {
  to = google_secret_manager_secret.groq_api_key
  id = "projects/your-gcp-project-id/secrets/GROQ_API_KEY"
}

import {
  to = google_secret_manager_secret.models_pat
  id = "projects/your-gcp-project-id/secrets/MODELS_PAT"
}

import {
  to = google_artifact_registry_repository.source_deploy
  id = "projects/your-gcp-project-id/locations/us-west1/repositories/cloud-run-source-deploy"
}

import {
  to = google_project_iam_member.datastore_user
  id = "your-gcp-project-id roles/datastore.user serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com"
}
