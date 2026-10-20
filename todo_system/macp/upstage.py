"""Upstage Solar — a minimal OpenAI-compatible chat client for an upstage account.

The pipeline's stages run `claude -p` directly (`orchestrator.stages`), so this
module is **not** a stage execution path; it is the measurement seam that lets a
registered upstage account actually be *called*: one `POST /chat/completions`
with a structured result (ok / model / elapsed / tokens / failure kind), which
`macp account probe` prints as one line of JSON.

Two rules shape the whole file:

* **The key never leaves the request.** It is read at call time
  (`accounts.read_api_key`), put in one `Authorization` header, and scrubbed out
  of anything returned — a server that echoes it back must not turn into a log
  line. Nothing here prints.
* **One call, no retry.** The measured account is on a tier that 429s after ~45
  consecutive calls (2026-10-19), and a `rate_limit` classification *is* the
  result a probe wants to report, not an error to paper over.

Stdlib only (`urllib`), so it runs from a bare clone like the rest of `macp`.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from . import accounts as ACC
from . import config as C

API_BASE = "https://api.upstage.ai/v1"
API_BASE_ENV = "MACP_UPSTAGE_BASE_URL"     # override for tests / a proxy
REDACTED = "***"

# error_kind values, in the order the classifier tries them.
KIND_NO_KEY = "no_key"
KIND_AUTH = "auth"                 # 401 / 403
KIND_RATE_LIMIT = "rate_limit"     # 429
KIND_HTTP = "http"                 # any other non-2xx
KIND_TIMEOUT = "timeout"
KIND_NETWORK = "network"           # DNS / refused / reset
KIND_BAD_RESPONSE = "bad_response"  # 2xx whose body is not the expected JSON


def base_url(env: dict | None = None) -> str:
    """:data:`API_BASE`, or ``MACP_UPSTAGE_BASE_URL`` when set, without a trailing
    slash so callers can append ``/chat/completions``."""
    src = os.environ if env is None else env
    return (src.get(API_BASE_ENV) or API_BASE).rstrip("/")


def _scrub(text: str, key: str | None) -> str:
    """``text`` with the API key replaced by :data:`REDACTED`. Upstream error
    bodies sometimes quote the credential they rejected; this is the last gate
    before a string becomes a return value a caller may print."""
    out = str(text)
    if key:
        out = out.replace(key, REDACTED)
    return out


def _result(model: str, started: float, **kw) -> dict:
    base: dict[str, Any] = {
        "ok": False, "model": model, "elapsed_s": round(time.monotonic() - started, 3),
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "error_kind": None, "error": None,
    }
    base.update(kw)
    return base


def chat(prompt: str, *, account=None, tier: str = "light", max_tokens: int = 32,
         timeout: float = 60, api_key: str | None = None,
         base: str | None = None) -> dict:
    """One Solar chat completion. Never raises, never prints, never retries.

    Returns ``{ok, model, elapsed_s, prompt_tokens, completion_tokens,
    total_tokens, error_kind, error}``. ``error_kind`` is None on success and
    otherwise one of the ``KIND_*`` constants. ``error`` carries a short scrubbed
    reason (status code + the first 200 characters of the server's message) —
    enough to classify a failure, never enough to leak the key.

    The model comes from ``config.solar_model_for(account, tier)``, so an
    account's ``models`` override decides it; ``api_key`` is for tests and
    defaults to ``accounts.read_api_key(account)`` at call time.
    """
    model = C.solar_model_for(account, tier)
    started = time.monotonic()
    key = api_key if api_key is not None else ACC.read_api_key(account)
    if not key:
        return _result(model, started, error_kind=KIND_NO_KEY,
                       error=f"no {ACC.UPSTAGE_KEY_ENV} in the env or the key file")
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        f"{base or base_url()}/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = getattr(e, "code", 0)
        kind = (KIND_AUTH if status in (401, 403) else
                KIND_RATE_LIMIT if status == 429 else KIND_HTTP)
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = str(e)
        return _result(model, started, error_kind=kind,
                       error=f"HTTP {status}: {_scrub(detail, key)[:200]}")
    except socket.timeout:
        return _result(model, started, error_kind=KIND_TIMEOUT,
                       error=f"no response within {timeout}s")
    except urllib.error.URLError as e:
        # URLError wraps a timeout too when it comes from the socket layer
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            return _result(model, started, error_kind=KIND_TIMEOUT,
                           error=f"no response within {timeout}s")
        return _result(model, started, error_kind=KIND_NETWORK,
                       error=_scrub(reason, key)[:200])
    except OSError as e:
        return _result(model, started, error_kind=KIND_NETWORK,
                       error=_scrub(e, key)[:200])
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        text = data["choices"][0]["message"]["content"]
    except Exception as e:
        return _result(model, started, error_kind=KIND_BAD_RESPONSE,
                       error=_scrub(f"{type(e).__name__}: {e}", key)[:200])
    usage = data.get("usage") or {}

    def _tok(name):
        v = usage.get(name)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return _result(str(data.get("model") or model), started, ok=True,
                   prompt_tokens=_tok("prompt_tokens"),
                   completion_tokens=_tok("completion_tokens"),
                   total_tokens=_tok("total_tokens"),
                   reply_chars=len(_scrub(text, key)))
