"""Runtime ActorTemplate management.

Substrate's ActorTemplate rejects tag-only images (a moving tag would
invalidate snapshots), but the build pipeline pushes `:latest`. The trick:
the concierge and the guest agent share ONE image, so at startup the
concierge reads its *own* pod's containerStatuses[].imageID — which the
kubelet reports digest-qualified (`repo:tag@sha256:...`) — and registers the
ActorTemplate from that. A rebuild + rollout automatically produces a new
template (templates are immutable) and a fresh golden snapshot.

The template is an ate API resource (CreateActorTemplate over gRPC, see
ate_client.RealAteClient.ensure_template). This module only builds the
protojson-shaped body — and the EgressPolicy rules every guest gets.

kubernetes is imported lazily; fake mode / tests never touch it.
"""

import logging
import os

logger = logging.getLogger(__name__)

TEMPLATE_PREFIX = "guest-"
# Where Substrate projects system info into the guest's filesystem.
SYSTEM_INFO_MOUNT = "/run/ate"
ACTOR_NAME_FILE = f"{SYSTEM_INFO_MOUNT}/actor/name"
TRUST_BUNDLE_FILE = f"{SYSTEM_INFO_MOUNT}/egress-trust-bundle/trust-bundle.pem"
# The Gemini API host the guests call; its API key never enters the sandbox —
# the egress gateway injects it (see egress_rules()).
GEMINI_HOST = os.environ.get("GEMINI_HOST", "generativelanguage.googleapis.com")
# A guest's size. An actor occupies capacity on its worker: with the
# WorkerPool's per-room capacity (infra/workerpool.yaml) sized to ONE of
# these, a room hosts exactly one guest — the hotel's whole premise.
GUEST_CPU_MILLICORES = int(os.environ.get("GUEST_CPU_MILLICORES", "1000"))
GUEST_MEMORY_BYTES = int(os.environ.get("GUEST_MEMORY_BYTES", str(512 * 1024 * 1024)))


def parse_image_digest_ref(image_id: str) -> str | None:
    """Normalize a kubelet imageID into a digest-pinned image reference.

    Accepts 'repo:tag@sha256:...', 'docker-pullable://repo@sha256:...', or
    '' (image still pulling). Returns None when unusable.
    """
    if not image_id:
        return None
    ref = image_id.removeprefix("docker-pullable://").strip()
    return ref if "@sha256:" in ref else None


def digest12(image_ref: str) -> str:
    return image_ref.rsplit("@sha256:", 1)[1][:12]


def template_name_for(image_ref: str, body: dict | None = None) -> str:
    """guest-<12 hex> — a fingerprint of the image digest AND the template's
    configuration (env, sizing, policy-relevant settings). Templates are
    immutable, so a config change must produce a NEW template (and golden
    snapshot) just like a rebuild does; hashing the image alone would
    silently keep serving the old env."""
    import hashlib
    import json

    if body is None:
        return TEMPLATE_PREFIX + digest12(image_ref)
    config = {k: v for k, v in body.items() if k != "metadata"}
    h = hashlib.sha256(
        (digest12(image_ref) + json.dumps(config, sort_keys=True)).encode()
    ).hexdigest()
    return TEMPLATE_PREFIX + h[:12]


def read_own_image_ref_sync() -> str | None:
    """Digest-pinned ref of this pod's main container image, via the K8s API."""
    from kubernetes import client, config

    pod_name = os.environ.get("POD_NAME")
    pod_ns = os.environ.get("POD_NAMESPACE")
    if not pod_name or not pod_ns:
        logger.warning("POD_NAME/POD_NAMESPACE not set; cannot self-inspect")
        return None
    config.load_incluster_config()
    pod = client.CoreV1Api().read_namespaced_pod(pod_name, pod_ns)
    for cs in pod.status.container_statuses or []:
        ref = parse_image_digest_ref(cs.image_id)
        if ref:
            return ref
    return None


def concierge_hostname(namespace: str) -> str:
    """FQDN the guests reach the concierge at. Fully qualified on purpose:
    the sandbox's DNS relay only resolves names an egress rule lists, and
    search-domain expansion would otherwise try (and be refused) names the
    policy never mentions."""
    return os.environ.get(
        "CONCIERGE_HOSTNAME",
        f"substrate-hotel-concierge.{namespace}.svc.cluster.local",
    )


