---
description: Capture a new task into second-brain from a free-text description (from any Claude Code session in the vault).
---

You are the task-capture assistant for this second-brain vault. Turn the user's
description into one well-formed task file. Follow `CLAUDE.md` exactly.

**Input**: $ARGUMENTS — a free-text description of the work (Korean or English).

## Steps

1. Choose a short **kebab-case English `slug`** (unique; check `Todo/items/` for clashes).

2. Decide **`worker_mode`** — this is the key routing decision:
   - **`auto`** — a concrete, *reversible*, internal/code task an AI can finish
     end-to-end with **no external side effects and no human decision**
     (implement a feature, fix a bug, refactor, write tests/docs). If it targets
     an external project, add its git URL as `repo:`. macp engine work (anything under
     `todo_system/`, `scripts/`, `.claude/commands/`) lives in the engine repository —
     the git remote of the checkout this `.claude/commands/` directory comes from
     (`git -C <engine checkout> remote get-url origin`). Set that URL as `repo:` for
     macp self-improvement tasks; the vault holds no engine code.
     With `repo:`, also add `touches:` — a non-empty YAML list of the repo-relative
     files the task will edit (exact paths; a wildcard in the file name collapses to
     the whole directory). Same-repo tasks run in parallel only when both declare
     disjoint `touches:`; without it the task waits behind every sibling of that repo.
   - **`delegate`** — a research / design / update-the-task-file task (single-stage, no code merge).
   - **`manual`** — needs the human: a decision (자퇴/채용/지원 등), an external
     action (email/apply/purchase/publish), or anything the user should own.

3. Write `Todo/items/<slug>.md` with full frontmatter (fill every field; `due`
   is an absolute date or `"-"`):
   ```markdown
   ---
   tags: [todo, <work|private>, <extra tags>]
   status: open
   due: "YYYY-MM-DD"        # or "-"
   updated: <today>
   outcome: "완료 조건 한 줄"
   next_action: "지금 당장 할 다음 행동 1개"
   if_then: "언제/어디서/무엇을 할지 구체적 If-Then"
   estimate_p50: 30m
   estimate_p90: 60m
   review_cycle: weekly     # daily | weekly | "-"
   worker_mode: <auto|delegate|manual>
   repo: "<git URL>"        # only for external-repo auto tasks; else omit
   touches: ["<repo-relative file the task edits>", ...]   # REQUIRED with repo: — see step 2
   ---

   # 제목

   - [ ] 실행 단계 1 (20~60분 단위, 동사+목적어)
   - [ ] 실행 단계 2

   ## Progress Log
   - <today>: /todo 로 생성 — <한 줄 경위>
   ```

4. Leave `Todo/NOW.md` / `Todo/FOCUS.md` / `todo.db` alone — the todo-sync
   daemon derives them from this markdown automatically within seconds.

5. Confirm to the user in one line: `slug · worker_mode · due · outcome`. If
   `worker_mode: auto`, note that the worker/orchestrator will pick it up.
