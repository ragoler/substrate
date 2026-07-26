#!/usr/bin/env bash
# Standalone provisioning for the Agent Hotel: GKE cluster (Workload Identity +
# Gateway API) and the one-per-cluster Agent Substrate control plane. Run
# deploy_app.sh afterwards to build/push the image and deploy the hotel.
#
# Prereq-script toolchain: gcloud, kubectl, git, go, ko (https://ko.build),
# openssl — the ate operators are built from upstream source with `ko`.
set -e

# --- Load configuration ------------------------------------------------------
if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found. Create one with: cp .env.example .env"
  exit 1
fi

for cmd in gcloud kubectl python3 git go ko openssl; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "Error: $cmd is required but not installed."
    exit 1
  fi
done

REGION="${REGION:-${ZONE%-*}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Mode dispatch -------------------------------------------------------------
#   (no flag)         create cluster + install the ate plane
#   --delete          remove the ate plane (keep the cluster)
#   --delete-cluster  the above, plus delete the GKE cluster
MODE="create"
case "${1:-}" in
  --delete)         MODE="delete" ;;
  --delete-cluster) MODE="delete-cluster" ;;
  -h|--help)        echo "Usage: $0 [--delete | --delete-cluster]"; exit 0 ;;
  "")               MODE="create" ;;
  *) echo "Unknown argument: $1 (use --delete, --delete-cluster, or no flag)"; exit 1 ;;
esac

cluster_exists() {
  gcloud container clusters describe "${CLUSTER_NAME}" \
    --zone="${ZONE}" --project="${PROJECT_ID}" &>/dev/null
}

if [ "$MODE" = "delete" ] || [ "$MODE" = "delete-cluster" ]; then
  if cluster_exists; then
    gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"
    echo "=== Removing the ate control plane (namespace ate-system + CRDs) ==="
    kubectl delete namespace ate-system --ignore-not-found --timeout=180s || true
    kubectl delete crd workerpools.ate.dev actortemplates.ate.dev sandboxconfigs.ate.dev \
      --ignore-not-found || true
    kubectl delete -f "${ROOT}/cluster/compute-class.yaml" --ignore-not-found || true
  else
    echo "Cluster ${CLUSTER_NAME} does not exist; nothing to remove."
  fi
  if [ "$MODE" = "delete-cluster" ] && cluster_exists; then
    echo "=== Deleting GKE cluster ${CLUSTER_NAME} (several minutes) ==="
    gcloud container clusters delete "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" --quiet || true
  fi
  echo "=== Teardown complete (the snapshot bucket is left in place) ==="
  exit 0
fi

# --- Step 1: Create the GKE cluster -------------------------------------------
# Workload Identity is what makes the keyless-Vertex story work: guests and
# the concierge fetch ADC tokens from the metadata server, no API key anywhere.
#
# With USE_COMPUTE_CLASS=TRUE, Node Auto-Provisioning is enabled so the
# hotel-rooms ComputeClass (cluster/compute-class.yaml) can create node pools
# for the WorkerPool's rooms on demand.
USE_COMPUTE_CLASS="${USE_COMPUTE_CLASS:-FALSE}"
NAP_FLAGS=()
if [ "${USE_COMPUTE_CLASS}" = "TRUE" ]; then
  NAP_FLAGS=(--enable-autoprovisioning
             --min-cpu 0 --max-cpu "${MAX_CPU:-100}"
             --min-memory 0 --max-memory "${MAX_MEMORY:-400}")
fi

echo "=== Step 1: Creating GKE cluster ${CLUSTER_NAME} (${ZONE}) ==="
if cluster_exists; then
  echo "Cluster ${CLUSTER_NAME} already exists. Skipping creation."
  if [ "${USE_COMPUTE_CLASS}" = "TRUE" ]; then
    echo "Ensuring Node Auto-Provisioning is enabled (USE_COMPUTE_CLASS=TRUE)..."
    gcloud container clusters update "${CLUSTER_NAME}" \
      --project="${PROJECT_ID}" --zone="${ZONE}" "${NAP_FLAGS[@]}" \
      || echo "NAP update failed; continuing (it may already be enabled)."
  fi
else
  gcloud container clusters create "${CLUSTER_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --num-nodes="${NUM_NODES}" \
    --gateway-api=standard \
    --workload-pool="${PROJECT_ID}.svc.id.goog" \
    ${NAP_FLAGS[@]+"${NAP_FLAGS[@]}"}
fi

echo "=== Step 2: Getting cluster credentials ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}"

if [ "${USE_COMPUTE_CLASS}" = "TRUE" ]; then
  echo "=== Step 2b: Applying the hotel-rooms ComputeClass ==="
  kubectl apply -f "${ROOT}/cluster/compute-class.yaml"
fi

# --- Step 3: Artifact Registry (the prereq's ko build pushes here) -------------
echo "=== Step 3: Ensuring Artifact Registry repo ${ARTIFACT_REGISTRY_REPO} ==="
gcloud artifacts repositories create "${ARTIFACT_REGISTRY_REPO}" \
  --repository-format=docker --location="${REGION}" \
  --description="Agent Hotel images" --project="${PROJECT_ID}" \
  || echo "Repo may already exist; continuing."

# --- Step 4: The Agent Substrate control plane ----------------------------------
# One idempotent script: CRDs, ate-api-server + valkey + router + dns +
# controller + atelet (with static certs instead of alpha pod-certificate
# APIs), the snapshot bucket, Vertex IAM grants, and the ateom worker image.
echo "=== Step 4: Installing the Agent Substrate control plane (this builds from source; ~10 min) ==="
PROJECT_NAME="${PROJECT_ID}" \
CLUSTER="${CLUSTER_NAME}" \
CLUSTER_LOCATION="${ZONE}" \
REGION="${REGION}" \
APP_NAMESPACE="${NAMESPACE:-default}" \
ARTIFACT_REGISTRY_REPO="${ARTIFACT_REGISTRY_REPO}" \
  bash "${ROOT}/cluster/install-substrate-hotel-prereq.sh"

echo "=== Setup complete. Next: ./deploy_app.sh ==="
