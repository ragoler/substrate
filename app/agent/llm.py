"""LLM access for a hotel guest.

Modes (env LLM_MODE):
  gemini  — call the Gemini API. The guest holds NO credential: it sends the
            `x-goog-api-key` header with a placeholder, and Substrate's egress
            gateway — which terminates the guest's TLS for this one hostname
            and is the only component allowed to read the Secret — replaces
            it with the real key on the way out. Nothing secret is ever in
            the sandbox, so nothing secret is ever in a snapshot.
  proxy   — POST to the concierge's /llm endpoint (the concierge holds the
            key as an ordinary env var; a fallback when the gateway path is
            not configured).
  canned  — deterministic scripted persona replies (offline / tests / no key).

`google-genai` and `httpx` are imported lazily so the offline test suite needs
neither installed.
"""

import logging
import os
import threading
import time

from app import personas

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

_client = None
_client_lock = threading.Lock()
# After a failed init, don't retry before this monotonic instant. A transient
# failure must not permanently downgrade the guest to canned replies — the
# process lives across many suspend/resume cycles.
_client_retry_at = 0.0
_CLIENT_RETRY_S = float(os.environ.get("LLM_INIT_RETRY_S", "60"))


def _mode() -> str:
    mode = os.environ.get("LLM_MODE", "").strip().lower()
    if mode in ("gemini", "proxy", "canned"):
        return mode
    return "gemini" if os.environ.get("GEMINI_API_KEY") else "canned"


def _gemini_client():
    """Lazily build one genai client per process (thread-safe).

    Failed inits are retried after _CLIENT_RETRY_S rather than latched off.
    """
    global _client, _client_retry_at
    if _client is not None:
        return _client
    if time.monotonic() < _client_retry_at:
        return None
    with _client_lock:
        if _client is not None or time.monotonic() < _client_retry_at:
            return _client
        try:
            from google import genai  # lazy: not needed offline

            # The key is whatever the environment says — inside a Substrate
            # sandbox that is a placeholder the egress gateway replaces.
            _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", "unset"))
            logger.info("Gemini client initialized")
        except Exception:
            logger.warning(
                "Gemini client init failed; canned replies for the next "
                "%.0fs, then retry", _CLIENT_RETRY_S, exc_info=True,
            )
            _client_retry_at = time.monotonic() + _CLIENT_RETRY_S
    return _client


def _transcript(persona: personas.Persona, history: list[dict], message: str) -> str:
    lines = [persona.system_prompt, "", "Conversation so far:"]
    for m in history:
        who = "Guest" if m.get("role") == "assistant" else "Visitor"
        lines.append(f"{who}: {m.get('text', '')}")
    lines.append(f"Visitor: {message}")
    lines.append("Guest:")
    return "\n".join(lines)


# A brand-new guest's first call can race its EgressPolicy: Substrate's DNS
# relay picks a new policy up within ~5 s, and until then the Gemini host
# resolves to NXDOMAIN. One short retry covers that (and other blips).
_GEMINI_RETRY_S = float(os.environ.get("GEMINI_RETRY_S", "3"))


def _gemini_reply(name: str, message: str, history: list[dict]) -> str | None:
    client = _gemini_client()
    if client is None:
        return None
    persona = personas.get(name)
    for attempt in range(2):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=_transcript(persona, history, message),
            )
            text = (resp.text or "").strip()
            return text or None
        except Exception:
            logger.warning("Gemini generate_content failed (attempt %d)", attempt + 1,
                           exc_info=True)
            if attempt == 0:
                time.sleep(_GEMINI_RETRY_S)
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
    if mode == "gemini":
        text = _gemini_reply(name, message, history)
    elif mode == "proxy":
        text = _proxy_reply(name, message, history)
    if text is None:
        text = personas.canned_reply(name, message, history)
    return text
