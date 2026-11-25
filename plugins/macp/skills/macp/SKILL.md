---
name: macp
description: "Run the macp CLI here: queue a task in one sentence and let a Claude Code worker finish it on its own branch (init, add, up, status, dashboard, memory)."
argument-hint: 'init | add "one sentence" | up [--once|--service] | status | dashboard | memory ...'
allowed-tools: Bash(macp *)
disable-model-invocation: true
---

Run the `macp` command with the user's arguments and show what it prints. The CLI does
all the work; this skill only starts it and adds nothing of its own.

1. Run `macp $ARGUMENTS` with the Bash tool, in the current directory. Pass the
   arguments as the user typed them, quoting the sentence after `add` as one shell
   word. With no arguments, run `macp status`.
2. How long it runs decides how to start it:
   - `up` (except `up --service`) and `dashboard` keep running; `up --once` ends after
     one worker pass, often several minutes later. Start them with `run_in_background`,
     say that they are running, and show their output when they end. `/macp status`
     shows progress meanwhile. A background `up` stops with this session;
     `/macp up --service` keeps the loop running as a systemd user unit.
   - Run every other verb in the foreground with a 10-minute timeout (`add` waits for
     one model call).
3. Show the output as printed, in a code block (for a long background run, its last 40
   lines). Then stop: do not summarize it, act on what it reports, or run any other
   command. The next step is the user's.
4. If the shell cannot find `macp` (exit status 127), macp is not installed. Tell the
   user to install it once with `uv tool install git+https://github.com/jjkim0807/macp`
   (it needs Python 3.12 or newer, uv and git), then to run the slash command again.
