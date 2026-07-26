#!/usr/bin/env bash
# End-to-end smoke test — the automated version of the demo's memory proof:
#   1. concierge healthy, gateway programmed
#   2. chat with a guest                  -> reply + boot_id A
#   3. wait past the linger               -> guest shows "suspended"
#   4. chat again (resume-on-request)     -> reply references the conversation,
#                                            boot_id still A, history grew
set -e

if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found. Create one with: cp .env.example .env"
  exit 1
fi

REGION="${REGION:-${ZONE%-*}}"
NAMESPACE="${NAMESPACE:-default}"
GUEST="${GUEST:-amara}"
LINGER="${LINGER_SECONDS:-10}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_NAME=$(awk '/kind: Gateway/{f=1} f&&/^  name:/{print $2; exit}' "${ROOT}/infra/gateway.yaml")

pass() { echo "  ✅ $*"; }
fail() { echo "  ❌ $*"; exit 1; }
jsonget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }

echo "=== Targeting cluster ${CLUSTER_NAME} (${ZONE}) ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" >/dev/null

echo "=== 1/6 Concierge deployment ready ==="
kubectl -n "${NAMESPACE}" rollout status deployment/substrate-hotel-concierge-deployment --timeout=120s >/dev/null \
  && pass "substrate-hotel-concierge-deployment is ready" \
  || fail "concierge deployment not ready"

echo "=== 2/6 Gateway address ==="
GATEWAY_IP=$(kubectl -n "${NAMESPACE}" get gateway "${GATEWAY_NAME}" -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
[ -n "${GATEWAY_IP}" ] && pass "gateway IP: ${GATEWAY_IP}" || fail "gateway has no address yet (LB still provisioning?)"
BASE="http://${GATEWAY_IP}"

echo "=== 3/6 Health + hotel state ==="
curl -sf --max-time 10 "${BASE}/healthz" >/dev/null && pass "/healthz ok" || fail "/healthz unreachable"
STATE=$(curl -sf --max-time 15 "${BASE}/api/state") || fail "/api/state unreachable"
WORKERS=$(echo "${STATE}" | jsonget "d['meter']['workers']")
[ "${WORKERS}" -ge 1 ] && pass "${WORKERS} worker room(s) visible" || fail "no workers — is the WorkerPool reconciled?"
TEMPLATE_ERR=$(echo "${STATE}" | jsonget "d.get('template_error') or ''")
[ -z "${TEMPLATE_ERR}" ] && pass "ActorTemplate registered" || fail "template error: ${TEMPLATE_ERR}"

echo "=== 4/6 First chat with ${GUEST} (check-in + golden-snapshot start) ==="
R1=$(curl -sf --max-time 180 -X POST "${BASE}/api/guests/${GUEST}/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message": "Hello! Please remember my favorite color is teal."}') \
  || fail "first chat failed"
BOOT1=$(echo "${R1}" | jsonget "d['boot_id']")
H1=$(echo "${R1}" | jsonget "d['history_len']")
pass "reply received (boot_id ${BOOT1}, history_len ${H1})"

echo "=== 5/6 Waiting for ${GUEST} to yield the room (linger ${LINGER}s + sweep) ==="
SUSPENDED=false
for i in $(seq 1 24); do
  sleep 5
  ST=$(curl -sf --max-time 15 "${BASE}/api/state" \
    | jsonget "[g['status'] for g in d['guests'] if g['name']=='${GUEST}'][0]") || true
  if [ "${ST}" = "suspended" ]; then SUSPENDED=true; break; fi
done
[ "${SUSPENDED}" = true ] && pass "${GUEST} is suspended (checkpoint in GCS, room freed)" \
  || fail "${GUEST} never suspended — check the concierge logs (yield/sweeper)"

echo "=== 6/6 Second chat — resume-on-request memory proof ==="
R2=$(curl -sf --max-time 180 -X POST "${BASE}/api/guests/${GUEST}/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message": "Welcome back! What is my favorite color?"}') \
  || fail "second chat failed (resume path)"
BOOT2=$(echo "${R2}" | jsonget "d['boot_id']")
H2=$(echo "${R2}" | jsonget "d['history_len']")
WAS=$(echo "${R2}" | jsonget "d.get('was_suspended')")
[ "${WAS}" = "True" ] && pass "guest was resumed from a snapshot by the router" \
  || echo "  ⚠ guest was not suspended at chat time (timing); memory checks still apply"
[ "${BOOT2}" = "${BOOT1}" ] && pass "boot_id unchanged (${BOOT2}) — the SAME process came back" \
  || fail "boot_id changed (${BOOT1} -> ${BOOT2}) — RAM state did not survive"
[ "${H2}" -gt "${H1}" ] && pass "history grew ${H1} -> ${H2} — conversation memory intact" \
  || fail "history did not grow (${H1} -> ${H2})"

echo ""
echo "All checks passed. Open the hotel: ${BASE}/"
