"""The concierge against the fake plane: the whole hotel lifecycle offline."""

import time


def test_healthz(concierge_client):
    assert concierge_client.get("/healthz").json() == {"status": "ok"}


def test_state_shape_empty_hotel(concierge_client):
    s = concierge_client.get("/api/state").json()
    assert len(s["guests"]) == 12
    assert all(g["status"] == "vacant" for g in s["guests"])
    assert len(s["workers"]) == 3
    m = s["meter"]
    assert m["checked_in"] == 0 and m["capacity"] == 12 and m["workers"] == 3


def test_chat_checks_in_and_remembers(concierge_client):
    r1 = concierge_client.post(
        "/api/guests/amara/chat", json={"message": "my favorite color is teal"}
    ).json()
    assert r1["guest"] == "amara"
    assert r1["was_suspended"] is False
    boot = r1["boot_id"]

    s = concierge_client.get("/api/state").json()
    amara = next(g for g in s["guests"] if g["name"] == "amara")
    assert amara["status"] == "awake"
    assert amara["worker_pod"] is not None

    r2 = concierge_client.post(
        "/api/guests/amara/chat", json={"message": "what did I tell you?"}
    ).json()
    assert r2["boot_id"] == boot
    assert "teal" in r2["reply"]


def test_unknown_guest_is_404(concierge_client):
    assert concierge_client.post(
        "/api/guests/nobody/chat", json={"message": "hi"}
    ).status_code == 404


def test_yield_suspends_only_when_idle(concierge_client):
    concierge_client.post("/api/guests/kai/chat", json={"message": "hi"})

    # Immediately after activity the yield is refused (spoof tolerance)...
    from app import concierge as mod
    mod.LINGER_SECONDS = 10
    r = concierge_client.post("/internal/yield", json={"guest": "kai"}).json()
    assert r["suspended"] is False

    # ...but once the concierge's own record agrees the guest is idle, it lands.
    mod.activity["kai"] = time.monotonic() - 999
    r = concierge_client.post("/internal/yield", json={"guest": "kai"}).json()
    assert r["suspended"] is True

    s = concierge_client.get("/api/state").json()
    kai = next(g for g in s["guests"] if g["name"] == "kai")
    assert kai["status"] == "suspended"
    assert kai["worker_pod"] is None


def test_resume_on_request_preserves_ram(concierge_client):
    from app import concierge as mod

    r1 = concierge_client.post(
        "/api/guests/wren/chat", json={"message": "remember: orion"}
    ).json()
    mod.activity["wren"] = time.monotonic() - 999
    concierge_client.post("/internal/yield", json={"guest": "wren"})

    # Chatting with the suspended guest transparently wakes it — same boot_id,
    # history intact — and reports was_suspended for the UI's proof banner.
    r2 = concierge_client.post(
        "/api/guests/wren/chat", json={"message": "what do you remember?"}
    ).json()
    assert r2["was_suspended"] is True
    assert r2["boot_id"] == r1["boot_id"]
    assert "orion" in r2["reply"]


def test_oversubscription_more_guests_than_rooms(concierge_client):
    """4 guests through 3 rooms: suspend one to free a slot, wake it later."""
    from app import concierge as mod

    trio = ["marisol", "viktor", "amara"]
    for g in trio:
        concierge_client.post(f"/api/guests/{g}/chat", json={"message": "hi"})

    # Hotel full: a 4th check-in fails until someone yields...
    assert concierge_client.post(
        "/api/guests/kai/chat", json={"message": "hi"}
    ).status_code == 409

    mod.activity["marisol"] = time.monotonic() - 999
    concierge_client.post("/internal/yield", json={"guest": "marisol"})

    # ...now kai fits, and marisol still resumes later with her RAM intact.
    assert concierge_client.post(
        "/api/guests/kai/chat", json={"message": "hi"}
    ).status_code == 200
    mod.activity["kai"] = time.monotonic() - 999
    concierge_client.post("/internal/yield", json={"guest": "kai"})
    r = concierge_client.post(
        "/api/guests/marisol/chat", json={"message": "back!"}
    ).json()
    assert r["was_suspended"] is True


