#!/bin/sh
set -eu

git diff --check

for required_file in README.md docs/devlog.md; do
  if [ ! -s "$required_file" ]; then
    echo "missing or empty required file: $required_file" >&2
    exit 1
  fi
done

# The integration tests call the real Gemini effort-estimator agent (no LLM
# mock — see tests/test_integration.py's docstring), so GOOGLE_API_KEY (or
# Vertex AI application-default credentials) must be available. See
# docs/setup_guide.md.
uv run python -m pytest tests/ -q

tracked_secrets=$(git ls-files | grep -E '(^|/)(\.env|gcal_credentials\.json|gcal_token\.json|taskmaster_config\.json|[^/]+\.(pem|key|p12))$' | grep -vE '(^|/)\.env\.example$' || true)
if [ -n "$tracked_secrets" ]; then
  echo "possible secret files are tracked:" >&2
  echo "$tracked_secrets" >&2
  exit 1
fi

echo "repository checks passed"
