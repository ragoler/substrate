"""The Agent Hotel concierge — the one service this demo actually deploys.

Responsibilities:
  * serve the hotel floor-plan UI (frontend/) and the /api endpoints;
  * drive Substrate's control plane: check guests in (CreateActor + their
    EgressPolicy), tuck idle guests away (SuspendActor), rewind crashed ones
    (RevertActor), reset the hotel (DeleteActor);
  * relay chat to a guest BY NAME through the atenet-router data path — the
    concierge never knows (or cares) which worker pod the guest is on, and a
    suspended guest is transparently resumed by the router;
  * execute guest yield requests (POST /internal/yield): guests decide when
    they're idle, but can't call SuspendActor themselves on a JWT-auth plane,
    so the concierge does it for them;
  * run the sweeper — the backstop that suspends anything idle too long
    (covers lost yield calls and frees rooms when the hotel is full);
  * register the ActorTemplate at startup from its own image digest
    (see template_manager.py).

Runs identically with ATE_MODE=fake (in-memory hotel, no cluster) — that is
how the test suite and local dev exercise everything.
"""

import asyncio
import base64
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import ate_client, personas
from app.ate_client import ASLEEP_STATES, AteError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINGER_SECONDS = float(os.environ.get("LINGER_SECONDS", "10"))
# Optional HTTP Basic auth for the public surface (UI + /api). The password
# arrives via the substrate-hotel-auth Secret (see deploy_app.sh) — empty
# means auth is disabled (local dev, tests, and the fake hotel).
HOTEL_USERNAME = os.environ.get("HOTEL_USERNAME", "guest")
HOTEL_PASSWORD = os.environ.get("HOTEL_PASSWORD", "")
SWEEP_IDLE_SECONDS = float(os.environ.get("SWEEP_IDLE_SECONDS", "30"))
SWEEP_INTERVAL_S = float(os.environ.get("SWEEP_INTERVAL_S", "5"))
POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "default")
# How many of the roster's personas are checked-in-able at start; "Add guest"
# admits more at runtime, up to the roster size.
GUEST_COUNT = max(1, min(int(os.environ.get("GUEST_COUNT", "12")),
                         len(personas.PERSONAS)))
MAX_ROOMS = int(os.environ.get("MAX_ROOMS", "12"))
FRONTEND_DIR = os.environ.get(
    "FRONTEND_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "frontend"),
)

# Keep hard references to background tasks: asyncio only holds weak refs, so
# an un-referenced sweeper/template-loop task can be garbage-collected mid-run.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _spawn(_sweeper())
    _spawn(_loop_lag_monitor())
    if not isinstance(ate, ate_client.FakeAteClient):
        _spawn(_ensure_template_loop())
    yield


app = FastAPI(title="Agent Hotel concierge", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _basic_auth_ok(header: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    # compare_digest both fields so neither leaks length/prefix via timing.
    return secrets.compare_digest(user, HOTEL_USERNAME) & secrets.compare_digest(
        password, HOTEL_PASSWORD
    )


@app.middleware("http")
async def _request_telemetry(request, call_next):
    """Log anything slow or 5xx — the trail for diagnosing LB-side
    'no healthy upstream' reports (a slow /healthz can flap the LB check)."""
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request raised: %s %s after %.2fs",
            request.method, request.url.path, time.monotonic() - start,
        )
        raise
    took = time.monotonic() - start
    if response.status_code >= 500 or took > 1.0:
        logger.warning(
            "slow/error request: %s %s -> %d in %.2fs",
            request.method, request.url.path, response.status_code, took,
        )
    return response


@app.middleware("http")
async def _access_control(request, call_next):
    path = request.url.path
    if path == "/llm" or path.startswith("/internal/"):
        # Guests call these pod-to-pod; the HTTPRoute never exposes them. If a
        # request still arrives through the external LB (GCLB always stamps
        # "Via: 1.1 google"), refuse it — belt-and-braces on top of the route.
        if "google" in request.headers.get("via", "").lower():
            return JSONResponse(
                status_code=403, content={"detail": "cluster-internal endpoint"}
            )
    elif HOTEL_PASSWORD and path != "/healthz":
        # /healthz stays open for kubelet probes and the LB health check.
        if not _basic_auth_ok(request.headers.get("authorization", "")):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Agent Hotel"'},
            )
    return await call_next(request)


