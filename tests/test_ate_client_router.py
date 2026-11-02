"""RealAteClient._router retry semantics, offline.

httpx is stubbed via sys.modules and the client object is built without
__init__ (which would import grpc), so this runs with zero cluster deps —
same spirit as the rest of the suite.

The contract under test (see app/ate_client.py):
  * every request carries Host <name>.hotel... AND X-Actor-Name <name>;
  * 421 (misdirect guard), 500 and 503 (pre-proxy resume failures) are
    retried — the guest never saw the message, so retries cannot duplicate
    a chat turn;
  * 504 is retried only for idempotent GETs — the guest MAY have processed
    a POST after the router gave up;
  * retries are bounded (6 attempts), then the last status surfaces as a
    502 AteError.
"""

import asyncio
import sys
import types

import pytest

from app import ate_client
from app.ate_client import AteError, RealAteClient


class FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self.text = text
        self._body = body if body is not None else {"ok": True}

    def json(self):
        return self._body


MTLS = object()   # stands in for the mTLS ssl.SSLContext


class FakeHttpx(types.ModuleType):
    """Stub httpx module: scripted responses + a call log."""

    def __init__(self, responses):
        super().__init__("httpx")
        self.responses = list(responses)
        self.calls = []
        self.clients_built = 0

        class HTTPError(Exception):
            pass

        self.HTTPError = HTTPError

        stub = self

        class AsyncClient:
            def __init__(self, timeout=None, verify=None):
                stub.clients_built += 1
                # The router demands mTLS: the client passes an SSL context
                # that presents its projected PodCertificate and verifies the
                # servicedns trust bundle.
                stub.tls = verify

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, headers=None, json=None):
                stub.calls.append(
                    {"method": method, "url": url, "headers": dict(headers or {})}
                )
                return stub.responses.pop(0)

        self.AsyncClient = AsyncClient


@pytest.fixture()
def router_env(monkeypatch):
    """A __init__-less RealAteClient + a way to script httpx responses."""

    def setup(responses):
        fake = FakeHttpx(responses)
        fake.sleeps = []
        monkeypatch.setitem(sys.modules, "httpx", fake)

        async def no_sleep(seconds):
            fake.sleeps.append(seconds)

        # Neutralize backoff so exhausting 6 attempts doesn't take ~30 s.
        monkeypatch.setattr(
            ate_client, "asyncio",
            types.SimpleNamespace(sleep=no_sleep),
        )
        client = RealAteClient.__new__(RealAteClient)
        client._http = None  # __init__ (skipped: imports grpc) would set this
        client._http_made_at = 0.0
        # No projected certificates on a dev box: stand in for the context.
        monkeypatch.setattr(RealAteClient, "_ssl_context", staticmethod(lambda: MTLS))
        return client, fake

    return setup


def run(coro):
    return asyncio.run(coro)


def test_success_carries_routing_and_identity_headers(router_env):
    client, fake = router_env([FakeResponse(200, {"guest": "amara"})])
    out = run(client._router("POST", "amara", "/chat", {"message": "hi"}))
    assert out == {"guest": "amara"}
    assert len(fake.calls) == 1
    headers = fake.calls[0]["headers"]
    # Substrate's routing header selects the actor; Host is left to httpx.
    assert headers["ate-target-actor"] == "hotel/amara"
    assert "Host" not in headers
    # The guest's misdirect guard compares this with its projected identity.
    assert headers["X-Actor-Name"] == "amara"
    assert fake.calls[0]["url"].startswith("https://atenet-router.ate-system.svc/")
    assert fake.tls is MTLS


def test_421_misdirect_is_retried_until_routing_settles(router_env):
    client, fake = router_env(
        [FakeResponse(421), FakeResponse(421), FakeResponse(200, {"guest": "kai"})]
    )
    out = run(client._router("POST", "kai", "/chat", {"message": "hi"}))
    assert out == {"guest": "kai"}
    assert len(fake.calls) == 3


def test_500_resume_failure_is_retried(router_env):
    client, fake = router_env([FakeResponse(500), FakeResponse(200)])
    run(client._router("POST", "lumi", "/chat", {"message": "hi"}))
    assert len(fake.calls) == 2


def test_retries_are_bounded_then_502(router_env):
    client, fake = router_env([FakeResponse(500)] * 6)
    with pytest.raises(AteError) as e:
        run(client._router("POST", "lumi", "/chat", {"message": "hi"}))
    assert e.value.status == 502
    assert "500" in e.value.detail
    assert len(fake.calls) == 6
    # Backoff happens BETWEEN attempts only — no wasted sleep after the final
    # failure (that used to add ~12 s to every hard error).
    assert len(fake.sleeps) == 5
    # One shared AsyncClient across all attempts (connection reuse).
    assert fake.clients_built == 1


def test_router_error_body_reaches_the_detail(router_env):
    """The router's body usually carries the real reason (resume failure, no
    free worker, …) — it must surface in the AteError, not just the code."""
    client, fake = router_env(
        [FakeResponse(503, text="no free worker slot to resume onto")] * 6
    )
    with pytest.raises(AteError) as e:
        run(client._router("POST", "amara", "/chat", {"message": "hi"}))
    assert "503" in e.value.detail
    assert "no free worker slot" in e.value.detail


def test_non_json_success_body_is_502(router_env):
    class HtmlResponse(FakeResponse):
        def json(self):
            raise ValueError("not json")

    client, fake = router_env([HtmlResponse(200, text="<html>oops</html>")])
    with pytest.raises(AteError) as e:
        run(client._router("GET", "amara", "/history"))
    assert "non-JSON" in e.value.detail


def test_504_is_retried_for_get_but_not_post(router_env):
    # GET /history is idempotent — safe to retry a router timeout.
    client, fake = router_env([FakeResponse(504), FakeResponse(200)])
    run(client._router("GET", "wren", "/history"))
    assert len(fake.calls) == 2

    # POST /chat may already have been processed — must NOT be retried.
    client, fake = router_env([FakeResponse(504), FakeResponse(200)])
    with pytest.raises(AteError):
        run(client._router("POST", "wren", "/chat", {"message": "hi"}))
    assert len(fake.calls) == 1


def test_non_retriable_status_fails_immediately(router_env):
    client, fake = router_env([FakeResponse(404)])
    with pytest.raises(AteError):
        run(client._router("POST", "ezra", "/chat", {"message": "hi"}))
    assert len(fake.calls) == 1


def test_atunnel_bad_gateway_502_is_retried(router_env):
    """The worker tunnel answers a bare 'bad gateway' when the just-restored
    guest isn't accepting yet — pre-delivery, so safe to retry."""
    client, fake = router_env(
        [FakeResponse(502, text="bad gateway\n"), FakeResponse(200, {"guest": "ezra"})]
    )
    out = run(client._router("POST", "ezra", "/chat", {"message": "hi"}))
    assert out == {"guest": "ezra"}
    assert len(fake.calls) == 2


def test_other_502_bodies_are_not_retried(router_env):
    """An Envoy-generated 502 (connection termination mid-stream) may mean
    the guest already got the message — retrying could duplicate a turn."""
    client, fake = router_env(
        [FakeResponse(502, text="upstream connect error or disconnect/reset before headers")]
    )
    with pytest.raises(AteError):
        run(client._router("POST", "ezra", "/chat", {"message": "hi"}))
    assert len(fake.calls) == 1
