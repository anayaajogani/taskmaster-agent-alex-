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
# Cloud Run service — managed by Terraform.
#
# The Makefile builds the container image via Cloud Build, then Terraform
# creates the service with the correct env vars and service account.
#
# Single service: Pub/Sub pushes assignment events straight to the ADK
# graph, which calls Gemini for the effort estimate and writes the
# Calendar block itself. No approval UI to gate — see main.tf.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "backend" {
  name                = var.backend_service_name
  location            = var.region
  project             = var.project_id
  deletion_protection = false

  template {
    service_account = google_service_account.backend.email

    scaling {
      min_instance_count = 1
    }

    containers {
      image = var.backend_image

      resources {
        cpu_idle = false
      }

      # Pre-minted OAuth token for the Calendar write (see docs/setup_guide.md).
      # Cloud Run has no browser for the interactive auth flow, so the token
      # is minted once locally and injected here instead of a token file.
      env {
        name = "GCAL_TOKEN_JSON"
        value_source {
          secret_key_ref {
            secret  = var.gcal_token_secret
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}
