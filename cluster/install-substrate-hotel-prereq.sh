#!/usr/bin/env bash
# One-time agent-substrate FULL-plane prerequisite for the Agent Hotel
# (substrate-hotel) demo. Adapted from the showcase hub's
# install-substrate-overcommit-prereq.sh — both install the SAME shared plane
# into ate-system, idempotently, at the same pinned upstream SHA.
#
# The hotel needs create/suspend/resume + resume-on-request — the FULL ate
# plane: ate-api-server + valkey (session state), atenet-router + atenet-dns
# (resume-on-connect proxy), ate-controller + atelet (reconcile). Upstream's
# jwt install path assumes the alpha agent-identity data plane
# (PodCertificate / ClusterTrustBundle projected volumes, GKE alpha only).
# This script deploys the SAME plane on a plain GKE Standard cluster by
# minting static certs with openssl at install time and applying the
# static-certs kustomize overlay (in this directory), which swaps every alpha
# volume source for the minted Secrets.
#
# Run once per cluster BEFORE deploy_app.sh (setup_infra.sh calls it for you).
# The operator images are built from upstream source with `ko`.
#
# Idempotent-ish: cert mint + pool creation + envvars ConfigMap are
# only-if-absent; CRD/manifest applies are safe to re-run; ko builds re-push
# :latest. The valkey-cluster-init Job is deleted before re-apply (Jobs are
# immutable) and no-ops if the valkey cluster is already formed.
set -euo pipefail

# --- Required vars (setup_infra.sh exports these from .env) --------------------
: "${PROJECT_NAME:?set PROJECT_NAME to your GCP project id}"
: "${CLUSTER:?set CLUSTER to your GKE cluster name}"
: "${REGION:?set REGION to your cluster region, e.g. us-central1}"
# Zonal or regional cluster location for gcloud --location (defaults to REGION).
CLUSTER_LOCATION="${CLUSTER_LOCATION:-${REGION}}"
# Namespace the hotel deploys into — Vertex WI grants are scoped to its SAs.
APP_NAMESPACE="${APP_NAMESPACE:-default}"
ARTIFACT_REGISTRY_REPO="${ARTIFACT_REGISTRY_REPO:-substrate-hotel}"

# Upstream agent-substrate source. Cloned fresh into WORK_DIR unless UPSTREAM_DIR
# points at an existing checkout (e.g. a cached clone).
UPSTREAM_REPO_URL="${UPSTREAM_REPO_URL:-https://github.com/agent-substrate/substrate.git}"
WORK_DIR="${WORK_DIR:-/tmp/substrate-src}"
UPSTREAM_DIR="${UPSTREAM_DIR:-}"
# Pinned to the upstream tip from when these hooks were authored
# (2026-07-06). 2026-07-24 upstream (98902191) made mTLS between components mandatory, requiring the
# alpha pod-certificate data plane this hook deliberately does not install —
# atelet/ate-controller crash-loop on missing /run/*/trust-bundle.pem without it.
UPSTREAM_SHA="${UPSTREAM_SHA:-d8f562d208c246a907fe7b3da81dd0318c5959f8}"

# ko pushes every operator image here. MUST match the registry path the
# feature's WorkerPool ateomImage references:
#   ${REGION}-docker.pkg.dev/${PROJECT_NAME}/${ARTIFACT_REGISTRY_REPO}/ateom-gvisor:latest
export KO_DOCKER_REPO="${REGION}-docker.pkg.dev/${PROJECT_NAME}/${ARTIFACT_REGISTRY_REPO}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail() { echo "FAIL [$1] $2" >&2; exit 1; }
note() { echo "==> $*"; }

# --- 0. preflight -------------------------------------------------------------
command -v gcloud  >/dev/null || fail preflight "gcloud not on PATH"
command -v kubectl >/dev/null || fail preflight "kubectl not on PATH"
command -v ko      >/dev/null || fail preflight "ko not on PATH (https://ko.build)"
command -v git     >/dev/null || fail preflight "git not on PATH"
command -v go      >/dev/null || fail preflight "go not on PATH (kubectl-ate pool mint runs via 'go run')"
command -v openssl >/dev/null || fail preflight "openssl not on PATH (static cert mint)"
[ -f "${SCRIPT_DIR}/static-certs/kustomization.yaml" ] \
  || fail preflight "static-certs overlay not found next to this script"

note "[0/10] cluster credentials + Artifact Registry auth"
gcloud container clusters get-credentials "${CLUSTER}" \
  --location "${CLUSTER_LOCATION}" --project "${PROJECT_NAME}" --quiet \
  || fail preflight "get-credentials failed — does the cluster exist?"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet \
  || fail preflight "configure-docker for ${REGION}-docker.pkg.dev failed"

