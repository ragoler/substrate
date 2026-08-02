"""Runtime ActorTemplate management.

Substrate's ActorTemplate CRD rejects tag-only images (a moving tag would
invalidate snapshots), but the build pipeline pushes `:latest`. The trick:
the concierge and the guest agent share ONE image, so at startup the
concierge reads its *own* pod's containerStatuses[].imageID — which the
kubelet reports digest-qualified (`repo:tag@sha256:...`) — and registers the
ActorTemplate from that. A rebuild + rollout automatically produces a new
template (templates are immutable) and a fresh golden snapshot.

kubernetes is imported lazily; fake mode / tests never touch it.
"""

import logging
import os

logger = logging.getLogger(__name__)

TEMPLATE_PREFIX = "guest-"
PAUSE_IMAGE = (
    "registry.k8s.io/pause:3.10.2"
    "@sha256:f548e0e8e3dc1896ca956272154dde3314e8cc4fde0a57577ee9fa1c63f5baf4"
)


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


def build_template_body(name: str, namespace: str, image_ref: str) -> dict:
    """The ActorTemplate CR for the shared guest image.

    Env holds ONLY plain values — Vertex access uses ADC via Workload
    Identity, so no credential is ever frozen into the golden snapshot.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    concierge_url = os.environ.get(
        "CONCIERGE_SVC_URL",
        f"http://substrate-hotel-concierge.{namespace}.svc",
    )
    snapshots = os.environ.get(
        "SNAPSHOTS_LOCATION",
        f"gs://{project}-substrate-snapshots/substrate-hotel/",
    )
    worker_pool = os.environ.get("WORKER_POOL", "substrate-hotel")
    return {
        "apiVersion": "ate.dev/v1alpha1",
        "kind": "ActorTemplate",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "pauseImage": PAUSE_IMAGE,
            "snapshotsConfig": {"onPause": "Full", "location": snapshots},
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
                    "readyz": {"httpGet": {"path": "/readyz", "port": 80}},
                    "env": [
                        {"name": "CONCIERGE_URL", "value": concierge_url},
                        {"name": "LINGER_SECONDS",
                         "value": os.environ.get("LINGER_SECONDS", "10")},
                        {"name": "LLM_MODE",
                         "value": os.environ.get("GUEST_LLM_MODE", "")},
                        {"name": "GOOGLE_GENAI_USE_VERTEXAI", "value": use_vertex},
                        {"name": "GOOGLE_CLOUD_PROJECT", "value": project},
                        {"name": "GOOGLE_CLOUD_LOCATION", "value": location},
                    ],
                }
            ],
            "workerSelector": {"matchLabels": {"workload": worker_pool}},
        },
    }


def ensure_template_sync(namespace: str) -> str | None:
    """Create (if absent) the ActorTemplate for the current image digest.

    Returns the template name, or None when the pod can't self-inspect yet
    (image still pulling) — the caller retries.
    """
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException

    image_ref = read_own_image_ref_sync()
    if image_ref is None:
        return None
    name = TEMPLATE_PREFIX + digest12(image_ref)
    body = build_template_body(name, namespace, image_ref)

    config.load_incluster_config()
    api = client.CustomObjectsApi()
    try:
        api.create_namespaced_custom_object(
            group="ate.dev", version="v1alpha1", namespace=namespace,
            plural="actortemplates", body=body,
        )
        logger.info("created ActorTemplate %s (image %s)", name, image_ref)
    except ApiException as e:
        if e.status != 409:  # already exists — templates are immutable
            raise
    return name
