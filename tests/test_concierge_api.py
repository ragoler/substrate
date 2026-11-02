"""The concierge against the fake plane: the whole hotel lifecycle offline."""

import base64
import time


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_healthz(concierge_client):
    assert concierge_client.get("/healthz").json() == {"status": "ok"}


def test_basic_auth_guards_ui_and_api(concierge_client):
    from app import concierge as mod
    mod.HOTEL_USERNAME = "guest"
    mod.HOTEL_PASSWORD = "s3cret"          # fresh module per test — no leakage

    # Missing or wrong credentials: 401 with the browser-prompt challenge.
    r = concierge_client.get("/api/state")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Basic")
    assert concierge_client.get(
        "/", headers=_basic("guest", "wrong")
    ).status_code == 401
    assert concierge_client.post(
        "/api/guests/amara/chat", json={"message": "hi"},
        headers=_basic("intruder", "s3cret"),
    ).status_code == 401

    # /healthz stays open: kubelet probes and the LB health check need it.
    assert concierge_client.get("/healthz").status_code == 200

    # Correct credentials: UI and API work.
    assert concierge_client.get(
        "/api/state", headers=_basic("guest", "s3cret")
    ).status_code == 200
    assert concierge_client.get(
        "/", headers=_basic("guest", "s3cret")
    ).status_code == 200


def test_basic_auth_skips_guest_facing_endpoints(concierge_client):
    """Guests call /internal/yield and /llm pod-to-pod with no credentials —
    Basic auth must not apply there (the HTTPRoute keeps them off the LB)."""
    from app import concierge as mod
    mod.HOTEL_PASSWORD = "s3cret"

    assert concierge_client.post(
        "/llm", json={"guest": "ezra", "message": "hi", "history": []}
    ).status_code == 200
    assert concierge_client.post(
        "/internal/yield", json={"guest": "ezra"}
    ).status_code == 200


def test_internal_endpoints_refuse_lb_stamped_requests(concierge_client):
    """Belt-and-braces behind the HTTPRoute lockdown: a request that reaches
    /internal/* or /llm THROUGH the external LB (GCLB stamps Via: 1.1 google)
    is refused even if the route somehow let it in."""
    lb = {"Via": "1.1 google"}
    assert concierge_client.post(
        "/llm", json={"guest": "ezra", "message": "hi", "history": []},
        headers=lb,
    ).status_code == 403
    assert concierge_client.post(
        "/internal/yield", json={"guest": "ezra"}, headers=lb
    ).status_code == 403


def test_state_shape_empty_hotel(concierge_client):
    s = concierge_client.get("/api/state").json()
    assert len(s["guests"]) == 12
    assert all(g["status"] == "vacant" for g in s["guests"])
    assert len(s["workers"]) == 3
    assert all(w["occupant"] is None for w in s["workers"])
    m = s["meter"]
    assert m["checked_in"] == 0 and m["capacity"] == 12 and m["workers"] == 3
    # Golden-snapshot readiness of the guest template (fake plane: ready).
    assert s["golden"]["golden_ready"] is True and s["golden"]["error"] is None
    assert s["golden"]["golden_tag"]          # the template's published golden Tag


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
    # Room occupancy is derived from the actors' worker assignment (workers
    # themselves no longer report who they host).
    rooms = [w for w in s["workers"] if w["occupant"] == "amara"]
    assert len(rooms) == 1 and rooms[0]["pod"] == amara["worker_pod"]

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
    assert s["meter"]["suspended"] == 1 and s["meter"]["awake"] == 0


def test_check_in_creates_the_egress_policy_first(concierge_client):
    """A Substrate actor reaches nothing (not even DNS) without an
    EgressPolicy, so check-in writes it right behind CreateActor — before the
    first chat can wake the guest and have it call Gemini."""
    from app import concierge as mod

    concierge_client.post("/api/guests/kai/chat", json={"message": "hi"})
    rules = mod.ate.egress_policies["kai"]
    assert rules and "http" in rules[0]
    assert rules[0]["http"]["hostnames"] == ["substrate-hotel-concierge.default.svc.cluster.local"]
    # A guest that exists without a policy (a failed earlier attempt, another
    # replica's check-in) gets one on its next chat rather than staying mute.
    mod.ate.egress_policies.pop("kai")
    mod.policy_ok.discard("kai")
    concierge_client.post("/api/guests/kai/chat", json={"message": "again"})
    assert "kai" in mod.ate.egress_policies


