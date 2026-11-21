"""Attention triage — why a parked or failed task stopped, and what one click may do.

The dashboard's attention list showed a slug and a 90-character note. The human
pasted the slug into a chat session, which spent 5-15 tool calls finding out
which of a handful of known causes it was before running `cli resume`,
`gh pr merge` or a task-md edit. This module is that investigation made
deterministic: it reads only local files (the ledger row, the task md, the
latest run dir, the registry) and never the network, gh or an LLM, then names
the cause and the one-click actions that fit it. `triage_actions.py` runs those
actions through the existing paths; this module writes nothing.

Triage-row contract (source of truth; macp docs/dashboard-parity.md "Triage
cards" restates it for the Android app):

* `/api/status` adds `triage` to every `attention` row (ledger state failed /
  awaiting_human, deferred rows excluded) and to every `human` row with
  `why == "parked"`. `recent` shares the attention row objects, so it carries
  the key too. Additive only: a client that ignores the key renders as before,
  and a row whose diagnosis failed simply has no `triage` key.
* Shape: `{"rule": str, "reason": str, "evidence": [str], "advice": str,
  "actions": [{"id": str, "label": str}]}`. `rule` is a stable id from RULES
  that tests and clients may key on (never shown). `reason`, `evidence`,
  `advice` and every `label` are sentences in the UI language. `actions` may
  be empty, and `evidence` has at most six lines.
* Action ids are a CLOSED set, ACTIONS = merge | resume | cancel | instruct.
  No id exists for deploying, submitting, publishing or deleting, and the
  server refuses any id outside the set, so an irreversible step never gets a
  button. A task md with `human_gate: true` keeps only `cancel`.
* The server decides; the web card and the app only render what they are given
  (labels included) and post `{task, action[, text]}` to `POST /api/action`.
  The server diagnoses again at click time and runs an action only if that
  fresh diagnosis still offers it, so a stale screen cannot act.
* A click is the approval. There is no confirm dialog.
* Fail-soft: gather() returns None for a missing or corrupt ledger row, and
  the dashboard leaves `triage` off any row whose diagnosis raises.

Rules — first match wins. `can_merge` = pr_ref(pr_url) is not None and
verdict == "PASS" and not rebase_conflict. DISPATCHABLE = auto | delegate |
quick | discover. `alert` is the ledger's `last_alert_kind`.

| # | rule              | when                                                  | actions                                   |
|---|-------------------|-------------------------------------------------------|-------------------------------------------|
| 1 | merged_blocked    | merged_park and blocked_on                            | (none: the exit is closing the md as done) |
| 2 | orphan_redelivery | last_note is the runner's orphan/unknown redelivery   | resume, cancel                            |
| 3 | workflow_scope    | no pr_url, a push refused for lack of workflow scope  | resume, instruct, cancel                  |
| 4 | nonff_push        | no pr_url, a push refused as non-fast-forward         | instruct, resume, cancel                  |
| 5 | remote_no_pr      | kind ai-remote, alert result-no-pr / failure-suspected, no local run dir | resume, instruct, cancel |
| 6 | human_misroute    | assigned to a human, md worker_mode DISPATCHABLE, not a decision | resume, cancel                 |
| 7 | reaped_off        | last history entry reaped-stale, never accepted, md worker_mode not DISPATCHABLE | cancel         |
| 8 | ui_merge_hold     | alert ui-merge-hold (an abolished hold)               | can_merge: merge, cancel / else resume, cancel |
| 9 | device_unverified | blocked_on (ledger or verify.md) names a device/emulator check | can_merge: merge, instruct, cancel / else instruct, resume, cancel |
| 10| review_pr         | alert review-pr, or a known unmerged PR on a parked row | can_merge: merge, instruct, cancel / else instruct, resume, cancel |
| 11| rescope_pending   | alert rescope-pending                                 | cancel                                    |
| 12| timeout_park      | alert timeout-park or timeout_streak >= 2             | instruct, resume, cancel                  |
| 13| blocked           | blocked_on set                                        | resume, instruct, cancel                  |
| 14| result_no_pr      | alert result-no-pr                                    | instruct, cancel                          |
| 15| fallback          | anything else                                         | resume, instruct, cancel                  |

Not a target (diagnose returns None): ledger state outside TARGET_STATES, a
deferred row, or a task md whose status is closed (done / cancelled /
canceled — read from the md, so a just-closed task loses its card at once).

Post-filters, in order: no task md -> no actions; md worker_mode not
DISPATCHABLE -> drop resume and instruct (a resumed row would sit FAILED,
never re-dispatched); a resume already reserved -> drop resume; human_gate ->
keep only cancel, and the advice says to do it by hand.

facts (gather() builds them, diagnose() reads them; tests pass dicts):

| key                | type        | source                                                  |
|--------------------|-------------|---------------------------------------------------------|
| slug               | str         | argument                                                |
| state              | str         | ledger `state` (stored, not the display state)          |
| deferred           | bool        | assignment.is_deferred(row)                             |
| todo_status        | str or None | task md `status`, lowercased                            |
| md_exists          | bool        | task md readable                                        |
| md_worker_mode     | str or None | task md `worker_mode`, else ledger payload              |
| human_gate         | bool        | task md `human_gate` truthy                             |
| needs_decision     | bool        | task md `needs_decision` truthy or a `decision` tag     |
| auto_merge         | bool or None| task md `auto_merge`, else ledger payload               |
| agent, agent_kind  | str         | ledger `agent_id`, `kind`                               |
| agent_is_human     | bool        | kind human, or the registry says the agent is human     |
| human_pattern      | str         | first of that human's `projects` matching the slug      |
| last_note, last_alert_kind, blocked_on | str | ledger                                    |
| timeout_streak, refail_count | int | ledger                                               |
| history_types      | list[str]   | ledger history[].type                                   |
| merged_park        | bool        | dashboard._merged_park (shared, never copied)           |
| resume_reserved    | bool        | resume.record_path(vault, slug) exists                  |
| run                | str         | latest run dir of THIS dispatch, else ""                |
| verdict, verify_blocked_on | str or None | verify.md, else the result artifact              |
| pr_url, merged, recommendation, rebase_conflict | | result artifact (stages.parse_report_*) |
| result_text, log_tail | str      | result artifact (<= 40k chars) / run.log tail (4k)      |

Human text comes from i18n.t() at call time (`triage.*` keys), so the language
follows paths.ui_language() even in the long-lived dashboard; data values
(slugs, notes, blockers, URLs) pass through untranslated. Stdlib-only at
import time, like trace.py.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

from .. import i18n
from ..session_log import parse_frontmatter
from . import assignment, resume, stages
from .dashboard import _merged_park     # the predicate only — shared with trace, never copied
from .statuses import is_closed

# The one-click actions, a closed set (see the contract above). The dashboard
# refuses any other id with a 400 before it diagnoses anything.
ACTIONS = ("merge", "resume", "cancel", "instruct")

# Ledger states that may carry a card: the attention list's own states.
TARGET_STATES = ("awaiting_human", "failed")

# Mirror of tasks.DISPATCHABLE_MODES. Not imported: tasks pulls in repos and the
# orchestrator, and this module must stay as light as trace.py for the dashboard.
# test_triage.FixedValues pins the two together.
_DISPATCHABLE = ("auto", "delegate", "quick", "discover")

# The alert kind the deleted merge_hold.py stamped (deletion D1 removed the
# engine constant with the module). Rows parked under it before the deletion
# still carry it, so the wire string is kept here.
UI_MERGE_HOLD = "ui-merge-hold"

_REVIEW_PR = "review-pr"                  # orchestrator.ALERT_REVIEW_PR
_RESULT_NO_PR = "result-no-pr"            # orchestrator.ALERT_RESULT_NO_PR
_FAILURE_SUSPECTED = "failure-suspected"  # orchestrator.ALERT_FAILURE_SUSPECTED
_TIMEOUT_PARK = "timeout-park"            # orchestrator.ALERT_TIMEOUT_PARK
_RESCOPE_PENDING = "rescope-pending"      # orchestrator.ALERT_RESCOPE_PENDING
_REAPED = "reaped-stale"                  # orchestrator.reap_stale history type
_RAN = ("ACCEPT", "PROGRESS")             # message.ACCEPT / message.PROGRESS

# runner._classify_redelivery's NEEDS_INPUT reason (claims.ORPHAN / UNKNOWN).
_ORPHAN_RE = re.compile(
    r"redelivered envelope, execution already started \((orphan|unknown)\)")

# GitHub refusing a push that touches .github/workflows without the scope
# (OAuth app, personal token or GitHub App wording), or a report saying so.
_WORKFLOW_RE = re.compile(
    r"refusing to allow .{0,80}? to create or update workflow"
    r"|without [`'\"]?workflows?[`'\"]? (?:scope|permission)"
    r"|(?:lacks?|missing|no) (?:the )?[`'\"]?workflow[`'\"]? scope"
    r"|workflow\W{0,2}(?:스코프|권한)\S{0,2}\s?없",
    re.I | re.S)

# A push refused as non-fast-forward: git's own lines, the pre-push hook
# stages.install_push_guard installs, or a report saying so in prose. A bare
# "non-fast-forward" must sit next to push/reject wording — run logs mention it
# in passing (a plan describing a reflog count) and that is not a refusal.
_NONFF_RE = re.compile(
    r"\(non-fast-forward\)|\(fetch first\)|updates were rejected because"
    r"|refusing non-fast-forward push|force-push is a hard rule"
    r"|non-(?:fast-forward|ff\b).{0,80}?(?:push|reject|거부)"
    r"|(?:push|reject|거부).{0,80}?non-(?:fast-forward|ff\b)",
    re.I | re.S)

# A blocker that is only an on-device / emulator check — not a merge condition
# since deletion D1 (decision 2026-11-20 C3).
_DEVICE_RE = re.compile(
    r"에뮬레이터|emulator|on-device|실기기|기기 확인|device check|\badb\b"
    r"|screen[_ ]check|화면 확인",
    re.I)

_PR_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?")

_RULE_EV_MAX = 5      # evidence lines a rule contributes
_EV_MAX = 6           # evidence lines on a card, post-filter lines included
_NOTE_MAX = 140       # chars of a note / blocker quoted on a card


def pr_ref(url) -> tuple[str, int] | None:
    """`("owner/repo", number)` for a github.com pull-request URL, else None."""
    m = _PR_RE.fullmatch(str(url or "").strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}", int(m.group(3))


def _clip(value, limit: int = _NOTE_MAX) -> str:
    """One line, at most `limit` chars."""
    s = " ".join(str(value or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ev(key: str, **fmt) -> str:
    return i18n.t(f"triage.ev.{key}", **fmt)


def _note_line(f: dict) -> str | None:
    note = _clip(f.get("last_note"))
    return _ev("note", note=note) if note else None


def _blocked_line(f: dict, blocker: str | None = None) -> str | None:
    b = _clip(blocker if blocker is not None else f.get("blocked_on"))
    return _ev("blocked_on", blocked_on=b) if b else None


def _pr_line(f: dict) -> str | None:
    return _ev("pr", pr=f["pr_url"]) if f.get("pr_url") else None


def _verify_line(f: dict) -> str | None:
    v = f.get("verdict")
    return _ev("verify", verdict=v, run=f.get("run") or "-") if v else None


def _agent_line(f: dict) -> str | None:
    return _ev("agent", agent=f["agent"]) if f.get("agent") else None


def _push_texts(f: dict) -> str:
    """Where a refused push shows up: the ledger note (a remote runner's FAIL
    reason), the run's result artifact and the run.log tail."""
    return "\n".join(str(f.get(k) or "") for k in ("last_note", "result_text", "log_tail"))


