"""Digest parsing + ActorTemplate construction (no kubernetes needed)."""

from app import template_manager as tm


def test_parse_image_digest_ref_variants():
    plain = "us-docker.pkg.dev/p/r/substrate-hotel:tag@sha256:" + "a" * 64
    assert tm.parse_image_digest_ref(plain) == plain
    assert tm.parse_image_digest_ref("docker-pullable://" + plain) == plain
    assert tm.parse_image_digest_ref("") is None            # still pulling
    assert tm.parse_image_digest_ref("repo/img:latest") is None  # tag-only


def test_digest12():
    ref = "repo/img:latest@sha256:" + "abcdef123456" + "0" * 52
    assert tm.digest12(ref) == "abcdef123456"


def test_build_template_body(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "fake-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    ref = "repo/img:latest@sha256:" + "b" * 64
    body = tm.build_template_body("guest-" + "b" * 12, "default", ref)

    spec = body["spec"]
    container = spec["containers"][0]
    assert "@sha256:" in container["image"]          # CRD requires digest pin
    assert "@sha256:" in spec["pauseImage"]
    assert container["readyz"]["httpGet"]["port"] == 80  # required upstream
    assert spec["workerSelector"]["matchLabels"] == {"workload": "substrate-hotel"}
    assert spec["snapshotsConfig"]["onPause"] == "Full"
    assert spec["snapshotsConfig"]["location"].startswith(
        "gs://fake-project-substrate-snapshots/"
    )

    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["GOOGLE_CLOUD_PROJECT"] == "fake-project"
    # Keyless: every env entry is a literal value — nothing secret to freeze
    # into the golden snapshot.
    assert all("value" in e for e in container["env"])
