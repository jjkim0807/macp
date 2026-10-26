"""Auto-rescope — execute a split plan a worker left behind when it parked, without a human.

When a task's Implement (or Verify) stage stops with "this needs re-scoping
beyond a single Implement stage", until now Verify set `blocked_on` and the
orchestrator's `collect()` parked it as `AWAITING_HUMAN`, sending a ⛔
KakaoTalk alert (2026-10-10 `dancemirror-unified-projects` took exactly this
path). But if the worker left a **machine-readable split plan**
(`rescope.json`) in the run directory and it satisfies the safety conditions,
no human judgment is needed — just create 2-5 sub-tasks from the plan, chain
them serially with `depends_on`, and flip the parent into a
`worker_mode: off` bundle.

This module performs that execution. It follows the same design constraints
as `resume.py`:

- **stdlib only.** This runs on the tick path, the tick must also start under
  a bare `python3`, and it must not break engine isolation
  (`scripts/check_engine_isolation.sh`). Sibling engine modules (`cli`·
  `repos`·`task_validate`) are **lazily imported inside functions**.
- **Judgment is a pure function.** `can_auto_rescope()` does no I/O — it takes
  the plan dict and the parent frontmatter dict as arguments, so each "hand
  off to a human" branch is unit-tested without any files.
- **Every uncertainty fails open to "cannot auto-rescope" (keep ⛔).** If the
  plan can't be read or the judgment is ambiguous, this does not auto-rescope
  and hands off to a human as before — safer than stamping out sub-tasks
  unconditionally.
- **All paths are injected as arguments.** `vault_root`·`items_dir`·
  `templates_dir` are arguments of the public functions (the tick assembles
  them from `ledger.root`; only the defaults come from `cli.*`). The module
  never computes the root itself, so tests can never escape the tmp vault.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

RESCOPE_SCHEMA_VERSION = 1
RESCOPE_FILENAME = "rescope.json"

# The framing collect() stamps on a parked row. Separate from `ALERT_BLOCKED`
# (⛔) so only this row routes to the reconcile_rescope sweep. This is a benign
# park (like ALERT_BLOCKED·ALERT_TIMEOUT_PARK), so it is not in
# `FAILURE_ALERT_KINDS`. The string must match orchestrator.ALERT_RESCOPE_PENDING
# (both are wire format).
ALERT_RESCOPE_PENDING = "rescope-pending"
# The framing to revert to when auto-rescope turns out not viable and this
# hands off to a human (= orchestrator.ALERT_BLOCKED).
_ALERT_BLOCKED = "blocked"

# notify levels (= kakao_format.LEVEL_DIGEST / LEVEL_IMMEDIATE). Mirrored as
# strings instead of importing kakao_format, for engine isolation — the caller
# (_notify_routed) does the mapping.
_LEVEL_DIGEST = "digest"
_LEVEL_IMMEDIATE = "immediate"

_MIN_SUBTASKS = 2
_MAX_SUBTASKS = 5
# Keys every sub-task must have. acceptance_criteria must be a "non-empty list".
_REQUIRED_SUBTASK_KEYS = ("slug", "title", "outcome", "acceptance_criteria")

# Signals to "hand off to a human" — be conservative (when in doubt, HUMAN).
_FALSEY = frozenset({"false", "0", "no", "off", "none", ""})
# Real account / live trading. If it appears in repo/working_dir, HUMAN.
_LIVE_MARKERS = ("ai-stock-trading",)
# Live-trading tags (lowercase).
_LIVE_TAGS = frozenset({"live", "real-trade", "real-account"})
# Planner's own-boundary guardrail (human-owned). If it appears in the parent's
# outcome/spec or a sub-task's files, HUMAN.
_GUARDRAIL_PATHS = ("Worker/planner_policy.yaml", ".claude/commands/worker-planner.md")
# Phrases in if_then that ask for human confirmation (case-insensitive
# substring). Conservatively HUMAN.
_HUMAN_CONFIRM_PHRASES = (
    "사람 확인", "사람이 확인", "사람이 판단", "사람 판단", "사람에게",
    "수동", "manual", "human",
)


def _now_iso() -> str:
    """tz-aware current time — carries an offset because ledger `updated_ts`/history are parsed with fromisoformat."""
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _default_log(msg: str) -> None:
    print(f"[orchestrator] rescope: {msg}")


# ---------------------------------------------------------------------------
# Load · discover rescope.json
# ---------------------------------------------------------------------------


def load_rescope(run_dir) -> dict | None:
    """Read `run_dir/rescope.json` into a dict. Returns None if missing or broken (never raises)."""
    if run_dir is None:
        return None
    try:
        data = json.loads((Path(run_dir) / RESCOPE_FILENAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — both missing and broken converge to "no split plan"
        return None
    return data if isinstance(data, dict) else None


def find_latest_rescope(runs_dir, slug: str) -> tuple[Path, dict] | None:
    """Among the `YYYYMMDDTHHMMSS-<slug>` directories under `runs_dir`, collect only
    those that **exactly** match slug, sort by name descending (= time descending),
    and return the (run_dir, plan) of the first run whose `rescope.json` reads
    successfully.

    Suffix matching on slug is forbidden — exact match means `foo` does not pick
    up `foo-bar`'s run ([[macp-resumable-run-glob-matches-slug-suffix]] #54
    lesson). None means discovery failed."""
    try:
        d = Path(runs_dir)
        if not d.is_dir():
            return None
        pat = re.compile(r"\d{8}T\d{6}-" + re.escape(slug) + r"\Z")
        cands = sorted(
            (p for p in d.iterdir() if p.is_dir() and pat.match(p.name)),
            key=lambda p: p.name, reverse=True,
        )
    except Exception:  # noqa: BLE001 — discovery failure ranks the same as "no split plan"
        return None
    for run_dir in cands:
        plan = load_rescope(run_dir)
        if plan is not None:
            return run_dir, plan
    return None


# ---------------------------------------------------------------------------
# Can auto-rescope? — pure judgment function (AC3)
# ---------------------------------------------------------------------------


@dataclass
class RescopeDecision:
    allow: bool
    reason: str


def _is_falsey(v) -> bool:
    return str(v).strip().lower() in _FALSEY


def _as_tag_set(tags) -> set[str]:
    items = tags if isinstance(tags, list) else [tags]
    return {str(t).strip().lower() for t in items if t is not None and str(t).strip()}


def _touches_live_account(parent_fm: dict, subtasks: list) -> bool:
    hay = " ".join(str(parent_fm.get(k) or "") for k in ("repo", "working_dir"))
    if any(mk in hay for mk in _LIVE_MARKERS):
        return True
    return bool(_as_tag_set(parent_fm.get("tags")) & _LIVE_TAGS)


def _touches_guardrail(parent_fm: dict, subtasks: list) -> bool:
    texts = [str(parent_fm.get(k) or "") for k in ("outcome", "next_action", "if_then")]
    for s in subtasks:
        if not isinstance(s, dict):
            continue
        files = s.get("files")
        if isinstance(files, list):
            texts.extend(str(f) for f in files)
        spec = s.get("spec")
        if isinstance(spec, dict):
            texts.extend(str(v) for v in spec.values())
    hay = "\n".join(texts)
    return any(gp in hay for gp in _GUARDRAIL_PATHS)


def _subtasks_malformed(subtasks: list) -> str | None:
    """One-line reason for a structural defect, or None if fine. Catches missing/empty required keys and duplicate slugs."""
    seen: set[str] = set()
    for i, s in enumerate(subtasks):
        if not isinstance(s, dict):
            return f"#{i} is not an object"
        for k in _REQUIRED_SUBTASK_KEYS:
            v = s.get(k)
            if k == "acceptance_criteria":
                if not (isinstance(v, list) and v and all(str(x).strip() for x in v)):
                    return f"#{i} acceptance_criteria must be a non-empty list of strings"
            elif not (isinstance(v, str) and v.strip()):
                return f"#{i} missing or empty {k}"
        slug = str(s.get("slug")).strip()
        if slug in seen:
            return f"duplicate subtask slug {slug!r}"
        seen.add(slug)
    return None


def can_auto_rescope(rescope: dict | None, parent_fm: dict, *,
                     lineage_depth_reached: bool | None = None) -> RescopeDecision:
    """A **pure function** (no I/O) that judges "can this split plan be executed
    without a human".

    Returns the first "hand off to a human" reason it hits. If
    `lineage_depth_reached` is None, it is derived from the presence of
    `auto_rescoped_from` in the parent frontmatter (= a task that was already
    born from one auto-split exceeds again → stop at depth 1 to prevent
    infinite splitting). If every check passes, allow=True.
    """
    if not isinstance(rescope, dict):
        return RescopeDecision(False, "no/broken rescope plan")
    subs = rescope.get("subtasks")
    if not isinstance(subs, list) or not (_MIN_SUBTASKS <= len(subs) <= _MAX_SUBTASKS):
        return RescopeDecision(
            False, f"rescope plan must have {_MIN_SUBTASKS}..{_MAX_SUBTASKS} subtasks")
    detail = _subtasks_malformed(subs)
    if detail:
        return RescopeDecision(False, f"subtask malformed: {detail}")
    if lineage_depth_reached is None:
        lineage_depth_reached = bool(str(parent_fm.get("auto_rescoped_from") or "").strip())
    if lineage_depth_reached:
        return RescopeDecision(False, "already auto-rescoped once in this lineage (depth cap 1)")
    am = parent_fm.get("auto_merge")
    if am is not None and _is_falsey(am):
        return RescopeDecision(False, "parent auto_merge:false — human merge gate")
    low = str(parent_fm.get("if_then") or "").lower()
    if any(ph.lower() in low for ph in _HUMAN_CONFIRM_PHRASES):
        return RescopeDecision(False, "if_then asks for human confirmation")
    if _touches_live_account(parent_fm, subs):
        return RescopeDecision(False, "real-account/live-trade task — human only")
    if _touches_guardrail(parent_fm, subs):
        return RescopeDecision(False, "touches a human-owned guardrail file")
    return RescopeDecision(True, f"{len(subs)} subtasks, inherits parent contract")


# ---------------------------------------------------------------------------
# Signal to auto-close a hand-split parent — pure judgment (reconcile_split_parents)
# ---------------------------------------------------------------------------

# Keywords (lowercase substrings) that identify a "exceeds a single Implement
# stage → needs re-scoping" park. These are the words left in the `blocked_on`/
# `last_note` that collect() sets on that kind of park — the triggering case,
# the `dancemirror-unified-projects` row, read "task exceeds a single Implement
# stage — needs re-scoping into the 3 sub-tasks …". An external-blocker park
# (e.g. waiting on an upstream PR) contains none of these keywords, and a
# timeout park (ALERT_TIMEOUT_PARK) has an empty blocked_on with last_note
# reading "N consecutive stage timeouts", so neither matches, and both are
# naturally excluded.
_RESCOPE_PARK_KEYWORDS = (
    "re-scop", "rescop", "exceeds a single implement stage",
    "sub-task", "sub task", "subtask",
)


def is_oversize_rescope_park(blocked_on: str, last_note: str = "") -> bool:
    """Is this park a "exceeds a single Implement stage → needs re-scoping" park
    — a pure function (no I/O).

    Concatenates `blocked_on` and `last_note`, lowercases them, and returns
    True if any of `_RESCOPE_PARK_KEYWORDS` is present. This is the primary
    signal reconcile_split_parents uses to pick out a "hand-split parent"; the
    joint required condition (≥1 children, all done) is counted separately by
    tasks.child_status_rollup. Like `can_auto_rescope`, it only looks at its
    arguments, so it is unit-tested without any files."""
    hay = f"{blocked_on}\n{last_note}".lower()
    return any(k in hay for k in _RESCOPE_PARK_KEYWORDS)


# ---------------------------------------------------------------------------
# Idempotent state file — prevent duplicate execution per park (AC4)
# ---------------------------------------------------------------------------


def _rescoped_state_path(vault_root) -> Path:
    """The `parent_reconcile.json` pattern. Host-local state under the gitignored `Worker/_state/`."""
    return Path(vault_root) / "Worker" / "_state" / "rescope_reconcile.json"


def _load_state(state_path) -> dict:
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _already_rescoped(state_path, parent_slug: str) -> bool:
    return parent_slug in _load_state(state_path)


def _record_rescoped(state_path, parent_slug: str, child_slugs: list[str]) -> None:
    """read-merge-write, never raises (same pattern as `_record_parent_surfaced`)."""
    try:
        p = Path(state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = _load_state(p)
        data[parent_slug] = {"ts": _now_iso(), "into": list(child_slugs)}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — the idempotent record is best-effort
        pass


def plan_is_auto_viable(run_dir, parent_fm: dict, *, state_path, parent_slug: str) -> bool:
    """The thin gate collect() uses — is the split plan in the run-dir auto-executable.

    load_rescope + `parent_slug` match + not yet executed + `can_auto_rescope.allow`.
    Exceptions fail open to False (= keep ⛔, prevent a silent skip)."""
    try:
        plan = load_rescope(run_dir)
        if plan is None:
            return False
        if str(plan.get("parent_slug") or "").strip() != parent_slug:
            return False
        if _already_rescoped(state_path, parent_slug):
            return False
        return can_auto_rescope(plan, parent_fm).allow
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Split plan → sub-task generator (chaining · inheritance · dedup) (AC2)
# ---------------------------------------------------------------------------

# frontmatter fields inherited from parent to child. AC2 specifies repo·
# auto_merge·allowed_tools·model_tier. max_turns·max_budget_usd·priority carry
# the parent's contract over as-is so the children run with the same budget/
# priority (template defaults if absent).
_INHERIT_FIELDS = ("repo", "auto_merge", "allowed_tools", "model_tier",
                   "max_turns", "max_budget_usd", "priority")
# Fields that get wrapped in quotes when given as a string (may contain
# commas·parens·URLs·colons).
_QUOTED_FIELDS = frozenset({"repo", "allowed_tools", "outcome", "next_action", "if_then"})


def _yaml_dq(v) -> str:
    """A YAML double-quoted scalar. Only backslashes and quotes are escaped (harmless even if the parser reads it as plain text)."""
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_scalar(key: str, v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"      # yaml True → `true` (frontmatter convention)
    if key in _QUOTED_FIELDS:
        return _yaml_dq(v)
    return str(v).strip()


def _set_block_list(content: str, key: str, rendered_items: list[str]) -> str:
    """Set frontmatter's `key` to a YAML block list (remove the existing value, then
    append at the end).

    `update_frontmatter_value` only writes a single scalar line, so it can't
    write a block list. Here, `remove_frontmatter_key` removes the existing
    line+block and the block list is re-attached. `rendered_items` are
    already-rendered strings (either `"..."` or a bare slug)."""
    from .. import cli

    content = cli.remove_frontmatter_key(content, key)
    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return content
    fm_part, body = parts
    block = f"{key}:\n" + "\n".join(f"  - {it}" for it in rendered_items)
    return fm_part + "\n" + block + "\n---\n" + body


def _subtask_tags(parent_fm: dict) -> list[str]:
    """todo + parent tags (excluding todo) + worker — the tag shape of a hand-made sub-task."""
    raw = parent_fm.get("tags")
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    out = ["todo"]
    for t in items:
        t = str(t).strip()
        if t and t.lower() != "todo" and t not in out:
            out.append(t)
    if "worker" not in out:
        out.append("worker")
    return out


def _render_body(spec: dict, design_notes: list, run_ref: str, title: str,
                 parent_slug: str) -> str:
    specd = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    goal = str(specd.get("goal") or spec.get("outcome") or "").strip()
    scope_in = str(specd.get("scope_in") or "").strip()
    scope_note = str(specd.get("scope_note") or "").strip()
    today = dt.date.today().isoformat()
    lines = [
        f"# {title}", "",
        "> 자동 재조정으로 만들어진 하위 태스크. 본문 `## 작업 명세 (Spec)` 섹션이 "
        "Research/Plan agent 에게 전달되는 1차 입력이다.", "",
        "## 작업 명세 (Spec)", "",
        f"- **목표**: {goal}",
    ]
    if scope_in:
        lines.append(f"- **범위(포함)**: {scope_in}")
    files = spec.get("files")
    if isinstance(files, list) and files:
        lines.append(f"- **범위 파일**: {', '.join(str(f) for f in files)}")
    if scope_note:
        lines.append(f"- **주의**: {scope_note}")
    header = f"## 이미 정해진 설계 (다시 도출하지 말 것 — {run_ref})" if run_ref \
        else "## 이미 정해진 설계 (다시 도출하지 말 것 — 상위 run.log)"
    lines += ["", header, ""]
    notes = [str(n).strip() for n in design_notes if str(n).strip()] \
        if isinstance(design_notes, list) else []
    if notes:
        lines += [f"- {n}" for n in notes]
    else:
        lines.append("- 상위 태스크 Implement 분석 참조.")
    lines += ["", "## 완료 조건 (Acceptance)", "",
              "- frontmatter `acceptance_criteria`와 같다.", "",
              "## 실행 단계 (선택적 — Plan agent 가 자체 생성하므로 비워둬도 됨)", ""]
    steps = spec.get("steps") if isinstance(spec.get("steps"), list) else []
    if steps:
        lines += [f"- [ ] {str(s).strip()}" for s in steps if str(s).strip()]
    else:
        lines.append("- [ ] (Plan agent 가 생성)")
    lines += ["", "## Progress Log", "",
              f"- {today}: 상위 `{parent_slug}` 자동 재조정으로 생성 (orchestrator)."]
    return "\n".join(lines) + "\n"


def build_subtask_markdown(spec: dict, *, parent_slug: str, parent_fm: dict,
                           design_notes: list, run_ref: str, position: int,
                           prev_slug: str | None, templates_dir) -> str:
    """Build the full sub-task md — fill the `auto-worker` template's frontmatter
    via cmd_new-style scaffolding + injection, and rewrite the body from the
    spec/design notes.

    `position==0` → `worker_mode: auto` (no dependency, goes straight to the
    queue). `position>0` → `worker_mode: blocked` + `on_unblock: auto` +
    `depends_on: [prev_slug]` (serial chain). `allowed_tools` must always be a
    **quoted comma string** (a YAML list gets ignored wholesale by the
    worker). Do not attach an inline `# comment` to a value (the parser
    doesn't strip it)."""
    from .. import cli

    slug = str(spec["slug"]).strip()
    title = str(spec.get("title") or slug).strip()
    today = dt.date.today().isoformat()
    content = (Path(templates_dir) / "auto-worker-task-template.md").read_text(encoding="utf-8")
    # cmd_new-style scaffolding (replace value placeholders).
    content = content.replace("updated: 2026-05-19", f"updated: {today}")
    content = content.replace("due: YYYY-MM-DD", 'due: "-"')
    content = content.replace('worker_branch: "worker/<slug>"', f'worker_branch: "worker/{slug}"')

    specd = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    goal = str(specd.get("goal") or spec.get("outcome") or "").strip()
    # If next_action/if_then are empty, derive them from the spec so a
    # template placeholder doesn't leak through.
    next_action = str(spec.get("next_action") or goal).strip()
    if_then = str(spec.get("if_then")
                  or f"워커가 이 하위 태스크를 잡으면 {goal}").strip()

    set_ = cli.update_frontmatter_value
    content = set_(content, "status", "open")
    content = set_(content, "parent", f'"[[{parent_slug}]]"')
    content = set_(content, "outcome", _yaml_dq(spec["outcome"]))
    content = set_(content, "next_action", _yaml_dq(next_action))
    content = set_(content, "if_then", _yaml_dq(if_then))
    if spec.get("estimate_p50"):
        content = set_(content, "estimate_p50", str(spec["estimate_p50"]).strip())
    if spec.get("estimate_p90"):
        content = set_(content, "estimate_p90", str(spec["estimate_p90"]).strip())
    content = set_(content, "auto_rescoped_from", parent_slug)

    for k in _INHERIT_FIELDS:
        v = parent_fm.get(k)
        if v is None:
            continue
        if isinstance(v, str) and (not v.strip() or v.strip() == "-"):
            continue
        content = set_(content, k, _render_scalar(k, v))

    if position == 0:
        content = set_(content, "worker_mode", "auto")
    else:
        content = set_(content, "worker_mode", "blocked")
        content = set_(content, "on_unblock", "auto")
        content = _set_block_list(content, "depends_on", [str(prev_slug)])

    content = _set_block_list(content, "tags", _subtask_tags(parent_fm))
    content = _set_block_list(content, "acceptance_criteria",
                              [_yaml_dq(x) for x in spec["acceptance_criteria"]])

    fm_part = content.split("\n---\n", 1)[0]
    body = _render_body(spec, design_notes, run_ref, title, parent_slug)
    return fm_part + "\n---\n\n" + body


def generate_subtasks(rescope: dict, *, parent_slug: str, parent_fm: dict,
                      items_dir, templates_dir, run_ref: str = "") -> list[Path]:
    """Write the plan's 2-5 sub-tasks to `items_dir` in order and validate each
    immediately.

    **dedup**: an already-existing sub-task file is skipped (safe to re-run,
    following the `_propagate_downstream` precedent). If a newly-written
    sub-task has a validate ERROR, raise `ValueError` and **roll back only the
    files this call created** (the executor falls back to the human path).
    WARN passes."""
    from .. import task_validate as _task_validate

    subs = rescope.get("subtasks") or []
    design_notes = rescope.get("design_notes") or []
    items_dir = Path(items_dir)
    items_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    created: list[Path] = []
    prev: str | None = None
    try:
        for i, spec in enumerate(subs):
            slug = str(spec["slug"]).strip()
            dest = items_dir / f"{slug}.md"
            if dest.exists():
                paths.append(dest)       # dedup — don't recreate if it already exists
                prev = slug
                continue
            md = build_subtask_markdown(
                spec, parent_slug=parent_slug, parent_fm=parent_fm,
                design_notes=design_notes, run_ref=run_ref, position=i,
                prev_slug=prev, templates_dir=templates_dir)
            dest.write_text(md, encoding="utf-8")
            created.append(dest)
            errors = [iss for iss in _task_validate.validate_file(dest)
                      if iss.level == _task_validate.ERROR]
            if errors:
                raise ValueError(f"{slug}: " + "; ".join(iss.message for iss in errors))
            paths.append(dest)
            prev = slug
    except ValueError:
        for p in created:                # Delete only what this call wrote — restore the vault
            try:
                p.unlink()
            except OSError:
                pass
        raise
    return paths


def _append_progress_log(content: str, note: str) -> str:
    content = content.rstrip("\n")
    if "## Progress Log" in content:
        return content + "\n" + note + "\n"
    return content + "\n\n## Progress Log\n\n" + note + "\n"


def flip_parent_to_bundle(parent_path, child_slugs: list[str]) -> None:
    """Flip the parent md into a `worker_mode: off` bundle and record the history
    in the Progress Log.

    `auto_rescoped: true` is the marker AC5's auto-close looks for, and
    `is_parent: true` is required for `parents_needing_review` to see this
    bundle — because import-md does **not** derive it from a child's
    `parent:` link, it only reads the task's own `is_parent` field
    (cli.parse_task_file). `cli.cli` is only reused, never modified."""
    from .. import cli

    p = Path(parent_path)
    content = p.read_text(encoding="utf-8")
    content = cli.update_frontmatter_value(content, "worker_mode", "off")
    content = cli.update_frontmatter_value(content, "auto_rescoped", "true")
    content = cli.update_frontmatter_value(content, "is_parent", "true")
    today = dt.date.today().isoformat()
    note = (f"- {today}: 자동 분할 → 하위 {len(child_slugs)}개"
            f"({', '.join(child_slugs)}), 상위 묶음(worker_mode: off)으로 전환 (orchestrator)")
    p.write_text(_append_progress_log(content, note), encoding="utf-8")


# ---------------------------------------------------------------------------
# Executor — detect park → execute → informational alert → idempotency → clear the park (AC2/AC4)
# ---------------------------------------------------------------------------


def compose_rescope_alert(parent_slug: str, child_slugs: list[str]) -> str:
    """Informational auto-split alert (🔀, digest). count+ask is the headline, the slugs are a word-clipped tail."""
    from ..kakao_format import compose_alert

    headline = f"🔀 자동 분할: {parent_slug} → 하위 {len(child_slugs)}개 · 첫 태스크 시작"
    return compose_alert(headline, tail=", ".join(child_slugs))


def _fallback_alert(parent_slug: str, reason: str) -> str:
    from ..kakao_format import compose_alert

    headline = "⛔ 자동 재조정 불가 — 사람 판단이 필요해요 (분할안을 손보거나 태스크를 접어주세요)"
    return compose_alert(headline, [f"📋 {parent_slug}"], tail=reason)


def _notify(notify, msg: str, event_key: str, emit, *, level: str | None = None) -> None:
    """Notification is a side effect — a failure does not undo an execution already saved (same pattern as resume._notify)."""
    if notify is None:
        return
    try:
        if level is None:
            notify(msg, event_key=event_key)
        else:
            notify(msg, event_key=event_key, level=level)
    except Exception as e:  # noqa: BLE001
        emit(f"알림 실패(무시): {type(e).__name__}: {e}")


def _refresh_db(paths, db_path=None) -> None:
    """Immediately reflect the parent+children md into todo.db — blocks the daemon's
    lag window (stale-DB redispatch). best-effort: if it fails, the todo-sync
    daemon absorbs it on the next tick (same pattern as `mark_done`)."""
    try:
        from .. import cli

        con = cli.db_connect(db_path or cli.DB_PATH)
        try:
            cli.init_db(con)
            for p in paths:
                cli.refresh_single_task(con, Path(p))
            con.commit()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _default_log(f"DB refresh best-effort 실패(데몬이 흡수): {type(e).__name__}: {e}")


def _fallback_to_human(a, ledger, notify, reason: str, emit) -> None:
    """Auto-rescope not viable → send one ⛔ (immediate) and keep the park.
    collect() may have suppressed the ⛔, so this prevents a silent park.
    Flips the framing to ALERT_BLOCKED to stop re-execution on the next tick
    and hands off to the reconcile_unparked (human clears the blocker) path."""
    a.last_alert_kind = _ALERT_BLOCKED
    a.last_note = f"rescope not auto-viable: {reason}"[:200]
    a.history.append({"ts": _now_iso(), "type": "rescope-fallback",
                      "from": "orchestrator", "reason": str(reason)[:120]})
    ledger.save(a)
    _notify(notify, _fallback_alert(a.task_id, reason),
            f"rescope-fallback:{a.task_id}", emit, level=_LEVEL_IMMEDIATE)
    emit(f"{a.task_id}: 자동 재조정 불가({reason}) — 사람에게 넘김(⛔), 파킹 유지")


def execute_rescope(ledger, vault_root, notify, *, items_dir=None, templates_dir=None,
                    db_path=None, log=None) -> list[str]:
    """Find parked (`AWAITING_HUMAN` + `blocked_on` + `ALERT_RESCOPE_PENDING`) rows
    and auto-execute them. Returns the list of executed parent slugs, and
    **never raises** (must not stop the tick's dispatch — one outer + one
    per-row try/except, the same structure as `resume.consume`).

    For each row: read the split plan from the run-dir and re-confirm with
    `can_auto_rescope` (defensive) → if AUTO: generate sub-tasks (including
    validation) · flip the parent into a bundle · properly clear the parked
    assignment via `ledger.save` (not a hand-edit of
    `Worker/assignments/*.json`) · idempotent record · DB refresh · one 🔀
    (digest). If HUMAN: fall back with one ⛔ (immediate) and keep the park.
    A validate failure rolls back what it created and falls back."""
    done: list[str] = []
    emit = log or _default_log
    try:
        from .assignment import AWAITING_HUMAN, DONE
        from . import repos
        root = Path(vault_root)
        runs_dir = Path(ledger.root).parent / "runs"
        if items_dir is None or templates_dir is None:
            from .. import cli
            items_dir = items_dir or cli.ITEMS_DIR
            templates_dir = templates_dir or (cli.TODO_DIR / "templates")
        items_dir = Path(items_dir)
        templates_dir = Path(templates_dir)
        state_path = _rescoped_state_path(root)
        for a in ledger.all():
            try:
                if not (a.state == AWAITING_HUMAN and getattr(a, "blocked_on", "")
                        and getattr(a, "last_alert_kind", "") == ALERT_RESCOPE_PENDING):
                    continue
                parent_slug = a.task_id
                if _already_rescoped(state_path, parent_slug):
                    continue
                stored = str((a.payload or {}).get("last_run_dir") or "").strip()
                run_dir = runs_dir / Path(stored).name if stored else None
                plan = load_rescope(run_dir) if run_dir else None
                if plan is None:
                    found = find_latest_rescope(runs_dir, parent_slug)
                    if found:
                        run_dir, plan = found
                parent_path = items_dir / f"{parent_slug}.md"
                parent_fm = repos._frontmatter(parent_path)
                decision = (can_auto_rescope(plan, parent_fm) if plan is not None
                            else RescopeDecision(False, "no/broken rescope plan"))
                if not decision.allow:
                    _fallback_to_human(a, ledger, notify, decision.reason, emit)
                    continue
                run_ref = f"Worker/runs/{run_dir.name}/run.log" if run_dir else ""
                try:
                    children = generate_subtasks(
                        plan, parent_slug=parent_slug, parent_fm=parent_fm,
                        items_dir=items_dir, templates_dir=templates_dir, run_ref=run_ref)
                except ValueError as e:
                    _fallback_to_human(a, ledger, notify,
                                       f"subtask validate failed: {e}", emit)
                    continue
                child_slugs = [p.stem for p in children]
                flip_parent_to_bundle(parent_path, child_slugs)
                a.state = DONE
                a.blocked_on = ""
                a.last_note = f"auto-rescoped into {len(child_slugs)} subtasks"[:200]
                a.history.append({"ts": _now_iso(), "type": "rescoped",
                                  "into": child_slugs, "from": "orchestrator"})
                ledger.save(a)
                _record_rescoped(state_path, parent_slug, child_slugs)
                _refresh_db([parent_path, *children], db_path)
                done.append(parent_slug)
                _notify(notify, compose_rescope_alert(parent_slug, child_slugs),
                        f"rescope:{parent_slug}", emit)
                emit(f"{parent_slug}: 자동 분할 → {child_slugs} (상위 묶음, 파킹 정리)")
            except Exception as e:  # noqa: BLE001 — per-row isolation
                emit(f"{getattr(a, 'task_id', '?')}: skipped: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — outer net: must not raise into tick()
        emit(f"sweep aborted: {type(e).__name__}: {e}")
    return done
