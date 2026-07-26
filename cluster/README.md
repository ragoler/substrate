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
  plane runs on a plain GKE Standard cluster
- the `gs://<project>-substrate-snapshots` bucket + IAM for snapshot upload
- **Vertex AI IAM** (`roles/aiplatform.user`) for the node SA and the app
  namespace's Workload Identity principals — this is what makes the guests'
  keyless Gemini calls work
- the `ateom-gvisor` worker image, built from upstream source with `ko`

## Toolchain

`gcloud`, `kubectl`, `git`, `go`, [`ko`](https://ko.build), `openssl`.

## Why the pinned SHA

`UPSTREAM_SHA` pins the upstream `agent-substrate/substrate` commit the plane
is built from. Newer upstream made component mTLS mandatory, which requires
the alpha pod-certificate data plane this installer deliberately avoids —
unpinned builds crash-loop on a Standard cluster. If you bump the pin, be
aware the plane in `ate-system` is shared by anything else using Substrate on
the cluster.

Normally you don't run this directly — `setup_infra.sh` calls it with the
right environment.
