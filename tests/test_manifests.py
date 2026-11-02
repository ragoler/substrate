"""Render infra/ exactly the way deploy_app.sh does and lint the results."""

import glob
import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INFRA = os.path.join(ROOT, "infra")
INSTALLER = os.path.join(ROOT, "cluster", "install-substrate-hotel-prereq.sh")

FAKE_VARS = {
    "NAMESPACE": "default",
    "PROJECT_NAME": "fake-project",
    "REGION": "us-central1",
    "ARTIFACT_REGISTRY_REPO": "substrate-hotel",
    "SUBSTRATE_HOTEL_IMAGE": "us-central1-docker.pkg.dev/fake-project/substrate-hotel/substrate-hotel:test",
    "GUEST_COUNT": "12",
    "WORKER_REPLICAS": "3",
    "WORKER_NODE_SELECTOR": "{}",
    "ATEWORKER_IMAGE": "ghcr.io/agent-substrate/substrate/ateworker-gvisor:v1.0.0-rc.1@sha256:" + "0" * 64,
    "LINGER_SECONDS": "10",
    "GEMINI_SECRET_URI": "ate-secret://k8s.io/default/default/substrate-hotel-gemini/api-key",
    "GEMINI_MODEL": "gemini-2.5-flash",
}


def render(path: str, variables: dict | None = None) -> str:
    text = open(path).read()
    for k, v in (variables or FAKE_VARS).items():
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


