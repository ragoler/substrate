"""The guest agent in isolation: identity from X-Actor-Name (with the Host
header as fallback), the 421 misdirect guard, RAM history, lazy boot_id,
immediate readyz."""

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


def test_actor_name_header_wins_over_host(agent_client):
    # The router's Envoy rewrites Host to the upstream pod IP; the explicit
    # X-Actor-Name header the concierge sends is the reliable identity.
    r = agent_client.post(
        "/chat", json={"message": "hi"},
        headers={"Host": "10.80.3.4:80", "X-Actor-Name": "lumi"},
    ).json()
    assert r["guest"] == "lumi"


def test_ip_shaped_host_never_becomes_a_name(agent_client):
    # Without the header, an IP-shaped Host must NOT name the guest "10" —
    # the regression that once had every guest introduce itself as "🧳 10".
    r = agent_client.post(
        "/chat", json={"message": "hi"}, headers={"Host": "10.80.3.4:80"}
    ).json()
    assert r["guest"] == "guest"          # identity stays unclaimed

    # The next properly-addressed request claims the real name.
    r = agent_client.post(
        "/chat", json={"message": "hi"},
        headers={"Host": "10.80.3.4:80", "X-Actor-Name": "amara"},
    ).json()
    assert r["guest"] == "amara"


def test_idlez_self_report(agent_client):
    # Before any chat: no last_activity, nothing in flight (the /idlez
    # request itself is subtracted from the count).
    r = agent_client.get("/idlez", headers=HOST).json()
    assert r["in_flight"] == 0
    assert r["idle_s"] is None

    agent_client.post("/chat", json={"message": "hi"}, headers=HOST)
    r = agent_client.get("/idlez", headers=HOST).json()
    assert r["in_flight"] == 0
    assert r["idle_s"] is not None and r["idle_s"] >= 0


def test_misdirected_request_is_421(agent_client):
    # Once a guest knows who it is, a request addressed to someone else is
    # refused — the suspend/resume shuffle can briefly route to the wrong
    # room, and answering would cross-pollute two conversations.
    agent_client.post(
        "/chat", json={"message": "hi"},
        headers={"Host": "10.0.0.1:80", "X-Actor-Name": "amara"},
    )
    resp = agent_client.post(
        "/chat", json={"message": "hi"},
        headers={"Host": "10.0.0.1:80", "X-Actor-Name": "viktor"},
    )
    assert resp.status_code == 421
    assert "amara" in resp.json()["detail"]
    # History is equally guarded (it leaks the whole conversation otherwise).
    assert agent_client.get(
        "/history", headers={"X-Actor-Name": "viktor"}
    ).status_code == 421
