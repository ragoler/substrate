#!/usr/bin/env bash
# One-time agent-substrate FULL-plane prerequisite for the Agent Hotel
# (substrate-hotel) demo.
#
# Since the 2026-08-02 upgrade (upstream 3ed6aa07) this wraps UPSTREAM'S OWN
# installer: `hack/install-ate.sh --deploy-ate-system` deploys the CRDs, the
# podcertificate-controller (upstream's PodCertificate signer), the api
# server + valkey, router + dns, controller and atelet — using the
# Kubernetes 1.36+ PodCertificate/ClusterTrustBundle beta APIs for all
# component mTLS. The static-certs overlay this script used to carry is gone.
#
# What remains ours, layered on top of the upstream install:
#   - a `servicedns-ca` Secret shim in ate-system (the concierge verifies the
#     api-server's TLS against it; the root now comes from the
#     podcertificate controller's service-dns CA pool)
#   - an atelet toleration for Custom-Compute-Class (NAP) nodes — upstream's
#     DaemonSet has none, and rooms on a tainted node are unusable without a
#     local atelet (every ResumeActor fails with 'found 0 atelet pods')
#   - the snapshot bucket + IAM, Vertex IAM, optional WI impersonation
#   - the ateom-gvisor worker image build/push
#
# Cluster requirement: K8s 1.36+ with the beta cert APIs enabled —
#   gcloud container clusters create/update <cluster> \
#     --enable-kubernetes-unstable-apis=certificates.k8s.io/v1beta1/podcertificaterequests,certificates.k8s.io/v1beta1/clustertrustbundles
# (setup_infra.sh creates clusters this way.)
#
# Run once per cluster BEFORE deploy_app.sh (setup_infra.sh calls it for you).
# Idempotent: the upstream installer applies manifests; our extras are
# apply/patch/add-iam-policy-binding style.
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
# Verified by the 2026-08-02 upgrade spike (see cluster/README.md). Bumping
# further: regenerate ateapipb/ if the proto changed, and re-run the spike's
# verify flow on a test cluster first.
UPSTREAM_SHA="${UPSTREAM_SHA:-3ed6aa07e1676e178ed4f31d23d5484e9fc5e0e0}"

# ko pushes every operator image here. MUST match the registry path the
# feature's WorkerPool ateomImage references:
#   ${REGION}-docker.pkg.dev/${PROJECT_NAME}/${ARTIFACT_REGISTRY_REPO}/ateom-gvisor:latest
export KO_DOCKER_REPO="${REGION}-docker.pkg.dev/${PROJECT_NAME}/${ARTIFACT_REGISTRY_REPO}"
export KO_DEFAULTPLATFORMS="${KO_DEFAULTPLATFORMS:-linux/amd64}"

fail() { echo "FAIL [$1] $2" >&2; exit 1; }
note() { echo "==> $*"; }

# --- 0. preflight -------------------------------------------------------------
command -v gcloud  >/dev/null || fail preflight "gcloud not on PATH"
command -v kubectl >/dev/null || fail preflight "kubectl not on PATH"
command -v ko      >/dev/null || fail preflight "ko not on PATH (https://ko.build)"
command -v git     >/dev/null || fail preflight "git not on PATH"
command -v go      >/dev/null || fail preflight "go not on PATH (upstream installer runs kubectl-ate via 'go run')"
command -v openssl >/dev/null || fail preflight "openssl not on PATH (CA root extraction)"

note "[0/8] cluster credentials + Artifact Registry auth"
gcloud container clusters get-credentials "${CLUSTER}" \
  --location "${CLUSTER_LOCATION}" --project "${PROJECT_NAME}" --quiet \
  || fail preflight "get-credentials failed — does the cluster exist?"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet \
  || fail preflight "configure-docker for ${REGION}-docker.pkg.dev failed"

# The upstream plane needs the beta PodCertificate data plane.
if ! kubectl api-versions | grep -q '^certificates.k8s.io/v1beta1$'; then
  fail preflight "certificates.k8s.io/v1beta1 not served — the cluster needs K8s 1.36+ with:
  gcloud container clusters update ${CLUSTER} --location ${CLUSTER_LOCATION} --project ${PROJECT_NAME} \\
    --enable-kubernetes-unstable-apis=certificates.k8s.io/v1beta1/podcertificaterequests,certificates.k8s.io/v1beta1/clustertrustbundles"
