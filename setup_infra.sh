#!/usr/bin/env bash
# Standalone provisioning for the Agent Hotel: GKE cluster (Workload Identity,
# Dataplane V2, Gateway API, managed OpenTelemetry) and the one-per-cluster
# Agent Substrate control plane, installed from upstream's release images.
# Run deploy_app.sh afterwards to build/push the hotel image and deploy it.
#
# Toolchain: gcloud, kubectl, git, go (runs upstream's installer and builds
# kubectl-ate), jq, python3. No ko, no docker needed here.
set -euo pipefail

# --- Load configuration ------------------------------------------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${ROOT}/lib.sh"
load_env

for cmd in gcloud kubectl python3 git go jq; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "Error: $cmd is required but not installed."
    exit 1
  fi
done

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
    echo "=== Removing the ate control plane (namespaces + CRDs) ==="
    kubectl delete namespace ate-system --ignore-not-found --timeout=180s || true
    kubectl delete namespace podcertificate-controller-system --ignore-not-found --timeout=180s || true
    kubectl get crd -o name 2>/dev/null | grep '\.ate\.dev$' | xargs -r kubectl delete --ignore-not-found || true
    kubectl delete computeclass hotel-rooms --ignore-not-found || true
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
# Workload Identity: ate-api-server and atelet are IAM principals for the
# snapshot bucket and the image registry. Dataplane V2: Substrate's
# NetworkPolicies (default-deny ingress to ate-system, the credential provider
# reachable only from the egress gateway) need an enforcing CNI. Managed
# OpenTelemetry: Substrate exports its metrics and traces to the GKE-managed
# collector. Rapid channel: Kubernetes 1.37+ serves the PodCertificateRequest
# and ClusterTrustBundle APIs Substrate's PKI runs on.
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
    --release-channel=rapid \
    --enable-dataplane-v2 \
    --gateway-api=standard \
    --workload-pool="${PROJECT_ID}.svc.id.goog" \
    --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS \
    ${NAP_FLAGS[@]+"${NAP_FLAGS[@]}"}
fi

echo "=== Step 2: Getting cluster credentials ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}"
SERVER_MINOR="$(kubectl version -o json 2>/dev/null | python3 -c "import json,sys; v=json.load(sys.stdin)['serverVersion']; print(int(v['major']), int(''.join(c for c in v['minor'] if c.isdigit())))")"
if [ "$(echo "${SERVER_MINOR}" | awk '{print ($1 > 1 || ($1 == 1 && $2 >= 37)) ? 1 : 0}')" != "1" ]; then
  echo "Error: Kubernetes ${SERVER_MINOR// /.} — Agent Substrate ${UPSTREAM_REF} needs 1.37+ (PodCertificateRequest/ClusterTrustBundle v1)."
  echo "       Pick a newer version (rapid channel) or add the 1.36 beta-API flags yourself; see cluster/README.md."
  exit 1
fi

if [ "${USE_COMPUTE_CLASS}" = "TRUE" ]; then
  echo "=== Step 2b: Applying the hotel-rooms ComputeClass ==="
  kubectl apply -f "${ROOT}/cluster/compute-class.yaml"
fi

# --- Step 3: Artifact Registry (the hotel image; atelet pulls it) ---------------
echo "=== Step 3: Ensuring Artifact Registry repo ${ARTIFACT_REGISTRY_REPO} ==="
ensure_ar_repo

# --- Step 4: The Agent Substrate control plane ----------------------------------
echo "=== Step 4: Installing Agent Substrate ${UPSTREAM_REF} (release images; ~5 min) ==="
PROJECT_NAME="${PROJECT_ID}" \
CLUSTER="${CLUSTER_NAME}" \
CLUSTER_LOCATION="${ZONE}" \
REGION="${REGION}" \
APP_NAMESPACE="${NAMESPACE:-default}" \
ARTIFACT_REGISTRY_REPO="${ARTIFACT_REGISTRY_REPO}" \
POSTGRES_STORAGE="${POSTGRES_STORAGE:-20Gi}" \
SNAPSHOT_BUCKET="${SNAPSHOT_BUCKET:-${PROJECT_ID}-substrate-snapshots}" \
  bash "${ROOT}/cluster/install-substrate-hotel-prereq.sh"

echo "=== Setup complete. Next: ./deploy_app.sh ==="