# --- 1. upstream source -------------------------------------------------------
if [ -n "${UPSTREAM_DIR}" ]; then
  note "[1/10] using existing upstream checkout: ${UPSTREAM_DIR}"
  SRC="${UPSTREAM_DIR}"
else
  note "[1/10] cloning ${UPSTREAM_REPO_URL} @ ${UPSTREAM_SHA} -> ${WORK_DIR}"
  rm -rf "${WORK_DIR}"
  git clone "${UPSTREAM_REPO_URL}" "${WORK_DIR}"
  git -C "${WORK_DIR}" checkout --quiet "${UPSTREAM_SHA}"
  SRC="${WORK_DIR}"
fi
cd "${SRC}"
[ -d manifests/ate-install/generated ] || fail source "manifests/ate-install/generated not found — unexpected upstream layout"

# --- 2. CRDs -------------------------------------------------------------------
note "[2/10] applying ate.dev CRDs"
ko apply -f manifests/ate-install/generated
kubectl wait --for=condition=Established --timeout=60s \
  crd/workerpools.ate.dev crd/actortemplates.ate.dev crd/sandboxconfigs.ate.dev

# --- 3. gVisor SandboxConfig + namespace ---------------------------------------
note "[3/10] applying SandboxConfig validation policy + default gVisor class + ate-system namespace"
kubectl apply -f manifests/ate-install/sandboxconfig-validation.yaml
kubectl apply -f manifests/ate-install/sandboxconfig-gvisor.yaml
kubectl apply -f manifests/ate-install/ate-system-namespace.yaml
until kubectl get namespace/ate-system -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Active; do
  sleep 1
done

# --- 4. static certs (replaces the alpha PodCertificate/ClusterTrustBundle plane)
# One ECDSA P-256 CA + one leaf whose SANs cover every serving identity in the
# plane (api-server, router, valkey cluster + per-pod names). The leaf carries
# BOTH serverAuth and clientAuth EKUs because the same credential-bundle doubles
# as the api-server's valkey client cert and the init job's client cert.
#
# KEY FORMAT IS LOAD-BEARING: cmd/ateapi's credbundle.Parse accepts only PKCS8
# ("PRIVATE KEY") blocks — `openssl genpkey` emits PKCS8; `openssl ecparam`
# would emit SEC1 ("EC PRIVATE KEY") and be rejected. Bundle order: key, leaf
# cert, CA cert — the exact PEM sequence credbundle.Parse expects.
if kubectl get secret servicedns-static-certs -n ate-system >/dev/null 2>&1; then
  note "[4/10] static-cert Secrets already present — skipping mint"
else
  note "[4/10] minting static CA + leaf and creating cert Secrets"
  CERT_DIR="$(mktemp -d)"
  trap 'rm -rf "${CERT_DIR}"' EXIT

  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
    -out "${CERT_DIR}/ca.key"
  openssl req -x509 -new -key "${CERT_DIR}/ca.key" -sha256 -days 3650 \
    -subj "/CN=ate-static-ca" -out "${CERT_DIR}/ca.crt"

  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
    -out "${CERT_DIR}/leaf.key"
  openssl req -new -key "${CERT_DIR}/leaf.key" \
    -subj "/CN=api.ate-system.svc" -out "${CERT_DIR}/leaf.csr"
  cat > "${CERT_DIR}/leaf.ext" <<'EXT'
subjectAltName=DNS:api.ate-system.svc,DNS:api.ate-system.svc.cluster.local,DNS:atenet-router.ate-system.svc,DNS:atenet-router.ate-system.svc.cluster.local,DNS:valkey-cluster.ate-system.svc,DNS:valkey-cluster.ate-system.svc.cluster.local,DNS:valkey-cluster-service.ate-system.svc,DNS:*.valkey-cluster-service.ate-system.svc,DNS:*.valkey-cluster-service.ate-system.svc.cluster.local
keyUsage=digitalSignature
extendedKeyUsage=serverAuth,clientAuth
EXT
  openssl x509 -req -in "${CERT_DIR}/leaf.csr" -CA "${CERT_DIR}/ca.crt" \
    -CAkey "${CERT_DIR}/ca.key" -CAcreateserial -sha256 -days 3650 \
    -extfile "${CERT_DIR}/leaf.ext" -out "${CERT_DIR}/leaf.crt"

  cat "${CERT_DIR}/leaf.key" "${CERT_DIR}/leaf.crt" "${CERT_DIR}/ca.crt" \
    > "${CERT_DIR}/credential-bundle.pem"

  # The 4 Secrets the static-certs overlay + base manifests reference:
  #   servicedns-static-certs  — key + leaf + CA bundle (every servicedns volume)
  #   servicedns-ca            — trust anchor for jwt client-auth mounts
  #   workerpool-ca-certs      — trust anchor the api-server verifies workers with
  #   valkey-ca-certs          — CA the api-server verifies valkey's cert with
  kubectl create secret generic servicedns-static-certs -n ate-system \
    --from-file=credential-bundle.pem="${CERT_DIR}/credential-bundle.pem"
  kubectl create secret generic servicedns-ca -n ate-system \
    --from-file=trust-bundle.pem="${CERT_DIR}/ca.crt"
  kubectl create secret generic workerpool-ca-certs -n ate-system \
    --from-file=trust-bundle.pem="${CERT_DIR}/ca.crt"
  kubectl create secret generic valkey-ca-certs -n ate-system \
    --from-file=ca.crt="${CERT_DIR}/ca.crt"
