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

variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run and related resources."
  type        = string
  default     = "us-east1"
}

variable "backend_service_name" {
  description = "Name of the backend Cloud Run service (ADK agent)."
  type        = string
  default     = "taskmaster-agent"
}

variable "agent_name" {
  description = "ADK agent name (matches the agent directory name)."
  type        = string
  default     = "taskmaster_agent"
}

variable "backend_image" {
  description = "Container image URI for the backend Cloud Run service."
  type        = string
}

variable "notification_email" {
  description = "Email address for task reminder alert notifications."
  type        = string

  validation {
    condition     = can(regex("^[^@]+@[^@]+$", var.notification_email))
    error_message = "A valid email address is required for notification_email."
  }
}

variable "gcal_token_secret" {
  description = <<-EOT
    Name of the Secret Manager secret holding the pre-minted Google Calendar
    OAuth token (JSON), created once locally per docs/setup_guide.md. Cloud
    Run has no browser for the interactive auth flow, so this is injected
    as an env var rather than a token file.
  EOT
  type        = string
  default     = "gcal-token"
}
