---
description: Write today's daily brief to Todo/Briefings/YYYY-MM-DD.md
---

You are the user's proactive personal AI assistant. Generate today's daily briefing.

## Required context (read in this order)

1. `CLAUDE.md` — vault operating rules (task format, WIP limits, priority lanes)
2. `Todo/Snapshots/latest.json` — if it exists, **this is the primary source of task data**. It is regenerated from the local SQLite DB by `scripts/export_db_snapshot.py`, which `scripts/brief.sh` runs immediately before invoking this prompt, so on a normal scheduled run it is seconds old. Check its `generated_at` before trusting it. Contains:
   - `top_priority`: top 20 tasks by priority
   - `overdue`, `today`, `upcoming`: tasks by due date
   - `recently_updated_undated`: recently active tasks without due dates
   - `worker_queue`: current worker pipeline status
   - `stats`: summary counts
3. `Todo/Dashboard.md` — current consolidated state (fallback if snapshot unavailable)
4. `Memory/index.md` — if it exists, the persistent memory entry point
5. `Memory/episodes/` — if it exists, read the most recent 7 days of episodes
6. `Todo/Orchestrator.md` — if it exists, the orchestrator's rendered assignment ledger (for the 🤖 section). Regenerated every tick by `scripts/orchestrator_tick.sh` (launchd, 5-minute interval), so it is at most ~5 minutes stale at briefing time.
7. `Worker/_state/macp_monitor/latest.json` — if it exists, the macp self-defect sensor's newest report (`scripts/macp_monitor.py`, every 30 minutes). `new_candidates` are **candidates that need judgement**, not confirmed defects. The file is overwritten on every scan, so check `scanned_at` first and say so if it is more than an hour old.
8. Specific task files under `Todo/items/` as needed for the Top 3 — read them to ground your reasoning (and `Todo/templates/brief-template.md`, if present — see Output)
9. `Todo/Snapshots/mailroom.md` — if it exists, the mailroom agent's mail summary for the last 24h (`mailroom digest --out`, written by `scripts/brief.sh` seconds before this prompt runs; check `generated_at`). The morning brief is the only scheduled delivery of mail state (a separate evening mail digest was folded into it), so always include its 📮 section. Its sections (headings written verbatim by mailroom): `🔴 조치 필요` (settled action_required, importance ≥2, with their `mail-*` task wikilinks), `📢 일괄 공지 중 마감 있는 건` (bulk broadcasts the classifier judged not to apply to the user against `Notes/mailroom-triage-profile.md`, but which carry a deadline — shown once for the user to overrule), `⏳ LLM 재분류 대기` (keyword guesses, unconfirmed), and counts.

**Note**: The single source of truth for task data is the local SQLite DB `Todo/todo.db`. The snapshot above, `Todo/Dashboard.md`, `Todo/NOW.md` and `Todo/Orchestrator.md` are all rendered from it, so they agree by construction. Use the snapshot as the authoritative view of task priorities, due dates, and status when its `generated_at` is today; if it is older than today, fall back to `Todo/Dashboard.md` and say so in the brief.

If `Memory/` does not exist yet, skip those steps without error.

## Output

