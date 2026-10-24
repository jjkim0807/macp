"""Opt-in pre-distribute emulator smoke gate for the beta publish lane.

Runs in the orchestrator RUNTIME (the runner / the beta sweep), NOT inside a
``claude -p`` stage — the same split :mod:`beta_publish`, :mod:`screen_check` and
:mod:`remote_gate` already make. ``beta_publish.maybe_publish`` calls
:func:`run_smoke` AFTER the build + tester-count gate and BEFORE ``distribute``,
but only when the repo's config carries a ``smoke`` block. A repo without one is
byte-identical to before (the gate is fully behind ``config.get("smoke")``); that
is the regression-safe contract.

What the gate does on the shared on-demand emulator node ``macp-app-test`` (via
``scripts/app-test-node``): start the node (one preemption retry) → ship a
``git archive`` tarball of the merged sha → build the debug APK ON THE NODE →
boot the AVD → install → (optional) seed a prebuilt DB via ``run-as <pkg> cp``
(debuggable build required) → run a tap/wait/expect scenario parsed from the
repo's ``smoke`` config → scan logcat for ``FATAL EXCEPTION`` / ``ANR`` → pull
screenshots → **always stop the node**. A non-PASS withholds ``distribute`` and
the watermark bump (the merge still stands); the one exception is a monthly-budget
trip, which skips the gate and still distributes (blocking a month of betas on a
cost ceiling is the worse failure — see ``maybe_publish``).

Why the runner and not the stage: the node round-trip needs gcloud/ssh/scp, which
a stage's ``--allowed-tools`` whitelist forbids, and it must run OUTSIDE the
stage's timeout window. Every external effect — ``git archive``, the node
start/push/ssh/boot/pull/stop — flows through the injected ``run=`` callable, so
the unit suite drives a fake and never shells out to gcloud/gradle/an emulator.
The real node round-trip is therefore NOT exercised by this Implement pipeline;
it is a runtime concern bounded by ``cap_s``.

Node identity (name/zone/rate) and cost are NOT hardcoded here. They are read
from the vault's ``.macp/remote_gate.json`` through :mod:`remote_gate`
(``node_params`` / the monthly cost ledger), so engine code stays neutral for the
public-identifier gate and total node spend is capped together with the remote
gate. Personal data (the seed DB, screenshots, uiautomator dumps) lives ONLY in
the gitignored result dir — alerts/Progress Log/PR body carry aggregates only.
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
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from . import lease
from . import remote_gate

# Per-run node-uptime cap: 30 min (task default). Override: env
# ``MACP_BETA_SMOKE_CAP_S`` (test seam) > repo ``smoke.cap_min`` (minutes) >
# this default. Independent of remote_gate's 120-min TTL — a smoke run is a
# minutes-long build+scenario, not a heavy test suite.
_DEFAULT_CAP_S = 1800
_CAP_ENV = "MACP_BETA_SMOKE_CAP_S"
_GIT_TIMEOUT = 60
# One throttled alert per (repo, kind) in this window, mirroring
# remote_gate.note_budget_alert's per-key flag and the beta sweep's alerted_sha.
_ALERT_WINDOW_S = 6 * 3600

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class SmokeError(Exception):
    """A handled node/infra/build failure inside :func:`run_smoke`. Carries the
    ``kind`` (``node`` | ``build``) so the always-stop wrapper can classify it;
    it is absorbed into a result dict and NEVER escapes — a smoke run can only
    ever return a non-PASS, it cannot raise into ``maybe_publish``."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _run(argv: list[str], *, cwd: str | None = None, env: dict | None = None,
         timeout: float | None = None) -> "subprocess.CompletedProcess[str]":
    """Default subprocess runner — the single seam every external effect flows
    through (``git archive``, the node start/push/ssh/boot/pull/stop). Never
    raises on a non-zero exit (callers inspect ``returncode``); a timeout is
    allowed to propagate so the cap can turn it into a forced stop."""
    return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Result shape (pure)
# ---------------------------------------------------------------------------

def _result(ok: bool, kind: str | None, detail: str, *,
            result_dir: Path | str = "", steps: list | None = None,
            duration_s: float = 0.0, cost_usd: float = 0.0,
            screenshots: list | None = None, locked: bool = False,
            logcat: dict | None = None) -> dict:
    """The canonical :func:`run_smoke` result dict. ``kind`` is ``None`` on PASS,
    else one of ``scenario`` | ``crash`` | ``node`` | ``build`` | ``budget``.

    ``locked`` is True ONLY on the lock-acquisition-failure branch (``kind`` stays
    ``"node"`` so ``beta_publish.maybe_publish`` is byte-identical); the emulator
    CLI reads it to return exit 75. ``logcat`` is the aggregate crash scan
    (``fatal`` / ``anr`` / ``max_skipped_frames``) on every exit path that reached
    the logcat scan, else ``{}`` — a stable shape for the paths that failed before
    a device was even reachable. All fields are aggregates only: no seed path,
    screenshot bytes, or dump text (personal-data rule)."""
    return {
        "ok": bool(ok),
        "kind": kind,
        "detail": detail,
        "steps": steps or [],
        "result_dir": str(result_dir),
        "duration_s": round(float(duration_s), 1),
        "cost_usd": round(float(cost_usd), 6),
        "screenshots": screenshots or [],
        "locked": bool(locked),
        "logcat": dict(logcat) if logcat else {},
    }


