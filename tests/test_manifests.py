"""Render infra/ exactly the way deploy_app.sh does and lint the results."""

import glob
import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INFRA = os.path.join(ROOT, "infra")

FAKE_VARS = {
    "NAMESPACE": "default",
    "PROJECT_NAME": "fake-project",
    "REGION": "us-central1",
    "ARTIFACT_REGISTRY_REPO": "substrate-hotel",
    "SUBSTRATE_HOTEL_IMAGE": "us-central1-docker.pkg.dev/fake-project/substrate-hotel/substrate-hotel:test",
    "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
    "GUEST_COUNT": "12",
    "WORKER_REPLICAS": "3",
    "WORKER_NODE_SELECTOR": "{}",
    "LINGER_SECONDS": "10",
}


def render(path: str) -> str:
    text = open(path).read()
    for k, v in FAKE_VARS.items():
        text = text.replace("${" + k + "}", v)
    return text


def manifest_files():
    return sorted(glob.glob(os.path.join(INFRA, "*.yaml")))


def rendered_docs():
    docs = []
    for f in manifest_files():
        for doc in yaml.safe_load_all(render(f)):
            if doc:
                docs.append((os.path.basename(f), doc))
    return docs


def test_infra_dir_is_not_empty():
    assert len(manifest_files()) >= 6


@pytest.mark.parametrize("path", manifest_files())
def test_no_unexpanded_vars(path):
    rendered = render(path)
    assert "${" not in rendered, f"unexpanded ${{VAR}} in {os.path.basename(path)}"


def test_no_namespace_objects():
    for fname, doc in rendered_docs():
        assert doc.get("kind") != "Namespace", f"Namespace object in {fname}"


def test_rbac_sorts_first():
    files = [os.path.basename(f) for f in manifest_files()]
    assert files[0] == "00-rbac.yaml"


def test_deployment_and_names_are_consistent():
    docs = rendered_docs()
    deployments = [d for _, d in docs if d.get("kind") == "Deployment"]
    assert len(deployments) == 1
    assert deployments[0]["metadata"]["name"] == "substrate-hotel-concierge-deployment"

    # WorkerPool must NOT collide with the concierge: the ate-controller
    # creates <workerpool>-deployment.
    pools = [d for _, d in docs if d.get("kind") == "WorkerPool"]
    assert len(pools) == 1
    assert pools[0]["metadata"]["name"] == "substrate-hotel"
    # replicas comes from ${WORKER_REPLICAS} and must render as a YAML int.
    assert pools[0]["spec"]["replicas"] == int(FAKE_VARS["WORKER_REPLICAS"])
    assert pools[0]["spec"]["sandboxClass"] == "gvisor"

    gateways = [d for _, d in docs if d.get("kind") == "Gateway"]
    assert len(gateways) == 1
    gw = gateways[0]
    assert gw["metadata"]["name"] == "substrate-hotel-gw"

    routes = [d for _, d in docs if d.get("kind") == "HTTPRoute"]
    assert routes[0]["spec"]["parentRefs"][0]["name"] == gw["metadata"]["name"]

    services = [d for _, d in docs if d.get("kind") == "Service"]
    assert services[0]["spec"]["ports"][0]["port"] == 80
    assert services[0]["spec"]["ports"][0]["targetPort"] == 8000


def test_concierge_env_is_keyless():
    """The Vertex story: plain values only, no secret refs anywhere."""
    for fname, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
        for e in env:
            assert "secretKeyRef" not in str(e.get("valueFrom", "")), (
                f"secret ref found in {fname}: {e}"
            )
        names = {e["name"] for e in env}
        assert {"GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_CLOUD_LOCATION"} <= names


def test_workerpool_node_selector_renders_both_ways():
    """deploy_app.sh renders {} (default pool) or the compute-class selector."""
    path = os.path.join(INFRA, "workerpool.yaml")

    default = yaml.safe_load(render(path))
    assert default["spec"]["template"]["nodeSelector"] == {}

    ccc_vars = dict(FAKE_VARS)
    ccc_vars["WORKER_NODE_SELECTOR"] = (
        '{"cloud.google.com/compute-class": "hotel-rooms"}'
    )
    text = open(path).read()
    for k, v in ccc_vars.items():
        text = text.replace("${" + k + "}", v)
    ccc = yaml.safe_load(text)
    assert ccc["spec"]["template"]["nodeSelector"] == {
        "cloud.google.com/compute-class": "hotel-rooms"
    }

    # The selector must match the ComputeClass the installer applies.
    cc = yaml.safe_load(
        open(os.path.join(ROOT, "cluster", "compute-class.yaml"))
    )
    assert cc["kind"] == "ComputeClass"
    assert cc["metadata"]["name"] == "hotel-rooms"
    assert cc["spec"]["nodePoolAutoCreation"]["enabled"] is True