def test_open_and_reset(concierge_client):
    concierge_client.post("/api/open")
    # /api/open checks guests in via a background task; in TestClient the
    # loop runs between requests — poll briefly.
    for _ in range(50):
        s = concierge_client.get("/api/state").json()
        if s["meter"]["checked_in"] == 3:  # 3 rooms => first 3 check-ins land
            break
        time.sleep(0.05)
    assert s["meter"]["checked_in"] >= 1

    concierge_client.post("/api/reset")
    s = concierge_client.get("/api/state").json()
    assert s["meter"]["checked_in"] == 0


def test_reset_deletes_awake_guests(concierge_client):
    """Reset while guests are RUNNING: the plane refuses a direct delete, so
    the concierge must suspend first (regression: reset used to error with
    'Actor X is not suspended (status: STATUS_RUNNING)')."""
    for g in ["marisol", "viktor"]:
        concierge_client.post(f"/api/guests/{g}/chat", json={"message": "hi"})
    s = concierge_client.get("/api/state").json()
    assert s["meter"]["awake"] == 2

    r = concierge_client.post("/api/reset").json()
    assert r["errors"] == []
    s = concierge_client.get("/api/state").json()
    assert s["meter"]["checked_in"] == 0


def test_add_guest_expands_capacity(concierge_client):
    from app import personas

    s = concierge_client.get("/api/state").json()
    assert s["meter"]["capacity"] == 12          # default GUEST_COUNT
    assert s["meter"]["roster"] == len(personas.PERSONAS)

    # Guest #13 is not admitted yet...
    thirteenth = personas.GUEST_NAMES[12]
    assert concierge_client.post(
        f"/api/guests/{thirteenth}/chat", json={"message": "hi"}
    ).status_code == 404

    # ...until we add them.
    r = concierge_client.post("/api/guests/add").json()
    assert r["added"] == thirteenth
    assert r["capacity"] == 13
    assert concierge_client.post(
        f"/api/guests/{thirteenth}/chat", json={"message": "hi"}
    ).status_code == 200
    s = concierge_client.get("/api/state").json()
    assert len(s["guests"]) == 13


def test_add_guest_stops_at_roster_end(concierge_client):
    from app import personas

    for _ in range(len(personas.PERSONAS) - 12):
        assert concierge_client.post("/api/guests/add").status_code == 200
    assert concierge_client.post("/api/guests/add").status_code == 409


def test_rooms_scale_up_and_down(concierge_client):
    assert len(concierge_client.get("/api/state").json()["workers"]) == 3

    r = concierge_client.post("/api/rooms", json={"count": 5}).json()
    assert r["rooms"] == 5
    assert len(concierge_client.get("/api/state").json()["workers"]) == 5

    # Scale down past an occupied room: the guest gets suspended, not lost.
    concierge_client.post("/api/guests/amara/chat", json={"message": "hi"})
    concierge_client.post("/api/rooms", json={"count": 1})
    s = concierge_client.get("/api/state").json()
    assert len(s["workers"]) == 1

    # Out-of-range requests are rejected.
    assert concierge_client.post("/api/rooms", json={"count": 0}).status_code == 422
    assert concierge_client.post("/api/rooms", json={"count": 99}).status_code == 422


def test_more_rooms_allow_more_awake_guests(concierge_client):
    """The knob works with the overcommit story: 4 awake needs 4 rooms."""
    trio = ["marisol", "viktor", "amara"]
    for g in trio:
        concierge_client.post(f"/api/guests/{g}/chat", json={"message": "hi"})
    assert concierge_client.post(
        "/api/guests/kai/chat", json={"message": "hi"}
    ).status_code == 409                          # 3 rooms: hotel full

    concierge_client.post("/api/rooms", json={"count": 4})
    assert concierge_client.post(
        "/api/guests/kai/chat", json={"message": "hi"}
    ).status_code == 200                          # 4th room fits kai


def test_llm_proxy_endpoint(concierge_client):
    r = concierge_client.post(
        "/llm",
        json={"guest": "ezra", "message": "hello",
              "history": [{"role": "user", "text": "first words"}]},
    ).json()
    assert r["guest"] == "ezra"
    assert "first words" in r["reply"]


def test_index_serves_frontend(concierge_client):
    resp = concierge_client.get("/")
    assert resp.status_code == 200
    assert "Agent Hotel" in resp.text
