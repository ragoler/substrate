# 🏨 Agent Hotel — an Agent Substrate demo

Twelve stateful AI hotel guests share three rooms. Each guest keeps its whole
conversation in plain process RAM, **yields its room ~10 seconds after
replying** (checkpoint → GCS), and is **transparently restored — memory
intact, usually in a different room — the moment you message it again**.

That's [Agent Substrate](https://github.com/agent-substrate/substrate) doing
the work: suspend/resume checkpointing, resume-on-request routing, and 4×
oversubscription of agents onto warm worker pods. Read **[spec.md](spec.md)**
for the full story, concepts, and architecture.

## Run it (standalone, on GKE)

Requirements: `gcloud`, `kubectl`, `docker`, `python3`, plus the toolchain the
Substrate installer builds with: `git`, `go`, [`ko`](https://ko.build),
`openssl`.

```bash
cp .env.example .env      # set PROJECT_ID (and anything else you like)

./setup_infra.sh          # 1. GKE cluster (WI + Gateway API) + the ate plane (~15 min)
./deploy_app.sh           # 2. build + push the image, deploy the hotel
./verify_setup.sh         # 3. automated chat → sleep → wake → memory-intact proof
```

`deploy_app.sh` prints the Gateway IP — open `http://<ip>/` and:

1. Click a guest, say hello, tell them something to remember.
2. Watch the card flip to 💤 about 10 s after the reply (the guest yielded its
   room; its RAM is now a snapshot in GCS).
3. Message them again — "waking… (~30 s)" — and the reply remembers
   everything, with the **same `boot_id`** and often a different room.
4. "Open the hotel" checks in all 12 guests; watch the 3 rooms churn.

Gemini replies use **Vertex AI with Workload Identity** — no API key
anywhere, nothing secret ever frozen into a snapshot. Set
`GOOGLE_GENAI_USE_VERTEXAI=FALSE` in `.env` to skip Vertex entirely; guests
then use scripted in-character replies (every Substrate mechanic still works).

Teardown: `./setup_infra.sh --delete-cluster`.

## Develop / test offline (no cluster, no docker)

`ATE_MODE=fake` swaps the real Substrate plane for an in-memory hotel with
the same observable semantics (3 slots, suspend frees a slot, chat wakes a
sleeping guest, RAM survives):

```bash
pip install fastapi uvicorn httpx pytest pyyaml

pytest tests/                                   # the whole suite is offline

ATE_MODE=fake LLM_MODE=canned uvicorn app.concierge:app --port 8000
# open http://localhost:8000/
```

## Layout

| Path | What it is |
| --- | --- |
| `app/agent/` | The guest: a tiny HTTP chat server (RAM history) + Gemini client |
| `app/concierge.py` | The concierge: UI/API server, actor lifecycle, yield executor, sweeper |
| `app/ate_client.py` | Real gRPC/router client for the ate plane + the in-memory fake |
| `app/template_manager.py` | Registers the digest-pinned ActorTemplate from the pod's own imageID |
| `app/personas.py` | The 12 guests (names, personalities, prompts) |
| `frontend/` | Hotel floor-plan UI |
| `infra/` | Per-namespace manifests: RBAC, concierge, Service/Gateway/HTTPRoute, WorkerPool |
| `cluster/` | One-per-cluster Substrate control-plane installer ([details](cluster/README.md)) |
| `ateapipb/` | Vendored ate-api gRPC stubs |
| `tests/` | Offline test suite |

## Operational notes

- **Waking takes ~30 s** (checkpoint restore from GCS). The UI says so
  honestly, and `infra/backend-policy.yaml` stretches the LB timeout to 180 s
  so wake requests aren't cut off.
- **Conversations live in snapshots**: suspended guests' RAM (including chat
  history) is stored in `gs://<project>-substrate-snapshots/substrate-hotel/`.
  "Reset" deletes the actors; the bucket outlives teardown.
- The installer pins the upstream Substrate commit (see
  [cluster/README.md](cluster/README.md)); the `ate-system` plane is shared
  cluster-wide.
- Two cluster-scoped RBAC objects (`substrate-hotel-servicedns-ca-reader`)
  survive a namespace teardown; re-applies are safe, delete them manually if
  you want a spotless cluster.
