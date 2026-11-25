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
# Size cap (bytes of the UTF-8 JSON form) on a split plan a remote runner carries
# INLINE in its plan-park FAIL payload — the run dir lives on the remote host, so
# HQ has no rescope.json file to read and the plan must ride in the message. Both
# the runner's read (read_rescope_for_payload) and HQ's write (save_remote_rescope)
# enforce it: a 2-5 subtask plan is a few KB, so this only ever drops a pathological
# plan from bloating the git-tracked mailbox — fail-closed to today's ⛔.
RESCOPE_MAX_BYTES = 64 * 1024

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
_GUARDRAIL_PATHS = ("Worker/planner_policy.yaml",)
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
# Remote-park bridge — carry a split plan inline in a FAIL payload, restore it
# on HQ. A plan-stage park on a remote runner (macbook·desktop-wsl) leaves its
# rescope.json in THAT host's run dir; HQ has no file, so until now the park
# reached a human as ⛔ even though a viable plan existed (#177 left this as the
# "범위 밖" gap). The runner now reads the plan for the payload, and HQ writes it
# into its own run dir so the ordinary load_rescope → can_auto_rescope →
# execute_rescope path runs exactly as for an HQ run — no second code path.
# ---------------------------------------------------------------------------


def read_rescope_for_payload(run_dir, *, max_bytes: int = RESCOPE_MAX_BYTES) -> dict | None:
    """Read `<run_dir>/rescope.json` for inlining into a remote runner's plan-park
    FAIL payload. Returns the plan dict only when it is present AND its serialized
    form is within `max_bytes` (so a pathological plan cannot bloat the git-tracked
    mailbox); None otherwise. Never raises — None simply leaves today's park, which
    HQ then routes to a human as before."""
    plan = load_rescope(run_dir)
    if not isinstance(plan, dict):
        return None
    try:
        if len(json.dumps(plan, ensure_ascii=False).encode("utf-8")) > max_bytes:
            return None
    except Exception:  # noqa: BLE001 — unserializable plan → carry nothing (fail-closed)
        return None
    return plan


def save_remote_rescope(runs_dir, run_id: str, plan, *,
                        max_bytes: int = RESCOPE_MAX_BYTES) -> "Path | None":
    """Persist a split plan a remote runner carried inline to HQ's
    `<runs_dir>/<run_id>/rescope.json`, so the local reconcile path runs exactly as
    for an HQ run. `run_id` is the run-dir basename (what collect() stores as
    `last_run_dir`), never the remote absolute path. Returns the path written, or
    None when nothing was: not a dict, over the cap, an empty id, or a file already
    present — an HQ run's own rescope.json is NEVER overwritten, so the existing HQ
    path is untouched. Never raises (fail-closed: no file → today's ⛔)."""
    try:
        name = str(run_id).strip()
        if not isinstance(plan, dict) or not name:
            return None
        blob = json.dumps(plan, ensure_ascii=False, indent=2)
        if len(blob.encode("utf-8")) > max_bytes:
            return None
        rd = Path(runs_dir) / name
        dest = rd / RESCOPE_FILENAME
        if dest.exists():
            return None
        rd.mkdir(parents=True, exist_ok=True)
        _write_new_json(dest, plan)
        return dest
    except Exception:  # noqa: BLE001 — fail-closed: no file → today's ⛔ to a human
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
    # `human_gate: true` = a person looks first (docs/tasks.md) — that holds for
    # the split too. Anything but an explicit falsey value denies (conservative).
    gate = parent_fm.get("human_gate")
    if gate is not None and not _is_falsey(gate):
        return RescopeDecision(False, "parent human_gate — a human looks first")
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
# Signal from a PLAN document that the task is too big for one Implement stage
# ---------------------------------------------------------------------------

# Strong phrases a plan uses to DECLARE that the task cannot be done in one
# Implement stage. Deliberately a strict SUBSET of _RESCOPE_PARK_KEYWORDS: it
# does NOT include "subtask"/"sub-task", because the plan template always carries
# a `## Parallelizable subtasks` heading — matching that bare word would skip
# Implement on EVERY task.
_PLAN_OVERSIZE_PHRASE = "exceeds a single implement stage"
# "re-scope"/"rescoped"/"rescoping" as a WORD — not the file `rescope.py`, the
# key `rescope.json`, or a `\|rescope\|` grep alternation. 2026-11-13: three
# design-doc plans were skipped as "oversize" only because they cited
# `todo_system/orchestrator/rescope.py` or grepped for it.
_PLAN_RESCOPE_WORD_RE = re.compile(
    r"(?<![\w/.\\|])re-?scop(?:e|ed|es|ing)?(?![\w.\\|/])")
# A sentence that holds a strong keyword but ALSO carries negation is saying the
# opposite ("this does not exceed a single implement stage", "no re-scoping
# needed", "재조정 불필요") and must not trip the signal. Matched as whole words
# (so "cannot" / "rescoped" are NOT read as negators) plus the Korean negation
# markers. Being generous here only ever SUPPRESSES a skip — the fail-closed
# direction — so it never wrongly skips Implement.
_PLAN_NEGATION_RE = re.compile(
    r"\bnot\b|\bno\b|\bnever\b|n['’]t|없|불필요|않")
# A conditional sentence is not a declaration ("Only if the orchestrator ever has
# to rescope, the hand-split boundary is …" — 2026-11-13). Same fail-closed side.
_PLAN_CONDITIONAL_RE = re.compile(
    r"\bif\b|\bwould\b|\bwhether\b|\bin case\b|\bunless\b|만약|경우|라면|한다면")
