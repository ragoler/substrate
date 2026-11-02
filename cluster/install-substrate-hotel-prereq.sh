#!/usr/bin/env bash
# One-time Agent Substrate control-plane prerequisite for the Agent Hotel
# (substrate-hotel) demo.
#
# Installs the plane from upstream's RELEASE: `go run ./cmd/ate-setup deploy
# ate-system` from a checkout of the pinned tag (lib.sh UPSTREAM_REF), with
# the images that release publishes to ghcr.io — nothing is built from source.
# That deploys the ate.dev CRDs (WorkerPool, SandboxConfig, CSIDriverConfig),
# the podcertificate-controller (Substrate's PKI: PodCertificates and
# ClusterTrustBundles for every component and client), ate-api-server with
# its PostgreSQL store, embedded OpenFGA authorization and ate-idp-server,
# atenet-router + dns, the atenet-egress gateway with the Kubernetes Secrets
# credential provider, ate-controller and the atelet DaemonSet. Component mTLS
# runs on the Kubernetes PodCertificate/ClusterTrustBundle APIs — GKE 1.37+
# serves them by default (setup_infra.sh creates such clusters).
#
# What remains ours, layered on top of the upstream install:
#   - the install document (storage location, identities the router admits,
#     the credential provider), rendered from cluster/substrate-install.yaml.tmpl
#   - GCP: the snapshot bucket, the snapshot-transfer service account and the
#     IAM bindings upstream's model needs — all RESOURCE-level (bucket, SA,
#     AR repo) so an org process that rewrites the project IAM policy cannot
#     wipe them
#   - the credential provider's namespace policy: the hotel atespace may
#     resolve Secrets in the hotel's namespace (the guests' Gemini key)
#   - a demo-sized PostgreSQL volume (upstream defaults to 500Gi)
#   - --parked-request-budget on the router (resumes can take tens of seconds)
#   - an atelet toleration for Custom-Compute-Class (NAP) room nodes
#   - kubectl-ate, built from the same checkout into .tools/
#
# Run once per cluster BEFORE deploy_app.sh (setup_infra.sh calls it for you).
# Idempotent: ate-setup converges; our extras are apply/patch/add-binding style.
set -euo pipefail

# --- Required vars (setup_infra.sh exports these from .env) --------------------
: "${PROJECT_NAME:?set PROJECT_NAME to your GCP project id}"
: "${CLUSTER:?set CLUSTER to your GKE cluster name}"
: "${REGION:?set REGION to your cluster region, e.g. us-central1}"
CLUSTER_LOCATION="${CLUSTER_LOCATION:-${REGION}}"
APP_NAMESPACE="${APP_NAMESPACE:-default}"
ARTIFACT_REGISTRY_REPO="${ARTIFACT_REGISTRY_REPO:-substrate-hotel}"
# PostgreSQL (the api-server's store) volume size; upstream sizes it for
# activation load (500Gi) — a demo hotel needs a fraction of that.
POSTGRES_STORAGE="${POSTGRES_STORAGE:-20Gi}"
SNAPSHOT_BUCKET="${SNAPSHOT_BUCKET:-${PROJECT_NAME}-substrate-snapshots}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/lib.sh"

WORK_DIR="${WORK_DIR:-/tmp/substrate-src}"
UPSTREAM_DIR="${UPSTREAM_DIR:-}"
# Install records (what ate-setup resolved; no secrets) land in the repo so
# an upgrade (upstream docs/guides/upgrade.md) starts from them.
export ATE_RECORD_DIR="${ATE_RECORD_DIR:-${ROOT}/.ate-setup}"

fail() { echo "FAIL [$1] $2" >&2; exit 1; }
note() { echo "==> $*"; }

# --- 0. preflight -------------------------------------------------------------
for tool in gcloud kubectl git go jq python3; do
  command -v "${tool}" >/dev/null || fail preflight "${tool} not on PATH (ate-setup runs with 'go run'; jq/python3 for our patches)"
done

note "[0/7] cluster credentials"
gcloud container clusters get-credentials "${CLUSTER}" \
  --location "${CLUSTER_LOCATION}" --project "${PROJECT_NAME}" --quiet \
  || fail preflight "get-credentials failed — does the cluster exist?"
KUBE_CONTEXT="$(kubectl config current-context)"

