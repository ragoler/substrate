"""LLM access for a hotel guest.

Modes (env LLM_MODE):
  direct  — call Gemini on Vertex AI with Application Default Credentials
            (Workload Identity). The whole point of ADC here: no key is ever
            frozen into a Substrate snapshot; a fresh token is fetched from
            the metadata server after every resume.
  proxy   — POST to the concierge's /llm endpoint (fallback when the metadata
            server is unreachable from inside the gVisor sandbox).
  canned  — deterministic scripted persona replies (offline / tests / no GCP).

`google-genai` and `httpx` are imported lazily so the offline test suite needs
neither installed.
"""

import logging
import os
import threading

from app import personas

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_client = None
_client_lock = threading.Lock()
_client_failed = False


def _mode() -> str:
    mode = os.environ.get("LLM_MODE", "").strip().lower()
    if mode in ("direct", "proxy", "canned"):
        return mode
    # Default: direct when Vertex is configured, canned otherwise.
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
    return "direct" if use_vertex else "canned"


def _vertex_client():
    """Lazily build one genai client per process (thread-safe)."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    with _client_lock:
        if _client is not None or _client_failed:
            return _client
        try:
            from google import genai  # lazy: not needed offline

            _client = genai.Client(
                vertexai=True,
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )
            logger.info("Gemini client initialized (Vertex AI, ADC)")
        except Exception:
            logger.warning("Vertex AI client init failed; using canned replies",
                           exc_info=True)
            _client_failed = True
    return _client


def _transcript(persona: personas.Persona, history: list[dict], message: str) -> str:
    lines = [persona.system_prompt, "", "Conversation so far:"]
    for m in history:
        who = "Guest" if m.get("role") == "assistant" else "Visitor"
        lines.append(f"{who}: {m.get('text', '')}")
    lines.append(f"Visitor: {message}")
    lines.append("Guest:")
    return "\n".join(lines)


def _direct_reply(name: str, message: str, history: list[dict]) -> str | None:
    client = _vertex_client()
    if client is None:
        return None
    persona = personas.get(name)
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=_transcript(persona, history, message),
        )
        text = (resp.text or "").strip()
        return text or None
    except Exception:
        logger.warning("Gemini generate_content failed", exc_info=True)
        return None


def _proxy_reply(name: str, message: str, history: list[dict]) -> str | None:
    concierge = os.environ.get("CONCIERGE_URL", "").rstrip("/")
    if not concierge:
        return None
    try:
        import httpx  # lazy

        resp = httpx.post(
            f"{concierge}/llm",
            json={"guest": name, "message": message, "history": history},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json().get("reply") or None
    except Exception:
        logger.warning("LLM proxy call to concierge failed", exc_info=True)
        return None


def reply(name: str, message: str, history: list[dict]) -> str:
    """In-persona reply; always returns something (canned as last resort)."""
    mode = _mode()
    text = None
    if mode == "direct":
        text = _direct_reply(name, message, history)
    elif mode == "proxy":
        text = _proxy_reply(name, message, history)
    if text is None:
        text = personas.canned_reply(name, message, history)
    return text
