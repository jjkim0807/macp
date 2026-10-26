# Apply: worker-planner.md keep-alive section

Headless worker stages cannot write under `.claude/**` (harness hard guard), so the
finished prompt is here as a complete file, not a diff. A human applies it with:

```bash
cp proposals/macp-planner-keep-alive-projects/dot-claude/commands/worker-planner.md \
   .claude/commands/worker-planner.md
git rm -r proposals/macp-planner-keep-alive-projects
git commit -m "docs(prompts): worker-planner keep-alive section + depth/trigger exceptions"
```

What it changes (against `.claude/commands/worker-planner.md` at the branch base):

- new `## Keep-alive projects (injected by planner_tick)` section after "Discovery reports"
- execution steps 3 and 5 and the `### Queue full` edge case: exception when the
  keep-alive block is present; step 7 and the "Safe work sources" priority order put
  keep-alive starved projects right after main-fix
- the trigger-skip rule gains the same exception
- the output-format verdict lists `error`

Until it is applied, the injected block itself (`keep_alive.render_context`) carries
the same rules into every keep-alive planner call.
