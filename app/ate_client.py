"""Clients for the Agent Substrate (ate) control + data planes.

Two interchangeable implementations, selected by env ATE_MODE:

  real — talks to the actual plane installed by
         cluster/install-substrate-hotel-prereq.sh:
           * control: gRPC to ate-api-server (api.ate-system.svc:443).
             TLS is verified against the static CA the installer minted
             (Secret ate-system/servicedns-ca); the bearer token is this
             pod's own projected ServiceAccount token with audience
             api.ate-system.svc (the api-server in --auth-mode=jwt accepts
             any cluster-issuer token with that audience).
           * data: HTTP through atenet-router with a
             Host: <guest>.hotel.actors... header. The router transparently
             RESUMES a suspended guest before proxying — resume-on-request.

  fake — a complete in-memory hotel (3 worker slots, per-guest in-process
         agent state) so the whole app + test suite runs with no cluster,
         no docker, and none of grpc/kubernetes/google-genai installed.

grpc / kubernetes imports live inside RealAteClient so `fake` mode stays
dependency-free.
"""

import asyncio
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

ATESPACE = "hotel"
WORKER_POOL = os.environ.get("WORKER_POOL", "substrate-hotel")
ATE_API_TARGET = os.environ.get("ATE_API_TARGET", "api.ate-system.svc:443")
ATE_SYSTEM_NS = "ate-system"
ROUTER_URL = os.environ.get("ROUTER_URL", "http://atenet-router.ate-system.svc/")
ACTOR_HOST_SUFFIX = f".{ATESPACE}.actors.resources.substrate.ate.dev"
# Chat through the router may transparently resume a suspended guest
# (snapshot restore + worker bind) — tens of seconds, not a proxy hop.
CHAT_TIMEOUT_S = float(os.environ.get("CHAT_TIMEOUT_S", "120"))
# Kubelet rotates the projected token; re-read it from disk at most this often.
PROJECTED_TOKEN_PATH = os.environ.get(
    "ATE_TOKEN_PATH", "/var/run/secrets/tokens/ate-api-token"
)
_TOKEN_REREAD_S = 300.0