def _can_merge(f: dict) -> bool:
    return (pr_ref(f.get("pr_url")) is not None and f.get("verdict") == "PASS"
            and not f.get("rebase_conflict"))


def _hit(rule: str, actions, evidence, *, reason_key: str | None = None,
         advice_key: str | None = None, **fmt) -> dict:
    return {"rule": rule,
            "reason": i18n.t(reason_key or f"triage.{rule}.reason", **fmt),
            "advice": i18n.t(advice_key or f"triage.{rule}.advice", **fmt),
            "actions": list(actions),
            "evidence": [e for e in evidence if e][:_RULE_EV_MAX]}


# --- the rules, in evaluation order (RULES is derived from this tuple) ---------

def _r_merged_blocked(f: dict) -> dict | None:
    if not (f.get("merged_park") and f.get("blocked_on")):
        return None
    # dashboard._parked_line's exit: close the md. A resume would flip the row to
    # FAILED and send already-merged work back down the retry path.
    return _hit("merged_blocked", (), [_ev("pr_merged"), _blocked_line(f)])


def _r_orphan_redelivery(f: dict) -> dict | None:
    m = _ORPHAN_RE.search(f.get("last_note") or "")
    if not m:
        return None
    return _hit("orphan_redelivery", ("resume", "cancel"),
                [_ev("redelivered", verdict=m.group(1)), _agent_line(f), _note_line(f)])


