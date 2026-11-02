# Agent Hotel: a tutorial

*How a stateful multi-agent demo is built on Agent Substrate, what Substrate
does for it, and how to read the code.*

Agent Hotel is a small web app: twelve chatty AI guests, each a real running
process with a personality and a memory, share three "rooms". You talk to a
guest, it answers, and ten seconds later it goes to sleep and gives up its
room. Talk to it again and it wakes up, in a different room if it has to,
remembering everything. The point of the demo is that the guest never saved
anything: its memory is plain process RAM, and Agent Substrate is what makes
that RAM survive.

This tutorial walks the system top down. Part 1 is the experience and the
ideas. Part 2 maps each idea onto a Substrate API and the code that calls it.
Part 3 is the operational side: how the cluster and the Substrate control plane
are set up, and how you run and debug the thing. Part 4 is what we learned the
hard way.

---

## Part 1: The idea

### Agents are idle most of the time

An agent waits: for a user, for a model, for a tool. A pod per agent keeps
RAM and CPU reserved for a process that is mostly asleep. Substrate's answer
is to treat an agent as an **actor**: a sandboxed process whose entire
state, memory included, can be checkpointed to storage when it goes idle and
restored on any free machine the moment someone needs it. Many actors then
share few machines, and the checkpoint *is* the persistence layer.

The hotel makes this visible:

| Hotel | Substrate |
| --- | --- |
| A guest | An **Actor**: a stateful process instance |
| A room | A **Worker**: a warm, gVisor-sandboxed pod in a **WorkerPool** |
| Falling asleep | **SuspendActor**: checkpoint RAM + filesystem, free the worker |
| Waking up when you talk | **Resume-on-request**: the router restores the actor before proxying |
| The guest's own "I'm done" | **Zero-idle**: the actor decides when to yield |
| 12 guests, 3 rooms | **Oversubscription** |
| New guests check in instantly | The **golden snapshot**: a published checkpoint of a freshly booted template |
| A guest can only phone two places | A per-actor **EgressPolicy**, deny by default down to DNS |
| The guest never holds a key | **Credential injection** at the egress gateway |

### The proof that nothing is faked

Every guest mints a random `boot_id` the first time it answers and keeps it
in a module global. If a guest's process were ever restarted, the `boot_id`
would change. It never does: after sleep, storage, and a move to another
room, the reply carries the same `boot_id` and the same conversation history.
The UI shows it on every message.

---

## Part 2: The architecture, idea by idea

Three layers:

```
Browser  ──►  Concierge (FastAPI, app/concierge.py)
                 │  gRPC (control plane)            HTTPS + mTLS (data plane)
                 ▼                                   ▼
             ate-api-server ◄──────────────────── atenet-router ──► Guest actor (app/agent/)
             (actors, templates, policies)         (resume-on-request)     │
                                                                           ▼ atunnel
                                                                    atenet-egress ──► Gemini API
                                                                    (policy + key injection)
```

The concierge is the only service we deploy. The guests are Substrate actors
created from one template. Everything between them is Substrate.

### 2.1 One image, two roles

The concierge and the guest share one container image (`app/Dockerfile`).
The concierge serves the UI on port 8000; the guest runs the same image with
a different command (`app.agent.server:app` on port 80, the port the router
proxies to). Sharing the image lets the concierge do something neat at
startup: it reads its *own* pod's `imageID`, which the kubelet reports with a
digest, and registers that digest-pinned image as the guest template. A
rebuild and rollout produce a new template automatically.

Code: `app/template_manager.py:read_own_image_ref_sync`,
`app/concierge.py:_ensure_template_loop`.

### 2.2 The ActorTemplate: what a guest is

An ActorTemplate is a Substrate API resource (not a Kubernetes object) that
says what process to run, how big it is, and how to snapshot it. The
concierge builds it as a protojson document and sends it with
`CreateActorTemplate`:

```yaml
metadata: {atespace: hotel, name: guest-<fingerprint>}
workerSelector: {matchLabels: {workload: substrate-hotel}}   # which pools may host it
containers:
- name: guest
  image: …/substrate-hotel@sha256:…                           # digest-pinned, required
  command: [uvicorn, --app-dir, /srv, app.agent.server:app, --host, 0.0.0.0, --port, "80"]
  wakeupProbe: {httpGet: {path: /readyz, port: 80}, timeoutSeconds: 60}
  env: [...plain values only...]
  volumeMounts: [{name: system-info, mountPath: /run/ate}]
volumes:
- name: system-info
  systemInfo:
    dataSources:
    - actorMetadata: {items: [{field: ACTOR_METADATA_FIELD_NAME, path: actor/name}]}
    - trustBundle: {names: [egress-mitm.ate.dev, system-roots.ate.dev],
                    path: egress-trust-bundle/trust-bundle.pem}
resources: {limits: {cpuMillicores: 1000, memoryBytes: 536870912}}
snapshotConfig: {preferredFidelity: SNAPSHOT_FIDELITY_MEMORY}
```