# ---------------------------------------------------------------------------
# uiautomator dump parsing (pure)
# ---------------------------------------------------------------------------

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True)
class UiNode:
    """One node from a ``uiautomator dump`` tree, with its tap target."""
    text: str = ""
    resource_id: str = ""
    content_desc: str = ""
    bounds: tuple[int, int, int, int] = field(default=(0, 0, 0, 0))

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    m = _BOUNDS_RE.search(raw or "")
    if not m:
        return (0, 0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def parse_ui_dump(xml: str) -> list[UiNode]:
    """Parse a ``uiautomator dump`` XML into a flat list of :class:`UiNode`.

    Malformed / empty XML → ``[]`` (never raises) — a torn dump must not crash
    the scenario; the expectation then simply fails to find its field."""
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError, ValueError):
        return []
    out: list[UiNode] = []
    for el in root.iter("node"):
        out.append(UiNode(
            text=el.get("text", "") or "",
            resource_id=el.get("resource-id", "") or "",
            content_desc=el.get("content-desc", "") or "",
            bounds=_parse_bounds(el.get("bounds", "")),
        ))
    return out


# ---------------------------------------------------------------------------
# Selector resolution (pure) — locators live in the REPO's smoke config, so
# engine code stays app-neutral.
# ---------------------------------------------------------------------------

def _id_matches(resource_id: str, want: str) -> bool:
    """Match an Android ``resource-id`` by its short id (``…:id/<want>``) or a
    full match, so a repo may give either ``timer_text`` or the full id."""
    rid = resource_id or ""
    return rid == want or rid.endswith(f"/{want}")


def _match_node(nodes: list[UiNode], selector: dict | None) -> UiNode | None:
    """First node matching every present criterion of ``selector``
    (``id`` / ``text`` / ``desc`` / ``pattern``). An empty / non-dict selector,
    or one with no criterion, matches nothing."""
    if not isinstance(selector, dict):
        return None
    rid = selector.get("id")
    text = selector.get("text")
    desc = selector.get("desc")
    pattern = selector.get("pattern")
    if rid is None and text is None and desc is None and pattern is None:
        return None
    rx = re.compile(pattern) if pattern else None
    for n in nodes:
        if rid is not None and not _id_matches(n.resource_id, str(rid)):
            continue
        if text is not None and n.text != str(text):
            continue
        if desc is not None and n.content_desc != str(desc):
            continue
        if rx is not None and not rx.search(n.text or ""):
            continue
        return n
    return None


def resolve_field(nodes: list[UiNode], selector: dict | None) -> str | None:
    """The ``text`` of the node matching ``selector``, or ``None`` when absent."""
    n = _match_node(nodes, selector)
    return n.text if n is not None else None


def resolve_tap(nodes: list[UiNode], target: dict | None) -> tuple[int, int] | None:
    """Tap coordinates for ``target``: an explicit ``xy: [x, y]`` wins, else the
    centre of the matched node, else ``None`` (the step cannot be tapped)."""
    if isinstance(target, dict) and target.get("xy"):
        xy = target["xy"]
        try:
            return (int(xy[0]), int(xy[1]))
        except (TypeError, ValueError, IndexError):
            return None
    n = _match_node(nodes, target)
    return n.center if n is not None else None


# ---------------------------------------------------------------------------
# Scenario expectations (pure) — evaluated over a time-series of snapshots
# ---------------------------------------------------------------------------
# A snapshot is ``(t_rel, nodes)``: seconds since the step's tap, and the parsed
# dump at that instant. The caller polls and builds this list; the window
# (``within_s``) is applied HERE so the "changed only after the window" case
# (the 2026-10-22 물꼬 regression (b)) fails deterministically in a pure test.

Snapshot = "tuple[float, list[UiNode]]"
_DEFAULT_WITHIN_S = 10.0
_TIMER_RESET_RE = r"^0:0\d$"


def evaluate_expect(kind: str, params: dict | None,
                    series: "list[tuple[float, list[UiNode]]]") -> tuple[bool, str]:
    """Judge one expectation over a windowed time-series. Returns ``(ok, detail)``.

    - ``text_changes`` — the field's value at the end of the window differs from
      its value at the start. *The 물꼬 fix*: after ⏭ the task title must change;
      a title that stays put (even with the timer reset) is a FAIL.
    - ``text_matches`` — the field matches ``pattern`` (regex) or ``contains``
      (substring), at the window end (``at:"end"``) or any snapshot (default).
    - ``timer_reset`` — the timer field dropped to a near-zero value (default
      ``^0:0\\d$``, override via ``pattern``) at some point in the window.
    """
    params = params or {}
    within = params.get("within_s", _DEFAULT_WITHIN_S)
    try:
        within_f = float(within)
    except (TypeError, ValueError):
        within_f = _DEFAULT_WITHIN_S
    windowed = [(t, nodes) for (t, nodes) in series if t <= within_f]
    if not windowed:
        return False, "창 안에 스냅샷 없음"
    fld = params.get("field")

    if kind == "text_changes":
        start = resolve_field(windowed[0][1], fld)
        end = resolve_field(windowed[-1][1], fld)
        ok = (start != end) and not (start is None and end is None)
        suffix = "" if ok else " (변화 없음)"
        return ok, f"'{start}' → '{end}'{suffix}"

    if kind == "text_matches":
        pattern = params.get("pattern")
        contains = params.get("contains")
        targets = [windowed[-1]] if params.get("at") == "end" else windowed
        for _t, nodes in targets:
            val = resolve_field(nodes, fld) or ""
            if pattern and re.search(pattern, val):
                return True, f"'{val}' ~ /{pattern}/"
            if contains and contains in val:
                return True, f"'{val}' ∋ '{contains}'"
        last = resolve_field(windowed[-1][1], fld)
        return False, f"일치 없음 (field='{last}', pattern={pattern!r} contains={contains!r})"

    if kind == "timer_reset":
        pattern = params.get("pattern", _TIMER_RESET_RE)
        for _t, nodes in windowed:
            val = resolve_field(nodes, fld) or ""
            if re.search(pattern, val):
                return True, f"타이머 리셋 관측 '{val}'"
        last = resolve_field(windowed[-1][1], fld)
        return False, f"타이머 리셋 없음 (field='{last}', /{pattern}/)"

    return False, f"알 수 없는 expect kind: {kind!r}"


