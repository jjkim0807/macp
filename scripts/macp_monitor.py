#!/usr/bin/env python3
"""macp usage monitor — a *sensor* that surfaces issue-worthy signals.

Runs cheaply on each loop tick, reads local macp runtime state (queue.json,
assignment ledger, daemon logs, orchestrator/planner state) and emits NEW,
*active* candidate findings as JSON on stdout. It does NOT file issues — the
loop model reviews the candidates, applies judgment, and files the genuine ones
via `gh issue create`, then calls `--mark-filed`.

Design notes:
- stdlib only, so it runs in a bare `python3` clone (matches session_log). launchd
  invokes it with the system `/usr/bin/python3`, which has no site-packages.
- The *script* is git-tracked (here, under `scripts/`), but all *runtime state*
  stays under `Worker/_state/macp_monitor/` — see STATE_DIR below. That directory
  is git-ignored on purpose so the vault auto-sync daemons never touch the log
  cursors or the latest report.
- Recency-aware: log scanning reads only the *delta* past a stored byte cursor,
  so historical spam (e.g. the pre-2026-07-10 Supabase errors) is never
  re-reported. First run initializes cursors to EOF and reports nothing from
  logs.
- Dedup: every finding has a stable `fingerprint`. Findings already in
  state.filed OR matching an open GitHub issue (by title marker) are dropped.

Finding severities:
- `bug`         — a real defect / active breakage worth filing.
- `warn`        — an `info` whose explanation has expired: the hold is still the
                  one originally stated, but it has now lasted long enough to
                  need a human nudge. Carries the same fingerprint as the `info`
                  it grew out of, so the climb is not read as a new finding.
- `triage`      — needs a human call; may be designed behavior or a real fault.
- `enhancement` — a papercut worth improving, not broken.
- `info`        — a real signal that is currently *explained* by a known hold, so
                  it must not read as a defect yet. Emitted rather than dropped so
                  the signal survives to be re-judged once the hold clears.

Usage:
  python3 scripts/macp_monitor.py scan            # print NEW candidates as JSON
  python3 scripts/macp_monitor.py scan --all      # all candidates (ignore filed)
  python3 scripts/macp_monitor.py scan --daemon   # launchd mode: always write
                                                  # latest.json, print only if NEW
  python3 scripts/macp_monitor.py mark-filed <fp> <url>  # record a filed issue
  python3 scripts/macp_monitor.py baseline        # set cursors to now, file nothing
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

_INSTALL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_INSTALL_DIR))          # bootstrap — raw install location
from todo_system import paths  # noqa: E402
# Engine root through the resolver: dev checkout -> repo root (same value as the
# bootstrap above); wheel install -> todo_system/_engine, where scripts/ and
# .claude/commands ship in repo layout. The bootstrap only puts the engine on
# sys.path; this is the definition the rest of the module uses.
_INSTALL_DIR = paths.engine_root()
# Data root through the resolver: honours MACP_VAULT_ROOT / .macp/config.toml.
# The start= anchor is paths.vault_search_start(): the engine root in a checkout
# (byte-identical to the former Path(__file__).resolve().parents[1] when unset),
# None in an installed wheel so the cwd-upward search runs instead of anchoring
# on site-packages, which never holds a vault marker.
ROOT = paths.vault_root(start=paths.vault_search_start())   # …/MaestroACP (scripts/ -> vault)
# State deliberately does NOT live next to this script: the script is tracked,
# the state is not. Keep this an explicit path rather than `__file__.parent`,
# or cursors/latest.json silently start landing in tracked `scripts/`.
STATE_DIR = ROOT / "Worker" / "_state" / "macp_monitor"
STATE_FILE = STATE_DIR / "state.json"
LATEST_FILE = STATE_DIR / "latest.json"     # newest report, for `scan --daemon`

QUEUE = ROOT / "Worker" / "queue.json"
ASSIGN_DIR = ROOT / "Worker" / "assignments"
LOG_DIR = ROOT / "logs"
ORCH_LOG = LOG_DIR / "orchestrator.out.log"
STALL = ROOT / "Worker" / "stall.json"
SNAPSHOT = ROOT / "Todo" / "Snapshots" / "latest.json"
SNAPSHOT_STALE_H = 48                               # latest.json older than this = stuck
RELEASE_CHANNELS = ROOT / "Worker" / "release_channels.json"   # human-updated release watermark (tracked)

# Config-key lookup anchored at the vault (its .macp/config.toml pins the slug);
# same file as the engine's in the combined layout, never site-packages in a wheel.
REPO = paths.repo_slug(start=ROOT)
ISSUE_MARKER = "[macp-monitor]"                     # title tag => auto-filed by us

POISON_THRESHOLD = 3
STALE_PENDING_HOURS = 48                            # pending, no assignment, this old
UNNUDGED_PARKED_HOURS = 48                          # awaiting_human, never nudged, this old
LOG_SIZE_WARN_MB = 200                              # unrotated single-file threshold
LOG_DELTA_ERR_MIN = 8                               # min occurrences of a NEW signature
UNMERGED_BRANCH_STALE_HOURS = 12                    # done assignment, branch still unmerged
# Vault branches get a much tighter threshold than repo branches, because they
# are usually waiting for *nobody*. A repo task's branch legitimately sits
# unmerged for hours: report opened a PR and CI has to go green before
# auto-merge fires, so 12h buys that out. A vault task classically had no PR, no
# CI and no auto-merge path at all (`runner.py`'s landed gate is
# `if auto_merge and repo_url:`) — the branch was abandoned the instant the
# run's `finally: restore_main` returned HEAD to main.
# On 2026-09-09 three delegate outputs stranded and a human hit each one within
# hours; `research-staff-cover-letter-draft` (updated 15:07) would not have
# fired until 03:07 the next day. There is nothing to wait for, so 1h.
# ⚠ "no PR to wait on" stopped being universal with `stages.publish_branch()`
# (merged dfc16b95a): a delegate vault branch can now be pushed *and* have a PR
# opened for it. The threshold stays 1h anyway — a branch that really does have
# an open PR is demoted to `info` by check_unmerged_delegate_branch, so raising
# the clock instead would only re-blind the still-common no-PR stranding.
UNMERGED_VAULT_BRANCH_STALE_HOURS = 1               # ditto, vault-local branch (open PR ⇒ demoted, not delayed)
# The age of the *PR*, not of the branch — deliberately independent of the two
# thresholds above, which clock the branch from the assignment's `updated_ts`.
# The `info` demotion those comments refer to (merged 9f5b3cb4a) shipped with no
# age axis at all: a PR open for five minutes and one open for five days both
# read as "리뷰 대기", so the stranding this check exists to catch went silent
# *permanently* the moment a PR existed — the demotion had no way back up. 24h is
# one working day: long enough that an ordinary review-then-merge never trips it,
# short enough that a forgotten PR resurfaces. Past it, the finding keeps its
# fingerprint and rises `info` → `warn` in check_unmerged_delegate_branch.
OPEN_PR_STALE_HOURS = 24                            # open PR at least this old ⇒ warn, not info
RELEASE_FRESHNESS_STALE_DAYS = 7                    # main merge leads beta release by this = stale

# Sentinel for "the PR lookup itself failed", kept distinct from `{}` = "looked,
# and no open PR covers this branch". Compare with `is` — never truthiness.
PR_LOOKUP_FAILED = object()


# --------------------------------------------------------------------------- io
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"filed": {}, "log_cursors": {}, "version": 1}


def save_state(s: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _now() -> dt.datetime:
    return dt.datetime.now()


def _parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _parse_pr_ts(s: str | None) -> dt.datetime | None:
    """A `gh` timestamp (UTC, `...Z`) in the same local-naive frame as `_now()`.

    `_parse_ts` above cannot serve these: it discards the offset via
    `.replace(tzinfo=None)`, which is correct for the local naive stamps the
    ledger writes (`updated_ts`) but wrong for `gh`'s UTC ones — subtracting the
    result from `_now()` (local naive) silently adds the UTC offset, so in KST a
    PR opened 15h ago already measures 24h old and OPEN_PR_STALE_HOURS fires
    9h early. Widening `_parse_ts` itself is not an option: returning an aware
    datetime would make every existing naive subtraction against `_now()` (e.g.
    the `updated` comparison in check_unmerged_delegate_branch) raise TypeError.

    Offset-less input is delegated back to `_parse_ts`, so a stamp without a
    timezone keeps behaving exactly as it always has.
    """
    if not s:
        return None
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return _parse_ts(s)
    return parsed.astimezone().replace(tzinfo=None)


# ---------------------------------------------------------------------- signals
def load_assignments() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not ASSIGN_DIR.exists():
        return out
    for f in ASSIGN_DIR.glob("*.json"):
        a = _read_json(f)
        if isinstance(a, dict) and a.get("task_id"):
            out[a["task_id"]] = a
    return out


def check_queue_ledger_desync(findings: list, q_runs: list, assigns: dict) -> None:
    """A task DONE in the assignment ledger but still `pending` in queue.json —
    the queue entry was never closed out, so it lingers as phantom work.

    DONE only. A `failed` ledger state means the last attempt failed, not that
    the task is over: a capacity/rate-limit failure retries once quota returns,
    so `pending` is the correct queue rendering, and flagging it here would
    contradict `Orchestrator.reconcile_queue()` (PR #28), which deliberately
    mirrors DONE alone. `rejected` likewise still awaits a human call.
    """
    stuck = []
    for r in q_runs:
        if r.get("status") != "pending":
            continue
        a = assigns.get(r.get("slug"))
        if a and a.get("state") == "done":
            stuck.append({"slug": r["slug"], "ledger_state": a["state"],
                          "queue_created": r.get("created_at")})
    if stuck:
        findings.append({
            "fingerprint": "queue-ledger-desync",
            "severity": "bug",
            "title": "queue.json이 완료된 태스크를 `pending`으로 방치 (ledger↔queue 상태 desync)",
            "evidence": stuck,
            "detail": ("assignment ledger는 done인데 Worker/queue.json 항목이 pending으로 남음 "
                       "→ 큐 뷰에 유령 작업이 표시됨. reconcile_queue()가 매 tick 닫아야 하는데 "
                       "남아 있다면 그 경로가 안 돌고 있다는 신호."),
        })


def _git(*args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15)
        return p.returncode, p.stdout.strip()
    except Exception:
        return 1, ""


def _branch_ref_exists(branch: str) -> str | None:
    """Resolve a worker branch to a usable ref (local, else origin/<branch>).
    Only ever resolves for branches that live in THIS repo (the vault itself);
    branches belonging to other repos (e.g. a delegated task's target repo)
    simply won't exist as a ref here, which is what excludes them below —
    this check is scoped to the vault's own worker branches by construction."""
    if _git("rev-parse", "--verify", "--quiet", branch)[0] == 0:
        return branch
    if _git("rev-parse", "--verify", "--quiet", f"origin/{branch}")[0] == 0:
        return f"origin/{branch}"
    return None


def _landed_on_main(task_id: str, ref: str) -> bool:
    """A branch counts as landed if its tip is a literal ancestor of main
    (fast-forward/merge-commit case) OR main has a commit whose message
    mentions the task_id (squash-merge case — the vault's own convention,
    e.g. PR titles/`chore(worker): report — <task_id> ...`). Ancestor-only
    checking false-positives on every squash-merged task since the squash
    commit has a different SHA than the branch tip."""
    if _git("merge-base", "--is-ancestor", ref, "main")[0] == 0:
        return True
    _, out = _git("log", "main", "--oneline", "--grep", task_id, "-i")
    if out.strip():
        return True
    # Patch-equivalence (2026-10-21): every commit the branch has over main
    # already exists on main as the same patch (cherry-picked / rebased in).
    # `git cherry` marks those `-`; one `+` means real unlanded work remains.
    rc, out = _git("cherry", "main", ref)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return rc == 0 and bool(lines) and all(ln.startswith("-") for ln in lines)


def _repo_slug_from_url(url: str | None) -> str | None:
    """`https://github.com/o/r.git` / `git@github.com:o/r` -> `o/r`."""
    if not url:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url.strip())
    return m.group(1) if m else None


def settled_pr_for_branch(branch: str, repo_url: str | None = None) -> dict | None | object:
    """The newest MERGED or CLOSED PR whose head is `branch`, looked up in the
    task's own repo (payload `repo`) and in the vault REPO:
    `{state, number, url, repo}`, `None` when none exists, or PR_LOOKUP_FAILED.

    Why (2026-10-21 brief): all 11 "stranded" branches the sensor escalated as
    `bug` were settled — 6 squash-merged through a PR whose squash subject does
    not carry the slug (so `_landed_on_main`'s `--grep` misses it), 4 closed on
    purpose by a human (superseded or rejected proposal), 1 cancelled. The
    local `worker/*` refs just outlive the PR. A merged PR means the work
    landed; a closed one means a person decided it should not.

    Fail-loud like `open_prs_by_branch`: any `gh` failure answers the sentinel
    so the caller keeps the finding at `bug` instead of muting it.
    """
    repos = []
    for r in (_repo_slug_from_url(repo_url), REPO):
        if r and r not in repos:
            repos.append(r)
    best = None
    for r in repos:
        try:
            out = subprocess.run(
                ["gh", "pr", "list", "--repo", r, "--head", branch, "--state", "all",
                 "--limit", "5", "--json", "number,url,state,createdAt"],
                capture_output=True, text=True, timeout=30, check=True).stdout
            prs = json.loads(out)
        except Exception:
            return PR_LOOKUP_FAILED
        for pr in prs:
            if pr.get("state") not in ("MERGED", "CLOSED"):
                continue
            cand = {"state": pr["state"], "number": pr["number"], "url": pr["url"], "repo": r}
            # A merged PR anywhere wins over a closed one: the work landed.
            if best is None or (cand["state"] == "MERGED" and best["state"] != "MERGED"):
                best = cand
    return best


def check_unmerged_delegate_branch(findings: list, assigns: dict,
                                   open_prs: dict | object | None = None) -> None:
    """A `delegate`/`auto` task finished (assignment state=done) and left a
    worker branch behind, but that branch was never merged into main — the
    real output (e.g. a proposal doc) never lands where the user can see it,
    and the queue entry stays phantom-pending (same root cause class as
    queue-ledger-desync, but the fix is "merge the branch", not "reconcile
    the ledger"). Surfaced 2026-07-31 by worldgraph-phase3-direction-proposal
    sitting unmerged for 24h+.

    ONE FINDING PER TASK, fingerprinted by task_id. It used to emit a single
    aggregate finding under the constant fingerprint "unmerged-delegate-branch",
    which is a latent permanent mute: `scan()` drops any finding whose
    fingerprint is in `state.filed` or appears in an open issue title, so the
    first time a human filed one stranding, *every future stranding forever*
    would be silently swallowed. Every sibling check already avoids this by
    embedding the slug (`poison:<slug>`, `release-freshness:<slug>:<date>`).
    Measured 2026-09-09: `filed` did not yet contain it, so this was a trap
    that had not sprung rather than the cause of that day's misses — the cause
    was the threshold, see below.

    Vault tasks are held to UNMERGED_VAULT_BRANCH_STALE_HOURS instead of the
    repo threshold. `payload["repo"]` is the discriminator the runner itself
    uses (`runner.py:442` `repo_url = payload.get("repo")`), and it is exactly
    the flag on that line's landed gate `if auto_merge and repo_url:` — i.e.
    the tasks that skip the gate are precisely the ones that need the tighter
    watch.

    An OPEN PR whose head is `payload.branch` means the branch was published and
    is waiting on review/merge — not stranded. That finding is demoted to `info`,
    never dropped (module convention: an explained signal stays visible so it can
    be re-judged once the explanation clears — see check_stale_pending). This
    path became reachable for vault tasks with `stages.publish_branch()` (merged
    dfc16b95a), which pushes the delegate branch and opens a PR; before it every
    vault branch genuinely was abandoned, and the check's "nothing to wait on"
    premise held. A lookup *failure* does not demote (fail-loud): a down `gh`
    must never silently mute a real stranding.

    That demotion needs an age axis or it is a permanent mute. As first shipped
    it read a PR open for five minutes and one open for five days identically,
    so "리뷰 대기" became the last thing this check ever said about the task and
    the stranding it exists to catch went quiet for good. Past
    OPEN_PR_STALE_HOURS the same finding therefore rises to `warn` and says how
    long the PR has been open; below it the `info` wording is untouched, and the
    fingerprint is identical on both sides so the climb is not a new finding.
    Age comes from the PR's own `createdAt` through `_parse_pr_ts` (UTC → the
    local naive frame `_now()` uses). A missing or unparseable `createdAt`
    stays `info` — fail-soft, the opposite of the fail-loud lookup failure
    above, and for the opposite reason: there the branch had no explanation at
    all, whereas here an open PR already explains it and only its age is in
    doubt.

    `open_prs` is resolved at call time, the same form as
    `usage_hold_reason(log_path=)` / `check_release_freshness(config_path=)`, so
    tests inject a branch→PR map, `{}`, or PR_LOOKUP_FAILED without ever running
    `gh`. Left as None it is fetched lazily — at most once per scan, and only
    after the first candidate clears the landed gate, so a scan with nothing
    stranded (the overwhelmingly common case) shells out zero times.
    """
    prs = open_prs
    for task_id, a in sorted(assigns.items()):
        if a.get("state") != "done":
            continue
        payload = a.get("payload") or {}
        branch = payload.get("branch")
        if not branch or payload.get("auto_merge") is False:
            continue
        is_vault = not payload.get("repo")
        threshold = UNMERGED_VAULT_BRANCH_STALE_HOURS if is_vault else UNMERGED_BRANCH_STALE_HOURS
        updated = _parse_ts(a.get("updated_ts"))
        if not updated or (_now() - updated).total_seconds() / 3600 < threshold:
            continue
        ref = _branch_ref_exists(branch)
        if ref is None:
            continue                                    # not this repo's branch, or already gone
        if _landed_on_main(task_id, ref):
            continue
        if prs is None:                     # lazy: a candidate exists, now it pays
            prs = open_prs_by_branch()
        # `is not` — never truthiness: `{}` (no open PR) and the failure sentinel
        # are both falsy, and conflating them is what would re-open the false alarm.
        pr = prs.get(branch) if prs is not PR_LOOKUP_FAILED else None
        where = "볼트" if is_vault else "리포"
        evidence = {"task_id": task_id, "branch": branch,
                    "worker_mode": payload.get("worker_mode"),
                    "repo": payload.get("repo"),
                    "is_vault": is_vault,
                    "threshold_hours": threshold,
                    "updated_ts": a.get("updated_ts")}
        if not pr:
            settled = settled_pr_for_branch(branch, payload.get("repo"))
            if settled is not PR_LOOKUP_FAILED and settled:
                if settled["state"] == "MERGED":
                    continue                            # landed through its PR
                findings.append({
                    "fingerprint": f"unmerged-delegate-branch:{task_id}",
                    "severity": "info",
                    "title": (f"{where} 브랜치 미머지(PR 닫힘): {task_id} — "
                              f"{settled['repo']}#{settled['number']}이 머지 없이 닫힘"),
                    "evidence": {**evidence, "pr_number": settled["number"],
                                 "pr_url": settled["url"], "pr_state": "CLOSED"},
                    "detail": ("이 브랜치를 head로 하는 PR이 머지 없이 닫혔다 → 사람이 반영하지 "
                               "않기로 한 결정(대체·기각)이라 좌초가 아니다. 로컬 `worker/*` "
                               f"ref만 남아 있다. PR: {settled['url']}"),
                })
                continue
        if pr:
            # The age of the PR itself, deliberately not the branch clock that
            # already fired above — an open PR explains the branch, so the only
            # question left is whether the *review* moved.
            opened = _parse_pr_ts(pr.get("createdAt"))
            age_h = ((_now() - opened).total_seconds() / 3600
                     if opened is not None else None)
            # `>=`: the threshold is "at least this old". An absent or
            # unparseable `createdAt` leaves age unknown and stays `info` —
            # fail-soft, because inferring `warn` from a missing field would
            # manufacture a false alarm rather than surface a real stall.
            stale = age_h is not None and age_h >= OPEN_PR_STALE_HOURS
            # Same fingerprint as the bug path on purpose: neither the demotion
            # nor this severity climb may read as a NEW finding to scan()'s
            # dedup, or a task would re-fire every time its PR opens, closes or
            # crosses the threshold. Built once and shared by both PR branches so
            # a future wording change cannot drag the fingerprint along with it.
            fingerprint = f"unmerged-delegate-branch:{task_id}"
            pr_evidence = {**evidence,
                           "pr_number": pr["number"], "pr_url": pr["url"],
                           "pr_created_at": pr.get("createdAt"),
                           "pr_open_hours": (round(age_h, 1)
                                             if age_h is not None else None)}
            if stale:
                # Days once it is past two of them; hours below that, where "1일"
                # would round away the difference between 24h and 47h.
                age_txt = f"{round(age_h / 24)}일" if age_h >= 48 else f"{round(age_h)}시간"
                findings.append({
                    "fingerprint": fingerprint,
                    "severity": "warn",
                    "title": (f"{where} 산출물 리뷰 정체: {task_id} — "
                              f"PR #{pr['number']}이 {age_txt}째 열린 채 미머지"),
                    "evidence": pr_evidence,
                    "detail": ("PR이 열려 있는 것 자체는 정상 경로지만, "
                               f"{age_txt}째 그대로면 리뷰가 멈춘 것이다 — 산출물은 여전히 "
                               "main에 없고 queue.json도 phantom pending으로 남는다. "
                               f"PR: {pr['url']}\n"
                               "사람이 할 일: PR을 머지하거나(산출물이 main에 반영된다) "
                               "닫아라(더 필요 없다는 판단). 어느 쪽이든 이 finding은 사라진다. "
                               "브랜치를 손으로 merge하거나 지우지 말 것 — PR을 통하면 된다.\n"
                               f"의도적으로 오래 열어두는 PR이라면 {OPEN_PR_STALE_HOURS}h "
                               "기준(OPEN_PR_STALE_HOURS)이 이 리포에 안 맞는다는 뜻이다."),
                })
                continue
            findings.append({
                "fingerprint": fingerprint,
                "severity": "info",
                "title": (f"{where} 산출물 리뷰 대기: {task_id} — PR #{pr['number']} 열림, "
                          f"{threshold}h+ 미머지"),
                "evidence": pr_evidence,
                "detail": ("assignment ledger state=done이고 payload.branch가 아직 main에 없지만, "
                           "이 브랜치를 head로 하는 열린 PR이 있다 → 버려진 게 아니라 리뷰/머지 "
                           f"대기 상태다. PR: {pr['url']}\n"
                           "사람이 할 일: PR을 리뷰해서 머지하면 브랜치가 main에 반영되며 이 "
                           "finding도 사라진다. 브랜치를 손으로 merge하거나 지우지 말 것 — "
                           "PR이 열려 있는 동안은 정상 경로다."),
            })
            continue
        findings.append({
            "fingerprint": f"unmerged-delegate-branch:{task_id}",
            "severity": "bug",
            "title": (f"{where} 산출물 좌초: {task_id} — worker 브랜치가 "
                      f"{threshold}h+ main에 미머지"),
            "evidence": evidence,
            "detail": ("assignment ledger state=done인데 payload.branch가 main에 안 landed(ancestor도 "
                       "아니고 main에 task_id 언급 커밋도 없음) → 산출물이 main에 반영 안 됨 + "
                       "queue.json도 phantom pending으로 남음. 이 브랜치를 head로 하는 열린 PR도 "
                       "없고(있으면 info로 강등된다) 볼트 태스크는 auto_merge 경로도 없어"
                       "(runner의 landed 게이트가 `auto_merge and repo_url`) 브랜치가 "
                       "그대로 버려진다.\n"
                       "선분류: `git -C " + str(ROOT) + " diff --stat main " + branch + "` — "
                       "출력이 **삭제 일색**이면 main이 상위집합이라 산출물은 이미 반영된 것이고"
                       "(사람이 브랜치를 안 머지한 채 내용만 손으로 옮긴 경우: 2026-09-09 "
                       "research-staff-cover-letter-draft가 실제 사례) 브랜치만 지우면 된다. "
                       "세 점 diff(`main..." + branch + "`)로 판정하지 말 것 — squash 머지된 "
                       "브랜치도 전체 diff가 그대로 남아 오탐한다.\n"
                       "복구(진짜 좌초일 때): ① 먼저 `pgrep -fl " + task_id + "` 가 "
                       "**비어 있는지 확인**(실행 중 워커의 체크아웃을 건드리면 레이스) "
                       "② `git -C " + str(ROOT) + " merge --no-ff " + branch + "`"),
        })


TICK_RE = re.compile(r"candidates=\d+ dispatched=\d+")
_HOLD_TOKEN = "USAGE-HOLD ("


def usage_hold_reason(log_path: Path | None = None) -> str | None:
    """The reason the orchestrator's most recent tick was held by the usage
    guard, or None if that tick was not held (or nothing is readable).

    "Most recent tick" is the *last* line matching TICK_RE — deliberately a
    single line, not "any of the last N". A hold that has since cleared must
    stop suppressing findings immediately; looking back N lines would keep
    explaining away a stall for N ticks after quota recovered.

    The reason is taken from the token to end-of-line because it *nests
    parentheses* — the live format is

        ... USAGE-HOLD (weekly 78% ahead of pace (66% elapsed + 12pp allowance))

    so a `\\(([^)]*)\\)` regex truncates at the inner ')'. Reading to EOL and
    dropping the final ')' is safe precisely because orchestrator.py appends
    this token last on the line (see `auto` command, `budget` suffix).

    `log_path` is resolved at call time rather than defaulted to ORCH_LOG at
    def time, so tests can point it at a fixture without monkeypatching a
    module constant frozen at import.
    """
    path = ORCH_LOG if log_path is None else log_path
    try:
        tail = path.read_text(errors="replace").splitlines()[-40:]
    except Exception:
        return None
    for line in reversed(tail):
        if not TICK_RE.search(line):
            continue
        if _HOLD_TOKEN not in line:
            return None                      # newest tick ran unheld
        reason = line.split(_HOLD_TOKEN, 1)[1].rstrip()
        if reason.endswith(")"):             # not str.removesuffix: this runs
            reason = reason[:-1]             # under whatever bare python3 launchd has
        return reason or None
    return None


def check_stale_pending(findings: list, q_runs: list, assigns: dict,
                        hold_reason: str | None = None) -> None:
    """Pending for a long time with NO assignment at all — genuinely stuck new
    work (unroutable slug, planner/orchestrator not picking it up).

    When the orchestrator is currently held by the usage guard (`hold_reason`),
    "pending with no assignment" is the *expected* rendering of paced work, not
    a routing failure — so the finding is demoted to `info` rather than dropped.
    Dropping it would erase the signal exactly when a genuinely stuck task is
    hardest to tell from a merely-paced one; keeping it visible lets the next
    scan after the hold clears re-report it at full `bug` severity.

    The fingerprint deliberately does NOT encode hold state: mixing it in would
    make the same stuck task re-fire as a "new" finding every time the hold
    toggles, defeating the state.filed dedup.
    """
    now = _now()
    stale = []
    for r in q_runs:
        if r.get("status") != "pending" or r.get("slug") in assigns:
            continue
        c = _parse_ts(r.get("created_at"))
        if c and (now - c).total_seconds() / 3600 >= STALE_PENDING_HOURS:
            stale.append({"slug": r["slug"], "age_h": round((now - c).total_seconds() / 3600, 1),
                          "mode": r.get("mode")})
    if stale:
        detail = ("pending인데 assignment ledger에 항목이 아예 없음 → 디스패치 자체가 안 됨"
                  "(라우팅 불가/누락 의심).")
        if hold_reason:
            detail += (f" (단, 최근 orchestrator tick이 USAGE-HOLD 중 — 보류 사유: {hold_reason}. "
                       "지금은 페이싱으로 설명되므로 info로 강등. 홀드 해제 후에도 남으면 실제 막힘.)")
        findings.append({
            "fingerprint": "stale-pending:" + ",".join(sorted(x["slug"] for x in stale)),
            "severity": "info" if hold_reason else "bug",
            "title": f"{len(stale)}개 태스크가 assignment 없이 {STALE_PENDING_HOURS}h+ pending에 고착",
            "evidence": stale,
            "detail": detail,
        })


def check_unnudged_parked(findings: list, assigns: dict) -> None:
    """Parked for a human but never actually nudged — the escalation is mute.

    `awaiting_human` means "human parked, or AI escalated for input"
    (assignment.py). `renudge_due` is what turns that state into a kakao ping on
    a 1d/3d/7d/14d backoff, so a parked assignment two days old that still has
    `nudge_index == 0` means the notification path never fired at all: the work
    is not merely waiting on someone, it is invisible to them.

    Deliberately narrow. It does NOT flag parked work as such — waiting on a
    human is a legitimate resting state that can last weeks, and firing on age
    alone would turn every correctly-nudged task into a false positive. The
    defect being detected is the *silence*, so any nudge at all (`nudge_index >
    0`) clears it no matter how old the assignment gets. Poisoned assignments
    are left to `check_poison`, which already reports them on their own path.

    Motivated by 2026-09-06: `renudge_due` filtered on the ASSIGNEE being human,
    which skipped everything an AI agent had escalated — those assignments name
    the AI, not the person. parent-match-app-16b sat 10.4 days, code complete
    and every gate green, without a single nudge; `stale.py` missed it too
    because it only looks for tasks with NO assignment.
    """
    now = _now()
    mute = []
    for a in assigns.values():
        if a.get("state") != "awaiting_human" or a.get("nudge_index", 0) > 0:
            continue
        if a.get("refail_count", 0) >= POISON_THRESHOLD:
            continue                                  # check_poison owns these
        since = _parse_ts(a.get("updated_ts") or a.get("created_ts"))
        if not since:
            continue
        age_h = (now - since).total_seconds() / 3600
        if age_h >= UNNUDGED_PARKED_HOURS:
            mute.append({"slug": a.get("task_id"), "age_h": round(age_h, 1),
                         "agent_id": a.get("agent_id"),
                         "blocked_on": (a.get("blocked_on") or "")[:160]})
    if mute:
        findings.append({
            "fingerprint": "unnudged-parked:" + ",".join(sorted(x["slug"] for x in mute)),
            "severity": "bug",
            "title": f"{len(mute)}개 파킹 태스크가 {UNNUDGED_PARKED_HOURS}h+ 동안 한 번도 nudge되지 않음",
            "evidence": mute,
            "detail": "awaiting_human인데 nudge_index=0 → renudge 경로가 아예 안 돌았다는 뜻. "
                      "사람이 기다리는 상태가 아니라 사람이 모르는 상태다. "
                      "registry projects 라우팅과 renudge_due 필터부터 확인할 것.",
        })


def check_poison(findings: list, assigns: dict) -> None:
    """Tasks quarantined (awaiting_human, refail>=threshold). These self-notify
    via kakao, but a recurring poison on macp's OWN repos is worth a tracked
    issue (vs one-off downstream flakes)."""
    poisoned = [
        {"slug": a["task_id"], "refail": a.get("refail_count", 0),
         "last_note": (a.get("last_note") or "")[:200]}
        for a in assigns.values()
        if a.get("state") == "awaiting_human" and a.get("refail_count", 0) >= POISON_THRESHOLD
    ]
    for p in poisoned:
        findings.append({
            "fingerprint": f"poison:{p['slug']}",
            "severity": "triage",
            "title": f"태스크 격리(poison): {p['slug']} — {p['refail']}회 재실패",
            "evidence": p,
            "detail": "반복 실패로 자동 격리됨(AWAITING_HUMAN). macp 자체 repo/파이프라인 원인이면 이슈로, "
                      "일회성 하류 flake면 무시.",
        })


def check_pre_poison(findings: list, assigns: dict) -> None:
    """Tasks one refail away from quarantine — the LAST tick a human can still
    act on the cause instead of the corpse.

    `check_poison` above only fires at `awaiting_human AND refail>=threshold`,
    i.e. after the quarantine already happened. That left the most informative
    window invisible: on 2026-09-01 mulkko-5-ladder-editor sat at refail 2 in
    `failed` for hours while the CLI's rebranded quota notice ("You've reached
    your Fable 5 limit …") was being misfiled as a real "missing verify.md"
    defect — a *classifier* bug in stages._CAPACITY_MARKERS, not a task defect.
    Nothing surfaced it; it was found by hand. The log-delta check could not
    help either: the runner writes `FAIL: …` (which its error regex did not
    match) and only 5 times (under LOG_DELTA_ERR_MIN).

    So this is deliberately ledger-based, not log-based: the ledger states the
    fact exactly, with no threshold to tune. `last_note` rides along in the
    title because that string IS the diagnosis — a note that looks like a stage
    artifact problem on a task that got that far is the signature of a
    misclassified capacity stall.
    """
    for a in assigns.values():
        refail = a.get("refail_count", 0)
        if a.get("state") != "failed" or refail < POISON_THRESHOLD - 1:
            continue
        if a.get("capacity_limited") or a.get("job_running") or a.get("checkout_busy"):
            continue        # deferred by design — these never escalate to poison
        slug = a["task_id"]
        note = (a.get("last_note") or "")[:200]
        findings.append({
            "fingerprint": f"pre-poison:{slug}",
            "severity": "triage",
            "title": f"격리 임박: {slug} — {refail}/{POISON_THRESHOLD}회 재실패 · {note[:60]}",
            "evidence": {"slug": slug, "refail": refail,
                         "threshold": POISON_THRESHOLD, "last_note": note},
            "detail": "다음 실패에 자동 격리된다. 같은 사유로 반복 실패 중이면 태스크 결함이 아니라 "
                      "파이프라인/분류기 버그일 수 있으니 run.log의 실제 출력을 먼저 확인할 것.",
        })


LOG_TS = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _err_signature(line: str) -> str:
    """Normalize an error line into a stable signature (strip volatile bits)."""
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:.+]+", "", line)
    s = re.sub(r"0x[0-9a-fA-F]+", "", s)
    s = re.sub(r"\d+", "N", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160]