ate = ate_client.make_client()

# Concierge-side bookkeeping (not the source of truth — Substrate is):
#   activity[guest]  — monotonic ts of the last create/chat, gates yields;
#   waking[guest]    — True while a chat is blocked on a router-driven resume,
#                      so the UI can show "waking…" honestly;
#   in_flight[guest] — number of chats currently awaiting the guest's reply.
#     Suspending mid-reply kills the tunnel connection (the room deactivates
#     and the client sees 502) — and it genuinely happens: a freshly RESTORED
#     guest's linger loop sees its stale pre-suspend last_activity and asks
#     to be tucked in while it is still composing the very reply it was woken
#     for. Yields and sweeps must never land while a chat is in flight.
activity: dict[str, float] = {}
waking: dict[str, bool] = {}
in_flight: dict[str, int] = {}
template_name: str | None = None
template_error: str | None = None
# Guests currently admitted (a prefix of the roster). Grows via /api/guests/add,
# and auto-extends when existing actors are discovered beyond it (e.g. after a
# concierge restart) — Substrate, not this counter, is the source of truth.
active_count: int = GUEST_COUNT


def _active_personas(actors: dict | None = None) -> list[personas.Persona]:
    global active_count
    if actors:
        for i, p in enumerate(personas.PERSONAS):
            if p.name in actors and i >= active_count:
                active_count = i + 1
    return personas.PERSONAS[:active_count]


class ChatIn(BaseModel):
    message: str


class YieldIn(BaseModel):
    guest: str


class RoomsIn(BaseModel):
    count: int


class LlmIn(BaseModel):
    guest: str
    message: str
    history: list[dict] = []


def _valid_guest(name: str) -> str:
    name = name.strip().lower()
    if name not in personas.BY_NAME:
        raise HTTPException(status_code=404, detail=f"unknown guest: {name}")
    if personas.GUEST_NAMES.index(name) >= active_count:
        raise HTTPException(
            status_code=404,
            detail=f"{name} has not been admitted yet — use 'Add guest'",
        )
    return name


def _template() -> str:
    if isinstance(ate, ate_client.FakeAteClient):
        return "guest-fake"
    if template_name is None:
        raise HTTPException(
            status_code=503,
            detail=template_error
            or "ActorTemplate not registered yet (still starting up)",
        )
    return template_name


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# --- Hotel state ------------------------------------------------------------
@app.get("/api/state")
async def get_state():
    effective_template = template_name or (
        "guest-fake" if isinstance(ate, ate_client.FakeAteClient) else None
    )
    try:
        actors = await ate.list_actors()
        workers = await ate.list_workers(actors)
        golden = (
            await ate.template_status(effective_template)
            if effective_template else None
        )
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

    guests = []
    n_awake = n_suspended = n_napping = n_crashed = 0
    for p in _active_personas(actors):
        actor = actors.get(p.name)
        if actor is None:
            status = "vacant"
        elif waking.get(p.name):
            status = "waking"
        else:
            status = actor["status"]
        if status in ("awake", "waking", "suspending"):
            n_awake += 1
        elif status == "napping":
            n_napping += 1
        elif status == "suspended":
            n_suspended += 1
        elif status == "crashed":
            n_crashed += 1
        guests.append({
            "name": p.name,
            "emoji": p.emoji,
            "tagline": p.tagline,
            "status": status,
            "worker_pod": actor["worker_pod"] if actor else None,
            "crash": actor.get("crash") if actor else None,
        })

    checked_in = n_awake + n_napping + n_suspended + n_crashed
    n_workers = len(workers)
    return {
        "guests": guests,
        "workers": workers,
        "meter": {
            "checked_in": checked_in,
            "capacity": active_count,
            "roster": len(personas.PERSONAS),
            "workers": n_workers,
            "awake": n_awake,
            "napping": n_napping,
            "suspended": n_suspended,
            "crashed": n_crashed,
            "oversubscription": round(checked_in / n_workers, 1) if n_workers else 0,
        },
        "template": effective_template,
        "template_error": template_error,
        # Golden-snapshot readiness: new guests check in from it instantly;
        # until it exists a first chat cold-boots the image (slower), and a
        # failed build means every check-in fails — the UI shows both.
        "golden": golden,
    }


