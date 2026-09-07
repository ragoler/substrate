# Shared helpers for setup_infra.sh / deploy_app.sh / verify_setup.sh.
# Sourced, not executed — callers set ROOT first:
#   ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "${ROOT}/lib.sh"
# shellcheck shell=bash

# Load .env from the repo root and derive the defaults every script needs.
load_env() {
  if [ -f "${ROOT}/.env" ]; then
    # shellcheck disable=SC1091
    source "${ROOT}/.env"
  else
    echo "Error: .env file not found. Create one with: cp .env.example .env"
    exit 1
  fi
  REGION="${REGION:-${ZONE%-*}}"
  NAMESPACE="${NAMESPACE:-default}"
}

# manifest_name <Kind> <file> — metadata.name of the first <Kind> in the file.
# The manifests are the source of truth for resource names, not .env.
manifest_name() {
  awk -v kind="$1" '$0 == "kind: " kind {f=1} f && /^  name:/ {print $2; exit}' "$2"
}

# Idempotent Artifact Registry repo creation (used by setup + deploy).
ensure_ar_repo() {
  gcloud artifacts repositories create "${ARTIFACT_REGISTRY_REPO}" \
    --repository-format=docker --location="${REGION}" \
    --description="Agent Hotel images" --project="${PROJECT_ID}" \
    || echo "Repo may already exist; continuing."
}

# curl args for the concierge's Basic auth — empty when auth is disabled.
# Use as: curl "${CURL_AUTH[@]+"${CURL_AUTH[@]}"}" ... (empty-array safe).
CURL_AUTH=()
init_curl_auth() {
  if [ -n "${HOTEL_PASSWORD:-}" ]; then
    CURL_AUTH=(-u "${HOTEL_USERNAME:-guest}:${HOTEL_PASSWORD}")
  fi
}