fi

# --- 1. upstream source -------------------------------------------------------
if [ -n "${UPSTREAM_DIR}" ]; then
  note "[1/8] using existing upstream checkout: ${UPSTREAM_DIR}"
  SRC="${UPSTREAM_DIR}"
else
  note "[1/8] cloning ${UPSTREAM_REPO_URL} @ ${UPSTREAM_SHA} -> ${WORK_DIR}"
  rm -rf "${WORK_DIR}"
  git clone "${UPSTREAM_REPO_URL}" "${WORK_DIR}"
  git -C "${WORK_DIR}" checkout --quiet "${UPSTREAM_SHA}"
  SRC="${WORK_DIR}"
fi
cd "${SRC}"
[ -f hack/install-ate.sh ] || fail source "hack/install-ate.sh not found — unexpected upstream layout"

# --- 1b. local patch to the pinned source ---------------------------------------
# The router's Envoy route for actor traffic carries a HARDCODED 10s upstream
# timeout at this SHA (cmd/atenet/internal/router/xds.go). A guest's reply can
# legitimately take longer — the first Gemini call after a resume is client
# init + generate (~15s) — and the whole demo narrative is slow wakes. Raise
# it to 120s (matches the concierge's CHAT_TIMEOUT_S). Worth an upstream
# issue: this should be a flag like --actor-route-timeout.
note "[1b/8] patching actor-route timeout 10s -> 120s in the router source"
sed -i.bak 's/Timeout: durationpb.New(10 \* time.Second)/Timeout: durationpb.New(120 * time.Second)/' \
  cmd/atenet/internal/router/xds.go
grep -q 'durationpb.New(120 \* time.Second)' cmd/atenet/internal/router/xds.go \
  || fail patch "route-timeout patch did not apply — did upstream change xds.go?"

# --- 2. the full ate plane, via upstream's installer ----------------------------
# Deploys: CRDs, sandboxconfigs, podcertificate-controller + CA pools,
# ate-api-server (with the K8SJWT issuer derived from PROJECT_ID/CLUSTER_*),
# valkey, atenet-router + dns, ate-controller, atelet — and waits for rollout.
note "[2/8] running upstream hack/install-ate.sh --deploy-ate-system (~10 min: ko builds from source)"
NO_DEV_ENV=1 \
PROJECT_ID="${PROJECT_NAME}" \
CLUSTER_NAME="${CLUSTER}" \
CLUSTER_LOCATION="${CLUSTER_LOCATION}" \
  bash hack/install-ate.sh --deploy-ate-system

# --- 2b. resume budget ------------------------------------------------------------
# The router bounds a resume-on-request by --parked-request-budget (default
# 5s) — but restoring a checkpoint from GCS takes ~5s WARM (OCI unpack alone
# is ~4s for the guest image) and up to ~30s cold, so default-budget resumes
# get context-canceled mid-restore and chats surface 502/503. 90s rides out
# cold restores, image-unpack contention, and a full linger cycle when every
# room is busy; the concierge's own chat timeout (120s) still caps the wait.
if ! kubectl -n ate-system get deployment atenet-router \
     -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q "parked-request-budget"; then
  note "[2b/8] setting --parked-request-budget=90s on atenet-router"
  kubectl -n ate-system patch deployment atenet-router --type=json -p '[
    {"op": "add", "path": "/spec/template/spec/containers/0/args/-",
     "value": "--parked-request-budget=90s"}
  ]'
  kubectl -n ate-system rollout status deployment/atenet-router --timeout=180s
fi

# --- 3. servicedns-ca shim ------------------------------------------------------
# The concierge verifies api.ate-system.svc's TLS against Secret
# ate-system/servicedns-ca (name-scoped RBAC in infra/00-rbac.yaml). The
# serving certs now come from the podcertificate controller's service-dns CA
# pool — mirror that root into the Secret the client expects.
note "[3/8] mirroring the service-dns CA root into Secret ate-system/servicedns-ca"
CERT_DIR="$(mktemp -d)"
trap 'rm -rf "${CERT_DIR}"' EXIT
kubectl get secret -n podcertificate-controller-system service-dns-ca-pool \
  -o jsonpath='{.data.pool}' | base64 --decode > "${CERT_DIR}/pool.json"
