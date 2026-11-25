# macp documentation

| Read this | When |
|---|---|
| [Getting started](getting-started.md) | first time: the loop without an agent, then a first real run |
| [How it works](concepts.md) | the control plane / data plane split, the life of a task, the planner, the repository map |
| [Task files](tasks.md) | writing tasks: every frontmatter field, worker modes, gating |
| [Running 24/7](running.md) | daemons, systemd and launchd units, the dashboard, several hosts |
| [Dashboard parity](dashboard-parity.md) | what the web page and the Android app render the same way: triage cards and `POST /api/action` |
| [Configuration](configuration.md) | environment variables, usage pacing, accounts, Anthropic usage |
| [Troubleshooting](troubleshooting.md) | a task is not dispatched, a stage fails, the queue is stuck |
| [Server deploy watch](server-deploy-watch.md) | alert when a merged server change is not running in production: config, mindchat example, throttling |
| [Security](../SECURITY.md) | what a worker can do, and how to contain it — before the first live run |
| [scripts/README.md](../scripts/README.md) | what each script in `scripts/` is for |
| [design/](design/) | design notes for work in progress |
