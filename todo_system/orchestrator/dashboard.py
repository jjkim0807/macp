"""Orchestrator dashboard — a human-readable view of who is doing what.

Renders the Assignment Ledger + registry into markdown (Todo/Orchestrator.md,
regenerated like NOW.md/FOCUS.md): what's in flight, what's parked awaiting a
human, what finished or failed, and live-session watch pointers. Stdlib-only.
"""

from __future__ import annotations

from . import assignment as A
from . import message as m
from .assignment import Ledger
from .registry import Registry

DEFAULT_OUT = "Todo/Orchestrator.md"

_ACTIVE = (A.DISPATCHED, A.ACCEPTED, A.IN_PROGRESS)

# The history stamp orchestrator.reconcile_merged leaves on a merged row it parks
# on its blocker instead of closing. Same literal as there; MergedButBlockedTest
# renders a row the real sweep parked, which is what keeps the two in step.
_MERGED_BUT_BLOCKED = "merged-but-blocked"


def _line(a) -> str:
    watch = a.payload.get("watch_url") or a.payload.get("attach")
    tail = f" — watch: {watch}" if watch else ""
    return f"- `{a.task_id}` → **{a.agent_id}**{tail}"


def _fail_line(a) -> str:
    """Failed/stalled task with WHY + how many times + where to dig — so
    diagnosing doesn't require opening run_dirs/verify.md by hand."""
    why = f" — {a.last_note}" if a.last_note else ""
    tries = f" (재실패 {a.refail_count})" if getattr(a, "refail_count", 0) else ""
    rd = a.payload.get("run_dir")
    where = f"\n    ↳ run_dir: `{rd}`" if rd else ""
    return f"- `{a.task_id}` → **{a.agent_id}**{tries}{why}{where}"


def _deferred_line(a) -> str:
    """A FAILED-but-waiting row (checkout_busy / job_running / scheduled resume): what it is waiting
    on, no retry count — a deferral never counts against the task."""
    if getattr(a, "job_running", False):
        what = "long_job 실행 중"
    elif getattr(a, "checkout_busy", False):
        what = "체크아웃 대기"
    else:
        what = "예약 재개 — 재디스패치 대기"
    why = f" — {a.last_note}" if a.last_note else ""
    return f"- `{a.task_id}` → **{a.agent_id}** ({what}){why}"


def _merged_park(a) -> bool:
    """True when reconcile_merged parked this row: its PR merged, a human step is
    still owed. `history` from an older build or a hand edit can hold anything,
    so only dict entries of a real list are read; anything else is 'not merged'.
    trace's _ledger_section imports this definition: share it, never copy it, or
    `macp trace` and this dashboard can name different exits for one row."""
    history = getattr(a, "history", None)
    if not isinstance(history, list):
        return False
    return any(isinstance(h, dict) and h.get("type") == _MERGED_BUT_BLOCKED
               for h in history)


def _parked_line(a) -> str:
    """Parked task, plus — when it is parked on an external blocker — the blocker
    itself and the one edit that releases it.

    "nudge #3" alone says a human was pinged, never what they are supposed to do,
    so a task parked on something already fixed (an API key that got set, a
    console toggle someone flipped) just kept nudging. Naming `blocked_on` and the
    release recipe makes this section actionable: the recipe is exactly the signal
    reconcile_unparked() watches for, so following it re-queues the task by itself
    — no ledger surgery. Rows with no blocker (PR review, NEEDS_INPUT) render
    unchanged; there is no self-service exit for those and inventing one would be
    a lie.

    A row whose PR already merged (reconcile_merged parked it on the blocker
    instead of closing it) gets a different exit. The recipe is wrong there:
    reconcile_unparked would flip the row to FAILED and clear the blocker, sending
    work that already merged back down the retry path. Its exit is the md — once
    the human step is done, closing the task md as `status: done` lets
    reconcile_merged's "todo already closed" path end it on the next tick.

    getattr-only, no I/O and no clock beyond the render's own — this runs inside
    the 5-minute tick, where an exception would take the whole dashboard down.
    """
    head = f"- `{a.task_id}` → **{a.agent_id}** (nudge #{a.nudge_index})"
    blocker = getattr(a, "blocked_on", "")
    if not blocker:
        return head
    line = f"{head}\n    ↳ ⛔ blocked_on: {blocker}"
    if _merged_park(a):
        return (f"{line}\n    ↳ 종결: PR은 이미 머지됨 — 남은 사람 단계를 마친 뒤 "
                f"태스크 md를 `status: done`으로 닫으면 다음 틱에 자동 종결 (재큐 불필요)")
    return (f"{line}"
            f"\n    ↳ 해제: 블로커를 해소한 뒤 태스크 md의 `worker_mode`를 "
            f"`blocked` 등 비-디스패치 값으로 바꿨다가 `auto`로 되돌리면 자동 재큐 "
            f"(orchestrator가 그 전이를 해제 신호로 읽는다)")