# `FAIL` (the runner's own failure prefix) and the CLI's quota notices are here
# because neither matched the original set: `FAIL: stage verify: …` is not the
# word "failed", and "You've reached your … limit. Run /usage-credits" contains
# none of them. Both were live blind spots on 2026-09-01. Word-bounded so
# ordinary prose ("no failures") stays out.
ERR_RE = re.compile(r"error|traceback|exception|fatal|failed|\bFAIL\b"
                    r"|/usage-credits|out of usage credits", re.I)


def check_log_deltas(findings: list, state: dict, baseline: bool) -> None:
    """Read only NEW bytes of each daemon log past a stored cursor; flag NEW
    recurring error signatures. Naturally ignores historical spam."""
    cursors = state.setdefault("log_cursors", {})
    if not LOG_DIR.exists():
        return
    for log in sorted(LOG_DIR.glob("*.out.log")):
        try:
            size = log.stat().st_size
        except OSError:
            continue
        key = log.name
        start = cursors.get(key, 0)
        if start > size:                       # log rotated/truncated
            start = 0
        if baseline:
            cursors[key] = size
            continue
        sigs: dict[str, int] = {}
        if size > start:
            with log.open("r", errors="replace") as fh:
                fh.seek(start)
                for line in fh:
                    if ERR_RE.search(line):
                        sigs[_err_signature(line)] = sigs.get(_err_signature(line), 0) + 1
        cursors[key] = size
        for sig, n in sigs.items():
            if n >= LOG_DELTA_ERR_MIN:
                findings.append({
                    "fingerprint": f"log-err:{key}:{abs(hash(sig)) % 10**8}",
                    "severity": "triage",
                    "title": f"{key}에 새 반복 에러 {n}회: {sig[:70]}",
                    "evidence": {"log": key, "count": n, "signature": sig},
                    "detail": "직전 스캔 이후 새로 발생한 반복 에러 시그니처. 활성 회귀/버그일 수 있음.",
                })


