# Cluster prerequisites (one-time per cluster)

`install-substrate-hotel-prereq.sh` installs the **Agent Substrate control
plane from upstream's release** — `go run ./cmd/ate-setup deploy ate-system`
from a checkout of the pinned tag, with the images that release publishes to
`ghcr.io/agent-substrate/substrate`. Nothing is built from source. The pin is
`UPSTREAM_REF` in [`lib.sh`](../lib.sh) (single source of truth for the
installer, `deploy_app.sh` — which pins the WorkerPool's worker image to the
same release by digest — and the vendored API stubs).

What upstream deploys: the `ate.dev` CRDs (WorkerPool, SandboxConfig,
CSIDriverConfig), the `podcertificate-controller` (Substrate's PKI:
PodCertificates and ClusterTrustBundles for every component *and client*),
`ate-api-server` with a bundled PostgreSQL store, embedded OpenFGA
authorization and `ate-idp-server`, `atenet-router` + `dns`, the
`atenet-egress` gateway with the Kubernetes Secrets credential provider,
`ate-controller` and the `atelet` DaemonSet. Component mTLS runs on the
Kubernetes PodCertificateRequest / ClusterTrustBundle APIs, which GKE 1.37+
serves by default (`setup_infra.sh` creates such clusters; 1.36 needs the beta
APIs enabled at creation — see upstream `tools/setup-gcp/README.md`).

What the installer layers on top:

- **the install document** ([`substrate-install.yaml.tmpl`](substrate-install.yaml.tmpl)):
  release images, the cluster-wide snapshot location
  `gs://<bucket>/agent-hotel/`, the GKE identifiers the JWT issuer derives
  from. Two identity settings travel as flags so they show in the run
  report verbatim: `--credential-provider '{"name":"k8s.io"}'` and
  `--ingress-allowed-spiffe-ids` (the concierge's ServiceAccount and
  `kubectl ate proxy actor`'s identity — the **only** clients the router lets
  reach actors). ate-setup records what it resolved under `.ate-setup/`; that
  record is the input of the next upgrade.
- **GCP, upstream's IAM model**: the snapshot bucket; the snapshot-transfer
  Google service account `ate-snapshot-transfer`, which `ate-api-server`
  impersonates (token creator on the SA) to mint per-actor downscoped
  storage tokens for the data plane; `objectAdmin` + `bucketViewer` on the
  bucket for the api-server's Workload Identity principal and the transfer
  SA; `artifactregistry.reader` on the hotel's repo for the atelet principal
  (atelet pulls the guest image itself). Every binding is on the **resource**
  (bucket, SA, repo), so a project IAM policy rewritten by an org process
  cannot wipe them.
- **who may talk to the plane**: `ate-system` denies ingress by default; the
  hotel's namespace is labeled `ate.dev/api-access=true` so the concierge can
  reach `ate-api-server`.
- **credential injection policy**: the bundled credential provider is
  default-deny per atespace; the `hotel` atespace is granted the hotel's
  namespace, where `deploy_app.sh` puts the Gemini key Secret.
- a demo-sized PostgreSQL volume (`POSTGRES_STORAGE`, default 20Gi; upstream's
  500Gi is sized for activation load) — edited in the checkout before the
  first apply because `volumeClaimTemplates` are immutable
- `--parked-request-budget=90s` on the router: a resume-on-request may
  restore a snapshot from GCS (~5 s warm, up to ~30 s cold); the default
  budget is 5 s
- an `atelet` toleration for Custom-Compute-Class (NAP) room nodes —
  suspend/resume call the node's atelet, so every node a room can land on
  needs one; appended, never replacing upstream's own toleration