# ---------------------------------------------------------------------------
# logcat crash scan (pure)
# ---------------------------------------------------------------------------

def scan_logcat(text: str) -> tuple[bool, str]:
    """``(ok, detail)`` for a ``logcat -d`` dump. FAIL on any ``FATAL EXCEPTION``
    or ``ANR in`` line; the detail is aggregate counts + the first offending
    line (truncated) — never the whole dump (personal-data rule)."""
    lines = (text or "").splitlines()
    fatals = [ln for ln in lines if "FATAL EXCEPTION" in ln]
    anrs = [ln for ln in lines if "ANR in " in ln]
    if fatals or anrs:
        first = (fatals or anrs)[0].strip()[:200]
        return False, f"FATAL={len(fatals)} ANR={len(anrs)}: {first}"
    return True, "no FATAL/ANR"


_SKIPPED_FRAMES_RE = re.compile(r"Skipped (\d+) frames")


def scan_logcat_aggregates(text: str) -> dict:
    """Aggregate crash/jank counts for a ``logcat -d`` dump — AGGREGATES ONLY, so
    the result JSON the worker reports never carries dump text (personal-data
    rule). Uses the SAME predicates as :func:`scan_logcat`
    (``FATAL EXCEPTION`` / ``ANR in ``) and the max of any Choreographer
    ``Skipped N frames`` jank warnings (0 when none). Pure: no device call."""
    lines = (text or "").splitlines()
    fatal = sum(1 for ln in lines if "FATAL EXCEPTION" in ln)
    anr = sum(1 for ln in lines if "ANR in " in ln)
    max_skipped = 0
    for ln in lines:
        m = _SKIPPED_FRAMES_RE.search(ln)
        if m:
            max_skipped = max(max_skipped, int(m.group(1)))
    return {"fatal": fatal, "anr": anr, "max_skipped_frames": max_skipped}


# ---------------------------------------------------------------------------
# Smoke-skip classifier (pure-ish — one git diff via run=) — "once per sweep"
# ---------------------------------------------------------------------------
# Trivial path patterns: a diff touching ONLY these since the last publish needs
# no device smoke (prose / tests / docs / resource strings). The inverse of
# detect_migration's scan: if EVERY changed path is trivial, skip and still
# distribute; any non-trivial path (real src/*.kt, build files) runs the gate.
_TRIVIAL_PATH_RES = (
    re.compile(r".*/src/test/.*"),
    re.compile(r".*/src/androidTest/.*"),
    re.compile(r".*Test\.kt$"),
    re.compile(r".*Test\.java$"),
    re.compile(r".*\.md$"),
    re.compile(r"(?:^|.*/)docs/.*"),
    re.compile(r"(?:^|.*/)LICENSE[^/]*$"),
    re.compile(r".*/res/values[^/]*/.*"),
    re.compile(r".*CareCopy.*"),
)


def _is_trivial_path(path: str) -> bool:
    return any(rx.match(path) for rx in _TRIVIAL_PATH_RES)


def smoke_skippable(repo_dir: Path | str, base: str | None, merge_sha: str, *,
                    run: Runner = _run) -> bool:
    """True iff EVERY path changed since ``base`` is prose/test/doc/resource — so
    the beta sweep smokes a given sha at most once and skips a notes-only bump.

    ``base`` is the last published sha. No base (first publish) or any git
    failure → ``False`` (run the gate — the safe direction for a gate). Reuses
    :func:`beta_publish.ensure_probe_range` so a just-merged sha missing from the
    clone is fetched before the diff (DripCoffee 131's gap), exactly like the
    migration probe."""
    if not base:
        return False
    # Lazy import breaks the cycle: beta_publish imports this module at top for
    # the maybe_publish `smoke_runner` default, so we cannot import it at ours.
    from . import beta_publish
    if not beta_publish.ensure_probe_range(repo_dir, merge_sha, run=run, diff_base=base):
        return False
    r = run(["git", "diff", "--name-only", f"{base}..{merge_sha}"],
            cwd=str(repo_dir), timeout=_GIT_TIMEOUT)
    if getattr(r, "returncode", 1) != 0:
        return False
    changed = [ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()]
    if not changed:
        return False                     # nothing resolved → don't skip the gate
    return all(_is_trivial_path(p) for p in changed)


# ---------------------------------------------------------------------------
# Cap + alert throttle (pure)
# ---------------------------------------------------------------------------

