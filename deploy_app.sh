#!/usr/bin/env bash
# Build & push the Agent Hotel image, then deploy the per-namespace infra
# (concierge + WorkerPool + Gateway + HTTPRoute + RBAC). Run setup_infra.sh first.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${ROOT}/lib.sh"
load_env
init_curl_auth

# --reset-gateway: delete the Gateway + HTTPRoute before applying, forcing the
# GKE controller to reconcile them fresh (use if the Gateway is wedged).
RESET_GW=false
case "${1:-}" in
  --reset-gateway) RESET_GW=true ;;
  -h|--help)       echo "Usage: $0 [--reset-gateway]"; exit 0 ;;
  "")              ;;
  *) echo "Unknown argument: $1 (use --reset-gateway or no flag)"; exit 1 ;;
esac

# Per-cluster image tag so multiple clusters never clobber each other's image
# (a moving :latest shared across clusters would also invalidate golden
# snapshots unpredictably).
IMAGE_TAG="${IMAGE_TAG:-${CLUSTER_NAME}}"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY_REPO}"
SUBSTRATE_HOTEL_IMAGE="${REGISTRY}/substrate-hotel:${IMAGE_TAG}"

# Source of truth for resource names is the manifests, not .env or literals.
GATEWAY_NAME=$(manifest_name Gateway "${ROOT}/infra/gateway.yaml")
HTTPROUTE_NAME=$(manifest_name HTTPRoute "${ROOT}/infra/http-route.yaml")
WORKERPOOL_NAME=$(manifest_name WorkerPool "${ROOT}/infra/workerpool.yaml")

# Portable ${VAR} substitution (no envsubst dependency).
render() { python3 -c "import os,sys;sys.stdout.write(os.path.expandvars(open(sys.argv[1]).read()))" "$1"; }

echo "=== Targeting cluster ${CLUSTER_NAME} (${ZONE}) ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"

# The vendored API stubs, the manifests and the worker image all follow the
# pinned release; refuse to deploy against a plane of another release.
INSTALLED_RELEASE="$(installed_substrate_release)"
if [ "${INSTALLED_RELEASE}" != "${UPSTREAM_REF}" ]; then
  echo "Error: the cluster runs Agent Substrate '${INSTALLED_RELEASE:-<none>}' but this checkout pins ${UPSTREAM_REF}."
  echo "       Run ./setup_infra.sh (fresh install) or follow cluster/README.md to upgrade the plane first."
  exit 1
fi
echo "    plane release: ${UPSTREAM_REF}"

echo "=== Ensuring Artifact Registry repo ${ARTIFACT_REGISTRY_REPO} exists ==="
ensure_ar_repo

echo "=== Authenticating Docker to ${REGION}-docker.pkg.dev ==="
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "=== Building image (linux/amd64 for GKE nodes): ${SUBSTRATE_HOTEL_IMAGE} ==="
# Context is the repo root so the image bakes app/ + ateapipb/ + frontend/.
docker build --platform linux/amd64 -t "${SUBSTRATE_HOTEL_IMAGE}" -f "${ROOT}/app/Dockerfile" "${ROOT}"

echo "=== Pushing image ==="
docker push "${SUBSTRATE_HOTEL_IMAGE}"

echo "=== Deploying per-namespace infra into: ${NAMESPACE} ==="
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"
# ate-system denies ingress by default; this label admits the namespace's
# pods (the concierge's gRPC) to ate-api-server.
kubectl label namespace "${NAMESPACE}" ate.dev/api-access=true --overwrite >/dev/null

# The guests' Gemini API key. It lives in this Secret and NOWHERE near a
# guest: Substrate's egress gateway injects it into the guests' HTTPS
# requests (see app/template_manager.py). GEMINI_API_KEY in .env, or
# GEMINI_API_KEY_RESOURCE (a projects/.../keys/... name gcloud resolves);
# neither set = no Secret = scripted replies.
GEMINI_SECRET_NAME="substrate-hotel-gemini"
GEMINI_KEY="${GEMINI_API_KEY:-}"
if [ -z "${GEMINI_KEY}" ] && [ -n "${GEMINI_API_KEY_RESOURCE:-}" ]; then
  GEMINI_KEY="$(gcloud services api-keys get-key-string "${GEMINI_API_KEY_RESOURCE}"     --project="${PROJECT_ID}" --format='value(keyString)')"