_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;。])\s+")


def _plan_sentences(text: str) -> list[str]:
    """Prose sentences of a plan, lowercased: fenced and inline code removed,
    indented command lines (4+ spaces or a tab, outside lists) dropped, the lines
    of each paragraph joined so a sentence wrapped across lines is judged whole,
    then split at sentence punctuation."""
    text = _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub(" ", text))
    paragraphs: list[list[str]] = [[]]
    for raw in text.splitlines():
        if not raw.strip():
            paragraphs.append([])
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)
        if (indent >= 4 or raw.startswith("\t")) and not stripped.startswith(("-", "*", "+")) \
                and not stripped[:1].isdigit():
            continue                         # an indented command / code line
        if stripped.startswith(("#", "-", "*", "+")) or stripped[:1].isdigit():
            paragraphs.append([])            # a heading / list item starts a new unit
        paragraphs[-1].append(stripped)
    out: list[str] = []
    for para in paragraphs:
        if para:
            out.extend(_SENTENCE_SPLIT_RE.split(" ".join(para).lower()))
    return out


_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_VERDICT_RE = re.compile(r"^declares_oversize:\s*[\"']?(yes|no|true|false)[\"']?\s*$", re.M | re.I)


def _frontmatter_verdict(text: str) -> bool | None:
    """`declares_oversize:` from the plan's leading frontmatter, or None."""
    m = _FM_RE.match(text)
    if not m:
        return None
    v = _VERDICT_RE.search(m.group(1))
    if not v:
        return None
    return v.group(1).lower() in ("yes", "true")


def plan_declares_oversize(text: str) -> bool:
    """Does an implementation plan DECLARE that the task is too big for one
    Implement stage — a pure function (no I/O).

    Judged per prose SENTENCE (see `_plan_sentences`): a sentence trips the
    signal when it holds the phrase "exceeds a single implement stage" or the
    word re-scope/rescoped/rescoping (not a file name or identifier), and carries
    neither negation nor a conditional. Code — fenced, inline, or an indented
    command line — is never read, so a plan that cites `rescope.py` or greps for
    it does not count. Returns True as soon as one sentence trips, else False.

    Deliberately conservative / fail-closed. The template's `## Parallelizable
    subtasks` heading is NOT matched (only the strong phrases are); an empty or
    None text is False; any uncertainty resolves to False — wrongly skipping
    Implement is worse than running it on a plan that merely discusses
    re-scoping."""
    if not text:
        return False
    # An explicit verdict in the plan's frontmatter wins (worker-plan writes
    # `declares_oversize: no|yes`). 2026-11-17: a plan for this very feature said
    # "the existing rescope tests stay green" and was parked as oversize while its
    # frontmatter said `declares_oversize: no`.
    verdict = _frontmatter_verdict(text)
    if verdict is not None:
        return verdict
    for sentence in _plan_sentences(text):
        if _PLAN_NEGATION_RE.search(sentence) or _PLAN_CONDITIONAL_RE.search(sentence):
            continue
        if _PLAN_OVERSIZE_PHRASE in sentence or _PLAN_RESCOPE_WORD_RE.search(sentence):
            return True
    return False


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


def _unquote(v) -> str:
    """Strip one pair of matching surrounding quotes — a defence for a list item a
    line parser handed over still quoted (YAML already strips them)."""
    s = str(v).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1].strip()
    return s


def _spec_tier(spec: dict) -> str:
    """A sub-task's own `model_tier` when it is one the classifier knows
    (lowercased), else "" — the child then inherits the parent's."""
    v = spec.get("model_tier")
    v = v.strip().lower() if isinstance(v, str) else ""
    return v if v in _PLAN_SPLIT_TIERS else ""


