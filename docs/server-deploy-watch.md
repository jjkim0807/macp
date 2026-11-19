# Server deploy watch

A server change can be merged to `main` and then not reach production. Deploying
some apps needs the owner's approval, so nothing deploys them automatically, and
nothing used to say that a deploy was waiting. One app's server fix sat undeployed
for about two days while the app kept calling the old server.

The server deploy watch notices that gap. It sends **one** alert that names the
repo, the undeployed commits and the command to run. It **never deploys**.

## Where it runs

The orchestrator tick calls `Orchestrator._server_deploy_watch_tick()` right after
the project-stall check. That calls `todo_system.orchestrator.server_deploy_watch.run_once`.

- It checks at most once every 10 minutes, across all repos.
- If `<vault>/.macp/server_deploy_watch.json` does not exist, the tick returns
  before importing the module. It writes no state and prints nothing.
- It is fail-open. An error in one repo is printed and the other repos are still
  checked. An error in the watch never stops the tick.

## What it checks, per repo

1. Ask `origin` for the default branch tip (`git ls-remote`). If the runner's
   checkout under the worker root (`~/Worker/<repo>` by default) doesn't have that
   commit yet, run `git fetch origin <branch>` once. The watch never checks out,
   resets or cleans that checkout, and never clones. A repo with no checkout is
   skipped.
2. Find the newest commit on the tip that touched `server_paths`. If none has,
   there is nothing to watch and the tag command is not run.
3. Run `tag_cmd` to read the image tag (short sha) that production is running.
4. Compare the two:
   - **current**: the running commit is that newest server commit or a descendant
     of it. Any earlier alert state is cleared, and no message is sent.
   - **behind**: the running commit is older. Once the oldest undeployed server
     commit is older than `grace_minutes`, one alert is sent.
   - **incomparable**: the running tag isn't a commit the checkout knows, for
     example a build from another branch. The watch prints one line and sends no
     alert.

The grace period is measured from the commit's committer time. For a squash merge
that is about the merge time, and it survives restarts. If you turn the watch on
while an old undeployed commit already exists, it alerts on the first check.

## Config entry

Put the config in `<vault>/.macp/server_deploy_watch.json`. It has the same shape
as `.macp/beta_publish_config.json`: a `repos` map keyed by the clone directory
name under the worker root. Keys that start with `_`, such as `_readme`, are ignored.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `server_paths` | list of strings, non-empty | required | Paths that count as server code. Each one is passed to git as a `:(glob)` pathspec, so `server/**` matches everything under `server/`. |
| `tag_cmd` | list of strings (argv) | required | A **read-only** command that prints the running image tag. It runs without a shell, and `~` is expanded in the first element only. The engine takes the last non-empty output line, drops any `@sha256:…` digest, and keeps the text after the last `/` and then after the last `:`. The result must be 7–40 hex characters. So both a bare `abc1234` and a full image ref `<registry>/app/server:abc1234` work. |
| `deploy_cmd` | string | required | Shown in the alert, with `<sha>` replaced by the newest undeployed short sha. **It is never executed.** |
| `grace_minutes` | int | `30` | How long production may be behind before the alert. |

An entry with a missing field or a wrong type, such as a string `tag_cmd`, is
skipped with one printed line. Repos with no entry are ignored.

### Worked example: mindchat

mindchat's server lives under `server/`. Production runs on Cloud Run with the
image tagged by the 7-character short sha, and the owner deploys with
`~/.config/mindchat/deploy.sh <sha>`.

```json
{
  "_readme": "Server deploy watch — see docs/server-deploy-watch.md in the engine repo.",
  "repos": {
    "mindchat": {
      "server_paths": ["server/**"],
      "tag_cmd": ["gcloud", "run", "services", "describe", "<service>",
                  "--region", "<region>", "--project", "<gcp-project>",
                  "--format", "value(spec.template.spec.containers[0].image)"],
      "deploy_cmd": "~/.config/mindchat/deploy.sh <sha>",
      "grace_minutes": 30
    }
  }
}
```

The `gcloud run services describe` command only reads. It prints the full image
ref, `<registry>/mindchat/server:<short sha>`, and the engine keeps the part after
the last `:`. Replace `<service>`, `<region>` and `<gcp-project>` with the real
values in the vault copy only. Don't put them in the engine repo.

The worker that built this feature does not add the vault entry. The interactive
session adds it after the merge.

## Alerts and throttling

- **Behind**: one alert per repo and per newest undeployed commit, for example:

  ```text
  🚨 [서버 미배포] mindchat — main의 서버 변경 2건이 30분 넘게 운영에 반영되지 않았어요
  운영 태그: 1a2b3c4
  배포 명령: ~/.config/mindchat/deploy.sh 9f8e7d6
  9f8e7d6 server: fix crash
  5c6d7e8 server: add endpoint
  ```

  The list shows at most 9 commits, then `외 N건`. Later ticks stay quiet while
  the same commit is still the newest undeployed one. If a newer server commit
  lands before production catches up, it alerts again only after that commit's
  own grace period, so a burst of merges does not send one alert per tick.
- **Cleared**: once production runs that commit or newer, the state is cleared.
  The watch sends no message. It just stops alerting.
- **Tag command failure** (non-zero exit, timeout, or output with no sha): at most
  one `⚠️ [서버 배포 감시] <repo> 운영 태그를 읽을 수 없음 (rc=<rc>)` alert per repo
  per day. Only the exit code is logged and sent (`rc=1`, `rc=timeout`,
  `rc=0(unparseable)`). The command's stdout and stderr are never logged or sent,
  because they can contain account or auth details.

Alerts go through `Orchestrator._notify_routed` with event keys
`server-undeployed:<repo>:<sha12>` and `server-tag-unreadable:<repo>:<YYYY-MM-DD>`,
at the default immediate tier.

## State

The throttle state is in `<vault>/Worker/_state/server_deploy_watch.json`. It is
gitignored and derived, like the other alert throttles. It holds the time of the
last check and, per repo, `alerted_sha`/`alerted_ts` and `tag_fail_day`. Deleting
it only means a pending alert may be sent once more.

## Never deploys

The watch only runs read-only git commands (`symbolic-ref`, `ls-remote`,
`cat-file`, `fetch`, `log`, `rev-parse`, `merge-base`) and the configured
`tag_cmd`. The module has no code path that runs `deploy_cmd`. A test checks that,
over a full behind → cleared → tag-failure cycle, every command run is one of
those. To turn the watch off, delete the repo's entry or the whole config file.
No code change is needed.
