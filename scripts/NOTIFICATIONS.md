# Author-only integrations are opt-in

Three integrations ship in this tree but are meaningful only on the author's
home base. Each one is switched on **by the presence of a config file or token
in the vault** — never by a new config key. On a checkout without those files
every one is a *silent* no-op: no progress line, no network call, no per-tick
import, and at most one warning per process for notifications. This keeps a bare
clone quiet while leaving the author's configured behaviour byte-for-byte
unchanged.

The vault directory is resolved the same way `scripts/kakao_send.sh` resolves
it: `KAKAO_VAULT_DIR` (test seam) → `MACP_VAULT_ROOT` (split install) → the
engine checkout itself (combined layout).

## 1. KakaoTalk / Slack notifications

`todo_system.worker.kakao_notify` sends through `scripts/kakao_send.sh`. A
channel is considered **configured** when the vault holds either of:

- `<vault>/.secrets/notify-slack.json` — selects the Slack backend, or
- `<vault>/.secrets/kakao-token.json` — the default Kakao token.

With **neither** file present, `kakao_notify` is a silent no-op after printing
exactly **one** `notifications disabled: …` line to stderr per process (the
daemon runs one tick per process, so the once-guard is in-process by design).
Opt-in noise tiers (`level` / `event_key`, see `KAKAO_ALERTS.md`) still route
first, so a `digest`/`silent` entry is recorded even without a channel; only the
immediate send is suppressed. With a channel present nothing changes.

## 2. App-beta publishing sweep

`todo_system.orchestrator.beta_publish.spawn_sweep_if_due` starts a detached
catch-up sweep. It is gated on:

- `<vault>/.macp/beta_publish_config.json` — the `repos` map of publishable apps.

When that file is missing or its `repos` map is empty, the sweep returns before
any progress line or state write — a checkout with no apps to publish prints
nothing each tick. With a non-empty config the sweep runs as before (min-poll,
HEAD-on-main and already-running gates follow).

## 3. advote bridge

`todo_system.advote_bridge.run_once` bridges advote user suggestions into the
pipeline. It is gated on:

- `<vault>/.secrets/advote.json` — endpoint + per-tenant admin keys.

Absent, `run_once` returns `None` before any poll, notify, or state write, and
the orchestrator's `_advote_bridge_tick` returns before even importing the
module — a bare checkout's tick does no work at all. With the secret present the
bridge polls and reverse-reports as before.