def test_crashed_guest_is_reverted_then_woken(concierge_client):
    """A failed restore leaves an actor CRASHED and the router refuses to
    resume it. The concierge rewinds it (RevertActor) to its last durable
    snapshot and the same chat wakes it from there — memory up to that
    snapshot intact."""
    from app import concierge as mod

    r1 = concierge_client.post("/api/guests/kai/chat", json={"message": "remember: tango"}).json()
    mod.activity["kai"] = time.monotonic() - 999
    concierge_client.post("/internal/yield", json={"guest": "kai"})
    mod.ate.crash("kai", "runsc restore: exit status 128")
    s = concierge_client.get("/api/state").json()
    kai = next(g for g in s["guests"] if g["name"] == "kai")
    assert kai["status"] == "crashed" and "runsc" in kai["crash"]
    assert s["meter"]["crashed"] == 1

    r2 = concierge_client.post("/api/guests/kai/chat", json={"message": "what did I say?"}).json()
    assert r2["was_suspended"] is True and r2["slept_as"] == "crashed"
    assert r2["boot_id"] == r1["boot_id"]
    assert "tango" in r2["reply"]
    s = concierge_client.get("/api/state").json()
    assert next(g for g in s["guests"] if g["name"] == "kai")["status"] == "awake"


def test_history_never_wakes_a_sleeping_guest(concierge_client):
    """Opening a drawer must not resume a guest (that would occupy a room
    just to render text); the UI keeps its own transcript."""
    from app import concierge as mod

    concierge_client.post("/api/guests/kai/chat", json={"message": "hi"})
    assert concierge_client.get("/api/guests/kai/history").json()["history"]
    mod.activity["kai"] = time.monotonic() - 999
    concierge_client.post("/internal/yield", json={"guest": "kai"})
    h = concierge_client.get("/api/guests/kai/history").json()
    assert h["asleep"] == "suspended" and h["history"] == []
    s = concierge_client.get("/api/state").json()
    assert next(g for g in s["guests"] if g["name"] == "kai")["status"] == "suspended"


def test_async_suspend_naps_until_the_upload_lands(concierge_client):
    """SuspendActor returns once the snapshot is on the node ("napping", the
    room is already free); atelet uploads it in the background and the guest
    becomes "suspended" (durable, wakes anywhere). Either state wakes on chat."""
    from app import concierge as mod

    mod.ate.upload_latency = 0.2
    r1 = concierge_client.post("/api/guests/kai/chat", json={"message": "hi"}).json()
    mod.activity["kai"] = time.monotonic() - 999
    concierge_client.post("/internal/yield", json={"guest": "kai"})
    s = concierge_client.get("/api/state").json()
    assert next(g for g in s["guests"] if g["name"] == "kai")["status"] == "napping"
    assert s["meter"]["napping"] == 1 and s["meter"]["workers"] == 3
    assert all(w["occupant"] is None for w in s["workers"])
    time.sleep(0.25)
    s = concierge_client.get("/api/state").json()
    assert next(g for g in s["guests"] if g["name"] == "kai")["status"] == "suspended"
    r2 = concierge_client.post("/api/guests/kai/chat", json={"message": "back?"}).json()
    assert r2["slept_as"] == "suspended" and r2["boot_id"] == r1["boot_id"]


def test_yield_refused_while_chat_in_flight(concierge_client):
    """A restored guest's linger loop can ask to be tucked in while it is
    still composing the reply it was woken for — suspending then kills the
    tunnel mid-reply (visitors saw 502s). The in-flight guard must win over
    the idle-time gate."""
    from app import concierge as mod

    concierge_client.post("/api/guests/kai/chat", json={"message": "hi"})
    mod.activity["kai"] = time.monotonic() - 999   # idle-gate would allow it
    mod.in_flight["kai"] = 1                       # ...but a chat is in flight
    r = concierge_client.post("/internal/yield", json={"guest": "kai"}).json()
    assert r["suspended"] is False
    assert r["reason"] == "chat in flight"

    mod.in_flight.pop("kai")
    r = concierge_client.post("/internal/yield", json={"guest": "kai"}).json()
    assert r["suspended"] is True


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
    assert r2["slept_as"] == "suspended"
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
    """Reset while guests are RUNNING: DeleteActor terminates the sandbox
    and releases the room — no suspend dance."""
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


def test_sweeper_leaves_non_guest_actors_alone(concierge_client):
    """Substrate builds a template's golden snapshot from a temporary actor
    in the hotel atespace (named after the template UID). The sweeper must
    never touch actors that are not hotel guests."""
    import asyncio

    from app import concierge as mod

    golden = "3f8b1c2d-0000-4000-8000-000000000001"
    asyncio.run(mod.ate.create_actor(golden, "guest-fake"))
    mod.SWEEP_IDLE_SECONDS = 0
    asyncio.run(mod._sweep_once())
    assert mod.ate.actors[golden]["status"] == "awake"
