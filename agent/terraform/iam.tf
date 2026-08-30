# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ---------------------------------------------------------------------------
# IAM: Dedicated service accounts with least-privilege permissions.
# ---------------------------------------------------------------------------

# --- Backend service identity ---

resource "google_service_account" "backend" {
  account_id   = "taskmaster-agent-backend"
  display_name = "Taskmaster Agent - Backend"
  project      = var.project_id
}

# Grant it Vertex AI User so it can call Gemini.
resource "google_project_iam_member" "backend_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

# Grant it access to the pre-minted Calendar OAuth token (see cloud_run.tf).
resource "google_secret_manager_secret_iam_member" "backend_gcal_token" {
  project   = var.project_id
  secret_id = var.gcal_token_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.backend.email}"
}

# --- Pub/Sub → Backend ---

# Service account for Pub/Sub push to invoke the backend Cloud Run service.
resource "google_service_account" "pubsub_invoker" {
  account_id   = "taskmaster-agent-invoker"
  display_name = "Taskmaster Agent - Pub/Sub Invoker"
  project      = var.project_id
}

# Grant the invoker permission to call the backend Cloud Run service.
resource "google_cloud_run_v2_service_iam_member" "pubsub_invoker" {
  name     = google_cloud_run_v2_service.backend.name
  location = var.region
  project  = var.project_id
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_invoker.email}"
}

# Allow the GCP-managed Pub/Sub service agent to create OIDC tokens
# for authenticated push delivery.
data "google_project" "project" {
  project_id = var.project_id
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.pubsub_invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