def _spec_touches(spec: dict) -> list[str] | None:
    """A sub-task's own `touches` when it is a non-empty list of non-empty
    strings, else None — the child then inherits the parent's."""
    raw = spec.get("touches")
    if not isinstance(raw, list) or not raw:
        return None
    out = [_unquote(t) if isinstance(t, str) else "" for t in raw]
    return out if all(out) else None


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

    tier = _spec_tier(spec)              # a child's own valid tier wins over the inherited one
    if tier:
        content = set_(content, "model_tier", tier)

    # `touches` must be a YAML block list (docs/tasks.md; a string is a validate
    # ERROR), so it is copied only from a real list — a child then overlaps its
    # siblings on the dispatcher exactly as the parent did. A child's own list
    # wins (a plan-authored split gives each child its real write scope); a child
    # without a usable one gets the parent's, as before.
    touches = _spec_touches(spec)
    if touches is None:
        touches = parent_fm.get("touches")
    if isinstance(touches, list):
        globs = [_unquote(t) for t in touches if t is not None]
        globs = [g for g in globs if g]
        if globs:
            content = _set_block_list(content, "touches", [_yaml_dq(g) for g in globs])

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
    and hands off to the human (clear the blocker, then `cli resume`)."""
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
                # A plan-stage split points its children at the plan itself.
                run_ref = _plan_run_ref(plan) or (
                    f"Worker/runs/{run_dir.name}/run.log" if run_dir else "")
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


# ---------------------------------------------------------------------------
# Plan-stage split — the engine writes rescope.json from the plan's Phases
# ---------------------------------------------------------------------------
#
# When the PLAN stage declares the task too big for one Implement stage
# (`plan_declares_oversize`), the runner skips Implement and parks the task. An
# Implement/Verify park can leave its own split plan, but no agent runs after a
# plan-stage park — so every one of them used to reach a human as ⛔. The
# runner's plan-park branch now calls `write_plan_rescope`, which builds the
# split along the plan's own `### Phase N:` headings; collect() and
# `execute_rescope` then treat it like any worker-written plan (the same
# `can_auto_rescope` gate, the same 🔀 digest, the parent Progress Log line).
#
# Precision first. The split is built only when every parent acceptance
# criterion maps cleanly: a criterion's leading `§N` (or `§a~§b`) key must match
# the `§N` tokens of exactly ONE Phase heading line — Phase bodies and Files
# lines are never read, because they cite other documents' sections — and a
# criterion without a key must be a local-gate item, which every child gets.
# Anything else (no single `## Phases` section, a numbering gap, a key in 0 or
# 2+ Phases, fewer than two groups, …) builds nothing: no file, today's ⛔.
#
# A plan may instead state its split itself, in a fenced `rescope` block (the
# "Plan-authored split" section below). `write_plan_rescope` looks for that block
# first, and the Phase-heading builder here runs only for a plan with no block:
# a block the engine cannot use is never replaced by a split the plan did not
# write.

_PLAN_FILENAME = "implementation_plan.md"
_PHASES_H2_RE = re.compile(r"^##\s+Phases\b", re.I)
# Only the next H2 ends the section: a stray `# …` line in a Phase body (an
# unfenced shell comment) must not cut it short and silently drop later Phases.
_H2_RE = re.compile(r"^##(?:\s|$)")
_PHASE_H3_ANY_RE = re.compile(r"^###\s+Phase\b", re.I)
# `### Phase N: title` (or a `—`/`-`/`.` separator). `(?!\w|\.\d)` refuses
# `Phase 2a` / `Phase 1.5`, which then make the whole plan ambiguous.
_PHASE_H3_RE = re.compile(r"^###\s+Phase\s+(\d+)(?!\w|\.\d)\s*[:：.—–-]?\s*(.*?)\s*$", re.I)
_KEY_RE = re.compile(r"§\s*(\d+)(?:\s*[~～–-]\s*§?\s*(\d+))?")
# Joiners inside a criterion's leading key cluster: `§1·§2`, `§1, §2`, `§1과 §2`.
_KEY_JOIN_RE = re.compile(r"\s*(?:[·,/+&]|및|and|과|와)?\s*(?=§)", re.I)
_MAX_KEY_SPAN = 50
_TICK_SPAN_RE = re.compile(r"`[^`\n]+`")
_LEAF_IF_THEN = ("계획서 단계가 이 leaf 범위를 넘으면 → 범위 안의 절만 쓰고 나머지는 "
                 "자리표시로 남겨 머지한다(다시 한 스테이지 초과로 파킹하지 않는다)")
_LEAF_SCOPE_NOTE = ("다른 Phase 범위는 건드리지 않는다 — 이미 머지된 앞 하위의 결과는 "
                    "고치지 않고, 뒤 Phase의 절은 자리표시로 남긴다")
_LEAF_CLOSING_NOTE = ("각 하위는 자기 Phase 범위만 머지한다 — 범위 밖 절은 자리표시로 남기고, "
                      "이미 머지된 앞 하위의 결과는 고치지 않는다.")
_RUN_REF_MAX = 200


def parse_plan_phases(text) -> list[tuple[int, str]] | None:
    """`[(n, title)]` of the `### Phase N:` headings in the plan's single
    `## Phases` section — a pure function. Frontmatter and fenced code are
    skipped and the section ends at the next H2. None unless the Phases are
    numbered exactly 1..n with n >= 2; a second `## Phases` section, or a Phase
    heading that cannot be numbered (`Phase 2a`), also yields None."""
    if not isinstance(text, str) or not text.strip():
        return None
    fm = _FM_RE.match(text)
    body = text[fm.end():] if fm else text
    phases: list[tuple[int, str]] = []
    seen = in_sec = False
    fence = ""
    for line in body.splitlines():
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            continue
        if _PHASES_H2_RE.match(line):
            if seen:
                return None                  # two `## Phases` sections — ambiguous
            seen = in_sec = True
            continue
        if _H2_RE.match(line):
            in_sec = False
            continue
        if in_sec and _PHASE_H3_ANY_RE.match(line):
            m = _PHASE_H3_RE.match(line)
            if not m:
                return None                  # a Phase heading this cannot number
            phases.append((int(m.group(1)), m.group(2)))
    nums = [n for n, _ in phases]
    if len(nums) < _MIN_SUBTASKS or nums != list(range(1, len(nums) + 1)):
        return None
    return phases


def _key_span(m) -> set[int] | None:
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if b < a or b - a > _MAX_KEY_SPAN:
        return None                          # an inverted or implausibly wide range
    return set(range(a, b + 1))


def _heading_keys(title: str) -> set[int] | None:
    """Every `§N` / `§a~§b` in a Phase heading. Whole numbers: `§10` is never 1."""
    out: set[int] = set()
    for m in _KEY_RE.finditer(title):
        span = _key_span(m)
        if span is None:
            return None
        out |= span
    return out


def _leading_keys(text: str) -> set[int] | None:
    """The key cluster a criterion STARTS with (`§2 …`, `§1~§3 …`, `§1·§2 …`);
    an empty set when it starts with anything else."""
    s = text.lstrip()
    out: set[int] = set()
    pos = 0
    while True:
        m = _KEY_RE.match(s, pos)
        if not m:
            return out
        span = _key_span(m)
        if span is None:
            return None
        out |= span
        joiner = _KEY_JOIN_RE.match(s, m.end())
        if not joiner:
            return out
        pos = joiner.end()


def _is_gate_ac(ac: str) -> bool:
    """A local-gate criterion (copied to every child): it says "로컬 green" /
    "local green", or names a backticked command and "green"."""
    low = ac.lower()
    if "로컬 green" in low or "local green" in low:
        return True
    return bool(_TICK_SPAN_RE.search(ac)) and "green" in low


def _group_phases(nums: list[int], owned: dict[int, list[int]]) -> list[list[int]]:
    """Contiguous Phase groups, one per Phase that owns a criterion. A Phase that
    owns none joins the group before it (a leading one joins the group after);
    more than `_MAX_SUBTASKS` groups merge into that many contiguous chunks whose
    sizes differ by at most one, larger chunks first."""
    groups: list[list[int]] = []
    pending: list[int] = []
    for n in nums:
        if owned[n]:
            groups.append(pending + [n])
            pending = []
        elif groups:
            groups[-1].append(n)
        else:
            pending.append(n)
    if len(groups) > _MAX_SUBTASKS:
        q, r = divmod(len(groups), _MAX_SUBTASKS)
        merged: list[list[int]] = []
        i = 0
        for j in range(_MAX_SUBTASKS):
            size = q + (1 if j < r else 0)
            merged.append([n for g in groups[i:i + size] for n in g])
            i += size
        groups = merged
    return groups


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# -- what the two plan-stage builders share: one child, and the document around them --


def _plan_refs(run_id: str) -> tuple[str, str]:
    """Vault-relative paths of a run's plan and of the parent task md, which
    states the requirements (there is no requirements.md since research and plan
    were folded into implement; a run id is `<timestamp>-<slug>`)."""
    slug = run_id.split("-", 1)[1] if "-" in run_id else run_id
    return (f"Worker/runs/{run_id}/{_PLAN_FILENAME}", f"Todo/items/{slug}.md")


def _phase_label(group) -> str:
    return "·".join(str(n) for n in group)


def _parent_estimates(parent_fm: dict) -> dict:
    """The parent's `estimate_p50` / `estimate_p90`, where it states them."""
    out = {}
    for k in ("estimate_p50", "estimate_p90"):
        v = str(parent_fm.get(k) or "").strip()
        if v and v != "-":
            out[k] = v
    return out


