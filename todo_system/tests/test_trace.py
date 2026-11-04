"""Language of the `orchestrator trace` view.

`trace` prints English on a vault with no `language` (like `auto`/`status`/
`agents`) and Korean on a ko vault — the Korean byte for byte what it was before
the text moved into ``i18n.MESSAGES``. Every test sets or clears
``MACP_LANGUAGE`` itself, so a host env or a real vault cannot decide the result.
All fixture text is ASCII: any Hangul in an en render came from the engine.
"""

from __future__ import annotations

import re
from pathlib import Path

from todo_system import i18n, paths
from todo_system.orchestrator import dashboard, stages, trace
from todo_system.orchestrator.assignment import Assignment, Ledger

SLUG = "demo-task"
RUN1 = f"20260101T000000-{SLUG}"
RUN2 = f"20260102T000000-{SLUG}"
_HANGUL = re.compile("[가-힣]")

MERGED_PARK = [{"ts": "2026-01-02T00:00:00+00:00",
                "type": dashboard._MERGED_BUT_BLOCKED, "from": "orchestrator"}]


def _runs(tmp_path: Path) -> Path:
    """Two runs: run 1 wrote research and verified FAIL; run 2 inherited the
    research, carries an unstamped foreign-author plan and an author-less
    run.log, and verified PASS."""
    runs = tmp_path / "runs"
    for rid in (RUN1, RUN2):
        (runs / rid).mkdir(parents=True)
    research = stages.STAGE_ARTIFACT["research"]
    (runs / RUN1 / research).write_text(
        f"---\nstage: research\nrun_id: {RUN1}\n---\n\n# R\n", encoding="utf-8")
    stages.record_stage(runs / RUN1, "research")
    (runs / RUN2 / research).write_text(
        f"---\nstage: research\nrun_id: {RUN1}\n---\n\n# R\n", encoding="utf-8")
    stages.stamp_inherited(runs / RUN2 / research, RUN1)
    stages.record_stage(runs / RUN2, "research")
    (runs / RUN2 / stages.STAGE_ARTIFACT["plan"]).write_text(
        f"---\nstage: plan\nrun_id: {RUN1}\n---\n\n# P\n", encoding="utf-8")
    stages.record_stage(runs / RUN2, "plan")
    (runs / RUN2 / stages.STAGE_ARTIFACT["implement"]).write_text(
        "\n=== implement (rc=0) ===\nout\n", encoding="utf-8")
    stages.record_stage(runs / RUN2, "implement")
    for rid, verdict in ((RUN1, "fail"), (RUN2, "pass")):
        (runs / rid / stages.STAGE_ARTIFACT["verify"]).write_text(
            f"---\nstage: verify\nrun_id: {rid}\noverall_status: {verdict}\n---\n",
            encoding="utf-8")
        stages.record_stage(runs / rid, "verify")
    return runs


def _ledger(tmp_path: Path, **kw) -> Ledger:
    led = Ledger(tmp_path / "assignments")
    led.save(Assignment(task_id=SLUG, agent_id="w", corr_id="c", **kw))
    return led


def _boom(*a, **k):
    raise OSError("lookup unavailable")


def _render(tmp_path: Path, ledger=None, *, slug=SLUG, merged=None, pushed=False) -> str:
    pr_lookup = merged if callable(merged) else (lambda *a, **k: merged)
    branch_lookup = pushed if callable(pushed) else (lambda *a, **k: pushed)
    runs = tmp_path / "runs"
    if not runs.exists():
        runs = _runs(tmp_path)
    return trace.render(slug, ledger, runs_dir=runs, repo_dir=tmp_path,
                        pr_lookup=pr_lookup, branch_lookup=branch_lookup)


def test_en_is_the_default_when_language_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_LANGUAGE", raising=False)
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text('# language = "ko"\n', encoding="utf-8")
    # keep the config search inside tmp_path so a real (ko) vault cannot answer
    monkeypatch.setattr(paths, "vault_root_or_cwd", lambda: tmp_path)
    assert paths.ui_language() == "en"

    md = _render(tmp_path, _ledger(tmp_path, state="failed"))
    lines = md.splitlines()
    assert "## Assignment ledger" in lines
    assert "- exists on origin: no (not pushed, or deleted after merge)" in lines
    assert "## 원장" not in md
    assert "origin에 존재" not in md
    assert not _HANGUL.search(md), md


