---
description: Register the CURRENT project with MaestroACP (macp) or update its progress memory. Use when the user says "register this with macp", "update progress", or the same in another language (e.g. "macp에 등록", "진행상황 업데이트").
---

You help the user manage the **current project (cwd)** with **MaestroACP (`macp`)** —
a control plane that isolates each project's context (profile, agent accounts, and
memory) like a Python virtualenv. Project memory lives in `<project>/.macp/memory/`
(git-tracked → push it and any environment gets the same working context).

The `macp` CLI should be on PATH. If `macp` is not found, use
`python -m todo_system.macp` (from the MaestroACP repo, or with `PYTHONPATH` set to
it), or tell the user to add `~/.local/bin` to PATH.

Request: **$ARGUMENTS**

## Step 1 — check registration
Run `macp status`. The project is registered iff it prints a `project:` line (a
`.macp/` directory exists in the current tree).

## Step 2 — REGISTER  (intent: "register" / not yet registered)
1. `macp profile list`. Use `default` unless the user names a profile (e.g. work /
   personal). If that profile doesn't exist: `macp profile add <name> --vault <path>`.
2. Pick `--worker-mode`: `auto` (full autonomy), `quick` (trivial one-file changes),
   `review` (open PR but human merges), or `manual`.
3. `macp init --profile <p> --worker-mode <m> [--capabilities a,b]`.
   This writes `.macp/config.toml` + `.macp/memory/` and links the memory into
   `CLAUDE.md` (`@`-import) and `AGENTS.md` (vendor-neutral pointer) so future
   sessions — Claude Code OR other agents — auto-load it.
4. **Seed memory** (step 4) with what this project is + key context.
5. `git add .macp CLAUDE.md AGENTS.md && git commit -m "chore(macp): register project"`.
6. **Turn GitHub Actions off for the repo** — this pipeline verifies with each
   project's local commands and does not use Actions unless the user explicitly asks
   for it on that project. A new GitHub repo starts with Actions enabled, so do this on
   every registration:
   `gh api -X PUT repos/<owner>/<repo>/actions/permissions -F enabled=false`
   (skip only if the user asked for Actions here). When seeding memory, record the
   project's **local** check commands as its gates — never "CI green".

## Step 3 — UPDATE PROGRESS  (intent: "update progress so far" / "update")
1. Summarize what changed / was decided since the last update — read the recent
   `git log`, the existing `.macp/memory/`, and your session context.
2. Write a dated entry:
   `macp memory add progress-<YYYYMMDD> --content "<concise: what was done, current state, decisions, next steps>"`.
3. If a durable topic changed (API, architecture, a gotcha), also
   `macp memory add <topic> --content "..."` (overwrites that named memory).
4. `git add .macp && git commit -m "chore(macp): update project memory"` (and push
   if the user wants it available elsewhere).

## Step 4 — seed memory on first registration
Write 1–3 entries capturing: what this project is, key architecture/decisions, and
conventions/gotchas an agent needs to know. Keep each factual and short.
`macp memory add <name> --content "..."`.

## Finish
Run `macp status` and confirm the project is registered with N memory file(s).
Tell the user what you did in one line.
