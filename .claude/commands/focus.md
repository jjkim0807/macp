---
description: Start or end a focused MANUAL session on a project — hand it off from (and back to) the automation, and keep the memory continuous.
---

You help the user run a **focused manual session**: they open their own Claude
Code session to drive one project intensively, and the autonomous worker /
orchestrator should NOT touch that project meanwhile (avoid double-edits, merge
conflicts, wasted runs). Manual work must still be **recorded** so the memory
graph stays continuous.

**Input**: $ARGUMENTS — `start <project>` or `end <project>`.
`<project>` is a slug prefix (e.g. `my-app`, `website`, `thesis`).

## `start <project>`

1. Find open `Todo/items/*.md` whose slug starts with `<project>` and whose
   `worker_mode` is `auto` or `delegate`.
2. Set each to `worker_mode: manual` (edit the frontmatter). This makes the
   worker/orchestrator **defer** — it won't auto-dispatch those while you work.
   List which tasks you changed.
3. Open a manual session log (repo dir = the project's checkout, e.g.
   `~/Worker/<project>`, or the task's `repo` field):
   ```bash
   python3 -m todo_system.session_log log --project <project> \
     --repo-dir <repo> --note "manual focus session start"
   ```
4. Tell the user: automation is now hands-off for `<project>` until
   `/focus end <project>`. Remind them to checkpoint as they go:
   `python3 -m todo_system.session_log log --project <project> --repo-dir <repo> --note "..."`
   (or `--commit --push` from a vault clone), so the brief/memory stay current.

## `end <project>`

1. Append a closing checkpoint (git delta auto-captured if `--repo-dir` given):
   ```bash
   python3 -m todo_system.session_log log --project <project> \
     --repo-dir <repo> --note "manual focus session end — <one-line summary>" --status done
   ```
2. For tasks you flipped to `manual` in `start` that the **AI should now resume**,
   restore `worker_mode: auto`. Leave genuinely human-owned ones `manual`.
3. Confirm the handback and what (if anything) the automation will pick up next.
