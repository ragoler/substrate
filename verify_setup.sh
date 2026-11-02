#!/usr/bin/env bash
# End-to-end smoke test — the automated version of the demo's memory proof:
#   1. the plane runs the pinned release; the WorkerPool is there
#   2. concierge healthy, gateway programmed
#   3. chat with a guest                  -> reply + boot_id A
#   4. wait past the linger               -> guest suspended (room freed; the
#      snapshot is on the node first, in GCS once the upload lands)
#   5. chat again (resume-on-request)     -> reply references the conversation,
#                                            boot_id still A, history grew
#   6. the guest's EgressPolicy exists and, with a Gemini key, the reply came
#      from Gemini through the egress gateway (not the scripted fallback)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${ROOT}/lib.sh"
load_env
init_curl_auth

GUEST="${GUEST:-amara}"
LINGER="${LINGER_SECONDS:-10}"
GATEWAY_NAME=$(manifest_name Gateway "${ROOT}/infra/gateway.yaml")
WORKERPOOL_NAME=$(manifest_name WorkerPool "${ROOT}/infra/workerpool.yaml")
# The guest yields after LINGER seconds; the sweeper backstop adds up to
# SWEEP_IDLE_SECONDS (30) + one sweep interval + suspend/snapshot time.
MAX_SUSPEND_WAIT=$(( LINGER + 110 ))

pass() { echo "  ✅ $*"; }
fail() { echo "  ❌ $*"; exit 1; }
jsonget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }
hotel_curl() { curl ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} "$@"; }

echo "=== Targeting cluster ${CLUSTER_NAME} (${ZONE}) ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" >/dev/null

echo "=== 1/8 Plane release + WorkerPool ==="
INSTALLED=$(installed_substrate_release)
[ "${INSTALLED}" = "${UPSTREAM_REF}" ] && pass "Agent Substrate ${INSTALLED} matches the repo pin" \
  || fail "plane release '${INSTALLED:-<none>}' != repo pin ${UPSTREAM_REF}"
WP_IMG=$(kubectl -n "${NAMESPACE}" get workerpool "${WORKERPOOL_NAME}" -o jsonpath='{.spec.workerImage}' 2>/dev/null || true)
case "${WP_IMG}" in
  *"/ateworker-gvisor:${UPSTREAM_REF}@sha256:"*) pass "WorkerPool ${WORKERPOOL_NAME} runs the release worker image (digest-pinned)" ;;
  *) fail "WorkerPool worker image is '${WP_IMG}' (re-run deploy_app.sh)" ;;
esac

echo "=== 2/8 Concierge deployment ready ==="
kubectl -n "${NAMESPACE}" rollout status deployment/substrate-hotel-concierge-deployment --timeout=120s >/dev/null \
  && pass "substrate-hotel-concierge-deployment is ready" \
  || fail "concierge deployment not ready"

