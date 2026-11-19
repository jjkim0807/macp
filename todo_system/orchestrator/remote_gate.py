"""Run a heavy/memory-bound verification command on a separate high-spec node.

Generalizes ``long_job`` for the case a verification GATE (not just a data
pre-fetch) is too big for one stage: a command that overruns the per-stage
subprocess cap, or that cannot finish inside the runner cgroup's ``MemoryHigh``.
The motivating case is if-then-habits' full ``:shared:testDebugUnitTest``
Robolectric suite — mulkko-118 timed it out on the 2 vCPU / 7 GB HQ VM across
Implement ×2 and Verify ×3 (≈5h, ≈$35) before a human merged without the suite
ever completing. This module sends that command to a roomier node and hands the
stage back only the RESULT (rc · JUnit XML aggregate · log tail · duration) to
use as a gate.

Runner, not stage — the same split :mod:`beta_publish` and :mod:`screen_check`
already make. The node round-trip needs gcloud/ssh/scp, which a stage's
``--allowed-tools`` whitelist forbids, and the heavy wall-clock must live OUTSIDE
the stage's timeout window or it just re-spends the very cap we are escaping.
:func:`run_remote_gate` is therefore the executor the *runner runtime* (or the
detached ``jobs``-style wrapper job) calls, mirroring ``screen_check.run_check``;
``scripts/macp-remote-gate`` is the thin stage-facing wrapper whose ``run``
subcommand starts that executor DETACHED and returns at once, so a stage can
request/inspect a remote gate without ever block-polling it. ``jobs.py``'s
docstring records the exact failure a block-poll reproduces (ai-stock-trading-8b
lost its collection at 15/100 when the stage exited).

Always tear the node down. Like ``screen_check``'s never-raise always-stop, the
executor stops-or-deletes the node on EVERY exit path — build failure, timeout,
preemption, cap breach, budget trip, or any exception — and ``create-delete``
also passes ``--max-run-duration`` so even a leaked VM self-terminates. A remote
failure (quota, create failure, ssh) is a handled state (``fell_back_local``),
never an exception: the caller then runs the command locally.

Cost is rationed. A per-run VM lifetime cap and a monthly USD cap (default $20,
a LOCAL ``vm_seconds × hourly_rate`` estimate — never a billing API call, so it
is offline-testable) live in the vault's ``.macp/remote_gate.json``; crossing the
monthly cap disables remote execution and fires exactly ONE throttled alert for
that month.

Testability: every external effect — gcloud up/create, ssh/scp, the remote
gradle, gcloud stop/delete — flows through the injected ``run=`` callable, and
the notify / jobs-dir / budget-ledger / clock are injectable too, so the unit
suite drives a fake and never shells out to gcloud or an emulator and never
touches the real vault (the todo_system test conftest fails the whole run if it
does). The real node round-trip is NOT exercised by this Implement pipeline (its
allowed tools carry no gcloud/ssh/scp); it is a runtime concern bounded by the
per-run TTL. Node project/zone/machine are read from the vault config, NEVER
hardcoded here — the public engine/vault split (``beta_publish``'s ``.macp/``
precedent): engine code carries only neutral fallbacks.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from .. import paths
from . import jobs
from . import stages

# Vault-data root — the same resolver split beta_publish / stages use. The config
# canon is operator-owned vault data (node identity, cost caps), so it follows the
# redirectable vault root, not the engine root.
_VAULT_DATA_ROOT = paths.vault_root(start=paths.vault_search_start())
# The remote-gate config/canon: node identity + cost caps. Tracked vault data in
# .macp/ (beta_publish_config.json precedent), OUTSIDE the public-export scan, so
# the real project/zone/node live here and engine code stays neutral.
REMOTE_CONFIG_PATH = _VAULT_DATA_ROOT / ".macp" / "remote_gate.json"
# Stage run dirs — the prior-run timeout signal (verify.md / stages.json) is read
# from here. Redirectable like stages.RUNS_DIR (same _VAULT_DATA_ROOT anchor).
RUNS_DIR = _VAULT_DATA_ROOT / "Worker" / "runs"
# Job output (gitignored runtime): Worker/jobs/<slug>-remote-<n>.{json,rc,log}.
# Reuse jobs.JOBS_DIR so the detached wrapper job and this executor agree on the
# directory without recomputing it.
JOBS_DIR = jobs.JOBS_DIR
# Per-install scratch for the budget ledger — gitignored Worker/_state, anchored
# like beta_publish's LOCK_DIR: the engine root in a checkout (immune to a
# redirected vault), the vault in an installed wheel (no writable Worker/ beside
# site-packages). Nothing here is canon; committing it would stall the queue.
_SCRATCH_ROOT = _VAULT_DATA_ROOT if paths.is_installed() else paths.engine_root()
BUDGET_PATH = _SCRATCH_ROOT / "Worker" / "_state" / "remote_gate_budget.json"

# Defaults when the config is absent or a key is missing — all NEUTRAL (no real
# project/node). The real values live in .macp/remote_gate.json (vault data).
_DEFAULT_MONTHLY_CAP_USD = 20.0
_DEFAULT_VM_TTL_MIN = 120                 # per-run VM lifetime cap: 2h
_DEFAULT_NODE_MODE = "start-stop"
_NODE_MODES = ("start-stop", "create-delete")
# A neutral default zone/machine (generic GCP values, not personal identifiers);
# project defaults to None so the backend falls back to gcloud's ACTIVE config
# (scripts/app-test-node passes no --project either). hourly_rate feeds only the
# offline cost estimate.
_DEFAULT_ZONE = "us-central1-a"
_DEFAULT_MACHINE_TYPE = "n2-standard-4"
_DEFAULT_NODE_NAME = "remote-gate-node"
_DEFAULT_HOURLY_RATE_USD = 0.05

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], *, cwd: str | None = None, env: dict | None = None,
         timeout: float | None = None) -> "subprocess.CompletedProcess[str]":
    """Default subprocess runner — the single seam every external effect flows
    through (gcloud up/create, ssh/scp, the remote gradle, gcloud stop/delete).
    Never raises on a non-zero exit (callers inspect ``returncode``); a timeout is
    allowed to propagate so the cap can turn it into a forced teardown."""
    return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace")


def _as_bool(v: object, default: bool = False) -> bool:
    """Truthiness tolerant of the string forms a JSON/frontmatter value arrives
    as (mirrors ``screen_check.requested``). ``None`` → ``default``."""
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(v)


# ---------------------------------------------------------------------------
# Config canon (git-tracked JSON under the vault's .macp/) — pure readers
# ---------------------------------------------------------------------------

def load_remote_config(config_path: Path | str = REMOTE_CONFIG_PATH) -> dict:
    """The remote-gate config dict, or ``{}`` when the file is missing/unreadable.

    Fail-safe like ``beta_publish.load_config``: a missing or corrupt config can
    only ever DISABLE remote execution (``remote_enabled({})`` is False), never
    crash a run. Callers apply their own defaults to the returned dict."""
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def remote_enabled(cfg: dict) -> bool:
    """Is remote execution turned on? Default OFF — an absent/empty config (a
    fresh clone with no ``.macp/remote_gate.json``) behaves exactly as before,
    local-only. Only an explicit truthy ``enabled`` turns it on."""
    return _as_bool((cfg or {}).get("enabled", False))


def monthly_cap_usd(cfg: dict) -> float:
    """The monthly USD cap (default $20). A non-numeric/non-positive value falls
    back to the default rather than disabling the cap — a broken cap must fail
    toward LESS spend, not unlimited."""
    raw = (cfg or {}).get("monthly_cap_usd", _DEFAULT_MONTHLY_CAP_USD)
    try:
        v = float(raw)
        return v if v > 0 else _DEFAULT_MONTHLY_CAP_USD
    except (TypeError, ValueError):
        return _DEFAULT_MONTHLY_CAP_USD


def per_run_vm_ttl_s(cfg: dict) -> int:
    """Per-run VM lifetime cap in seconds (default 7200 = 2h), from the config's
    ``per_run_vm_ttl_min`` (minutes). Non-positive/unparseable → default. This is
    both the local deadline and (``create-delete``) the server-side
    ``--max-run-duration`` that orphan-proofs a leaked VM."""
    raw = (cfg or {}).get("per_run_vm_ttl_min", _DEFAULT_VM_TTL_MIN)
    try:
        mins = int(raw)
        if mins > 0:
            return mins * 60
    except (TypeError, ValueError):
        pass
    return _DEFAULT_VM_TTL_MIN * 60


def node_mode(cfg: dict) -> str:
    """The node backend: ``"start-stop"`` (default — reuse an existing spot node,
    proven gcloud perms) or ``"create-delete"`` (on-demand, needs a toolchain
    image + ``instances create`` IAM). An unknown value falls back to the safe
    default rather than driving an unimplemented backend."""
    mode = str((cfg or {}).get("node_mode", _DEFAULT_NODE_MODE)).strip()
    return mode if mode in _NODE_MODES else _DEFAULT_NODE_MODE


def node_params(cfg: dict) -> dict:
    """Node identity/placement for the backend, with NEUTRAL fallbacks in code.

    Keys: ``project`` (None → gcloud active config), ``zone``, ``machine_type``,
    ``node`` (instance name), ``image`` (None for start-stop), ``hourly_rate_usd``
    (for the offline cost estimate). The real values are read from
    ``.macp/remote_gate.json``; this module never hardcodes a project/node.

    The ``create-delete`` create spec (see :func:`node_create_argv`) adds
    optional keys, each OMITTED from the create argv when absent so a bare
    config still creates a plain VM: ``spot`` / ``nested_virtualization`` /
    ``no_address`` / ``no_service_account`` (bools, default False),
    ``boot_disk_gb`` (int), ``boot_disk_type``, ``image_family`` (wins over
    ``image``), ``image_project``, ``network``, ``subnet``, ``tags``,
    ``service_account``, ``scopes``, ``labels`` (strings, gcloud's own
    comma-separated forms)."""
    cfg = cfg or {}
    return {
        "project": cfg.get("project") or None,
        "zone": str(cfg.get("zone") or _DEFAULT_ZONE),
        "machine_type": str(cfg.get("machine_type") or _DEFAULT_MACHINE_TYPE),
        "node": str(cfg.get("node") or _DEFAULT_NODE_NAME),
        "image": cfg.get("image") or None,
        "hourly_rate_usd": _coerce_rate(cfg.get("hourly_rate_usd")),
        "spot": _as_bool(cfg.get("spot")),
        "nested_virtualization": _as_bool(cfg.get("nested_virtualization")),
        "no_address": _as_bool(cfg.get("no_address")),
        "no_service_account": _as_bool(cfg.get("no_service_account")),
        "boot_disk_gb": _coerce_pos_int(cfg.get("boot_disk_gb")),
        **{k: _opt_str(cfg.get(k)) for k in _CREATE_STR_KEYS},
    }


# Optional string keys of the create spec, in the order their flags are emitted.
_CREATE_STR_KEYS = ("boot_disk_type", "image_family", "image_project", "network",
                    "subnet", "tags", "service_account", "scopes", "labels")


def _opt_str(raw: object) -> str | None:
    s = "" if raw is None else str(raw).strip()
    return s or None


def _coerce_pos_int(raw: object) -> int | None:
    try:
        v = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _coerce_rate(raw: object) -> float:
    try:
        v = float(raw)  # type: ignore[arg-type]
        return v if v >= 0 else _DEFAULT_HOURLY_RATE_USD
    except (TypeError, ValueError):
        return _DEFAULT_HOURLY_RATE_USD


# ---------------------------------------------------------------------------
# Per-repo gate config (the TARGET repo's .macp/gates.json) — pure readers
# ---------------------------------------------------------------------------

def load_gates(repo_checkout: Path | str) -> dict:
    """The target repo's ``<checkout>/.macp/gates.json`` as a dict, or ``{}``.

    This reads the REPO being verified (task body: "리포의 .macp/gates.json"),
    not the vault — a repo marks which of its verification commands are heavy /
    must run remote. Missing/unreadable → ``{}`` (no gate opinions → fall through
    to the prior-run signal and then local)."""
    try:
        data = json.loads((Path(repo_checkout) / ".macp" / "gates.json")
                          .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def gate_for_command(gates: dict, cmd: str) -> dict | None:
    """The ``commands[]`` entry whose ``cmd`` EXACTLY equals ``cmd``, else None.

    Exact match on purpose: a repo opts a specific command in to remote/heavy, and
    a substring rule would sweep in sibling invocations (``:shared:testDebug`` vs
    ``:shared:testDebugUnitTest``) the operator did not mark."""
    want = (cmd or "").strip()
    for entry in (gates or {}).get("commands", []) or []:
        if isinstance(entry, dict) and str(entry.get("cmd", "")).strip() == want:
            return entry
    return None


# ---------------------------------------------------------------------------
# Prior-run "could-not-finish-locally" signal + the decision rule
# ---------------------------------------------------------------------------

# Phrases a verify.md checks-table row (or a run.log line) uses for a per-command
# cap or a memory stop. Matched case-insensitively and ONLY in a row that ALSO
# names the command, so a green row that merely runs the same command is never
# flagged.
_TIMEOUT_PHRASE_RE = re.compile(
    r"(?i)timed?\s*out|60-?\s*min(?:ute)?\s*cap|per-command cap|"
    r"memory\.high|\bOOM\b|sigkill|sigterm|freeze|frozen|killed")
# Exit 143 (SIGTERM, the cap's forced kill) / 137 (SIGKILL, an OOM kill) as a
# STANDALONE token — bounded by non-alphanumeric/non-dot so a sha like `0a143fd`
# or `abc137` can never trip it, only a table cell like ` 143 `.
_TIMEOUT_EXIT_RE = re.compile(r"(?<![0-9A-Za-z.])(?:143|137)(?![0-9A-Za-z.])")


def _row_marks_timeout(line: str) -> bool:
    """Does this verify.md row / run.log line carry a timeout-or-memory marker?"""
    return bool(_TIMEOUT_PHRASE_RE.search(line) or _TIMEOUT_EXIT_RE.search(line))


def _verify_md_timed_out_cmd(verify_md_path: Path, cmd: str) -> bool:
    """Did a prior run's verify.md record that THIS command could not finish?

    PRIMARY signal (mulkko-118's actual on-disk shape): a per-command 60-min cap
    shows up as a checks-table row that names the command AND carries a timeout
    marker (``Exit 143`` / "Timed out at 60-min cap"). Requiring BOTH on the same
    row is the whole guard — mulkko-118's row #1 runs the very same command to a
    green ``Exit 0`` and must NOT be read as a timeout."""
    needle = (cmd or "").strip()
    if not needle:
        return False
    try:
        text = Path(verify_md_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(needle in line and _row_marks_timeout(line)
               for line in text.splitlines())


def _stages_json_timed_out(stages_json_path: Path) -> bool:
    """SECONDARY signal: a whole-STAGE subprocess timeout, which stages.json
    records as ``timed_out_stage`` (command-agnostic — the stage itself was
    SIGKILLed, most likely on the heavy command). mulkko-118 has no such field
    because all five stages completed; the per-command cap lived only in
    verify.md, which is why that is the primary source."""
    try:
        data = json.loads(Path(stages_json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("timed_out_stage"))


def prior_run_timed_out(task_id: str, cmd: str, *, runs_dir: Path | str = RUNS_DIR,
                        exclude_run_id: str | None = None) -> bool:
    """Did the most-recent PRIOR run of this task fail to finish ``cmd`` locally?

    Reads the newest run dir for ``task_id`` other than ``exclude_run_id`` (the
    current run, which has no verdict yet): verify.md first (the per-command cap),
    then stages.json ``timed_out_stage`` (a whole-stage timeout). Run discovery
    reuses ``stages.runs_for`` — the ``<timestamp>-<slug>`` match is defined there
    and nowhere else, so this cannot drift from the retry-inheritance path."""
    runs = [p for p in stages.runs_for(task_id, Path(runs_dir))
            if p.name != exclude_run_id]
    if not runs:
        return False
    latest = runs[-1]                       # runs_for yields oldest-first
    return (_verify_md_timed_out_cmd(latest / stages.STAGE_ARTIFACT["verify"], cmd)
            or _stages_json_timed_out(latest / "stages.json"))


def decide(cmd: str, repo_checkout: Path | str | None = None, *,
           gates: dict | None = None, prior_timed_out: bool = False,
           budget_exhausted: bool = False) -> str:
    """Where to run ``cmd``: ``"remote"`` or ``"local"``. Pure.

    Order (budget override first — it is the one thing that can VETO remote):
      - ``budget_exhausted`` → ``"local"`` (can't afford remote; the caller emits
        the single throttled alert separately).
      (a) the repo's ``.macp/gates.json`` marks this command ``remote`` or
          ``heavy`` → ``"remote"``.
      (b) else the same command timed out / hit the memory wall last run → ``"remote"``.
      (c) else → ``"local"`` (the common path — unchanged behaviour).

    ``gates`` may be passed directly (the test / a caller that already loaded it);
    when omitted it is loaded from ``repo_checkout``'s ``.macp/gates.json``."""
    if budget_exhausted:
        return "local"
    if gates is None:
        gates = load_gates(repo_checkout) if repo_checkout is not None else {}
    gate = gate_for_command(gates, cmd)
    if gate is not None and (_as_bool(gate.get("remote")) or _as_bool(gate.get("heavy"))):
        return "remote"
    if prior_timed_out:
        return "remote"
    return "local"


# ---------------------------------------------------------------------------
# JUnit XML aggregation (pure) + local cost estimate (pure)
# ---------------------------------------------------------------------------

_JUNIT_KEYS = ("tests", "failures", "errors", "skipped")


def _int_attr(el: "ET.Element", name: str) -> int:
    try:
        return int(float(el.get(name, 0)))
    except (TypeError, ValueError):
        return 0


def _read_xml_item(item: object) -> str | None:
    """An item is either a path to a JUnit XML file or an XML text string."""
    try:
        p = Path(item)  # type: ignore[arg-type]
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        pass
    return item if isinstance(item, str) and "<" in item else None


def aggregate_junit_xml(items) -> dict:
    """Sum tests/failures/errors/skipped across JUnit XML ``build/test-results``.

    Each item is an XML file path OR an XML text string (so the pure unit test
    passes strings and the executor passes the chunks it harvested). Robust to a
    ``<testsuites>`` wrapper, to missing attributes (counted 0), and to
    unparseable input (skipped) — a torn XML must never crash the gate harvest.
    Returns ``{"tests","failures","errors","skipped","suites"}``; ``passed`` is
    derivable (tests − failures − errors − skipped) and added for convenience."""
    totals = {k: 0 for k in _JUNIT_KEYS}
    suites = 0
    for item in items or []:
        text = _read_xml_item(item)
        if not text:
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue
        for suite in root.iter("testsuite"):    # includes root itself if it IS a testsuite
            suites += 1
            for k in _JUNIT_KEYS:
                totals[k] += _int_attr(suite, k)
    totals["suites"] = suites
    totals["passed"] = max(0, totals["tests"] - totals["failures"]
                           - totals["errors"] - totals["skipped"])
    return totals


def aggregate_junit_dir(results_root: Path | str) -> dict:
    """``aggregate_junit_xml`` over every ``*.xml`` under ``results_root``."""
    try:
        files = sorted(Path(results_root).glob("**/*.xml"))
    except OSError:
        files = []
    return aggregate_junit_xml(files)


def estimate_cost_usd(vm_seconds: float, hourly_rate_usd: float) -> float:
    """A LOCAL estimate (``vm_seconds / 3600 × rate``), never a billing API call —
    so the monthly cap is offline-testable (requirements OQ#5). Non-negative."""
    try:
        return max(0.0, float(vm_seconds)) / 3600.0 * max(0.0, float(hourly_rate_usd))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Monthly budget ledger (gitignored runtime, Worker/_state) — one alert/month
# ---------------------------------------------------------------------------

def _month_key(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now().astimezone()).strftime("%Y-%m")


def load_budget(path: Path | str = BUDGET_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_budget(path: Path | str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _month_entry(data: dict, now: dt.datetime | None) -> dict:
    return data.setdefault("months", {}).setdefault(
        _month_key(now), {"usd": 0.0, "alerted": False})


def month_spent_usd(now: dt.datetime | None = None, *, path: Path | str = BUDGET_PATH) -> float:
    try:
        return float(_month_entry(load_budget(path), now).get("usd", 0.0))
    except (TypeError, ValueError):
        return 0.0


def record_cost(usd: float, now: dt.datetime | None = None, *,
                path: Path | str = BUDGET_PATH) -> float:
    """Add ``usd`` to this month's running total; return the new total."""
    data = load_budget(path)
    month = _month_entry(data, now)
    try:
        month["usd"] = round(float(month.get("usd", 0.0)) + max(0.0, float(usd)), 6)
    except (TypeError, ValueError):
        month["usd"] = float(usd or 0.0)
    _save_budget(path, data)
    return float(month["usd"])


def budget_exhausted(cfg: dict, now: dt.datetime | None = None, *,
                     path: Path | str = BUDGET_PATH) -> bool:
    """Has this month's remote spend reached the monthly cap? When True the
    caller forces ``decide`` → local and (once) alerts."""
    return month_spent_usd(now, path=path) >= monthly_cap_usd(cfg)


def note_budget_alert(cfg: dict, now: dt.datetime | None = None, *,
                      path: Path | str = BUDGET_PATH,
                      notify: Callable[[str], None] | None = None,
                      progress: Callable[[str], None] | None = None) -> bool:
    """Fire EXACTLY ONE alert the first time a month crosses its cap.

    Per-month ``alerted`` flag (mirror beta_publish sweep's ``alerted_sha``): the
    cap can be re-checked on every dispatch, but a human should hear about it once
    a month, not once a tick. Returns True iff it alerted on this call."""
    now_dt = now or dt.datetime.now().astimezone()
    data = load_budget(path)
    month = _month_entry(data, now_dt)
    spent = float(month.get("usd", 0.0))
    if spent < monthly_cap_usd(cfg) or month.get("alerted"):
        return False
    month["alerted"] = True
    _save_budget(path, data)
    msg = (f"💸 [원격 게이트 비활성] {_month_key(now_dt)} 원격 실행비 추정 "
           f"${spent:.2f} ≥ 월 상한 ${monthly_cap_usd(cfg):.0f} — 이번 달 원격 실행을 "
           f"끄고 로컬로 폴백합니다 (상향은 .macp/remote_gate.json)")
    if progress:
        progress(msg)
    try:
        (notify or _default_notify)(msg)
    except Exception:       # noqa: BLE001 — an alert failure must not fail the run
        pass
    return True


def _default_notify(message: str) -> None:
    """Best-effort kakao alert. Imported lazily so this engine module stays
    stdlib-only AT IMPORT (``check_engine_isolation.sh`` archives and imports the
    tree); ``worker.kakao_notify`` itself never raises."""
    try:
        from .. import worker
        worker.kakao_notify(message)
    except Exception:       # noqa: BLE001 — notification is best-effort
        pass


# ---------------------------------------------------------------------------
# Node round-trip (every external effect via the injected run=) — always tear down
# ---------------------------------------------------------------------------

_STOP_TIMEOUT_S = 180           # teardown gets its OWN fixed timeout, like screen_check
_SSH_PREP_TIMEOUT_S = 900       # clone+fetch+checkout on a fresh node
_JUNIT_SEP = "===RG-JUNIT==="   # per-file separator for the harvest ssh call
_REMOTE_REPO_DIR = "rg-src"     # the node-side checkout dir (under $HOME)


class RemoteGateError(RuntimeError):
    """A node/infra failure (up/create, ssh prepare, teardown) — distinct from the
    gate COMMAND's own non-zero rc, which is a legitimate verdict, not an error.
    The executor absorbs this into ``fell_back_local=True`` and never re-raises."""


def _gcloud() -> str:
    """The gcloud binary (env override, else the snap path scripts/app-test-node
    also defaults to). Never a project literal — project is a config value."""
    return os.environ.get("GCLOUD") or "/snap/bin/gcloud"


def _base_node_argv(params: dict, *tail: str) -> list[str]:
    argv = [_gcloud(), "compute", *tail, params["node"], "--zone", params["zone"], "--quiet"]
    if params.get("project"):
        argv += ["--project", params["project"]]
    return argv


def _ssh(run: Runner, params: dict, command: str, *, timeout: float) -> "subprocess.CompletedProcess[str]":
    argv = [_gcloud(), "compute", "ssh", params["node"], "--zone", params["zone"],
            "--quiet", "--command", command]
    if params.get("project"):
        argv += ["--project", params["project"]]
    return run(argv, timeout=timeout)


def node_create_argv(params: dict, ttl_s: int) -> list[str]:
    """The ``instances create`` argv for ``create-delete``. Pure.

    ``--max-run-duration`` + termination=DELETE are unconditional: a server-side
    orphan backstop even if teardown is somehow missed (and, on a spot VM, a
    preemption therefore deletes rather than stops). Every other spec flag comes
    from :func:`node_params` and is omitted when its key is absent, so the engine
    carries no node/image literal."""
    argv = [_gcloud(), "compute", "instances", "create", params["node"],
            "--zone", params["zone"], "--machine-type", params["machine_type"],
            f"--max-run-duration={int(ttl_s)}s",
            "--instance-termination-action=DELETE", "--quiet"]
    if params.get("image_family"):
        argv += ["--image-family", params["image_family"]]
    elif params.get("image"):
        argv += ["--image", params["image"]]
    if params.get("image_project"):
        argv += ["--image-project", params["image_project"]]
    if params.get("spot"):
        argv.append("--provisioning-model=SPOT")
    if params.get("nested_virtualization"):
        argv.append("--enable-nested-virtualization")
    if params.get("boot_disk_gb"):
        argv.append(f"--boot-disk-size={int(params['boot_disk_gb'])}GB")
    flag = {"boot_disk_type": "--boot-disk-type", "network": "--network",
            "subnet": "--subnet", "tags": "--tags", "labels": "--labels"}
    for key in ("boot_disk_type", "network", "subnet", "tags", "labels"):
        if params.get(key):
            argv.append(f"{flag[key]}={params[key]}")
    if params.get("no_service_account"):
        argv += ["--no-service-account", "--no-scopes"]
    else:
        if params.get("service_account"):
            argv.append(f"--service-account={params['service_account']}")
        if params.get("scopes"):
            argv.append(f"--scopes={params['scopes']}")
    if params.get("no_address"):
        argv.append("--no-address")
    if params.get("project"):
        argv += ["--project", params["project"]]
    return argv


# gcloud's not-found wording on describe/delete/stop of a missing instance
# ("The resource '…/instances/x' was not found", or the API reason notFound).
_NOT_FOUND_RE = re.compile(r"(?i)was not found|\bnotFound\b")
# Statuses where the instance is (or is becoming) up — reuse, never create.
_UP_STATUSES = ("RUNNING", "PROVISIONING", "STAGING", "REPAIRING")
# Statuses where it exists but is down — `instances start` it.
_DOWN_STATUSES = ("TERMINATED", "STOPPED", "SUSPENDED", "STOPPING", "SUSPENDING")


def _is_not_found(r: object) -> bool:
    return bool(_NOT_FOUND_RE.search((getattr(r, "stderr", "") or "")
                                     + (getattr(r, "stdout", "") or "")))


def _node_status(run: Runner, params: dict, *, timeout: float) -> str | None:
    """The instance's status (``RUNNING``, ``TERMINATED``, …), or ``None`` when
    it does not exist. Any other describe failure raises :class:`RemoteGateError`
    — guessing "absent" there would create a second VM next to a live one."""
    r = run(_base_node_argv(params, "instances", "describe") + ["--format=value(status)"],
            timeout=timeout)
    if getattr(r, "returncode", 1) != 0:
        if _is_not_found(r):
            return None
        raise RemoteGateError(f"node describe rc={getattr(r, 'returncode', None)}")
    return (getattr(r, "stdout", "") or "").strip().upper()


def _node_up(run: Runner, mode: str, params: dict, ttl_s: int, *, timeout: float,
             progress: Callable[[str], None] | None = None) -> str:
    """Start (start-stop) or create (create-delete) the node. Raises
    :class:`RemoteGateError` on a non-zero rc — an infra failure, so the caller
    falls back to local. Returns what it did: ``started`` / ``created`` /
    ``reused``.

    ``create-delete`` is idempotent so a retry after a spot preemption (which
    DELETES the VM) and a second caller both work: describe first — up → reuse,
    down → ``instances start``, absent → ``instances create``
    (:func:`node_create_argv`)."""
    say = progress or (lambda _m: None)
    if mode == "create-delete":
        status = _node_status(run, params, timeout=timeout)
        if status is None:
            argv, action = node_create_argv(params, ttl_s), "created"
        elif status in _UP_STATUSES:
            say(f"remote gate: node already {status} — reused")
            return "reused"
        elif status in _DOWN_STATUSES:
            argv, action = _base_node_argv(params, "instances", "start"), "started"
        else:
            raise RemoteGateError(f"node up (create-delete) unexpected status {status!r}")
    else:
        argv, action = _base_node_argv(params, "instances", "start"), "started"
    r = run(argv, timeout=timeout)
    if getattr(r, "returncode", 1) != 0:
        raise RemoteGateError(f"node up ({mode}) rc={getattr(r, 'returncode', None)}")
    say(f"remote gate: node {action}")
    return action


def _node_down(run: Runner, mode: str, params: dict,
               progress: Callable[[str], None], *, timeout: float = _STOP_TIMEOUT_S) -> int | None:
    """Stop (start-stop) or delete (create-delete) the node, NEVER raising.

    The twin of ``screen_check._stop_node``: runs in the inner ``finally`` on every
    exit path, with its OWN fixed timeout (not the expired cap deadline), so the
    VM is still torn down after a cap breach. Stopping/deleting an un-started node
    is idempotent, so it is called unconditionally. Returns the gcloud rc — ``0``
    also when the instance was already absent (a preempted create-delete VM is
    already gone) — or ``None`` when the call itself raised."""
    sub = "delete" if mode == "create-delete" else "stop"
    try:
        r = run(_base_node_argv(params, "instances", sub), timeout=timeout)
        rc = getattr(r, "returncode", None)
        if rc not in (0, None) and _is_not_found(r):
            progress(f"remote gate: node {sub} — already absent")
            return 0
        progress(f"remote gate: node {sub} rc={rc}")
        return rc
    except Exception as e:      # noqa: BLE001 — teardown must not mask the real outcome
        progress(f"remote gate: node {sub} skipped ({type(e).__name__})")
        return None


def _remote_prepare(run: Runner, params: dict, repo_url: str, sha: str, *, timeout: float) -> None:
    """Clone (once) + fetch + check out ``sha`` on the node. Infra → RemoteGateError."""
    script = (
        f'set -e; d="$HOME/{_REMOTE_REPO_DIR}"; '
        f'if [ ! -d "$d/.git" ]; then git clone "{repo_url}" "$d"; fi; '
        f'cd "$d"; git fetch origin --prune --quiet; '
        f'git checkout -f "{sha}" 2>/dev/null || '
        f'{{ git fetch origin "{sha}" --quiet && git checkout -f "{sha}"; }}')
    r = _ssh(run, params, script, timeout=timeout)
    if getattr(r, "returncode", 1) != 0:
        raise RemoteGateError(f"remote prepare rc={getattr(r, 'returncode', None)}")


def _remote_exec(run: Runner, params: dict, cmd: str, *, timeout: float) -> tuple[int, str]:
    """Run the gate command in the checkout. Returns ``(rc, combined_log)``; the
    command's rc is a VERDICT (not an error), so a non-zero rc never raises."""
    r = _ssh(run, params, f'cd "$HOME/{_REMOTE_REPO_DIR}" && {cmd}', timeout=timeout)
    log = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
    return getattr(r, "returncode", 1), log


def _harvest_junit(run: Runner, params: dict, *, timeout: float) -> dict:
    """Pull every ``build/test-results/**/*.xml`` from the node as one stream
    (each file preceded by a separator line) and aggregate. Best-effort: a failed
    harvest returns an empty aggregate rather than failing the gate — the rc is
    the primary verdict and Gradle writes XML only on a clean finalize."""
    script = (
        f'cd "$HOME/{_REMOTE_REPO_DIR}" 2>/dev/null || exit 0; '
        f'find . -path "*/build/test-results/*.xml" -type f 2>/dev/null | '
        f'while IFS= read -r f; do echo "{_JUNIT_SEP}"; cat "$f"; done')
    try:
        r = _ssh(run, params, script, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return aggregate_junit_xml([])
    if getattr(r, "returncode", 1) != 0:
        return aggregate_junit_xml([])
    chunks = [c for c in (getattr(r, "stdout", "") or "").split(_JUNIT_SEP) if c.strip()]
    return aggregate_junit_xml(chunks)


# ---------------------------------------------------------------------------
# Worker/jobs output + the executor
# ---------------------------------------------------------------------------

def _next_index(jobs_dir: Path, slug: str) -> int:
    n = 0
    rx = re.compile(re.escape(slug) + r"-remote-(\d+)\.json$")
    try:
        for p in Path(jobs_dir).glob(f"{slug}-remote-*.json"):
            m = rx.search(p.name)
            if m:
                n = max(n, int(m.group(1)))
    except OSError:
        pass
    return n + 1


def _tail(text: str, n: int = 200) -> str:
    return "\n".join((text or "").splitlines()[-n:])


def _write_job_files(jobs_dir: Path, handle: str, result: dict, log_text: str) -> None:
    """Write ``Worker/jobs/<handle>.{json,rc,log}`` (AC#2 output)."""
    jobs_dir = Path(jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{handle}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rc = result.get("rc")
    (jobs_dir / f"{handle}.rc").write_text("" if rc is None else str(rc), encoding="utf-8")
    (jobs_dir / f"{handle}.log").write_text(log_text or "", encoding="utf-8")


def run_remote_gate(slug: str, repo_url: str, sha: str, cmd: str, *,
                    toolchain: str = "android", timeout: int | None = None,
                    cfg: dict | None = None, run: Runner = _run,
                    notify: Callable[[str], None] | None = None,
                    jobs_dir: Path | str | None = None,
                    budget_path: Path | str = BUDGET_PATH,
                    now: dt.datetime | None = None,
                    progress: Callable[[str], None] | None = None,
                    index: int | None = None) -> dict:
    """Run ``cmd`` on the remote node and return/record the gate result.

    Structure mirrors ``screen_check._run_locked``: the body (node up → checkout
    sha → run cmd → harvest JUnit) is the outer try; the inner ``finally`` tears
    the node down UNCONDITIONALLY (success, build-fail, timeout, cap breach,
    exception). An INFRA failure (node up/create, ssh prepare — a
    :class:`RemoteGateError`, timeout, or OS error) is absorbed into
    ``fell_back_local=True`` + one progress line and NEVER re-raised; the gate
    COMMAND's own non-zero rc is a verdict and is reported as-is.

    Writes ``Worker/jobs/<slug>-remote-<n>.{json,rc,log}`` and returns the result
    dict. Over the monthly cap it tears NOTHING up: it alerts once and returns
    ``fell_back_local=True`` without touching a node (don't spend when over cap).
    Every external effect flows through ``run=`` so the unit suite never shells
    out; ``notify``/``jobs_dir``/``budget_path``/``now`` are injectable too."""
    now = now or dt.datetime.now().astimezone()
    progress = progress or (lambda _m: None)
    notify = notify or _default_notify
    cfg = cfg if cfg is not None else load_remote_config()
    jobs_dir = Path(jobs_dir) if jobs_dir is not None else JOBS_DIR
    params = node_params(cfg)
    mode = node_mode(cfg)
    ttl_s = per_run_vm_ttl_s(cfg)
    cmd_timeout = int(timeout) if timeout else ttl_s
    handle = f"{slug}-remote-{index if index is not None else _next_index(jobs_dir, slug)}"

    base = {"handle": handle, "slug": slug, "cmd": cmd, "repo_url": repo_url,
            "sha": sha, "toolchain": toolchain, "node_mode": mode,
            "started_ts": now.isoformat(timespec="seconds")}

    # Budget veto — don't even start a node when the month is over cap.
    if budget_exhausted(cfg, now, path=budget_path):
        note_budget_alert(cfg, now, path=budget_path, notify=notify, progress=progress)
        result = {**base, "rc": None, "duration_s": 0.0, "junit": aggregate_junit_xml([]),
                  "tail": "", "fell_back_local": True, "cost_usd": 0.0,
                  "reason": "monthly budget exhausted — ran nothing remote"}
        _write_job_files(jobs_dir, handle, result, "")
        progress(f"remote gate: over monthly cap — {cmd!r} falls back to local")
        return result

    started = time.monotonic()
    rc: int | None = None
    log_text = ""
    junit = aggregate_junit_xml([])
    fell_back = False
    error: str | None = None
    try:
        try:
            _node_up(run, mode, params, ttl_s, timeout=_STOP_TIMEOUT_S, progress=progress)
            _remote_prepare(run, params, repo_url, sha, timeout=_SSH_PREP_TIMEOUT_S)
            rc, log_text = _remote_exec(run, params, cmd, timeout=cmd_timeout)
            junit = _harvest_junit(run, params, timeout=_STOP_TIMEOUT_S)
        finally:
            _node_down(run, mode, params, progress)
    except Exception as e:      # noqa: BLE001 — a remote gate can only ever fall
        # back to local, never crash the caller (screen_check's absorb-everything
        # pattern). RemoteGateError (infra non-zero rc), a TimeoutExpired cap
        # breach, an OSError, or any other node fault all land here; the inner
        # finally has already torn the node down.
        fell_back = True
        rc = None
        error = type(e).__name__
        progress(f"remote gate: {cmd!r} could not run remotely ({error}) — local fallback")

    duration = round(time.monotonic() - started, 1)
    cost = round(estimate_cost_usd(duration, params["hourly_rate_usd"]), 6)
    record_cost(cost, now, path=budget_path)
    # This run may have pushed the month over the cap — fire the one alert now so
    # the NEXT decide() sees it disabled and the human already heard once.
    note_budget_alert(cfg, now, path=budget_path, notify=notify, progress=progress)

    result = {**base, "rc": rc, "duration_s": duration, "junit": junit,
              "tail": _tail(log_text), "fell_back_local": fell_back,
              "cost_usd": cost}
    if error:
        result["error"] = error
    _write_job_files(jobs_dir, handle, result, log_text)
    return result


def gate_passed(result: dict) -> bool:
    """Map a :func:`run_remote_gate` result to a boolean gate verdict for Verify.

    PASS iff the command actually ran on the node (NOT a local fallback) with
    ``rc == 0`` and a JUnit aggregate showing zero failures/errors. A fallback, a
    ``None`` rc, a non-zero rc, or any failing/erroring test is NOT a pass — the
    caller then runs the command locally or reports it unverified, exactly as a
    normal gate would. This is the helper mulkko-118's reproduction uses: a prior
    60-min timeout routes the command remote, and an ``rc == 0`` result here is
    what Verify may treat as the passed gate."""
    if not result or result.get("fell_back_local"):
        return False
    if result.get("rc") != 0:
        return False
    junit = result.get("junit") or {}
    try:
        return int(junit.get("failures", 0) or 0) == 0 and int(junit.get("errors", 0) or 0) == 0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# CLI — the stage-facing wrapper (scripts/macp-remote-gate) dispatches to this.
# `run` starts the executor DETACHED (never blocks the stage); `poll`/`result`
# read Worker/jobs/ back; `decide` is read-only. The detached job and this
# executor use DISJOINT file namespaces — jobs bookkeeping at
# Worker/jobs/<handle>.job.{json,rc,log,pid}, the gate result at <handle>.{json,rc,log}.
# ---------------------------------------------------------------------------

def _join_cmd(parts) -> str:
    """The command after the ``--`` separator, re-joined. argparse REMAINDER keeps
    the leading ``--``; drop exactly that one, not any ``--flag`` in the command."""
    parts = list(parts or [])
    if parts and parts[0] == "--":
        parts = parts[1:]
    return " ".join(parts).strip()


def poll_handle(handle: str, *, jobs_dir: Path | str = JOBS_DIR) -> dict:
    """``running | done | failed | absent`` for a remote-gate handle.

    The gate RESULT (``<handle>.json``) is the truth once written — the executor
    writes it on every exit path (success, build-fail, fallback). Until then the
    detached job's own state (``<handle>.job``) reports liveness; a dead job with
    no result is a crash the executor could not record."""
    jobs_dir = Path(jobs_dir)
    result = jobs_dir / f"{handle}.json"
    if result.is_file():
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"state": "unknown", "handle": handle}
        return {"state": "done", "handle": handle, "rc": data.get("rc"),
                "fell_back_local": data.get("fell_back_local"),
                "duration_s": data.get("duration_s"), "junit": data.get("junit")}
    js = jobs.poll(f"{handle}.job")
    st = js.get("state")
    if st == "running":
        return {"state": "running", "handle": handle, "elapsed_s": js.get("elapsed_s")}
    return {"state": "failed" if st == "failed" else "absent", "handle": handle}


def node_script_env(cfg: dict | None, base_env: dict | None = None) -> dict:
    """Environment for ``scripts/app-test-node``: ``base_env`` (default: this
    process's env) plus the node identity from the vault config — ``node`` →
    ``APP_TEST_NODE``, ``zone`` → ``APP_TEST_ZONE`` — when present, and
    ``APP_TEST_PYTHON`` = this interpreter, so the script's ``start``/``stop``
    reach :func:`_main`'s ``node-up``/``node-down`` with the same Python the
    engine runs on. Absent keys leave the script's own defaults in force; the
    real node identity stays in vault data, never a literal here."""
    import sys

    env = dict(os.environ if base_env is None else base_env)
    node = (cfg or {}).get("node")
    zone = (cfg or {}).get("zone")
    if node:
        env["APP_TEST_NODE"] = str(node)
    if zone:
        env["APP_TEST_ZONE"] = str(zone)
    env.setdefault("APP_TEST_PYTHON", sys.executable)
    return env


# `node-up` gets longer than a stop: a create from an image restores the boot
# disk before gcloud returns. Still bounded — the script then waits for ssh.
_NODE_UP_CLI_TIMEOUT_S = 600
# Exit code for "the helper cannot decide" (no vault config / unresolvable vault):
# scripts/app-test-node falls back to its legacy direct start/stop ONLY on this
# (or 127), never on a real gcloud failure.
NODE_CLI_UNAVAILABLE = 3


def _node_cli(action: str, *, run: Runner = _run, cfg: dict | None = None,
              environ: dict | None = None) -> int:
    """``node-up`` / ``node-down`` for ``scripts/app-test-node start|stop``.

    Mode/spec come from the vault's ``.macp/remote_gate.json`` (``node_mode``:
    start-stop → ``instances start|stop``; create-delete → idempotent
    create-from-image / delete). ``APP_TEST_NODE`` / ``APP_TEST_ZONE`` override
    the config's node/zone so the script and this helper always address the
    same instance. rc: 0 ok (down: also already absent), 1 gcloud failure,
    :data:`NODE_CLI_UNAVAILABLE` when there is no config to follow."""
    import sys

    def say(m: str) -> None:
        print(m.replace("remote gate:", "app-test-node:"), file=sys.stderr)

    env = os.environ if environ is None else environ
    cfg = load_remote_config() if cfg is None else cfg
    if not cfg:
        say(f"remote gate: no node config at {REMOTE_CONFIG_PATH} — helper unavailable")
        return NODE_CLI_UNAVAILABLE
    params = node_params(cfg)
    if env.get("APP_TEST_NODE"):
        params["node"] = env["APP_TEST_NODE"]
    if env.get("APP_TEST_ZONE"):
        params["zone"] = env["APP_TEST_ZONE"]
    mode = node_mode(cfg)
    say(f"remote gate: {action} {params['node']} ({mode})")
    if action == "node-up":
        try:
            _node_up(run, mode, params, per_run_vm_ttl_s(cfg),
                     timeout=_NODE_UP_CLI_TIMEOUT_S, progress=say)
        except (RemoteGateError, subprocess.SubprocessError, OSError) as e:
            say(f"remote gate: node up failed ({e})")
            return 1
        return 0
    rc = _node_down(run, mode, params, say)
    return 0 if rc == 0 else 1


def _main(argv: list[str] | None = None) -> int:
    """`macp-remote-gate <decide|run|poll|result>` — see the wrapper's header.
    `node-up` / `node-down` are the node lifecycle for ``scripts/app-test-node``."""
    import argparse
    import shlex
    import sys

    p = argparse.ArgumentParser(prog="macp-remote-gate")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("node-up", help="bring the node up per .macp/remote_gate.json node_mode")
    sub.add_parser("node-down", help="stop/delete the node per node_mode (absent = ok)")

    d = sub.add_parser("decide", help="print remote|local (read-only)")
    d.add_argument("--repo", default=None, help="the TARGET repo checkout (.macp/gates.json)")
    d.add_argument("--task-id", default=None, help="slug whose prior run to check for a timeout")
    d.add_argument("--runs-dir", default=None)
    d.add_argument("cmd", nargs=argparse.REMAINDER)

    for name in ("run", "_execute"):
        s = sub.add_parser(name)
        s.add_argument("--slug", required=True)
        s.add_argument("--repo", required=True)
        s.add_argument("--sha", required=True)
        s.add_argument("--toolchain", default="android")
        s.add_argument("--timeout", type=int, default=None)
        if name == "_execute":
            s.add_argument("--index", type=int, required=True)
        s.add_argument("cmd", nargs=argparse.REMAINDER)

    sub.add_parser("poll").add_argument("handle")
    sub.add_parser("result").add_argument("handle")

    args = p.parse_args(argv)

    if args.action in ("node-up", "node-down"):
        return _node_cli(args.action)

    if args.action == "decide":
        cmd = _join_cmd(args.cmd)
        cfg = load_remote_config()
        prior = bool(args.task_id) and prior_run_timed_out(
            args.task_id, cmd,
            runs_dir=Path(args.runs_dir) if args.runs_dir else RUNS_DIR)
        print(decide(cmd, args.repo, prior_timed_out=prior,
                     budget_exhausted=budget_exhausted(cfg)))
        return 0

    if args.action == "run":
        cmd = _join_cmd(args.cmd)
        n = _next_index(JOBS_DIR, args.slug)
        handle = f"{args.slug}-remote-{n}"
        exec_args = ["_execute", "--slug", args.slug, "--index", str(n),
                     "--repo", args.repo, "--sha", args.sha, "--toolchain", args.toolchain]
        if args.timeout:
            exec_args += ["--timeout", str(args.timeout)]
        exec_args += ["--", cmd]
        inner = " ".join(shlex.quote(a) for a in
                         [sys.executable, "-m", "todo_system.orchestrator.remote_gate", *exec_args])
        jobs.start(f"{handle}.job", inner)       # detached; PYTHONPATH is inherited
        print(handle)                            # return a handle AT ONCE, never block
        return 0

    if args.action == "_execute":                # internal: the detached worker
        run_remote_gate(args.slug, args.repo, args.sha, _join_cmd(args.cmd),
                        toolchain=args.toolchain, timeout=args.timeout, index=args.index)
        return 0

    if args.action == "poll":
        print(json.dumps(poll_handle(args.handle), ensure_ascii=False))
        return 0

    if args.action == "result":
        path = JOBS_DIR / f"{args.handle}.json"
        if not path.is_file():
            print(f"no result for {args.handle} (not finished, or wrong handle)", file=sys.stderr)
            return 1
        print(path.read_text(encoding="utf-8"), end="")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
