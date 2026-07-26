"""Test fixtures. The whole suite runs offline: ATE_MODE=fake means no
cluster, no docker, and no grpc/kubernetes/google-genai imports."""

import os
import sys

# Repo root on sys.path so `app` and `ateapipb` import from a plain checkout.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ATE_MODE", "fake")
os.environ.setdefault("LLM_MODE", "canned")
# Keep background loops effectively inert during tests.
os.environ.setdefault("SWEEP_INTERVAL_S", "3600")
os.environ.setdefault("LINGER_SECONDS", "0")

import pytest


@pytest.fixture()
def concierge_client():
    """A TestClient over a FRESH concierge app (clean fake hotel per test)."""
    import importlib

    from fastapi.testclient import TestClient

    from app import concierge

    importlib.reload(concierge)
    with TestClient(concierge.app) as client:
        yield client


@pytest.fixture()
def agent_client():
    """A TestClient over a fresh guest agent process."""
    import importlib

    from fastapi.testclient import TestClient

    from app.agent import server

    importlib.reload(server)
    with TestClient(server.app) as client:
        yield client
