# Agent Hotel

*A demo that teaches [Agent Substrate](https://github.com/agent-substrate/substrate) by
letting you chat with 12 AI agents who live in a hotel with only 3 rooms.*

---

## The idea in one paragraph

AI agents spend most of their life waiting for the next message. Keeping a pod warm for
every agent wastes almost all of its compute. Agent Substrate solves this by
**checkpointing an agent's entire memory to storage the moment it goes idle, and
restoring it — mid-thought, on any available machine — the moment someone needs it
again**. Agent Hotel makes that visible and fun: twelve chatty hotel guests share three
rooms, and you can watch them fall asleep, get evicted to the cloud, and wake up
somewhere else without ever losing the thread of your conversation.

## The experience

You open the **hotel floor plan**:

```
  AGENT HOTEL                        compute saved: 75%  (12 guests / 3 rooms)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  Rooms      [ Room 1: Marisol 🟢 ] [ Room 2: empty ] [ Room 3: Kai 🟢 ]
 ├──────────────────────────────────────────────────────────────────────┤
 │  🟢 Marisol   the poet          awake     Room 1                     │
 │  💤 Viktor    the chess coach   sleeping  —                          │
 │  💤 Amara     the travel guide  sleeping  —                          │
 │  🟢 Kai       the comedian      awake     Room 3                     │
 │  💤 Nadia     the historian     sleeping  —          ... (12 total)  │
 └──────────────────────────────────────────────────────────────────────┘
```

Each guest is a real, running AI agent (Gemini on Vertex AI) with a distinct personality.
What you do:

1. **Chat with a guest.** Click Amara, ask her to plan a trip to Kyoto. She replies in
   character. Her card shows which room (worker pod) she's occupying.
2. **Watch her go to sleep.** About 10 seconds after her last reply, Amara decides she's
   idle and *yields her room*: her whole memory — the conversation, every variable in her
   process — is checkpointed to cloud storage, and the room frees up for someone else.
   Her card flips to 💤.
3. **Wake her just by talking to her.** Send another message: "actually, make it Osaka."
   You'll see a "waking Amara… (restoring checkpoint, ~30 s)" state, and then she answers
   **remembering the entire Kyoto conversation** — even though her process was frozen,
   shipped to storage, and restored, usually into a *different room* than before.
4. **Fill the hotel.** Hit "Open the hotel" to check in all 12 guests and chat with
   several at once. Only 3 can be awake at a time, so you watch the rooms churn:
   whoever's needed wakes up, whoever's idle gets tucked away. The **compute-saved
   meter** shows the point of it all: 12 persistent, stateful agents are living on 3
   pods' worth of hardware.

The proof that nothing is faked: every guest carries a `boot_id` minted once at process
start. It survives sleep, storage, and migration between rooms — the same process really
did come back to life.

## What it teaches about Substrate

| Concept | What Substrate means by it | Where you see it in the demo |
| --- | --- | --- |
| **Actor** | A stateful process instance — the unit that gets suspended and resumed | Each hotel guest |
| **Worker / WorkerPool** | A pool of warm pods; each hosts at most one actor at a time | The 3 hotel rooms |
| **Suspend (checkpoint)** | Freeze the actor's RAM + filesystem into a snapshot in GCS, freeing the worker | A guest going 💤 |
| **Resume-on-request** | Traffic addressed to a suspended actor transparently restores it first | Messaging a sleeping guest just… works |
| **Zero-idle** | The actor itself decides to yield when it has nothing to do | Guests go to sleep ~10 s after replying |
| **Oversubscription** | Many actors mapped onto few workers | 12 guests, 3 rooms, 4× density |
| **Golden snapshot** | A version-0 checkpoint taken at template creation; new actors start from it instantly instead of cold-booting | New guests "check in" in moments |
| **Keyless credentials** | Nothing secret is baked into snapshots; agents authenticate to Vertex AI with Workload Identity tokens fetched fresh after every resume | Guests call Gemini with no API key anywhere |

## Architecture

Three layers, from what you see down to what does the magic:

```
   Browser ──── hotel floor-plan UI (static HTML/JS)
      │
      ▼
   Concierge (FastAPI, a normal pod — the only "smart" service we write)
      │  • serves the UI and the /api/state, /chat endpoints
      │  • tells Substrate: create / suspend / resume guests (gRPC to ate-api-server)
      │  • forwards each chat message "to the guest by name" via Substrate's router
      ▼
   Agent Substrate (the ate control plane, installed once per cluster)
      │  • ate-api-server — control API (create/suspend/resume actors)
      │  • atenet-router  — resolves "amara.hotel.…" to wherever Amara is,
      │                     waking her first if she's suspended
      │  • WorkerPool     — the 3 warm worker pods ("rooms"), gVisor-sandboxed
      │  • GCS bucket     — where sleeping guests' checkpoints live
      ▼
   Guest agent (tiny HTTP server inside a sandboxed actor)
         • keeps conversation history in plain Python memory — no database
         • calls Gemini (Vertex AI, Workload Identity) to reply in persona
         • ~10 s after its last reply, tells the concierge "I'm done, tuck me in"
```

The key flows:

- **Chat**: browser → concierge → Substrate router (addressed by guest *name*, not pod) →
  the guest's actor → Gemini → reply. The concierge never needs to know where a guest
  physically is.
- **Sleep (zero-idle)**: the guest itself decides it's idle and asks to be suspended; the
  concierge executes the authenticated suspend call on its behalf, and a background
  sweeper catches any guest that forgets. Checkpoint goes to GCS; the room frees up.
- **Wake**: a new message for a sleeping guest hits the router, which restores the
  checkpoint onto any free room (evicting an idle guest if the hotel is full), then
  delivers the message. RAM, conversation, `boot_id` — all intact.

A few deliberate design choices, in plain terms:

- **The guest holds its memory in RAM on purpose.** No database, no session store —
  demonstrating that Substrate's checkpointing *is* the persistence layer.
- **No secrets in snapshots.** The agents use Vertex AI with Workload Identity (the same
  pattern as the hub's `agent-sandbox` feature), so credentials are re-fetched after
  every resume instead of being frozen into a checkpoint. If Vertex isn't available, the
  guests fall back to scripted in-character replies so the demo always works.
- **One container image plays both roles** (concierge and guest), and the concierge
  registers the guest template with Substrate at startup using its own image digest —
  Substrate requires digest-pinned images, and this keeps rebuilds automatic.
- **Waking takes ~30 seconds** (checkpoint restore from GCS). The UI is honest about it —
  a "waking…" state, not a spinner — and the load balancer timeout is raised so the
  request survives the wait.

## How it runs

Two modes, same code:

- **Inside the GKE Feature Showcase Hub** (`gke_all`): this repo mounts as a git
  submodule at `features/substrate-hotel/`. The hub deploys it into its own namespace
  from a dashboard card; the playroom UI appears at `/substrate-hotel/`. The hub's
  offline mock mode shows a canned hotel with scripted replies, so the UI works even
  with no cluster at all.
- **Standalone**: three scripts — `setup_infra.sh` (create a GKE cluster and install the
  Substrate control plane), `deploy_app.sh` (build and deploy the hotel),
  `verify_setup.sh` (an automated end-to-end proof: chat → sleep → wake → memory
  intact). Configuration comes from a `.env` file.

Both modes share the same cluster prerequisite: the Agent Substrate control plane
(pinned to the same upstream commit the hub's existing `substrate-overcommit` feature
uses), installed once per cluster by an idempotent script.

## Repository tour

| Path | What it is |
| --- | --- |
| `app/agent/` | The guest: a tiny HTTP chat server + Gemini client |
| `app/concierge.py` | The concierge API and UI server |
| `app/ate_client.py` | The gRPC client that talks to Substrate's control plane |
| `app/personas.py` | The 12 guests — names, personalities, system prompts |
| `frontend/` | The hotel floor-plan UI |
| `infra/` | Kubernetes manifests the hub (or `deploy_app.sh`) applies: concierge, WorkerPool, gateway |
| `cluster/` | One-per-cluster prerequisites: the Substrate control-plane installer |
| `hub_router.py`, `feature.yaml` | The hub integration: feature descriptor + mock mode |
| `tests/` | Offline test suite (the whole hotel runs against a fake Substrate for tests) |

## Build plan (summary)

1. **Offline first** — the entire hotel (concierge, guests, UI) runs locally against a
   fake in-memory Substrate; full test suite, no cluster needed.
2. **Hub mock mode** — the card and playroom render in the hub with canned data.
3. **Real Substrate plane** — verify the risky bits in order: golden snapshots, Vertex
   calls from inside the sandbox, the sleep/wake cycle with a stable `boot_id`.
4. **Standalone path** — fresh cluster from scratch via the three scripts.
5. **Hub live + polish** — deploy from the dashboard, animations, checklist.

The main open risk: whether guests can reach Vertex AI from inside their gVisor sandbox
(upstream's Claude Code demo suggests yes). If not, the concierge proxies the LLM call —
one environment variable, no redesign.