def _r_workflow_scope(f: dict) -> dict | None:
    if f.get("pr_url") or not _WORKFLOW_RE.search(_push_texts(f)):
        return None
    ev = [_ev("workflow"), _agent_line(f)]
    if not f.get("run"):
        ev.append(_ev("remote_artifacts"))
    ev.append(_note_line(f))
    return _hit("workflow_scope", ("resume", "instruct", "cancel"), ev)


def _r_nonff_push(f: dict) -> dict | None:
    if f.get("pr_url") or not _NONFF_RE.search(_push_texts(f)):
        return None
    return _hit("nonff_push", ("instruct", "resume", "cancel"),
                [_ev("nonff"), _verify_line(f), _note_line(f)], slug=f.get("slug") or "")


def _r_remote_no_pr(f: dict) -> dict | None:
    # The run dir of a remote runner stays on that machine (Worker/runs/ is
    # gitignored), so this host sees the ledger only. The workflow-scope refusal
    # is the usual cause there, which is why the advice names it.
    if not (f.get("agent_kind") == "ai-remote"
            and f.get("last_alert_kind") in (_RESULT_NO_PR, _FAILURE_SUSPECTED)
            and not f.get("run")):
        return None
    return _hit("remote_no_pr", ("resume", "instruct", "cancel"),
                [_agent_line(f), _ev("remote_artifacts"), _ev("workflow"), _note_line(f)],
                agent=f.get("agent") or "?")


