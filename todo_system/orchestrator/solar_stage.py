"""Solar single-stage execution — run a quick/delegate stage with Upstage Solar.

The pipeline's stages normally run ``claude -p`` (``orchestrator.stages`` +
``run_stage_subprocess``). This module is the alternative execution path for a
task whose frontmatter says ``agent: upstage``: an in-engine, OpenAI-compatible
**tool-calling loop** that reads/writes files in the task's working dir and
finishes, driven by Upstage Solar's ``/chat/completions`` endpoint. The runner
(``StagedExecutor._execute``) routes to :func:`run` only when an upstage account
is resolved AND the stage is quick/delegate (``stages.route_to_solar``); for
every other task the claude argv/env is byte-identical, because that branch is
simply never taken.

Two invariants shape the whole file (inherited from ``macp.upstage``):

* **The key never leaves the request.** It is read at call time
  (``accounts.read_api_key``) and placed in one ``Authorization`` header. It is
  scrubbed (``upstage._scrub``) out of anything returned, of the stage stdout,
  and of the artifact — a server that echoes it back must not become a log line.
  Nothing here prints.
* **The loop runs IN-PROCESS, bounded, concurrency 1.** No subprocess env ever
  carries the key, and the loop cannot run away: a per-call socket timeout, a
  max-iteration cap, and a monotonic deadline each bound it. Calls are strictly
  sequential (concurrency 1) — Solar's Tier-0 429 wall sits around ~45
  *consecutive* calls, so :func:`_with_retry` backs off rather than fanning out.

Stdlib only (``urllib``/``json``/``time``/``os``/``socket``), like
``upstage.py``, so it survives ``check_engine_isolation.sh`` from a bare clone.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from ..macp import upstage as UP


def _scrub_obj(obj: Any, key: str | None) -> Any:
    """Deep-scrub ``key`` out of a JSON-serializable object — a server-returned
    assistant message whose ``content`` or whose nested ``tool_calls[].arguments``
    might echo the credential. Round-trips through json so nested argument
    strings are covered too; returns ``obj`` unchanged when there is no key or
    the object is not serializable."""
    if not key:
        return obj
    try:
        return json.loads(UP._scrub(json.dumps(obj, ensure_ascii=False), key))
    except (TypeError, ValueError):
        return obj


def _fail(error_kind: str, status: int, error: str) -> dict:
    """A failure result in the shape :func:`_chat_once` returns on success."""
    return {"ok": False, "status": status, "message": None, "tool_calls": [],
            "usage": {}, "error_kind": error_kind, "error": error}


def _chat_once(messages: list[dict], tools: list[dict] | None = None, *,
               model: str, api_key: str, base: str | None = None,
               timeout: float = 60, max_tokens: int = 1024) -> dict:
    """One OpenAI-compatible ``/chat/completions`` POST. Never raises, never
    prints, never retries (``_with_retry`` owns the 429 backoff).

    Sends ``{model, messages, tools?, tool_choice?, max_tokens}`` with the key in
    the ``Authorization`` header only. Returns
    ``{ok, status, message, tool_calls, usage, error_kind, error}``, all scrubbed
    of the key: ``message`` is the assistant message dict on success (``content``
    plus any ``tool_calls``), None on failure; ``error_kind`` is None on success
    and otherwise one of ``upstage.KIND_*``; ``error`` is a short scrubbed reason.
    """
    payload: dict[str, Any] = {"model": model, "messages": messages,
                               "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base or UP.base_url()}/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = getattr(e, "code", 0)
        kind = (UP.KIND_AUTH if status in (401, 403) else
                UP.KIND_RATE_LIMIT if status == 429 else UP.KIND_HTTP)
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = str(e)
        return _fail(kind, status, f"HTTP {status}: {UP._scrub(detail, api_key)[:200]}")
    except socket.timeout:
        return _fail(UP.KIND_TIMEOUT, 0, f"no response within {timeout}s")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            return _fail(UP.KIND_TIMEOUT, 0, f"no response within {timeout}s")
        return _fail(UP.KIND_NETWORK, 0, UP._scrub(reason, api_key)[:200])
    except OSError as e:
        return _fail(UP.KIND_NETWORK, 0, UP._scrub(e, api_key)[:200])
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        message = data["choices"][0]["message"]
    except Exception as e:
        return _fail(UP.KIND_BAD_RESPONSE, 0,
                     UP._scrub(f"{type(e).__name__}: {e}", api_key)[:200])
    message = _scrub_obj(message, api_key)
    tool_calls = message.get("tool_calls") or [] if isinstance(message, dict) else []
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {"ok": True, "status": 200, "message": message,
            "tool_calls": tool_calls, "usage": _scrub_obj(usage, api_key),
            "error_kind": None, "error": None}


def _with_retry(fn: Callable[[], dict], *, retries: int = 5,
                base_delay: float = 0.5, max_delay: float = 30.0,
                sleep: Callable[[float], None] = time.sleep) -> dict:
    """Call ``fn()``; on a 429 (``error_kind == rate_limit``) sleep an
    exponential backoff (``base_delay * 2**attempt``, capped at ``max_delay``)
    and retry up to ``retries`` times, else return the result unchanged. ``sleep``
    is injectable so tests assert the retry count without a real wait. Any other
    outcome — success or a non-429 failure — returns on the first call: a 401 is
    not transient and a persistent 429 is a result worth reporting, not an error
    to paper over."""
    result = fn()
    attempt = 0
    while result.get("error_kind") == UP.KIND_RATE_LIMIT and attempt < retries:
        sleep(min(base_delay * (2 ** attempt), max_delay))
        attempt += 1
        result = fn()
    return result
