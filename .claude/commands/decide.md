---
description: Record your decision on a task and, if it was dispatched to you, reply to the orchestrator.
---

You are the decision-recording assistant for this second-brain vault. The user
is responding to a task — usually a human-owned decision the orchestrator
dispatched to them (it arrived through the configured notification channel).

**Input**: $ARGUMENTS — a task slug (or a fuzzy title) followed by the decision.

## Steps

1. **Find the task**: resolve the slug against `Todo/items/*.md` (exact slug, else
   best fuzzy title match). If ambiguous, list candidates and ask which one.

2. **Record the decision** in that task's `## Progress Log` (dated line, verbatim
   intent). Preserve prior log entries.

3. **Update status/next_action**:
   - If the decision *closes* the task → set frontmatter `status: done`, check its
     boxes (`- [x]`), set `updated: <today>`.
   - If it's a step (not the end) → keep `status: open`, rewrite `next_action` /
     `if_then` to reflect the decision, `updated: <today>`.

4. **Reply to the orchestrator** — only if this task was dispatched to you, i.e.
   `Worker/assignments/<slug>.json` exists with `kind: human`. Then run:
   ```bash
   uv run python -m todo_system.orchestrator.human --uri file://Worker/agents \
     reply --agent <agent_id> --task <slug> \
     --status <done|progress|needs-input> --note "<decision, one line>"
   ```
   `<agent_id>` is the `agent_id` field of that assignment JSON (the human agent's id in
   `Worker/agents/registry.yaml`).
   (`done` if closed, `progress` if a step, `needs-input` if you are handing a
   sub-question back.)

5. Leave the auto-generated views (`Todo/NOW.md`, `Todo/FOCUS.md`, `todo.db`) alone —
   the todo-sync daemon regenerates them. Confirm what changed in one line.