def cap_seconds(smoke_cfg: dict | None) -> int:
    """Per-run node-uptime cap in seconds. Priority: env ``MACP_BETA_SMOKE_CAP_S``
    (test seam) > ``smoke.cap_min`` (minutes) > ``_DEFAULT_CAP_S`` (30 min). A
    non-positive / unparseable value falls through rather than raising (mirrors
    ``screen_check.cap_seconds``)."""
    env_raw = os.environ.get(_CAP_ENV)
    if env_raw is not None:
        try:
            s = int(env_raw)
            if s > 0:
                return s
        except (TypeError, ValueError):
            pass
    raw = (smoke_cfg or {}).get("cap_min")
    if raw is not None:
        try:
            mins = int(raw)
            if mins > 0:
                return mins * 60
        except (TypeError, ValueError):
            pass
    return _DEFAULT_CAP_S


def alert_throttled(state: dict | None, repo: str, kind: str, now: float, *,
                    window: float = _ALERT_WINDOW_S) -> tuple[bool, dict]:
    """``(should_alert, new_state)`` for a per-(repo, kind) throttle.

    Pure, mirroring ``remote_gate.note_budget_alert``'s per-key flag: the gate
    can fail every sweep, but a human should hear about one (repo, failure-kind)
    at most once per ``window``. ``now`` is epoch seconds. The returned state is a
    fresh dict (the input is never mutated)."""
    state = dict(state or {})
    key = f"{repo}:{kind}"
    last = state.get(key)
    try:
        last_f = float(last) if last is not None else None
    except (TypeError, ValueError):
        last_f = None
    if last_f is not None and (now - last_f) < window:
        return False, state
    state[key] = now
    return True, state


# ---------------------------------------------------------------------------
# Node round-trip (every external effect via the injected run=) — always-stop
# ---------------------------------------------------------------------------
# Paths mirror beta_publish / remote_gate's resolver split: config/data follow
# the redirectable vault root; engine files anchor on the engine root; gitignored
# scratch hangs off the engine root in a checkout (immune to a redirected vault),
# the vault in an installed wheel.
_ENGINE_ROOT = paths.engine_root()
_VAULT_DATA_ROOT = paths.vault_root(start=paths.vault_search_start())
_SCRATCH_ROOT = _VAULT_DATA_ROOT if paths.is_installed() else _ENGINE_ROOT
# SAME lock string the runner wires screen_check to (runner.py:728), so beta_smoke
# and screen_check serialize on the ONE physical node macp-app-test.
_NODE_LOCK = _VAULT_DATA_ROOT / "Worker" / "screen-test-node.lock"
_NODE_SCRIPT = _ENGINE_ROOT / "scripts" / "app-test-node"
# Result dir (gitignored HQ-local): seed DB / screenshots / uiautomator dumps live
# ONLY here — never committed/pushed/PR'd. Alerts/Progress Log carry aggregates only.
_RESULT_ROOT = _SCRATCH_ROOT / "Worker" / "_state" / "beta_smoke"
_LOCK_MARGIN = 120
# Teardown gets its OWN fixed timeout, not the (expired) cap deadline, so the VM
# is still stopped after a cap breach — like screen_check / remote_gate.
_STOP_TIMEOUT_S = 180
_DEFAULT_POLL_INTERVAL_S = 1.0
# Node-side paths (conventions on macp-app-test, NOT personal identifiers): the
# emulator SDK and the per-sha build dir live under $HOME, mirroring the node
# script's own `shot` and remote_gate's `$HOME/rg-src`.
_ADB = "~/android/platform-tools/adb"
_AAPT2 = "~/android/build-tools/*/aapt2"
_DEFAULT_GRADLE_TASK = ":app:assembleDebug"


def _remaining(deadline: float) -> float:
    """Seconds left until the cap deadline, floored at 1s so a call always has a
    positive timeout; a breached deadline yields the floor and the next node call
    times out at once, firing the forced-stop path (mirrors screen_check)."""
    return max(1.0, deadline - time.monotonic())


def _node_env(result_dir: Path | str, remote_cfg: dict | None) -> dict:
    """Environment for the node script. ``APP_TEST_OUT`` points screenshots at the
    result dir; node name/zone come from the vault's ``.macp/remote_gate.json``
    (``remote_cfg``) when present, so the real node identity stays in vault data
    and engine code carries no literal. Absent keys leave the script's own
    defaults in force."""
    env = dict(os.environ)
    env["APP_TEST_OUT"] = str(Path(result_dir) / "screens")
    node = (remote_cfg or {}).get("node")
    zone = (remote_cfg or {}).get("zone")
    if node:
        env["APP_TEST_NODE"] = str(node)
    if zone:
        env["APP_TEST_ZONE"] = str(zone)
    return env


def _node(run: Runner, node_script: Path | str, subcmd: str, *args: str,
          env: dict, timeout: float) -> "subprocess.CompletedProcess[str]":
    """One ``scripts/app-test-node <subcmd> [args…]`` invocation via the seam."""
    return run(["bash", str(node_script), subcmd, *args], env=env, timeout=timeout)


def _ssh(run: Runner, node_script: Path | str, command: str, *, env: dict,
         timeout: float) -> "subprocess.CompletedProcess[str]":
    """Run a shell ``command`` on the node (``app-test-node ssh '<cmd>'``)."""
    return _node(run, node_script, "ssh", command, env=env, timeout=timeout)