def _liveness(ledger: Ledger) -> dict:
    """Read the orchestrator's agent-seen state (best-effort) for the dashboard."""
    import json
    from datetime import datetime
    try:
        seen = json.loads((ledger.root.parent / "_state" / "agent_seen.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return {}
    now = datetime.now().timestamp()
    out = {}
    for aid, ts in seen.items():
        try:
            age = now - datetime.fromisoformat(ts).timestamp()
        except Exception:
            continue
        out[aid] = ("🟢 online" if age < 900 else "🟡 stale" if age < 2700 else "🔴 dead")
    return out


def _stale_section(db_path) -> list[str]:
    """Bullets for the stale-human-task sweep (todo.db), or why there are none.

    `db_path=None` means the caller wired no task DB — embedders and the
    existing dashboard tests call `render()` with two args. A default of
    "Todo/todo.db" here would make every such no-arg render read the real vault
    DB, so the default stays None and the CLI passes the path explicitly.

    Fail-soft, because a missing or corrupt DB must not take the 5-minute tick
    down with it — but *loud*: an error swallowed into "없음" would look exactly
    like "nothing is stale", which is the one claim this section exists to test.
    """
    if db_path is None:
        return ["- 없음"]
    try:
        from . import stale
        return stale.render_section(stale.find_stale(db_path))
    except Exception as exc:
        return [f"- ⚠ 스윕 실패 — {type(exc).__name__}: {exc}"]


def _decisions_section(decisions_dir, items_dir) -> list[str]:
    """Bullets for the decision action-item sweep, or why there are none.

    Same shape and the same reasoning as `_stale_section`: `None` means the
    caller wired no paths (embedders and the older dashboard tests call
    `render()` with fewer args), and a hardcoded "Decisions" default here would
    make every such render read the real vault. Fail-soft so a malformed
    decision file cannot take the 5-minute tick down, but loud — an error
    swallowed into "없음" would read as "every decision is accounted for", which
    is the one claim this section exists to test.
    """
    if decisions_dir is None or items_dir is None:
        return ["- 없음"]
    try:
        from . import decisions
        return decisions.render_section(
            decisions.find_action_item_issues(decisions_dir, items_dir))
    except Exception as exc:
        return [f"- ⚠ 스윕 실패 — {type(exc).__name__}: {exc}"]


def _divergence_section(ledger: Ledger) -> list[str]:
    """origin/main vs local main drift, from the host-local state that
    `_check_divergence` writes each auto tick (Worker/_state/divergence.json).

    Same fail-soft-but-loud posture as `_stale_section`: this renders inside the
    5-minute tick, so a missing file is a plain "- 없음" and a corrupt one is a
    visible error line — never an exception that takes the whole dashboard down.
    When the consecutive-behind streak has crossed its threshold the line is
    promoted to ⚠, because a persistent behind means the pull is failing and a
    human needs to look.
    """
    import json
    path = ledger.root.parent / "_state" / "divergence.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["- 없음"]
    except Exception as exc:
        return [f"- ⚠ 스윕 실패 — {type(exc).__name__}: {exc}"]
    ahead, behind = d.get("ahead", 0), d.get("behind", 0)
    streak = d.get("consecutive_behind", 0)
    if d.get("alert"):
        return [f"- ⚠ origin/main과 갈라짐 — ahead {ahead} · behind {behind} "
                f"(연속 {streak}틱 ≥ 임계값 {d.get('threshold', '?')}): "
                f"pull이 계속 실패하는지 확인"]
    return [f"- origin/main 대비 ahead {ahead} · behind {behind} (연속 {streak}틱)"]


def render(ledger: Ledger, registry: Registry, db_path=None,
           decisions_dir=None, items_dir=None) -> str:
    items = ledger.all()
    live = _liveness(ledger)
    by = lambda *states: [a for a in items if a.state in states]  # noqa: E731

    active = by(*_ACTIVE)
    parked = by(A.AWAITING_HUMAN, A.NEEDS_INPUT)
    done = by(A.DONE)
    # Deferred rows are stored FAILED but are only waiting (A.is_deferred) —
    # grouped apart so the failure section and count hold real failures only.
    deferred = [a for a in items if a.is_deferred]
    failed = [a for a in by(A.FAILED, A.REJECTED) if not a.is_deferred]

    out = [
        "---", "tags: [orchestrator, dashboard]", f"updated: {m.now_iso()}", "---",
        "", "# 🛰 Orchestrator", "",
        f"> assignments: {len(items)} · active {len(active)} · "
        f"awaiting-human {len(parked)} · deferred {len(deferred)} · done {len(done)} "
        f"· failed {len(failed)}",
        # origin/main drift, top of the board: a persistent behind means the
        # tick's pull is failing and vault state is stranding on one host.
        "", "## 🔀 Divergence (origin/main)", *_divergence_section(ledger),
        "", "## 🏃 진행 중", *([_line(a) for a in active] or ["- 없음"]),
        "", "## ⏳ 사람 대기 (parked)",
        *([_parked_line(a) for a in parked] or ["- 없음"]),
        "", "## 💤 대기 (deferred — 실패 아님, 자동 재시도)",
        *([_deferred_line(a) for a in deferred] or ["- 없음"]),
        "", "## ❌ 실패 (원인·재실패횟수)", *([_fail_line(a) for a in failed] or ["- 없음"]),
        # Before "✅ 완료" (Done) on purpose: the brief is told to stop reading at
        # the completed list, so anything placed after it is effectively invisible.
        "", "## 🕰 방치된 사람 태스크 (마감 없음 · 사람 소유)", *_stale_section(db_path),
        # Same reason as the sweep above: before "✅ 완료" (Done), or it is invisible.
        "", "## 📌 결정문 행동 항목 (미기재 · 끊긴 링크)",
        *_decisions_section(decisions_dir, items_dir),
        "", "## ✅ 완료", *([_line(a) for a in done] or ["- 없음"]),
        "", "## 🤖 에이전트 (liveness)",
        # Runners only. Humans send no heartbeat, so a `⚪ 사람` line here read as
        # "did a person die?"; the parked / "방치된 사람 태스크" (stale human tasks)
        # sections above own everything people-shaped.
        *[f"- {live.get(ag.id, '⚫ unknown')} "
          f"**{ag.id}** ({ag.kind})"
          + (f" — {', '.join(ag.projects)}" if ag.projects else "")
          for ag in registry.agents if not ag.is_human],
        "",
    ]
    return "\n".join(out)


def write(ledger: Ledger, registry: Registry, out_path: str = DEFAULT_OUT,
          db_path=None, decisions_dir=None, items_dir=None) -> str:
    from pathlib import Path
    text = render(ledger, registry, db_path, decisions_dir, items_dir)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(text, encoding="utf-8")
    return text
