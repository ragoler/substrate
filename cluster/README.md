# Cluster prerequisites (one-time per cluster)

`install-substrate-hotel-prereq.sh` installs the full **Agent Substrate control
plane** into the `ate-system` namespace, adapted from the GKE showcase hub's
`substrate-overcommit` installer (both are idempotent and install the same
shared plane at the same pinned upstream commit):

- ate.dev CRDs (`workerpools`, `actortemplates`, `sandboxconfigs`)
- `ate-api-server` (gRPC control plane) + `valkey-cluster` (session state)
- `atenet-router` + `dns` (resume-on-request data path)
- `ate-controller` + `atelet` (reconcile + node agent)
- static openssl-minted TLS certs via the `static-certs/` kustomize overlay —
  a stand-in for the alpha PodCertificate/ClusterTrustBundle APIs, so the
  plane runs on a plain GKE Standard cluster. The overlay also adds a
  toleration so the `atelet` DaemonSet runs on Custom-Compute-Class (NAP)
  nodes — suspend/resume calls the atelet on the worker's node, so every
  node a room can land on needs one
- the `gs://<project>-substrate-snapshots` bucket + IAM for snapshot upload
- **Vertex AI IAM** (`roles/aiplatform.user`) for the node SA and the app
  namespace's Workload Identity principals — this is what makes the guests'
  keyless Gemini calls work. On projects where an org process periodically
  rewrites the project IAM policy (wiping these grants), set
  `WI_IMPERSONATE_GSA` instead: step 8b makes the hotel's KSAs impersonate
  that GSA via SA-resource-level bindings, which the sweep doesn't touch
- the `ateom-gvisor` worker image, built from upstream source with `ko`

Note on JWT auth: the api-server validates client tokens against the
cluster's OIDC issuer, whose location segment is the **zone** for zonal
clusters (`CLUSTER_LOCATION`), not the region. The issuer lands in the
`ate-api-server-envvars` ConfigMap, which is created only if absent — if it
is ever wrong on an existing cluster (every control-plane call fails with
`UNAUTHENTICATED: unexpected issuer`), patch the ConfigMap and restart
`ate-api-server-deployment`; re-running the installer will NOT fix it.

## Toolchain

`gcloud`, `kubectl`, `git`, `go`, [`ko`](https://ko.build), `openssl`.

## Why the pinned SHA

`UPSTREAM_SHA` pins the upstream `agent-substrate/substrate` commit the plane
is built from. Newer upstream made component mTLS mandatory, which requires
the alpha pod-certificate data plane this installer deliberately avoids —
unpinned builds crash-loop on a Standard cluster. If you bump the pin, be
aware the plane in `ate-system` is shared by anything else using Substrate on
the cluster.

## Optional: Custom Compute Class for the rooms

`compute-class.yaml` (applied by `setup_infra.sh` only when
`USE_COMPUTE_CLASS=TRUE`) defines the `hotel-rooms` ComputeClass. Together
with Node Auto-Provisioning it gives the WorkerPool's worker pods their own
auto-created, right-sized node pools — the default fixed-pool path needs
neither. On-demand machine families only: rooms hold awake guests'
un-checkpointed RAM, which a Spot preemption would destroy.

Normally you don't run this directly — `setup_infra.sh` calls it with the
right environment.