# Guests this replica has confirmed an EgressPolicy for (ensure is cheap and
# idempotent, but not free: one RPC per chat otherwise).
policy_ok: set[str] = set()


async def _ensure_policy(name: str) -> None:
    """A Substrate actor reaches nothing until it has an EgressPolicy (deny by
    default, down to DNS). Ensure it exists before any traffic — and keep
    ensuring on later chats, so a guest whose policy creation once failed
    (or that another replica created) never stays mute."""
    from app import template_manager

    if name in policy_ok:
        return
    await ate.ensure_egress_policy(name, template_manager.egress_rules(POD_NAMESPACE))
    policy_ok.add(name)


async def _check_in(name: str, template: str) -> None:
    """CreateActor + its EgressPolicy, in that order and before any traffic:
    the policy goes in right behind the actor, before the first chat resumes
    it and the guest calls Gemini."""
    await ate.create_actor(name, template)
    await _ensure_policy(name)


# --- Chat (the resume-on-request path) ---------------------------------------
@app.post("/api/guests/{name}/chat")
async def chat(name: str, body: ChatIn):
    name = _valid_guest(name)
    try:
        actors = await ate.list_actors()
        slept_as = actors.get(name, {}).get("status")
        was_suspended = slept_as in ASLEEP_STATES
        if name not in actors:
            await _check_in(name, _template())
        else:
            await _ensure_policy(name)
        if slept_as == "crashed":
            # A failed restore or an over-limit sandbox leaves the actor
            # CRASHED, and the router refuses to resume it. RevertActor
            # rewinds it to its last durable snapshot — the conversation up
            # to that point survives — and the chat below wakes it from there.
            if await ate.revert_actor(name):
                was_suspended, slept_as = True, "crashed"
                logger.info("reverted crashed guest %s to its last snapshot", name)
        activity[name] = time.monotonic()
        if was_suspended:
            waking[name] = True
        in_flight[name] = in_flight.get(name, 0) + 1
        try:
            result = await ate.chat(name, body.message)
        finally:
            waking.pop(name, None)
            n = in_flight.get(name, 1) - 1
            if n > 0:
                in_flight[name] = n
            else:
                in_flight.pop(name, None)
        activity[name] = time.monotonic()
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    # was_suspended: the guest was asleep (either tier) and the router restored
    # it for this message; slept_as says which tier it woke from.
    result["was_suspended"] = was_suspended
    result["slept_as"] = slept_as if was_suspended else None
    return result


@app.get("/api/guests/{name}/history")
async def history(name: str):
    name = _valid_guest(name)
    try:
        actors = await ate.list_actors()
        if name not in actors:
            return {"guest": name, "boot_id": None, "history": []}
        status = actors[name]["status"]
        if status in ASLEEP_STATES:
            # Reading history goes through the router, which would RESUME the
            # guest (and occupy a room) just to render a drawer. The UI keeps
            # its own transcript; the guest's RAM copy comes back on its next
            # message.
            return {"guest": name, "boot_id": None, "history": [], "asleep": status}
        activity[name] = time.monotonic()
        return await ate.history(name)
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


