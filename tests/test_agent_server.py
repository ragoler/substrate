"""The guest agent in isolation: identity from Host header, RAM history,
lazy boot_id, immediate readyz."""

HOST = {"Host": "amara.hotel.actors.resources.substrate.ate.dev"}


def test_healthz_and_readyz(agent_client):
    assert agent_client.get("/healthz").json() == {"status": "ok"}
    # readyz must succeed with no identity and no LLM — the golden pod
    # depends on it.
    assert agent_client.get("/readyz").status_code == 200


def test_chat_builds_history_and_stable_boot_id(agent_client):
    r1 = agent_client.post("/chat", json={"message": "hello"}, headers=HOST).json()
    assert r1["guest"] == "amara"
    assert r1["history_len"] == 2
    assert r1["reply"]

    r2 = agent_client.post(
        "/chat", json={"message": "again"}, headers=HOST
    ).json()
    assert r2["history_len"] == 4
    # boot_id is minted on first request and stable for the process lifetime —
    # the property the demo uses as its RAM-survival proof.
    assert r2["boot_id"] == r1["boot_id"]

    h = agent_client.get("/history", headers=HOST).json()
    assert h["guest"] == "amara"
    assert [m["text"] for m in h["history"][:2]] == ["hello", r1["reply"]]


def test_name_comes_from_host_header(agent_client):
    r = agent_client.post(
        "/chat", json={"message": "hi"},
        headers={"Host": "viktor.hotel.actors.resources.substrate.ate.dev"},
    ).json()
    assert r["guest"] == "viktor"
