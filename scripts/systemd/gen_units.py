#!/usr/bin/env python3
"""Generate the systemd --user units for the MaestroACP engine control plane on a
Linux host (Ubuntu). Renders only the engine-core units — runner, orchestrator,
planner, maintenance, todo-sync, dashboard, brief, health — so a third party can
run gen_units.py from a bare engine checkout and every rendered ``ExecStart``
resolves to a script that ships in this repository (or a data path under their
vault). Author-only units (stock trading, mailroom, Remote Control, Kakao token
refresh / digest, the logged-in browser, the usage monitor) live in the author's
vault, not in the engine, and are deliberately NOT rendered here.

    python3 scripts/systemd/gen_units.py            # write ~/.config/systemd/user/*
    python3 scripts/systemd/gen_units.py --print    # dry run to stdout
    python3 scripts/systemd/gen_units.py --enable   # write + daemon-reload + enable --now
    python3 scripts/systemd/gen_units.py --engine-python /path/to/venv/bin/python  # installed mode (env: MACP_ENGINE_PYTHON)

Paths: the vault root (data — logs/, Todo/, Worker/, git refs) comes from
``MACP_VAULT_ROOT`` (``MAESTRO_VAULT_DIR`` kept as a backward-compat alias),
defaulting to ``~/Projects/MaestroACP``. Engine-shipped scripts (brief.sh,
orchestrator_tick.sh, …) resolve from the engine checkout (``MACP_ENGINE_ROOT``,
default: this repo), so a rendered default-mode unit never points at a script that
is not on disk.

Installed mode (public split 13c): units whose script ships with the engine run
``<python> -P -m todo_system.engine_script <name>`` with ``MACP_VAULT_ROOT`` set,
so they resolve the script from the installed wheel. The units that ran
``uv run python -m todo_system...`` (runner, maintenance, todo-sync, dashboard —
public split 14) run the same module as ``<python> -P -m ...``, so no macp unit
imports the vault's own todo_system/. Without the knob every unit runs from the
engine checkout via ``uv run`` / the interpreter it already used.

Env: units read ``~/.config/macp/env`` (0600) — PATH/JAVA_HOME/ANDROID_HOME plus the
host-identity knobs — written by ``--write-env`` from the template below when
missing. The dashboard also reads ``~/.config/macp/dashboard.env`` (its token).

Logs go to ``<vault>/logs/<unit>.out.log``, so health_report keeps reading the same
files. Stdlib-only.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
# Vault root (data: logs/, Todo/, Worker/, git refs). MACP_VAULT_ROOT is primary;
# MAESTRO_VAULT_DIR is a backward-compat alias kept for hosts that still set it.
VAULT = Path(os.environ.get("MACP_VAULT_ROOT") or os.environ.get("MAESTRO_VAULT_DIR")
             or HOME / "Projects" / "MaestroACP")
# Engine checkout (code: the engine-shipped scripts a default-mode unit execs).
# Defaults to this repo so a bare checkout renders runnable units; MACP_ENGINE_ROOT
# overrides it (the golden/tests pin a neutral root rather than baking the worktree
# path into the identifier-gate's scanned tree).
ENGINE_ROOT = Path(os.environ.get("MACP_ENGINE_ROOT") or Path(__file__).resolve().parents[2])
UV = HOME / ".local" / "bin" / "uv"
ENV_FILE = HOME / ".config" / "macp" / "env"
# Shared secret for the live dashboard; generated once by --write-env (0600).
DASHBOARD_ENV_FILE = ENV_FILE.parent / "dashboard.env"
UNIT_DIR = HOME / ".config" / "systemd" / "user"
LOGS = VAULT / "logs"

# Host-identity knobs. Neutral defaults: a live host pins its real name in
# ~/.config/macp/env (MACP_HOST_ID / CLAUDE_RC_NAME), which write_env() never
# rewrites, and these only feed ENV_TEMPLATE — so rendered units never depend on
# them. A third party overrides these to render their own env template.
HOST_ID = os.environ.get("MACP_HOST_ID", "macp-host")
CLAUDE_RC_NAME = os.environ.get("MACP_CLAUDE_RC_NAME", HOST_ID)


def _config_value(key: str) -> str:
    """Read a string ``key`` from ``<VAULT>/.macp/config.toml`` (stdlib tomllib), else "".

    gen_units runs as a bare ``python3 scripts/systemd/gen_units.py`` where
    ``todo_system`` is not on sys.path, so this reads the vault config directly
    rather than calling ``paths.primary_calendar_id()`` / ``paths.label_prefix()`` —
    same env→config→neutral resolution, no import-path dependency. Env still wins:
    callers do ``os.environ.get(<VAR>) or _config_value(<key>)``.
    """
    try:
        import tomllib
    except ImportError:
        return ""
    cfg = VAULT / ".macp" / "config.toml"
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    value = data.get(key)
    return value if isinstance(value, str) and value else ""


# Effective calendar id / label prefix: env wins, then this vault's config, then a
# neutral empty default (no personal identifier baked into the engine). An empty
# CALENDAR_ID makes the todo-sync unit omit --calendar-id so the CLI default applies.
CALENDAR_ID = os.environ.get("MACP_CALENDAR_ID") or _config_value("calendar_id")
LABEL_PREFIX = os.environ.get("MACP_LABEL_PREFIX") or _config_value("label_prefix")

ENV_TEMPLATE = f"""# MaestroACP daemon environment (systemd --user EnvironmentFile). 0600.
PATH={HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
HOME={HOME}
JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ANDROID_HOME={HOME}/Android/sdk
ANDROID_SDK_ROOT={HOME}/Android/sdk
PYTHONUNBUFFERED=1
MACP_HOST_ID={HOST_ID}
CLAUDE_RC_NAME={CLAUDE_RC_NAME}
# Pin the daemon's effective label prefix / calendar id in the process env so
# daemonctl.label_prefix() resolves them regardless of the unit's WorkingDirectory
# (env is highest priority). Empty when this host has no config/env → the resolver
# falls through to its neutral default, which is harmless.
MACP_LABEL_PREFIX={LABEL_PREFIX}
MACP_CALENDAR_ID={CALENDAR_ID}
"""


def service(unit: str, desc: str, exec_start: str, *, cwd: Path = VAULT,
            kind: str = "oneshot", restart: str | None = None, restart_sec: int = 30,
            extra_env: Path | None = None, wants_network: bool = True,
            extra_lines: list[str] | None = None, timeout_start_sec: int = 1800,
            unit_lines: list[str] | None = None, install: bool | None = None) -> str:
    lines = ["[Unit]", f"Description={desc}"]
    if wants_network:
        lines += ["After=network-online.target", "Wants=network-online.target"]
    lines += unit_lines or []
    lines += ["", "[Service]", f"Type={'simple' if kind == 'simple' else 'oneshot'}",
              f"WorkingDirectory={cwd}", f"EnvironmentFile={ENV_FILE}"]
    if extra_env:
        lines.append(f"EnvironmentFile={extra_env}")
    lines += [f"ExecStart={exec_start}",
              f"StandardOutput=append:{LOGS}/{unit}.out.log",
              f"StandardError=append:{LOGS}/{unit}.err.log"]
    # Type=oneshot's TimeoutStartSec default is `infinity`; a hung internal git
    # fetch/clone would then occupy the unit forever and block the next timer
    # tick. Meaningless for Type=simple (long-running daemons), so skip it there.
    if kind != "simple":
        lines.append(f"TimeoutStartSec={timeout_start_sec}")
    if restart:
        lines += [f"Restart={restart}", f"RestartSec={restart_sec}"]
    lines += extra_lines or []
    # A timerless oneshot is still an enable_targets() target; `install=True` gives
    # it the [Install] section `systemctl enable` needs (default: simple only).
    if install if install is not None else kind == "simple":
        lines += ["", "[Install]", "WantedBy=default.target"]
    return "\n".join(lines) + "\n"


def timer(unit: str, desc: str, *, every: str | None = None, calendar: list[str] | None = None,
          boot_delay: str = "2min") -> str:
    lines = ["[Unit]", f"Description={desc} (timer)", "", "[Timer]"]
    if every:
        lines += [f"OnBootSec={boot_delay}", f"OnUnitActiveSec={every}"]
    for c in calendar or []:
        lines.append(f"OnCalendar={c}")
    if calendar:
        lines.append("Persistent=true")
    lines += [f"Unit={unit}.service", "", "[Install]", "WantedBy=timers.target"]
    return "\n".join(lines) + "\n"


def build(engine_python: str | None = None) -> dict[str, str]:
    """All units. ``engine_python`` (default: env ``MACP_ENGINE_PYTHON``; empty =
    off) is the absolute interpreter of an installed engine and turns on installed
    mode for the engine-script units."""
    if engine_python is None:
        engine_python = os.environ.get("MACP_ENGINE_PYTHON", "")
    if engine_python and not Path(engine_python).is_absolute():
        raise ValueError(f"engine python must be an absolute path (systemd ExecStart): {engine_python}")

    def script(unit: str, desc: str, legacy_exec: str, name: str, **kw) -> str:
        # Default mode: exactly the legacy service() call (byte-identical units).
        if not engine_python:
            return service(unit, desc, legacy_exec, **kw)
        # -P: WorkingDirectory is the vault, which may still be a checkout with its
        # own todo_system/ — without it that copy would shadow the installed one.
        kw["extra_lines"] = [*(kw.get("extra_lines") or []), f"Environment=MACP_VAULT_ROOT={VAULT}"]
        return service(unit, desc, f"{engine_python} -P -m todo_system.engine_script {name}", **kw)

    def module(unit: str, desc: str, legacy_exec: str, installed_exec: str, **kw) -> str:
        # Resident/per-tick units that run ``python -m todo_system...`` (or a
        # Python engine script) through ``uv run`` in the vault (public split 14).
        # Installed mode runs the same command from the installed interpreter, so
        # once the vault's todo_system/ is gone nothing still imports it.
        if not engine_python:
            return service(unit, desc, legacy_exec, **kw)
        kw["extra_lines"] = [f"Environment=MACP_VAULT_ROOT={VAULT}", *(kw.get("extra_lines") or [])]
        return service(unit, desc, f"{engine_python} -P {installed_exec}", **kw)

    u: dict[str, str] = {}
    py = f"{UV} run python"

    # -- MaestroACP control plane --
    runner_args = "--agent worker-default --mode live-staged --uri file://Worker/agents --daemon"
    u["macp-runner-worker-default.service"] = module(
        "macp-runner-worker-default", "macp worker-default runner (v2 staged pipeline)",
        f"{py} -m todo_system.orchestrator.runner {runner_args}",
        f"-m todo_system.orchestrator.runner {runner_args}",
        # OOMPolicy=continue: a build the runner spawned dying to the kernel OOM
        # killer must not take the whole runner unit (and its in-flight run) down
        # with it — the default `stop` kills every process in the cgroup. Only the
        # runner gets this; the control-plane daemons should still restart on OOM.
        kind="simple", restart="always", extra_lines=["OOMPolicy=continue"])
    u["macp-orchestrator.service"] = script(
        "macp-orchestrator", "macp orchestrator tick (dispatch/collect/dashboard)",
        f"/bin/zsh {ENGINE_ROOT}/scripts/orchestrator_tick.sh", "orchestrator_tick.sh")
    u["macp-orchestrator.timer"] = timer("macp-orchestrator", "macp orchestrator", every="5min")
    u["macp-planner.service"] = script(
        "macp-planner", "macp planner tick", f"/bin/bash {ENGINE_ROOT}/scripts/planner_tick.sh",
        "planner_tick.sh")
    u["macp-planner.timer"] = timer("macp-planner", "macp planner", every="30min", boot_delay="5min")
    u["macp-maintenance.service"] = module(
        "macp-maintenance", "macp maintenance (vault sync, daemon restart, upkeep)",
        f"{py} -m todo_system.worker maintenance", "-m todo_system.worker maintenance")
    u["macp-maintenance.timer"] = timer("macp-maintenance", "macp maintenance", every="5min")
    cal_arg = f" --calendar-id {CALENDAR_ID}" if CALENDAR_ID else ""
    sync_args = f"daemon --interval 3 --calendar-sync{cal_arg}"
    u["macp-todo-sync.service"] = module(
        "macp-todo-sync", "macp todo-sync daemon (md -> todo.db, calendar)",
        f"{py} -u -m todo_system.cli {sync_args}", f"-u -m todo_system.cli {sync_args}",
        kind="simple", restart="always")
    # Loopback-only; remote access = `tailscale serve` in front of it (HTTPS on the
    # tailnet). The token file is there so the same unit can never run open if
    # someone later flips --host (the script refuses a non-loopback bind without it).
    dash_args = "--host 127.0.0.1 --port 8787 --no-open"
    u["macp-dashboard.service"] = module(
        "macp-dashboard", "macp live dashboard (loopback :8787; tailscale serve fronts it)",
        f"{py} -u {ENGINE_ROOT}/scripts/macp_dashboard.py {dash_args}",
        f"-u -m todo_system.engine_script macp_dashboard.py {dash_args}",
        kind="simple", restart="always", restart_sec=10, extra_env=DASHBOARD_ENV_FILE)
    u["macp-brief.service"] = script("macp-brief", "macp morning brief", f"/bin/zsh {ENGINE_ROOT}/scripts/brief.sh",
                                     "brief.sh")
    u["macp-brief.timer"] = timer("macp-brief", "macp morning brief", calendar=["*-*-* 08:30:00"])
    u["macp-health.service"] = script(
        "macp-health", "macp weekly health report", f"/usr/bin/python3 {ENGINE_ROOT}/scripts/health_report.py",
        "health_report.py")
    u["macp-health.timer"] = timer("macp-health", "macp weekly health report", calendar=["Sun *-*-* 08:30:00"])
    return u


def _backfill_identity(path: Path) -> None:
    """Append the identity keys an env file written before they existed lacks.

    write_env() never rewrites an existing env file (it holds hand-filled
    secrets), so a host installed before MACP_LABEL_PREFIX was added kept running
    without it: units with WorkingDirectory=$HOME cannot see .macp/config.toml,
    fell back to the neutral com.macp. prefix, and daemonctl could no longer map
    the host's pinned labels to units (claude-upgrade failed to restart remote-control,
    2026-09-26). Only missing keys with a known value are appended."""
    present = {ln.split("=", 1)[0].strip() for ln in path.read_text().splitlines()
               if "=" in ln and not ln.lstrip().startswith("#")}
    add = [f"{k}={v}" for k, v in (("MACP_LABEL_PREFIX", LABEL_PREFIX),
                                   ("MACP_CALENDAR_ID", CALENDAR_ID))
           if v and k not in present]
    if add:
        with path.open("a") as f:
            f.write("\n".join(add) + "\n")
        print(f"backfilled {path}: {', '.join(a.split('=')[0] for a in add)}")


def write_env() -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists():
        print(f"keep {ENV_FILE}")
        _backfill_identity(ENV_FILE)
    else:
        ENV_FILE.write_text(ENV_TEMPLATE)
        ENV_FILE.chmod(0o600)
        print(f"wrote {ENV_FILE} (0600)")
    if DASHBOARD_ENV_FILE.exists():
        print(f"keep {DASHBOARD_ENV_FILE}")
    else:
        DASHBOARD_ENV_FILE.write_text(f"MACP_DASHBOARD_TOKEN={secrets.token_urlsafe(24)}\n")
        DASHBOARD_ENV_FILE.chmod(0o600)
        print(f"wrote {DASHBOARD_ENV_FILE} (random dashboard token)")


def enable_targets(units: dict[str, str], only: list[str] | None) -> list[str]:
    """Unit file names to ``systemctl enable --now`` for, given an optional
    ``--only`` stem filter: every timer, and every timerless service (the simple
    daemons). All rendered units are engine-core, so there is nothing to suppress."""
    targets: list[str] = []
    for name in units:
        stem, kind = name.rsplit(".", 1)
        if only is not None and stem not in only:
            continue
        if kind == "timer" or f"{stem}.timer" not in units:
            targets.append(name)   # timers, and services with no timer (simple daemons)
    return targets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="dry run: print units to stdout")
    ap.add_argument("--enable", action="store_true", help="daemon-reload + enable --now timers/services")
    ap.add_argument("--only", nargs="*", default=None, help="unit stems to enable (default: all)")
    ap.add_argument("--write-env", action="store_true", help="create env files from templates if missing")
    ap.add_argument("--engine-python", default=None, metavar="PATH",
                    help="installed mode: absolute interpreter of the installed engine "
                         "(default: env MACP_ENGINE_PYTHON; empty = off)")
    args = ap.parse_args(argv)

    try:
        units = build(args.engine_python)
    except ValueError as e:
        ap.error(str(e))
    if args.print:
        for name, text in units.items():
            print(f"##### {name}\n{text}")
        return 0
    if args.write_env:
        write_env()
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (UNIT_DIR / name).write_text(text)
    print(f"wrote {len(units)} units to {UNIT_DIR}")
    if not args.enable:
        return 0
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    targets = enable_targets(units, args.only)
    proc = subprocess.run(["systemctl", "--user", "enable", "--now", *targets],
                          capture_output=True, text=True)
    print(proc.stdout or proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
