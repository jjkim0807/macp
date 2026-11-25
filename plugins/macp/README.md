# macp — Claude Code plugin

Type the [macp](../../README.md) commands as slash commands in any Claude Code session.

| Slash command | Runs |
|---|---|
| `/macp init` | `macp init`: makes the vault under `~/.macp`, registers this repository |
| `/macp add "<one sentence>"` | `macp add "<one sentence>"`: turns the sentence into a validated task |
| `/macp up --once` | `macp up --once`: one pass, dispatch → worker → result |
| `/macp status` | `macp status`: the vault, this repository, the login, and each task's state |

`/macp up`, `/macp up --service`, `/macp dashboard` and `/macp memory …` pass through the
same way.

## Install

The plugin calls the `macp` command, so install that first. It needs Python ≥ 3.12,
[`uv`](https://docs.astral.sh/uv/), `git` and a logged-in Claude Code:

    uv tool install git+https://github.com/jjkim0807/macp

Then, inside any Claude Code session:

    /plugin marketplace add jjkim0807/macp
    /plugin install macp@macp

## How it works

The plugin is one skill, [`skills/macp/SKILL.md`](skills/macp/SKILL.md), and no code. The
skill runs `macp <your arguments>` in the session's current directory and shows what it
prints. Everything else happens in the CLI.

- `up` and `dashboard` keep running, so the skill starts them in the background of the
  session. A background `up` stops when the session ends; `/macp up --service` installs
  the loop as a systemd user unit instead.
- The skill pre-approves only `Bash(macp *)`, and only for the turn you invoke it in.
- Claude does not run it on its own (`disable-model-invocation: true`); you type `/macp`.
- Where another `/macp` command already exists (this repository has one,
  `.claude/commands/macp.md`), the plugin's skill is `/macp:macp`.

## Check the manifests

From the repository root:

    claude plugin validate .               # the marketplace, .claude-plugin/marketplace.json
    claude plugin validate ./plugins/macp  # this plugin

A valid manifest ends with `✔ Validation passed`.
`todo_system/tests/test_claude_plugin.py` checks the same documented rules without
`claude`, and checks that the skill only calls the CLI.

Installed copies update only when `version` changes. To ship a change to the skill, raise
`version` in `.claude-plugin/plugin.json` together with the version in `pyproject.toml`;
the test keeps the two equal.
