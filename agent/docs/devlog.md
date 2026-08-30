# Engineering devlog

Keep entries short and limited to meaningful or demo-worthy decisions.

## 2026-08-29 — Wire the ADK graph to a real Calendar write

- **Before:** The repo had two disconnected halves: an ADK/Gemini graph
  (`agent.py`) that routed tasks HIGH_PRIORITY/QUIET but only ever printed
  structured logs, and a separate local scheduler
  (`taskmaster_calendar.py`) that did real, capacity-aware Google Calendar
  writes with no Gemini or ADK involvement. Two bugs made this worse than
  it looked: `parse_task_event` never wrote to `ctx.state`, so every task
  reaching the scorer was `{}` ("Untitled task", due now); and
  `estimate_and_score` compared the priority score (typically 0–20) against
  a leftover expense-agent threshold of `100.0`, so `HIGH_PRIORITY` was
  unreachable.
- **Decision:** Fix both bugs, then give the graph one consequential
  action — `calendar_tool.schedule_block`, called from both routes —
  that reuses `taskmaster_calendar`'s auth/calendar/color logic instead of
  standing up a second scheduler (the old `gcal.py` wrote single blocks to
  the *primary* calendar; deleted, since two schedulers disagreeing is
  worse than one).
- **After:** A Pub/Sub-triggered task now flows: parse → real Gemini effort
  estimate → deterministic score → route → idempotent Calendar write (and a
  reminder alert on the high-priority path). Verified against real Gemini
  calls, not a mock.
- **Evidence:** `tests/test_integration.py` — 3 passing tests against a
  faked Calendar service and a real Gemini call: QUIET schedules a block,
  HIGH_PRIORITY schedules a block *and* emits `alert_type=task_reminder`,
  and reprocessing the same `source_ref` patches rather than duplicates.
- **Limitation:** Deployed Cloud Run verification (the actual `make deploy`
  + `make remote-test` path) hasn't run yet — the Calendar token secret and
  a real project are needed for that, and only local/faked runs are
  confirmed so far.

## 2026-08-29 — Retarget the deployment from expenses to tasks

- **Before:** `terraform/` still deployed the sample's two-service shape
  (an ADK backend plus an IAP-protected expense-approval frontend), and
  `monitoring.tf`'s alert filter (`alert_type="expense_review"`) didn't
  match what the code actually emits (`"task_reminder"`) — the reminder
  email could never have fired.
- **Decision:** Drop the frontend/IAP service entirely (this agent's
  Calendar writes are autonomous and reversible — a dedicated calendar it
  owns — so there's no approval step to gate), fix the monitoring filter,
  and rename Pub/Sub topics and service names from `expense-*` to
  match the actual product.
- **After:** One Cloud Run service, one Pub/Sub topic
  (`assignment-events`), a working reminder-alert path, and a
  `GCAL_TOKEN_JSON` Secret Manager injection since Cloud Run has no browser
  for the interactive OAuth flow the local scheduler uses.
- **Evidence:** `terraform/` files reviewed; not yet applied against a real
  project (see limitation above).
- **Limitation:** IAM/Terraform changes are unverified against a live
  `terraform plan`/`apply` — only read against the diff.

## 2026-08-29 — Consolidate onto one repo instead of two

- **Before:** A parallel repo (co-submitter, same hackathon) had cleaner
  scaffolding — CI, structured docs, Firestore-backed source ingestion with
  SSRF-hardened URL/PDF parsing, a React setup wizard — but no Calendar,
  Pub/Sub, Canvas connector, or Terraform, and did not run end to end. This
  repo had the opposite shape: everything above ran, but with weaker docs
  and no CI.
- **Decision:** Keep this repo as the base (it's the one with a working
  golden path), and port over only what's cheap and self-contained: this
  devlog template, the setup guide, and CI (below). Explicitly did not port
  the source-ingestion/Firestore stack — real value, but standing up
  Firestore/GCS is wall-clock risk two days before the deadline, and the
  calendar demo has to be recordable first.
- **After:** One repo, one story. The other repo's `taskmaster/` directory
  was itself a port *out of* this one — useful as independent confirmation
  of the two bugs fixed above, not as code to bring back in.
- **Evidence:** N/A — a documentation/process decision, not a code change.
- **Limitation:** Non-Canvas source ingestion (course websites, PDF-only
  syllabi) stays a real gap; `TASK_LIST.md`'s own "Check manually (not on
  Canvas)" section is the visible evidence of it. Revisit only if the
  calendar demo is solid with time to spare.

## Entry template

### YYYY-MM-DD — Change title

- **Before:**
- **Decision:**
- **After:**
- **Evidence:**
- **Limitation:**