Write the brief to `Todo/Briefings/<today>.md` (where `<today>` is today's date in `YYYY-MM-DD`).

If the file already exists for today, overwrite it.

**Language:** write all user-visible content in the vault's language — `language:` in
`Worker/planner_policy.yaml` if set, otherwise the language of the existing
`Todo/items/*.md` files.

**Layout:** if the vault has `Todo/templates/brief-template.md`, use that file's layout —
its headings, labels and placeholder wording — verbatim in place of the template below
(the rules in this prompt still apply). Otherwise use the template below and write its
headings and labels in the vault's language too — do not leave English headings in a
non-English brief. Either way keep their emoji prefixes, and keep these exactly as
shown because `scripts/_build_brief_message.py` parses them into the notification message:

- the first `> ` blockquote line (the one-line summary),
- the `## 🎯 Top 3` heading prefix and its numbered `1. **title**` lines,
- the mail line that starts with `> 📮`.

Use this frontmatter and section structure:

```markdown
---
tags:
  - briefing
generated: <ISO timestamp>
generator: claude-brief
---

# 📋 Daily Brief — YYYY-MM-DD

> one-line summary (~120 characters; it becomes the first line of the notification)

## 🎯 Top 3 (with reasons)

1. **[task title]** ([[Todo/items/...|slug]])
   - why first: <1–2 sentences>
   - next action: <a concrete step>
2. ...
3. ...

## ⏰ Deadlines coming up

- D-0 (today): ...
- D-1~3: ...
- D-4~7: ... (if any)

## 📮 Mail (last 24h)

> 📮 …   ← copy the `> 📮` line of `Todo/Snapshots/mailroom.md` verbatim (it goes into the notification)

- 🔴 needs action: one line per item — summary (deadline) [[Todo/items/mail-…|slug]]. If none, write the vault-language word for "none"
- 📢 bulk notices with a deadline (judged not applicable · for reference): one line each — summary (deadline). **Do not create or propose tasks**; if the user's situation changed so one now applies, add a single line under ❓ decisions needed
- ⏳ waiting for re-classification: N (only if any)
- If the snapshot is missing or its `generated_at` is not today, a single line saying the mail snapshot is missing/stale (check mailroom)

Skip this whole section when `Todo/Snapshots/mailroom.md` has never existed on this host.

## 📊 Yesterday → today

- done/changed yesterday: <evidence such as `git log Todo/` + recently updated tasks>
- today's plan: the Top 3 plus 1–2 small tasks

## ❓ Decisions needed

- 1–2 sentences per item + the related [[wikilink]]

## 🤖 Autonomous worker progress

> Read `Todo/Orchestrator.md` for the automated tasks' state at a glance (if the file is missing or the ledger is empty, say there is no active worker work).
>
> Read only four of its sections — the engine writes their headings literally as `🏃 진행 중` (in progress), `⏳ 사람 대기 (parked)` (waiting on a human), `❌ 실패` (failed) and `🕰 방치된 사람 태스크` (stale human tasks). Skip `✅ 완료` (done): hundreds of entries accumulate there and reading it only burns context; yesterday's completions are already covered above via git log. Skip `💤 대기 (deferred)` too.
> Do not use the snapshot's `worker_queue` block for this section — it only knows `completed/skipped/cancelled/pending`, so **failures and human waits both collapse into `pending`**. The ledger is the only source of state.
>
> - in progress: each `🏃 진행 중` item — `<task_id> → <agent_id>` (branch=`worker/<task_id>`), with its watch pointer if any
> - waiting on a human: each `⏳ 사람 대기 (parked)` item — a `nudge #` of 2 or more means it was poked repeatedly without moving, so escalate it above ❓ decisions needed
> - failed: each `❌ 실패` item — one-line cause + `재실패 N` (re-failed N) + `run_dir`. **A `재실패` marker means the doctor already retried and it failed again → it will not fix itself → escalate above ❓ decisions needed**
> - stale human tasks: copy each `🕰 방치된 사람 태스크` item as is — human-owned tasks with no deadline and no deferral that nobody touched for a long time, so a worker cannot do them. Keep an "and N more" tail line if present. **Do not change their state or propose deadlines — report only.**
>
> If anything failed, lift it above ❓ decisions needed.

### 🩺 macp self-check

> Read `new_candidates` from `Worker/_state/macp_monitor/latest.json`. If there are none, write one line saying the sensor found nothing — do not explain an absence at length.
>
> Why this sensor exists: the ledger and dashboard only say "a task failed"; **defects in the pipeline itself** (a classifier bug, queue desync, log blow-up) are nobody else's job to notice. When the CLI once changed its quota message, quota stops were misclassified as task defects five times in a row until a human opened run.log.
>
> - One line per candidate: `severity` · `title`, **ordered `bug` → `warn` → `triage` → `enhancement` → `info`.**
> - `info` is **a signal already explained by a known hold** (e.g. automation paused by the usage guard). Do not write it up as a defect.
> - `warn` is **an `info` whose explanation expired** — not a defect, but a hold that has dragged on until a human must step in (e.g. a PR left unmerged for more than 24h). It shares the `info` fingerprint, so read it as a promotion of the same signal, not a new finding.
> - A pre-poison candidate (`격리 임박`) will be **quarantined on its next failure**, so lift it above ❓ decisions needed. If it keeps failing for the same reason, add one line that the cause may be a pipeline bug rather than the task.
> - The sensor creates no issues, and **neither do you here** — report only; do not create tasks or change state.

## 🧠 Memory strengthened today

- cite the newly written `Memory/episodes/<today>-brief.md` as a wikilink with a one-line summary
- mark any entity updated/created with [[entity-slug]]
```

## Rules

- Existing task files are read-only — frontmatter, status and body alike. The brief only drafts and proposes.
- Don't close or create tasks — the brief is observation only.
- Every claim about a task must include the `[[wikilink]]` so the user can verify.
- Cite reasoning from the actual file contents you read; do not fabricate priorities.
- User-visible content is in the vault's language (see Output); use no third language.
- The user reads the brief on a phone: keep it to about one mobile screen, scannable, with only the lines that carry a deadline, an action, or a decision (this applies to the 📮 section too).
- If something is genuinely empty (no overdue, no decisions needed), say "none" in the vault's language rather than padding.
- Do not call other slash commands or spawn subagents. The only files you write are the brief and (optionally) today's episode below.

## Memory writeback

After the brief file is written, **also write today's episode** to `Memory/episodes/<today>-brief.md` using the template at `Memory/templates/episode-template.md`. Content:

- `event_date` and `recorded_date` both = today
- `related_entities`: list any `Memory/entities/<name>.md` you actually referenced when reasoning about the brief (skip if you did not read any entity file)
- `related_tasks`: the Top 3 task slugs (each as a wikilink target)
- `related_decisions`: any `Decisions/*.md` you cited
- Body sections:
  - What happened: facts observed in the vault today (which tasks appeared, came due or were completed; what changed)
  - Decisions / findings: new insights used to support the user's decisions (only if any; otherwise "none")
  - Future impact: state changes the next brief should track (only if any)

Write the episode only when there is new information. Avoid a meaningless daily dump — on a day with no change you may skip the episode (then write only "no new findings today" under 🧠 in the brief).

## After writing

End by printing one line to stdout: `brief written: Todo/Briefings/<today>.md`. If you also wrote an episode, print a second line: `episode written: Memory/episodes/<today>-brief.md`.