grep -o '"RootCertificateDER":"[^"]*' "${CERT_DIR}/pool.json" | head -1 \
  | sed 's/"RootCertificateDER":"//' | base64 --decode \
  | openssl x509 -inform der -outform pem > "${CERT_DIR}/servicedns-root.pem"
openssl x509 -in "${CERT_DIR}/servicedns-root.pem" -noout >/dev/null \
  || fail shim "could not extract a valid CA root from service-dns-ca-pool"
kubectl create secret generic servicedns-ca -n ate-system \
  --from-file=trust-bundle.pem="${CERT_DIR}/servicedns-root.pem" \
  --dry-run=client -o yaml | kubectl apply -f -

# --- 4. atelet toleration for Custom-Compute-Class (NAP) nodes -------------------
# Upstream's atelet DaemonSet carries no tolerations; rooms scheduled onto a
# compute-class-tainted node would have no local atelet and every resume
# there fails. Safe on any cluster (matches only the compute-class taint).
note "[4/8] patching atelet tolerations for compute-class nodes"
kubectl -n ate-system patch daemonset atelet --type=json -p '[
  {"op": "add", "path": "/spec/template/spec/tolerations",
   "value": [{"key": "cloud.google.com/compute-class", "operator": "Exists", "effect": "NoSchedule"}]}
]'

# --- 4b. daily router restart (cert-rotation workaround) -------------------------
# The router loads its atunnel-egress client certificate STATICALLY into the
# Envoy config at startup; kubelet rotates the underlying PodCertificate but
# Envoy never re-reads it, so after the cert lifetime (~4 days observed) every
# router->worker handshake fails with SSLV3_ALERT_CERTIFICATE_EXPIRED and all
# chats 503. Until upstream serves the cert via SDS, restart the router daily
# (rolling restart — the surge pod comes up before the old one drains).
note "[4b/8] installing daily atenet-router restart CronJob (cert workaround)"
kubectl apply -f - <<'YAML'
apiVersion: v1
kind: ServiceAccount
metadata: {name: router-restarter, namespace: ate-system}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: router-restarter, namespace: ate-system}
rules:
- apiGroups: ["apps"]
  resources: ["deployments"]
  resourceNames: ["atenet-router"]
  verbs: ["get", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: router-restarter, namespace: ate-system}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: router-restarter}
subjects: [{kind: ServiceAccount, name: router-restarter, namespace: ate-system}]
---
apiVersion: batch/v1
kind: CronJob
metadata: {name: atenet-router-cert-refresh, namespace: ate-system}
spec:
  schedule: "43 3 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 2
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: router-restarter
          restartPolicy: Never
          containers:
          - name: restart
            image: bitnami/kubectl:latest
            command: ["kubectl", "-n", "ate-system", "rollout", "restart",
                      "deployment/atenet-router"]
YAML

# --- 5. snapshot bucket ----------------------------------------------------------
# SuspendActor takes a FULL snapshot; the feature's ActorTemplate points
# snapshotsConfig.location at gs://${PROJECT_NAME}-substrate-snapshots/. The
# upload is performed by the atelet DaemonSet (KSA ate-system/atelet). On a
# cluster WITHOUT Workload Identity the atelet inherits the node service
# account, so the node SA gets objectAdmin. On a cluster WITH Workload
# Identity the atelet's GCS calls carry the WI principal for
# ns/ate-system/sa/atelet, NOT the node SA, so that principal needs the same
# grant or every suspend 403s and the actor wedges in SUSPENDING.
note "[5/8] ensuring snapshot bucket gs://${PROJECT_NAME}-substrate-snapshots"
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
  # The atelet also PULLS the guest image itself (it assembles the gVisor OCI
  # bundle in userspace, not via kubelet), so on a Workload Identity cluster
  # its principal needs Artifact Registry read or every restore fails with
  # "artifactregistry.repositories.downloadArtifacts denied".
  gcloud artifacts repositories add-iam-policy-binding "${ARTIFACT_REGISTRY_REPO}" \
    --location "${REGION}" --project "${PROJECT_NAME}" \
    --member "principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WORKLOAD_POOL}/subject/ns/ate-system/sa/atelet" \
    --role roles/artifactregistry.reader >/dev/null
