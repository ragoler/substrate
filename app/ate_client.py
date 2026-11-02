"""Clients for the Agent Substrate (ate) control + data planes.

Two interchangeable implementations, selected by env ATE_MODE:

  real — talks to the plane installed by cluster/install-substrate-hotel-prereq.sh
         (upstream release images, see lib.sh UPSTREAM_REF):
           * control: gRPC to ate-api-server (api.ate-system.svc:443).
             TLS is verified against the servicedns ClusterTrustBundle that
             kubelet projects into the pod; the bearer token is the pod's own
             projected ServiceAccount token with audience api.ate-system.svc.
             What the token may do is decided by Substrate's access policies
             (deploy_app.sh grants this ServiceAccount `editor` on the hotel
             atespace and `viewer` globally).
           * data: HTTPS through atenet-router with the pod's PodCertificate
             as client certificate (the router admits only allowlisted SPIFFE
             IDs) and the `ate-target-actor: hotel/<guest>` routing header.
             The router transparently RESUMES a suspended guest before
             proxying — resume-on-request.

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

ATESPACE = os.environ.get("ATESPACE", "hotel")
WORKER_POOL = os.environ.get("WORKER_POOL", "substrate-hotel")
ATE_API_TARGET = os.environ.get("ATE_API_TARGET", "api.ate-system.svc:443")
ROUTER_URL = os.environ.get("ROUTER_URL", "https://atenet-router.ate-system.svc/")
# Projected by kubelet (see infra/deployment.yaml):
#   servicedns-ca.pem      — ClusterTrustBundle of the servicedns signer; the
#                            serving CA of ate-api-server AND atenet-router
#   credential-bundle.pem  — this pod's PodCertificate (chain + key) from the
#                            podidentity signer: the router's client cert
ATE_CA_FILE = os.environ.get("ATE_CA_FILE", "/run/ate-client/servicedns-ca.pem")
ATE_CLIENT_CERT_FILE = os.environ.get(
    "ATE_CLIENT_CERT_FILE", "/run/ate-client/credential-bundle.pem"
)
PROJECTED_TOKEN_PATH = os.environ.get(
    "ATE_TOKEN_PATH", "/var/run/secrets/tokens/ate-api-token"
)
# Chat through the router may transparently resume a suspended guest
# (snapshot restore + worker bind) — tens of seconds, not a proxy hop.
CHAT_TIMEOUT_S = float(os.environ.get("CHAT_TIMEOUT_S", "120"))
# Kubelet rotates the projected token and certificate; re-read them from disk
# at most this often (the client cert lives ~24h, the token 1h).
_TOKEN_REREAD_S = 300.0
_HTTP_CLIENT_REFRESH_S = 600.0

# How the concierge names Substrate actor states to the UI.
STATUS_AWAKE = "awake"
STATUS_WAKING = "waking"
STATUS_SUSPENDING = "suspending"
STATUS_NAPPING = "napping"        # suspended; snapshot still only on the node
STATUS_SUSPENDED = "suspended"    # suspended; snapshot durable in GCS
STATUS_CRASHED = "crashed"
ASLEEP_STATES = (STATUS_NAPPING, STATUS_SUSPENDED)


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
        self._token: str | None = None
        self._token_read_at = 0.0
        self._http = None  # lazy shared httpx.AsyncClient (connection reuse)
        self._http_made_at = 0.0

    # -- auth plumbing ------------------------------------------------------
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
            ca_pem = await asyncio.to_thread(lambda: open(ATE_CA_FILE, "rb").read())
            self._channel = grpc.aio.secure_channel(
                ATE_API_TARGET,
                grpc.ssl_channel_credentials(root_certificates=ca_pem),
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
        if code in (grpc.StatusCode.ALREADY_EXISTS, grpc.StatusCode.FAILED_PRECONDITION,
                    grpc.StatusCode.ABORTED):
            return AteError(409, e.details())
        if code == grpc.StatusCode.PERMISSION_DENIED:
            return AteError(
                403,
                "ate-api-server denied the concierge: " + e.details()
                + " (deploy_app.sh grants its ServiceAccount editor on the "
                "hotel atespace and viewer globally)",
            )
        if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
            return AteError(503, "no room free: " + e.details())
        if code == grpc.StatusCode.UNAVAILABLE:
            return AteError(503, "ate api-server unavailable")
        return AteError(502, f"{code.name}: {e.details()}")

    def _ref(self, name: str):
        from ateapipb import ateapi_pb2

        return ateapi_pb2.ObjectRef(atespace=ATESPACE, name=name)

    # -- control plane: template ----------------------------------------------
    async def ensure_template(self, name: str, body: dict) -> str:
        """Create the ActorTemplate `body` (protojson-shaped) if absent.

        Templates are immutable ate API resources in the hotel atespace, so
        ALREADY_EXISTS means a previous concierge replica (or rollout) already
        registered this image digest — that is success. Returns the name.
        """
        import grpc
        from google.protobuf import json_format

        from ateapipb import ateapi_pb2

        template = json_format.ParseDict(body, ateapi_pb2.ActorTemplate())
        stub = await self._stub()
        try:
            await stub.CreateActorTemplate(
                ateapi_pb2.CreateActorTemplateRequest(actor_template=template),
                metadata=await self._md(),
            )
            logger.info("created ActorTemplate %s/%s", ATESPACE, name)
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.ALREADY_EXISTS:
                raise self._wrap(e)
        return name

    async def template_status(self, name: str) -> dict:
        """Golden-snapshot readiness of a template.

        Substrate boots one golden actor per template, snapshots it and
        publishes the snapshot as a Tag; new actors start from that tag in
        moments. Until it exists a first chat cold-boots the image; if it
        failed, every check-in fails — surface both states to the UI.
        """
        import grpc

        from ateapipb import ateapi_pb2

        stub = await self._stub()
        try:
            tmpl = await stub.GetActorTemplate(
                ateapi_pb2.GetActorTemplateRequest(actor_template=self._ref(name)),
                metadata=await self._md(),
            )
        except grpc.aio.AioRpcError as e:
            raise self._wrap(e)
        golden = tmpl.status.golden_snapshot_status
        return {
            "golden_ready": bool(golden.golden_tag.name),
            "golden_tag": golden.golden_tag.name or None,
            "error": golden.error_message or None,
        }

    # -- control plane: actors ---------------------------------------------------
    async def create_actor(self, name: str, template_name: str) -> None:
        import grpc

        from ateapipb import ateapi_pb2

        stub = await self._stub()
        try:
            await stub.CreateActor(
                ateapi_pb2.CreateActorRequest(
                    actor=ateapi_pb2.Actor(
                        metadata=ateapi_pb2.ResourceMetadata(
                            atespace=ATESPACE, name=name
                        ),
                        # The template lives in the same atespace as the actor.
                        actor_template=self._ref(template_name),
                        worker_selector=ateapi_pb2.Selector(
                            match_labels={"workload": WORKER_POOL}
                        ),
                    )
                ),
                metadata=await self._md(),
            )
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.ALREADY_EXISTS:
                raise self._wrap(e)

    async def ensure_egress_policy(self, name: str, rules: list[dict]) -> None:
        """Give a guest its network allowlist (protojson-shaped EgressRules).

        Without an EgressPolicy an actor resolves no DNS name and opens no
        tunnel: the sandbox's DNS relay answers NXDOMAIN and the egress
        gateway refuses the CONNECT. The policy must exist before the
        guest's first outbound call — i.e. right after CreateActor, before
        the first chat. ALREADY_EXISTS is success (one policy per actor).
        """
        import grpc
        from google.protobuf import json_format

        from ateapipb import ateapi_pb2

        policy = json_format.ParseDict(
            # One policy per actor, named "default", in the actor's atespace.
            {"metadata": {"atespace": ATESPACE, "name": "default"}, "rules": rules},
            ateapi_pb2.EgressPolicy(),
        )
        stub = await self._stub()
        try:
            await stub.CreateActorEgressPolicy(
                ateapi_pb2.CreateActorEgressPolicyRequest(
                    actor=self._ref(name), egress_policy=policy
                ),
                metadata=await self._md(),
            )
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.ALREADY_EXISTS:
                raise self._wrap(e)

    async def suspend_actor(self, name: str, durable: bool = False) -> bool:
        """Checkpoint the guest and free its room.

        The default (asynchronous) suspend returns as soon as the snapshot is
        captured on the worker's node — sub-second — and atelet uploads it to
        GCS in the background; the actor can resume anywhere once the upload
        lands and immediately on its own node before that. durable=True
        waits for the upload. False when already asleep/absent/in flux.
        """
        import grpc

        from ateapipb import ateapi_pb2

        mode = (ateapi_pb2.DURABILITY_MODE_SYNCHRONOUS if durable
                else ateapi_pb2.DURABILITY_MODE_ASYNCHRONOUS)
        stub = await self._stub()
        try:
            await stub.SuspendActor(
                ateapi_pb2.SuspendActorRequest(
                    actor=self._ref(name),
                    options=ateapi_pb2.SuspendOptions(mode=mode),
                ),
                metadata=await self._md(),
            )
            return True
        except grpc.aio.AioRpcError as e:
            if e.code() in (
                grpc.StatusCode.FAILED_PRECONDITION,
                grpc.StatusCode.NOT_FOUND,
                grpc.StatusCode.ALREADY_EXISTS,
                grpc.StatusCode.ABORTED,
            ):
                return False
            raise self._wrap(e)

    async def revert_actor(self, name: str) -> bool:
        """Rewind a CRASHED (or stuck RUNNING) guest to its last durable
        snapshot, leaving it SUSPENDED and resumable. False when the plane
        says there is nothing to revert (already at its durable snapshot)."""
        import grpc

        from ateapipb import ateapi_pb2

        stub = await self._stub()
        try:
            await stub.RevertActor(
                ateapi_pb2.RevertActorRequest(actor=self._ref(name)),
                metadata=await self._md(),
            )
            return True
        except grpc.aio.AioRpcError as e:
            if e.code() in (grpc.StatusCode.FAILED_PRECONDITION,
                            grpc.StatusCode.NOT_FOUND,
                            grpc.StatusCode.ABORTED):
                return False
            raise self._wrap(e)

    async def delete_actor(self, name: str) -> None:
        """Delete an actor in ANY state: the plane terminates a running
        sandbox, releases the room and collects the actor's snapshots."""
        import grpc

        from ateapipb import ateapi_pb2

        stub = await self._stub()
        try:
            await stub.DeleteActor(
                ateapi_pb2.DeleteActorRequest(actor=self._ref(name)),
                metadata=await self._md(),
            )
        except grpc.aio.AioRpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise self._wrap(e)

    @staticmethod
    def _status_of(actor) -> str:
        """Map an Actor's state (+ snapshot durability) onto hotel vocabulary."""
        from ateapipb import ateapi_pb2

        st = actor.status.state
        if st == ateapi_pb2.ACTOR_STATE_RESUMING:
            return STATUS_WAKING
        if st == ateapi_pb2.ACTOR_STATE_RUNNING:
            return STATUS_AWAKE
        if st in (ateapi_pb2.ACTOR_STATE_SUSPENDING,
                  ateapi_pb2.ACTOR_STATE_DELETING,
                  ateapi_pb2.ACTOR_STATE_REVERTING):
            return STATUS_SUSPENDING
        if st == ateapi_pb2.ACTOR_STATE_CRASHED:
            return STATUS_CRASHED
        if st == ateapi_pb2.ACTOR_STATE_SUSPENDED:
            # Asynchronous suspends leave the snapshot on the node first and
            # upload it in the background: "napping" until the copy in object
            # storage is COMPLETED, "suspended" (deep sleep) after.
            latest = None
            for snap in actor.status.snapshots:
                if snap.owner != ateapi_pb2.SNAPSHOT_OWNER_ACTOR:
                    continue
                if latest is None or snap.generation > latest.generation:
                    latest = snap
            if latest is not None and (
                latest.durable_snapshot.state
                != ateapi_pb2.SNAPSHOT_STORAGE_STATE_COMPLETED
            ):
                return STATUS_NAPPING
            return STATUS_SUSPENDED
        return "unknown"

    async def list_actors(self) -> dict[str, dict]:
        """{name: {"status": ..., "worker_pod": ..., "crash": ...}} for the hotel."""
        import grpc

        from ateapipb import ateapi_pb2

        stub = await self._stub()
        md = await self._md()
        out: dict[str, dict] = {}
        try:
            page_token = ""
            while True:
                resp = await stub.ListActors(
                    ateapi_pb2.ListActorsRequest(
                        atespace=ATESPACE,
                        options=ateapi_pb2.ListOptions(
                            page_size=100, page_token=page_token
                        ),
                    ),
                    metadata=md,
                )
                for actor in resp.actors:
                    st = actor.status
                    pod = (st.worker_assignment.worker_pod
                           if st.HasField("worker_assignment") else "")
                    out[actor.metadata.name] = {
                        "status": self._status_of(actor),
                        "worker_pod": pod or None,
                        "crash": st.crash.message if st.HasField("crash") else None,
                    }
                page_token = resp.next_page_token
                if not page_token:
                    break
        except grpc.aio.AioRpcError as e:
            raise self._wrap(e)
        return out

    async def list_workers(self, actors: dict[str, dict] | None = None) -> list[dict]:
        """The hotel's rooms: [{pod, node, occupant, capacity}].

        Workers do not carry their assignment; occupancy is the inverse of
        the actors' worker_assignment, so pass the list_actors() result in
        (get_state already has it) to avoid a second round-trip.
        """
        import grpc

        from ateapipb import ateapi_pb2

        if actors is None:
            actors = await self.list_actors()
        occupant = {
            a["worker_pod"]: n for n, a in actors.items() if a.get("worker_pod")
        }
        stub = await self._stub()
        md = await self._md()
        workers: list[dict] = []
        try:
            page_token = ""
            while True:
                resp = await stub.ListWorkers(
                    ateapi_pb2.ListWorkersRequest(
                        options=ateapi_pb2.ListOptions(
                            page_size=100, page_token=page_token
                        )
                    ),
                    metadata=md,
                )
                for w in resp.workers:
                    if w.worker_pool != WORKER_POOL:
                        continue
                    cap = w.status.capacity
                    workers.append({
                        "pod": w.worker_pod,
                        "node": w.node_name,
                        "occupant": occupant.get(w.worker_pod),
                        "draining": (w.status.state
                                     == ateapi_pb2.WORKER_STATE_DRAINING),
                        "capacity": {"cpu_millicores": cap.cpu_millicores,
                                     "memory_bytes": cap.memory_bytes},
                    })
                page_token = resp.next_page_token
                if not page_token:
                    break
        except grpc.aio.AioRpcError as e:
            raise self._wrap(e)
        return workers

    async def set_room_count(self, count: int) -> int:
        """Scale the hotel by patching the WorkerPool's replicas.

        The ate-controller reconciles the worker Deployment to the new size;
        new rooms take a little while to warm up. Shrinking evicts an awake
        guest's worker — the plane suspends it on the way out and its next
        message resumes it, which is itself a nice demo of the mechanics.
        """
        def _patch():
            from kubernetes import client, config

            config.load_incluster_config()
            client.CustomObjectsApi().patch_namespaced_custom_object(
                group="ate.dev", version="v1",
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
    @staticmethod
    def _ssl_context():
        """mTLS to the router: verify its servicedns serving cert, present
        this pod's PodCertificate. Built explicitly — httpx's `cert=` shortcut
        did not present the client certificate (TLS 1.3 "certificate
        required" from the router), an SSLContext does."""
        import ssl

        ctx = ssl.create_default_context(cafile=ATE_CA_FILE)
        ctx.load_cert_chain(ATE_CLIENT_CERT_FILE)
        return ctx

    def _http_client(self):
        import httpx

        now = time.monotonic()
        if self._http is None or now - self._http_made_at > _HTTP_CLIENT_REFRESH_S:
            # Rebuilt periodically: kubelet rotates the projected client
            # certificate on disk and the SSL context loads it once.
            self._http = httpx.AsyncClient(timeout=CHAT_TIMEOUT_S, verify=self._ssl_context())
            self._http_made_at = now
        return self._http

    async def _router(self, method: str, name: str, path: str, json_body=None) -> dict:
        import httpx

        # Retriable statuses, all pre-proxy (the guest never saw the message,
        # so a retry cannot duplicate a chat turn):
        #   421 — the guest's misdirect guard: the room still hosts its
        #         previous occupant (suspend/resume shuffle race);
        #   500/503 — resume failed or timed out in the router's ext_proc,
        #         typically a cold image pull on that room's node, or no room
        #         free yet; the pull keeps going / a room frees up, so a
        #         later attempt succeeds.
        # 504 means the guest MAY have processed the message after the router
        # gave up — only idempotent GETs are safe to retry there.
        # Six attempts spanning ~30s of backoff: enough to ride out a full
        # linger cycle when every room is busy — an occupant yields after
        # LINGER_SECONDS idle, freeing a room for this guest's resume.
        attempts = 6
        for attempt in range(attempts):
            try:
                resp = await self._http_client().request(
                    method, ROUTER_URL.rstrip("/") + path,
                    headers={
                        # Substrate's routing header: WHICH actor. Host stays
                        # ordinary application metadata.
                        "ate-target-actor": f"{ATESPACE}/{name}",
                        # The guest's misdirect guard compares this with the
                        # identity Substrate projected into its sandbox.
                        "X-Actor-Name": name,
                    },
                    json=json_body,
                )
            except httpx.HTTPError as e:
                raise AteError(502, f"router request failed: {e}")
            retriable = resp.status_code in (421, 500, 503) or (
                resp.status_code == 504 and method == "GET"
            ) or (
                # The worker tunnel's reverse proxy answers a bare
                # "bad gateway" when it cannot reach the guest — right after
                # a restore the sandbox's listener may not accept yet. That
                # failure is pre-delivery (the guest never saw the message),
                # so retrying cannot duplicate a chat turn. Envoy-generated
                # 502s carry different bodies and stay non-retriable.
                resp.status_code == 502 and resp.text.strip() == "bad gateway"
            )
            if resp.status_code == 200 or not retriable:
                break
            if attempt < attempts - 1:
                logger.warning("router %d for %s (attempt %d); retrying",
                               resp.status_code, name, attempt + 1)
                await asyncio.sleep(2.0 * (attempt + 1))
        if resp.status_code != 200:
            # The router's body usually carries the real reason (resume
            # failure, no free worker, …) — surface it, not just the code.
            body = " ".join(resp.text.split())[:300]
            raise AteError(
                502,
                f"router returned {resp.status_code} for {name}"
                + (f": {body}" if body else ""),
            )
        try:
            return resp.json()
        except Exception:
            raise AteError(502, "guest returned a non-JSON response")

    async def chat(self, name: str, message: str) -> dict:
        return await self._router("POST", name, "/chat", {"message": message})

    async def history(self, name: str) -> dict:
        return await self._router("GET", name, "/history")

    async def idlez(self, name: str) -> dict:
        """The guest's own idle self-report (see the sweeper). Only call for
        AWAKE actors — the router transparently resumes suspended ones."""
        return await self._router("GET", name, "/idlez")


# --------------------------------------------------------------------------
# Fake plane (offline dev + tests)
# --------------------------------------------------------------------------
class FakeAteClient:
    """In-memory hotel: N worker slots, per-guest agent state, zero deps.

    Mimics the observable semantics the concierge relies on:
      * a guest occupies a worker slot while awake, none while asleep;
      * a suspend is "napping" (snapshot on the node) until its upload lands
        after `upload_latency` seconds, then "suspended" (durable);
      * chatting with a sleeping guest transparently "resumes" it (onto any
        free slot, evicting nobody — resume waits for a free slot);
      * RAM state (history, boot_id) survives suspend/resume;
      * a crashed guest is refused until reverted (RevertActor).
    """

    def __init__(self, slots: int | None = None, resume_latency: float = 0.0,
                 upload_latency: float = 0.0):
        if slots is None:
            slots = int(os.environ.get("WORKER_REPLICAS", "3"))
        self._next_slot = slots
        self.slots = [f"fake-worker-{i}" for i in range(slots)]
        self.resume_latency = resume_latency
        self.upload_latency = upload_latency
        self.actors: dict[str, dict] = {}  # name -> {status, worker, boot_id, history, durable_at}
        self.egress_policies: dict[str, list[dict]] = {}
        self.templates: dict[str, dict] = {}

    def _free_slot(self) -> str | None:
        used = {a["worker"] for a in self.actors.values() if a["worker"]}
        for s in self.slots:
            if s not in used:
                return s
        return None

    def _status(self, actor: dict) -> str:
        if actor["status"] == STATUS_NAPPING and time.monotonic() >= actor["durable_at"]:
            actor["status"] = STATUS_SUSPENDED
        return actor["status"]

    async def ensure_template(self, name: str, body: dict) -> str:
        self.templates.setdefault(name, body)
        return name

    async def template_status(self, name: str) -> dict:
        return {"golden_ready": True, "golden_tag": f"golden-{name}", "error": None}

    async def create_actor(self, name: str, template_name: str) -> None:
        if name in self.actors:
            return
        slot = self._free_slot()
        if slot is None:
            raise AteError(409, "no free worker slot")
        self.actors[name] = {
            "status": STATUS_AWAKE,
            "worker": slot,
            "boot_id": uuid.uuid4().hex[:12],
            "history": [],
            "durable_at": 0.0,
            "crash": None,
        }

    async def ensure_egress_policy(self, name: str, rules: list[dict]) -> None:
        if name not in self.actors:
            raise AteError(404, f"actor {name} not found")
        self.egress_policies.setdefault(name, rules)

    async def suspend_actor(self, name: str, durable: bool = False) -> bool:
        actor = self.actors.get(name)
        if actor is None or actor["status"] != STATUS_AWAKE:
            return False
        actor["worker"] = None
        if durable or self.upload_latency <= 0:
            actor["status"] = STATUS_SUSPENDED
        else:
            actor["status"] = STATUS_NAPPING
            actor["durable_at"] = time.monotonic() + self.upload_latency
        return True

    def crash(self, name: str, message: str = "sandbox died") -> None:
        """Test hook: what the plane does when a restore/checkpoint fails."""
        actor = self.actors[name]
        actor["status"] = STATUS_CRASHED
        actor["worker"] = None
        actor["crash"] = message

    async def revert_actor(self, name: str) -> bool:
        actor = self.actors.get(name)
        if actor is None or actor["status"] not in (STATUS_CRASHED, STATUS_AWAKE,
                                                    STATUS_NAPPING):
            return False
        actor["status"] = STATUS_SUSPENDED
        actor["worker"] = None
        actor["crash"] = None
        return True

    async def delete_actor(self, name: str) -> None:
        self.actors.pop(name, None)
        self.egress_policies.pop(name, None)

    async def list_actors(self) -> dict[str, dict]:
        return {
            n: {"status": self._status(a), "worker_pod": a["worker"], "crash": a["crash"]}
            for n, a in self.actors.items()
        }

    async def list_workers(self, actors: dict[str, dict] | None = None) -> list[dict]:
        occupants = {
            a["worker"]: n for n, a in self.actors.items() if a["worker"]
        }
        return [
            {"pod": s, "node": "fake-node", "occupant": occupants.get(s),
             "draining": False,
             "capacity": {"cpu_millicores": 1000, "memory_bytes": 1 << 30}}
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
                    actor["status"] = STATUS_SUSPENDED
                    actor["worker"] = None
        return len(self.slots)

    async def _wake(self, name: str) -> dict:
        actor = self.actors.get(name)
        if actor is None:
            raise AteError(404, f"guest {name} is not checked in")
        if actor["status"] == STATUS_CRASHED:
            # The router refuses to resume a crashed actor (FAILED_PRECONDITION).
            raise AteError(502, f"router returned 503 for {name}: actor is crashed")
        if self._status(actor) in ASLEEP_STATES:
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
            actor["status"] = STATUS_AWAKE
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

    async def idlez(self, name: str) -> dict:
        # The fake hotel has no real guest process; report "idle" so the
        # sweeper behaves as in the single-replica tests.
        if name not in self.actors:
            raise AteError(404, f"guest {name} is not checked in")
        return {"guest": name, "in_flight": 0, "idle_s": None}


def make_client():
    mode = os.environ.get("ATE_MODE", "real").strip().lower()
    if mode == "fake":
        logger.info("ATE_MODE=fake — using the in-memory hotel")
        return FakeAteClient()
    return RealAteClient()
