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

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
            screenshots: list | None = None) -> dict:
    """The canonical :func:`run_smoke` result dict. ``kind`` is ``None`` on PASS,
    else one of ``scenario`` | ``crash`` | ``node`` | ``build`` | ``budget``."""
    return {
        "ok": bool(ok),
        "kind": kind,
        "detail": detail,
        "steps": steps or [],
        "result_dir": str(result_dir),
        "duration_s": round(float(duration_s), 1),
        "cost_usd": round(float(cost_usd), 6),
        "screenshots": screenshots or [],
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
