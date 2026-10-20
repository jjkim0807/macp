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
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..macp import accounts as ACC
from ..macp import config as C
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


# ---- the tool-calling loop ---------------------------------------------------

# The three tools the loop exposes. Deliberately tiny: read, write (full
# contents), and finish — no shell, no git, no network — so the loop can only
# touch files under the working dir and cannot run away. `write_file` sends the
# WHOLE file each time (no patch semantics), which is what a small text edit
# needs and what a model gets right without diff tooling.
TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file in the working directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Path relative to the working directory."}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 text file in the working "
                       "directory with the full new contents.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Path relative to the working directory."},
            "content": {"type": "string",
                        "description": "The complete new file contents."}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Call when the task is complete.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string",
                        "description": "One line: what changed."}},
            "required": ["summary"]}}},
]

_SYSTEM_PROMPT = (
    "You are a worker completing one small task by editing files. Use the tools: "
    "read_file to inspect a file, write_file to create or overwrite one (always "
    "send the FULL new contents), and finish when done. Every path is relative to "
    "the working directory. Do the minimum needed, then call finish with a "
    "one-line summary. Do not ask questions; act with the tools."
)

_READ_CAP = 20_000      # chars returned to the model from one read_file


def _resolve_in(working_dir: str, path: str) -> str:
    """Resolve ``path`` under ``working_dir`` (a sandbox guard) or raise
    ``ValueError`` when the real target escapes it — ``../`` or an absolute path
    cannot reach outside the one directory the stage was handed."""
    root = os.path.realpath(working_dir)
    target = os.path.realpath(os.path.join(root, path))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"path escapes the working directory: {path!r}")
    return target


