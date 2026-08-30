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

output "backend_url" {
  description = "URL of the backend Cloud Run service (ADK agent)."
  value       = google_cloud_run_v2_service.backend.uri
}

output "pubsub_topic" {
  description = "Pub/Sub topic for publishing assignment events."
  value       = google_pubsub_topic.assignment_events.id
}

output "dead_letter_topic" {
  description = "Dead-letter topic for failed assignment processing."
  value       = google_pubsub_topic.dead_letter.id
}

output "trigger_endpoint" {
  description = "Full trigger endpoint URL for Pub/Sub."
  value       = "${google_cloud_run_v2_service.backend.uri}/apps/${var.agent_name}/trigger/pubsub"
}

output "alert_policy" {
  description = "Cloud Monitoring alert policy for high-priority task reminders."
  value       = google_monitoring_alert_policy.task_reminders.display_name
}