def check_log_growth(findings: list) -> None:
    if not LOG_DIR.exists():
        return
    for log in LOG_DIR.glob("*.log"):
        try:
            mb = log.stat().st_size / 1024 / 1024
        except OSError:
            continue
        if mb >= LOG_SIZE_WARN_MB:
            findings.append({
                "fingerprint": f"log-growth:{log.name}",
                "severity": "enhancement",
                "title": f"로그 파일 미회전으로 비대: {log.name} ({mb:.0f}MB)",
                "evidence": {"log": log.name, "mb": round(mb, 1)},
                "detail": "단일 로그 파일이 회전 없이 무한 증가 → 디스크/그렙 부담. 로테이션 도입 검토.",
            })


_IN_FLIGHT_STATES = ("dispatched", "accepted", "in_progress")


def check_orchestrator_noop(findings: list, assigns: dict) -> None:
    """Persistent `candidates>0 dispatched=0` in the orchestrator log tail =
    the scheduler keeps finding candidates it can never dispatch.

    `dispatched=0` is the *correct* outcome far more often than not, so this
    only fires when nothing explains it. A task stays a candidate (its todo is
    still `open`) while it is held for any of these reasons:

    - **in flight** — dedup refuses to re-dispatch an active assignment. This is
      the common case: any task actually running keeps `dispatched=0` on every
      tick until it finishes;
    - **capacity-limited** — held through an exponential cooldown (up to 6h)
      after a usage/rate limit; a correct backoff, not a stuck dispatcher;
    - **awaiting_human** — parked on purpose (auto_merge didn't land, or a
      poison quarantine) and must not be re-dispatched.

    This check false-positived on all three before they were enumerated
    (2026-07-31/08-01), which is worth remembering if it fires: with these
    suppressed, a firing means candidates exist that are neither running, nor
    cooling down, nor parked — i.e. genuinely unroutable or cooldown-stuck.
    """
    if not ORCH_LOG.exists():
        return
    held_on_purpose = any(
        a.get("state") in _IN_FLIGHT_STATES
        or a.get("state") == "awaiting_human"
        or (a.get("capacity_limited") and a.get("state") == "failed")
        for a in assigns.values()
    )
    if held_on_purpose:
        return
    try:
        tail = ORCH_LOG.read_text(errors="replace").splitlines()[-40:]
    except Exception:
        return
    # A tick held by the usage guard prints USAGE-HOLD on the same line —
    # dispatched=0 is then the *intended* outcome (quota pacing), not a stall.
    noop = [ln for ln in tail
            if re.search(r"candidates=[1-9]\d* dispatched=0", ln)
            and "USAGE-HOLD" not in ln]
    if len(noop) >= 6:
        findings.append({
            "fingerprint": "orchestrator-noop-dispatch",
            "severity": "triage",
            "title": "오케스트레이터가 candidate를 계속 못 디스패치 (candidates>0 dispatched=0 반복)",
            "evidence": {"recent_noop_lines": len(noop), "sample": noop[-1]},
            "detail": "최근 tick에서 candidate는 있으나 dispatched=0 반복 — capacity 대기도 "
                      "awaiting_human 파킹도 아니다. 라우팅 불가·쿨다운 고착 등 실제 막힘일 수 있음.",
        })