def _last_line(cp: "subprocess.CompletedProcess[str]") -> str:
    lines = [ln.strip() for ln in (getattr(cp, "stdout", "") or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _stop_node(run: Runner, node_script: Path | str, env: dict,
               progress: Callable[[str], None]) -> None:
    """``app-test-node stop`` behind a never-raise guard (the twin of
    ``screen_check._stop_node``): runs in the inner ``finally`` on EVERY exit
    path with its own fixed timeout, so the billed spot VM is always stopped —
    even after a cap breach. Stopping an un-started/stopped VM is idempotent."""
    try:
        r = _node(run, node_script, "stop", env=env, timeout=_STOP_TIMEOUT_S)
        progress(f"beta-smoke: node stop rc={getattr(r, 'returncode', None)}")
    except Exception as e:      # noqa: BLE001 — stop must not mask the real outcome
        progress(f"beta-smoke: node stop skipped ({type(e).__name__})")


def _default_notify(message: str) -> None:
    """Best-effort kakao alert, imported lazily so this module stays stdlib-only
    AT IMPORT (``check_engine_isolation.sh`` archives and imports the tree)."""
    try:
        from .. import worker
        worker.kakao_notify(message)
    except Exception:       # noqa: BLE001 — notification is best-effort
        pass


def _step(name: str, ok: bool, duration_s: float, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "duration_s": round(float(duration_s), 1),
            "detail": detail}


def _poll_interval(smoke_cfg: dict) -> float:
    """Seconds between uiautomator polls (default 1s). ``smoke.poll_interval_s``
    overrides it; a non-positive value (the unit suite pins 0) means "poll twice,
    back to back" so tests never sleep."""
    raw = (smoke_cfg or {}).get("poll_interval_s", _DEFAULT_POLL_INTERVAL_S)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_POLL_INTERVAL_S
    return v if v > 0 else 0.0


def _resolve_selector(sel, fields: dict) -> dict | None:
    """A step/expect locator is either a NAME into the repo's ``smoke.fields`` map
    or an inline selector dict. Keeping named locators in vault config is what lets
    engine code stay app-neutral."""
    if isinstance(sel, str):
        f = (fields or {}).get(sel)
        return f if isinstance(f, dict) else None
    return sel if isinstance(sel, dict) else None


def _node_dump(run: Runner, node_script: Path | str, env: dict, deadline: float) -> list:
    """One fresh uiautomator dump, parsed. A dump failure → ``[]`` (the scenario
    then fails to find its field, which the expectation reports)."""
    cmd = (f"{_ADB} shell uiautomator dump /sdcard/mk-ui.xml >/dev/null 2>&1; "
           f"{_ADB} shell cat /sdcard/mk-ui.xml")
    r = _ssh(run, node_script, cmd, env=env, timeout=_remaining(deadline))
    return parse_ui_dump(getattr(r, "stdout", "") or "")


def _poll_series(run: Runner, node_script: Path | str, env: dict, deadline: float, *,
                 within_s: float, interval: float) -> "list[tuple[float, list[UiNode]]]":
    """Collect ``(t_rel, nodes)`` snapshots up to the ``within_s`` window. Stamps
    are synthetic (``i*interval``) so the window semantics are deterministic
    regardless of real ssh latency, and ``evaluate_expect`` can be unit-tested on
    a canned sequence. ``interval<=0`` (tests) → two back-to-back samples."""
    series: "list[tuple[float, list[UiNode]]]" = []
    n = max(1, int(round(within_s / interval))) if interval > 0 else 1
    for i in range(n + 1):
        series.append((i * interval, _node_dump(run, node_script, env, deadline)))
        if i * interval >= within_s:
            break
        if interval > 0:
            time.sleep(min(interval, _remaining(deadline)))
    return series


def _archive_and_push(run: Runner, node_script: Path | str, repo_dir: Path | str,
                      sha: str, result_dir: Path, env: dict, deadline: float) -> str:
    """``git archive`` the merged sha locally, ship the tarball to the node, and
    return the node-side tarball path. Infra failure → ``SmokeError("node")``."""
    tar = result_dir / "src.tar.gz"
    # `git archive -o` does not create parent dirs: without this the first smoke of
    # every new sha failed with rc=128 (found by the first real round-trip, 10-24).
    Path(result_dir).mkdir(parents=True, exist_ok=True)
    a = run(["git", "archive", "--format=tar.gz", "-o", str(tar), sha],
            cwd=str(repo_dir), timeout=_remaining(deadline))
    if getattr(a, "returncode", 1) != 0:
        raise SmokeError("node", f"git archive 실패 (rc={getattr(a, 'returncode', None)})")
    remote_tar = "~/mk-smoke-src.tar.gz"
    p = _node(run, node_script, "push", str(tar), remote_tar, env=env, timeout=_remaining(deadline))
    if getattr(p, "returncode", 1) != 0:
        raise SmokeError("node", f"tar push 실패 (rc={getattr(p, 'returncode', None)})")
    return remote_tar


def _save_build_log(env: dict, cp, keep: int = 80) -> None:
    """Keep the last ``keep`` lines of a failed node build in ``<result_dir>/build.log``
    (beside screens/, never in the result JSON) so a red build can be diagnosed
    without another paid node round-trip. Best-effort."""
    try:
        out = Path(env.get("APP_TEST_OUT", ".")).parent / "build.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        text = ((getattr(cp, "stdout", "") or "") + "\n" + (getattr(cp, "stderr", "") or ""))
        out.write_text("\n".join(text.splitlines()[-keep:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _build_debug_apk(run: Runner, node_script: Path | str, sha: str, remote_tar: str,
                     smoke_cfg: dict, env: dict, deadline: float) -> str:
    """Extract the tarball into a per-sha dir on the node and build the debug APK.
    Returns the node-side APK path (echoed on the build's last line). A build
    rc≠0 → ``SmokeError("build")``; a build that yields no APK → also build."""
    task = str(smoke_cfg.get("gradle_task") or _DEFAULT_GRADLE_TASK)
    bdir = f"$HOME/mk-smoke-{sha[:12]}"
    script = (
        "set -e; "
        f'd="{bdir}"; mkdir -p "$d"; '
        # Unquoted on purpose: remote_tar starts with `~/`, which the node shell
        # expands only outside quotes (quoted, tar looked for a literal "~" dir).
        f'tar -xzf {remote_tar} -C "$d"; cd "$d"; '
        'printf "sdk.dir=%s\\n" "$HOME/android" > local.properties; '
        f'./gradlew {task} --no-daemon; '
        "find \"$d\" -path '*/build/outputs/apk/*' -name '*debug*.apk' | head -1")
    b = _ssh(run, node_script, script, env=env, timeout=_remaining(deadline))
    if getattr(b, "returncode", 1) != 0:
        _save_build_log(env, b)
        raise SmokeError("build", f"debug 빌드 실패 (rc={getattr(b, 'returncode', None)}) — build.log")
    apk = _last_line(b)
    if not apk.endswith(".apk"):
        raise SmokeError("build", "debug APK 산출물 없음")
    return apk


def _install_and_launch(run: Runner, node_script: Path | str, apk: str,
                        smoke_cfg: dict, env: dict, deadline: float) -> str:
    """Install the APK, launch it, and return the resolved package name. The
    package is ``smoke.package`` when given, else detected with aapt2. An install
    failure → ``SmokeError("node")``."""
    pkg = str(smoke_cfg.get("package") or "").strip()
    pkg_expr = f'"{pkg}"' if pkg else f'$({_AAPT2} dump packagename "{apk}" | head -1)'
    script = (
        "set -e; "
        f'pkg={pkg_expr}; '
        f'{_ADB} install -r "{apk}" >/dev/null; '
        f'{_ADB} shell monkey -p "$pkg" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; '
        'echo "$pkg"')
    r = _ssh(run, node_script, script, env=env, timeout=_remaining(deadline))
    if getattr(r, "returncode", 1) != 0:
        raise SmokeError("node", f"설치/실행 실패 (rc={getattr(r, 'returncode', None)})")
    return _last_line(r) or pkg


def _seed_db(run: Runner, node_script: Path | str, pkg: str, smoke_cfg: dict,
             result_dir: Path, env: dict, deadline: float) -> None:
    """Optional: push a prebuilt DB and ``run-as <pkg> cp`` it into the app's
    databases dir (needs a debuggable build). ``smoke.seed`` is
    ``{"source": <HQ path>, "db_name": <file>}``; absent → no-op. A seed failure →
    ``SmokeError("node")`` (a half-seeded app would smoke-test the wrong state).

    The full JSON-backup→DB transform (id-preserving placeholder names) is a
    deferred follow-up; this first cut takes a prebuilt SQLite file."""
    seed = smoke_cfg.get("seed")
    if not isinstance(seed, dict) or not seed.get("source"):
        return
    src = str(seed["source"])
    db_name = str(seed.get("db_name") or "app.db")
    remote_db = "~/mk-smoke-seed.db"
    p = _node(run, node_script, "push", src, remote_db, env=env, timeout=_remaining(deadline))
    if getattr(p, "returncode", 1) != 0:
        raise SmokeError("node", f"시드 DB 전송 실패 (rc={getattr(p, 'returncode', None)})")
    script = (f'{_ADB} shell "run-as {pkg} cp {remote_db} '
              f'/data/data/{pkg}/databases/{db_name}"')
    r = _ssh(run, node_script, script, env=env, timeout=_remaining(deadline))
    if getattr(r, "returncode", 1) != 0:
        raise SmokeError("node", f"시드 DB 주입 실패 (rc={getattr(r, 'returncode', None)})")


def _run_scenario(run: Runner, node_script: Path | str, smoke_cfg: dict, env: dict,
                  deadline: float, steps_out: list) -> tuple[bool, str | None, str]:
    """Run the tap/wait/expect scenario. Returns ``(ok, kind, detail)``; a failed
    expectation or an untappable target → ``(False, "scenario", …)``. Each step's
    per-step result (name/ok/duration/detail) is appended to ``steps_out``."""
    fields = smoke_cfg.get("fields") or {}
    scenario = smoke_cfg.get("scenario") or []
    interval = _poll_interval(smoke_cfg)
    for idx, step in enumerate(scenario):
        name = str(step.get("name") or f"step {idx + 1}")
        t0 = time.monotonic()
        tap_target = _resolve_selector(step.get("tap"), fields)
        if tap_target is not None:
            xy = resolve_tap(_node_dump(run, node_script, env, deadline), tap_target)
            if xy is None:
                steps_out.append(_step(name, False, time.monotonic() - t0,
                                       f"탭 대상 못 찾음: {step.get('tap')!r}"))
                return False, "scenario", f"{name}: 탭 대상 못 찾음"
            _ssh(run, node_script, f"{_ADB} shell input tap {xy[0]} {xy[1]}",
                 env=env, timeout=_remaining(deadline))
        wait_s = step.get("wait_s")
        if wait_s:
            try:
                time.sleep(min(float(wait_s), _remaining(deadline)))
            except (TypeError, ValueError):
                pass
        expect = step.get("expect")
        if not expect:
            steps_out.append(_step(name, True, time.monotonic() - t0, "탭 완료"))
            continue
        within = expect.get("within_s", _DEFAULT_WITHIN_S)
        try:
            within_f = float(within)
        except (TypeError, ValueError):
            within_f = _DEFAULT_WITHIN_S
        series = _poll_series(run, node_script, env, deadline,
                              within_s=within_f, interval=interval)
        params = {**expect, "field": _resolve_selector(expect.get("field"), fields)}
        ok, detail = evaluate_expect(str(expect.get("kind")), params, series)
        steps_out.append(_step(name, ok, time.monotonic() - t0, detail))
        if not ok:
            return False, "scenario", f"{name}: {detail}"
    return True, None, "scenario PASS"


def _scan_node_logcat(run: Runner, node_script: Path | str, env: dict,
                      deadline: float) -> tuple[bool, str, dict]:
    """Pull ``logcat -d`` from the node and scan for FATAL/ANR (aggregates only).
    Returns ``(ok, detail, aggregates)`` where ``aggregates`` is the
    :func:`scan_logcat_aggregates` dict for the SAME fetched text (``{}`` when no
    logcat could be fetched). A logcat fetch failure is treated as clean
    (``True``) — the scenario verdict is the primary signal, and a torn logcat
    must not fail a passing run."""
    try:
        r = _ssh(run, node_script, f"{_ADB} logcat -d", env=env, timeout=_remaining(deadline))
    except subprocess.TimeoutExpired:
        raise
    except Exception:       # noqa: BLE001 — a logcat fetch fault is not a crash verdict
        return True, "logcat 수집 실패 — 생략", {}
    if getattr(r, "returncode", 1) != 0:
        return True, "logcat 수집 실패 — 생략", {}
    text = getattr(r, "stdout", "") or ""
    ok, detail = scan_logcat(text)
    return ok, detail, scan_logcat_aggregates(text)


def _pull_screens(run: Runner, node_script: Path | str, result_dir: Path, env: dict,
                  deadline: float) -> list[str]:
    """Best-effort: capture one screencap and pull it into the result dir. Returns
    the local screenshot paths (never raises — a missing screenshot is not a
    gate failure)."""
    screens = result_dir / "screens"
    try:
        screens.mkdir(parents=True, exist_ok=True)
        _ssh(run, node_script, f"{_ADB} exec-out screencap -p > ~/mk-smoke-shot.png",
             env=env, timeout=_remaining(deadline))
        dst = screens / "final.png"
        _node(run, node_script, "pull", "~/mk-smoke-shot.png", str(dst),
              env=env, timeout=_remaining(deadline))
    except Exception:       # noqa: BLE001 — screenshots are evidence, not a verdict
        pass
    return [str(p) for p in sorted(screens.glob("*.png"))] if screens.is_dir() else []


def _start_node(run: Runner, node_script: Path | str, env: dict, deadline: float) -> None:
    """``start`` with one preemption retry (the node is a spot VM). Still failing
    after one retry → ``SmokeError("node")``."""
    r = _node(run, node_script, "start", env=env, timeout=_remaining(deadline))
    if getattr(r, "returncode", 1) == 0:
        return
    r = _node(run, node_script, "start", env=env, timeout=_remaining(deadline))
    if getattr(r, "returncode", 1) != 0:
        raise SmokeError("node", f"노드 기동 실패 (rc={getattr(r, 'returncode', None)})")


def _boot(run: Runner, node_script: Path | str, smoke_cfg: dict, env: dict,
          deadline: float) -> None:
    """Boot the AVD (``app-test-node boot [avd]``). ``smoke.avd`` overrides the
    script's default. rc≠0 → ``SmokeError("node")``."""
    avd = str(smoke_cfg.get("avd") or "").strip()
    args = [avd] if avd else []
    r = _node(run, node_script, "boot", *args, env=env, timeout=_remaining(deadline))
    if getattr(r, "returncode", 1) != 0:
        raise SmokeError("node", f"AVD 부팅 실패 (rc={getattr(r, 'returncode', None)})")


def _run_locked(repo_name: str, repo_dir: Path | str, sha: str, smoke_cfg: dict, *,
                cap_s: int, result_dir: Path, remote_cfg: dict | None,
                node_script: Path | str, run: Runner,
                progress: Callable[[str], None]) -> dict:
    """The node round-trip, assuming the lock is held. Returns a result dict and
    NEVER raises — every failure is absorbed into ``kind``. The whole body is the
    outer try; the inner ``finally`` stops the node UNCONDITIONALLY (even a build
    failure stops, since stopping an un-started VM is idempotent)."""
    deadline = time.monotonic() + cap_s
    env = _node_env(result_dir, remote_cfg)
    steps: list = []
    screenshots: list[str] = []
    try:
        try:
            _start_node(run, node_script, env, deadline)
            remote_tar = _archive_and_push(run, node_script, repo_dir, sha, result_dir, env, deadline)
            apk = _build_debug_apk(run, node_script, sha, remote_tar, smoke_cfg, env, deadline)
            _boot(run, node_script, smoke_cfg, env, deadline)
            pkg = _install_and_launch(run, node_script, apk, smoke_cfg, env, deadline)
            _seed_db(run, node_script, pkg, smoke_cfg, result_dir, env, deadline)
            sc_ok, sc_kind, sc_detail = _run_scenario(run, node_script, smoke_cfg, env, deadline, steps)
            lc_ok, lc_detail, lc_agg = _scan_node_logcat(run, node_script, env, deadline)
            screenshots = _pull_screens(run, node_script, result_dir, env, deadline)
        finally:
            _stop_node(run, node_script, env, progress)
    except SmokeError as e:
        return _result(False, e.kind, e.detail, result_dir=result_dir, steps=steps, screenshots=screenshots)
    except subprocess.TimeoutExpired:
        return _result(False, "node", f"상한 {cap_s // 60}분 초과 — 강제 stop",
                       result_dir=result_dir, steps=steps, screenshots=screenshots)
    except Exception as e:      # noqa: BLE001 — a smoke run can only ever return non-PASS
        return _result(False, "node", f"노드 오류: {type(e).__name__}",
                       result_dir=result_dir, steps=steps, screenshots=screenshots)
    if not sc_ok:
        return _result(False, sc_kind, sc_detail, result_dir=result_dir, steps=steps,
                       screenshots=screenshots, logcat=lc_agg)
    if not lc_ok:
        return _result(False, "crash", lc_detail, result_dir=result_dir, steps=steps,
                       screenshots=screenshots, logcat=lc_agg)
    return _result(True, None, "smoke PASS", result_dir=result_dir, steps=steps,
                   screenshots=screenshots, logcat=lc_agg)


def _write_result(result_dir: Path, result: dict) -> None:
    """Write ``result.json`` (aggregates only) beside the screenshots/dumps."""
    try:
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def run_smoke(repo_name: str, repo_dir: Path | str, sha: str, config: dict, *,
              run: Runner = _run, node_script: Path | str = _NODE_SCRIPT,
              lock_path: Path | str = _NODE_LOCK, result_root: Path | str = _RESULT_ROOT,
              remote_cfg: dict | None = None, budget_path: Path | str = remote_gate.BUDGET_PATH,
              notify: Callable[[str], None] | None = None,
              progress: Callable[[str], None] = lambda _m: None,
              cap_s: int | None = None, now: "dt.datetime | None" = None,
              lock_holder: str | None = None) -> dict:
    """Run the emulator smoke gate for ``sha`` and return the result dict. NEVER
    raises — a non-PASS is a result, not an exception, so ``maybe_publish`` treats
    a failure as a handled abort (deploy withheld, merge stands).

    Serialized with ``screen_check`` on the one physical node via the shared
    ``lock_path`` lease; failing to take it is ``kind="node"`` ("node busy") with
    ZERO node calls and no alert (``maybe_publish`` withholds quietly, retries next
    sweep). Over the reused remote-gate monthly budget it is ``kind="budget"`` with
    ZERO node calls and one throttled monthly alert — the caller then STILL
    distributes (a cost ceiling must not block a month of betas). Every other exit
    stops the node in an inner ``finally`` (AC1) and records the VM-seconds cost in
    the shared ledger. Every external effect flows through ``run=``.

    ``lock_holder`` overrides the lease holder id (default ``<host>:<pid>``): the
    beta sweep passes nothing and keeps that default, while the worker emulator
    CLI passes its run_id so a stage's take of the SAME physical node lock is
    attributable. The holder never changes WHICH file is locked, so the beta
    sweep and a worker run still serialize on the one node and can never
    co-drive it."""
    now = now or dt.datetime.now().astimezone()
    notify = notify or _default_notify
    remote_cfg = remote_cfg if remote_cfg is not None else remote_gate.load_remote_config()
    params = remote_gate.node_params(remote_cfg)
    cap = cap_s if cap_s is not None else cap_seconds(config)
    result_dir = Path(result_root) / f"{repo_name}-{sha[:12]}"

    lock = lease.Lease(str(lock_path), holder=lock_holder, ttl=cap + _LOCK_MARGIN)
    if not lock.acquire():
        return _result(False, "node", "노드 사용 중 — 락 획득 실패",
                       result_dir=result_dir, locked=True)
    try:
        # Budget veto — don't even start a node when the month is over cap.
        if remote_gate.budget_exhausted(remote_cfg, now, path=budget_path):
            remote_gate.note_budget_alert(remote_cfg, now, path=budget_path,
                                          notify=notify, progress=progress)
            res = _result(False, "budget", "월 비용 상한 초과 — 스모크 생략", result_dir=result_dir)
            _write_result(result_dir, res)
            progress(f"beta-smoke: over monthly cap — {repo_name} smoke skipped")
            return res
        started = time.monotonic()
        res = _run_locked(repo_name, repo_dir, sha, config, cap_s=cap, result_dir=result_dir,
                          remote_cfg=remote_cfg, node_script=node_script, run=run,
                          progress=progress)
        duration = round(time.monotonic() - started, 1)
        cost = round(remote_gate.estimate_cost_usd(duration, params["hourly_rate_usd"]), 6)
        remote_gate.record_cost(cost, now, path=budget_path)
        # This run may have crossed the monthly cap — fire the one alert now so the
        # NEXT run sees it disabled and the human already heard once.
        remote_gate.note_budget_alert(remote_cfg, now, path=budget_path,
                                      notify=notify, progress=progress)
        res["duration_s"] = duration
        res["cost_usd"] = cost
        _write_result(result_dir, res)
        return res
    finally:
        lock.release()
