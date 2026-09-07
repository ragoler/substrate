# Cluster prerequisites (one-time per cluster)

`install-substrate-hotel-prereq.sh` installs the full **Agent Substrate control
plane** by wrapping upstream's own installer (`hack/install-ate.sh
--deploy-ate-system` at the pinned commit), then layering the hotel's extras:

- upstream deploys: ate.dev CRDs, the `podcertificate-controller` (upstream's
  PodCertificate signer), `ate-api-server` + `valkey-cluster`,
  `atenet-router` + `dns`, `ate-controller` + `atelet`. Component mTLS runs
  on the Kubernetes 1.36+ PodCertificate/ClusterTrustBundle **beta APIs** —
  the cluster must have them enabled (`setup_infra.sh` creates clusters with
  `--release-channel=rapid --enable-kubernetes-unstable-apis=...`; the
  installer fails fast with the exact `gcloud` command otherwise)
- a `servicedns-ca` Secret shim in `ate-system` — the concierge verifies the
  api-server's TLS against it; the root is mirrored from the podcertificate
  controller's service-dns CA pool
- an `atelet` toleration patch for Custom-Compute-Class (NAP) nodes —
  suspend/resume calls the atelet on the worker's node, so every node a room
  can land on needs one; upstream's DaemonSet ships without tolerations
- the `gs://<project>-substrate-snapshots` bucket + IAM for snapshot upload
- **Vertex AI IAM** (`roles/aiplatform.user`) for the node SA and the app
  namespace's Workload Identity principals — this is what makes the guests'
  keyless Gemini calls work. On projects where an org process periodically
  rewrites the project IAM policy (wiping these grants), set
  `WI_IMPERSONATE_GSA` instead: step 6 makes the hotel's KSAs impersonate
  that GSA via SA-resource-level bindings, which the sweep doesn't touch
- the `ateom-gvisor` worker image, built from upstream source with `ko`

Note on JWT auth: the api-server validates client tokens against the
cluster's OIDC issuer, whose location segment is the **zone** for zonal
clusters (`CLUSTER_LOCATION`), not the region. The issuer lands in the
`ate-api-server-envvars` ConfigMap, which the installer now applies (not
create-if-absent) and restarts the api-server when the values changed — so
re-running the installer fixes a wrong issuer on an existing cluster.

## Toolchain

`gcloud`, `kubectl`, `git`, `go`, [`ko`](https://ko.build), `openssl`.

## The pinned SHA (upgraded 2026-08-02)

`UPSTREAM_SHA` pins the upstream `agent-substrate/substrate` commit the plane
is built from — currently `3ed6aa07` (upstream tip, 2026-08-02), verified by
an end-to-end upgrade spike: chat → suspend → resume → same `boot_id`, golden
snapshots, GCS suspend/resume, the atunnel actor-ingress path, keyless Vertex
calls from inside the gVisor sandbox, and JWT client auth (audience
`api.ate-system.svc`). If you bump the pin further:

- regenerate `ateapipb/` from `pkg/proto/ateapipb/ateapi.proto`
  (`python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=.
  ateapipb/ateapi.proto`) and adapt `app/ate_client.py` if the proto changed
  (the 2026-08 changes were: `ActorRef` → `ObjectRef`, `CreateActorRequest`
  takes a full `Actor`, `Actor.actor_id` → `Actor.metadata.name`);
- be aware the plane in `ate-system` is shared by anything else using
  Substrate on the cluster.

Two upstream timing defaults are too tight for real checkpoint restores and
LLM calls, and the installer adjusts both:

- `--parked-request-budget` (default 5 s) bounds the whole resume-on-request;
  a warm restore takes ~5 s (OCI unpack alone ~4 s) and a cold one ~30 s, so
  default-budget resumes get context-canceled mid-restore and chats surface
  as 502/503. The installer sets it to 90 s on the atenet-router Deployment.
- the actor route's Envoy upstream timeout is HARDCODED to 10 s at this SHA
  (`cmd/atenet/internal/router/xds.go`); a guest's first Gemini call after a
  resume takes ~15 s. The installer patches the pinned source to 120 s
  before the `ko` build (step 1b) — worth an upstream flag request.

## Optional: Custom Compute Class for the rooms

`compute-class.yaml` (applied by `setup_infra.sh` only when
`USE_COMPUTE_CLASS=TRUE`) defines the `hotel-rooms` ComputeClass. Together
with Node Auto-Provisioning it gives the WorkerPool's worker pods their own
auto-created, right-sized node pools — the default fixed-pool path needs
neither. On-demand machine families only: rooms hold awake guests'
un-checkpointed RAM, which a Spot preemption would destroy.

Normally you don't run this directly — `setup_infra.sh` calls it with the
right environment.