def check_snapshot_staleness(findings: list) -> None:
    """Todo/Snapshots/latest.json is produced by scripts/export_db_snapshot.py for
    the brief agent. If it stops refreshing, the brief silently reads stale data."""
    if not SNAPSHOT.exists():
        return
    age_h = (_now().timestamp() - SNAPSHOT.stat().st_mtime) / 3600
    if age_h >= SNAPSHOT_STALE_H:
        findings.append({
            "fingerprint": "snapshot-export-stale",
            "severity": "bug",
            "title": f"Todo/Snapshots/latest.json이 {age_h/24:.0f}일+ stale (스냅샷 export 중단)",
            "evidence": {"path": "Todo/Snapshots/latest.json", "age_hours": round(age_h, 1),
                         "mtime": dt.datetime.fromtimestamp(SNAPSHOT.stat().st_mtime).isoformat(timespec="seconds")},
            "detail": "export_db_snapshot.py가 스냅샷을 갱신하지 못함(Supabase 의존 잔재 의심). "
                      "brief 에이전트가 stale 데이터를 읽게 됨.",
        })


def check_usage_guard(findings: list) -> None:
    """todo_system/usage_guard.py paces Claude Code usage to the refill window.
    Two states are worth a human's attention:

    - **hard-cap block** — automation is stopped until the window resets
      (possibly days for the weekly cap). Working as designed, but the human
      should know the pipeline is parked and why.
    - **blind guard** — the guard has failed to fetch usage for 24h+ and is
      fail-open, i.e. pacing protection is silently OFF (expired OAuth token,
      endpoint change). This is exactly the class of silent degradation that
      let the weekly quota burn out unnoticed.

    Ordinary pace-blocks (utilization ahead of elapsed% + allowance) are
    routine self-clearing behavior and are NOT surfaced.
    """
    st = _read_json(ROOT / "Worker" / "_state" / "usage_guard" / "latest.json")
    if not isinstance(st, dict):
        return
    decision = st.get("decision") or {}
    reason = decision.get("reason") or ""
    if not decision.get("allowed", True) and "hard cap" in reason:
        findings.append({
            "fingerprint": "usage-guard-hard-cap",
            "severity": "triage",
            "title": "usage guard: 구독 사용량 hard cap — 자동화가 리셋까지 정지됨",
            "evidence": {"reason": reason,
                         "windows": decision.get("windows", [])},
            "detail": "설계된 동작(사람 몫 확보)이지만 리셋까지 파이프라인이 멈춘다. "
                      "의도적 대량 사용이었는지, 임계 조정이 필요한지 판단할 것.",
        })
    fetched_at = st.get("fetched_at")
    attempted = st.get("attempted_at")
    if st.get("error") and attempted:
        blind_h = (_now().timestamp() - float(fetched_at or 0)) / 3600
        if blind_h >= 24:
            findings.append({
                "fingerprint": "usage-guard-blind",
                "severity": "bug",
                "title": f"usage guard가 {blind_h/24:.0f}일+ 사용량을 못 읽는 중 (fail-open, 페이싱 보호 꺼짐)",
                "evidence": {"error": st.get("error"),
                             "last_success_hours_ago": round(blind_h, 1)},
                "detail": "가드는 fail-open이라 자동화는 계속 돌지만 페이싱 보호가 사실상 꺼진 상태. "
                          "OAuth 토큰 만료/엔드포인트 변경 의심 — usage_guard status로 확인.",
            })


