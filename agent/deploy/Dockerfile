# Taskmaster on Cloud Run.
#
# Cloud Run gives each container an ephemeral filesystem, so anything the agent
# writes (daily_view.json, task_list.json, course_sites.json) disappears when the
# instance restarts. STATE_DIR points at a mounted Cloud Storage bucket so state
# survives; see deploy.sh.

FROM python:3.12-slim

# curl is used by the container healthcheck; the rest keeps the image small.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so Docker caches this layer between code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY expense_agent/ ./expense_agent/
COPY index.html .

# Where the agent reads and writes its state. Overridden by the bucket mount.
ENV STATE_DIR=/app/state
RUN mkdir -p /app/state /app/syllabi

# Cloud Run injects PORT and expects the container to listen on it.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "-m", "expense_agent.run"]
