# 🏨 Agent Hotel — an Agent Substrate demo

Twelve stateful AI hotel guests share three rooms. Each guest keeps its whole
conversation in plain process RAM, **yields its room ~10 seconds after
replying** (checkpoint → node, then GCS), and is **transparently restored —
memory intact, usually in a different room — the moment you message it again**.

That's [Agent Substrate](https://github.com/agent-substrate/substrate) doing
the work: suspend/resume checkpointing, resume-on-request routing, per-actor
egress policy with credential injection, and 4× oversubscription of agents
onto warm worker pods. Read **[spec.md](spec.md)** for the full story,
concepts, and architecture.

## Run it (standalone, on GKE)

Requirements: `gcloud`, `kubectl`, `docker`, `python3`, `git`, `go`, `jq`.
The Substrate plane is installed from upstream's **release images**
(`ghcr.io/agent-substrate/substrate`, release pinned in `lib.sh`); nothing is
built from source.

```bash
cp .env.example .env      # set PROJECT_ID, a Gemini key (and anything else you like)

./setup_infra.sh          # 1. GKE cluster (WI, Dataplane V2, Gateway API, managed OTel) + Agent Substrate (~15 min)
./deploy_app.sh           # 2. build + push the image, deploy the hotel
./verify_setup.sh         # 3. automated chat → sleep → wake → memory-intact proof
```

`deploy_app.sh` prints the Gateway IP — open `http://<ip>/` and:

1. Click a guest, say hello, tell them something to remember. Open another
   guest while the first is still replying — conversations run in parallel,
   one room each; the transcript you see is kept by your browser, so opening
   a drawer never wakes a sleeping guest.
2. Watch the card flip to 😴 about 10 s after the reply: the guest yielded
   its room. Its RAM is a snapshot on the node first (the room is already
   free) and 💤 in GCS once atelet's background upload lands.
3. Message them again — about a second from a node-local snapshot, a few
   seconds from GCS — and the reply remembers everything, with the **same
   `boot_id`** and often a different room.
4. "Open the hotel" checks in all admitted guests; watch the rooms churn.

**Sizing is flexible.** `.env` sets the starting shape (`GUEST_COUNT=12`
guests from a 20-persona roster, `WORKER_REPLICAS=3` rooms), and the UI
changes it live: **+ Guest** admits the next persona from the roster, and
**+ Room / − Room** rescale the WorkerPool on the spot (the ate-controller
resizes the worker Deployment). Scaling rooms *down* below the number of
awake guests is itself a demo: the evicted guest is suspended on the way out
and comes back, memory intact, on its next message. A room's capacity is
sized to one guest (`infra/workerpool.yaml`); raise it and Substrate packs
several guests per room.

**Node provisioning is flexible too.** `USE_COMPUTE_CLASS=TRUE` (the default)
enables Node Auto-Provisioning plus a Custom Compute Class
([cluster/compute-class.yaml](cluster/compute-class.yaml)): the rooms select
`cloud.google.com/compute-class: hotel-rooms`, and GKE creates `c3-standard-4`
nodes for them on demand — "+ Room" scales the WorkerPool *and*, when
capacity runs out, the cluster underneath it. One machine type on purpose: a
gVisor memory snapshot restores in full only on the CPU platform it was taken
on. Set `USE_COMPUTE_CLASS=FALSE` to run the rooms on the default pool (then
pick a single-platform `MACHINE_TYPE`).

**The guests hold no credential.** They call the Gemini API with a
placeholder key; Substrate's egress gateway — the only path out of a
sandbox — terminates their TLS for that one hostname and replaces the
`x-goog-api-key` header with the real key, read from a Kubernetes Secret by
Substrate's credential provider. Nothing secret is in the sandbox, so nothing
secret is in a snapshot, and a guest cannot read or leak its own key. Set
`GEMINI_API_KEY` or `GEMINI_API_KEY_RESOURCE` in `.env`; with neither, guests
use scripted in-character replies (every Substrate mechanic still works).

Teardown: `./setup_infra.sh --delete-cluster`.

## Develop / test offline (no cluster, no docker)