def check_stall(findings: list) -> None:
    s = _read_json(STALL)
    if isinstance(s, dict) and s.get("consecutive_count", 0) >= 3 and not s.get("notified"):
        findings.append({
            "fingerprint": "stall:" + ",".join(sorted(s.get("files", []))),
            "severity": "triage",
            "title": f"planner stall {s.get('consecutive_count')}회 연속 (동일 파일 재제안)",
            "evidence": s,
            "detail": "플래너가 같은 파일 세트를 반복 제안(진전 없음). 루프/막힘 가능성.",
        })


def _reflog_last_epoch(repo_dir: str, branch: str) -> dt.datetime | None:
    """Last-line epoch of `<repo>/.git/logs/refs/heads/<branch>`, as a naive
    local datetime (consistent with `_now()`/`_parse_ts`).

    The reflog is append-only and its last line records when that checkout
    last gained new commits — readable with stdlib text handling, no `git`
    subprocess. Anything unreadable (missing checkout, a linked worktree's
    `.git` *file*, absent branch, malformed line) returns None: callers must
    skip, never alarm, on absent data.
    """
    try:
        log = Path(repo_dir) / ".git" / "logs" / "refs" / "heads" / branch
        last = ""
        for line in log.read_text(errors="replace").splitlines():
            if line.strip():
                last = line
        if not last:
            return None
        # "<old-sha> <new-sha> Name <email> <epoch> <tz>\t<action>: <msg>"
        return dt.datetime.fromtimestamp(int(last.split("\t", 1)[0].split()[-2]))
    except Exception:
        return None