fi

# --- 5. session-id pools (jwt signing + CA pool Secrets) ------------------------
note "[5/10] ensuring session-id jwt + CA pool Secrets"
kubectl get secret session-id-jwt-pool -n ate-system >/dev/null 2>&1 \
  || go run ./cmd/kubectl-ate admin make-jwt-pool \
       --key-id="1" --name="session-id-jwt-pool" --secret-namespace=ate-system
kubectl get secret session-id-ca-pool -n ate-system >/dev/null 2>&1 \
  || go run ./cmd/kubectl-ate admin make-ca-pool \
       --ca-id="1" --name="session-id-ca-pool" --secret-namespace=ate-system

# --- 6. api-server env vars -----------------------------------------------------
note "[6/10] ensuring ate-api-server-envvars ConfigMap"
if ! kubectl get configmap ate-api-server-envvars -n ate-system >/dev/null 2>&1; then
  kubectl create configmap ate-api-server-envvars -n ate-system \
    --from-literal=ATE_API_REDIS_ADDRESS="valkey-cluster.ate-system.svc:6379" \
    --from-literal=ATE_API_REDIS_USE_IAM_AUTH="false" \
    --from-literal=ATE_API_REDIS_TLS_SERVER_NAME="valkey-cluster.ate-system.svc" \
    --from-literal=ATE_API_REDIS_CLIENT_CERT="/run/servicedns.podcert.ate.dev/credential-bundle.pem" \
    --from-literal=ATE_API_K8SJWT_ISSUER="https://container.googleapis.com/v1/projects/${PROJECT_NAME}/locations/${REGION}/clusters/${CLUSTER}"
fi

# --- 7. snapshot bucket ----------------------------------------------------------
# SuspendActor takes a FULL snapshot; the feature's ActorTemplate points
# snapshotsConfig.location at gs://${PROJECT_NAME}-substrate-snapshots/. The
# upload is performed by the atelet DaemonSet (KSA ate-system/atelet). On a
# cluster WITHOUT Workload Identity the atelet inherits the node service
# account, so the node SA gets objectAdmin. On a cluster WITH Workload
# Identity (GKE_METADATA node metadata — e.g. gke-showcase-validation) the
# atelet's GCS calls carry the WI principal for ns/ate-system/sa/atelet, NOT
# the node SA, so that principal needs the same grant or every suspend 403s
# on the snapshot upload and the actor wedges in SUSPENDING.
note "[7/10] ensuring snapshot bucket gs://${PROJECT_NAME}-substrate-snapshots"
if ! gcloud storage buckets describe "gs://${PROJECT_NAME}-substrate-snapshots" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${PROJECT_NAME}-substrate-snapshots" \
    --project "${PROJECT_NAME}" --location "${REGION}" \
    --uniform-bucket-level-access
fi
NODE_SA="$(gcloud container clusters describe "${CLUSTER}" --location "${CLUSTER_LOCATION}" \
  --project "${PROJECT_NAME}" --format 'value(nodeConfig.serviceAccount)')"
if [ -z "${NODE_SA}" ] || [ "${NODE_SA}" = "default" ]; then
  PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_NAME}" --format 'value(projectNumber)')"
  NODE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
gcloud storage buckets add-iam-policy-binding \
  "gs://${PROJECT_NAME}-substrate-snapshots" \
  --member "serviceAccount:${NODE_SA}" --role roles/storage.objectAdmin >/dev/null
WORKLOAD_POOL="$(gcloud container clusters describe "${CLUSTER}" --location "${CLUSTER_LOCATION}" \
  --project "${PROJECT_NAME}" --format 'value(workloadIdentityConfig.workloadPool)')"
