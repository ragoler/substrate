# Shared helpers for setup_infra.sh / deploy_app.sh / verify_setup.sh and the
# cluster installer. Sourced, not executed — callers set ROOT first:
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

# --- The pinned upstream release ------------------------------------------------
# Agent Substrate publishes release images to ghcr.io; the plane is installed
# from them (no source build), and the WorkerPool's worker image is the same
# release's ateworker-gvisor, pinned by digest. UPSTREAM_REF is the git tag the
# manifests and the installer (`go run ./cmd/ate-setup`) are taken from, and
# the tag the images carry. Single source of truth for every script.
UPSTREAM_REPO_URL="${UPSTREAM_REPO_URL:-https://github.com/agent-substrate/substrate.git}"
UPSTREAM_REF="${UPSTREAM_REF:-v1.0.0-rc.1}"
UPSTREAM_IMAGE_REPO="${UPSTREAM_IMAGE_REPO:-ghcr.io/agent-substrate/substrate}"
# Substrate's own names the hotel binds to.
ATE_SYSTEM_NS="ate-system"
ATESPACE="${ATESPACE:-hotel}"
# The snapshot transfer Google service account upstream's IAM model uses:
# ate-api-server impersonates it (token creator binding on the SA) and hands
# per-actor downscoped tokens to the data plane for snapshot uploads/downloads.
SNAPSHOT_TRANSFER_GSA_NAME="${SNAPSHOT_TRANSFER_GSA_NAME:-ate-snapshot-transfer}"
# Repo-local tool install dir (kubectl-ate from the pinned checkout).
TOOLS_DIR="${ROOT}/.tools"
KUBECTL_ATE="${TOOLS_DIR}/kubectl-ate"

# The installed release, read off the api-server image on the current cluster.
installed_substrate_release() {
  kubectl -n "${ATE_SYSTEM_NS}" get deployment ate-api-server \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="ate-api-server")].image}' 2>/dev/null \
    | sed -E 's/^[^:]+:([^@]+).*/\1/'
}

# ghcr_digest <image-name> — the manifest digest ghcr.io serves for
# ${UPSTREAM_IMAGE_REPO}/<image-name>:${UPSTREAM_REF} (anonymous pull token).
ghcr_digest() {
  python3 - "$1" "${UPSTREAM_IMAGE_REPO}" "${UPSTREAM_REF}" <<'PY'
import json, sys, urllib.request
image, repo, tag = sys.argv[1], sys.argv[2], sys.argv[3]
host, path = repo.split("/", 1)
scope = f"repository:{path}/{image}:pull"
tok = json.load(urllib.request.urlopen(f"https://{host}/token?scope={scope}"))["token"]
req = urllib.request.Request(
    f"https://{host}/v2/{path}/{image}/manifests/{tag}", method="HEAD",
    headers={"Authorization": f"Bearer {tok}",
             "Accept": ", ".join([
                 "application/vnd.oci.image.index.v1+json",
                 "application/vnd.docker.distribution.manifest.list.v2+json",
                 "application/vnd.oci.image.manifest.v1+json"])})
with urllib.request.urlopen(req) as r:
    print(r.headers["Docker-Content-Digest"])
PY
}

# kate — kubectl-ate from the pinned checkout (installed by the cluster
# installer into .tools/). Talks to ate-api-server through its own
# port-forward, as the ate-client ServiceAccount (the install's bootstrap owner).
kate() {
  [ -x "${KUBECTL_ATE}" ] || { echo "Error: ${KUBECTL_ATE} missing — run ./setup_infra.sh first" >&2; return 1; }
  "${KUBECTL_ATE}" "$@"
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