def check_release_freshness(findings: list, config_path: Path | None = None) -> None:
    """An app whose main has moved past its beta-channel release by
    RELEASE_FRESHNESS_STALE_DAYS+ days — merged work sitting unpublished.

    Motivation (2026-09-03): DanceMirror's release channel froze on 07-22 while
    three features merged on 07-23/07-30, and nobody noticed for six weeks —
    no monitored axis covered "merged vs released". This check is that axis.

    Local data only, so the sensor stays stdlib-only and offline:
    - merge side: `_reflog_last_epoch` over the app's *release-build* checkouts
      (`repo_dirs`, max wins). Releases are built from those same checkouts, so
      "reflog epoch >> released_at" genuinely means unreleased work. Worker/
      clones are deliberately not configured — their late pulls would read as
      fresh merges and false-alarm.
    - release side: the human-updated watermark in Worker/release_channels.json
      (`released_at`, refreshed at publish time — keeping that watermark
      current IS the manual-publish practice this check leans on).

    Missing data never alarms (skip), with one deliberate exception: a missing
    `released_at` surfaces as `info`, because an app without a watermark is
    invisible to this check and a human must backfill it. The stale fingerprint
    embeds the released_at date so a NEW staleness episode (against a newer
    watermark) re-fires even after an older episode was filed and resolved.

    `config_path` is resolved at call time (same reason as `usage_hold_reason`):
    tests point it at a fixture without monkeypatching module constants.
    """
    cfg = _read_json(RELEASE_CHANNELS if config_path is None else config_path)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("apps"), list):
        return
    for app in cfg["apps"]:
        if not isinstance(app, dict) or not app.get("slug"):
            continue
        slug = app["slug"]
        channel = app.get("channel") or "?"
        released = _parse_ts(app.get("released_at"))
        if released is None:
            findings.append({
                "fingerprint": f"release-freshness:no-release:{slug}",
                "severity": "info",
                "title": f"{slug}: 베타 채널 발행 기록 없음 (released_at 미기재)",
                "evidence": {"slug": slug, "channel": channel,
                             "released_at": app.get("released_at"),
                             "config": "Worker/release_channels.json"},
                "detail": "머지↔발행 비교가 불가능한 상태. 마지막 발행 시각을 확인해 "
                          "Worker/release_channels.json의 released_at에 백필할 것 — "
                          "이 항목이 남아 있는 한 이 앱의 배포 채널 침묵은 감지되지 않는다.",
            })
            continue
        branch = app.get("branch") or "main"
        dirs = app.get("repo_dirs")
        if not isinstance(dirs, list):
            dirs = []
        epochs = [e for e in (_reflog_last_epoch(d, branch) for d in dirs)
                  if e is not None]
        if not epochs:
            continue                # no readable merge source → skip, never alarm
        merged = max(epochs)
        lead = merged - released
        if lead < dt.timedelta(days=RELEASE_FRESHNESS_STALE_DAYS):
            continue
        findings.append({
            "fingerprint": f"release-freshness:{slug}:{released.date().isoformat()}",
            "severity": "triage",
            "title": f"{slug}: main 머지가 베타 발행({channel})보다 {lead.days}일 앞섬 — 미발행 작업 방치",
            "evidence": {"slug": slug, "channel": channel,
                         "merged_at": merged.isoformat(timespec="seconds"),
                         "released_at": released.isoformat(timespec="seconds"),
                         "lead_days": lead.days,
                         "repo_dirs": dirs},
            "detail": "릴리즈 빌드 체크아웃의 reflog상 최신 머지가 released_at 워터마크보다 "
                      f"{RELEASE_FRESHNESS_STALE_DAYS}일+ 새로움 → 머지된 작업이 베타 채널에 "
                      "안 올라간 채 방치. 발행 후에는 Worker/release_channels.json의 "
                      "released_at(가능하면 released_version도)을 갱신할 것 — DanceMirror가 "
                      "6주간 조용히 어긋났던 바로 그 축이다.",
        })


