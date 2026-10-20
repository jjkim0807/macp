"""Validate the worker contract of a task file.

`cli.py audit` already checks the planning fields (outcome/next_action/...)
for open tasks. This module covers the other half: the frontmatter fields
`worker.py` reads when it actually runs a task.

That half is the dangerous one, because getting it wrong is *silent*. Writing

    allowed_tools:
      - Read
      - Bash

instead of the string form leaves `isinstance(raw, str)` false in
`WorkerTask.allowed_tools_implement`, so the worker discards the whitelist and
falls back to `DEFAULT_IMPLEMENT_TOOLS` — which has no `gh`, `gcloud` or
`./gradlew`. The task then dies partway through with a permissions error that
looks nothing like its cause. 15 task files in this vault carry that mistake.

Everything here is deliberately checked through `worker.parse_frontmatter` and
the worker's own constants, so validation cannot drift away from what the
worker really sees at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import re
from functools import lru_cache

from . import worker as _worker
from .ownership import HUMAN_OVERRIDE_WORKER_MODES


@lru_cache(maxsize=1)
def known_worker_modes() -> frozenset[str]:
    """Modes the codebase actually branches on.

    Derived rather than hand-listed: a hand-list drifts, and the first draft of
    this file wrongly flagged the live `blocked` and `quick` modes for exactly
    that reason.
      - task_deps.VALID_MODES: what an unblock may flip a task to
      - "blocked": task_deps gates dependency resolution on it
      - ownership.HUMAN_OVERRIDE_WORKER_MODES: the not-worker-owned values
      - the rest: literals worker.py compares against

    task_deps is imported lazily because it imports cli, which imports this
    module — a module-level import would close that cycle.
    """
    from .task_deps import VALID_MODES

    return frozenset(
        set(VALID_MODES)
        | {"blocked", "review", "review_required", "auto-review-disabled"}
        | set(HUMAN_OVERRIDE_WORKER_MODES)
    )

ERROR = "error"
WARN = "warn"


@dataclass(frozen=True)
class Issue:
    level: str
    field: str
    message: str

    def format(self, slug: str) -> str:
        return f"[{self.level}] {slug}: {self.field} — {self.message}"


_BASH_PATTERN_RE = re.compile(r"Bash\(([^)]*)\)")

# macp-dashboard-android-1-app (2026-09-16): spec required a committed release
# keystore, but allowed_tools had no keytool rule. Implement built the APK with
# a debug-key fallback and Verify caught it at the end of the pipeline as
# blocked_on — a wasted run. Catch the gap at authoring time instead.
_KEYSTORE_BODY_RE = re.compile(r"키스토어|keytool|\.jks|\.keystore", re.IGNORECASE)


def _check_keystore_signing(raw_tools: str, body: str) -> list[Issue]:
    if not body or not _KEYSTORE_BODY_RE.search(body):
        return []
    if "keytool" in raw_tools:
        return []
    return [
        Issue(
            WARN,
            "allowed_tools",
            "spec 본문이 키스토어/서명을 언급하지만 allowed_tools에 keytool이 없다. "
            "릴리즈 서명 단계에서 워커가 debug 키로 폴백하고 Verify가 blocked_on으로 "
            "되돌린다 (macp-dashboard-android-1-app 사례) — Bash(keytool:*)를 추가할 것.",
        )
    ]


# GitHub Actions is not used in any project unless the user explicitly asks for
# it on that project (Decisions/2026-09-11-no-github-actions-local-release-lane,
# extended to every project 2026-09-16). A task whose contract still leans on it
# ("CI green", "CI에서 생성") can never be satisfied — Verify runs the project's
# checks locally — so flag it at authoring time. `ci: actions` is the opt-in.
_ACTIONS_CONTRACT_FIELDS = ("outcome", "next_action", "if_then", "acceptance_criteria")
_ACTIONS_RE = re.compile(
    r"(?i:github\s*actions|\bgh\s+run\b|\bgh\s+pr\s+checks\b)|(?<![A-Za-z])CI(?![A-Za-z])"
)
CI_OPT_IN = "actions"


def _check_actions_dependency(fm: dict) -> list[Issue]:
    issues: list[Issue] = []
    ci = fm.get("ci")
    opted_in = isinstance(ci, str) and ci.strip().lower() == CI_OPT_IN
    if ci is not None and not opted_in:
        issues.append(
            Issue(
                WARN,
                "ci",
                f"unrecognised value {ci!r}; the only value is `ci: actions` (the user "
                "explicitly asked for GitHub Actions on this project). Drop the field "
                "otherwise — local verification is the default.",
            )
        )
    if opted_in:
        return issues
    for field in _ACTIONS_CONTRACT_FIELDS:
        value = fm.get(field)
        texts = value if isinstance(value, list) else [value]
        if any(isinstance(t, str) and _ACTIONS_RE.search(t) for t in texts):
            issues.append(
                Issue(
                    WARN,
                    field,
                    "GitHub Actions/CI에 기대는 문구가 있다. Actions는 모든 프로젝트에서 "
                    "쓰지 않으므로(Decisions/2026-09-11-no-github-actions-local-release-lane) "
                    "Verify가 돌릴 로컬 명령으로 적을 것. 사용자가 이 프로젝트에 Actions를 "
                    "명시적으로 요청했다면 `ci: actions`를 추가한다.",
                )
            )
    return issues


# Decision-gate fields written by todo_system.orchestrator.project_stall: a
# project opts out of the quiet-stall sweep with `decision_gate: off`, and the
# generated decision task carries `decision_gate_for: <project>`. The validator
# ignored unknown fields, so a typo (`decision_gate: of`) silently did nothing;
# flag it at authoring time. Both checks fire ONLY when the field is present, so
# no existing task file changes.
_DECISION_GATE_VALUES = frozenset({"off", "on"})


def _check_decision_gate(fm: dict) -> list[Issue]:
    issues: list[Issue] = []
    gate = fm.get("decision_gate")
    if gate is not None:
        # yaml.safe_load turns a bare `off`/`on` into a bool; the line parser
        # keeps the string (and any inline comment). Normalise both.
        if gate is False:
            norm = "off"
        elif gate is True:
            norm = "on"
        else:
            norm = str(gate).split("#")[0].strip().lower()
        if norm not in _DECISION_GATE_VALUES:
            issues.append(
                Issue(
                    WARN,
                    "decision_gate",
                    f"unrecognised value {gate!r}; the only opt-out value is "
                    "`decision_gate: off` (suppress the project-stall sweep).",
                )
            )
    target = fm.get("decision_gate_for")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        issues.append(
            Issue(
                WARN,
                "decision_gate_for",
                "should be the project slug this decision task belongs to "
                "(a non-empty string).",
            )
        )
    return issues


# if-then-habits (2026-10-17): five candidates were held 201 times with
# "겹침 미판정 (touches 없음)". Two authoring mistakes produce that: a repo task
# that declares no touches at all (always serialised), and a glob that puts the
# wildcard in the filename (`dir/Foo*Test.kt`). The dispatcher compares globs by
# static prefix only, so the latter reads as all of `dir` and overlaps every
# sibling. Both run fine — they just never run in parallel — so they are WARNs.
_TOUCHES_REQUIRED_MODES = frozenset({"auto", "quick", "delegate"})


def _check_touches_filename_globs(globs: list[str]) -> list[Issue]:
    # The runtime's own prefix rule, not a copy, so this cannot drift from the
    # dispatcher. Lazy for the same cycle reason as task_deps above.
    from .orchestrator.orchestrator import _static_prefix

    issues: list[Issue] = []
    for glob in globs:
        last = glob.strip().strip("/").split("/")[-1]
        if last in ("*", "**") or not any(ch in last for ch in "*?["):
            continue
        prefix = _static_prefix(glob)
        if prefix:
            reach = (
                f"정적 접두 `{prefix}`로 읽는다 — `{prefix}` 아래 전부(형제 파일 "
                "포함)와 겹쳐 직렬화된다. 디렉토리 의도면 "
                f"`{prefix}/**`, 아니면 파일 경로를 하나씩 열거할 것."
            )
        else:
            reach = (
                "정적 접두를 빈 문자열로 읽는다 — 같은 repo의 모든 태스크와 겹쳐 "
                "직렬화된다. 리포 루트 기준 경로를 적을 것."
            )
        issues.append(
            Issue(
                WARN,
                "touches",
                f"{glob!r}: 파일명 컴포넌트에 와일드카드가 있어 디스패처"
                f"(_static_prefix)는 {reach}",
            )
        )
    return issues


def _check_bash_patterns(tools: str) -> list[Issue]:
    """Check Bash(...) rules against how this vault actually delivers them.

    Claude Code documents three trailing-wildcard forms, but only two survive
    the trip through worker.py, which builds argv as `*tools.split()`:

      Bash(git:*)   word-boundary prefix match. The only boundary-enforcing
                    form usable here.
      Bash(git*)    valid, but no word boundary — per the docs `Bash(ls*)`
                    matches `lsof` as well as `ls -la`. Looser than intended.
      Bash(git *)   documented as equivalent to `git:*`, but tools.split()
                    shreds it into the argv tokens `Bash(git` and `*)`,
                    neither of which is a rule. Silently grants nothing.
    """
    issues: list[Issue] = []
    for pattern in _BASH_PATTERN_RE.findall(tools):
        if " " in pattern:
            head = pattern.split()[0]
            issues.append(
                Issue(
                    ERROR,
                    "allowed_tools",
                    f"Bash({pattern}) contains a space. worker.py passes the "
                    "whitelist as *tools.split(), so it arrives as separate argv "
                    "tokens and grants nothing. No space-containing rule works "
                    "here — neither the ` *` wildcard form nor an exact-match "
                    f"rule. Use Bash({head}:*), or narrow the command another way.",
                )
            )
        elif pattern.endswith("*") and not pattern.endswith(":*") and pattern != "*":
            stem = pattern[:-1]
            issues.append(
                Issue(
                    WARN,
                    "allowed_tools",
                    f"Bash({pattern}) has no word boundary, so it also matches "
                    f"commands merely starting with {stem!r} (the docs' example: "
                    f"Bash(ls*) matches lsof). Prefer Bash({stem}:*).",
                )
            )
    return issues


def validate_frontmatter(fm: dict, body: str = "") -> list[Issue]:
    """Check the fields worker.py reads. Order: errors first, then warnings."""
    issues: list[Issue] = []

    raw_tools = fm.get("allowed_tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, str):
            issues.append(
                Issue(
                    ERROR,
                    "allowed_tools",
                    "must be a quoted comma-separated string, not a YAML list. "
                    "The worker only accepts str and would silently fall back to "
                    f'"{_worker.DEFAULT_IMPLEMENT_TOOLS}" — no gh/gcloud/gradlew.',
                )
            )
        else:
            hits = [f for f in _worker.FORBIDDEN_TOOL_SUBSTRINGS if f in raw_tools]
            if hits:
                issues.append(
                    Issue(
                        ERROR,
                        "allowed_tools",
                        f"contains forbidden pattern(s) {', '.join(hits)}; "
                        "the worker refuses to start a stage with these.",
                    )
                )
            issues.extend(_check_bash_patterns(raw_tools))
            issues.extend(_check_keystore_signing(raw_tools, body))

    # `touches` is the exact inverse of the allowed_tools trap above: there a
    # YAML list is wrong (the worker only reads a str), here a bare string is
    # wrong. It must be a NON-EMPTY YAML list of path-glob strings — the
    # serial-per-repo dispatcher reads it to decide whether two same-repo tasks
    # touch disjoint files and may run at once. A bare string, an empty list, or
    # a non-string entry all silently read as "no globs declared", which forces
    # the task to be serialised; flag it rather than let the mistake pass.
    raw_touches = fm.get("touches")
    if raw_touches is not None:
        if isinstance(raw_touches, str):
            issues.append(
                Issue(
                    ERROR,
                    "touches",
                    "must be a YAML list of path globs, not a bare string. A bare "
                    "string declares no globs, so the dispatcher reads it as 'none "
                    "declared' and always serialises the task. Use the list form "
                    "(the inverse of allowed_tools, which must be a string).",
                )
            )
        elif not isinstance(raw_touches, list):
            issues.append(
                Issue(
                    ERROR,
                    "touches",
                    "must be a YAML list of path globs, not "
                    f"{type(raw_touches).__name__}.",
                )
            )
        elif not raw_touches:
            issues.append(
                Issue(
                    ERROR,
                    "touches",
                    "is declared but empty; an empty list reads as 'no globs "
                    "declared'. Drop the field entirely if the task declares none.",
                )
            )
        elif any(not isinstance(x, str) or not x.strip() for x in raw_touches):
            issues.append(
                Issue(
                    ERROR,
                    "touches",
                    "every entry must be a non-empty glob string.",
                )
            )
        else:
            issues.extend(_check_touches_filename_globs(raw_touches))

    # This parser keeps everything after an unquoted value, inline comment and
    # all, so `worker_mode: auto  # auto | off` resolves to the whole string and
    # the task never matches "auto". Prose fields legitimately contain "#"
    # ("PR #8"), so only single-token fields are checked. Reported before the
    # enum check below, which would otherwise also fire and bury the cause.
    contaminated: set[str] = set()
    for scalar in ("worker_mode", "status", "auto_merge", "due", "review_cycle"):
        value = fm.get(scalar)
        if isinstance(value, str) and "#" in value:
            contaminated.add(scalar)
            issues.append(
                Issue(
                    ERROR,
                    scalar,
                    "value contains an inline comment; this parser does not strip "
                    f"it, so the value is literally {value!r}. Move the comment to "
                    "its own line above.",
                )
            )

    mode = fm.get("worker_mode")
    if (
        mode is not None
        and "worker_mode" not in contaminated
        and str(mode).strip().lower() not in known_worker_modes()
    ):
        issues.append(
            Issue(
                WARN,
                "worker_mode",
                f"unrecognised value {mode!r}; expected one of "
                f"{', '.join(sorted(m for m in known_worker_modes() if m))}.",
            )
        )

    # A runnable repo task without touches can never be proven disjoint from
    # its siblings, so the dispatcher serialises it against every other task
    # on that repo. Vault tasks (no repo) and parked modes are not dispatched
    # against siblings this way.
    repo = fm.get("repo")
    if (
        isinstance(repo, str)
        and repo.strip()
        and "worker_mode" not in contaminated
        and str(mode or "").strip().lower() in _TOUCHES_REQUIRED_MODES
        and raw_touches is None
    ):
        issues.append(
            Issue(
                WARN,
                "touches",
                "repo 태스크인데 touches가 없다 — 디스패처가 겹침을 판정할 수 없어"
                "(`겹침 미판정 (touches 없음)`) 같은 repo의 다른 태스크와 항상 "
                "직렬화된다. 이 태스크가 건드릴 경로 glob 리스트를 적을 것 "
                "(예: `src/feature/**`).",
            )
        )

    tier = fm.get("model_tier")
    if tier is not None:
        from .orchestrator.model_select import TIERS

        if isinstance(tier, str) and "#" in tier:
            issues.append(
                Issue(
                    ERROR,
                    "model_tier",
                    "value contains an inline comment; this parser does not strip "
                    f"it, so the value is literally {tier!r}. Move the comment to "
                    "its own line above.",
                )
            )
        elif str(tier).strip().lower() not in TIERS:
            issues.append(
                Issue(
                    WARN,
                    "model_tier",
                    f"unrecognised value {tier!r}; the classifier ignores it and "
                    f"falls through to tags/estimate. Expected one of "
                    f"{', '.join(TIERS)}.",
                )
            )

    criteria = fm.get("acceptance_criteria")
    if criteria is not None and not isinstance(criteria, list):
        issues.append(
            Issue(WARN, "acceptance_criteria", "should be a YAML list of strings.")
        )

    branch = fm.get("worker_branch")
    if isinstance(branch, str) and "<slug>" in branch:
        issues.append(
            Issue(
                WARN,
                "worker_branch",
                "still holds the template placeholder; the worker ignores it and "
                "uses worker/<slug>. Drop the field or fill it in.",
            )
        )

    issues.extend(_check_actions_dependency(fm))
    issues.extend(_check_decision_gate(fm))

    for numeric in ("max_turns", "priority"):
        value = fm.get(numeric)
        if value is not None:
            try:
                int(str(value).strip())
            except (TypeError, ValueError):
                issues.append(Issue(WARN, numeric, f"not an integer: {value!r}"))

    return issues


def _strict_frontmatter_error(text: str) -> str | None:
    """A one-line reason iff this task's frontmatter is strict-YAML broken, else
    None. `validate_frontmatter` is reached through the worker's lenient line
    parser (`worker.parse_frontmatter`), which never raises on malformed YAML —
    so a file whose frontmatter `yaml.safe_load` rejects would pass validation
    even though `load_dispatchable` refuses to dispatch it. Delegating to the
    orchestrator's shared detector makes "validate ERRORs" and "loader skips"
    the exact same set of files. Imported lazily to stay clear of the
    task_deps/cli import cycle this module already dodges."""
    from .orchestrator import repos

    return repos.frontmatter_error_from_text(text)


def validate_file(path: Path) -> list[Issue]:
    text = path.read_text(encoding="utf-8")
    err = _strict_frontmatter_error(text)
    if err is not None:
        return [Issue(ERROR, "frontmatter", f"YAML parse error — {err}")]
    fm, body = _worker.parse_frontmatter(text)
    return validate_frontmatter(fm, body)


def validate_dir(items_dir: Path, *, only_open: bool = True) -> dict[str, list[Issue]]:
    """Validate every task file, keyed by slug. Skips clean files.

    `only_open` exists because a done task's broken whitelist can no longer
    hurt anything — surfacing it is just noise.
    """
    found: dict[str, list[Issue]] = {}
    for path in sorted(items_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        err = _strict_frontmatter_error(text)
        if err is not None:
            # A YAML-broken file can't be parsed for `status`, so the only_open
            # filter can't judge it — surface it unconditionally rather than let
            # a broken file hide behind an unreadable status.
            found[path.stem] = [Issue(ERROR, "frontmatter", f"YAML parse error — {err}")]
            continue
        fm, body = _worker.parse_frontmatter(text)
        if only_open and str(fm.get("status", "")).strip().lower() != "open":
            continue
        issues = validate_frontmatter(fm, body)
        if issues:
            found[path.stem] = issues
    return found
