"""Stage-facing CLI behind ``scripts/macp-emulator`` — a pre-merge on-device
check that reuses :func:`beta_smoke.run_smoke` to drive the shared on-demand
emulator node.

The same split :mod:`beta_publish` / :mod:`screen_check` / :mod:`remote_gate`
already make: the node round-trip (gcloud/ssh/scp, build, boot, scenario) lives
in :mod:`beta_smoke`, which the orchestrator *runtime* also calls for the beta
sweep. What is new here is a SPACE-FREE wrapper name a worker stage's
``--allowed-tools`` whitelist can grant (``Bash(macp-emulator:*)`` —
:data:`stages.EMULATOR_TOOL_RULE`), so a stage can request one on-device scenario
run without block-polling it and without a task ``allowed_tools`` edit.

Subcommands (see the wrapper header):

* ``run --repo-dir <checkout> --sha <sha> --scenario <json|name> [--seed owner|none]``
  — build→install→(seed)→scenario→logcat→**always stop**, print an
  AGGREGATES-ONLY result JSON to stdout, and map the result to an exit code:
  ``0`` PASS · ``75`` node lease held by another owner · ``76`` over the shared
  monthly budget · ``1`` any real failure (build/scenario/crash/node/config).
  Progress lines go to stderr so stdout stays pure JSON.
* ``status`` — read-only: the node lease state, month spend, cap, over-budget.
* ``stop`` — operator-only node teardown; refuses (``64``) when invoked INSIDE a
  stage (the orchestrator injects :data:`stages.EMULATOR_OWNER_ENV`), since a
  stage must never stop the node another run — or the beta sweep — is using.

The shared node lease is :mod:`beta_smoke`'s ``_NODE_LOCK``; the holder is this
run's run_id (``--owner`` > ``$MACP_EMULATOR_OWNER`` > ``<host>:<pid>``), so a
worker run and the beta sweep serialize on the ONE physical node and can never
co-drive it. Node identity/zone and the owner-seed SOURCE path are read at
runtime from the vault's ``.macp/remote_gate.json`` (via :mod:`remote_gate`),
never hardcoded here — the public engine/vault split. Aggregates only: the seed
DB, screenshots and uiautomator dumps stay in the gitignored result dir and are
NEVER echoed into the result JSON (personal-data rule).

Stdlib-only at import (``check_engine_isolation.sh`` archives and imports the
tree); the engine siblings it imports are themselves stdlib-only at import.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import beta_smoke
from . import lease
from . import remote_gate
from . import stages

_OWNER_ENV = stages.EMULATOR_OWNER_ENV
_DEFAULT_SEED_DB = "app.db"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 64            # unknown subcommand / operator-only inside a stage
EXIT_LEASE_BUSY = 75       # the shared node lease is held by another owner
EXIT_OVER_BUDGET = 76      # over the shared monthly cost cap


def _stderr_progress(msg: str) -> None:
    """run_smoke progress → stderr, so stdout carries ONLY the result JSON."""
    print(msg, file=sys.stderr)


def _resolve_owner(explicit: str | None) -> str | None:
    """``--owner`` > ``$MACP_EMULATOR_OWNER`` > None. None lets ``run_smoke`` /
    ``lease.Lease`` fall back to the default ``<host>:<pid>`` holder."""
    return explicit or os.environ.get(_OWNER_ENV) or None


def _resolve_scenario_path(scenario: str, repo_dir: Path) -> Path:
    """``--scenario`` is either a literal JSON file path (used as-is when it
    exists) or a bare NAME resolving to ``<repo-dir>/.macp/emulator/<name>.json``
    — app-specific locators/steps live in the TARGET repo, engine stays neutral."""
    p = Path(scenario).expanduser()
    if p.is_file():
        return p
    return Path(repo_dir) / ".macp" / "emulator" / f"{scenario}.json"


def _load_scenario(scenario: str, repo_dir: Path) -> dict:
    """Load the scenario config dict (the ``smoke_cfg`` ``run_smoke`` consumes).
    Raises on a missing/unreadable file or a non-object payload."""
    path = _resolve_scenario_path(scenario, repo_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("scenario JSON must be an object")
    return data


def _apply_seed(config: dict, seed: str, remote_cfg: dict) -> dict:
    """``--seed none`` → config unchanged. ``--seed owner`` → inject
    ``config["seed"] = {"source": <owner_seed_source>, "db_name": …}`` where the
    SOURCE is read at runtime from ``.macp/remote_gate.json`` (``owner_seed_source``
    key) so the HQ path never appears in engine code, the result JSON or a commit.
    Raises when the key is absent — silently skipping the seed would smoke-test the
    wrong (empty) state."""
    if seed != "owner":
        return config
    src = (remote_cfg or {}).get("owner_seed_source")
    if not src:
        raise ValueError(
            "--seed owner requested but .macp/remote_gate.json carries no "
            "owner_seed_source key")
    existing = config.get("seed") if isinstance(config.get("seed"), dict) else {}
    db_name = str(existing.get("db_name") or _DEFAULT_SEED_DB)
    out = dict(config)
    out["seed"] = {"source": str(src), "db_name": db_name}
    return out


def _exit_for(res: dict) -> int:
    """Map a ``run_smoke`` result to the exit-code contract. Order matters: a
    lock-fail result is ``ok=False, kind="node", locked=True`` and must read as
    75, not 1; a budget trip is ``kind="budget"`` → 76; PASS → 0; everything else
    (scenario/crash/build/node/config) is a real failure → 1."""
    if res.get("locked"):
        return EXIT_LEASE_BUSY
    if res.get("kind") == "budget":
        return EXIT_OVER_BUDGET
    if res.get("ok"):
        return EXIT_OK
    return EXIT_FAILURE


def _config_error(detail: str) -> int:
    print(json.dumps({"ok": False, "kind": "config", "detail": detail},
                     ensure_ascii=False))
    return EXIT_FAILURE


def _cmd_run(args, *, run, node_script, lock_path, result_root, budget_path,
             remote_cfg, notify, now) -> int:
    repo_dir = Path(args.repo_dir).expanduser()
    owner = _resolve_owner(args.owner)
    try:
        config = _load_scenario(args.scenario, repo_dir)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return _config_error(f"scenario load failed ({type(e).__name__}): {e}")

    # Load the remote config once (for the owner-seed source) and reuse it as
    # run_smoke's remote_cfg so the real config is read at most once per run.
    cfg = remote_cfg
    if args.seed == "owner" and cfg is None:
        cfg = remote_gate.load_remote_config()
    try:
        config = _apply_seed(config, args.seed, cfg or {})
    except ValueError as e:
        return _config_error(str(e))

    kwargs: dict = {"run": run, "lock_holder": owner, "progress": _stderr_progress,
                    "remote_cfg": cfg}
    if node_script is not None:
        kwargs["node_script"] = node_script
    if lock_path is not None:
        kwargs["lock_path"] = lock_path
    if result_root is not None:
        kwargs["result_root"] = result_root
    if budget_path is not None:
        kwargs["budget_path"] = budget_path
    if notify is not None:
        kwargs["notify"] = notify
    if now is not None:
        kwargs["now"] = now

    repo_name = repo_dir.name or "repo"
    res = beta_smoke.run_smoke(repo_name, repo_dir, args.sha, config, **kwargs)
    # res is aggregates-only by construction (no seed path / dump text / bytes).
    print(json.dumps(res, ensure_ascii=False))
    return _exit_for(res)


def _cmd_status(*, lock_path, budget_path, remote_cfg) -> int:
    lp = Path(lock_path) if lock_path is not None else beta_smoke._NODE_LOCK
    bp = budget_path if budget_path is not None else remote_gate.BUDGET_PATH
    cfg = remote_cfg if remote_cfg is not None else remote_gate.load_remote_config()
    info = lease.Lease(str(lp)).current()
    out = {
        "node_lease": {
            "state": "held" if info else "free",
            "holder": info.holder if info else None,
            "expires_at": info.expires_at if info else None,
        },
        "month_spent_usd": round(float(remote_gate.month_spent_usd(path=bp)), 6),
        "monthly_cap_usd": round(float(remote_gate.monthly_cap_usd(cfg)), 6),
        "over_budget": remote_gate.budget_exhausted(cfg, path=bp),
    }
    print(json.dumps(out, ensure_ascii=False))
    return EXIT_OK


def _cmd_stop(*, run, node_script, remote_cfg) -> int:
    # Human-only: the orchestrator injects MACP_EMULATOR_OWNER into a stage's
    # env, so its presence means a stage is calling → refuse. A human on the box
    # has no such env and may stop the node.
    if os.environ.get(_OWNER_ENV):
        print(json.dumps(
            {"ok": False, "kind": "human_only",
             "detail": "stop is operator-only; a stage must not stop the shared node"},
            ensure_ascii=False), file=sys.stderr)
        return EXIT_USAGE
    ns = node_script if node_script is not None else beta_smoke._NODE_SCRIPT
    cfg = remote_cfg if remote_cfg is not None else remote_gate.load_remote_config()
    env = dict(os.environ)
    node = (cfg or {}).get("node")
    zone = (cfg or {}).get("zone")
    if node:
        env["APP_TEST_NODE"] = str(node)
    if zone:
        env["APP_TEST_ZONE"] = str(zone)
    r = beta_smoke._node(run, ns, "stop", env=env, timeout=beta_smoke._STOP_TIMEOUT_S)
    rc = getattr(r, "returncode", None)
    print(json.dumps({"ok": rc == 0, "kind": None if rc == 0 else "node",
                      "detail": f"node stop rc={rc}"}, ensure_ascii=False))
    return EXIT_OK if rc == 0 else EXIT_FAILURE


def _main(argv: list[str] | None = None, *, run=None, node_script=None,
          lock_path=None, result_root=None, budget_path=None, remote_cfg=None,
          notify=None, now=None) -> int:
    """``macp-emulator <status|run|stop>``. All node work flows through
    ``run_smoke``'s own always-stop ``finally``; this CLI only parses, resolves
    config, and formats — a pre-``run_smoke`` crash never started a node.

    Keyword seams (``run``/``node_script``/``lock_path``/``result_root``/
    ``budget_path``/``remote_cfg``/``notify``/``now``) let the unit suite drive a
    fake and tmp paths; runtime invocations pass none and fall through to the
    beta_smoke / remote_gate defaults."""
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: macp-emulator <status|run|stop> …", file=sys.stderr)
        return EXIT_USAGE
    action, rest = argv[0], argv[1:]
    run = run if run is not None else beta_smoke._run

    if action == "status":
        argparse.ArgumentParser(prog="macp-emulator status").parse_args(rest)
        return _cmd_status(lock_path=lock_path, budget_path=budget_path,
                           remote_cfg=remote_cfg)

    if action == "run":
        p = argparse.ArgumentParser(prog="macp-emulator run")
        p.add_argument("--repo-dir", required=True)
        p.add_argument("--sha", required=True)
        p.add_argument("--scenario", required=True,
                       help="literal <path.json> or a NAME → <repo-dir>/.macp/emulator/<name>.json")
        p.add_argument("--seed", choices=("owner", "none"), default="none")
        p.add_argument("--owner", default=None)
        args = p.parse_args(rest)
        return _cmd_run(args, run=run, node_script=node_script, lock_path=lock_path,
                        result_root=result_root, budget_path=budget_path,
                        remote_cfg=remote_cfg, notify=notify, now=now)

    if action == "stop":
        argparse.ArgumentParser(prog="macp-emulator stop").parse_args(rest)
        return _cmd_stop(run=run, node_script=node_script, remote_cfg=remote_cfg)

    print(f"usage: macp-emulator <status|run|stop> — unknown subcommand {action!r}",
          file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(_main())
