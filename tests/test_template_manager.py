"""Digest parsing + ActorTemplate / EgressPolicy construction (no kubernetes needed)."""

import pytest

from app import template_manager as tm


def test_parse_image_digest_ref_variants():
    plain = "us-docker.pkg.dev/p/r/substrate-hotel:tag@sha256:" + "a" * 64
    assert tm.parse_image_digest_ref(plain) == plain
    assert tm.parse_image_digest_ref("docker-pullable://" + plain) == plain
    assert tm.parse_image_digest_ref("") is None            # still pulling
    assert tm.parse_image_digest_ref("repo/img:latest") is None  # tag-only


def test_digest12_and_template_name(monkeypatch):
    ref = "repo/img:latest@sha256:" + "abcdef123456" + "0" * 52
    assert tm.digest12(ref) == "abcdef123456"
    assert tm.template_name_for(ref) == "guest-abcdef123456"
    # With a body, the name fingerprints image + configuration: a config-only
    # change (new model, new linger) must yield a NEW immutable template.
    monkeypatch.setenv("GEMINI_MODEL", "gemini-a")
    a = tm.template_name_for(ref, tm.build_template_body("x", "default", ref))
    monkeypatch.setenv("GEMINI_MODEL", "gemini-b")
    b = tm.template_name_for(ref, tm.build_template_body("x", "default", ref))
    assert a != b and a.startswith("guest-") and len(a) == len("guest-") + 12
    # ...and is stable for the same inputs regardless of the placeholder name.
    assert b == tm.template_name_for(ref, tm.build_template_body("y", "default", ref))


def _body(monkeypatch, secret_uri="ate-secret://k8s.io/default/default/substrate-hotel-gemini/api-key"):
    monkeypatch.setenv("GEMINI_SECRET_URI", secret_uri)
    ref = "repo/img:latest@sha256:" + "b" * 64
    return tm.build_template_body("guest-" + "b" * 12, "default", ref)


def test_build_template_body(monkeypatch):
    body = _body(monkeypatch)

    # An ate API resource in the hotel atespace — not a Kubernetes object.
    assert body["metadata"] == {"atespace": "hotel", "name": "guest-" + "b" * 12}
    assert "apiVersion" not in body and "spec" not in body
    assert body["workerSelector"]["matchLabels"] == {"workload": "substrate-hotel"}

    container = body["containers"][0]
    assert "@sha256:" in container["image"]          # the API requires a digest pin
    # ResumeActor blocks on this probe; a slow/failing probe crashes the actor.
    assert container["wakeupProbe"]["httpGet"] == {"path": "/readyz", "port": 80}
    assert container["wakeupProbe"]["timeoutSeconds"] >= 30

    # Sizing is mandatory and is what a room's capacity is measured against.
    assert body["resources"]["limits"]["cpuMillicores"] > 0
    assert body["resources"]["limits"]["memoryBytes"] > 0
    # MEMORY fidelity: RAM travels with the snapshot — the whole demo rests on it.
    assert body["snapshotConfig"]["preferredFidelity"] == "SNAPSHOT_FIDELITY_MEMORY"

    # Substrate projects the guest's own name and the egress trust bundle.
    vol = body["volumes"][0]["systemInfo"]["dataSources"]
    assert {"field": "ACTOR_METADATA_FIELD_NAME", "path": "actor/name"} in vol[0]["actorMetadata"]["items"]
    assert set(vol[1]["trustBundle"]["names"]) == {"egress-mitm.ate.dev", "system-roots.ate.dev"}
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts["system-info"] == tm.SYSTEM_INFO_MOUNT

    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["ACTOR_NAME_FILE"] == tm.ACTOR_NAME_FILE
    assert env["SSL_CERT_FILE"] == tm.TRUST_BUNDLE_FILE
    assert env["LLM_MODE"] == "gemini"
    # Keyless: every env entry is a literal value and the "key" is a
    # placeholder — the egress gateway injects the real one outside the sandbox.
    assert all("value" in e for e in container["env"])
    assert env["GEMINI_API_KEY"] == "injected-by-substrate-egress"
    assert env["CONCIERGE_URL"] == "http://substrate-hotel-concierge.default.svc.cluster.local"


def test_no_gemini_key_means_canned_guests(monkeypatch):
    body = _body(monkeypatch, secret_uri="")
    env = {e["name"]: e.get("value") for e in body["containers"][0]["env"]}
    assert env["LLM_MODE"] == "canned"
    rules = tm.egress_rules("default")
    assert len(rules) == 1 and "http" in rules[0]


def test_egress_rules_allow_concierge_and_gemini_with_injection(monkeypatch):
    monkeypatch.setenv("GEMINI_SECRET_URI", "ate-secret://k8s.io/default/default/substrate-hotel-gemini/api-key")
    rules = tm.egress_rules("default")
    # Plain HTTP to the concierge (yield requests, LLM proxy fallback): FQDN,
    # because the sandbox's DNS relay only resolves names a rule lists.
    assert rules[0] == {"http": {"hostnames": ["substrate-hotel-concierge.default.svc.cluster.local"]}}
    # HTTPS to Gemini, TLS terminated at the gateway, API key header replaced.
    https = rules[1]["https"]
    assert https["hostnames"] == ["generativelanguage.googleapis.com"]
    assert https["effects"]["replaceHeaders"] == [{
        "header": "x-goog-api-key",
        "credentialUri": "ate-secret://k8s.io/default/default/substrate-hotel-gemini/api-key",
    }]


def test_bodies_match_vendored_proto(monkeypatch):
    """Schema drift guard: the template and the egress policy must parse into
    the vendored ateapi messages exactly (unknown keys / wrong enum names fail
    here, offline, instead of as INVALID_ARGUMENT after a deploy)."""
    pytest.importorskip("google.protobuf")
    from google.protobuf import json_format

    from ateapipb import ateapi_pb2

    msg = json_format.ParseDict(_body(monkeypatch), ateapi_pb2.ActorTemplate())
    assert msg.metadata.atespace == "hotel"
    assert msg.snapshot_config.preferred_fidelity == ateapi_pb2.SNAPSHOT_FIDELITY_MEMORY
    assert msg.containers[0].wakeup_probe.http_get.port == 80
    assert msg.resources.limits.cpu_millicores == tm.GUEST_CPU_MILLICORES
    assert msg.volumes[0].system_info.data_sources[0].actor_metadata.items[0].field == ateapi_pb2.ACTOR_METADATA_FIELD_NAME

    policy = json_format.ParseDict(
        {"metadata": {"atespace": "hotel", "name": "default"}, "rules": tm.egress_rules("default")},
        ateapi_pb2.EgressPolicy(),
    )
    assert policy.rules[1].https.effects.replace_headers[0].header == "x-goog-api-key"