fi
if [ -n "${GEMINI_KEY}" ]; then
  echo "=== Configuring the Gemini key Secret (injected by the egress gateway) ==="
  kubectl -n "${NAMESPACE}" create secret generic "${GEMINI_SECRET_NAME}"     --from-literal=api-key="${GEMINI_KEY}"     --from-literal=GEMINI_API_KEY="${GEMINI_KEY}"     --dry-run=client -o yaml | kubectl apply -n "${NAMESPACE}" -f -
  export GEMINI_SECRET_URI="ate-secret://k8s.io/default/${NAMESPACE}/${GEMINI_SECRET_NAME}/api-key"
else
  echo "=== No Gemini key configured — guests will use scripted replies ==="
  kubectl -n "${NAMESPACE}" delete secret "${GEMINI_SECRET_NAME}" --ignore-not-found
  export GEMINI_SECRET_URI=""
fi
export GEMINI_MODEL="${GEMINI_MODEL:-gemini-flash-latest}"

# HTTP Basic auth for the hotel's public surface. The password lives only in
# .env and this Secret — never rendered through the ${VAR}-templated
# manifests. Empty HOTEL_PASSWORD disables auth (and removes a stale Secret).
if [ -n "${HOTEL_PASSWORD:-}" ]; then
  echo "=== Configuring Basic auth Secret (user: ${HOTEL_USERNAME:-guest}) ==="
  kubectl -n "${NAMESPACE}" create secret generic substrate-hotel-auth \
    --from-literal=HOTEL_USERNAME="${HOTEL_USERNAME:-guest}" \
    --from-literal=HOTEL_PASSWORD="${HOTEL_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -n "${NAMESPACE}" -f -
else
  echo "=== HOTEL_PASSWORD not set — the hotel will be UNAUTHENTICATED ==="
  kubectl -n "${NAMESPACE}" delete secret substrate-hotel-auth --ignore-not-found
fi

# Force-delete the Gateway, stripping a stuck finalizer if its controller never
# adopted it. Safe here: a wedged Gateway has no GCP resources to orphan.
force_delete_gateway() {
  local gw="$1"
  kubectl -n "${NAMESPACE}" get gateway "${gw}" >/dev/null 2>&1 || return 0
  kubectl -n "${NAMESPACE}" delete gateway "${gw}" --ignore-not-found --timeout=60s && return 0
  echo "Gateway delete is stuck; removing finalizer..."
  kubectl -n "${NAMESPACE}" patch gateway "${gw}" --type=merge -p '{"metadata":{"finalizers":null}}' || true
  kubectl -n "${NAMESPACE}" wait --for=delete "gateway/${gw}" --timeout=60s || true
}

# Recreate the Gateway + HTTPRoute together when the class changed
# (gatewayClassName is immutable) or on --reset-gateway.
DESIRED_GW_CLASS=$(grep -m1 'gatewayClassName:' "${ROOT}/infra/gateway.yaml" | awk '{print $2}')
CUR_GW_CLASS=$(kubectl -n "${NAMESPACE}" get gateway "${GATEWAY_NAME}" -o jsonpath='{.spec.gatewayClassName}' 2>/dev/null || true)
if [ "${RESET_GW}" = true ] || { [ -n "${CUR_GW_CLASS}" ] && [ "${CUR_GW_CLASS}" != "${DESIRED_GW_CLASS}" ]; }; then
  echo "Recreating Gateway + HTTPRoute for a clean reconcile (its IP will change)."
  kubectl -n "${NAMESPACE}" delete httproute "${HTTPROUTE_NAME}" --ignore-not-found
  force_delete_gateway "${GATEWAY_NAME}"
fi

# Variables the infra manifests reference. PROJECT_NAME mirrors PROJECT_ID so
# the manifests use one canonical var name.
export NAMESPACE REGION ARTIFACT_REGISTRY_REPO SUBSTRATE_HOTEL_IMAGE
export PROJECT_NAME="${PROJECT_ID}"
# Hotel sizing + timing knobs (see .env.example).
export GUEST_COUNT="${GUEST_COUNT:-12}"
export WORKER_REPLICAS="${WORKER_REPLICAS:-3}"
export LINGER_SECONDS="${LINGER_SECONDS:-10}"
export YIELD_MODE="${YIELD_MODE:-pause}"
export DEEP_SLEEP_SECONDS="${DEEP_SLEEP_SECONDS:-120}"
# The rooms run upstream's release worker image, pinned by digest.
export ATEWORKER_IMAGE="${UPSTREAM_IMAGE_REPO}/ateworker-gvisor:${UPSTREAM_REF}@$(ghcr_digest ateworker-gvisor)"
echo "    worker image: ${ATEWORKER_IMAGE}"
# Rooms scheduling: with USE_COMPUTE_CLASS=TRUE the worker pods select the
# hotel-rooms ComputeClass (NAP provisions c3 nodes for them); otherwise any node.
if [ "${USE_COMPUTE_CLASS:-FALSE}" = "TRUE" ]; then
  export WORKER_NODE_SELECTOR='{"cloud.google.com/compute-class": "hotel-rooms"}'