def _parse_args(raw: Any) -> dict:
    """An OpenAI tool-call ``arguments`` is a JSON string; tolerate a dict (some
    servers send one) or junk (→ ``{}`` so a malformed call is a reported tool
    error, not a crash)."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def _accumulate_usage(total: dict, usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = usage.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            total[k] += v


def _exec_tool(name: str, args: dict, working_dir: str) -> tuple[str, bool, str]:
    """Run one tool call. Returns ``(result_text, is_finish, finish_summary)``.
    ``result_text`` is fed back to the model as the ``tool`` message; it is never
    allowed to raise (a bad path / IO error is reported as text so the model can
    recover)."""
    if name == "finish":
        return "ok", True, str(args.get("summary") or "")
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return "error: missing 'path'", False, ""
    try:
        target = _resolve_in(working_dir, path)
    except ValueError as e:
        return f"error: {e}", False, ""
    if name == "read_file":
        try:
            with open(target, encoding="utf-8") as f:
                return f.read(_READ_CAP), False, ""
        except OSError as e:
            return f"error: {e}", False, ""
    if name == "write_file":
        content = args.get("content")
        if not isinstance(content, str):
            return "error: missing 'content'", False, ""
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return f"wrote {len(content)} chars to {path}", False, ""
        except OSError as e:
            return f"error: {e}", False, ""
    return f"error: unknown tool {name!r}", False, ""


def run(stage: str, *, task_prompt: str, working_dir: str,
        account: Any = None, tier: str = "light", artifact_path: str | Path,
        env: dict | None = None, api_key: str | None = None,
        base: str | None = None, deadline_s: float = 1800, max_iters: int = 24,
        timeout: float = 60, sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None) -> subprocess.CompletedProcess:
    """Run one quick/delegate ``stage`` with Solar's tool-calling loop.

    Reads the key with :func:`accounts.read_api_key` at call time (``api_key``
    overrides, for tests), resolves the model with :func:`config.solar_model_for`,
    then loops read/write/finish tool calls against ``working_dir`` until the
    model calls ``finish``, replies without a tool call, or a bound trips
    (``max_iters`` / the monotonic ``deadline_s`` / a failed call). Writes a short
    markdown artifact to ``artifact_path`` (the stage's ``delegate.md`` /
    ``quick.md``) and returns a ``CompletedProcess`` shaped exactly like
    ``run_stage_subprocess``'s so the runner's shared tail reads ``returncode`` /
    ``stdout`` unchanged. ``returncode`` is 0 only when ``finish`` was called.

    The key is used only in the request header. The returned ``stdout`` and the
    artifact are scrubbed and carry ``error_kind`` *tokens* (``rate_limit`` etc.),
    never a raw server body — so nothing the runner scans for a claude capacity
    marker can ever match (the shared tail stays inert for Solar).
    """
    def _say(line: str) -> None:
        if progress:
            progress(line)

    started = time.monotonic()
    deadline = started + max(deadline_s, 1)
    model = C.solar_model_for(account, tier)
    key = api_key if api_key is not None else ACC.read_api_key(account, env)
    argv = ["solar", stage, model]
    if not key:
        # No key — the caller records blocked_on; write no artifact. Routing
        # engages only when an upstage account is configured, so this is a
        # misconfiguration (account but no key), surfaced not papered over.
        _say(f"solar {stage}: no {ACC.UPSTAGE_KEY_ENV} — cannot run")
        return subprocess.CompletedProcess(
            argv, 1, f"no {ACC.UPSTAGE_KEY_ENV} in the env or the key file", "")

    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": task_prompt}]
    transcript = [f"solar {stage} run: model={model} working_dir={working_dir}"]
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    tool_counts: dict[str, int] = {}
    files_written: set[str] = set()
    finished = False
    finish_summary = ""
    last_error: str | None = None
    iters = 0
    while iters < max_iters and time.monotonic() < deadline:
        iters += 1
        res = _with_retry(
            lambda: _chat_once(messages, TOOLS, model=model, api_key=key,
                               base=base, timeout=timeout),
            sleep=sleep)
        _accumulate_usage(usage_total, res.get("usage"))
        if not res["ok"]:
            last_error = res["error_kind"]
            transcript.append(
                f"iter {iters}: call failed error_kind={res['error_kind']} "
                f"status={res['status']}")
            break
        message = res["message"] or {}
        tool_calls = res["tool_calls"]
        messages.append(message)            # let the model see its own turn
        if not tool_calls:
            transcript.append(f"iter {iters}: assistant reply, no tool_calls (stop)")
            break
        outcomes: list[str] = []
        stop = False
        for tc in tool_calls:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = str(fn.get("name") or "")
            tool_counts[name] = tool_counts.get(name, 0) + 1
            args = _parse_args(fn.get("arguments"))
            result_text, is_finish, summary = _exec_tool(name, args, working_dir)
            if name == "write_file" and result_text.startswith("wrote"):
                files_written.add(str(args.get("path")))
            messages.append({"role": "tool",
                             "tool_call_id": str(tc.get("id") or name),
                             "content": result_text})
            # Compact per-call outcome for the transcript (→ stdout). A read's
            # result is the file body, so log only its size, never the content;
            # a write/finish/error line is short and key-free by construction.
            short = (f"read {len(result_text)} chars"
                     if name == "read_file" and not result_text.startswith("error:")
                     else result_text[:120])
            outcomes.append(f"{name or '?'}->{short}")
            if is_finish:
                finished, finish_summary, stop = True, summary, True
        transcript.append(f"iter {iters}: " + "; ".join(outcomes))
        _say(f"solar {stage}: iter {iters} {[o.split('->')[0] for o in outcomes]}")
        if stop:
            break

    elapsed = round(time.monotonic() - started, 3)
    if finished:
        error_kind = None
    elif last_error:
        error_kind = last_error
    elif iters >= max_iters:
        error_kind = "max_iters"
    elif time.monotonic() >= deadline:
        error_kind = "deadline"
    else:
        error_kind = "no_finish"

    tool_summary = ", ".join(f"{n}={c}" for n, c in sorted(tool_counts.items())) or "none"
    files = sorted(files_written)
    metrics = [
        f"status: {'ok' if finished else 'incomplete'}",
        f"error_kind: {error_kind or 'none'}",
        f"elapsed_s: {elapsed}",
        f"iterations: {iters}",
        f"tool_calls: {tool_summary}",
        f"files_written: {', '.join(files) or 'none'}",
        f"tokens: prompt={usage_total['prompt_tokens']} "
        f"completion={usage_total['completion_tokens']} "
        f"total={usage_total['total_tokens']}",
    ]
    if finish_summary:
        metrics.append(f"finish_summary: {finish_summary}")

    art_lines = [f"# {stage} (Solar) — {'ok' if finished else 'incomplete'}", "",
                 f"- model: `{model}`", *[f"- {m}" for m in metrics], "",
                 "> Processed in-engine by the Solar tool-calling loop "
                 "(todo_system.orchestrator.solar_stage). The API key appears "
                 "nowhere in this file."]
    Path(artifact_path).write_text(UP._scrub("\n".join(art_lines) + "\n", key),
                                   encoding="utf-8")

    stdout = UP._scrub("\n".join(transcript + ["", "--- metrics ---"] + metrics), key)
    return subprocess.CompletedProcess(argv, 0 if finished else 1, stdout, "")