# --- Hotel management ---------------------------------------------------------
@app.post("/api/open")
async def open_hotel():
    """Check in every admitted guest (bounded; idle ones get swept to 💤)."""
    template = _template()
    names = [p.name for p in _active_personas()]

    async def check_in_all():
        sem = asyncio.Semaphore(3)

        async def one(guest: str):
            async with sem:
                try:
                    await _check_in(guest, template)
                    # Backdate activity so the sweeper tucks them in promptly.
                    activity.setdefault(
                        guest, time.monotonic() - SWEEP_IDLE_SECONDS + 5
                    )
                except AteError as e:
                    logger.warning("check-in failed for %s: %s", guest, e.detail)

        await asyncio.gather(*(one(g) for g in names))

    _spawn(check_in_all())
    return {"action": "open", "guests": len(names)}


@app.post("/api/guests/add")
async def add_guest():
    """Admit the next persona from the roster (they check in on first chat)."""
    global active_count
    if active_count >= len(personas.PERSONAS):
        raise HTTPException(
            status_code=409,
            detail=f"the roster is full ({len(personas.PERSONAS)} personas)",
        )
    active_count += 1
    p = personas.PERSONAS[active_count - 1]
    return {
        "added": p.name, "emoji": p.emoji, "tagline": p.tagline,
        "capacity": active_count, "roster": len(personas.PERSONAS),
    }


@app.post("/api/rooms")
async def set_rooms(body: RoomsIn):
    """Scale the WorkerPool (the hotel's rooms) up or down at runtime."""
    count = body.count
    if not 1 <= count <= MAX_ROOMS:
        raise HTTPException(
            status_code=422, detail=f"rooms must be between 1 and {MAX_ROOMS}"
        )
    try:
        actual = await ate.set_room_count(count)
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return {"rooms": actual}


async def _delete_actor_hard(guest: str) -> str | None:
    """Delete an actor whatever state it is in; returns an error or None.

    DeleteActor terminates a running sandbox, releases the room and collects
    the actor's snapshots — exactly what Reset means. A guest caught
    mid-transition can still answer a transient 409; retry briefly.
    """
    last = None
    for _ in range(10):
        try:
            await ate.delete_actor(guest)
            return None
        except AteError as e:
            last = e
        await asyncio.sleep(1.5)
    return f"{guest}: {last.detail}" if last else None


@app.post("/api/reset")
async def reset_hotel():
    """Delete every guest actor; conversations are gone for good."""
    results = await asyncio.gather(
        *(_delete_actor_hard(g) for g in personas.GUEST_NAMES)
    )
    errors = [r for r in results if r]
    activity.clear()
    waking.clear()
    in_flight.clear()
    policy_ok.clear()
    return {"action": "reset", "errors": errors}


# --- Guest-facing internal endpoints (cluster network, not the Gateway) -------
@app.post("/internal/yield")
async def guest_yield(body: YieldIn):
    """A guest says it's idle. Tuck it in — but only if the concierge agrees.

    Spoof-tolerant: anything on the cluster network can POST here, but the
    suspend only happens when our own activity record confirms the guest has
    really been idle for the linger period.
    """
    name = _valid_guest(body.guest)
    if in_flight.get(name):
        return {"guest": name, "suspended": False, "reason": "chat in flight"}
    last = activity.get(name)
    if last is not None and time.monotonic() - last < LINGER_SECONDS:
        return {"guest": name, "suspended": False, "reason": "recent activity"}
    try:
        # Asynchronous suspend: the room is free as soon as the snapshot is
        # on the node ("napping"); atelet uploads it to GCS in the background
        # ("suspended", wakes anywhere).
        suspended = await ate.suspend_actor(name)
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    if suspended:
        logger.info("guest %s yielded — room freed, snapshot uploading", name)
    return {"guest": name, "suspended": suspended}


@app.post("/llm")
async def llm_proxy(body: LlmIn):
    """Fallback LLM path (LLM_MODE=proxy in the guests): the concierge calls
    Gemini with its own key when the egress-injection path is not configured.

    The concierge is a normal pod where ADC just works; the guest sends its
    own history, so the concierge stays stateless about conversations.
    """
    from app.agent import llm as llm_mod

    name = _valid_guest(body.guest)
    text = await asyncio.to_thread(
        llm_mod.reply, name, body.message, list(body.history)
    )
    return {"guest": name, "reply": text}