# ------------------------------------------------------------------------- dedup
def open_issue_titles() -> set[str]:
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--repo", REPO, "--state", "open",
             "--limit", "100", "--json", "title"],
            capture_output=True, text=True, timeout=30, check=True).stdout
        return {i["title"] for i in json.loads(out)}
    except Exception:
        return set()


def open_prs_by_branch() -> dict | object:
    """Open PRs on REPO indexed by head branch:
    `{headRefName: {number, url, createdAt}}`.

    Returns the PR_LOOKUP_FAILED sentinel — NOT an empty dict — when `gh` is
    missing, errors, times out, or returns something unparseable.

    ⚠ Deliberately not the fail-quiet shape of its sibling `open_issue_titles()`,
    which swallows failure into an empty set. Here `{}` is load-bearing: it
    asserts "looked, and no open PR covers this branch", which is exactly the
    condition that keeps check_unmerged_delegate_branch at severity `bug`.
    Collapsing a failed lookup into `{}` would make a down `gh` silently claim
    "no PR exists" and re-fire the false stranding alarm this seam exists to
    suppress. Failure is therefore fail-loud via a sentinel the caller must
    compare with `is`. Do not "simplify" this back into the `set()` form.

    stdlib only (subprocess + json), like the rest of this module — launchd runs
    it under a bare `/usr/bin/python3` with no site-packages.
    """
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--repo", REPO, "--state", "open",
             "--limit", "100", "--json", "number,url,headRefName,createdAt"],
            capture_output=True, text=True, timeout=30, check=True).stdout
        # headRefName verbatim as the key. A cross-repo (fork) head would collide
        # on the bare branch name, but REPO is single-owner private so no fork
        # heads exist; and a mis-key demotes rather than drops, so no signal is
        # lost either way. Hence no `isCrossRepository` filter.
        # `createdAt` via `.get`, unlike its two siblings: a payload missing it
        # must degrade to "age unknown" (⇒ stays `info`, fail-soft) instead of
        # raising KeyError into the except below, which would answer the fail-loud
        # sentinel and re-fire the very `bug` alarm this seam exists to suppress.
        return {p["headRefName"]: {"number": p["number"], "url": p["url"],
                                   "createdAt": p.get("createdAt")}
                for p in json.loads(out)}
    except Exception:
        return PR_LOOKUP_FAILED