def deployments():
    return [d for _, d in rendered_docs() if d.get("kind") == "Deployment"]


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
    assert len(deployments()) == 1
    assert deployments()[0]["metadata"]["name"] == "substrate-hotel-concierge-deployment"

    # WorkerPool must NOT collide with the concierge: the ate-controller
    # creates a Deployment named after the pool.
    pools = [d for _, d in docs if d.get("kind") == "WorkerPool"]
    assert len(pools) == 1
    pool = pools[0]
    assert pool["apiVersion"] == "ate.dev/v1"
    assert pool["metadata"]["name"] == "substrate-hotel"
    # replicas comes from ${WORKER_REPLICAS} and must render as a YAML int.
    assert pool["spec"]["replicas"] == int(FAKE_VARS["WORKER_REPLICAS"])
    # The sandbox runtime is the pool's (class default SandboxConfig).
    assert pool["spec"]["sandboxClasses"] == [{"name": "gvisor"}]
    # Upstream's release worker image, pinned by digest.
    assert pool["spec"]["workerImage"].startswith("ghcr.io/agent-substrate/substrate/ateworker-gvisor:")
    assert "@sha256:" in pool["spec"]["workerImage"]
    # Limits = the room's capacity (one guest); requests = what nodes pack by.
    res = pool["spec"]["template"]["resources"]
    assert res["limits"]["cpu"] and res["limits"]["memory"]
    assert res["requests"]["cpu"] and res["requests"]["memory"]

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
    """No credential in the concierge's env: the Gemini key reaches guests by
    egress injection and the concierge only ever sees a Secret URI."""
    env = deployments()[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    for e in env:
        assert "secretKeyRef" not in str(e.get("valueFrom", "")), f"secret ref: {e}"
    names = {e["name"]: e.get("value") for e in env}
    assert names["GEMINI_SECRET_URI"].startswith("ate-secret://k8s.io/")
    assert "GEMINI_API_KEY" not in names


def test_concierge_mounts_substrate_pki():
    """The concierge authenticates to the plane with what kubelet projects:
    a ServiceAccount token (ate-api-server), a PodCertificate client cert
    (atenet-router admits only allowlisted SPIFFE IDs) and the servicedns
    ClusterTrustBundle to verify both. No Secrets, no CA shims."""
    spec = deployments()[0]["spec"]["template"]["spec"]
    sources = [
        s for v in spec["volumes"] if v.get("projected")
        for s in v["projected"]["sources"]
    ]
    tokens = [s["serviceAccountToken"] for s in sources if "serviceAccountToken" in s]
    assert tokens and tokens[0]["audience"] == "api.ate-system.svc"
    certs = [s["podCertificate"] for s in sources if "podCertificate" in s]
    assert certs and certs[0]["signerName"] == "podidentity.podcert.ate.dev/identity"
    assert certs[0]["credentialBundlePath"] == "credential-bundle.pem"
    bundles = [s["clusterTrustBundle"] for s in sources if "clusterTrustBundle" in s]
    assert bundles and bundles[0]["signerName"] == "servicedns.podcert.ate.dev/identity"
    assert bundles[0]["path"] == "servicedns-ca.pem"
    mounts = {m["name"]: m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    assert mounts["ate-client"] == "/run/ate-client"


def test_rbac_is_minimal():
    """Substrate authorizes the concierge with ITS access policies; Kubernetes
    RBAC is only for self-inspection and scaling the WorkerPool."""
    roles = [d for _, d in rendered_docs() if d.get("kind") in ("Role", "ClusterRole")]
    assert len(roles) == 1 and roles[0]["kind"] == "Role"
    rules = {(tuple(r["apiGroups"]), tuple(r["resources"]), tuple(r["verbs"])) for r in roles[0]["rules"]}
    assert rules == {
        (("",), ("pods",), ("get", "list")),
        (("ate.dev",), ("workerpools",), ("get", "list", "patch")),
    }


def test_workerpool_node_selector_renders_both_ways():
    """deploy_app.sh renders {} (default pool) or the compute-class selector."""
    path = os.path.join(INFRA, "workerpool.yaml")

    default = yaml.safe_load(render(path))
    assert default["spec"]["template"]["nodeSelector"] == {}

    ccc_vars = dict(FAKE_VARS)
    ccc_vars["WORKER_NODE_SELECTOR"] = '{"cloud.google.com/compute-class": "hotel-rooms"}'
    ccc = yaml.safe_load(render(path, ccc_vars))
    assert ccc["spec"]["template"]["nodeSelector"] == {
        "cloud.google.com/compute-class": "hotel-rooms"
    }

    # The selector must match the ComputeClass setup_infra.sh applies; one
    # machine type only (a gVisor memory snapshot restores in full only on
    # the CPU platform it was taken on).
    cc = yaml.safe_load(open(os.path.join(ROOT, "cluster", "compute-class.yaml")))
    assert cc["kind"] == "ComputeClass"
    assert cc["metadata"]["name"] == "hotel-rooms"
    assert cc["spec"]["nodePoolAutoCreation"]["enabled"] is True
    assert cc["spec"]["priorities"] == [{"machineType": "c3-standard-4"}]
    assert cc["spec"]["whenUnsatisfiable"] == "DoNotScaleUp"


def test_concierge_drains_on_termination():
    """Hitless rollouts: the GCLB data plane lags ~1-2 min detaching a killed
    endpoint, so the pod must keep serving (preStop sleep) while it drains —
    without it every rollout serves 'no healthy upstream'."""
    spec = deployments()[0]["spec"]["template"]["spec"]
    container = spec["containers"][0]
    pre_stop = container["lifecycle"]["preStop"]["exec"]["command"]
    assert pre_stop[0] == "sleep"
    drain = int(pre_stop[1])
    assert drain >= 30
    # Grace must outlast the drain or the kubelet kills the pod mid-sleep.
    assert spec["terminationGracePeriodSeconds"] > drain


def test_installer_installs_the_pinned_release_with_prebuilt_images():
    """The plane comes from upstream's release: ate-setup from a checkout of
    the tag in lib.sh, images from ghcr.io, no source build."""
    lib = open(os.path.join(ROOT, "lib.sh")).read()
    assert 'UPSTREAM_REF="${UPSTREAM_REF:-v' in lib
    assert "ghcr.io/agent-substrate/substrate" in lib
    script = open(INSTALLER).read()
    assert "go run ./cmd/ate-setup deploy ate-system" in script
    assert "--config" in script and "substrate-install.yaml.tmpl" in script
    assert "ko build" not in script and "xds.go" not in script
    tmpl = open(os.path.join(ROOT, "cluster", "substrate-install.yaml.tmpl")).read()
    assert "repo: ${UPSTREAM_IMAGE_REPO}" in tmpl and "tag: ${UPSTREAM_REF}" in tmpl
    assert "snapshotLocation: gs://${SNAPSHOT_BUCKET}/" in tmpl


def test_installer_wires_identity_and_access():
    """Who may reach what: the router admits the concierge's and kubectl-ate's
    SPIFFE IDs; the hotel namespace is admitted to ate-api-server; the
    credential provider may resolve the hotel namespace's Secrets for the
    hotel atespace."""
    script = open(INSTALLER).read()
    assert "--ingress-allowed-spiffe-ids" in script
    assert 'spiffe://cluster.local/ns/${APP_NAMESPACE}/sa/substrate-hotel-concierge' in script
    assert "spiffe://cluster.local/ns/${ATE_SYSTEM_NS}/sa/atenet-client" in script
    assert "ate.dev/api-access=true" in script
    assert '--credential-provider \'{"name":"k8s.io"}\'' in script
    assert "k8s-credential-provider-namespace-policy" in script
    assert "allowedNamespaces" in script


def test_installer_tunes_the_plane_without_replacing_upstream_lists():
    """Resumes take tens of seconds (router parked budget); NAP room nodes
    are tainted (atelet toleration APPENDED — upstream ships its own)."""
    script = open(INSTALLER).read()
    assert "--parked-request-budget=90s" in script
    assert "/spec/template/spec/tolerations/-" in script
    assert '"path": "/spec/template/spec/tolerations"' not in script
    assert "cloud.google.com/compute-class" in script
    # The version-label machinery is gone upstream; nothing may reference it.
    assert "substrate-version" not in script
    assert "substrate-version" not in open(os.path.join(ROOT, "setup_infra.sh")).read()


def test_internal_paths_never_reach_the_backend():
    """/internal/* and /llm are cluster-internal (guest yield requests, the
    Gemini proxy fallback). The route must redirect them away instead of
    proxying — an exposed /llm is a free Gemini proxy for the whole internet."""
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


def test_secrets_are_injected_and_optional():
    """Basic-auth credentials and the Gemini key come from Secrets created by
    deploy_app.sh (never rendered into manifests) and must be optional — no
    Secret means an unauthenticated hotel / scripted replies."""
    env_from = deployments()[0]["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    refs = {e["secretRef"]["name"]: e["secretRef"]["optional"] for e in env_from if "secretRef" in e}
    assert refs == {"substrate-hotel-auth": True, "substrate-hotel-gemini": True}