def egress_rules(namespace: str) -> list[dict]:
    """The guests' network allowlist (protojson-shaped EgressRules).

    Deny by default is Substrate's posture: an actor reaches exactly what its
    EgressPolicy names, by hostname. Two destinations:
      * the concierge (plain HTTP, cluster-internal): yield requests and the
        LLM proxy fallback;
      * the Gemini API over HTTPS. The egress gateway terminates the guest's
        TLS (the guest trusts the gateway CA projected into its sandbox) and
        REPLACES the `x-goog-api-key` header with the real key, read from a
        Kubernetes Secret by Substrate's credential provider. The guest sends
        a placeholder and never holds, snapshots or can leak the secret.
    """
    rules: list[dict] = [
        {"http": {"hostnames": [concierge_hostname(namespace)]}},
    ]
    secret_uri = os.environ.get("GEMINI_SECRET_URI", "")
    if secret_uri:
        rules.append({
            "https": {
                "hostnames": [GEMINI_HOST],
                "effects": {
                    "replaceHeaders": [
                        {"header": "x-goog-api-key", "credentialUri": secret_uri},
                    ]
                },
            }
        })
    return rules


def build_template_body(name: str, namespace: str, image_ref: str) -> dict:
    """The ActorTemplate (protojson-shaped ateapi.ActorTemplate) for the
    shared guest image. `namespace` seeds the concierge Service name.

    Env holds ONLY plain values. The one credential a guest needs, the Gemini
    API key, is injected by the egress gateway outside the sandbox — the env
    carries a placeholder, so nothing secret is ever frozen into a snapshot.
    """
    worker_pool = os.environ.get("WORKER_POOL", "substrate-hotel")
    atespace = os.environ.get("ATESPACE", "hotel")
    llm_mode = os.environ.get("GUEST_LLM_MODE", "")
    if not llm_mode:
        llm_mode = "gemini" if os.environ.get("GEMINI_SECRET_URI") else "canned"
    return {
        "metadata": {"atespace": atespace, "name": name},
        "workerSelector": {"matchLabels": {"workload": worker_pool}},
        "containers": [
            {
                "name": "guest",
                "image": image_ref,
                # The atenet-router proxies to the actor on port 80 (fixed
                # upstream), so the guest listens there.
                # --app-dir pins the import root: the gVisor OCI bundle
                # does not inherit the image's WORKDIR, so a bare
                # "app.agent.server:app" fails with ModuleNotFoundError.
                "command": [
                    "uvicorn", "--app-dir", "/srv",
                    "app.agent.server:app",
                    "--host", "0.0.0.0", "--port", "80",
                ],
                # ResumeActor returns only once this answers 200: the router
                # never proxies a message into a sandbox that is still
                # booting, and the golden snapshot skips its 20 s settle delay.
                "wakeupProbe": {
                    "httpGet": {"path": "/readyz", "port": 80},
                    "timeoutSeconds": 60,
                },
                "env": [
                    {"name": "CONCIERGE_URL",
                     "value": f"http://{concierge_hostname(namespace)}"},
                    {"name": "LINGER_SECONDS",
                     "value": os.environ.get("LINGER_SECONDS", "10")},
                    {"name": "LLM_MODE", "value": llm_mode},
                    {"name": "GEMINI_MODEL",
                     "value": os.environ.get("GEMINI_MODEL", "gemini-flash-latest")},
                    # Placeholder: the egress gateway replaces the header.
                    {"name": "GEMINI_API_KEY", "value": "injected-by-substrate-egress"},
                    # The guest's own name, projected by Substrate (below).
                    {"name": "ACTOR_NAME_FILE", "value": ACTOR_NAME_FILE},
                    # Trust the egress gateway's CA (and public roots) for
                    # the HTTPS it terminates — Python/httpx honour this.
                    {"name": "SSL_CERT_FILE", "value": TRUST_BUNDLE_FILE},
                ],
                "volumeMounts": [
                    {"name": "system-info", "mountPath": SYSTEM_INFO_MOUNT},
                ],
            }
        ],
        "volumes": [
            {
                "name": "system-info",
                "systemInfo": {
                    "dataSources": [
                        # Who am I: Substrate writes the actor's name into the
                        # sandbox, so the guest needs no header to learn it.
                        {"actorMetadata": {"items": [
                            {"field": "ACTOR_METADATA_FIELD_NAME",
                             "path": "actor/name"},
                        ]}},
                        {"trustBundle": {
                            "names": ["egress-mitm.ate.dev", "system-roots.ate.dev"],
                            "path": "egress-trust-bundle/trust-bundle.pem",
                        }},
                    ]
                },
            }
        ],
        "resources": {"limits": {
            "cpuMillicores": GUEST_CPU_MILLICORES,
            "memoryBytes": GUEST_MEMORY_BYTES,
        }},
        # MEMORY: a suspend captures process memory, which is the whole point —
        # a guest's conversation lives in RAM and survives the trip.
        "snapshotConfig": {"preferredFidelity": "SNAPSHOT_FIDELITY_MEMORY"},
    }
