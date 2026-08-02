"""The guest agent — the process that lives inside a Substrate actor.

Everything interesting about this server is that it is *ordinary*: the
conversation lives in a plain Python list, the identity in plain module
globals. Substrate's checkpoint/restore is the persistence layer — after a
suspend → resume cycle (usually onto a different worker pod) all of this
state is simply still here.

Identity notes:
  - Every guest actor is created from ONE ActorTemplate, whose golden snapshot
    is taken from a single "golden pod". State minted at import time would
    therefore be IDENTICAL across all guests. So `boot_id` is minted lazily on
    the first real request — after the actor has been created and diverged
    from the golden snapshot — making it unique per actor AND stable across
    every later suspend/resume. Its stability is the proof RAM survived.
  - The guest learns its own name from the X-Actor-Name header the concierge
    sends (with the Host header `<name>.hotel.actors...` as fallback — the
    router's Envoy rewrites Host to the upstream pod IP, so Host alone is
    unreliable).

Zero-idle: LINGER_SECONDS after the last reply, the agent asks the concierge
to tuck it in (POST /internal/yield). The agent cannot call SuspendActor
itself — the plane runs JWT auth and any token baked into the golden snapshot
would be frozen and expired — so the concierge executes the suspend on its
behalf. See also the concierge's sweeper, which backstops lost yield calls.
"""

import asyncio
import logging
import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import personas
from app.agent import llm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINGER_SECONDS = float(os.environ.get("LINGER_SECONDS", "10"))
CONCIERGE_URL = os.environ.get("CONCIERGE_URL", "").rstrip("/")

app = FastAPI(title="Agent Hotel guest")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# --- The state Substrate checkpoints ----------------------------------------
state: dict = {
    "boot_id": None,          # minted on first request (see module docstring)
    "guest": None,            # learned from the Host header
    "history": [],            # [{"role": "user"|"assistant", "text": ...}]
    "last_activity": None,    # time.monotonic() of the last chat
    "yield_requested_at": 0.0,
}


def _ensure_identity(request: Request) -> None:
    if state["boot_id"] is None:
        state["boot_id"] = uuid.uuid4().hex[:12]
    # Prefer the explicit header: the router's Envoy rewrites Host to the
    # upstream pod IP, so the Host trick alone would name every guest after
    # the first octet of its pod's IP.
    name = request.headers.get("x-actor-name", "").strip().lower()
    if not name:
        host = request.headers.get("host", "")
        name = host.split(".", 1)[0].strip().lower()
        if name and not name.isidentifier():
            name = ""  # an IP-shaped Host label is not a guest name
    if name and state["guest"] is None:
        state["guest"] = name
    # Misdirect guard: during a suspend/resume shuffle the router can briefly
    # proxy a request to a room whose previous occupant is still bound. A 421
    # tells the concierge to retry rather than let guests answer for each
    # other (cross-talk shows the wrong persona AND pollutes checkpointed
    # history).
    if name and state["guest"] and name != state["guest"]:
        raise HTTPException(
            status_code=421,
            detail=f"this is {state['guest']}, not {name}",
        )


class ChatIn(BaseModel):
    message: str


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    # Must return 200 immediately with no external calls: the golden pod has
    # to go Ready for the golden snapshot to be taken at all.
    return {"status": "ok"}


@app.post("/chat")
async def chat(body: ChatIn, request: Request):
    _ensure_identity(request)
    guest = state["guest"] or "guest"
    history = state["history"]
    # The LLM call may block for seconds; keep the event loop free.
    reply = await asyncio.to_thread(llm.reply, guest, body.message, list(history))
    history.append({"role": "user", "text": body.message})
    history.append({"role": "assistant", "text": reply})
    state["last_activity"] = time.monotonic()
    return {
        "guest": guest,
        "reply": reply,
        "boot_id": state["boot_id"],
        "history_len": len(history),
    }


@app.get("/history")
async def get_history(request: Request):
    _ensure_identity(request)
    return {
        "guest": state["guest"],
        "boot_id": state["boot_id"],
        "history": state["history"],
    }


async def _request_yield() -> None:
    """Tell the concierge this guest is idle and can be suspended."""
    if not CONCIERGE_URL or not state["guest"]:
        return
    try:
        import httpx  # lazy so offline tests don't need it

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{CONCIERGE_URL}/internal/yield", json={"guest": state["guest"]}
            )
        state["yield_requested_at"] = time.monotonic()
        logger.info("guest %s requested yield", state["guest"])
    except Exception:
        # Best-effort: the concierge's sweeper is the backstop.
        logger.warning("yield request failed", exc_info=True)


async def _linger_loop() -> None:
    """Fire a yield request once the guest has been idle for LINGER_SECONDS."""
    while True:
        await asyncio.sleep(1.0)
        last = state["last_activity"]
        if last is None:
            continue
        idle = time.monotonic() - last
        if idle >= LINGER_SECONDS and state["yield_requested_at"] < last:
            await _request_yield()


@app.on_event("startup")
async def _start_linger():
    if LINGER_SECONDS > 0:
        asyncio.create_task(_linger_loop())
