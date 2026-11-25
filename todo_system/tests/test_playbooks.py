"""Repo playbooks — the update rules (todo_system/playbooks.py).

The fixtures are shaped like real FAIL→PASS pairs of 2026-10/11, renamed: a
test double whose override a new interface parameter broke (twice, in two
tasks), the full unit suite running past the 60-minute command cap (twice),
a branch cut from a stale base, an acceptance row that only says "does not
compile". The repo is `acme-app`, its tasks' slugs start with `acme-`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from todo_system import playbooks as pb
from todo_system.orchestrator import related_context as rc

REPO = "https://github.com/example/acme-app.git"
OTHER = "https://github.com/example/other-svc.git"
KEY = "acme-app"
TODAY = dt.date(2026, 11, 25)

FAIL_COMPILE_A = """---
stage: verify
overall_status: fail
---

# Verification — acme 157

## Checks Run

| # | Command | Tree | Exit | Decisive output |
|---|---|---|---|---|
| 1 | `./gradlew :shared:testDebugUnitTest` | x | 1 | compile error |

## Conclusion

FAIL. 결정 요인은 수락 기준이 이름으로 지목한 게이트 `./gradlew :shared:testDebugUnitTest`가
브랜치에서 테스트 소스 컴파일 단계에서 멈춘다는 것이다(`BackupImportTest.kt:60: 'importAll' overrides
nothing` — `SituationDao.importAll`에 인자 2개를 더하면서 기존 테스트의 override를 고치지 않았다).
그 오류를 걷어 낸 진단용 트리에서는 대부분 통과한다.
"""

PASS_LOG_A = """
## Phase 1: BackupImportTest follows importAll — 2026-11-16T09:44:52+09:00
- files changed:
  - shared/src/androidUnitTest/kotlin/BackupImportTest.kt
- commit: d2a52f0 chore(worker): acme-157-routine phase 1 — BackupImportTest follows importAll's two new parameters
"""

FAIL_COMPILE_B = """---
stage: verify
overall_status: fail
---

## Conclusion

