# Agent Hotel — an Agent Substrate demo feature

A demo app that teaches [Agent Substrate](https://github.com/agent-substrate/substrate) by
running **12 named, Gemini-powered chat agents ("guests") on a 3-worker pool** (4×
oversubscription). Each guest keeps its entire conversation history in RAM, **yields its
compute ~10 seconds after replying** (checkpoint → GCS, zero-idle), and is **transparently
restored — memory intact, often on a different worker — the moment you message it again**
(resume-on-request through the atenet-router).

Built as an **external feature** for the GKE Feature Showcase Hub
(`gke_all/feature.md` contract): this repo runs **standalone** on its own GKE cluster and
also mounts as a git submodule at `features/agent-hotel/` in the hub.

What it showcases (beyond the existing `substrate-basic` / `substrate-overcommit` features):

| Substrate capability | How the demo shows it |
| --- | --- |
| Zero-idle self-suspension | Agent decides to yield seconds after its last reply |
| Resume-on-request | Messaging a 💤 guest transparently wakes it via the DNS mesh |
| Oversubscription | 12 persistent agents on a 3-pod WorkerPool, live worker-lane churn |
| RAM-state persistence | Conversation + `boot_id` survive suspend/resume/migration |
| Secrets to sandboxes | `GEMINI_API_KEY` delivered via ActorTemplate `secretKeyRef` |
| Golden snapshots | New guests check in instantly from the v0 checkpoint |

UI: a "hotel floor plan" — 12 guest cards (🟢 awake / 💤 suspended, current worker), a
3-lane worker strip showing occupancy churn, a compute-saved meter, and a chat drawer that
proves the guest remembers everything across suspends.

---

## Verified facts the design is built on

Upstream source verified at the pinned SHA `d8f562d208c246a907fe7b3da81dd0318c5959f8`
(the same pin `substrate-overcommit` uses; newer upstream requires alpha pod-certificate
APIs):

- ActorTemplate container images **must be digest-pinned** (CEL rule requires `@`).
- `readyz.httpGet.port` is **required**; only `env[].valueFrom.secretKeyRef` is supported
  (with `optional: true`).
- **The ate-api-server itself resolves `secretKeyRef`** when building a workload spec, so
  the `ate-system/ate-api-server` SA needs `get` on the Secret (upstream
  `demos/agent-secret` grants exactly this).
- In `--auth-mode=jwt` (which our static-certs plane runs), the api-server accepts **any**
  cluster-issuer SA token with audience `api.ate-system.svc` — no SA allowlist. A pod can
  authenticate with its own projected SA token.
- Upstream's in-actor self-suspend (`demos/agent-secret`) dials the api-server with no
  bearer token — it **will be rejected on our jwt-auth plane** (drives decision D4).
- Resume of an actor with no last snapshot falls back to the **golden snapshot**; env
  values are frozen at golden-snapshot creation time.

Hub internals verified:

- The Hub's manifest applier **rewrites `metadata.namespace` and all RBAC subjects** to
  the deploy namespace → grants whose subject must stay `ate-system/ate-api-server`
  must ship in `cluster/` (applied verbatim at bootstrap), never in `infra/`.
- The Hub process already carries `config.GEMINI_API_KEY`; `GEMINI_API_KEY` is **not** a
  manifest template var.
- Hub RBAC (`infra/main-app.yaml`) and `showcase_admin/requirements-dev.txt` already
  cover everything this feature needs — **no hub-core edits required**.
- The hub build pipeline pushes `:latest` only (drives decision D7).

## Design decisions

### D1 — Name and layout
Feature name **`agent-hotel`** (namespace `gke-showcase-agent-hotel`, playroom slug
`agent-hotel`). Repo root == feature dir, jobset-style; submodule mount
`features/agent-hotel/`.

### D2 — Dual-mode architecture (jobset model)
- A **concierge** FastAPI Deployment in the feature namespace owns *all* ate logic: the
  gRPC ateapi client, actor lifecycle, chat proxying through
  `http://atenet-router.ate-system.svc` with `Host: <guest>.hotel.actors.resources.substrate.ate.dev`
  (120 s timeout), and serving the frontend itself (standalone UI requirement).
- `hub_router.py` stays **thin**: in LIVE, `GET /config` returns mode + gateway IP (Hub
  SDK) and lazily ensures the Gemini Secret (below); in MOCK it serves deterministic
  canned hotel state and scripted persona replies. It imports only FastAPI + the Hub SDK —
  zero new hub dependencies, no protos in the Hub image.
- gRPC client lives once in `app/ate_client.py` (ported from `substrate-overcommit`'s
  hub_router), with vendored `ateapipb/` stubs baked into the concierge image.
- **Concierge → api-server auth**: projected `serviceAccountToken` volume with
  `audience: api.ate-system.svc` (kubelet auto-rotates). TLS CA: read Secret
  `ate-system/servicedns-ca` via the K8s API, granted by a name-scoped ClusterRole in
  `infra/00-rbac.yaml` (the Hub's subject rewrite points the binding at the deploy
  namespace — exactly what we want here).

### D3 — LLM access: `GEMINI_API_KEY` via `secretKeyRef` (primary), concierge proxy (fallback)
- Secret `agent-hotel-gemini` (key `api-key`) referenced by ActorTemplate env
  `GEMINI_API_KEY` with `optional: true` — mirrors upstream `claude-code-multiplex` and
  showcases the secrets feature.
- Secret creation (the key can't live in `infra/`):
  - *Hub mode:* `hub_router.py` ensures the Secret on first LIVE `/config` call from
    `config.GEMINI_API_KEY` (best-effort; missing key ⇒ canned replies + UI banner).
  - *Standalone:* `deploy_app.sh` creates it from the gitignored `.env`.
- ate-api-server read grant ships as `cluster/ate-env-reader-rbac.yaml` (ClusterRole
  `secrets get resourceNames:[agent-hotel-gemini]` + ClusterRoleBinding to
  `ate-system/ate-api-server`), applied at bootstrap / by `setup_infra.sh`.
- **Caveats (document in README):** the key is frozen into the golden snapshot — rotation
  requires `POST /api/reset`; conversation RAM lands in GCS snapshots.
- **Fallback** if gVisor egress to `generativelanguage.googleapis.com` misbehaves:
  concierge `POST /llm` proxy; the agent's `llm.py` switches on `LLM_MODE=direct|proxy`.

### D4 — Zero-idle yield: agent-initiated, concierge-executed (+ sweeper)
The agent cannot call `SuspendActor` itself on a jwt-auth plane (a baked token would
freeze in the golden snapshot and expire). Instead:
- ~`LINGER_SECONDS` (10 s) after its last reply, the agent POSTs
  `CONCIERGE_URL/internal/yield {"guest": <name>}` (name parsed from the request `Host`
  header, agent-secret pattern). The **agent still decides when to yield** — the
  concierge merely executes the authenticated `SuspendActor` as its proxy.
- `/internal/yield` is spoof-tolerant: it suspends only if the concierge's own
  last-activity record for that guest is older than the linger; it is exempt from hub-JWT
  (cluster-internal, not reachable via the Gateway path).
- A background **sweeper** (every 5 s, suspend guests idle > 30 s) backstops lost yield
  calls and frees slots when all 3 workers are busy and a 4th guest is woken.

### D5 — Cluster prereqs: copy of the overcommit installer
`cluster/install-agent-hotel-prereq.sh` = near-verbatim copy of
`install-substrate-overcommit-prereq.sh` (verified feature-agnostic and idempotent), plus
the copied `cluster/static-certs/` overlay. **Same `UPSTREAM_SHA` pin — keep in lockstep
with `substrate-overcommit`** (both hooks share the one `ate-system` plane).
`requires.crds: [workerpools.ate.dev, actortemplates.ate.dev]`.

### D6 — Feature shape: jobset-style
Concierge Deployment `agent-hotel-concierge-deployment` (== `deployment_name`), dedicated
Gateway `agent-hotel-gw` (`gke-l7-global-external-managed`), HTTPRoute, Service 80→8000.
WorkerPool named `agent-hotel` (its reconciled `agent-hotel-deployment` never collides
with the concierge name).

### D7 — Digest pinning via one image + self-inspection
One image (`agent-hotel`) serves as both concierge and agent guest. At startup the
concierge reads its own pod's `status.containerStatuses[].imageID` (downward-API
`POD_NAME`/`POD_NAMESPACE`; tolerate `docker-pullable://` prefix and empty-while-pulling)
to get `…:latest@sha256:…`, then **creates the ActorTemplate at runtime**
(`guest-<digest12>`; templates are immutable, so a rebuild+rollout automatically yields a
new template and fresh golden snapshot). Consequence: no `actortemplate.yaml` in
`infra/` — only the WorkerPool is static.

### D8 — Resume latency vs the L7 LB
`infra/backend-policy.yaml`: `GCPBackendPolicy` with `timeoutSec: 180` on the concierge
Service (the ALB's default 30 s would cut cold resumes). Frontend renders an explicit
"💤 → waking… (checkpoint restore, ~30 s)" state instead of a spinner.

## Repository layout

```
spec.md                       This spec
feature.yaml                  Hub descriptor (see sketch below)
README.md                     Story, quickstarts (standalone + hub), caveats
ARCHITECTURE.md               Concierge/agent/ate-plane wiring; chat/yield/wake sequences
.gitignore  .dockerignore  .env.example
setup_infra.sh                Standalone: create cluster (gateway-api=standard, 3×e2-standard-4)
                              → apply cluster/*.yaml → run prereq; --delete/--delete-cluster
deploy_app.sh                 AR repo, docker build --platform linux/amd64 + push, namespace,
                              Gemini Secret from .env, render() infra/, rollout + Gateway IP wait
verify_setup.sh               Smoke: ready → gateway → /healthz → chat → wait linger →
                              suspended → chat again → history grew, boot_id unchanged
hub_router.py                 Thin: LIVE /config (+ensure_gemini_secret); MOCK canned /state + /chat
app/
  Dockerfile                  python:3.11-slim; app/ + ateapipb/ + frontend/; uvicorn concierge:app :8000
  requirements.txt            fastapi uvicorn httpx grpcio protobuf>=6.31 kubernetes pyjwt google-genai
  concierge.py                /healthz, static UI, /api/state, /api/guests/{name}/chat,
                              /api/guests/{name}/history, /api/open, /api/reset, /internal/yield, sweeper
  ate_client.py               gRPC ateapi client (projected-token creds, CA via K8s API, atespace "hotel")
  template_manager.py         imageID → digest → ensure ActorTemplate guest-<digest12>
  personas.py                 The 12 guests: name/emoji/persona/system prompt (single source of truth)
  hub_auth.py                 Copy of jobset's opt-in HS256 hub-JWT dependency
  agent/
    server.py                 The actor, HTTP :80 — /healthz /readyz, POST /chat (RAM history →
                              llm.reply → arm yield timer), GET /history; boot_id + resume counter
    llm.py                    google-genai (gemini-2.5-flash) or deterministic canned replies (no key)
ateapipb/                     Vendored proto stubs (copied from features/substrate-overcommit/ateapipb/)
frontend/
  index.html  app.js  style.css   Floor plan, worker lanes, meter, chat drawer; jobset dual-surface
                              pattern (config probe → gateway origin, healthz-gated; mock fallback); 2 s poll
infra/
  00-rbac.yaml                Concierge SA; Role (pods get/list, actortemplates CRUD, secret get);
                              ClusterRole+CRB for the servicedns-ca read
  backend-policy.yaml         GCPBackendPolicy timeoutSec 180
  deployment.yaml             agent-hotel-concierge-deployment (${AGENT_HOTEL_IMAGE}, probes /healthz:8000,
                              JWT_SECRET_KEY, downward API, projected SA-token volume)
  gateway.yaml  http-route.yaml  service.yaml
  workerpool.yaml             WorkerPool agent-hotel, replicas 3, sandboxClass gvisor,
                              ateomImage ${REGION}-docker.pkg.dev/${PROJECT_NAME}/${ARTIFACT_REGISTRY_REPO}/ateom-gvisor:latest
cluster/
  README.md
  ate-env-reader-rbac.yaml    ate-api-server may `get` Secret agent-hotel-gemini
  install-agent-hotel-prereq.sh   Copy of the overcommit prereq (same UPSTREAM_SHA)
  static-certs/kustomization.yaml  static-certs/patches.yaml
tests/
  conftest.py  test_manifests.py  test_agent_server.py  test_concierge_api.py  test_hub_router_mock.py
```

## Key content sketches

**feature.yaml**

```yaml
name: agent-hotel
title: Agent Hotel — persistent agents, checkpointed (Agent Substrate)
description: >-
  12 named chat agents live in a hotel with only 3 rooms (workers). Each agent keeps its
  whole conversation in RAM, yields its compute seconds after replying (checkpoint to
  GCS), and is transparently restored — memory intact, often on a different worker — the
  moment you message it again.
gke_features:
  - Zero-idle agents — self-suspend to snapshots seconds after replying
  - Resume-on-request — atenet-router restores RAM state transparently
  - 4x oversubscription — 12 persistent agents on a 3-pod WorkerPool
  - Secrets to sandboxes — GEMINI_API_KEY via ActorTemplate secretKeyRef
  - Golden snapshots — new guests check in instantly from a v0 checkpoint
paths:
  infra_dir: infra
  cluster_dir: cluster
  cluster_setup: cluster/install-agent-hotel-prereq.sh
  frontend_dir: frontend
  playroom_slug: agent-hotel
requires:
  crds: [workerpools.ate.dev, actortemplates.ate.dev]
deployment_name: agent-hotel-concierge-deployment
gateway: { name: agent-hotel-gw, class: gke-l7-global-external-managed }
build:
  - image: agent-hotel          # one image: concierge + agent guest
    context: .
    dockerfile: app/Dockerfile
template_vars: [NAMESPACE, PROJECT_NAME, REGION, ARTIFACT_REGISTRY_REPO, JWT_SECRET_KEY]
template_defaults:
  NAMESPACE: default
  AGENT_HOTEL_IMAGE: "${REGION}-docker.pkg.dev/${PROJECT_NAME}/${ARTIFACT_REGISTRY_REPO}/agent-hotel:latest"
hub_router: "hub_router:router"
```

**Runtime ActorTemplate** (built by `template_manager.py`):

```yaml
apiVersion: ate.dev/v1alpha1
kind: ActorTemplate
metadata: { name: guest-<digest12>, namespace: <POD_NAMESPACE> }
spec:
  pauseImage: "registry.k8s.io/pause:3.10.2@sha256:…"      # digest-pinned
  snapshotsConfig: { onPause: Full, location: "gs://<project>-substrate-snapshots/agent-hotel/" }
  containers:
    - name: guest
      image: "<own imageID: …/agent-hotel:latest@sha256:…>"  # digest-pinned (CRD requires '@')
      command: ["uvicorn", "agent.server:app", "--host", "0.0.0.0", "--port", "80"]
      readyz: { httpGet: { path: /readyz, port: 80 } }        # port is required upstream
      env:
        - { name: PORT, value: "80" }
        - { name: CONCIERGE_URL, value: "http://agent-hotel-concierge.<ns>.svc" }
        - { name: LINGER_SECONDS, value: "10" }
        - name: GEMINI_API_KEY
          valueFrom: { secretKeyRef: { name: agent-hotel-gemini, key: api-key, optional: true } }
  workerSelector: { matchLabels: { workload: agent-hotel } }
```

**Concierge API** (hub-JWT-gated via `hub_auth` except `/healthz`, static UI, `/internal/yield`):

- `GET /api/state` → `{mode, guests: [{name, persona, emoji, status: vacant|checking-in|awake|suspending|suspended|waking, worker_pod, messages, boot_id}], workers: [{pod, node, occupant}], meter: {guests_checked_in, workers, awake, suspended, oversubscription}}`
  from `ListActors(atespace="hotel")` + `ListWorkers` filtered to this WorkerPool.
- `POST /api/guests/{name}/chat {message}` → check-in on demand (`CreateActor` from the
  current template) → proxy through atenet-router → `{reply, boot_id, history_len, worker_pod, was_suspended}`.
- `GET /api/guests/{name}/history` → proxied (also demonstrates wake-on-read).
- `POST /api/open` → background check-in of all 12 guests (3 at a time), suspending each once ready.
- `POST /api/reset` → delete all actors + stale `guest-*` templates, re-ensure template (key-rotation path).
- `POST /internal/yield {guest}` → `SuspendActor` iff idle ≥ linger (tolerates FAILED_PRECONDITION/NOT_FOUND).

**Agent server**: `boot_id = uuid4()` at process start — its stability across
suspend/resume is the proof RAM survived; `/readyz` returns 200 immediately (the golden
actor must go Ready without calling Gemini).

## Hub-side changes (gke_all repo)

1. Push this repo: `git remote add origin https://github.com/ragoler/substrate.git`, commit, push `main`.
2. `git submodule add https://github.com/ragoler/substrate.git features/agent-hotel` + commit.
3. `showcase_admin/requirements-dev.txt`: **no change**. `infra/main-app.yaml`: **no change** (both verified).
4. Add `tests/integration/test_agent_hotel_mock.py` (list features → mock deploy → `/config`, `/state`, `/guests/<name>/chat` shapes → teardown).
5. Ops when going live: `./scripts/build_and_push.sh --feature agent-hotel`; re-run `./build_infra.sh` (applies `cluster/` + runs the prereq hook — a no-op where overcommit already installed the plane; `GEMINI_API_KEY` is already plumbed to the admin Deployment).

## Phased build order

1. **Logic offline** (no cluster, no docker): scaffold repo; `personas.py`, agent server +
   canned `llm.py`, concierge against a `FakeAteClient` (in-memory 3-slot hotel simulator,
   reused by tests); frontend served locally by the concierge. All `tests/` green.
2. **Hub MOCK**: `feature.yaml` + `hub_router.py`; mount into gke_all; Hub `MODE=MOCK`;
   playroom renders at `/agent-hotel/`; hub mock test + `test_manifests.py`
   (`${VAR}` coverage, no Namespace kind, names match descriptor).
3. **Real plane** (existing showcase cluster, scratch namespace): verify in order —
   (a) imageID digest + runtime ActorTemplate + golden snapshot Ready,
   (b) secretKeyRef resolution with the `cluster/` grant,
   (c) Gemini egress from inside a gVisor guest,
   (d) chat → yield → suspend → chat → resume with stable `boot_id`,
   (e) `GCPBackendPolicy` stretches the LB timeout past a cold resume.
4. **Standalone**: `setup_infra.sh` on a fresh cluster; `verify_setup.sh` end-to-end; docs.
5. **Hub LIVE + polish**: deploy via the Hub UI; LB-provisioning gate; worker-lane churn
   animation; feature.md §10 pre-merge checklist line-by-line.

## Verification

- `pytest tests/` in this repo (manifest rendering, agent server, concierge vs FakeAteClient, mock router).
- gke_all: `.venv/bin/pytest tests/` including the new mock integration test.
- `verify_setup.sh` — the automated standalone smoke test (the suspend/resume memory proof).
- Manual E2E: hub deploy → chat with a guest → card flips to 💤 after ~10 s → message it →
  "waking…" → the reply references the earlier conversation, `boot_id` unchanged, worker
  usually different.

## Risks / must-verify during implementation

1. **Gemini egress from gVisor actors** — precedent exists (upstream claude-code-multiplex
   calls api.anthropic.com from actors), but verify early; fallback is the `/llm` proxy (D3).
2. **`imageID` format** on GKE containerd (`repo:tag@sha256:…`; handle `docker-pullable://`
   prefix and empty-while-pulling).
3. **Template lifecycle**: semantics of deleting an old `guest-<digest>` template while its
   actors still exist — GC old templates only in `/api/reset`.
4. **Slot contention**: all 3 workers busy + a 4th guest woken — confirm the router blocks
   and succeeds within 120 s (the sweeper frees a slot) rather than erroring.
5. **Key + conversation RAM persisted in GCS snapshots**; key rotation = reset (document).
6. **Upstream SHA drift**: keep the prereq pin in lockstep with `substrate-overcommit` —
   bumping either alone changes the shared `ate-system` plane for both.
7. **Cluster-scoped RBAC from `infra/` leaks past namespace teardown** — deterministic
   names, re-apply-safe; note in README.
8. **Projected-token acceptance** re-check once on the live plane before removing the
   TokenRequest fallback from `ate_client.py`.