def test_concierge_drains_on_termination():
    """Hitless rollouts: the GCLB data plane lags ~1-2 min detaching a killed
    endpoint, so the pod must keep serving (preStop sleep) while it drains —
    without it every rollout serves 'no healthy upstream'."""
    for fname, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        container = spec["containers"][0]
        pre_stop = container["lifecycle"]["preStop"]["exec"]["command"]
        assert pre_stop[0] == "sleep"
        drain = int(pre_stop[1])
        assert drain >= 30
        # Grace must outlast the drain or the kubelet kills the pod mid-sleep.
        assert spec["terminationGracePeriodSeconds"] > drain


def test_atelet_tolerates_compute_class_nodes():
    """The installer must schedule atelet onto NAP nodes: rooms on a
    Custom-Compute-Class node are unusable without a local atelet (every
    ResumeActor fails with 'found 0 atelet pods on node'). Upstream's
    DaemonSet has no tolerations, so the installer patches one in."""
    script = open(
        os.path.join(ROOT, "cluster", "install-substrate-hotel-prereq.sh")
    ).read()
    patch_at = script.find("patch daemonset atelet")
    assert patch_at != -1, "no atelet toleration patch in the installer"
    patch_block = script[patch_at:patch_at + 400]
    assert "cloud.google.com/compute-class" in patch_block
    assert '"operator": "Exists"' in patch_block


def test_internal_paths_never_reach_the_backend():
    """/internal/* and /llm are cluster-internal (guest yield requests, the
    Vertex proxy). The route must redirect them away instead of proxying —
    an exposed /llm is a free Gemini proxy for the whole internet."""
    routes = [d for _, d in rendered_docs() if d.get("kind") == "HTTPRoute"]
    assert len(routes) == 1
    blocked = [
        r for r in routes[0]["spec"]["rules"]
        if any(f.get("type") == "RequestRedirect" for f in r.get("filters", []))
    ]
    assert len(blocked) == 1, "no RequestRedirect rule for the internal paths"
    paths = {m["path"]["value"] for m in blocked[0]["matches"]}
    assert paths == {"/internal", "/llm"}
    assert "backendRefs" not in blocked[0]


def test_lb_health_check_targets_healthz():
    """With Basic auth enabled '/' answers 401; the default GCLB health check
    against '/' would mark the backend unhealthy and take the site down."""
    policies = [d for _, d in rendered_docs() if d.get("kind") == "HealthCheckPolicy"]
    assert len(policies) == 1
    hc = policies[0]
    assert hc["spec"]["targetRef"]["kind"] == "Service"
    assert hc["spec"]["targetRef"]["name"] == "substrate-hotel-concierge"
    check = hc["spec"]["default"]["config"]["httpHealthCheck"]
    assert check["requestPath"] == "/healthz"
    assert check["port"] == 8000


def test_auth_secret_is_injected_and_optional():
    """Basic-auth credentials come from the substrate-hotel-auth Secret
    (created by deploy_app.sh, never rendered into manifests) and must be
    optional — no Secret simply means an unauthenticated hotel."""
    for fname, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        env_from = doc["spec"]["template"]["spec"]["containers"][0]["envFrom"]
        secret_refs = [e["secretRef"] for e in env_from if "secretRef" in e]
        assert secret_refs, f"no secretRef envFrom in {fname}"
        assert secret_refs[0]["name"] == "substrate-hotel-auth"
        assert secret_refs[0]["optional"] is True


def test_projected_token_audience():
    for _, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        vols = doc["spec"]["template"]["spec"]["volumes"]
        tokens = [
            s["serviceAccountToken"]
            for v in vols if v.get("projected")
            for s in v["projected"]["sources"] if "serviceAccountToken" in s
        ]
        assert tokens and tokens[0]["audience"] == "api.ate-system.svc"