**FAIL.** `./gradlew :shared:testDebugUnitTest …` (check #1) fails to compile with
`BackupImportTest.kt:60:9 'importAll' overrides nothing`: Phase 5 added a defaulted trailing
parameter to the `SituationDao.importAll` interface method but did not update the one test
double (`ImportThrowingDao`) that explicitly overrides it.
"""

PASS_LOG_B = ("- commit: `598ccec chore(worker): acme-151 phase 1 — propagate importAll 18th "
              "param to ImportThrowingDao test double`\n")

FAIL_BUDGET_A = """---
overall_status: skip
---

## Conclusion

SKIP — 시간이 모자라 AC 게이트 가운데 전체 `:shared:testDebugUnitTest`만 끝내지 못했다. 이 호스트에서
53분 넘게 돌아도 끝나지 않아 60분 한도에 걸렸다(exit 143). diff는 버그 원인을 정확히 겨냥한다. 원인은
방금 닫은 블록과 상관없는 후보가 질문으로 뜬 것이다.
"""

FAIL_BUDGET_B = """---
overall_status: skip
---

## Conclusion

**SKIP.** The deciding factor is the 60-minute cap, not the code. The fix does what the task asks.
"""

FAIL_BLOCKED = """---
overall_status: fail
blocked_on: "stale base — worker/<slug> conflicts (content) with #219 in CareViewModel.kt on origin/main; rebase onto origin/main"
---

## Conclusion

FAIL (blocked on rebase). Everything is green on the branch base.
"""

FAIL_AC_ROW_ONLY = """---
overall_status: fail
---

## Acceptance Criteria

| # | Criterion | Check | Status | Evidence |
|---|-----------|-------|--------|----------|
| 1 | "tests pass" | #1 | PASS | 12 passed |
| 2 | "QR import works" | #2 | **FAIL** | `RecipeListScreen.kt` does not compile |
"""

PASS_VERIFY = """---
overall_status: pass
---

## Conclusion

**PASS.** The gate ran in batches and every class is green.
"""


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _task(root: Path, slug: str, *, repo: str = REPO, tags: tuple = ("todo", "work"),
          title: str = "") -> None:
    tag_lines = "".join(f"  - {t}\n" for t in tags)
    _write(root, f"Todo/items/{slug}.md",
           f"---\ntags:\n{tag_lines}status: done\nrepo: \"{repo}\"\n---\n\n# {title or slug}\n")


def _run(root: Path, run_id: str, verify: str, run_log: str = "") -> None:
    _write(root, f"Worker/runs/{run_id}/verify.md", verify)
    if run_log:
        _write(root, f"Worker/runs/{run_id}/run.log", run_log)


def _pair(root: Path, slug: str, fail_ts: str, pass_ts: str, fail_verify: str,
          pass_log: str = "") -> tuple[str, str]:
    fail_id, pass_id = f"{fail_ts}-{slug}", f"{pass_ts}-{slug}"
    _run(root, fail_id, fail_verify)
    _run(root, pass_id, PASS_VERIFY, pass_log)
    return fail_id, pass_id


def _episode(root: Path, slug: str, run_id: str, lessons: list[str], *,
             tags: tuple = ("memory", "episode", "worker")) -> None:
    tag_lines = "".join(f"  - {t}\n" for t in tags)
    bullets = "\n".join(f"- {x}" for x in lessons)
    _write(root, f"Memory/episodes/{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}-worker-{slug}.md",
           f"---\ntags:\n{tag_lines}run_id: {run_id}\nslug: {slug}\nstatus: pass\n---\n\n"
           f"# Worker run: {slug}\n\n- run_id: `{run_id}`\n\n## 요약\n\n고쳤다.\n\n"
           f"## 학습 / 메모\n\n{bullets}\n")


def _memory(root: Path, stem: str, description: str, *, mtype: str = "project",
            tags: tuple = (), modified: str = "2026-11-20T00:00:00.000Z") -> None:
    tag_lines = ("tags:\n" + "".join(f"  - {t}\n" for t in tags)) if tags else ""
    _write(root, f"Memory/claude/vault/{stem}.md",
           f"---\nname: {stem}\ndescription: \"{description}\"\n{tag_lines}metadata:\n"
           f"  type: {mtype}\n  modified: {modified}\n---\n\nbody\n")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(pb, "VAULT_ROOT", root)
    monkeypatch.delenv(rc.KILL_ENV, raising=False)
    return root


def _seed(root: Path) -> dict[str, tuple[str, str]]:
    """The acme-app history used by most tests, plus one other-repo pair."""
    for slug in ("acme-157-routine", "acme-151-tags", "acme-176-proposal", "acme-90-calendar",
                 "acme-122-prefetch"):
        _task(root, slug)
    _task(root, "other-svc-3-api", repo=OTHER)
    return {
        "a": _pair(root, "acme-157-routine", "20261116T050726", "20261116T085049",
                   FAIL_COMPILE_A, PASS_LOG_A),
        "b": _pair(root, "acme-151-tags", "20261110T164113", "20261110T191257",
                   FAIL_COMPILE_B, PASS_LOG_B),
        "budget_a": _pair(root, "acme-176-proposal", "20261122T161634", "20261123T050717",
                          FAIL_BUDGET_A, "- commit: 0514f4f2 phase 1 — run the gate under 100 min\n"),
        "budget_b": _pair(root, "acme-90-calendar", "20261018T223002", "20261019T013252",
                          FAIL_BUDGET_B),
        "rebase": _pair(root, "acme-122-prefetch", "20261023T205720", "20261025T140557",
                        FAIL_BLOCKED, "- commit: fbbb67d — \"chore(worker): acme-122 phase 4 — "
                        "merge origin/main, re-apply the invalidation\"\n"),
        "other": _pair(root, "other-svc-3-api", "20261120T010101", "20261120T020202",
                       FAIL_COMPILE_B, PASS_LOG_B),
    }


# -- the deterministic rule: cause, fix, category ---------------------------------

def test_cause_is_blocked_on_when_the_verify_names_one():
    assert pb.cause_of(FAIL_BLOCKED).startswith("stale base — worker/<slug> conflicts")
    assert pb.classify(pb.cause_of(FAIL_BLOCKED)) == "rebase"


def test_cause_is_the_whole_deciding_sentence_across_wrapped_lines():
    cause = pb.cause_of(FAIL_COMPILE_A)
    assert cause.startswith("수락 기준이 이름으로 지목한 게이트")
    assert "컴파일 단계에서 멈춘다" in cause and "overrides nothing" in cause
    assert "진단용 트리" not in cause            # the next sentence is not part of it
    assert pb.classify(cause) == "compile"


def test_cause_skips_the_sentence_about_the_bug_the_task_fixes():
    """"원인은 …" in a Conclusion describes the product bug, not why verify failed."""
    cause = pb.cause_of(FAIL_BUDGET_A)
    assert cause.startswith("시간이 모자라")
    assert "원인은" not in cause
    assert pb.classify(cause) == "budget"


def test_cause_after_the_deciding_factor_marker():
    assert pb.cause_of(FAIL_BUDGET_B).startswith("the 60-minute cap")
    assert pb.classify(pb.cause_of(FAIL_BUDGET_B)) == "budget"


def test_cause_falls_back_to_the_first_failing_acceptance_row():
    cause = pb.cause_of(FAIL_AC_ROW_ONLY)
    assert cause.startswith("AC 2")
    assert "does not compile" in cause
    assert pb.classify(cause) == "compile"


def test_no_cause_from_a_verify_that_names_none():
    assert pb.cause_of("---\noverall_status: fail\n---\n\nnothing here\n") == ""


@pytest.mark.parametrize("raw, subject", [
    ("d2a52f0 chore(worker): acme-157-routine phase 1 — BackupImportTest follows importAll",
     "BackupImportTest follows importAll"),
    ("`598ccec chore(worker): acme-151 phase 1 — propagate importAll to the test double`",
     "propagate importAll to the test double"),
    ("`844fd04` chore(worker): … phase 1 — drop the ambiguous search assertion",
     "drop the ambiguous search assertion"),
    ("e7dbd7f `fix(acme-106): 고르기 목록을 LazyColumn으로 — 보이는 행만 합성` (2 files, +68/−23) → push 완료",
     "고르기 목록을 LazyColumn으로 — 보이는 행만 합성"),
    ("434b605 (434b6058c9f5f00234a89b8bc2ef9aac160eb9ea) chore(worker): … fix — 초안 rememberSaveable "
     "— 커밋 뒤 `git status --short` 빈 출력", "초안 rememberSaveable"),
    ("fbbb67d — \"chore(worker): acme-122 phase 4 — merge origin/main\"", "merge origin/main"),
    ("1d1b77d fix(worker): acme-qr-share — add missing layout.width import", "add missing layout.width import"),
    ("dc0efb8 — fix(app): use List instead of value-class vararg", "use List instead of value-class vararg"),
    ("없음 (0-커밋 결말 — 계획이 승인)", ""),
    ("none", ""),
])
def test_commit_subject_shapes_seen_in_real_run_logs(raw, subject):
    assert pb.commit_subject(raw, "acme-157-routine") == subject


def test_fix_is_the_first_two_distinct_commit_subjects():
    log = ("- commit: aaaaaaa phase 1 — first\n- commit: bbbbbbb phase 2 — second\n"
           "- commit: ccccccc phase 3 — third\n- commit: 없음\n")
    assert pb.fix_of(log) == "first; second"
    assert pb.fix_of("no commits recorded") == ""


# -- one pair → one lesson; the optional summary --------------------------------

def test_a_pair_gives_cause_arrow_fix_without_a_summary(vault):
    ids = _seed(vault)
    fail_id, pass_id = ids["a"]
    les = pb.lesson_from_pair(vault, vault / "Worker/runs" / fail_id, vault / "Worker/runs" / pass_id,
                              "acme-157-routine", summarize=None)
    assert les.kind == "run" and les.category == "compile"
    assert les.source == pass_id and les.date == "2026-11-16"
    assert "컴파일 단계에서 멈춘다" in les.text
    assert les.text.endswith("→ BackupImportTest follows importAll's two new parameters")
    assert {"backupimporttest.kt", "importall", "situationdao.importall"} <= les.keys


def test_the_episode_first_lesson_is_the_summary_and_the_category_stays(vault):
    ids = _seed(vault)
    fail_id, pass_id = ids["a"]
    _episode(vault, "acme-157-routine", pass_id, [
        "[[robolectric-dialog-never-idle]]: 텍스트 필드를 품은 wrap-content 다이얼로그는 "
        "Robolectric 411dp에서 idle에 닿지 않는다. 300dp로 해결했다.",
        "베타에서 볼 것: 이름 칸 IME 조합. 이 필드만 ViewModel StateFlow를 왕복한다.",
    ])
    les = pb.lesson_from_pair(vault, vault / "Worker/runs" / fail_id, vault / "Worker/runs" / pass_id,
                              "acme-157-routine")
    assert les.text.startswith("robolectric-dialog-never-idle: 텍스트 필드를 품은")   # link flattened
    assert les.category == "compile"                 # still the deterministic cause's
    assert "backupimporttest.kt" in les.keys         # merge identifiers too


def test_a_later_lesson_wins_only_with_two_more_failure_identifiers(vault):
    ids = _seed(vault)
    _fail, pass_id = ids["b"]
    lessons = ["앱 태스크는 허용 목록을 검토할 것.",
               "`SituationDao.importAll`에 파라미터를 더하면 `ImportThrowingDao` override가 깨진다."]
    _episode(vault, "acme-151-tags", pass_id, lessons)
    context = pb.cause_of(FAIL_COMPILE_B) + "\n" + pb.fix_of(PASS_LOG_B)
    assert pb.episode_summary(vault, "acme-151-tags", pass_id, context) == pb.flat(lessons[1])
    one_key = ["앱 태스크는 허용 목록을 검토할 것.", "`ImportThrowingDao` 하나만 언급."]
    _episode(vault, "acme-151-tags", pass_id, one_key)
    assert pb.episode_summary(vault, "acme-151-tags", pass_id, context) == one_key[0]


def test_a_summarizer_that_raises_leaves_the_deterministic_text(vault):
    ids = _seed(vault)
    fail_id, pass_id = ids["a"]

    def boom(*_a):
        raise RuntimeError("model down")

    les = pb.lesson_from_pair(vault, vault / "Worker/runs" / fail_id, vault / "Worker/runs" / pass_id,
                              "acme-157-routine", summarize=boom)
    assert "→ BackupImportTest follows" in les.text


def test_a_custom_summarizer_replaces_only_the_text(vault):
    ids = _seed(vault)
    fail_id, pass_id = ids["a"]
    les = pb.lesson_from_pair(vault, vault / "Worker/runs" / fail_id, vault / "Worker/runs" / pass_id,
                              "acme-157-routine", summarize=lambda *_a: "요약: 시그니처를 바꾸면 테스트 더블도 고친다")
    assert les.text == "요약: 시그니처를 바꾸면 테스트 더블도 고친다"
    assert les.category == "compile"


# -- merge, expiry, cap ---------------------------------------------------------

def test_pairs_naming_the_same_code_merge_and_keep_the_newest(vault):
    _seed(vault)
    lessons = pb.collect(REPO, root=vault, today=TODAY, summarize=None)
    runs = [x for x in lessons if x.kind == "run"]
    compile_ = [x for x in runs if x.category == "compile"]
    assert len(compile_) == 1 and compile_[0].count == 2
    assert compile_[0].source == "20261116T085049-acme-157-routine"      # the newer pair
    budget = [x for x in runs if x.category == "budget"]
    assert len(budget) == 1 and budget[0].count == 2                     # merged by category
    assert budget[0].date == "2026-11-23"
    assert [x.category for x in runs].count("rebase") == 1


def test_different_code_in_one_category_does_not_merge():
    a = pb.Lesson("run", "compile", "x", "r1", "2026-11-01", 1, frozenset({"fooservice.kt", "barmapper"}))
    b = pb.Lesson("run", "compile", "y", "r2", "2026-11-02", 1, frozenset({"recipescreen.kt", "modifier.width"}))
    assert len(pb.merge([a, b])) == 2
    c = b._replace(keys=frozenset({"fooservice.kt", "barmapper", "other.kt"}))
    merged = pb.merge([a, c])
    assert len(merged) == 1 and merged[0].count == 2 and merged[0].source == "r2"


def test_old_run_lessons_expire_and_memory_lines_do_not():
    old = pb.Lesson("run", "budget", "old", "r-old", "2026-09-01")
    new = pb.Lesson("run", "budget", "new", "r-new", "2026-11-20")
    mem = pb.Lesson("memory", "resource", "m", "Memory/claude/vault/m.md", "2026-01-01")
    kept = pb.expire([old, new, mem], TODAY)
    assert [x.source for x in kept] == ["r-new", "Memory/claude/vault/m.md"]
    # expired BEFORE merging: the count is of recent repeats only
    assert pb.merge(pb.expire([old, new], TODAY))[0].count == 1


def test_the_section_stays_within_2kb_in_whole_lines():
    lessons = [pb.Lesson("run", "test", pb.clip("가나다라마바사 " * 40, pb.SUMMARY_MAX_BYTES),
                         f"20261101T0000{i:02d}-acme-{i}", "2026-11-01", 1, frozenset({f"k{i}.kt"}))
               for i in range(40)]
    section, shown = pb.render_section(KEY, lessons)
    assert len(section.encode("utf-8")) <= pb.MAX_BYTES == 2048
    assert 0 < shown < len(lessons)
    items = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(items) == shown
    whole = {pb.item_line(x) for x in lessons}
    assert all(line in whole for line in items)


def test_run_and_memory_lines_interleave_with_pitfalls_first():
    runs = [pb.Lesson("run", "budget", "r", f"r{i}", f"2026-11-0{i}", count) for i, count in ((1, 3), (2, 1))]
    mems = [pb.Lesson("memory", "other", "design note", "Memory/claude/vault/d.md", "2026-11-09"),
            pb.Lesson("memory", "resource", "freezes", "Memory/claude/vault/f.md", "2026-10-01")]
    assert [x.source for x in pb.ordered(runs + mems)] == [
        "r1", "Memory/claude/vault/f.md", "r2", "Memory/claude/vault/d.md"]


def test_clip_cuts_on_a_character_boundary():
    text = pb.clip("가" * 100, 50)
    assert len(text.encode("utf-8")) <= 50 and text.endswith("…")
    assert pb.clip("short", 50) == "short"


# -- privacy (the related_context boundary) ----------------------------------------

def test_scrub_takes_out_contact_codes_keys_queries_and_homes():
    text = pb.scrub("mail someone@example.com or 010-1234-5678, 인증번호 482913, key "
                    "sk-abcdefghijklmnopqrstuv, https://x.example/cb?token=abc in /home/someone/Worker/x")
    for leaked in ("someone@example.com", "1234-5678", "482913", "sk-abcdefghijklmnopqrstuv",
                   "token=abc", "/home/someone"):
        assert leaked not in text
    assert "~/Worker/x" in text and "https://x.example/cb" in text


def test_a_task_outside_r5_contributes_no_pair(vault):
    _task(vault, "acme-9-sms-bridge", tags=("todo", "sms"))
    _pair(vault, "acme-9-sms-bridge", "20261120T010101", "20261120T020202", FAIL_COMPILE_B, PASS_LOG_B)
    assert "acme-9-sms-bridge" not in pb.task_index(vault)
    assert pb.collect(REPO, root=vault, today=TODAY, summarize=None) == []


def test_an_episode_outside_r5_is_not_used_as_the_summary(vault):
    ids = _seed(vault)
    _fail, pass_id = ids["a"]
    _episode(vault, "acme-157-routine", pass_id, ["비밀 요약"], tags=("memory", "pii"))
    assert pb.episode_lessons(vault, "acme-157-routine", pass_id) == []


def test_memory_lines_follow_the_r5_boundary_type_and_lead_word(vault):
    _seed(vault)
    _memory(vault, "acme-full-suite-freezes",
            "acme-app: full `:shared:testDebugUnitTest` FREEZES under default parallel forks — --max-workers=1")
    _memory(vault, "worker-acme-room-test-seam", "Room 2.7: do not subclass AppDatabase for a fake")
    _memory(vault, "other-svc-deploy", "other-svc deploy recipe — shares the acme-pg database")
    _memory(vault, "acme-owner-preference", "the owner likes short replies", mtype="user")
    _memory(vault, "acme-private-numbers", "acme numbers", tags=("pii",))
    _write(vault, "Memory/claude/vault/MEMORY.md", "- [acme](acme-full-suite-freezes.md)\n")
    lines = {Path(x.source).stem: x for x in pb.memory_lessons(vault, pb.repo_aliases(KEY, pb.task_index(vault)))}
    assert set(lines) == {"acme-full-suite-freezes", "worker-acme-room-test-seam"}
    assert lines["acme-full-suite-freezes"].category == "resource"
    assert lines["acme-full-suite-freezes"].date == "2026-11-20"


def test_aliases_are_the_key_and_a_slug_prefix_only_this_repo_uses(vault):
    _seed(vault)
    _task(vault, "fix-1", repo=OTHER)
    _task(vault, "fix-2")
    assert pb.repo_aliases(KEY, pb.task_index(vault)) == {"acme-app", "acme"}


# -- refresh / prompt_section -----------------------------------------------------

def test_refresh_writes_once_and_only_when_the_bytes_change(vault):
    _seed(vault)
    line = pb.refresh(REPO, today=TODAY, local_id="hq")
    path = vault / "Memory/playbooks/acme-app.md"
    assert line == f"playbook: acme-app — {pb.build(REPO, today=TODAY).shown} line(s) → Memory/playbooks/acme-app.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nkind: playbook\nrepo: acme-app\n")
    assert "generated" not in text                  # no timestamp: same lessons, same bytes
    assert pb.refresh(REPO, today=TODAY, local_id="hq") is None
    assert path.read_text(encoding="utf-8") == text
    assert not (vault / "Memory/playbooks/other-svc.md").exists()


def test_refresh_writes_on_the_hq_host_only(vault):
    _seed(vault)
    _write(vault, "Worker/hq_host", "the-hq\n")
    assert pb.refresh(REPO, today=TODAY, local_id="a-laptop") is None
    assert not (vault / "Memory/playbooks").exists()
    assert pb.refresh(REPO, today=TODAY, local_id="the-hq") is not None


def test_a_repo_with_nothing_left_to_say_loses_its_file(vault):
    _seed(vault)
    assert pb.refresh(REPO, today=TODAY, local_id="hq")
    later = TODAY + dt.timedelta(days=pb.EXPIRE_DAYS + 40)       # every run lesson expired
    assert pb.refresh(REPO, today=later, local_id="hq") == (
        "playbook: acme-app — nothing left to say, removed Memory/playbooks/acme-app.md")
    assert not (vault / "Memory/playbooks/acme-app.md").exists()


def test_the_kill_switch_turns_refresh_and_the_section_off(vault, monkeypatch):
    _seed(vault)
    assert pb.refresh(REPO, today=TODAY, local_id="hq")
    monkeypatch.setenv(rc.KILL_ENV, "0")
    assert pb.prompt_section(REPO) == ""
    assert pb.refresh(OTHER, today=TODAY, local_id="hq") is None


def test_prompt_section_is_the_file_body_for_this_repo_only(vault):
    _seed(vault)
    pb.refresh(REPO, today=TODAY, local_id="hq")
    section = pb.prompt_section(REPO)
    assert section.startswith("## Repo playbook — acme-app\n")
    assert "kind: playbook" not in section
    assert "[compile]" in section and "×2" in section
    assert len(section.encode("utf-8")) <= pb.MAX_BYTES
    assert pb.prompt_section(OTHER) == ""            # its pair exists, its file does not
    assert pb.prompt_section(None) == "" and pb.prompt_section("") == ""


def test_prompt_section_recaps_a_hand_edited_file(vault):
    lines = ["## Repo playbook — acme-app", ""] + [f"- [test] {'가' * 60} ({i})" for i in range(100)]
    _write(vault, "Memory/playbooks/acme-app.md", "---\nkind: playbook\n---\n" + "\n".join(lines) + "\n")
    section = pb.prompt_section(REPO)
    assert len(section.encode("utf-8")) <= pb.MAX_BYTES
    assert section.splitlines() == lines[:len(section.splitlines())]


# -- measurement -------------------------------------------------------------------

def _ev(run: str, slug: str, cat: str, day: dt.date, repo: str = KEY) -> pb.FailEvent:
    return pb.FailEvent(run, slug, repo, cat, day)


def test_recurrence_counts_a_category_another_task_of_the_repo_failed_on():
    d = dt.date
    events = [
        _ev("20261103T0-a", "acme-1", "compile", d(2026, 11, 3)),
        _ev("20261104T0-a", "acme-1", "compile", d(2026, 11, 4)),      # same task: a retry, not a repeat
        _ev("20261111T0-b", "acme-2", "compile", d(2026, 11, 11)),     # repeat
        _ev("20261112T0-c", "other-1", "compile", d(2026, 11, 12), repo="other-svc"),  # other repo
        _ev("20261118T0-d", "acme-3", "budget", d(2026, 11, 18)),
        _ev("20261124T0-e", "acme-4", "budget", d(2026, 11, 24)),      # repeat
        _ev("20261125T0-f", "acme-5", "other", d(2026, 11, 25)),       # "other" never repeats
    ]
    rows = pb.recurrence(weeks=4, today=TODAY, events=events)       # TODAY is a Wednesday
    assert [r["week"].isoformat() for r in rows] == ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"]
    assert [(r["fails"], r["repeats"]) for r in rows] == [(2, 0), (2, 1), (1, 0), (2, 1)]
    assert rows[1]["categories"] == {"compile": 1}
    table = pb.recurrence_table(rows)
    assert "| 2026-11-09 | 2 | 1 | compile×1 |" in table


def test_recurrence_ignores_a_repeat_older_than_the_horizon():
    events = [_ev("20260801T0-a", "acme-1", "budget", dt.date(2026, 8, 1)),
              _ev("20261124T0-b", "acme-2", "budget", dt.date(2026, 11, 24))]
    assert pb.recurrence(weeks=1, today=TODAY, events=events)[0]["repeats"] == 0


def test_fail_events_and_the_brief_line_from_a_vault(vault):
    _seed(vault)
    events = pb.fail_events(vault)
    assert [e.category for e in events if e.repo == KEY] == ["budget", "rebase", "compile",
                                                             "compile", "budget"]
    line = pb.brief_line(vault, today=TODAY)
    assert line.startswith("리포 플레이북 — 같은 원인 FAIL 재발/전체 FAIL")
    # week of 11/16: acme-157 repeats acme-151's compile cause, acme-176 repeats
    # acme-90's budget cause (35 days earlier), other-svc's compile is its own first
    assert "11/9 0/1 · 11/16 2/3 · 11/23 0/0" in line


def test_the_cli_show_and_recurrence(vault, capsys):
    _seed(vault)
    assert pb.main(["show", "--repo", REPO, "--vault-root", str(vault), "--today", "2026-11-25"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nkind: playbook\nrepo: acme-app\n")
    assert pb.main(["recurrence", "--weeks", "2", "--vault-root", str(vault), "--today", "2026-11-25"]) == 0
    assert "| week of | fail/skip |" in capsys.readouterr().out
    assert not (vault / "Memory/playbooks").exists()       # neither command writes