def _r_human_misroute(f: dict) -> dict | None:
    if not (f.get("agent_is_human") and f.get("md_worker_mode") in _DISPATCHABLE
            and not f.get("needs_decision")):
        return None
    ev = [_ev("agent_human", agent=f.get("agent") or "?")]
    if f.get("human_pattern"):
        ev.append(_ev("pattern", pattern=f["human_pattern"]))
    ev.append(_ev("worker_mode", mode=f.get("md_worker_mode")))
    return _hit("human_misroute", ("resume", "cancel"), ev)


def _last_history_type(f: dict) -> str:
    """The newest history type that is not the reconcile sweep's bookkeeping."""
    for t in reversed(f.get("history_types") or []):
        if isinstance(t, str) and not t.startswith("reconciled"):
            return t
    return ""


def _r_reaped_off(f: dict) -> dict | None:
    mode = f.get("md_worker_mode")
    types = f.get("history_types") or []
    if (_last_history_type(f) != _REAPED or mode in _DISPATCHABLE
            or any(t in _RAN for t in types)):
        return None
    # runner._skip_reason drops an envelope for an `off` md without a reply, so
    # the row sits dispatched until reap_stale fails it: it never ran.
    return _hit("reaped_off", ("cancel",), [_ev("reaped"), _ev("worker_mode", mode=mode or "-")],
                mode=mode or "-")


def _r_ui_merge_hold(f: dict) -> dict | None:
    if f.get("last_alert_kind") != UI_MERGE_HOLD:
        return None
    ev = [_pr_line(f), _verify_line(f), _note_line(f)]
    if _can_merge(f):
        return _hit("ui_merge_hold", ("merge", "cancel"), ev)
    return _hit("ui_merge_hold", ("resume", "cancel"), ev,
                advice_key="triage.ui_merge_hold.advice_nopr")


def _r_device_unverified(f: dict) -> dict | None:
    blocker = next((b for b in (f.get("blocked_on"), f.get("verify_blocked_on"))
                    if b and _DEVICE_RE.search(str(b))), None)
    if blocker is None:
        return None
    ev = [_blocked_line(f, str(blocker)), _pr_line(f), _verify_line(f)]
    if _can_merge(f):
        return _hit("device_unverified", ("merge", "instruct", "cancel"), ev)
    return _hit("device_unverified", ("instruct", "resume", "cancel"), ev,
                advice_key="triage.device_unverified.advice_nopr")


