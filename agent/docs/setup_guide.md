# Taskmaster credential and cloud setup

This guide prepares one bCourses account and one personal Google account to
run Taskmaster locally and deploy it to Cloud Run. Complete it on the
machine where you'll run or deploy the agent.

Do **not** paste tokens, OAuth client JSON, or secret values into GitHub,
screenshots, logs, or chat. The application reads secrets from your local
environment or Secret Manager; nobody else needs to see the values.

## What this creates

- One bCourses personal access token (read-only Canvas API calls).
- One Google Cloud project with billing enabled and Vertex AI, Pub/Sub,
  Cloud Run, Cloud Monitoring, Secret Manager, Artifact Registry, and Cloud
  Build APIs.
- One OAuth desktop client + a one-time Calendar authorization, minted
  locally (Cloud Run has no browser for the interactive flow) and stored as
  a Secret Manager secret.

Gmail access is never requested. Calendar access is scoped to
`https://www.googleapis.com/auth/calendar` on a calendar the agent creates
and owns itself ("Taskmaster") — it never reads or writes your other
calendars.

## 1. Create the Canvas token

1. Sign in to [bCourses](https://bcourses.berkeley.edu).
2. Open **Account → Settings**.
3. Under **Approved Integrations**, select **New Access Token**.
4. Purpose: `Taskmaster`. Pick an expiry after the hackathon.
5. Create the token and copy it immediately — Canvas shows it once.

Taskmaster's own Canvas usage is limited to read-only `GET` calls: active
courses, assignments (with points), and syllabus body/files. It never
submits work or modifies Canvas.

Locally, put it in `.env` (already gitignored):

```sh
echo "CANVAS_TOKEN=your-token-here" >> .env
```

For Cloud Run, store it in Secret Manager instead (see step 5) rather than
baking it into the container.

## 2. Install and authenticate the Google Cloud CLI

```sh
gcloud auth login
gcloud auth application-default login
```

## 3. Create/select the Google Cloud project

```sh
export TASKMASTER_PROJECT_ID="replace-with-a-unique-project-id"
gcloud config set project "$TASKMASTER_PROJECT_ID"
```

Link a billing account in the
[Cloud Billing console](https://console.cloud.google.com/billing/linkedaccount),
then:

```sh
gcloud auth application-default set-quota-project "$TASKMASTER_PROJECT_ID"
```

The APIs below are also enabled automatically by `make deploy`
(`terraform/main.tf`), but enabling them here lets you verify access first:

```sh
gcloud services enable \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  monitoring.googleapis.com \
  pubsub.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com
```

## 4. Create the OAuth desktop client for Calendar

1. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview)
   for this project.
2. **Branding**: app name `Taskmaster`, your email for support/contact.
3. **Audience**: External, publishing status **Testing**, add your own
   email as a test user.
4. **Data access**: add only
   `https://www.googleapis.com/auth/calendar`.
5. **Clients → Create client → Desktop app**, name it `Taskmaster local`.
6. Download the client JSON as `gcal_credentials.json` in the `agent/`
   directory (already gitignored — never commit it).

## 5. Mint the Calendar token once, locally

```sh
uv run python -m expense_agent.taskmaster_calendar
```

This opens a browser for one-time consent, then writes `gcal_token.json`
next to `gcal_credentials.json` (also gitignored). Local runs and
`make dev` use this file automatically.

## 6. Put the token in Secret Manager for Cloud Run

Cloud Run has no browser for the interactive flow, so the pre-minted token
is injected as an env var instead of a file
(`taskmaster_calendar.py:_get_service` checks `GCAL_TOKEN_JSON` first):

```sh
gcloud secrets create gcal-token \
  --data-file=gcal_token.json \
  --replication-policy=automatic
```

If it already exists, add a new version instead of recreating it:

```sh
gcloud secrets versions add gcal-token --data-file=gcal_token.json
```

`terraform/iam.tf` grants the backend service account
`roles/secretmanager.secretAccessor` on this secret automatically.

## 7. Deploy

```sh
make deploy NOTIFICATION_EMAIL=you@example.com
```

Builds the container, applies Terraform (Cloud Run, Pub/Sub with
dead-letter, IAM, Cloud Monitoring), and prints the backend URL and topic
name. Then:

```sh
make remote-test
```

publishes one sample assignment to the deployed agent — check Cloud
Logging (command printed by `make remote-test`) for the routing decision,
and the "Taskmaster" Google Calendar for the new work block.

## 8. Verify readiness

```sh
gcloud auth list
gcloud config get-value project
gcloud services list --enabled \
  --filter='name:(aiplatform.googleapis.com pubsub.googleapis.com run.googleapis.com secretmanager.googleapis.com)'
gcloud secrets describe gcal-token
```

Then confirm in Google Auth Platform that the app is still **External /
Testing** with your email as the only test user — OAuth testing-mode
refresh tokens can expire after 7 days, so re-run step 5 if Calendar writes
start failing with an auth error.