# Substrate's PKI runs on the PodCertificateRequest / ClusterTrustBundle APIs
# (v1 on Kubernetes 1.37+). Discovery can lag a minute after cluster creation.
for attempt in $(seq 1 12); do
  kubectl api-resources --api-group=certificates.k8s.io 2>/dev/null | grep -q "podcertificaterequests" && break
  [ "${attempt}" -lt 12 ] && { note "waiting for certificates.k8s.io PodCertificateRequests in API discovery (${attempt}/12)"; sleep 10; }
done
kubectl api-resources --api-group=certificates.k8s.io 2>/dev/null | grep -q "podcertificaterequests" \
  || fail preflight "PodCertificateRequests not served — the cluster needs Kubernetes 1.37+ (GKE rapid channel); setup_infra.sh creates one."

# --- 1. upstream checkout at the pinned release ----------------------------------
if [ -n "${UPSTREAM_DIR}" ]; then
  note "[1/7] using existing upstream checkout: ${UPSTREAM_DIR}"
  SRC="${UPSTREAM_DIR}"
else
  note "[1/7] cloning ${UPSTREAM_REPO_URL} @ ${UPSTREAM_REF} -> ${WORK_DIR}"
  rm -rf "${WORK_DIR}"
  git clone --quiet --depth 1 --branch "${UPSTREAM_REF}" "${UPSTREAM_REPO_URL}" "${WORK_DIR}"
  SRC="${WORK_DIR}"
fi
cd "${SRC}"
[ -d cmd/ate-setup ] || fail source "cmd/ate-setup not found — unexpected upstream layout"

# Demo-size the bundled PostgreSQL. The PVC (500Gi) and CPU request (2 cores)
# are sized for activation load; volumeClaimTemplates are immutable, so this
# has to happen in the manifest, before the first apply (upstream's own kind
# overlay does the same).
note "[1b/7] sizing PostgreSQL for a demo cluster (PVC ${POSTGRES_STORAGE}, 250m CPU request)"
python3 - "${POSTGRES_STORAGE}" <<'PY'
import re, sys, pathlib
p = pathlib.Path("manifests/ate-install/postgres/postgres.yaml")
s = p.read_text()
s, n1 = re.subn(r"storage: 500Gi\b", f"storage: {sys.argv[1]}", s)
s, n2 = re.subn(r'(requests:\n\s+cpu: )"2"', r"\g<1>250m", s, count=1)
if n1 != 1 or n2 != 1:
    sys.exit(f"postgres.yaml did not match (storage={n1}, cpu={n2}) — did upstream change it?")
p.write_text(s)
PY

# --- 2. GCP: bucket, snapshot-transfer SA, IAM (upstream's model) -------------------
# Snapshots: ate-api-server (Workload Identity principal of KSA
# ate-system/ate-api-server) copies/collects snapshot objects itself and mints
# per-actor downscoped tokens by impersonating the snapshot transfer GSA,
# which the data plane uses for uploads/downloads. Images: atelet pulls the
# guest image itself (it assembles the gVisor OCI bundle in userspace), so its
# WI principal needs read on the hotel's Artifact Registry repo. Every binding
# is on the RESOURCE (bucket / SA / repo): project-level policy may be
# rewritten by an org process (see .env.example), resource-level IAM is not.
note "[2/7] bucket gs://${SNAPSHOT_BUCKET}, transfer SA, IAM bindings"
if ! gcloud storage buckets describe "gs://${SNAPSHOT_BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${SNAPSHOT_BUCKET}" \
    --project "${PROJECT_NAME}" --location "${REGION}" --uniform-bucket-level-access
fi
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_NAME}" --format 'value(projectNumber)')"
WORKLOAD_POOL="$(gcloud container clusters describe "${CLUSTER}" --location "${CLUSTER_LOCATION}" \
  --project "${PROJECT_NAME}" --format 'value(workloadIdentityConfig.workloadPool)')"
[ -n "${WORKLOAD_POOL}" ] || fail iam "the cluster has no Workload Identity pool; setup_infra.sh creates clusters with one"
WI="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WORKLOAD_POOL}/subject/ns/${ATE_SYSTEM_NS}/sa"
TRANSFER_GSA="${SNAPSHOT_TRANSFER_GSA_NAME}@${PROJECT_NAME}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${TRANSFER_GSA}" --project "${PROJECT_NAME}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SNAPSHOT_TRANSFER_GSA_NAME}" --project "${PROJECT_NAME}" \
    --display-name "Substrate snapshot transfers (Agent Hotel)"
fi
gcloud iam service-accounts add-iam-policy-binding "${TRANSFER_GSA}" --project "${PROJECT_NAME}" \
  --member "${WI}/ate-api-server" --role roles/iam.serviceAccountTokenCreator >/dev/null
