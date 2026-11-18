"""Batch verify — light tasks pass a QUICK verify and share one full gate.

Every auto-merged task used to run its repo's whole gate alone, so five small
changes to one repo paid for five full builds. With a batch gate configured for
a repo, an eligible run verifies only what it changed (QUICK mode), opens a
ready PR and waits in the ledger as ``batch_ready``; the repo's full gate then
runs ONCE on an integration branch carrying several such tasks, and the PRs are
merged in order. A task that cannot be batched, or whose batch fails, goes back
to the ordinary individual full verify.

This module holds the parts with no side effects on a repo: the wire strings,
the per-repo config reader, the eligibility rule, and the two ledger-row
transitions. The integration branch, the gate run and the ordered merge are the
integration-gate leaf's and are not here yet; until they are, the
orchestrator's safety-net sweep sends a waiting row back to a full verify.

Opt-in per repo, off by default. The config is vault data::

    <vault_root>/.macp/batch_gate.json
    {"enabled": true,
     "repos": {"<repo_name>": {"full_gate": [["./gradlew", "test"]],
                               "quick_hint": "optional"}}}

``full_gate`` is a list of argv lists (not shell strings), all of which must
exit 0. The key is ``repos.repo_name(url)``. No file, ``enabled`` not true, or
no entry for the repo: that repo behaves exactly as before.

Stdlib-only at import. Sibling modules are imported inside the functions that
need them (``tasks`` imports this module, and ``remote_gate`` pulls in
``stages``), and the vault root is always passed in, never computed here.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

# Where the config lives, relative to the vault root.
CONFIG_REL = Path(".macp") / "batch_gate.json"

# --- wire strings ------------------------------------------------------------
# All of these land in git-tracked assignment JSON or in mailbox envelopes, so
# they are defined once, here, and never re-spelled by a caller. The ledger
# state and the "returned" history type live in assignment.py (BATCH_READY,
# BATCH_RETURNED) because assignment.is_deferred reads them.

# Dispatch payload keys.
KEY_VERIFY_BATCH = "verify_batch"          # frontmatter opt-in / opt-out
KEY_VERIFY_FULL = "verify_full"            # sent back once: never batch again
KEY_RETURN_REASON = "batch_return_reason"  # why it was sent back (RETURN_*)
KEY_BATCH = "batch"                        # the batch record: ledger row + RESULT
# What dispatch() copies from the previous ledger row onto the next payload.
CARRY_KEYS = (KEY_VERIFY_FULL, KEY_RETURN_REASON)

# RESULT payload keys the runner sets.
RESULT_READY = "batch_ready"
RESULT_RETURN = "batch_return"

# Ledger history types.
HISTORY_READY = "batch-ready"
HISTORY_MERGED = "batch-merged"            # wired by the integration-gate leaf

# Why a task left the batch for an individual full verify.
RETURN_NOT_ENABLED = "not-enabled"         # the repo's gate left the config
RETURN_EXPIRED = "expired"                 # no gate ran within EXPIRE_S
RETURN_NO_DEVICE = "no-device-evidence"    # needs a device run, none this run
RETURN_NO_PR = "no-pr"                     # the report opened no PR
RETURN_NO_HEAD_SHA = "no-head-sha"         # the judged sha could not be read
RETURN_QUICK_NOT_PASS = "quick-not-pass"   # the quick verify did not say PASS
# wired by the integration-gate leaf
RETURN_GATE_FAILED = "gate-failed"
RETURN_GATE_TIMEOUT = "gate-timeout"
RETURN_CONFLICT = "conflict"
RETURN_BRANCH_MOVED = "branch-moved"
RETURN_BRANCH_MISSING = "branch-missing"
RETURN_NO_CHECKOUT = "no-checkout"
RETURN_RESTARTS = "restarts"
RETURN_MERGE_FAILED = "merge-failed"

# --- numbers -----------------------------------------------------------------
EXPIRE_S = 86400            # a batch_ready row older than this goes back
# wired by the integration-gate leaf
MIN_BATCH = 2               # start a gate once this many tasks wait…
MAX_WAIT_S = 1800           # …or once the oldest has waited this long
MAX_BATCH = 5               # tasks per integration branch
MAX_RESTARTS = 3            # rebuilds after the default branch moved
GATE_TIMEOUT_S = 7200       # one full-gate run

_BATCHABLE_TIERS = ("light", "standard")


def _as_bool(v) -> bool | None:
    """True/False for a bool or a true/yes/1 · false/no/0 string, else None.
    The same rule as tasks._fm_bool, kept apart because tasks imports this
    module."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def load_config(vault_root) -> dict:
    """The vault's batch-gate config, or ``{}`` when the feature is off.

    Off means any of: no file, an unreadable file, broken JSON, a non-object, or
    ``enabled`` not true. Never raises — a config that cannot be read must leave
    every run on the ordinary path."""
    try:
        data = json.loads((Path(vault_root) / CONFIG_REL).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config = feature off
        return {}
    if not isinstance(data, dict) or _as_bool(data.get("enabled")) is not True:
        return {}
    return data


def _normalise_gate(raw) -> list[list[str]] | None:
    """``full_gate`` as a list of argv lists, or None when it is unusable.

    A single argv (``["./gradlew", "test"]``) is wrapped once. Empty lists and
    empty or non-string tokens make the whole gate unusable: a gate that cannot
    be run exactly as written is not a gate."""
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    commands = [raw] if all(isinstance(tok, str) for tok in raw) else raw
    out: list[list[str]] = []
    for argv in commands:
        if not isinstance(argv, (list, tuple)) or not argv:
            return None
        if not all(isinstance(tok, str) and tok.strip() for tok in argv):
            return None
        out.append(list(argv))
    return out


def repo_gate(cfg, repo_url) -> dict | None:
    """This repo's config entry with ``full_gate`` normalised, or None.

    None for an empty config, a task with no repo, a repo with no entry, or an
    entry whose ``full_gate`` is missing or malformed. Never raises."""
    try:
        if not cfg or not repo_url:
            return None
        from . import repos
        entries = cfg.get("repos")
        if not isinstance(entries, dict):
            return None
        entry = entries.get(repos.repo_name(str(repo_url)))
        if not isinstance(entry, dict):
            return None
        full_gate = _normalise_gate(entry.get("full_gate"))
        if not full_gate:
            return None
        return {**entry, "full_gate": full_gate}
    except Exception:  # noqa: BLE001 - a bad entry = no gate for this repo
        return None


def remote_marked(gate, checkout) -> bool:
    """Does the repo mark any full-gate command to run on the remote node?

    Such a repo is left out of batch verify for now: a command that must run
    remotely is handled by each task's own verify, and the integration gate runs
    locally. Reads ``<checkout>/.macp/gates.json`` through remote_gate.decide,
    which matches the command string exactly. Any error answers True — when the
    marking cannot be read, the conservative answer is to keep the repo out."""
    try:
        from . import remote_gate
        for argv in (gate or {}).get("full_gate") or []:
            if remote_gate.decide(shlex.join(argv), checkout) == "remote":
                return True
        return False
    except Exception:  # noqa: BLE001 - unreadable marking = exclude
        return True


def needs_device(payload) -> bool:
    """Does the task ask for an on-device run (frontmatter ``emulator: true``)?

    tasks.load_dispatchable carries it as the ``emulator`` entry of
    ``heavy_signals``; a payload built another way may carry ``emulator``
    itself."""
    p = payload if isinstance(payload, dict) else {}
    signals = p.get("heavy_signals")
    if isinstance(signals, (list, tuple)) and "emulator" in signals:
        return True
    return _as_bool(p.get("emulator")) is True


def eligible(payload, *, tier, gate, five_stage, auto_merge,
             remote_marked=False) -> tuple[bool, str]:
    """(batch this run?, the reason) — the first matching rule wins.

    ``tier`` is the run's model tier (model_select.classify_tier), ``gate`` the
    repo's config entry (repo_gate), ``five_stage`` whether the run uses the full
    research→report pipeline, ``auto_merge`` the runner's own merge decision and
    ``remote_marked`` the answer of :func:`remote_marked`."""
    p = payload if isinstance(payload, dict) else {}
    if gate is None:
        return False, "repo has no batch gate"
    if not p.get("repo"):
        return False, "vault task"
    if _as_bool(p.get(KEY_VERIFY_FULL)):
        return False, "returned to full verify"
    if not five_stage or str(p.get("worker_mode") or "").lower() != "auto":
        return False, "not the 5-stage auto pipeline"
    if not auto_merge:
        return False, "auto_merge off"
    if remote_marked:
        return False, "full gate is remote-marked"
    asked = _as_bool(p.get(KEY_VERIFY_BATCH))
    if asked is False:
        return False, f"{KEY_VERIFY_BATCH}: false"
    if asked is True:
        return True, f"{KEY_VERIFY_BATCH}: true"
    return tier in _BATCHABLE_TIERS, f"tier {tier}"


def device_ok(payload, *, device_pass, ui_hold) -> bool:
    """May a quick-verified run wait for the integration gate?

    Not when the UI confidence guard fired (a high-confidence app-UI change
    nobody saw on a device), and not when the task asked for a device run that
    left no evidence this run."""
    return not ui_hold and (not needs_device(payload) or bool(device_pass))


def mark_ready(a, result_payload, *, now_iso: str) -> bool:
    """Move a ledger row to ``batch_ready`` from a runner RESULT. True if moved.

    Needs a PR url, a repo and the sha the quick verify judged. If any is
    missing the row is left exactly as it was and False is returned, so the
    caller falls back to the ordinary review park."""
    from .assignment import BATCH_READY

    rp = result_payload if isinstance(result_payload, dict) else {}
    row = a.payload if isinstance(a.payload, dict) else {}
    batch = rp.get(KEY_BATCH)
    pr_url = str(rp.get("pr_url") or "").strip()
    repo = str(rp.get("repo") or row.get("repo") or "").strip()
    head_sha = str((batch.get("head_sha") if isinstance(batch, dict) else "")
                   or "").strip()
    if not (pr_url and repo and head_sha):
        return False
    branch = str(rp.get("branch") or row.get("branch") or f"worker/{a.task_id}")
    run_dir = str(rp.get("run_dir") or "").strip()
    if not isinstance(a.payload, dict):
        a.payload = {}
    a.state = BATCH_READY
    a.payload[KEY_BATCH] = {
        "pr_url": pr_url,
        "head_sha": head_sha,
        "branch": branch,
        "ready_ts": now_iso,
        # The run-id basename only: the ledger is git-tracked and must not carry
        # one host's absolute path (the `last_run_dir` precedent).
        "run": Path(run_dir).name if run_dir else "",
    }
    a.updated_ts = now_iso
    a.last_note = "묶음 검증 대기 — quick Verify 통과, 통합 게이트 뒤 머지"
    a.history.append({"ts": now_iso, "type": HISTORY_READY, "from": "orchestrator"})
    return True


def mark_returned(a, reason: str, *, now_iso: str) -> None:
    """Send a ledger row back to an individual full verify.

    The row becomes FAILED with `batch-returned` as its last history entry,
    which assignment.is_batch_return_pending reads: the orchestrator then treats
    it as a deferral, not a failure. ``refail_count`` and ``timeout_streak`` are
    left alone for the same reason — nothing failed. ``verify_full`` on the
    payload keeps the next run out of the batch."""
    from .assignment import BATCH_RETURNED, FAILED

    if not isinstance(a.payload, dict):
        a.payload = {}
    a.state = FAILED
    a.payload.pop(KEY_BATCH, None)
    a.payload[KEY_VERIFY_FULL] = True
    a.payload[KEY_RETURN_REASON] = reason
    a.updated_ts = now_iso
    a.capacity_limited = False
    a.job_running = False
    a.checkout_busy = False
    a.timed_out = False
    a.blocked_on = ""
    a.last_note = f"묶음 복귀 — 개별 전체 Verify로 ({reason})"[:200]
    a.history.append({"ts": now_iso, "type": BATCH_RETURNED, "reason": reason,
                      "from": "orchestrator"})


def verify_hint(gate, default_branch: str = "") -> str:
    """The sentence appended to the QUICK verify instruction for this repo: the
    full-gate commands being deferred, the command that lists the changed files,
    and the repo's own ``quick_hint`` when it has one."""
    g = gate if isinstance(gate, dict) else {}
    commands = [shlex.join(argv) for argv in g.get("full_gate") or []]
    parts: list[str] = []
    if commands:
        parts.append("Deferred full gate for this repo: "
                     + ", ".join(f"`{c}`" for c in commands) + ".")
    parts.append("Changed files: `git diff --name-only "
                 f"origin/{default_branch or '<default>'}...HEAD`.")
    hint = g.get("quick_hint")
    if isinstance(hint, str) and hint.strip():
        parts.append(hint.strip())
    return " ".join(parts)
