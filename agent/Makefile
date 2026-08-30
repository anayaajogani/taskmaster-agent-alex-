PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION ?= us-east1
BACKEND_SERVICE ?= taskmaster-agent
NOTIFICATION_EMAIL ?= your-email@example.com
GCAL_TOKEN_SECRET ?= gcal-token
REGISTRY = $(REGION)-docker.pkg.dev/$(PROJECT_ID)/taskmaster-agent
BACKEND_IMAGE = $(REGISTRY)/backend

# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------

install:
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Installing uv..."; curl -LsSf https://astral.sh/uv/0.6.12/install.sh | sh; source $$HOME/.local/bin/env; }
	uv sync

dev:
	uv run python expense_agent/fast_api_app.py

playground:
	uv run adk web --port 8501

test:
	uv run python -m pytest tests/ -xvs

check:
	./scripts/check.sh

lint:
	uv run codespell . && \
		uv run ruff check . --fix && \
		uv run ruff format . && \
		uv run mypy .

# ---------------------------------------------------------------------------
# Cloud deployment (one command)
#
#   make deploy NOTIFICATION_EMAIL=you@example.com
#
# Builds the container image, then deploys everything via Terraform:
# Cloud Run, Pub/Sub, IAM, Cloud Monitoring.
#
# Prerequisite: a Secret Manager secret named $(GCAL_TOKEN_SECRET) holding
# the pre-minted Calendar OAuth token JSON — see docs/setup_guide.md. Cloud
# Run has no browser for the interactive auth flow.
# ---------------------------------------------------------------------------

deploy:
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID is not set."; \
		echo "Run: gcloud config set project <your-project-id>"; \
		exit 1; \
	fi
	@if [ "$(NOTIFICATION_EMAIL)" = "your-email@example.com" ]; then \
		echo "Warning: Using default NOTIFICATION_EMAIL. Set your own:"; \
		echo "  make deploy NOTIFICATION_EMAIL=you@example.com"; \
		echo ""; \
	fi
	@echo "==> [1/4] Enabling required APIs..."
	gcloud services enable \
		artifactregistry.googleapis.com \
		cloudbuild.googleapis.com \
		--project=$(PROJECT_ID) --quiet
	@echo ""
	@echo "==> [2/4] Setting up Artifact Registry and Cloud Build permissions..."
	-gcloud artifacts repositories create taskmaster-agent \
		--repository-format=docker \
		--location=$(REGION) \
		--project=$(PROJECT_ID) 2>/dev/null
	@PROJECT_NUMBER=$$(gcloud projects describe $(PROJECT_ID) --format='value(projectNumber)') && \
		gcloud projects add-iam-policy-binding $(PROJECT_ID) \
			--member="serviceAccount:$${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
			--role="roles/artifactregistry.writer" \
			--quiet >/dev/null && \
		gcloud projects add-iam-policy-binding $(PROJECT_ID) \
			--member="serviceAccount:$${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
			--role="roles/storage.objectViewer" \
			--quiet >/dev/null
	@echo ""
	@echo "==> [3/4] Building the container image..."
	gcloud builds submit . \
		--tag $(BACKEND_IMAGE) \
		--project=$(PROJECT_ID) --quiet
	@echo ""
	@echo "==> [4/4] Deploying infrastructure (Cloud Run, Pub/Sub, IAM, monitoring)..."
	cd terraform && terraform init -input=false && terraform apply -auto-approve \
		-var=project_id=$(PROJECT_ID) \
		-var=region=$(REGION) \
		-var=backend_service_name=$(BACKEND_SERVICE) \
		-var=backend_image=$(BACKEND_IMAGE) \
		-var=notification_email=$(NOTIFICATION_EMAIL) \
		-var=gcal_token_secret=$(GCAL_TOKEN_SECRET)
	@echo ""
	@echo "==> Deployment complete!"
	@echo ""
	@cd terraform && \
		echo "  Backend:   $$(terraform output -raw backend_url)" && \
		echo "  Topic:     assignment-events" && \
		echo "  Alerts:    High-priority tasks -> $(NOTIFICATION_EMAIL)"

remote-test:
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID is not set."; \
		echo "Run: gcloud config set project <your-project-id>"; \
		exit 1; \
	fi
	@echo "Publishing a test assignment (high priority: due soon, worth 40% of grade)..."
	gcloud pubsub topics publish assignment-events \
		--project=$(PROJECT_ID) \
		--message='{"source":"canvas","source_ref":"make-test","title":"Midterm Project","course":"DATA C100","description":"A substantial project submission.","due_at":"2026-09-02T04:00:00Z","points_possible":40,"course_total_points":100}' \
		--attribute=source=make-test
	@echo ""
	@echo "Check Cloud Logging for results:"
	@echo "  gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=$(BACKEND_SERVICE)' --project=$(PROJECT_ID) --limit=5 --format='table(timestamp, textPayload)'"

clean:
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID is not set."; \
		echo "Run: gcloud config set project <your-project-id>"; \
		exit 1; \
	fi
	@echo "==> Tearing down all infrastructure..."
	cd terraform && terraform destroy -auto-approve \
		-var=project_id=$(PROJECT_ID) \
		-var=region=$(REGION) \
		-var=backend_service_name=$(BACKEND_SERVICE) \
		-var=backend_image=$(BACKEND_IMAGE) \
		-var=notification_email=$(NOTIFICATION_EMAIL) \
		-var=gcal_token_secret=$(GCAL_TOKEN_SECRET) \
	|| (echo "" && \
		echo "Retrying in 60s (GCP needs time to propagate alert policy deletion)..." && \
		sleep 60 && \
		cd terraform && terraform destroy -auto-approve \
			-var=project_id=$(PROJECT_ID) \
			-var=region=$(REGION) \
			-var=backend_service_name=$(BACKEND_SERVICE) \
			-var=backend_image=$(BACKEND_IMAGE) \
			-var=notification_email=$(NOTIFICATION_EMAIL) \
			-var=gcal_token_secret=$(GCAL_TOKEN_SECRET))
	@echo "Cleanup complete."