def _phase_leaf(parent_slug: str, run_id: str, base_title: str, group: list[int],
                title_of: dict, *, slug: str, acceptance_criteria: list, tail: str = "",
                if_then: str = _LEAF_IF_THEN, scope_note: str = _LEAF_SCOPE_NOTE) -> dict:
    """One child of a plan-stage split, described by the contiguous Phase group
    it covers: a title and an outcome that name those Phases (`tail`, when given,
    is appended to the outcome), a next_action that points at the parent run's
    plan and requirements, and the spec and steps the child md is rendered from."""
    plan_ref, req_ref = _plan_refs(run_id)
    label = _phase_label(group)
    names = " / ".join(title_of[n] or f"Phase {n}" for n in group)
    outcome = (f"부모 {parent_slug} 계획서 Phase {label}({_clip(names, 160)}) 범위가 머지돼 "
               "이 하위의 acceptance_criteria가 충족된다")
    return {
        "slug": slug,
        "title": _clip(f"{base_title} — Phase {label}: {names}", 200),
        "outcome": f"{outcome} — {tail}" if tail else outcome,
        "acceptance_criteria": list(acceptance_criteria),
        "next_action": (f"{plan_ref}의 부모 계획서 Phase {label}과 {req_ref}를 읽고 "
                        "그 단계만 수행한다(이미 머지된 앞 부분은 고치지 않는다)"),
        "if_then": if_then,
        "spec": {
            "goal": f"부모 계획서 Phase {label}({_clip(names, 160)})만 수행한다",
            "scope_note": scope_note,
        },
        "steps": [f"Phase {n}: {title_of[n]}" if title_of[n] else f"Phase {n}" for n in group],
    }


def _split_doc(parent_slug: str, run_id: str, parent_fm: dict, n_phases: int,
               groups: list, subs: list, *, source: str, reason: str,
               closing_note: str = _LEAF_CLOSING_NOTE) -> dict:
    """The schema-v1 document around a plan-stage split's children. Its design
    notes carry what a child must not re-derive: where the plan and requirements
    are, which Phases went to which child, the parent's outcome, and the one rule
    every child follows (`closing_note`)."""
    plan_ref, req_ref = _plan_refs(run_id)
    mapping = ", ".join(f"Phase {_phase_label(g)} → {s['slug']}" for g, s in zip(groups, subs))
    notes = [
        f"계획서: {plan_ref} · 요구사항: {req_ref} — 설계를 다시 도출하지 말고 이 둘을 따른다.",
        f"분할: 계획서 Phase {n_phases}개 → 하위 {len(subs)}개 ({mapping})",
    ]
    outcome = _unquote(parent_fm.get("outcome") or "")
    if outcome and outcome != "-":
        notes.append(f"부모 outcome: {_clip(outcome, 400)}")
    notes.append(closing_note)
    return {
        "version": RESCOPE_SCHEMA_VERSION,
        "parent_slug": parent_slug,
        "source": source,
        "reason": reason,
        "run_ref": plan_ref,
        "design_notes": notes,
        "subtasks": subs,
    }


