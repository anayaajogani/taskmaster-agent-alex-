#!/bin/bash
# Deploy Taskmaster to Cloud Run.
#
# Run from the agent directory:  bash deploy/deploy.sh
#
# Assumes you're logged in (`gcloud auth login`) and have a billing account
# attached to the project.

set -e

PROJECT="taskmaster-507123"
REGION="us-west1"          # close to Berkeley
SERVICE="taskmaster"
BUCKET="${PROJECT}-state"

echo ""
echo "=================================================="
echo "  Deploying Taskmaster to Cloud Run"
echo "  project: $PROJECT   region: $REGION"
echo "=================================================="
echo ""

gcloud config set project "$PROJECT" --quiet

echo "--> enabling APIs (takes a minute the first time)"
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    storage.googleapis.com \
    --quiet

# --- state bucket ----------------------------------------------------------
# Cloud Run's filesystem is ephemeral. The agent's JSON state lives here so it
# survives restarts and scale-to-zero.
echo "--> creating state bucket (ignore an error if it already exists)"
gcloud storage buckets create "gs://${BUCKET}" \
    --location="$REGION" --uniform-bucket-level-access --quiet 2>/dev/null || true

# seed the bucket with existing config so the deployed agent knows your courses
if [ -f taskmaster_config.json ]; then
    echo "--> uploading your onboarding config"
    gcloud storage cp taskmaster_config.json "gs://${BUCKET}/" --quiet
fi
for f in course_sites.json syllabus_analysis.json manual_sources.json; do
    [ -f "$f" ] && gcloud storage cp "$f" "gs://${BUCKET}/" --quiet
done

# --- secrets ---------------------------------------------------------------
# Tokens never go in the image. Read from .env and store in Secret Manager.
echo "--> storing secrets"
if [ -f .env ]; then
    CANVAS_TOKEN=$(grep '^CANVAS_TOKEN=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")
    GOOGLE_API_KEY=$(grep '^GOOGLE_API_KEY=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")

    for pair in "canvas-token:$CANVAS_TOKEN" "gemini-key:$GOOGLE_API_KEY"; do
        name="${pair%%:*}"; value="${pair#*:}"
        [ -z "$value" ] && continue
        if gcloud secrets describe "$name" --quiet >/dev/null 2>&1; then
            printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --quiet
        else
            printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --quiet
        fi
    done
else
    echo "    WARNING: no .env found - the agent will start without credentials"
fi

# --- build + deploy --------------------------------------------------------
echo "--> copying deploy files into place"
cp deploy/Dockerfile .
cp deploy/requirements.txt .
cp deploy/.dockerignore .

echo "--> building and deploying (this takes 3-5 minutes)"
gcloud run deploy "$SERVICE" \
    --source . \
    --region "$REGION" \
    --allow-unauthenticated \
    --memory 1Gi \
    --cpu 1 \
    --timeout 3600 \
    --min-instances 0 \
    --max-instances 2 \
    --add-volume "name=state,type=cloud-storage,bucket=${BUCKET}" \
    --add-volume-mount "volume=state,mount-path=/app/state" \
    --set-env-vars "STATE_DIR=/app/state,STATIC_DIR=/app,CANVAS_BASE_URL=https://bcourses.berkeley.edu" \
    --set-secrets "CANVAS_TOKEN=canvas-token:latest,GOOGLE_API_KEY=gemini-key:latest" \
    --quiet

rm -f Dockerfile requirements.txt .dockerignore

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')

echo ""
echo "=================================================="
echo "  DEPLOYED"
echo "=================================================="
echo ""
echo "  $URL"
echo ""
echo "  Logs:   gcloud run services logs tail $SERVICE --region $REGION"
echo "  Delete: gcloud run services delete $SERVICE --region $REGION"
echo ""