# --------------------------------------------------------------------------- run
def scan(show_all: bool = False, baseline: bool = False, daemon: bool = False) -> None:
    state = load_state()
    q = _read_json(QUEUE) or {}
    q_runs = q.get("runs", []) if isinstance(q, dict) else []
    assigns = load_assignments()

    findings: list[dict] = []
    check_queue_ledger_desync(findings, q_runs, assigns)
    check_unmerged_delegate_branch(findings, assigns)
    check_stale_pending(findings, q_runs, assigns, usage_hold_reason())
    check_poison(findings, assigns)
    check_unnudged_parked(findings, assigns)
    check_pre_poison(findings, assigns)
    check_orchestrator_noop(findings, assigns)
    check_usage_guard(findings)
    check_snapshot_staleness(findings)
    check_stall(findings)
    check_release_freshness(findings)
    check_log_deltas(findings, state, baseline)
    check_log_growth(findings)

    save_state(state)                       # persist advanced log cursors

    if baseline:
        print(json.dumps({"baseline": True, "cursors_set": len(state["log_cursors"])},
                         ensure_ascii=False))
        return

    filed = state.get("filed", {})
    titles = open_issue_titles()
    new = []
    for f in findings:
        if show_all:
            new.append(f)
            continue
        if f["fingerprint"] in filed:
            continue
        if any(ISSUE_MARKER in t and f["fingerprint"] in t for t in titles):
            continue
        new.append(f)

    report = {"scanned_at": _now().isoformat(timespec="seconds"),
              "queue_runs": len(q_runs), "assignments": len(assigns),
              "new_candidates": new, "total_findings": len(findings)}

    if daemon:
        # Unattended (launchd) mode. The sensor still never files anything — it
        # parks the latest report where a session can pick it up, and only writes
        # to stdout (= the daemon log) when there is something new, so an idle
        # system leaves no noise to scroll past.
        LATEST_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        if not new:
            return

    print(json.dumps(report, ensure_ascii=False, indent=2))


def mark_filed(fp: str, url: str) -> None:
    state = load_state()
    state.setdefault("filed", {})[fp] = {"url": url, "at": _now().isoformat(timespec="seconds")}
    save_state(state)
    print(f"marked filed: {fp} -> {url}")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "scan"
    if cmd == "scan":
        scan(show_all="--all" in argv, daemon="--daemon" in argv)
    elif cmd == "baseline":
        scan(baseline=True)
    elif cmd == "mark-filed" and len(argv) >= 4:
        mark_filed(argv[2], argv[3])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