def rescope_from_plan_phases(plan_text, parent_fm, *, parent_slug, run_id,
                             parent_title: str = "") -> dict | None:
    """Build a schema-v1 split plan from a plan's `### Phase N:` headings — a
    **pure function** (no I/O), so every "hand it to a human" branch is
    unit-tested without files. Returns None whenever the split is not
    unambiguous (see the section comment above); the caller then writes nothing.

    Each child covers one contiguous Phase group: `<parent>-p<a>` or
    `<parent>-p<a>-<b>`; the parent criteria its Phases own plus every gate
    criterion; a next_action that points at the parent run's plan and
    requirements; the fixed leaf if_then; the parent `touches` as `files`, so the
    guardrail scan sees the real write scope (the plan's Files lines also cite
    read-only references and are copied nowhere); the parent estimates."""
    phases = parse_plan_phases(plan_text)
    if phases is None or not isinstance(parent_fm, dict):
        return None
    slug = str(parent_slug or "").strip()
    rid = str(run_id or "").strip()
    if not slug or not rid or slug.startswith(".") or any(c in slug + rid for c in "/\\\n"):
        return None
    raw = parent_fm.get("acceptance_criteria")
    if not isinstance(raw, list) or not raw:
        return None
    acs = [_unquote(x) if x is not None else "" for x in raw]
    if not all(acs):
        return None
    heading_keys: dict[int, set[int]] = {}
    for n, title in phases:
        keys = _heading_keys(title)
        if keys is None:
            return None
        heading_keys[n] = keys
    owned: dict[int, list[int]] = {n: [] for n, _ in phases}
    gates: list[int] = []
    for i, ac in enumerate(acs):
        keys = _leading_keys(ac)
        if keys is None:
            return None
        if not keys:
            if not _is_gate_ac(ac):
                return None                  # a keyless criterion that is not a gate
            gates.append(i)
            continue
        hits = [n for n, _ in phases if heading_keys[n] & keys]
        if len(hits) != 1:
            return None                      # owned by no Phase, or by two or more
        owned[hits[0]].append(i)
    groups = _group_phases([n for n, _ in phases], owned)
    if len(groups) < _MIN_SUBTASKS:
        return None

    title_of = dict(phases)
    base = _clip(str(parent_title or "").strip() or slug, 120)
    gate_acs = [acs[i] for i in gates]
    touches = parent_fm.get("touches")
    files = [g for g in (_unquote(t) for t in touches if t is not None) if g] \
        if isinstance(touches, list) else []
    estimates = _parent_estimates(parent_fm)
    subs: list[dict] = []
    for pos, g in enumerate(groups):
        tail = ("앞 하위가 머지한 부분은 고치지 않는다" if pos == len(groups) - 1
                else "범위 밖 절은 자리표시로 남기고 다음 하위가 채운다")
        sub = _phase_leaf(
            slug, rid, base, g, title_of,
            slug=f"{slug}-p{g[0]}" if len(g) == 1 else f"{slug}-p{g[0]}-{g[-1]}",
            acceptance_criteria=([acs[i] for i in sorted(i for n in g for i in owned[n])]
                                 + gate_acs),
            tail=tail)
        sub.update(estimates)
        if files:
            sub["files"] = list(files)
        subs.append(sub)

    return _split_doc(
        slug, rid, parent_fm, len(phases), groups, subs, source="plan-phases",
        reason=(f"plan stage declared the task larger than one Implement stage — split at "
                f"the plan's Phase boundaries ({len(phases)} phases → {len(subs)} subtasks)"))


# ---------------------------------------------------------------------------
# Plan-authored split — the plan states its own split in a `rescope` block
# ---------------------------------------------------------------------------
#
# The Phase-heading builder above needs every parent criterion to start with a
# `§N` key. A code task's criteria rarely do (2026-11-18: eight Phases, six
# keyless criteria, and a three-way split the plan spelled out as a table — which
# then had to be applied by hand). So the Plan stage may state the split itself,
# as ONE fenced block whose info string is `rescope`:
#
#     ```rescope
#     {"subtasks": [
#       {"slug": "task-1-first", "phases": [1, 2],
#        "acceptance_criteria": ["..."], "touches": ["path/file.py", "dir/**"]},
#       {"slug": "task-2-second", "phases": [3], "depends_on": ["task-1-first"],
#        "acceptance_criteria": ["..."], "touches": ["docs/page.md"]}
#     ]}
#     ```
#
# Per sub-task `slug`, `phases`, `acceptance_criteria` and `touches` are
# required; `title`, `outcome`, `model_tier`, `estimate_p50`, `estimate_p90` and
# `depends_on` are optional. Block order IS the serial chain, so `depends_on`
# may only repeat it. `next_action`, `if_then`, `spec`, `steps` and `files` are
# written by the engine; any other key is ignored.
#
# The block is used as written or not at all. A plan that has one never reaches
# the Phase-heading builder: a block the engine cannot use (invalid JSON, fewer
# than two or more than five sub-tasks, Phases not covered exactly once and in
# order, an unsafe slug or path, …) builds nothing — no file, today's ⛔ — so the
# engine never runs a split the plan did not write.

