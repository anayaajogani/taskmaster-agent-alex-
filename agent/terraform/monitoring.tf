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
# Cloud Monitoring: log-based metric + alert policy + notification channel
#
# When the graph routes a task HIGH_PRIORITY, reminder_agent emits a
# structured JSON log with alert_type="task_reminder" (expense_agent/
# agent.py:emit_reminder_alert). Cloud Logging ingests it, a log-based
# metric counts it, and an alert policy emails the student. This is the
# second consequential action in the demo, alongside the calendar write.
# ---------------------------------------------------------------------------

resource "google_logging_metric" "task_reminders" {
  name    = "task-reminder-alerts"
  project = var.project_id

  description = "Counts high-priority task reminder alerts from the taskmaster agent."

  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.backend_service_name}"
    jsonPayload.alert_type="task_reminder"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "Taskmaster Agent - Reminder Alerts"
  project      = var.project_id
  type         = "email"

  labels = {
    email_address = var.notification_email
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "task_reminders" {
  display_name = "Taskmaster Agent - High-Priority Task Reminder"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Task reminder count > 0"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.task_reminders.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_COUNT"
      }
    }
  }

  notification_channels = [
    google_monitoring_notification_channel.email.id
  ]

  documentation {
    content   = <<-EOT
## High-Priority Task Reminder

A newly detected assignment scored high enough on urgency/grade-weight to
get both a calendar work block and a reminder.

### What happened

1. The agent estimated effort with Gemini and scored the task deterministically
2. It scheduled a work block on the "Taskmaster" Google Calendar
3. Because the score crossed the high-priority threshold, it also logged this alert

Check the "Taskmaster" Google Calendar for the new block, or Cloud Logging
(`jsonPayload.alert_type="task_reminder"`) for the full detail this alert
was generated from.

EOT
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}