if [ -n "${WORKLOAD_POOL}" ]; then
  PROJECT_NUMBER="${PROJECT_NUMBER:-$(gcloud projects describe "${PROJECT_NAME}" --format 'value(projectNumber)')}"
  gcloud storage buckets add-iam-policy-binding \
    "gs://${PROJECT_NAME}-substrate-snapshots" \
    --member "principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WORKLOAD_POOL}/subject/ns/ate-system/sa/atelet" \
    --role roles/storage.objectAdmin >/dev/null
fi

# --- 7b. Vertex AI access (keyless — the whole point) ---------------------------
# Guests call Gemini with ADC. Egress from a gVisor actor rides the worker
# pod's network namespace, so the identity the call carries is either the
# node SA (no Workload Identity) or the WI principal of the worker pod's KSA
# (`default` in APP_NAMESPACE). The concierge SA gets the same grant for the
# /llm proxy fallback. All grants are idempotent add-iam-policy-bindings.
note "[7b/10] granting roles/aiplatform.user for Vertex AI (ADC)"
gcloud projects add-iam-policy-binding "${PROJECT_NAME}" \
  --member "serviceAccount:${NODE_SA}" --role roles/aiplatform.user \
  --condition=None >/dev/null
if [ -n "${WORKLOAD_POOL}" ]; then
  for KSA in default substrate-hotel-concierge; do
    gcloud projects add-iam-policy-binding "${PROJECT_NAME}" \
      --member "principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WORKLOAD_POOL}/subject/ns/${APP_NAMESPACE}/sa/${KSA}" \
      --role roles/aiplatform.user --condition=None >/dev/null
  done
fi

# --- 8. full ate plane via the static-certs overlay -----------------------------
# The overlay's ../*.yaml refs resolve inside the upstream tree, so copy it in.
# The valkey-cluster-init Job is immutable — delete before re-apply; it no-ops
# when the valkey cluster is already formed.
note "[8/10] applying full ate plane (static-certs overlay)"
rm -rf manifests/ate-install/static-certs
cp -r "${SCRIPT_DIR}/static-certs" manifests/ate-install/static-certs
kubectl delete job valkey-cluster-init -n ate-system --ignore-not-found
kubectl kustomize manifests/ate-install/static-certs --load-restrictor LoadRestrictionsNone \
  | ko resolve -f - \
  | kubectl apply -f -

# --- 9. rollout waits ------------------------------------------------------------
note "[9/10] waiting for the plane to come up"
# The api-server Deployment is named 'ate-api-server' at the pinned upstream SHA
# (later upstream renamed it 'ate-api-server-deployment') — wait on whichever exists.
API_DEP="ate-api-server"
kubectl get deployment/ate-api-server-deployment -n ate-system >/dev/null 2>&1 && API_DEP="ate-api-server-deployment"
kubectl rollout status "deployment/${API_DEP}" -n ate-system --timeout=300s
kubectl rollout status deployment/ate-controller            -n ate-system --timeout=300s
kubectl rollout status deployment/atenet-router             -n ate-system --timeout=300s
kubectl rollout status deployment/dns                       -n ate-system --timeout=300s
kubectl rollout status statefulset/valkey-cluster           -n ate-system --timeout=300s
kubectl rollout status daemonset/atelet                     -n ate-system --timeout=300s
kubectl wait --for=condition=complete job/valkey-cluster-init -n ate-system --timeout=300s

# --- 10. ateom-gvisor worker image ------------------------------------------------
note "[10/10] building + pushing ateom-gvisor:latest -> ${KO_DOCKER_REPO}/ateom-gvisor"
ko build --base-import-paths --tags=latest --push ./cmd/ateom-gvisor

cat <<EOF

==> Done. FULL ate plane is installed (static-certs, no alpha APIs):
      - CRDs:            workerpools / actortemplates / sandboxconfigs (ate.dev)
      - api server:      deployment/ate-api-server-deployment (ate-system)
      - controller:      deployment/ate-controller            (ate-system)
      - router + dns:    deployment/atenet-router, deployment/dns
      - session store:   statefulset/valkey-cluster
      - node agent:      daemonset/atelet
      - certs:           static openssl-minted CA + leaf (Secrets in ate-system)
      - snapshots:       gs://${PROJECT_NAME}-substrate-snapshots (node SA + WI atelet objectAdmin)
      - vertex ai:       roles/aiplatform.user for node SA + ${APP_NAMESPACE} KSAs (keyless Gemini)
      - worker image:    ${KO_DOCKER_REPO}/ateom-gvisor:latest

    Now run ./deploy_app.sh to build and deploy the Agent Hotel.
EOF