`ATE_MODE=fake` swaps the real Substrate plane for an in-memory hotel with
the same observable semantics (3 slots, suspend frees a slot, chat wakes a
sleeping guest, RAM survives, crashed guests are reverted):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# (behind a private pip mirror you aren't authenticated to? add: -i https://pypi.org/simple)

pytest tests/                                   # the whole suite is offline

ATE_MODE=fake LLM_MODE=canned uvicorn app.concierge:app --port 8000
# open http://localhost:8000/
```

If local `pip` can't reach any index at all, run the suite inside the app
image instead (its build already installed the runtime deps):

```bash
docker build --platform linux/amd64 -t hotel-test -f app/Dockerfile .
docker run --rm -v "$PWD":/work -w /work hotel-test \
  sh -c 'pip install -q pytest pyyaml && python -m pytest -q tests/'
```

## Layout

| Path | What it is |
| --- | --- |
| `app/agent/` | The guest: a tiny HTTP chat server (RAM history) + Gemini client |
| `app/concierge.py` | The concierge: UI/API server, actor lifecycle (check-in with egress policy, yield, revert, reset), sweeper |
| `app/ate_client.py` | Real gRPC/mTLS-router client for the ate plane + the in-memory fake |
| `app/template_manager.py` | Builds the digest-pinned ActorTemplate (an ate API resource) and the guests' EgressPolicy |
| `app/personas.py` | The guest roster: 20 personas (12 admitted at start; `+ Guest` adds more) |
| `frontend/` | Hotel floor-plan UI |
| `infra/` | Per-namespace manifests: RBAC, concierge, Service/Gateway/HTTPRoute, WorkerPool |
| `cluster/` | One-per-cluster Substrate control-plane installer + install document ([details](cluster/README.md)) |
| `ateapipb/` | Vendored ate-api gRPC stubs (regenerated per release, see `cluster/README.md`) |
| `tests/` | Offline test suite |
| `.tools/` | `kubectl-ate` built from the pinned release (by the installer) |

## Operational notes

- **Waking takes ~1 s from a node-local snapshot and a few seconds from
  GCS** (longer the first time a node pulls the image). The UI says so
  honestly, and `infra/backend-policy.yaml` stretches the LB timeout to 180 s
  so wake requests aren't cut off. The concierge also retries transient
  router failures (cold pulls, full-hotel contention, misdirects) for ~30 s
  before surfacing an error.
- **A crashed guest is rewound, not lost.** If a restore fails (or a guest
  exceeds its memory limit) Substrate marks the actor CRASHED; the
  concierge calls `RevertActor` on the guest's next message, which returns
  it to its last durable snapshot, and the chat wakes it from there.
- **`deploy_app.sh` ends warm and clean**: after the rollout it chats once
  per room (pre-pulling the rebuilt image onto each room's node), then
  resets the hotel. Rollouts are hitless — the concierge drains for 60 s on
  termination so the load balancer never serves "no healthy upstream" — but
  the very first deploy still waits ~3–5 min for Gateway programming.
- **The public LB has brief 503 windows** (GKE Gateway re-syncs, scanner
  churn on its connection pool). The UI retries with backoff and keeps the
  last floor plan; it only reports "reconnecting" after several failed polls.
- **GKE auto-upgrades restart everything** (control plane, nodes, the whole
  ate plane) and guests recover from snapshots — but set a cluster
  maintenance window if you demo at unpredictable hours.
- **Conversations live in snapshots**: sleeping guests' RAM (including chat
  history) is stored under `gs://<bucket>/agent-hotel/`. "Reset" deletes the
  actors (and their snapshots); the bucket outlives teardown.
- **Operate the plane with `kubectl ate`** (`.tools/kubectl-ate`): `get
  actors -a hotel`, `get workers`, `get egress-policy <guest> -a hotel`,
  `suspend/resume/revert actor`, `proxy actor <guest> -a hotel` to reach a
  guest from your laptop through the router's mTLS CONNECT tunnel.
- The installer pins the upstream Substrate **release** (`UPSTREAM_REF` in
  `lib.sh`, see [cluster/README.md](cluster/README.md)); `deploy_app.sh`
  refuses a plane/repo mismatch. The `ate-system` plane is shared
  cluster-wide.
