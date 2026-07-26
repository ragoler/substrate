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
