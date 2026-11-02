# Agent Hotel

*A demo that teaches [Agent Substrate](https://github.com/agent-substrate/substrate) by
letting you chat with 12 AI agents who live in a hotel with only 3 rooms.
(Those are the starting numbers — the roster holds 20 personas, and both the
admitted-guest count and the room count are adjustable live from the UI.)*

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

Each guest is a real, running AI agent (Gemini) with a distinct personality.
What you do:

1. **Chat with a guest.** Click Amara, ask her to plan a trip to Kyoto. She replies in
   character. Her card shows which room (worker pod) she's occupying.
2. **Watch her go to sleep.** About 10 seconds after her last reply, Amara decides she's
   idle and *yields her room*: her whole memory — the conversation, every variable in her
   process — is checkpointed to cloud storage, and the room frees up for someone else.
   Her card flips to 💤.
3. **Wake her just by talking to her.** Send another message: "actually, make it Osaka."
   You'll see a "waking Amara… (restoring checkpoint, ~10–30 s)" state, and then she answers
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
| **Asynchronous suspend** | The room is free as soon as the snapshot is on the node (sub-second); atelet uploads it to object storage in the background, after which the actor wakes anywhere | A guest shows 😴 while its snapshot uploads and 💤 once it is in GCS |
| **Oversubscription** | Many actors mapped onto few workers | 12 guests, 3 rooms, 4× density |
| **Golden snapshot** | A checkpoint taken at template creation and published as a Tag; new actors start from it instantly instead of cold-booting | New guests "check in" in moments |
| **Egress policy** | Deny by default, down to DNS: an actor reaches exactly the hostnames its EgressPolicy names, through the egress gateway | Each guest may reach the concierge and the Gemini API, nothing else |
| **Credential injection** | The egress gateway terminates the actor's TLS for an allowed hostname and replaces a header with a secret only *it* may read | Guests call Gemini with a placeholder key; the real key never enters a sandbox or a snapshot |
| **Crash + revert** | A failed restore crashes the actor; `RevertActor` rewinds it to its last durable snapshot | A crashed guest is rewound on its next message |

## Architecture

Three layers, from what you see down to what does the magic:

```
   Browser ──── hotel floor-plan UI (static HTML/JS)
      │
      ▼
   Concierge (FastAPI, a normal pod — the only "smart" service we write)
      │  • serves the UI and the /api/state, /chat endpoints
      │  • tells Substrate: create guests + their egress policy, suspend, revert,
      │    delete (gRPC to ate-api-server, authorized by Substrate's access policies)
      │  • forwards each chat message "to the guest by name" via Substrate's router
      │    (mTLS with its PodCertificate; the router admits only allowlisted identities)
      ▼
   Agent Substrate (the ate control plane, installed once per cluster)
      │  • ate-api-server — control API (create/suspend/resume actors)
      │  • atenet-router  — resolves `ate-target-actor: hotel/amara` to wherever
      │                     Amara is, waking her first if she's suspended
      │  • atenet-egress  — the only way out of a sandbox: enforces each guest's
      │                     EgressPolicy and injects the Gemini key
      │  • WorkerPool     — the 3 warm worker pods ("rooms"), gVisor-sandboxed
      │  • GCS bucket     — where sleeping guests' checkpoints live
      ▼
   Guest agent (tiny HTTP server inside a sandboxed actor)
         • keeps conversation history in plain Python memory — no database
         • calls the Gemini API to reply in persona — with a placeholder key
         • ~10 s after its last reply, tells the concierge "I'm done, tuck me in"
```

The key flows:

- **Chat**: browser → concierge → Substrate router (addressed by guest *name*, not pod) →
  the guest's actor → Gemini → reply. The concierge never needs to know where a guest
  physically is.
- **Sleep (zero-idle)**: the guest itself decides it's idle and asks to be tucked in; the
  concierge executes the authenticated `SuspendActor` on its behalf (asynchronous: the
  room is free once the snapshot is on the node, ~0.5 s; atelet uploads it to GCS in the
  background, after which the guest can wake on any room), and a background sweeper
  catches any guest that forgets.
- **Wake**: a new message for a sleeping guest hits the router, which restores the
  checkpoint onto any free room (evicting an idle guest if the hotel is full), then
  delivers the message. RAM, conversation, `boot_id` — all intact.

A few deliberate design choices, in plain terms:

- **The guest holds its memory in RAM on purpose.** No database, no session store —
  demonstrating that Substrate's checkpointing *is* the persistence layer.
- **A guest learns its identity from Substrate itself**: the ActorTemplate projects the
  actor's metadata into the sandbox (a `systemInfo` volume), so the guest reads its own
  name from a file. The concierge still sends `X-Actor-Name` with every request, which
  powers a **misdirect guard**: should the router ever proxy a request to a room whose
  previous occupant is still bound, the guest answers `421 Misdirected Request` instead
  of impersonating someone else, and the concierge retries until the routing settles.
- **The concierge retries transient data-path failures** (cold image pulls on a room's
  node, full-hotel resume contention, misdirects) for up to ~30 s — long enough for an
  idle guest to yield a room — before surfacing an error. `deploy_app.sh` additionally
  warms every room after a rebuild (one throwaway chat per room pre-pulls the new image
  onto each node) and then resets, so first real interactions are fast.
- **No secrets in snapshots — or in sandboxes.** Substrate blocks the metadata server
  and everything else a guest did not get an egress rule for, so the guest cannot fetch
  a credential itself. Instead the guests send the Gemini API key header with a
  placeholder value and the egress gateway replaces it with the real key, read from a
  Kubernetes Secret by Substrate's credential provider. A guest cannot read, snapshot
  or exfiltrate its own key. If no key is configured, the guests fall back to scripted
  in-character replies so the demo always works.
- **One container image plays both roles** (concierge and guest), and the concierge
  registers the guest template with Substrate at startup (an ActorTemplate created
  through the ate API) using its own image digest — Substrate requires digest-pinned
  images, and this keeps rebuilds automatic. A new template gets a new golden
  snapshot, published as a Tag; the UI shows whether it is ready. Each guest gets its
  EgressPolicy at check-in, before its first message can wake it.
- **Waking takes ~1 s from a node-local snapshot and a few seconds from GCS** (longer
  the first time a node has to pull the guest image). The UI is honest about it — a
  "waking…" state, not a spinner — and the load balancer timeout is raised so the
  request survives the wait.

## How it runs

Today the demo runs **standalone**: three scripts — `setup_infra.sh` (create a GKE
cluster and install the Substrate control plane), `deploy_app.sh` (build and deploy the
hotel, then warm each room and reset), `verify_setup.sh` (an automated end-to-end
proof: chat → sleep → wake → memory intact). Configuration comes from a `.env` file;
`ATE_MODE=fake` runs the whole hotel in-memory for local dev and tests.

*(Planned, not yet implemented: mounting this repo into the GKE Feature Showcase Hub
(`gke_all`) as a `features/substrate-hotel/` submodule with a dashboard card and mock
mode. The cluster prerequisite is the same shape as the hub's existing
`substrate-overcommit` feature — upstream's own installer — but this repo pins a newer
upstream commit (see `cluster/README.md`); the hub feature would need the same bump.)*

## Repository tour

| Path | What it is |
| --- | --- |
| `app/agent/` | The guest: a tiny HTTP chat server + Gemini client |
| `app/concierge.py` | The concierge API and UI server |
| `app/ate_client.py` | The gRPC/router client for Substrate's control + data planes, plus the in-memory fake |
| `app/template_manager.py` | Builds the digest-pinned ActorTemplate (an ate API resource) and the guests' EgressPolicy |
| `app/personas.py` | The guest roster — 20 personas (names, personalities, system prompts); 12 admitted by default |
| `frontend/` | The hotel floor-plan UI |
| `infra/` | Kubernetes manifests `deploy_app.sh` applies: concierge, WorkerPool, gateway |
| `cluster/` | One-per-cluster prerequisites: the Substrate control-plane installer |
| `tests/` | Offline test suite (the whole hotel runs against a fake Substrate for tests) |

## Build plan (status)

1. **Offline first** ✅ — the entire hotel (concierge, guests, UI) runs locally against a
   fake in-memory Substrate; full test suite, no cluster needed.
2. **Real Substrate plane** ✅ — golden snapshots, Gemini calls from inside the sandbox,
   and the sleep/wake cycle with a stable `boot_id` all verified end to end
   (`verify_setup.sh` automates the proof).
3. **Standalone path** ✅ — fresh cluster from scratch via the three scripts.
4. **Hub integration (mock mode, dashboard card, live deploy)** — planned, not started.

The original open risk (can a sandboxed guest reach an LLM at all?) is resolved the way
Substrate now intends: a guest reaches exactly the hostnames its EgressPolicy allows,
through the egress gateway, and the gateway injects the credential. The concierge
`/llm` proxy fallback also exists (`LLM_MODE=proxy`).