PLAN_SPLIT_FENCE = "rescope"
_SPLIT_SOURCE = "plan-split-block"
_SPLIT_BLOCK_MAX_CHARS = 64_000
_SPLIT_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SPLIT_SLUG_MAX = 80
_SPLIT_PATH_MAX = 200
# `30m` / `2h` / `1h30m` / `1d` — model_select.estimate_minutes' grammar, less seconds.
_SPLIT_ESTIMATE_RE = re.compile(r"(?:\d+(?:\.\d+)?[dhm])+")
# = model_select.TIERS, mirrored as strings for engine isolation (a test pins the two).
_PLAN_SPLIT_TIERS = ("heavy", "standard", "light")
_SPLIT_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")
_SPLIT_WHY_MAX = 200
# The block path's wording. A Phase of a code task has no document sections to
# leave as placeholders, so these do not borrow the leaf sentences above.
_SPLIT_IF_THEN = ("워커가 잡으면 → {plan_ref}의 Phase {label}과 {req_ref}를 읽고 그 Phase만 "
                  "수행한다(설계는 계획서를 따르고 다시 도출하지 않는다). 범위 밖 Phase는 건드리지 "
                  "않고, 한 스테이지에 다 안 들어가면 끝낸 Phase까지 커밋하고 남은 Phase를 run.log에 "
                  "적는다(다시 한 스테이지 초과로 파킹하지 않는다)")
_SPLIT_SCOPE_NOTE = ("다른 Phase 범위는 건드리지 않는다 — 이미 머지된 앞 하위의 결과는 "
                     "고치지 않고, 뒤 Phase의 일은 뒤 하위에 남긴다")
_SPLIT_CLOSING_NOTE = ("각 하위는 자기 Phase 범위만 머지한다 — 범위 밖 Phase는 건드리지 않고, "
                       "이미 머지된 앞 하위의 결과는 고치지 않는다.")


def find_plan_split_blocks(text) -> list[str]:
    """Bodies of the plan's top-level fenced blocks whose info string starts
    with `rescope` (any case) — a pure function. Frontmatter is skipped; a fence
    inside a longer fence (an example quoted in the plan) is not top-level; a
    block that is never closed still counts, so a plan that tried to state a
    split is never mistaken for one that did not."""
    if not isinstance(text, str) or not text.strip():
        return []
    fm = _FM_RE.match(text)
    blocks: list[str] = []
    char, size, body = "", 0, None      # the open fence; body is a list only inside a split block
    for line in (text[fm.end():] if fm else text).splitlines():
        stripped = line.strip()
        if char:
            if len(stripped) >= size and set(stripped) == {char}:
                if body is not None:
                    blocks.append("\n".join(body))
                char, size, body = "", 0, None
            elif body is not None:
                body.append(line)
            continue
        m = _SPLIT_FENCE_OPEN_RE.match(line)
        if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
            continue                     # not a fence (a code span that starts the line)
        char, size = m.group(1)[0], len(m.group(1))
        info = m.group(2).split()
        body = [] if info and info[0].lower() == PLAN_SPLIT_FENCE else None
    if char and body is not None:
        blocks.append("\n".join(body))
    return blocks


def _unprintable(ch: str) -> bool:
    """A character a task md's YAML frontmatter cannot carry in a quoted scalar:
    a C0 / C1 control, DEL, a lone surrogate, or one of the two noncharacters
    that end the basic plane."""
    n = ord(ch)
    return n < 0x20 or 0x7F <= n <= 0x9F or 0xD800 <= n <= 0xDFFF or n in (0xFFFE, 0xFFFF)


def _split_text(v) -> str:
    """A block's text value on one line (whitespace runs collapsed). "" when it
    is not a string, is blank, or holds a character the task md cannot carry."""
    if not isinstance(v, str):
        return ""
    s = " ".join(v.split())
    return "" if any(_unprintable(ch) for ch in s) else s


def _split_estimate(v) -> str:
    """A block's estimate as `30m` / `2h` / `1h30m` / `1d` (lowercased, inner
    spaces dropped), or "" when it is anything else."""
    if not isinstance(v, str):
        return ""
    s = "".join(v.split()).lower()
    return s if _SPLIT_ESTIMATE_RE.fullmatch(s) else ""


def _reaches_guardrail(path: str) -> bool:
    """Could this path or glob cover a human-owned guardrail file: it names one,
    or its literal prefix (the text before the first wildcard) is a prefix of
    one (`Worker/**`, `**/*.md`, or the bare directory `Worker`).
    Every glob that can match such a file starts with a prefix of its path, so
    none of them passes; a few harmless paths are refused with them."""
    cut = min([i for i in (path.find(c) for c in "*?[") if i != -1] or [len(path)])
    return any(gp in path or gp.startswith(path[:cut]) for gp in _GUARDRAIL_PATHS)


def _split_touches(raw) -> tuple[list[str] | None, str]:
    """A block child's `touches` as `(paths, "")`: a non-empty list of
    repo-relative paths or globs, one per entry, duplicates dropped. `(None,
    why)` when any entry is unusable — the whole list is refused, never trimmed."""
    if not isinstance(raw, list) or not raw:
        return None, "must be a non-empty list of repo-relative paths or globs"
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None, "every entry must be a string"
        p = item.strip()
        while p.startswith("./"):
            p = p[2:]
        shown = _clip(" ".join(p.split()), 60)
        if not p:
            return None, "an entry is empty"
        if len(p) > _SPLIT_PATH_MAX or any(c in p for c in "\\\n\r\t"):
            return None, f"not a plain one-line path: {shown}"
        if p.startswith(("/", "~")) or ".." in p.split("/"):
            return None, f"must stay inside the repo: {shown}"
        if "{" in p or "}" in p:
            return None, f"list each path on its own (no {{a,b}} braces): {shown}"
        if _reaches_guardrail(p):
            return None, f"reaches a human-owned guardrail file: {shown}"
        if p not in out:
            out.append(p)
    return out, ""


def _refuse(why: str) -> tuple[None, str]:
    """`(None, why)` with the reason on one clipped line (it goes to the run log)."""
    return None, _clip(" ".join(str(why).split()), _SPLIT_WHY_MAX)