Things worth noticing:

- **`wakeupProbe`**: `ResumeActor` returns only once this endpoint answers
  200, so the router never forwards a message into a sandbox that is still
  booting. It also lets Substrate skip its default 20 s "settle" delay before
  taking the golden snapshot. A probe that fails crashes the actor, so
  `/readyz` must be trivially cheap.
- **`resources.limits`** is mandatory and is what a room's capacity is
  measured against. The WorkerPool gives each room 1000 millicores; a guest
  declares 1000 millicores; therefore one guest per room, which is the whole
  premise of the demo. Raise the room's limits and Substrate packs several
  guests into one room.
- **`systemInfo` volumes** are how Substrate hands facts into a sandbox. The
  guest reads its own name from `/run/ate/actor/name`, and gets a trust bundle
  it needs for the egress story below.
- **`SNAPSHOT_FIDELITY_MEMORY`**: snapshots include process memory. The
  alternatives keep only the filesystem or only volumes and cold-boot the
  process.
- **Immutability**: templates cannot change. The template name fingerprints
  the image digest *and* the configuration, so a config change (a new model,
  a different linger time) yields a new template, exactly like a rebuild.

Code: `app/template_manager.py:build_template_body`,
`app/ate_client.py:RealAteClient.ensure_template`.

### 2.3 The golden snapshot

When a template is created, Substrate boots one throwaway actor from it,
waits for its wakeup probe, snapshots it, publishes the snapshot as a **Tag**,
and deletes the throwaway actor. Every guest created afterwards starts from
that tag instead of cold-booting the image: that is why "Open the hotel"
checks in twelve guests in moments. The concierge watches
`status.goldenSnapshotStatus.goldenTag` and the UI shows a banner until it is
ready.

One subtlety: the throwaway golden actor lives in the hotel's own atespace
(named after the template UID), so the concierge's sweeper deliberately
ignores any actor that is not a persona.

### 2.4 Checking in: CreateActor plus EgressPolicy

A guest is created the first time you talk to it:

```python
await ate.create_actor(name, template)            # CreateActor
await ate.ensure_egress_policy(name, rules)       # CreateActorEgressPolicy
```