else
  export WORKER_NODE_SELECTOR='{}'
fi
for f in "${ROOT}"/infra/*.yaml; do
  base=$(basename "$f")
  echo "    applying ${base}"
  render "$f" | kubectl apply -n "${NAMESPACE}" -f -
done

# --- Substrate-side setup: the atespace and the concierge's roles -------------------
# Everything the concierge does goes through the ate API as its ServiceAccount
# token. Substrate authorizes every call with access policies; the install's
# bootstrap owner (ate-client, the identity kubectl-ate acts as) creates the
# hotel's atespace and grants the concierge what it needs and nothing more:
# editor on the hotel atespace (actors, templates, egress policies) and
# global viewer (ListWorkers — the rooms on the floor plan).
echo "=== Substrate: atespace '${ATESPACE}' + concierge access policies ==="
CONCIERGE_PRINCIPAL="user:system:serviceaccount:${NAMESPACE}:substrate-hotel-concierge"
kate get atespace "${ATESPACE}" >/dev/null 2>&1 || kate create atespace "${ATESPACE}"

# grant_access <scope flag(s)> <role> <member>: add a binding to the scope's
# access policy if it is not there yet (policies are whole-document updates,
# guarded by the uid/version `get` returns — JSON is valid manifest input).
grant_access() {
  local scope="$1" role="$2" member="$3" current merged rc
  # shellcheck disable=SC2086
  current="$(kate get access-policy ${scope} -o json 2>/dev/null || true)"
  if [ -z "${current}" ]; then
    printf '{"metadata":{"name":"default"},"bindings":[{"role":"%s","members":["%s"]}]}' "${role}" "${member}" \
      | kate create access-policy ${scope} -f - >/dev/null
    echo "    granted ${role} (${scope}) to ${member}"
    return
  fi
  merged="$(ROLE="${role}" MEMBER="${member}" python3 -c '
import json, os, sys
doc = json.load(sys.stdin)
role, member = os.environ["ROLE"], os.environ["MEMBER"]
bindings = doc.get("bindings") or []
for b in bindings:
    if b.get("role") == role:
        if member in (b.get("members") or []):
            sys.exit(3)
        b.setdefault("members", []).append(member)
        break
else:
    bindings.append({"role": role, "members": [member]})
doc["bindings"] = bindings
json.dump(doc, sys.stdout)' <<<"${current}")" && rc=0 || rc=$?
  if [ "${rc}" = "3" ]; then
    echo "    ${member} already holds ${role} (${scope})"
  elif [ "${rc}" = "0" ]; then
    # shellcheck disable=SC2086
    kate update access-policy ${scope} -f - <<<"${merged}" >/dev/null
    echo "    granted ${role} (${scope}) to ${member}"
  else
    echo "Error: could not merge the access policy (${scope})"; exit 1
  fi
}
grant_access "-a ${ATESPACE}" editor "${CONCIERGE_PRINCIPAL}"
grant_access "--global" viewer "${CONCIERGE_PRINCIPAL}"

echo "=== Rolling out the concierge ==="
# Force a fresh pull of the rebuilt image (stable per-cluster tag + Always
# policy). A new image digest makes the concierge register a fresh
# ActorTemplate (and golden snapshot) automatically.
kubectl -n "${NAMESPACE}" rollout restart deployment/substrate-hotel-concierge-deployment
kubectl -n "${NAMESPACE}" rollout status deployment/substrate-hotel-concierge-deployment --timeout=600s || true

echo "=== Waiting for the WorkerPool's rooms ==="
# The ate-controller names the worker Deployment after the pool.
kubectl -n "${NAMESPACE}" rollout status "deployment/${WORKERPOOL_NAME}" --timeout=600s || \
  echo "WorkerPool deployment not ready yet — the ate-controller may still be reconciling."

echo "=== Deployed. Discovering Gateway IP (may take 3-5 minutes) ==="
gateway_ip() {
  local ip rules
  ip=$(kubectl -n "${NAMESPACE}" get gateway "${GATEWAY_NAME}" -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
  if [ -z "${ip}" ]; then
    # Fallback while the Gateway status lags. The forwarding-rule name only
    # encodes namespace + gateway name — with TWO clusters in the project
    # using the same names this would match the OTHER cluster's LB, so only
    # trust it when the match is unambiguous.
    rules=$(gcloud compute forwarding-rules list --global --project="${PROJECT_ID}" \
      --filter="name~gkegw1.*-${NAMESPACE}-${GATEWAY_NAME}" --format="value(IPAddress)" 2>/dev/null || true)
    [ "$(echo "${rules}" | grep -c .)" = "1" ] && ip="${rules}"
  fi
  echo "${ip}"
}
for i in {1..30}; do
  GATEWAY_IP=$(gateway_ip)
  if [ -n "${GATEWAY_IP}" ]; then
    echo "Gateway IP: ${GATEWAY_IP}"
    echo "  Hotel UI:  http://${GATEWAY_IP}/"
    echo "  Health:    http://${GATEWAY_IP}/healthz"
    break
  fi
  sleep 10
done
[ -z "${GATEWAY_IP:-}" ] && echo "Gateway IP not ready yet; check: kubectl -n ${NAMESPACE} get gateway ${GATEWAY_NAME}"

# --- Warm-up: pull the new guest image onto every room's node -------------------
# Each rebuild changes the image digest, so the FIRST actor start on a node
# re-pulls the image — slower than the router's patience, surfacing as a
# one-off 500/504 to whoever chats first. Chat once per room here (in
# parallel, so each lands on a different room), then reset: the pull cache
# survives the reset, the demo starts clean AND warm.
if [ -n "${GATEWAY_IP:-}" ]; then
  # Rooms are runtime-scalable from the UI: warm the LIVE WorkerPool size,
  # not the initial .env value.
  LIVE_ROOMS=$(kubectl -n "${NAMESPACE}" get workerpool "${WORKERPOOL_NAME}" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || true)
  LIVE_ROOMS="${LIVE_ROOMS:-${WORKER_REPLICAS}}"
  echo "=== Warming ${LIVE_ROOMS} room(s) (first pull of the new image per node) ==="
  json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)" 2>/dev/null; }
  # `|| true` everywhere: this script runs under set -e, and the LB briefly
  # drops connections while it swaps to the restarted concierge pod.
  # Wait for the template's GOLDEN snapshot: guests check in from it in
  # moments; before it exists a first chat cold-boots the image instead.
  GOLDEN=""
  for i in {1..36}; do
    GOLDEN=$(curl -s --max-time 10 ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \
      "http://${GATEWAY_IP}/api/state" \
      | json_get "('ready' if (d.get('golden') or {}).get('golden_ready') else ((d.get('golden') or {}).get('error') or d.get('template_error') or ''))" || true)
    [ "${GOLDEN}" = "ready" ] && break
    sleep 10
  done
  if [ "${GOLDEN}" != "ready" ]; then
    echo "    golden snapshot not ready — skipping warm-up (first chats may be slow). ${GOLDEN:+Last status: ${GOLDEN}}"
  else
    WARM_GUESTS=$(curl -s --max-time 10 ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \
      "http://${GATEWAY_IP}/api/state" \
      | json_get "' '.join(g['name'] for g in d['guests'][:${LIVE_ROOMS}])" || true)
    warm_one() {
      for a in 1 2 3 4; do
        curl -sf --max-time 150 ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \
          -X POST "http://${GATEWAY_IP}/api/guests/$1/chat" \
          -H 'Content-Type: application/json' \
          -d '{"message":"(warm-up ping — just say hi briefly)"}' >/dev/null \
          && { echo "    room warmed via $1"; return 0; }
        sleep 5
      done
      echo "    warm-up via $1 did not complete (non-fatal)"
    }
    WARM_PIDS=()
    for g in ${WARM_GUESTS}; do warm_one "${g}" & WARM_PIDS+=($!); done
    wait ${WARM_PIDS[@]+"${WARM_PIDS[@]}"} 2>/dev/null || true
    curl -s --max-time 30 ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \
      -X POST "http://${GATEWAY_IP}/api/reset" \
      -H 'Content-Type: application/json' -d '{}' >/dev/null || true
    echo "    rooms warm; hotel reset to a clean slate"
  fi
fi

echo "=== Done. Run ./verify_setup.sh to smoke-test the suspend/resume proof. ==="