def rescope_from_split_block(blocks, plan_text, parent_fm, *, parent_slug, run_id,
                             parent_title: str = "") -> tuple[dict | None, str]:
    """Build a schema-v1 split plan from the plan's own `rescope` block — a
    **pure function** (no I/O) that never raises. `blocks` is what
    `find_plan_split_blocks` returned. Answers `(plan, "")`, or `(None, why)`
    with the first defect it found; the caller then writes nothing.

    Checked in this order: exactly one block; its size; a JSON object with a
    `subtasks` list of 2..5; the plan's Phases readable and the parent / run
    names plain; then each sub-task (see the section comment above for the
    fields); and last that the `phases` lists, read in block order, are exactly
    the plan's Phases 1..n — one comparison that refuses a gap, an overlap, a
    reordering and a non-contiguous group alike.

    A child keeps the block's criteria as written (the engine adds no gate line:
    the plan repeats it where it applies) and its `touches`, which also become
    its `files`, so `can_auto_rescope`'s guardrail scan sees the real write
    scope. Title and outcome are the block's or derived from the Phase headings;
    estimates are the block's or the parent's; a `model_tier` is carried only
    when the block gives one (the generator otherwise inherits the parent's)."""
    if not isinstance(blocks, list) or not blocks:
        return _refuse("the plan has no rescope block")
    if len(blocks) != 1:
        return _refuse(f"{len(blocks)} rescope blocks — a plan carries exactly one")
    body = blocks[0]
    if not isinstance(body, str):
        return _refuse("the rescope block is not text")
    if len(body) > _SPLIT_BLOCK_MAX_CHARS:
        return _refuse(f"the rescope block is longer than {_SPLIT_BLOCK_MAX_CHARS} characters")
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError) as e:
        return _refuse(f"the rescope block is not valid JSON ({_clip(str(e), 80)})")
    raw = doc.get("subtasks") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return _refuse('the rescope block must be a JSON object with a "subtasks" list')
    if not (_MIN_SUBTASKS <= len(raw) <= _MAX_SUBTASKS):
        return _refuse(f"{len(raw)} subtasks — a split has {_MIN_SUBTASKS} to {_MAX_SUBTASKS}")
    phases = parse_plan_phases(plan_text)
    if phases is None:
        return _refuse("the plan's Phases section cannot be read as Phase 1..n")
    if not isinstance(parent_fm, dict):
        return _refuse("the parent frontmatter is unreadable")
    slug = str(parent_slug or "").strip()
    rid = str(run_id or "").strip()
    if not slug or not rid or slug.startswith(".") or any(c in slug + rid for c in "/\\\n"):
        return _refuse("the parent slug or the run id is not a plain name")

    seen: list[str] = []
    cover: list[int] = []
    children: list[dict] = []
    for i, s in enumerate(raw):
        at = f"subtask #{i}"
        if not isinstance(s, dict):
            return _refuse(f"{at} is not an object")
        child = s.get("slug")
        if not (isinstance(child, str) and len(child) <= _SPLIT_SLUG_MAX
                and _SPLIT_SLUG_RE.fullmatch(child)):
            return _refuse(f"{at} slug must be kebab-case (a-z, 0-9, single hyphens, "
                           f"at most {_SPLIT_SLUG_MAX} characters)")
        if child == slug:
            return _refuse(f"{at} slug is the parent's own ({child})")
        if child in seen:
            return _refuse(f"{at} slug repeats an earlier subtask ({child})")
        group = s.get("phases")
        if not (isinstance(group, list) and group
                and all(isinstance(n, int) and not isinstance(n, bool) for n in group)):
            return _refuse(f"{at} phases must be a non-empty list of Phase numbers")
        want = seen[-1:]                 # block order is the chain: [] first, then [previous]
        deps = s.get("depends_on")
        if deps is not None and deps != want:
            return _refuse(f"{at} depends_on must be {json.dumps(want)} or left out — "
                           "subtasks run in block order")
        acs_raw = s.get("acceptance_criteria")
        acs = [_split_text(x) for x in acs_raw] if isinstance(acs_raw, list) else []
        if not acs or not all(acs):
            return _refuse(f"{at} acceptance_criteria must be a non-empty list of "
                           "non-blank strings")
        touches, why = _split_touches(s.get("touches"))
        if touches is None:
            return _refuse(f"{at} touches: {why}")
        tier = s.get("model_tier")
        if tier is not None:
            tier = tier.strip().lower() if isinstance(tier, str) else ""
            if tier not in _PLAN_SPLIT_TIERS:
                return _refuse(f"{at} model_tier must be one of "
                               f"{'|'.join(_PLAN_SPLIT_TIERS)}")
        estimates: dict[str, str] = {}
        for k in ("estimate_p50", "estimate_p90"):
            if s.get(k) is None:
                continue
            estimates[k] = _split_estimate(s[k])
            if not estimates[k]:
                return _refuse(f"{at} {k} must look like 30m, 2h, 1h30m or 1d")
        texts: dict[str, str] = {}
        for k in ("title", "outcome"):
            if s.get(k) is None:
                continue
            texts[k] = _split_text(s[k])
            if not texts[k]:
                return _refuse(f"{at} {k} must be a non-blank string")
        if any(gp in text for gp in _GUARDRAIL_PATHS for text in (*acs, *texts.values())):
            return _refuse(f"{at} names a human-owned guardrail file")
        seen.append(child)
        cover.extend(group)
        children.append({"slug": child, "phases": list(group), "acceptance_criteria": acs,
                         "touches": touches, "tier": tier or "", "estimates": estimates,
                         "texts": texts})

    n = len(phases)
    if cover != list(range(1, n + 1)):
        return _refuse(f"the subtasks' phases, in block order, must be exactly Phase 1 to {n} "
                       f"of the plan — each once, none left out (got "
                       f"{', '.join(str(k) for k in cover)})")

    title_of = dict(phases)
    plan_ref, req_ref = _plan_refs(rid)
    base = _clip(str(parent_title or "").strip() or slug, 120)
    inherited = _parent_estimates(parent_fm)
    subs: list[dict] = []
    for c in children:
        sub = _phase_leaf(
            slug, rid, base, c["phases"], title_of, slug=c["slug"],
            acceptance_criteria=c["acceptance_criteria"],
            if_then=_SPLIT_IF_THEN.format(plan_ref=plan_ref, req_ref=req_ref,
                                          label=_phase_label(c["phases"])),
            scope_note=_SPLIT_SCOPE_NOTE)
        if "title" in c["texts"]:
            sub["title"] = _clip(c["texts"]["title"], 200)
        if "outcome" in c["texts"]:
            sub["outcome"] = c["texts"]["outcome"]
        sub.update({**inherited, **c["estimates"]})
        sub["files"] = list(c["touches"])
        sub["touches"] = list(c["touches"])
        sub["phases"] = list(c["phases"])
        if c["tier"]:
            sub["model_tier"] = c["tier"]
        subs.append(sub)

    plan = _split_doc(
        slug, rid, parent_fm, n, [c["phases"] for c in children], subs, source=_SPLIT_SOURCE,
        reason=(f"plan stage declared the task larger than one Implement stage — split by "
                f"the plan's own rescope block ({n} phases → {len(subs)} subtasks)"),
        closing_note=_SPLIT_CLOSING_NOTE)
    return plan, ""