def _r_review_pr(f: dict) -> dict | None:
    if not (f.get("last_alert_kind") == _REVIEW_PR
            or (f.get("pr_url") and f.get("merged") is not True
                and f.get("state") == "awaiting_human")):
        return None
    ev = [_pr_line(f), _verify_line(f)]
    if f.get("auto_merge") is False:
        ev.append(_ev("auto_merge_off"))
    if f.get("recommendation") == "review":
        ev.append(_ev("review_hold"))
    if f.get("rebase_conflict"):
        ev.append(_ev("rebase_conflict"))
    if _can_merge(f):
        return _hit("review_pr", ("merge", "instruct", "cancel"), ev)
    why = "advice_blocked" if pr_ref(f.get("pr_url")) else "advice_nopr"
    return _hit("review_pr", ("instruct", "resume", "cancel"), ev,
                advice_key=f"triage.review_pr.{why}")


def _r_rescope_pending(f: dict) -> dict | None:
    if f.get("last_alert_kind") != _RESCOPE_PENDING:
        return None
    return _hit("rescope_pending", ("cancel",), [_note_line(f)])


def _r_timeout_park(f: dict) -> dict | None:
    streak = _int(f.get("timeout_streak"))
    if not (f.get("last_alert_kind") == _TIMEOUT_PARK or streak >= 2):
        return None
    n = max(streak, 2)
    return _hit("timeout_park", ("instruct", "resume", "cancel"),
                [_ev("timeouts", n=n), _note_line(f)], n=n)


def _r_blocked(f: dict) -> dict | None:
    if not f.get("blocked_on"):
        return None
    return _hit("blocked", ("resume", "instruct", "cancel"), [_blocked_line(f), _verify_line(f)])


def _r_result_no_pr(f: dict) -> dict | None:
    if f.get("last_alert_kind") != _RESULT_NO_PR:
        return None
    return _hit("result_no_pr", ("instruct", "cancel"), [_note_line(f), _verify_line(f)])


def _r_fallback(f: dict) -> dict:
    note = _clip(f.get("last_note"))
    ev = []
    if _int(f.get("refail_count")):
        ev.append(_ev("refail", n=_int(f.get("refail_count"))))
    if f.get("last_alert_kind"):
        ev.append(_ev("alert", kind=f["last_alert_kind"]))
    ev += [_verify_line(f), _agent_line(f)]
    return _hit("fallback", ("resume", "instruct", "cancel"), ev,
                reason_key="triage.fallback.reason" if note else "triage.fallback.reason_none",
                note=note)


_RULE_FUNCS = (_r_merged_blocked, _r_orphan_redelivery, _r_workflow_scope, _r_nonff_push,
               _r_remote_no_pr, _r_human_misroute, _r_reaped_off, _r_ui_merge_hold,
               _r_device_unverified, _r_review_pr, _r_rescope_pending, _r_timeout_park,
               _r_blocked, _r_result_no_pr, _r_fallback)
RULES = tuple(fn.__name__[len("_r_"):] for fn in _RULE_FUNCS)


def _post_filter(f: dict, hit: dict) -> dict:
    actions = list(hit["actions"])
    advice = hit["advice"]
    post: list[str] = []
    if not f.get("md_exists"):
        actions = []
        post.append(_ev("md_missing"))
    mode = f.get("md_worker_mode")
    if mode not in _DISPATCHABLE and any(a in actions for a in ("resume", "instruct")):
        actions = [a for a in actions if a not in ("resume", "instruct")]
        post.append(_ev("not_dispatchable", mode=mode or "-"))
    if f.get("resume_reserved"):
        actions = [a for a in actions if a != "resume"]
        post.append(_ev("resume_reserved"))
    if f.get("human_gate"):
        actions = [a for a in actions if a == "cancel"]
        post.append(_ev("human_gate"))
        advice = i18n.t("triage.gate.advice")
    room = max(0, _EV_MAX - len(post))
    return {"rule": hit["rule"],
            "reason": hit["reason"],
            "evidence": hit["evidence"][:room] + post,
            "advice": advice,
            "actions": [{"id": a, "label": i18n.t(f"triage.action.{a}")} for a in actions]}