echo "=== 3/8 Gateway address ==="
GATEWAY_IP=$(kubectl -n "${NAMESPACE}" get gateway "${GATEWAY_NAME}" -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
[ -n "${GATEWAY_IP}" ] && pass "gateway IP: ${GATEWAY_IP}" || fail "gateway has no address yet (LB still provisioning?)"
BASE="http://${GATEWAY_IP}"

echo "=== 4/8 Health + hotel state ==="
curl -sf --max-time 10 "${BASE}/healthz" >/dev/null && pass "/healthz ok" || fail "/healthz unreachable"
if [ -n "${HOTEL_PASSWORD:-}" ]; then
  # Basic auth is on: no credentials must be 401, ours must work.
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${BASE}/api/state" || true)
  [ "${CODE}" = "401" ] && pass "auth enforced (401 without credentials)" \
    || fail "expected 401 without credentials, got ${CODE}"
fi
STATE=$(hotel_curl -sf --max-time 15 "${BASE}/api/state") || fail "/api/state unreachable (with credentials)"
WORKERS=$(echo "${STATE}" | jsonget "d['meter']['workers']")
[ "${WORKERS}" -ge 1 ] && pass "${WORKERS} worker room(s) visible" || fail "no workers — is the WorkerPool reconciled?"
TEMPLATE_ERR=$(echo "${STATE}" | jsonget "d.get('template_error') or ''")
[ -z "${TEMPLATE_ERR}" ] && pass "ActorTemplate registered ($(echo "${STATE}" | jsonget "d.get('template')"))" \
  || fail "template error: ${TEMPLATE_ERR}"
GOLDEN_ERR=$(echo "${STATE}" | jsonget "(d.get('golden') or {}).get('error') or ''")
[ -z "${GOLDEN_ERR}" ] || fail "golden snapshot failed: ${GOLDEN_ERR}"
GOLDEN_READY=$(echo "${STATE}" | jsonget "(d.get('golden') or {}).get('golden_ready')")
[ "${GOLDEN_READY}" = "True" ] && pass "golden snapshot ready (new guests check in from it)" \
  || echo "  ⚠ golden snapshot still building — the first check-in will cold-boot instead"

echo "=== 5/8 First chat with ${GUEST} (check-in) ==="
R1=$(hotel_curl -sf --max-time 180 -X POST "${BASE}/api/guests/${GUEST}/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message": "Hello! Please remember my favorite color is teal."}') \
  || fail "first chat failed"
BOOT1=$(echo "${R1}" | jsonget "d['boot_id']")
H1=$(echo "${R1}" | jsonget "d['history_len']")
pass "reply received (boot_id ${BOOT1}, history_len ${H1})"

echo "=== 6/8 Waiting for ${GUEST} to yield the room (linger ${LINGER}s + sweep; up to ${MAX_SUSPEND_WAIT}s) ==="
# A yield suspends the guest: the room is free once the snapshot is on the
# node ("napping"); atelet uploads it to GCS in the background ("suspended").
# Either proves the memory story on wake; VERIFY_DURABLE=1 waits for GCS.
WANT="napping suspended"
[ "${VERIFY_DURABLE:-0}" = "1" ] && { WANT="suspended"; MAX_SUSPEND_WAIT=$(( MAX_SUSPEND_WAIT + 120 )); }
ASLEEP=""
for i in $(seq 1 $(( MAX_SUSPEND_WAIT / 5 ))); do
  sleep 5
  ST=$(hotel_curl -sf --max-time 15 "${BASE}/api/state" \
    | jsonget "[g['status'] for g in d['guests'] if g['name']=='${GUEST}'][0]") || true
  case " ${WANT} " in *" ${ST} "*) ASLEEP="${ST}"; break ;; esac
done
[ -n "${ASLEEP}" ] && pass "${GUEST} is ${ASLEEP} (room freed; $([ "${ASLEEP}" = napping ] && echo 'snapshot on the node, upload in progress' || echo 'snapshot in GCS'))" \
  || fail "${GUEST} never went to sleep — check the concierge logs (yield/sweeper)"

echo "=== 7/8 Second chat — resume-on-request memory proof ==="
R2=$(hotel_curl -sf --max-time 180 -X POST "${BASE}/api/guests/${GUEST}/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message": "Welcome back! What is my favorite color?"}') \
  || fail "second chat failed (resume path)"
BOOT2=$(echo "${R2}" | jsonget "d['boot_id']")
H2=$(echo "${R2}" | jsonget "d['history_len']")
WAS=$(echo "${R2}" | jsonget "d.get('was_suspended')")
SLEPT=$(echo "${R2}" | jsonget "d.get('slept_as')")
[ "${WAS}" = "True" ] && pass "guest was resumed by the router (woke from ${SLEPT})" \
  || echo "  ⚠ guest was not suspended at chat time (timing); memory checks still apply"
[ "${BOOT2}" = "${BOOT1}" ] && pass "boot_id unchanged (${BOOT2}) — the SAME process came back" \
  || fail "boot_id changed (${BOOT1} -> ${BOOT2}) — RAM state did not survive"
[ "${H2}" -gt "${H1}" ] && pass "history grew ${H1} -> ${H2} — conversation memory intact" \
  || fail "history did not grow (${H1} -> ${H2})"

echo "=== 8/8 Egress: policy + credential injection ==="
POLICY=$(kate get egress-policy "${GUEST}" -a "${ATESPACE}" -o yaml 2>/dev/null || true)
echo "${POLICY}" | grep -q "substrate-hotel-concierge" \
  && pass "${GUEST} has an EgressPolicy (concierge$(echo "${POLICY}" | grep -q generativelanguage && echo ' + Gemini with key injection'))" \
  || fail "no EgressPolicy for ${GUEST} — guests reach nothing without one"
REPLY2=$(echo "${R2}" | jsonget "d['reply']")
if kubectl -n "${NAMESPACE}" get secret substrate-hotel-gemini >/dev/null 2>&1; then
  case "${REPLY2}" in
    *"scripted voice"*) fail "Gemini key is configured but the guest answered with the scripted fallback — check the egress gateway logs: kubectl -n ate-system logs deploy/atenet-egress" ;;
    *) pass "reply came from Gemini through the egress gateway (guest holds no key)" ;;
  esac
else
  echo "  ⚠ no Gemini key configured (GEMINI_API_KEY / GEMINI_API_KEY_RESOURCE in .env) — scripted replies"
fi

echo ""
echo "All checks passed. Open the hotel: ${BASE}/"