fi

# --- 5b. Vertex AI access (keyless — the whole point) ---------------------------
# Guests call Gemini with ADC. Egress from a gVisor actor rides the worker
# pod's network namespace, so the identity the call carries is either the
# node SA (no Workload Identity) or the WI principal of the worker pod's KSA
# (`default` in APP_NAMESPACE). The concierge SA gets the same grant for the
# /llm proxy fallback. All grants are idempotent add-iam-policy-bindings.
note "[5b/8] granting roles/aiplatform.user for Vertex AI (ADC)"
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

# --- 6. optional: Workload Identity impersonation (org-policy-swept projects) ---
# Some projects have their project IAM policy rewritten by an org process every
# few minutes, silently wiping the WI-principal grants from steps 5/5b. Setting
# WI_IMPERSONATE_GSA=<gsa-email> makes the hotel's workloads run as that GSA
# instead: the workloadIdentityUser bindings live on the SA RESOURCE, not the
# project policy, so they survive the sweep. The GSA must already hold Artifact
# Registry read, snapshot-bucket write, and aiplatform.user (the default
# compute SA typically does). Wired identities:
#   ate-system/atelet             — pulls guest images (userspace OCI bundle
#                                   assembly) + uploads suspend snapshots
#   APP_NAMESPACE/default         — worker pods; guest egress (Vertex calls)
#                                   carries this identity
#   APP_NAMESPACE/substrate-hotel-concierge — /llm proxy fallback (annotated in
#                                   deploy_app.sh once the KSA exists)
if [ -n "${WI_IMPERSONATE_GSA:-}" ]; then
  note "[6/8] wiring WI impersonation of ${WI_IMPERSONATE_GSA}"
  for KSA_REF in "ate-system/atelet" "${APP_NAMESPACE}/default" \
                 "${APP_NAMESPACE}/substrate-hotel-concierge"; do
    gcloud iam service-accounts add-iam-policy-binding "${WI_IMPERSONATE_GSA}" \
      --project "${PROJECT_NAME}" --role roles/iam.workloadIdentityUser \
      --member "serviceAccount:${PROJECT_NAME}.svc.id.goog[${KSA_REF}]" >/dev/null
  done
  kubectl -n ate-system annotate serviceaccount atelet \
    "iam.gke.io/gcp-service-account=${WI_IMPERSONATE_GSA}" --overwrite
  kubectl -n "${APP_NAMESPACE}" annotate serviceaccount default \
    "iam.gke.io/gcp-service-account=${WI_IMPERSONATE_GSA}" --overwrite
fi

# --- 7. settle the atelet DaemonSet (toleration patch + optional annotation) -----
note "[7/8] rolling the atelet DaemonSet"
kubectl -n ate-system rollout restart daemonset/atelet
kubectl -n ate-system rollout status daemonset/atelet --timeout=300s

# --- 8. ateom-gvisor worker image ------------------------------------------------
note "[8/8] building + pushing ateom-gvisor:latest -> ${KO_DOCKER_REPO}/ateom-gvisor"
ko build --base-import-paths --tags=latest --push ./cmd/ateom-gvisor

cat <<EOF

==> Done. FULL ate plane is installed (upstream PodCertificate data plane):
      - CRDs:            workerpools / actortemplates / sandboxconfigs (ate.dev)
      - cert signer:     deployment/podcertificate-controller (podcertificate-controller-system)
      - api server:      deployment/ate-api-server (ate-system)
      - controller:      deployment/ate-controller  (ate-system)
      - router + dns:    deployment/atenet-router, deployment/dns
      - session store:   statefulset/valkey-cluster
      - node agent:      daemonset/atelet (compute-class toleration patched in)
      - CA shim:         Secret ate-system/servicedns-ca (mirrors service-dns pool root)
      - snapshots:       gs://${PROJECT_NAME}-substrate-snapshots (node SA + WI atelet objectAdmin)
      - vertex ai:       roles/aiplatform.user for node SA + ${APP_NAMESPACE} KSAs (keyless Gemini)
      - worker image:    ${KO_DOCKER_REPO}/ateom-gvisor:latest

    Now run ./deploy_app.sh to build and deploy the Agent Hotel.
EOF