def diagnose(f) -> dict | None:
    """The card for one row's facts, or None when the row is not a target.

    Pure: no I/O and no clock; the only outside read is the UI language for the
    sentences. See the module docstring for the rules and the result shape."""
    if not isinstance(f, dict):
        return None
    if (f.get("state") not in TARGET_STATES or f.get("deferred")
            or is_closed(f.get("todo_status"))):
        return None
    for rule in _RULE_FUNCS:
        hit = rule(f)
        if hit is not None:
            return _post_filter(f, hit)
    return None    # unreachable: _r_fallback always matches


# --- gathering the facts from local files ----------------------------------------

_RUN_NAME_RE = re.compile(r"^(\d{8}T\d{6})-(.+)$")
_RESULT_ARTIFACTS = ("report.md", "quick.md", "delegate.md")   # first present wins
_RESULT_MAX = 40_000      # chars of a result artifact / verify.md read
_LOG_TAIL = 4_000         # chars from the END of run.log
# A run dir named before this dispatch (ledger created_ts) belongs to an earlier
# attempt. A small allowance absorbs the second-level naming.
_RUN_SLACK = dt.timedelta(minutes=2)
_TRUE = ("true", "yes", "1", "on")
_FALSE = ("false", "no", "0", "off")

# (path) -> (mtime_ns, size, parsed value). /api/status recomputes every few
# seconds over files that rarely change, so each is parsed once per change.
# Bounded by a blunt clear; tests call _CACHE.clear().
_CACHE: dict[str, tuple[int, int, object]] = {}
_CACHE_MAX = 512


def _cached(path: Path, load):
    """`load(path)`, re-run only when the file's mtime or size changed. None for
    a missing file. Loaders swallow their own read errors and return None."""
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        _CACHE.pop(key, None)
        return None
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    value = load(path)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (st.st_mtime_ns, st.st_size, value)
    return value


def _load_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_frontmatter(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fm, _ = parse_frontmatter(text)
    return fm


def _load_head(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(_RESULT_MAX)
    except OSError:
        return None


def _load_tail(path: Path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _LOG_TAIL * 4))   # utf-8: <= 4 bytes a char
            data = fh.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")[-_LOG_TAIL:]


def latest_runs(runs_dir) -> dict[str, str]:
    """`{slug: newest run dir name}` from ONE listing of `runs_dir` ({} on error).

    The slug is everything after the `<timestamp>-` prefix, so `…-api-poc` is
    `api-poc`'s run, never `poc`'s (the trap stages.runs_for documents)."""
    out: dict[str, str] = {}
    try:
        names = os.listdir(runs_dir)
    except OSError:
        return out
    for name in names:
        m = _RUN_NAME_RE.match(name)
        if m and name > out.get(m.group(2), ""):
            out[m.group(2)] = name
    return out


def _run_is_current(run_name: str, created_ts) -> bool:
    """False when the run dir was started before this dispatch's ledger row was
    created: its artifacts describe an earlier attempt, not this park.

    A run name is the runner host's local wall clock and `created_ts` carries the
    offset of the host that wrote it; for a local run both are this machine, so
    the name is read in that offset. Unparseable either side -> True (use it)."""
    try:
        started = dt.datetime.strptime(str(run_name)[:15], "%Y%m%dT%H%M%S")
        created = dt.datetime.fromisoformat(str(created_ts))
    except (TypeError, ValueError):
        return True
    if created.tzinfo is not None:
        started = started.replace(tzinfo=created.tzinfo)
    return started >= created - _RUN_SLACK


def _run_facts(slug: str, runs_dir, latest_run, created_ts) -> dict:
    out = {"run": "", "verdict": None, "verify_blocked_on": None, "pr_url": None,
           "merged": None, "recommendation": None, "rebase_conflict": False,
           "result_text": "", "log_tail": ""}
    if latest_run is None:
        runs = stages.runs_for(slug, Path(runs_dir))
        latest_run = runs[-1].name if runs else ""
    if not latest_run or not _run_is_current(latest_run, created_ts):
        return out
    rd = Path(runs_dir) / latest_run
    result = ""
    for name in _RESULT_ARTIFACTS:
        text = _cached(rd / name, _load_head)
        if text is not None:
            result = text
            break
    verify = _cached(rd / stages.STAGE_ARTIFACT["verify"], _load_head)
    judged = verify if verify is not None else result
    out.update({
        "run": latest_run,
        "verdict": stages.parse_verify_verdict(judged),
        "verify_blocked_on": stages.parse_blocked_on(judged),
        "pr_url": stages.parse_report_pr_url(result),
        "merged": stages.parse_report_merged(result),
        "recommendation": stages.parse_report_worker_mode(result),
        "rebase_conflict": stages.parse_report_rebase_conflict(result),
        "result_text": result,
        "log_tail": _cached(rd / "run.log", _load_tail) or "",
    })
    return out


def _flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    s = str(value if value is not None else "").strip().lower()
    return True if s in _TRUE else False if s in _FALSE else None


def _tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value]
    return [t.strip() for t in str(value or "").split(",") if t.strip()]