def test_en_render_has_no_hangul_in_any_branch(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    ledgers = [
        None,                                                     # not injected
        Ledger(tmp_path / "empty-assignments"),                   # no assignment
        _ledger(tmp_path / "a", state="awaiting_human", blocked_on="needs X"),
        _ledger(tmp_path / "b", state="awaiting_human", blocked_on="needs X",
                history=MERGED_PARK),
        _ledger(tmp_path / "c", state="failed", job_running=True),
        _ledger(tmp_path / "d", state="failed", checkout_busy=True,
                payload={"run_dir": "runs/x"}, last_note="note"),
    ]
    seen = set()
    for ledger in ledgers:
        for pushed in (True, False, _boom):
            for merged in (True, False, None, _boom):
                md = _render(tmp_path, ledger, merged=merged, pushed=pushed)
                assert not _HANGUL.search(md), md
                seen.update(md.splitlines())
    for slug_md in (_render(tmp_path, slug="never-ran"),):     # no runs, no verify
        assert not _HANGUL.search(slug_md), slug_md
        seen.update(slug_md.splitlines())

    for line in (
        f"# 🔎 trace — `{SLUG}`",
        "## Latest run",
        "- last of 2 run(s) (2/2)",
        "## Branch · PR",
        "## Assignment ledger",
        "- ledger not consulted (no ledger injected)",
        f"- no assignment for `{SLUG}` (never assigned, or already cleaned up)",
        "- long_job running",
        "- run_dir the ledger points at: `runs/x`",
        "- exists on origin: yes",
        "- exists on origin: no (not pushed, or deleted after merge)",
        "- exists on origin: unknown (git lookup failed — not a failure)",
        "- PR: merged",
        "- PR: exists but not merged yet (open/closed)",
        "- PR: unknown (no gh / not authenticated / no PR — not a failure)",
        f"- `requirements.md` (research) — **inherited** ← `{RUN1}`",
        "- `verify.md` (verify) — this run",
        "- no runs (no `*-never-ran` directory at all)",
        "- no run left a verify.md (all 0 run(s)) — this task has never been verified",
    ):
        assert line in seen, line
    joined = "\n".join(seen)
    for fragment in ("**unknown origin**", "presumed written by this run",
                     "the {after} later run(s)".format(after=1), "verdict of the latest run",
                     "stored in the ledger as failed", "waiting for the checkout",
                     "is **parked**", "`reconcile_merged()`", "`reconcile_unparked()`"):
        assert fragment in joined, fragment


def test_ko_ledger_and_branch_lines_are_byte_identical(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    lines = _render(tmp_path, _ledger(tmp_path, state="failed")).splitlines()
    for line in (
        f"# 🔎 trace — `{SLUG}`",
        "> 런 2개 · runs_dir `" + str(tmp_path / "runs") + "` · 읽기 전용 (상태를 바꾸지 않음)",
        "## 최신 런",
        "- 런 2개 중 마지막 (2/2)",
        "### 산출물 출처 (판정 근거는 stages.json이 아니라 파일 자체의 스탬프)",
        "## verify 판정 (⚠ verify는 상속되지 않음 — 판정은 그 런의 디렉터리에만 남는다)",
        f"- 런 1/2 `{RUN1}`: **FAIL** — ⚠ 이 판정은 이후 1번의 런 **이전** 상태",
        f"- 런 2/2 `{RUN2}`: **PASS** — 최신 런의 판정",
        "## 원장 (assignment ledger)",
        "## 브랜치 · PR",
        "- origin에 존재: 아니오 (미push, 또는 머지 후 삭제됨)",
        "- PR: unknown (gh 없음/미인증/PR 없음 — 실패가 아님)",
    ):
        assert line in lines, line


def test_language_resolves_per_call(monkeypatch, tmp_path):
    # an import-time table would freeze whichever language loaded first
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    ko = _render(tmp_path).splitlines()
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    en = _render(tmp_path).splitlines()
    assert "## 원장 (assignment ledger)" in ko
    assert "## Assignment ledger" in en
    assert "## 원장 (assignment ledger)" not in en


def test_every_trace_key_is_used():
    src = Path(trace.__file__).read_text(encoding="utf-8")
    keys = [k for k in i18n.MESSAGES["en"] if k.startswith("trace.")]
    assert keys
    for key in keys:
        assert f'"{key}"' in src, f"dead i18n key {key}"
