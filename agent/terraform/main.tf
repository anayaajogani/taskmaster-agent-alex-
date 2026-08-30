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
# Terraform configuration for the Taskmaster agent infrastructure.
#
# This provisions the GCP resources needed for the student Taskmaster
# agent: one Cloud Run service running the ADK graph, a Pub/Sub trigger
# (fed by the Canvas poller), Cloud Monitoring for the reminder alert, and
# IAM. No frontend/approval service — calendar writes are autonomous and
# reversible (a dedicated calendar the agent owns), so there's no
# human-in-the-loop step to gate here.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = {
    goog-terraform-provisioned = "true"
    app                        = "taskmaster-agent"
  }
}

locals {
  # Enable required GCP APIs.
  required_apis = [
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  project = var.project_id
  service = each.value

  disable_on_destroy = false
}