class AteError(Exception):
    """Control/data-plane failure with an HTTP-ish status for the API layer."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------
# Real plane
# --------------------------------------------------------------------------
class RealAteClient:
    def __init__(self):
        import grpc  # noqa: F401 — fail fast at construction if missing

        self._channel = None
        self._ca_pem: bytes | None = None
        self._token: str | None = None
        self._token_read_at = 0.0
        self._atespace_ready = False

    # -- auth plumbing ------------------------------------------------------
    def _read_ca_pem_sync(self) -> bytes:
        """CA from Secret ate-system/servicedns-ca (name-scoped ClusterRole)."""
        import base64

        from kubernetes import client, config

        config.load_incluster_config()
        secret = client.CoreV1Api().read_namespaced_secret(
            "servicedns-ca", ATE_SYSTEM_NS
        )
        return base64.b64decode(secret.data["trust-bundle.pem"])

    async def _get_token(self) -> str:
        now = time.monotonic()
        if self._token is None or now - self._token_read_at > _TOKEN_REREAD_S:
            self._token = (
                await asyncio.to_thread(
                    lambda: open(PROJECTED_TOKEN_PATH).read().strip()
                )
            )
            self._token_read_at = now
        return self._token

    async def _stub(self):
        import grpc

        from ateapipb import ateapi_pb2_grpc

        if self._channel is None:
            if self._ca_pem is None:
                self._ca_pem = await asyncio.to_thread(self._read_ca_pem_sync)
            self._channel = grpc.aio.secure_channel(
                ATE_API_TARGET,
                grpc.ssl_channel_credentials(root_certificates=self._ca_pem),
            )
        return ateapi_pb2_grpc.ControlStub(self._channel)

    async def _md(self) -> tuple:
        return (("authorization", f"Bearer {await self._get_token()}"),)

    @staticmethod
    def _wrap(e) -> AteError:
        import grpc

        code = e.code()
        if code == grpc.StatusCode.NOT_FOUND:
            return AteError(404, e.details())
        if code in (grpc.StatusCode.ALREADY_EXISTS, grpc.StatusCode.FAILED_PRECONDITION):
            return AteError(409, e.details())
        if code == grpc.StatusCode.UNAVAILABLE:
            return AteError(503, "ate api-server unavailable")
        return AteError(502, f"{code.name}: {e.details()}")

    def _ref(self, name: str):
        from ateapipb import ateapi_pb2

        return ateapi_pb2.ActorRef(atespace=ATESPACE, name=name)

    # -- control plane ------------------------------------------------------
    async def ensure_atespace(self) -> None:
        import grpc

        from ateapipb import ateapi_pb2

        if self._atespace_ready:
            return
        stub = await self._stub()
        try:
            await stub.CreateAtespace(
                ateapi_pb2.CreateAtespaceRequest(name=ATESPACE),
                metadata=await self._md(),
            )
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.ALREADY_EXISTS:
                raise self._wrap(e)
        self._atespace_ready = True

    async def create_actor(self, name: str, template_ns: str, template_name: str) -> None:
        import grpc

        from ateapipb import ateapi_pb2

        await self.ensure_atespace()
        stub = await self._stub()
        try:
            await stub.CreateActor(
                ateapi_pb2.CreateActorRequest(
                    actor_ref=self._ref(name),
                    actor_template_namespace=template_ns,
                    actor_template_name=template_name,
                    worker_selector=ateapi_pb2.Selector(
                        match_labels={"workload": WORKER_POOL}
                    ),
                ),
                metadata=await self._md(),
            )
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.ALREADY_EXISTS:
                raise self._wrap(e)

    async def suspend_actor(self, name: str) -> bool:
        """Best-effort suspend; False when already suspending/absent."""
        import grpc

        from ateapipb import ateapi_pb2

        stub = await self._stub()
        try:
            await stub.SuspendActor(
                ateapi_pb2.SuspendActorRequest(actor_ref=self._ref(name)),
                metadata=await self._md(),
            )
            return True
        except grpc.aio.AioRpcError as e:
            if e.code() in (
                grpc.StatusCode.FAILED_PRECONDITION,
                grpc.StatusCode.NOT_FOUND,
                grpc.StatusCode.ALREADY_EXISTS,
            ):
                return False
            raise self._wrap(e)

    async def delete_actor(self, name: str) -> None:
        import grpc

        from ateapipb import ateapi_pb2

        stub = await self._stub()
        try:
            await stub.DeleteActor(
                ateapi_pb2.DeleteActorRequest(actor_ref=self._ref(name)),
                metadata=await self._md(),
            )
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise self._wrap(e)

    async def list_actors(self) -> dict[str, dict]:
        """{name: {"status": ..., "worker_pod": ...}} for the hotel atespace."""
        import grpc

        from ateapipb import ateapi_pb2

        await self.ensure_atespace()
        stub = await self._stub()
        md = await self._md()
        names = {
            ateapi_pb2.Actor.STATUS_RESUMING: "waking",
            ateapi_pb2.Actor.STATUS_RUNNING: "awake",
            ateapi_pb2.Actor.STATUS_SUSPENDING: "suspending",
            ateapi_pb2.Actor.STATUS_SUSPENDED: "suspended",
            ateapi_pb2.Actor.STATUS_PAUSING: "suspending",
            ateapi_pb2.Actor.STATUS_PAUSED: "suspended",
        }
        out: dict[str, dict] = {}
        try:
            page_token = ""
            while True:
                resp = await stub.ListActors(
                    ateapi_pb2.ListActorsRequest(
                        page_size=100, page_token=page_token, atespace=ATESPACE
                    ),
                    metadata=md,
                )
                for actor in resp.actors:
                    out[actor.actor_id] = {
                        "status": names.get(actor.status, "unknown"),
                        "worker_pod": actor.ateom_pod_name or None,
                    }
                page_token = resp.next_page_token
                if not page_token:
                    break
        except grpc.aio.AioRpcError as e:
            raise self._wrap(e)
        return out

    async def list_workers(self) -> list[dict]:
        import grpc

        from ateapipb import ateapi_pb2

        stub = await self._stub()
        try:
            resp = await stub.ListWorkers(
                ateapi_pb2.ListWorkersRequest(), metadata=await self._md()
            )
        except grpc.aio.AioRpcError as e:
            raise self._wrap(e)
        return [
            {
                "pod": w.worker_pod,
                "node": w.node_name,
                "occupant": (
                    w.assignment.actor.name if w.HasField("assignment") else None
                ),
            }
            for w in resp.workers
            if w.worker_pool == WORKER_POOL
        ]

    async def set_room_count(self, count: int) -> int:
        """Scale the hotel by patching the WorkerPool's replicas.

        The ate-controller reconciles the worker Deployment to the new size;
        new rooms take a little while to warm up. Shrinking may evict an
        awake guest's worker — its next message resumes it from a snapshot,
        which is itself a nice demo of the mechanics.
        """
        def _patch():
            from kubernetes import client, config

            config.load_incluster_config()
            client.CustomObjectsApi().patch_namespaced_custom_object(
                group="ate.dev", version="v1alpha1",
                namespace=os.environ.get("POD_NAMESPACE", "default"),
                plural="workerpools", name=WORKER_POOL,
                body={"spec": {"replicas": count}},
            )

        try:
            await asyncio.to_thread(_patch)
        except Exception as e:
            raise AteError(502, f"WorkerPool patch failed: {e}")
        return count

    # -- data plane (resume-on-request) --------------------------------------
    async def _router(self, method: str, name: str, path: str, json_body=None) -> dict:
        import httpx

        host = f"{name}{ACTOR_HOST_SUFFIX}"
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_S) as http:
                resp = await http.request(
                    method, ROUTER_URL.rstrip("/") + path,
                    headers={"Host": host}, json=json_body,
                )
        except httpx.HTTPError as e:
            raise AteError(502, f"router request failed: {e}")
        if resp.status_code != 200:
            raise AteError(502, f"router returned {resp.status_code} for {name}")
        try:
            return resp.json()
        except Exception:
            raise AteError(502, "guest returned a non-JSON response")

    async def chat(self, name: str, message: str) -> dict:
        return await self._router("POST", name, "/chat", {"message": message})

    async def history(self, name: str) -> dict:
        return await self._router("GET", name, "/history")


# --------------------------------------------------------------------------
# Fake plane (offline dev + tests)
# --------------------------------------------------------------------------
class FakeAteClient:
    """In-memory hotel: N worker slots, per-guest agent state, zero deps.

    Mimics the observable semantics the concierge relies on:
      * a guest occupies a worker slot while awake, none while suspended;
      * chatting with a suspended guest transparently "resumes" it (onto any
        free slot, evicting nobody — resume waits for a free slot);
      * RAM state (history, boot_id) survives suspend/resume.
    """

    def __init__(self, slots: int | None = None, resume_latency: float = 0.0):
        if slots is None:
            slots = int(os.environ.get("WORKER_REPLICAS", "3"))
        self._next_slot = slots
        self.slots = [f"fake-worker-{i}" for i in range(slots)]
        self.resume_latency = resume_latency
        self.actors: dict[str, dict] = {}  # name -> {status, worker, boot_id, history}

    def _free_slot(self) -> str | None:
        used = {a["worker"] for a in self.actors.values() if a["worker"]}
        for s in self.slots:
            if s not in used:
                return s
        return None

    async def ensure_atespace(self) -> None:
        return None

    async def create_actor(self, name: str, template_ns: str, template_name: str) -> None:
        if name in self.actors:
            return
        slot = self._free_slot()
        if slot is None:
            raise AteError(409, "no free worker slot")
        self.actors[name] = {
            "status": "awake",
            "worker": slot,
            "boot_id": uuid.uuid4().hex[:12],
            "history": [],
        }

    async def suspend_actor(self, name: str) -> bool:
        actor = self.actors.get(name)
        if actor is None or actor["status"] != "awake":
            return False
        actor["status"] = "suspended"
        actor["worker"] = None
        return True

    async def delete_actor(self, name: str) -> None:
        self.actors.pop(name, None)

    async def list_actors(self) -> dict[str, dict]:
        return {
            n: {"status": a["status"], "worker_pod": a["worker"]}
            for n, a in self.actors.items()
        }

    async def list_workers(self) -> list[dict]:
        occupants = {
            a["worker"]: n for n, a in self.actors.items() if a["worker"]
        }
        return [
            {"pod": s, "node": "fake-node", "occupant": occupants.get(s)}
            for s in self.slots
        ]

    async def set_room_count(self, count: int) -> int:
        occupied = {a["worker"] for a in self.actors.values() if a["worker"]}
        while len(self.slots) < count:
            self.slots.append(f"fake-worker-{self._next_slot}")
            self._next_slot += 1
        while len(self.slots) > count:
            # Drop a free slot; mimic real eviction if only occupied ones remain
            # (the guest's RAM lives on — its next message resumes it).
            free = [s for s in self.slots if s not in occupied]
            victim = free[-1] if free else self.slots[-1]
            self.slots.remove(victim)
            for actor in self.actors.values():
                if actor["worker"] == victim:
                    actor["status"] = "suspended"
                    actor["worker"] = None
        return len(self.slots)

    async def _wake(self, name: str) -> dict:
        actor = self.actors.get(name)
        if actor is None:
            raise AteError(404, f"guest {name} is not checked in")
        if actor["status"] == "suspended":
            # Resume-on-request: wait for a free slot, restore RAM as-is.
            deadline = time.monotonic() + 5.0
            slot = self._free_slot()
            while slot is None and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
                slot = self._free_slot()
            if slot is None:
                raise AteError(503, "no free worker slot to resume onto")
            if self.resume_latency:
                await asyncio.sleep(self.resume_latency)
            actor["status"] = "awake"
            actor["worker"] = slot
        return actor

    async def chat(self, name: str, message: str) -> dict:
        from app import personas

        actor = await self._wake(name)
        reply = personas.canned_reply(name, message, actor["history"])
        actor["history"].append({"role": "user", "text": message})
        actor["history"].append({"role": "assistant", "text": reply})
        return {
            "guest": name,
            "reply": reply,
            "boot_id": actor["boot_id"],
            "history_len": len(actor["history"]),
        }

    async def history(self, name: str) -> dict:
        actor = await self._wake(name)
        return {
            "guest": name,
            "boot_id": actor["boot_id"],
            "history": actor["history"],
        }


def make_client():
    mode = os.environ.get("ATE_MODE", "real").strip().lower()
    if mode == "fake":
        logger.info("ATE_MODE=fake — using the in-memory hotel")
        return FakeAteClient()
    return RealAteClient()