The order matters, and the second call is not optional. A Substrate actor
reaches **nothing** until it has an EgressPolicy: not even DNS resolves (the
sandbox's DNS relay answers NXDOMAIN for any name no rule lists). The hotel's
policy allows exactly two destinations:

```yaml
rules:
- http:  {hostnames: [substrate-hotel-concierge.default.svc.cluster.local]}
- https:
    hostnames: [generativelanguage.googleapis.com]
    effects:
      replaceHeaders:
      - header: x-goog-api-key
        credentialUri: ate-secret://k8s.io/default/default/substrate-hotel-gemini/api-key
```

The hostname is fully qualified on purpose: a short name would go through
search-domain expansion and try names the policy never mentions.

Code: `app/concierge.py:_check_in`, `app/template_manager.py:egress_rules`.

### 2.5 The guest never holds a credential

This is the most interesting change from the earlier generations of the app,
and it is pure Substrate. The guest calls the Gemini API with a **placeholder**
key. Its TLS connection is intercepted by the egress gateway (the guest trusts
the gateway's CA through the projected trust bundle, `SSL_CERT_FILE`), the
gateway matches the `https` rule, asks Substrate's credential provider for the
Secret named in `credentialUri`, replaces the `x-goog-api-key` header with the
real value, and opens a fresh TLS connection to Google.

What that buys you:

- The key is never in the sandbox's environment, filesystem, or memory, so it
  is never in a snapshot either. A guest cannot read or exfiltrate its own key.
- The credential provider is default-deny: the install grants the `hotel`
  atespace permission to resolve Secrets in the hotel's namespace, nothing else.
- Substrate also blocks the cloud metadata server from actors, so the older
  "fetch a Workload Identity token from inside the sandbox" approach is not
  available any more. Injection is the intended replacement.

You can watch it happen: `kubectl -n ate-system logs deploy/atenet-egress -c envoy`
shows each guest's `POST /v1beta/models/…:generateContent` on the `mitm` leg,
and the credential provider logs `resolving credential … actor/hotel/<guest>`.

Code: `app/agent/llm.py` (`LLM_MODE=gemini`), `deploy_app.sh` (the Secret),
`cluster/install-substrate-hotel-prereq.sh` step 6 (the provider policy).

### 2.6 Talking to a guest: the router and resume-on-request

The concierge never knows where a guest is. It sends every chat to
`https://atenet-router.ate-system.svc/` with one header:

```
ate-target-actor: hotel/amara
```

The router looks the actor up, and if it is suspended it **resumes it first**,
parking the request until the actor is routable (the installer raises the
parking budget to 90 s; a restore can take seconds). Then it proxies the
request into the sandbox.

Two security details shape the client:

- The router serves **mTLS only** and admits an allowlist of SPIFFE IDs. The
  concierge's pod gets a certificate from Substrate's pod-identity signer
  through a projected `podCertificate` volume, and verifies the router with a
  projected `clusterTrustBundle`. The installer passes the concierge's identity
  (`spiffe://cluster.local/ns/default/sa/substrate-hotel-concierge`) in
  `--ingress-allowed-spiffe-ids`. Nothing else in the cluster can reach a guest.
- The concierge also sends `X-Actor-Name`. The guest compares it with the
  name Substrate projected into its sandbox and answers `421` on a mismatch,
  so two guests can never answer for each other even during a routing race.

The concierge retries a few pre-delivery failures (421, 500, 503, and 504 only
for idempotent GETs) for about 30 s, which is long enough for a busy hotel to
free a room.

Code: `app/ate_client.py:RealAteClient._router`, `app/agent/server.py:_ensure_identity`,
`infra/deployment.yaml` (the projected volumes).

### 2.7 Going to sleep: zero-idle and the sweeper

Ten seconds after its last reply, the guest's own linger loop posts
`/internal/yield` to the concierge. The guest cannot suspend itself (it has no
Substrate credential, on purpose), so the concierge calls `SuspendActor` on its
behalf, but only if its own bookkeeping agrees the guest is idle and no chat is
in flight. A background sweeper backstops lost yields; with two concierge
replicas it first asks the guest itself (`/idlez`) whether it is busy, because
no single replica's memory can prove that.

`SuspendActor` is **asynchronous** by default: it returns as soon as the
snapshot is on the worker's node, and the room is free at that instant. The
atelet uploads the snapshot to object storage in the background. The UI
reads this off the actor's `status.snapshots`: "asleep on the node" while the
upload is pending (it wakes in well under a second, on that node), "asleep in
GCS" once it is durable (it wakes anywhere).

Code: `app/agent/server.py:_linger_loop`, `app/concierge.py:guest_yield`,
`_sweep_once`, `app/ate_client.py:RealAteClient._status_of`.

### 2.8 When things go wrong: crash and revert

If a restore fails, or a guest exceeds its memory limit, Substrate marks the
actor `CRASHED` and the router refuses to resume it. The concierge calls
`RevertActor` on the guest's next message, which rewinds it to its last
durable snapshot (the conversation up to that point survives), and the same
chat wakes it from there. The UI shows the state and the rewind.

### 2.9 Scaling rooms

"+ Room" patches the WorkerPool's `spec.replicas`; the ate-controller resizes
the worker Deployment. With the default compute class, GKE's Node
Auto-Provisioning creates `c3-standard-4` nodes for the rooms on demand, so
scaling the hotel scales the cluster. "− Room" below the number of awake
guests is a demo in itself: the evicted guest is suspended on the way out and
comes back on its next message.

One machine type is pinned on purpose: a gVisor memory snapshot records the
CPU features it was taken on, and Substrate restores it in full only on a CPU
that has them all (otherwise it keeps the files and cold-boots the process).

### 2.10 Who may do what

Substrate authorizes every API call with access policies. The concierge
authenticates with its projected ServiceAccount token and holds exactly two
roles, granted by `deploy_app.sh` through `kubectl ate` as the install's
bootstrap owner: **editor** on the `hotel` atespace (actors, templates, egress
policies) and **viewer** globally (`ListWorkers`, to draw the rooms). Kubernetes
RBAC is down to two rules: read its own pod, patch the WorkerPool.

---

## Part 3: Running it

### 3.1 Layout

| Path | Role |
| --- | --- |
| `app/concierge.py` | UI/API server, actor lifecycle, sweeper |
| `app/ate_client.py` | The Substrate client (gRPC + mTLS router) and an in-memory fake |
| `app/template_manager.py` | The ActorTemplate body and the guests' EgressPolicy |
| `app/agent/` | The guest: chat server, identity, linger loop, Gemini client |
| `frontend/` | The floor plan |
| `infra/` | Concierge Deployment, WorkerPool, Gateway, HTTPRoute, RBAC |
| `cluster/` | Substrate installer and install document |
| `lib.sh` | The pinned release (`UPSTREAM_REF`), shared helpers |
| `ateapipb/` | Vendored Python stubs of Substrate's API |
| `tests/` | Offline suite against the fake plane |

### 3.2 The three scripts

```bash
cp .env.example .env     # PROJECT_ID, a Gemini key, cluster name
./setup_infra.sh         # cluster + Agent Substrate
./deploy_app.sh          # image, manifests, atespace, access policies, Secret
./verify_setup.sh        # the automated proof
```

**`setup_infra.sh`** creates a GKE cluster the way Substrate wants it:
Kubernetes 1.37+ (its PKI runs on the PodCertificateRequest and
ClusterTrustBundle APIs), Dataplane V2 (Substrate ships NetworkPolicies),
Workload Identity, managed OpenTelemetry, Gateway API, and optionally NAP with
the `hotel-rooms` compute class. It then runs the installer.

**`cluster/install-substrate-hotel-prereq.sh`** installs Substrate from the
pinned release: it clones the tag, renders an install document
(`cluster/substrate-install.yaml.tmpl`: release images on ghcr.io, the
snapshot location, GKE identifiers) and runs upstream's
`go run ./cmd/ate-setup deploy ate-system`. Around that it does the hotel's
part: the snapshot bucket and the IAM bindings upstream's model needs (all on
resources, so an org process rewriting project IAM cannot wipe them), the
router's identity allowlist, the namespace label that admits the concierge
to the API server, the credential provider's namespace policy, a demo-sized
PostgreSQL volume, a router parking budget, an atelet toleration for NAP
nodes, and `kubectl-ate` built into `.tools/`.

**`deploy_app.sh`** builds and pushes the image, creates the Gemini and
Basic-auth Secrets, resolves the release worker image digest for the
WorkerPool, applies the manifests, creates the atespace and the concierge's
access policies, warms every room, and resets the hotel.

**`verify_setup.sh`** is the proof as a script: plane release matches the pin,
golden snapshot ready, chat, sleep, wake with the same `boot_id` and a longer
history, EgressPolicy present, reply really from Gemini.

### 3.3 Operating with kubectl ate

```bash
.tools/kubectl-ate get actors -a hotel
.tools/kubectl-ate get workers
.tools/kubectl-ate get actor-templates -a hotel
.tools/kubectl-ate get egress-policy amara -a hotel -o yaml
.tools/kubectl-ate suspend actor amara -a hotel
.tools/kubectl-ate revert actor amara -a hotel
.tools/kubectl-ate proxy actor amara -a hotel      # then curl http://127.0.0.1:8000/history
```

Each deploy that changes the image or the guest configuration leaves a new
template (and golden tag) behind; prune old ones with
`kubectl ate delete actor-template <name> -a hotel`.

### 3.4 Developing offline

`ATE_MODE=fake` replaces the plane with an in-memory hotel that keeps the
observable semantics (slots, asynchronous suspend, resume on chat, crash and
revert, egress policies recorded). The whole test suite runs against it:

```bash
pip install -r requirements.txt
pytest tests/
ATE_MODE=fake LLM_MODE=canned uvicorn app.concierge:app --port 8000
```

One test parses the generated template and policy into the vendored protobuf
messages, so a schema drift fails offline rather than as an
`INVALID_ARGUMENT` after a deploy.

### 3.5 Upgrading Substrate

Bump `UPSTREAM_REF` in `lib.sh`, regenerate `ateapipb/` from
`api/ateapi/v1/ateapi.proto`, run the tests, then follow upstream's upgrade
runbook from the install record in `.ate-setup/installs/`. From 1.0 on,
patch and minor releases roll in place.

---

## Part 4: Things we learned

- **Deny by default is really deny.** An actor without an EgressPolicy is
  mute, and the failure looks like a DNS problem inside the guest. The policy's
  `metadata` must name the atespace, or the API rejects it and the guest stays
  mute. The concierge now re-ensures the policy on later chats, not just at
  creation.
- **A new policy takes a few seconds to reach the DNS relay.** A brand-new
  guest's very first outbound call can race it; the guest retries once.
- **Present the client certificate explicitly.** httpx's `cert=` shortcut did
  not send the PodCertificate; the router answered "certificate required".
  An `ssl.SSLContext` with `load_cert_chain` works.
- **Model names move.** The Gemini API stopped serving `gemini-2.5-flash` to
  this key; the default is now the `gemini-flash-latest` alias.
- **Templates are immutable, so name them by what they contain.** Hashing
  only the image digest let a config change silently keep the old template.
- **Wake times.** From a node-local snapshot a guest answers in well under a
  second of restore time; three guests waking at once on one `c3-standard-4`
  node measured 0.2 to 0.7 s each at the router. The visible latency is then
  mostly the model call.
- **The public load balancer has quiet windows.** GKE Gateway re-syncs after a
  rollout cause a few minutes of 503s; the UI retries with backoff and keeps the
  last floor plan.