- `kubectl-ate`, built from the same checkout into `.tools/` (`deploy_app.sh`
  uses it to create the atespace and the concierge's access policies)

## Toolchain

`gcloud`, `kubectl`, `git`, `go` (runs upstream's installer; downloads the
Go toolchain upstream's `go.mod` asks for), `jq`, `python3`. `docker` only
for `deploy_app.sh`.

## Authorization model

`ate-api-server` checks every call against access policies (OpenFGA). The
install's bootstrap owner is upstream's default, the `ate-client`
ServiceAccount — the identity `kubectl ate` acts as. `deploy_app.sh` uses it
to create the `hotel` atespace and to grant the concierge's ServiceAccount
exactly what it needs: **editor** on the hotel atespace (actors, templates,
egress policies) and **viewer** globally (ListWorkers for the floor plan).
The concierge authenticates with its projected ServiceAccount token. See
upstream `docs/guides/authorization.md`; for a shared cluster, replace the
bootstrap owner.

## The pinned release (upgraded 2026-10-31)

`UPSTREAM_REF=v1.0.0-rc.1` (2026-10-29). Upstream now cuts weekly releases
with GitHub release notes; this is the first release candidate of 1.0, from
which the [version skew policy](https://github.com/agent-substrate/substrate/blob/main/docs/reference/version-skew-policy.md)
applies (patch and minor releases of one major are compatible and can be
rolled in place).

What changed between the previous pin (`7a9abab3`, 2026-09-07, pre-0.2) and
this one, and what it meant for the hotel:

| Upstream change | Hotel change |
| --- | --- |
| Release images on ghcr.io; `ate-setup` is the installer (config document, run report, install records) | No `ko`, no source patches; `substrate-install.yaml.tmpl`; `.ate-setup/` records |
| Router serves mTLS only and admits an allowlist of SPIFFE IDs; actors addressed by the `ate-target-actor` header | Concierge mounts a projected PodCertificate + ClusterTrustBundle; its SPIFFE ID is allowlisted at install |
| `ate-system` default-deny ingress; namespace label `ate.dev/api-access` | Installer labels the hotel namespace |
| Authorization enforced (access policies, bootstrap owners) | `deploy_app.sh` grants the concierge editor/viewer via `kubectl ate` |
| EgressPolicy enforced down to DNS; link-local (metadata server) blocked from actors; credential injection GA | Every guest gets a policy at check-in; Gemini via API key injected by the gateway (`x-goog-api-key`), trust bundle projected into the sandbox |
| `PauseActor` removed; `SuspendActor` is asynchronous (node-local first, background upload) | The nap/deep-sleep tiers collapse into one `SuspendActor`; the UI reads durability off `status.snapshots` |
| Failed checkpoint/restore crashes the actor; `RevertActor` | Concierge reverts a crashed guest on its next message |
| `DeleteActor` works in any state (`any_state` gone) | Reset is one call |
| ActorTemplate: `resources.limits{cpuMillicores,memoryBytes}` required, `snapshotConfig.preferredFidelity`, `wakeupProbe`, sandbox selection moved to the WorkerPool (`sandboxClasses`), systemInfo volumes (actor metadata, trust bundles) | `template_manager.py` rewritten; guests learn their name from Substrate |
| Version-label install removed; `ate.dev/v1` CRDs | NAP selector/toleration pairing gone; plain compute-class selector |
| Snapshot location is install-wide (`--snapshot-location`) | Set in the install document |

If you bump the pin further:

- update `UPSTREAM_REF` in `lib.sh`;
- regenerate `ateapipb/` from upstream `api/ateapi/v1/ateapi.proto` (from the
  repo root, in a venv with `grpcio-tools`):
  ```bash
  cp <upstream>/api/ateapi/v1/ateapi.proto ateapipb/ateapi.proto
  python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. ateapipb/ateapi.proto
  ```
  then raise the `grpcio`/`protobuf` floors in `app/requirements.txt` to the
  versions stamped in the generated headers, adapt `app/ate_client.py` /
  `app/template_manager.py` if the proto changed, and run `pytest tests/`
  (`test_bodies_match_vendored_proto` catches schema drift offline);
- **upgrade the plane in place** with upstream's runbook
  (`docs/guides/upgrade.md`): it starts from the install record in
  `.ate-setup/installs/`, rolls podcertificate-controller → atelet → the
  WorkerPool's `workerImage` (edit `infra/workerpool.yaml`'s digest via
  `deploy_app.sh`) → api-server → controller → atenet, and rolls back the same
  way. Between incompatible releases (before 1.0), reinstall;
- be aware the plane in `ate-system` is shared by anything else using
  Substrate on the cluster.

## Optional: Custom Compute Class for the rooms

`compute-class.yaml` (applied by `setup_infra.sh` when `USE_COMPUTE_CLASS=TRUE`,
the default) defines the `hotel-rooms` ComputeClass. Together with Node
Auto-Provisioning it gives the WorkerPool's worker pods their own
auto-created nodes — the fixed-pool path needs neither. The class pins **one
machine type, `c3-standard-4`**, and never falls back: a gVisor memory
snapshot records the CPU features it was taken on, and Substrate restores it
in full only on a worker whose CPU is a superset (otherwise it downgrades to
a ROOTFS restore: files survive, memory does not). Every room, and the node
the golden snapshot is built on, must therefore be the same CPU platform.
On-demand only: rooms hold awake guests' un-checkpointed RAM and the
node-local snapshots of guests whose upload is still in flight.