for ROLE in roles/storage.objectAdmin roles/storage.bucketViewer; do
  gcloud storage buckets add-iam-policy-binding "gs://${SNAPSHOT_BUCKET}" \
    --member "${WI}/ate-api-server" --role "${ROLE}" >/dev/null
  gcloud storage buckets add-iam-policy-binding "gs://${SNAPSHOT_BUCKET}" \
    --member "serviceAccount:${TRANSFER_GSA}" --role "${ROLE}" >/dev/null
done
gcloud artifacts repositories add-iam-policy-binding "${ARTIFACT_REGISTRY_REPO}" \
  --location "${REGION}" --project "${PROJECT_NAME}" \
  --member "${WI}/atelet" --role roles/artifactregistry.reader >/dev/null

# --- 3. the plane, from the release ---------------------------------------------------
# The install document is the operator artifact upstream recommends: every
# setting ate-setup resolves is listed in the run report, and the record it
# writes to ATE_RECORD_DIR is the input of the next upgrade.
CONCIERGE_SPIFFE="spiffe://cluster.local/ns/${APP_NAMESPACE}/sa/substrate-hotel-concierge"
KUBECTL_ATE_SPIFFE="spiffe://cluster.local/ns/${ATE_SYSTEM_NS}/sa/atenet-client"
INSTALL_CFG="$(mktemp -t substrate-install.XXXXXX.yaml)"
trap 'rm -f "${INSTALL_CFG}"' EXIT
UPSTREAM_IMAGE_REPO="${UPSTREAM_IMAGE_REPO}" UPSTREAM_REF="${UPSTREAM_REF}" \
SNAPSHOT_BUCKET="${SNAPSHOT_BUCKET}" SNAPSHOT_TRANSFER_GSA_NAME="${SNAPSHOT_TRANSFER_GSA_NAME}" \
PROJECT_NAME="${PROJECT_NAME}" CLUSTER="${CLUSTER}" CLUSTER_LOCATION="${CLUSTER_LOCATION}" \
KUBE_CONTEXT="${KUBE_CONTEXT}" \
  python3 -c "import os,sys; sys.stdout.write(os.path.expandvars(open(sys.argv[1]).read()))" \
  "${ROOT}/cluster/substrate-install.yaml.tmpl" > "${INSTALL_CFG}"
note "[3/7] go run ./cmd/ate-setup deploy ate-system (release ${UPSTREAM_REF}, prebuilt images; ~5 min)"
echo "    router admits: ${CONCIERGE_SPIFFE}, ${KUBECTL_ATE_SPIFFE}"
NO_DEV_ENV=1 go run ./cmd/ate-setup deploy ate-system \
  --config "${INSTALL_CFG}" \
  --credential-provider '{"name":"k8s.io"}' \
  --ingress-allowed-spiffe-ids "${CONCIERGE_SPIFFE},${KUBECTL_ATE_SPIFFE}" \
  --setup-csi=none \
  --rollout-timeout 10m

# --- 4. the hotel's plane tweaks -----------------------------------------------------------
# Router: a resume-on-request may restore a snapshot from GCS (~5 s warm, up to
# ~30 s cold, plus a cold image pull on a new node); the router parks the
# request meanwhile, bounded by --parked-request-budget (default 5 s). 90 s
# also rides out a full linger cycle when every room is busy.
ROUTER_IDX="$(kubectl -n "${ATE_SYSTEM_NS}" get deployment atenet-router -o json \
  | jq -r '.spec.template.spec.containers | to_entries[] | select(.value.name == "atenet-router") | .key')"
[ -n "${ROUTER_IDX}" ] || fail router "no 'atenet-router' container in deployment/atenet-router"
if ! kubectl -n "${ATE_SYSTEM_NS}" get deployment atenet-router -o json \
     | jq -e --argjson i "${ROUTER_IDX}" '.spec.template.spec.containers[$i].args // [] | any(startswith("--parked-request-budget="))' >/dev/null; then
  note "[4/7] setting --parked-request-budget=90s on atenet-router"
  kubectl -n "${ATE_SYSTEM_NS}" patch deployment atenet-router --type=json \
    -p "[{\"op\": \"add\", \"path\": \"/spec/template/spec/containers/${ROUTER_IDX}/args/-\", \"value\": \"--parked-request-budget=90s\"}]"
  kubectl -n "${ATE_SYSTEM_NS}" rollout status deployment/atenet-router --timeout=300s
fi

