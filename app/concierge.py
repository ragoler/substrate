"""The Agent Hotel concierge — the one service this demo actually deploys.

Responsibilities:
  * serve the hotel floor-plan UI (frontend/) and the /api endpoints;
  * drive Substrate's control plane: check guests in (CreateActor), tuck idle
    guests away (SuspendActor), reset the hotel (DeleteActor);
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
import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import ate_client, personas
from app.ate_client import AteError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINGER_SECONDS = float(os.environ.get("LINGER_SECONDS", "10"))
SWEEP_IDLE_SECONDS = float(os.environ.get("SWEEP_IDLE_SECONDS", "30"))
SWEEP_INTERVAL_S = float(os.environ.get("SWEEP_INTERVAL_S", "5"))
POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "default")
FRONTEND_DIR = os.environ.get(
    "FRONTEND_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "frontend"),
)

app = FastAPI(title="Agent Hotel concierge")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

ate = ate_client.make_client()

# Concierge-side bookkeeping (not the source of truth — Substrate is):
#   activity[guest]  — monotonic ts of the last create/chat, gates yields;
#   waking[guest]    — True while a chat is blocked on a router-driven resume,
#                      so the UI can show "waking…" honestly.
activity: dict[str, float] = {}
waking: dict[str, bool] = {}
template_name: str | None = None
template_error: str | None = None


class ChatIn(BaseModel):
    message: str


class YieldIn(BaseModel):
    guest: str


class LlmIn(BaseModel):
    guest: str
    message: str
    history: list[dict] = []


def _valid_guest(name: str) -> str:
    name = name.strip().lower()
    if name not in personas.BY_NAME:
        raise HTTPException(status_code=404, detail=f"unknown guest: {name}")
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
    try:
        actors = await ate.list_actors()
        workers = await ate.list_workers()
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

    guests = []
    n_awake = n_suspended = 0
    for p in personas.PERSONAS:
        actor = actors.get(p.name)
        if actor is None:
            status = "vacant"
        elif waking.get(p.name):
            status = "waking"
        else:
            status = actor["status"]
        if status in ("awake", "waking", "suspending"):
            n_awake += 1
        elif status == "suspended":
            n_suspended += 1
        guests.append({
            "name": p.name,
            "emoji": p.emoji,
            "tagline": p.tagline,
            "status": status,
            "worker_pod": actor["worker_pod"] if actor else None,
        })

    checked_in = n_awake + n_suspended
    n_workers = len(workers)
    return {
        "guests": guests,
        "workers": workers,
        "meter": {
            "checked_in": checked_in,
            "capacity": len(personas.PERSONAS),
            "workers": n_workers,
            "awake": n_awake,
            "suspended": n_suspended,
            "oversubscription": round(checked_in / n_workers, 1) if n_workers else 0,
        },
        "template": template_name or ("guest-fake" if isinstance(
            ate, ate_client.FakeAteClient) else None),
        "template_error": template_error,
    }


# --- Chat (the resume-on-request path) ---------------------------------------
@app.post("/api/guests/{name}/chat")
async def chat(name: str, body: ChatIn):
    name = _valid_guest(name)
    try:
        actors = await ate.list_actors()
        was_suspended = actors.get(name, {}).get("status") == "suspended"
        if name not in actors:
            await ate.create_actor(name, POD_NAMESPACE, _template())
        activity[name] = time.monotonic()
        if was_suspended:
            waking[name] = True
        try:
            result = await ate.chat(name, body.message)
        finally:
            waking.pop(name, None)
        activity[name] = time.monotonic()
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    result["was_suspended"] = was_suspended
    return result


@app.get("/api/guests/{name}/history")
async def history(name: str):
    name = _valid_guest(name)
    try:
        actors = await ate.list_actors()
        if name not in actors:
            return {"guest": name, "boot_id": None, "history": []}
        activity[name] = time.monotonic()
        return await ate.history(name)
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


# --- Hotel management ---------------------------------------------------------
@app.post("/api/open")
async def open_hotel():
    """Check in every guest (bounded concurrency; idle ones get swept to 💤)."""
    template = _template()

    async def check_in_all():
        sem = asyncio.Semaphore(3)

        async def one(guest: str):
            async with sem:
                try:
                    await ate.create_actor(guest, POD_NAMESPACE, template)
                    # Backdate activity so the sweeper tucks them in promptly.
                    activity.setdefault(
                        guest, time.monotonic() - SWEEP_IDLE_SECONDS + 5
                    )
                except AteError as e:
                    logger.warning("check-in failed for %s: %s", guest, e.detail)

        await asyncio.gather(*(one(g) for g in personas.GUEST_NAMES))

    asyncio.create_task(check_in_all())
    return {"action": "open", "guests": len(personas.GUEST_NAMES)}


@app.post("/api/reset")
async def reset_hotel():
    """Delete every guest actor; conversations are gone for good."""
    errors = []
    for guest in personas.GUEST_NAMES:
        try:
            await ate.delete_actor(guest)
        except AteError as e:
            errors.append(f"{guest}: {e.detail}")
    activity.clear()
    waking.clear()
    return {"action": "reset", "errors": errors}


# --- Guest-facing internal endpoints (cluster network, not the Gateway) -------
@app.post("/internal/yield")
async def guest_yield(body: YieldIn):
    """A guest says it's idle. Suspend it — but only if the concierge agrees.

    Spoof-tolerant: anything on the cluster network can POST here, but the
    suspend only happens when our own activity record confirms the guest has
    really been idle for the linger period.
    """
    name = _valid_guest(body.guest)
    last = activity.get(name)
    if last is not None and time.monotonic() - last < LINGER_SECONDS:
        return {"guest": name, "suspended": False, "reason": "recent activity"}
    try:
        suspended = await ate.suspend_actor(name)
    except AteError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    if suspended:
        logger.info("guest %s yielded — suspended to snapshot", name)
    return {"guest": name, "suspended": suspended}


@app.post("/llm")
async def llm_proxy(body: LlmIn):
    """Fallback LLM path for guests whose sandbox can't reach Vertex (ADC).

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
async def _sweeper():
    """Suspend guests idle > SWEEP_IDLE_SECONDS.

    The backstop behind guest-initiated yields — and the mechanism that frees
    a room when all workers are busy and a sleeping guest gets a message.
    """
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        try:
            actors = await ate.list_actors()
        except AteError:
            continue
        now = time.monotonic()
        for name, actor in actors.items():
            if actor["status"] != "awake" or waking.get(name):
                continue
            last = activity.get(name)
            if last is None or now - last >= SWEEP_IDLE_SECONDS:
                try:
                    if await ate.suspend_actor(name):
                        logger.info("sweeper tucked %s in", name)
                except AteError as e:
                    logger.warning("sweeper suspend %s failed: %s", name, e.detail)


async def _ensure_template_loop():
    """Register the ActorTemplate from our own image digest; retry until done.

    Runs in the background so a slow ate plane (or an image still pulling)
    never blocks /healthz — the Deployment must go Ready regardless.
    """
    global template_name, template_error
    from app import template_manager

    while template_name is None:
        try:
            template_name = await asyncio.to_thread(
                template_manager.ensure_template_sync, POD_NAMESPACE
            )
            template_error = None
            if template_name is None:
                template_error = "own image digest not visible yet"
        except Exception as e:  # keep retrying; surface the cause in /api/state
            template_error = f"ActorTemplate registration failing: {e}"
            logger.warning(template_error, exc_info=True)
        if template_name is None:
            await asyncio.sleep(10)
    logger.info("ActorTemplate ready: %s", template_name)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_sweeper())
    if not isinstance(ate, ate_client.FakeAteClient):
        asyncio.create_task(_ensure_template_loop())


# --- Static UI (mounted last so /api and /internal win) -------------------------
if os.path.isdir(FRONTEND_DIR):
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