# --- Background tasks ----------------------------------------------------------
async def _loop_lag_monitor():
    """Log when the event loop stalls.

    The kubelet probes and the LB health check both ride /healthz; if the
    single event loop is blocked, those time out and — with one replica —
    the whole site shows 'no healthy upstream'. This makes such stalls
    visible in the pod log with their duration.
    """
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(1.0)
        lag = time.monotonic() - t0 - 1.0
        if lag > 0.5:
            logger.warning("event loop stalled for %.2fs (blocked/overloaded)", lag)


# Consecutive /idlez failures per guest — a few sweeps of grace before a
# genuinely unreachable (wedged) actor is reaped.
_idlez_failures: dict[str, int] = {}


async def _sweeper():
    """Tuck in guests idle > SWEEP_IDLE_SECONDS.

    The backstop behind guest-initiated yields — and the mechanism that frees
    a room when all workers are busy and a sleeping guest gets a message.

    With TWO concierge replicas, this pod's activity/in_flight dicts cannot
    prove a guest is idle — the other replica may be mid-chat with it (the
    regression: replica B suspended guests replica A had just woken, 2s after
    restore). So before suspending, ask the GUEST itself via /idlez; it knows
    its own in-flight requests and last-chat time.
    """
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        await _sweep_once()


async def _sweep_once() -> None:
    try:
        actors = await ate.list_actors()
    except AteError:
        return
    now = time.monotonic()
    for name, actor in actors.items():
        # Only the hotel's guests. The template controller builds each
        # golden snapshot from a temporary actor in THIS atespace (named
        # after the template UID); suspending it would break the build.
        if name not in personas.BY_NAME:
            continue
        if actor["status"] != "awake" or waking.get(name) or in_flight.get(name):
            continue
        last = activity.get(name)
        if last is not None and now - last < SWEEP_IDLE_SECONDS:
            continue
        try:
            idle = await ate.idlez(name)
            _idlez_failures.pop(name, None)
            busy = idle.get("in_flight", 0) > 0 or (
                idle.get("idle_s") is not None
                and idle["idle_s"] < LINGER_SECONDS
            )
            if busy:
                activity[name] = now  # remember what we learned locally
                continue
        except AteError as e:
            # Unreachable guests get a few sweeps' grace (a just-restored
            # listener may still be warming up), then are reaped as wedged.
            n = _idlez_failures.get(name, 0) + 1
            _idlez_failures[name] = n
            if n < 3:
                logger.info("sweeper: %s unreachable (%d/3): %s",
                            name, n, e.detail)
                continue
        try:
            if await ate.suspend_actor(name):
                _idlez_failures.pop(name, None)
                logger.info("sweeper tucked %s in", name)
        except AteError as e:
            logger.warning("sweeper tuck-in %s failed: %s", name, e.detail)


async def _ensure_template_loop():
    """Register the ActorTemplate from our own image digest; retry until done.

    Runs in the background so a slow ate plane (or an image still pulling)
    never blocks /healthz — the Deployment must go Ready regardless.
    """
    global template_name, template_error
    from app import template_manager

    while template_name is None:
        try:
            image_ref = await asyncio.to_thread(
                template_manager.read_own_image_ref_sync
            )
            if image_ref is None:
                template_error = "own image digest not visible yet"
            else:
                body = template_manager.build_template_body(
                    "pending", POD_NAMESPACE, image_ref
                )
                name = template_manager.template_name_for(image_ref, body)
                body["metadata"]["name"] = name
                template_name = await ate.ensure_template(name, body)
                template_error = None
        except Exception as e:  # keep retrying; surface the cause in /api/state
            template_error = f"ActorTemplate registration failing: {e}"
            logger.warning(template_error, exc_info=True)
        if template_name is None:
            await asyncio.sleep(10)
    logger.info("ActorTemplate ready: %s", template_name)


# --- Static UI (mounted last so /api and /internal win) -------------------------
if os.path.isdir(FRONTEND_DIR):
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