# atelet must run on every node a room can land on (suspend/resume call the
# node's atelet). NAP taints Custom-Compute-Class nodes; append a toleration
# (upstream's DaemonSet ships its own for its sandbox-class taint — never
# replace the list).
if ! kubectl -n "${ATE_SYSTEM_NS}" get ds atelet -o json \
     | jq -e '.spec.template.spec.tolerations // [] | any(.key == "cloud.google.com/compute-class")' >/dev/null; then
  note "[4/7] appending the compute-class toleration to daemonset/atelet"
  kubectl -n "${ATE_SYSTEM_NS}" patch daemonset atelet --type=json -p '[
    {"op": "add", "path": "/spec/template/spec/tolerations/-",
     "value": {"key": "cloud.google.com/compute-class", "operator": "Exists", "effect": "NoSchedule"}}
  ]'
  kubectl -n "${ATE_SYSTEM_NS}" rollout status daemonset/atelet --timeout=300s
fi

# --- 5. who may talk to the plane -----------------------------------------------------------
# ate-system denies ingress by default; a namespace labeled ate.dev/api-access
# may reach ate-api-server (the concierge's gRPC). The router's allowlist
# (above) is the other half: WHO may reach actors.
note "[5/7] admitting namespace ${APP_NAMESPACE} to ate-api-server"
kubectl get namespace "${APP_NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${APP_NAMESPACE}"
kubectl label namespace "${APP_NAMESPACE}" ate.dev/api-access=true --overwrite >/dev/null

# --- 6. credential injection: which Secrets the hotel atespace may resolve ---------------
# The egress gateway replaces the guests' `x-goog-api-key` header with the
# value of a Kubernetes Secret, read by the bundled credential provider. The
# provider is default-deny per atespace; grant the hotel atespace the hotel's
# namespace (deploy_app.sh creates the Secret there). The provider reads the
# policy at startup, so restart it when it changed.
note "[6/7] credential provider policy: atespace ${ATESPACE} -> namespace ${APP_NAMESPACE}"
DESIRED_POLICY="$(printf 'policies:\n- atespace: %s\n  allowedNamespaces:\n  - %s\n' "${ATESPACE}" "${APP_NAMESPACE}")"
CURRENT_POLICY="$(kubectl -n "${ATE_SYSTEM_NS}" get configmap k8s-credential-provider-namespace-policy \
  -o jsonpath='{.data.namespace-policy\.yaml}' 2>/dev/null || true)"
if [ "${CURRENT_POLICY}" != "${DESIRED_POLICY}" ]; then
  kubectl -n "${ATE_SYSTEM_NS}" create configmap k8s-credential-provider-namespace-policy \
    --from-literal=namespace-policy.yaml="${DESIRED_POLICY}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "${ATE_SYSTEM_NS}" rollout restart deployment/k8s-credential-provider
  kubectl -n "${ATE_SYSTEM_NS}" rollout status deployment/k8s-credential-provider --timeout=180s
fi

# --- 7. kubectl-ate from the same checkout ---------------------------------------------------
note "[7/7] installing kubectl-ate (${UPSTREAM_REF}) into ${TOOLS_DIR}"
mkdir -p "${TOOLS_DIR}"
GOBIN="${TOOLS_DIR}" go install ./cmd/kubectl-ate
"${KUBECTL_ATE}" --version || true

cat <<EOF

==> Done. Agent Substrate ${UPSTREAM_REF} is installed (release images from ${UPSTREAM_IMAGE_REPO}):
      - control plane:   deployment/ate-api-server (+ embedded OpenFGA authz, ate-idp-server), statefulset/postgres (${POSTGRES_STORAGE})
      - PKI:             podcertificate-controller (PodCertificates + ClusterTrustBundles for components AND clients)
      - data plane:      daemonset/atelet (compute-class toleration appended), ate-controller
      - networking:      atenet-router (mTLS ingress, admits the concierge + kubectl-ate; parked budget 90s),
                         atenet-egress (per-actor EgressPolicy, credential injection via k8s-credential-provider)
      - snapshots:       gs://${SNAPSHOT_BUCKET}/agent-hotel/ (api-server WI + ${TRANSFER_GSA})
      - images:          atelet WI may read AR repo ${ARTIFACT_REGISTRY_REPO}
      - authz:           bootstrap owner = ate-client (kubectl-ate); deploy_app.sh grants the concierge its roles
      - tools:           ${KUBECTL_ATE}
      - install record:  ${ATE_RECORD_DIR}/installs/ (input for the next upgrade)

    Now run ./deploy_app.sh to build and deploy the Agent Hotel.
EOF