def _plan_run_ref(plan) -> str:
    """The plan's own `run_ref` when it is a safe one-line vault path (a
    plan-stage split names its implementation_plan.md); "" otherwise, and
    `execute_rescope` falls back to the run's run.log as before."""
    v = plan.get("run_ref") if isinstance(plan, dict) else None
    if not isinstance(v, str):
        return ""
    v = v.strip()
    if (v.startswith("Worker/runs/") and len(v) <= _RUN_REF_MAX
            and "\n" not in v and "\r" not in v and ".." not in v):
        return v
    return ""


def _md_title(text: str) -> str:
    """The first `# ` heading after a task md's frontmatter, or ""."""
    m = _FM_RE.match(text)
    for line in (text[m.end():] if m else text).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _write_new_json(dest: Path, data: dict) -> None:
    """Write through a sibling temp file + rename, so a reader never sees half a
    file; the temp file is removed when the write fails."""
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_plan_rescope(run_dir, *, parent_slug: str, items_dir) -> str:
    """Write `<run_dir>/rescope.json` for a plan-stage park, built from the run's
    `implementation_plan.md` and the parent task md in `items_dir`. Returns one
    line for the runner's progress log and **never raises**.

    The plan's own `rescope` block comes first (`rescope_from_split_block`): when
    the plan has one, that split is written or nothing is. Only a plan with no
    block at all is split along its Phase headings (`rescope_from_plan_phases`).

    Fail-closed at every step: an existing rescope.json is never replaced, and
    nothing is written when the plan or the parent md is unreadable, the block is
    unusable, the Phase split is not unambiguous (`rescope_from_plan_phases` →
    None), `can_auto_rescope` denies it, or a child md of the same name already
    exists (`generate_subtasks` would otherwise chain that unrelated file in as a
    child). No file means the park stays today's ⛔ to a human. The parent
    frontmatter is read with `repos._frontmatter`, the parser collect() and
    `execute_rescope` judge it with, so all three see the same dict."""
    try:
        rd = Path(run_dir)
        items = Path(items_dir)
        dest = rd / RESCOPE_FILENAME
        if dest.exists():
            return "rescope.json already present — left as is"
        plan_text = (rd / _PLAN_FILENAME).read_text(encoding="utf-8")
        parent_path = items / f"{parent_slug}.md"
        from . import repos
        parent_fm = repos._frontmatter(parent_path)
        if not parent_fm:
            return "no split: parent task md unreadable"
        title = _md_title(parent_path.read_text(encoding="utf-8"))
        blocks = find_plan_split_blocks(plan_text)
        if blocks:
            # The plan's own split is used as written. An unusable block is never
            # replaced by a Phase-heading split the plan did not ask for.
            plan, why = rescope_from_split_block(
                blocks, plan_text, parent_fm, parent_slug=parent_slug, run_id=rd.name,
                parent_title=title)
            if plan is None:
                return f"no split: plan rescope block unusable — {why}"
        else:
            plan = rescope_from_plan_phases(
                plan_text, parent_fm, parent_slug=parent_slug, run_id=rd.name,
                parent_title=title)
            if plan is None:
                return "no split: plan Phases do not map the parent's criteria unambiguously"
        decision = can_auto_rescope(plan, parent_fm)
        if not decision.allow:
            return f"no split: {decision.reason}"
        clash = [s["slug"] for s in plan["subtasks"] if (items / f"{s['slug']}.md").exists()]
        if clash:
            return f"no split: subtask md already exists ({clash[0]})"
        _write_new_json(dest, plan)
        slugs = [s["slug"] for s in plan["subtasks"]]
        return f"rescope.json written — {len(slugs)} subtasks: {', '.join(slugs)}"
    except Exception as e:  # noqa: BLE001 — fail-closed: no file means today's ⛔
        return f"no split: {type(e).__name__}"
