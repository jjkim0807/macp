# Vault guide (MaestroACP)

This directory is a MaestroACP vault: the task files, run records and memory that
the `macp` orchestrator and its headless Claude Code workers read and write.

- A task is `Todo/items/<slug>.md` — YAML frontmatter (`outcome`, `next_action`,
  `acceptance_criteria`, `repo`, `auto_merge`, …) and a body. The `slug` in a
  stage's run metadata names that file: read it first, it is the specification.
- A run writes its artifacts under `Worker/runs/<run-id>/` — the `run_dir` in the
  run metadata.
- A task with `repo:` is worked on in its own clone; `working_dir` in the run
  metadata is that checkout. Edit and commit there, on the branch the metadata
  names, never on the default branch.