def _human_owner(registry, agent_id: str, slug: str) -> tuple[bool, str]:
    """(the registry lists `agent_id` as a person, the first of its projects
    that matches `slug` — the glob that sent the task there)."""
    for agent in getattr(registry, "agents", None) or []:
        if getattr(agent, "id", None) != agent_id:
            continue
        if getattr(agent, "kind", "") != "human":
            return False, ""
        pats = [p for p in (getattr(agent, "projects", None) or []) if isinstance(p, str)]
        return True, next((p for p in pats if fnmatch.fnmatch(slug, p)), "")
    return False, ""


def gather(slug: str, *, assign_dir, items_dir, runs_dir, vault_root, registry=None,
           latest_run: str | None = None) -> dict | None:
    """The facts diagnose() reads for one task, from local files only.

    None when the ledger row is missing or not a JSON object. A missing or
    unreadable task md leaves the md facts empty (md_exists False). `registry`
    is any object with `.agents` (registry.Registry); None skips the human
    lookup. `latest_run` is the newest run dir name when the caller listed
    runs_dir already (latest_runs(); "" = none), None = look it up here."""
    row = _cached(Path(assign_dir) / f"{slug}.json", _load_json)
    if not isinstance(row, dict):
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    fm = _cached(Path(items_dir) / f"{slug}.md", _load_frontmatter)
    md = fm if isinstance(fm, dict) else {}

    def md_text(key: str) -> str:
        v = md.get(key)
        return v.strip() if isinstance(v, str) else ""

    mode = (md_text("worker_mode") or str(payload.get("worker_mode") or "")).strip().lower()
    auto_merge = _flag(md.get("auto_merge"))
    if auto_merge is None:
        auto_merge = _flag(payload.get("auto_merge"))
    history = row.get("history") if isinstance(row.get("history"), list) else []
    agent, kind = str(row.get("agent_id") or ""), str(row.get("kind") or "")
    reg_human, pattern = _human_owner(registry, agent, slug)
    facts = {
        "slug": slug,
        "state": str(row.get("state") or ""),
        "deferred": assignment.is_deferred(row),
        "todo_status": md_text("status").lower() or None,
        "md_exists": isinstance(fm, dict),
        "md_worker_mode": mode or None,
        "human_gate": _flag(md.get("human_gate")) is True,
        "needs_decision": (_flag(md.get("needs_decision")) is True
                           or "decision" in _tags(md.get("tags"))),
        "auto_merge": auto_merge,
        "agent": agent,
        "agent_kind": kind,
        "agent_is_human": kind == "human" or reg_human,
        "human_pattern": pattern,
        "last_note": str(row.get("last_note") or ""),
        "last_alert_kind": str(row.get("last_alert_kind") or ""),
        "blocked_on": str(row.get("blocked_on") or ""),
        "timeout_streak": _int(row.get("timeout_streak")),
        "refail_count": _int(row.get("refail_count")),
        "history_types": [str(h.get("type") or "") for h in history if isinstance(h, dict)],
        "merged_park": _merged_park(SimpleNamespace(history=row.get("history"))),
        "resume_reserved": resume.record_path(vault_root, slug).is_file(),
    }
    facts.update(_run_facts(slug, runs_dir, latest_run, row.get("created_ts")))
    return facts


def triage_for(slug: str, **kw) -> dict | None:
    """diagnose(gather(slug, **kw)), or None on any error — a card is optional,
    a broken /api/status is not."""
    try:
        return diagnose(gather(slug, **kw))
    except Exception:  # noqa: BLE001 - fail-soft by contract
        return None
