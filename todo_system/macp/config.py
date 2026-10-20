"""MaestroACP context isolation — manage a project's control-plane context like a
Python virtualenv.

Two layers, mirroring uv/pyenv/venv:
  - `~/.macp/` (user home, override with $MACP_HOME): global config + named
    **profiles**. A profile is an isolated control plane — its own vault (ledger,
    todo.db, Memory), budget, model, and a set of Claude Code **accounts** (each an
    isolated CLAUDE_CONFIG_DIR → parallel capacity across accounts). Split work vs
    personal by using different profiles, exactly like separate venvs.
  - `<project>/.macp/config.toml` — marks a directory as MaestroACP-managed (like
    `.git` marks a repo) and pins which profile/account/worker_mode runs it.

Stdlib-only: tomllib to read; a tiny hand-rolled writer for our fixed schema.
"""
from __future__ import annotations

import os
try:
    import tomllib
except ImportError:          # Python < 3.11 (macOS launchd's bare python3 is 3.9):
    tomllib = None           # every profile/project reads as absent = pre-profile behaviour
from dataclasses import dataclass, field
from pathlib import Path

from .accounts import (
    LEGACY_CONFIG_DIR, UPSTAGE_KEY_ENV, config_dir_for, credentials_file_for,
    load_runtime_state, read_api_key, resolve_active, token_file_for,
)

HOME = Path(os.environ.get("MACP_HOME") or (Path.home() / ".macp"))
PROFILES = HOME / "profiles"
GLOBAL_CONFIG = HOME / "config.toml"
PROJECT_DIR = ".macp"                  # marker dir inside a managed project

WORKER_MODES = ("auto", "quick", "review", "delegate", "discover", "manual")

# Default model_tier → Upstage Solar model id. These are DATE-PINNED snapshots,
# the only ids whose existence was measured (2026-10-19); undated aliases are
# not known to be served, and an alias default that 404s turns a live smoke into
# "model not found" instead of a measurement. Swap a pin per account with
# `macp account add … --models light=<id>` / `models = { … }` in the profile —
# that configurability is the point, not the default value.
SOLAR_TIER_MODELS = {
    "heavy": "solar-pro4-260806",
    "standard": "solar-pro4-260806",
    "light": "solar-mini4-260922",
}


@dataclass
class Account:
    """One agent execution identity. Multiple accounts under a profile = parallel
    capacity, and they can be from **different vendors** (Claude Code, OpenAI
    Codex, Cursor, Gemini CLI, Aider, …).
      - agent: vendor id ('claude' default, or codex/cursor/gemini/aider/custom).
      - claude_config_dir: auth isolation for Claude (CLAUDE_CONFIG_DIR).
      - command: invocation template for non-Claude agents; '{prompt}' is
        substituted (e.g. 'codex exec {prompt}', 'aider --message {prompt}').
      - env: extra environment (config dirs, non-secret settings) for this
        account. **Never an API key**: `write_profile` serializes this verbatim
        into the profile TOML and `account list` prints `command`, so a secret
        placed here leaks. Keys are read at call time —
        `accounts.read_api_key` for upstage; see `key_file` below.
      - priority: selection order among a profile's accounts (lower first,
        ties by listing order; default 100). Written only when non-default.
      - key_file: for a key-authenticated vendor (upstage), the path to the file
        holding `UPSTAGE_API_KEY=…` (0600). The PATH only — the value is
        resolved by `accounts.read_api_key` when a call is made.
      - models: model_tier → vendor model id overrides (e.g.
        `{"light": "solar-mini4-260922"}`); see `solar_model_for`.
    The last two are appended: the schema test pins existing field order."""
    name: str
    agent: str = "claude"
    claude_config_dir: str = LEGACY_CONFIG_DIR
    command: str = ""
    env: dict = field(default_factory=dict)
    priority: int = 100
    key_file: str = ""
    models: dict = field(default_factory=dict)


@dataclass
class Profile:
    name: str
    vault: str = ""                    # dir holding Worker/ledger, todo.db, Memory
    daily_run_budget: int = 40
    # Cap on concurrent worker runs that share ONE repo checkout. Default 2 =
    # concurrency ON (leaf 5 flipped it from the historical 1 now that worktree
    # isolation + the touches-overlap judge are merged); disjoint-`touches`
    # same-repo tasks may run together, everything else stays serial. It is a
    # SUB-constraint of the global usage_guard slot pacing, never a raise of it.
    max_parallel_per_repo: int = 2
    model: str = "auto"                # auto | claude-fable-5-1 | claude-opus-5-5 | claude-opus-4-8
    accounts: list[Account] = field(default_factory=list)
    # Name of the account to prefer (empty = none pinned → lowest priority wins).
    # Written only when set, so a profile that never used it is byte-identical.
    active_account: str = ""
    # Cross-checkout cap on concurrent runs whose checkout is a Gradle project
    # (orchestrator.max_parallel_gradle_builds — the memory guard for Android
    # builds on a small host). 0 = unlimited (dormant). Until 2026-10-18 the
    # orchestrator read this off the profile with getattr(..., 0) but the field
    # did not exist here, so `max_parallel_gradle_builds = N` in config.toml was
    # silently ignored and only the MACP_MAX_PARALLEL_GRADLE_BUILDS env worked.
    # Appended last: the schema test pins existing field order.
    max_parallel_gradle_builds: int = 0


@dataclass
class ProjectConfig:
    profile: str = "default"
    worker_mode: str = "auto"
    capabilities: list[str] = field(default_factory=list)
    # Pin one of the profile's accounts; without a pin the profile's active_account /
    # lowest priority decides (current_account). account_for() keeps its round-robin.
    account: str | None = None
    # --- cross-project links: some projects (e.g. advote's feedback SDK) are
    #     consumed by others, so a change upstream must fan out to downstream. ---
    name: str = ""                     # canonical project id (default: dir basename)
    provides: str = ""                 # the interface/lib this project exposes, if any
    depends_on: list[str] = field(default_factory=list)   # upstream project names consumed


# ---- read -----------------------------------------------------------------

def _read_toml(p: Path) -> dict:
    if tomllib is None:                # no TOML parser → no profile → pre-profile behaviour
        return {}
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def default_profile() -> str:
    return _read_toml(GLOBAL_CONFIG).get("default_profile", "default")


def load_profile(name: str) -> Profile | None:
    d = _read_toml(PROFILES / name / "config.toml")
    if not d:
        return None
    accounts = [Account(name=a.get("name", ""), agent=a.get("agent", "claude"),
                        claude_config_dir=a.get("claude_config_dir", LEGACY_CONFIG_DIR),
                        command=a.get("command", ""), env=dict(a.get("env", {})),
                        priority=int(a.get("priority", 100)),
                        key_file=a.get("key_file", ""),
                        models=dict(a.get("models", {})))
                for a in d.get("account", [])]
    return Profile(name=name, vault=d.get("vault", ""),
                   daily_run_budget=int(d.get("daily_run_budget", 40)),
                   max_parallel_per_repo=int(d.get("max_parallel_per_repo", 2)),
                   max_parallel_gradle_builds=int(d.get("max_parallel_gradle_builds", 0)),
                   model=d.get("model", "auto"), accounts=accounts,
                   active_account=str(d.get("active_account", "") or ""))


def load_project(project_root: Path) -> ProjectConfig | None:
    d = _read_toml(Path(project_root) / PROJECT_DIR / "config.toml")
    if not d:
        return None
    return ProjectConfig(profile=d.get("profile", "default"),
                         worker_mode=d.get("worker_mode", "auto"),
                         capabilities=list(d.get("capabilities", [])),
                         account=d.get("account"),
                         name=d.get("name", ""),
                         provides=d.get("provides", ""),
                         depends_on=list(d.get("depends_on", [])))


def list_profiles() -> list[str]:
    if not PROFILES.exists():
        return []
    return sorted(p.name for p in PROFILES.iterdir() if (p / "config.toml").exists())


def find_project_root(start: Path | str | None = None) -> Path | None:
    """Walk up from `start` (default cwd) for a `.macp/config.toml` — like git finds
    `.git`. Requiring config.toml (not just a `.macp/` dir) means a detected root is
    always a real managed project (load_project won't return None), and we skip
    MACP_HOME itself — its `.macp` holds profiles/global config, not a project — so
    an unmanaged dir under $HOME isn't mistaken for one."""
    cur = Path(start or Path.cwd()).resolve()
    home = HOME.resolve()
    for d in (cur, *cur.parents):
        marker = d / PROJECT_DIR
        if marker.resolve() == home:
            continue                             # MACP_HOME, not a managed project
        if (marker / "config.toml").is_file():
            return d
    return None


def resolve(start: Path | str | None = None) -> dict:
    """The effective control-plane context for a location: its project `.macp`
    config + the profile it points at (or the global default profile)."""
    root = find_project_root(start)
    proj = load_project(root) if root else None
    if proj is None:
        root = None                    # keep project_root and project consistent:
                                       # never report a root without a real project
    prof_name = proj.profile if proj else default_profile()
    return {"project_root": str(root) if root else None,
            "project": proj, "profile_name": prof_name,
            "profile": load_profile(prof_name)}


def solar_model_for(account: Account | None, tier: str) -> str:
    """The Upstage Solar model id for a ``model_tier`` — the account's ``models``
    override first, then :data:`SOLAR_TIER_MODELS`. An unknown tier string falls
    back to ``standard`` rather than raising, so a new worker tier name never
    breaks a dispatch."""
    over = (getattr(account, "models", None) or {}) if account is not None else {}
    return str(over.get(tier) or SOLAR_TIER_MODELS.get(tier)
               or SOLAR_TIER_MODELS["standard"])


def account_for(profile: Profile, project: ProjectConfig | None, task_id: str = "",
                agent: str | None = "claude") -> Account | None:
    """Pick the account to run with: the project's pinned account if set, else
    round-robin the profile's accounts (stable per task_id for reproducibility).

    Only accounts of ``agent`` are candidates (default ``claude``, as in
    ``accounts.pick_account``; ``agent=None`` considers every account). The
    filter is what keeps the round-robin stable when a profile gains a
    non-claude account: the index is ``hash(task_id) % len(candidates)``, so
    without it registering one upstage account would re-shuffle every existing
    task's claude mapping.
    """
    if not profile or not profile.accounts:
        return None
    cands = [a for a in profile.accounts
             if agent is None or getattr(a, "agent", "claude") == agent]
    if not cands:
        return None
    if project and project.account:
        for a in cands:
            if a.name == project.account:
                return a
    idx = (hash(task_id) % len(cands)) if task_id else 0
    return cands[idx]


def current_account(start: Path | str | None = None) -> tuple[Account | None, str]:
    """The claude account work at ``start`` (default: cwd) runs as, with the rule
    that chose it — ``resolve(start)`` fed to ``accounts.resolve_active`` (project
    pin → runtime override → profile active_account → priority, each skipping an
    account the runtime state marks unavailable). The runtime state is read from
    ``accounts.load_runtime_state()`` (``MACP_CLAUDE_ACCOUNTS_STATE`` or the
    vault's ``Worker/_state/claude_accounts.json``; absent = no filter).
    ``(None, "none")`` when the profile is missing or has no claude account: the
    pre-account behaviour every consumer keeps (runner, claude_auth, transcript
    root, usage guard, dashboard)."""
    ctx = resolve(start)
    return resolve_active(ctx.get("profile"), ctx.get("project"), state=load_runtime_state())


def active_config_dir(start: Path | str | None = None) -> Path:
    """``CLAUDE_CONFIG_DIR`` of :func:`current_account` (the legacy dir when none)."""
    return config_dir_for(current_account(start)[0])


def active_token_file(start: Path | str | None = None) -> Path:
    """``automation-token`` of :func:`current_account` (the legacy dir when none)."""
    return token_file_for(current_account(start)[0])


def active_credentials_file(start: Path | str | None = None) -> Path:
    """``.credentials.json`` of :func:`current_account` (the legacy dir when none)."""
    return credentials_file_for(current_account(start)[0])


def build_invocation(account: Account, prompt: str,
                     extra_args: list[str] | None = None) -> tuple[list[str], dict]:
    """(argv, env) to run `prompt` with this account's agent — vendor-neutral.
    - claude (default / no command): `claude -p <prompt> [extra_args]` + CLAUDE_CONFIG_DIR.
    - other vendor: account.command with '{prompt}' substituted (appended if no
      placeholder), plus account.env. `extra_args` (claude-specific flags) are
      ignored for non-claude agents.
    - upstage: HTTP-only by default. Without a `command` there is no CLI to run,
      so this RAISES ValueError instead of falling through to the claude branch
      (`agent == "claude" or not account.command`) and silently launching
      `claude` under an upstage account's name — use `upstage.chat()` for the
      API path. With a `command` it takes the generic vendor branch and the key
      is injected into the returned env at call time (`accounts.read_api_key`,
      never from the profile).

    The returned env is the caller's to pass to a subprocess; this function
    prints nothing, so an injected key stays out of every log.
    """
    import shlex
    env = dict(account.env or {})
    if account.agent == "upstage":
        if not account.command:
            raise ValueError(
                f"upstage account '{account.name}' has no CLI command; "
                "call the HTTP client (todo_system.macp.upstage.chat) instead")
        key = read_api_key(account)
        if key:
            env[UPSTAGE_KEY_ENV] = key
        argv = [p.replace("{prompt}", prompt) for p in shlex.split(account.command)]
        if "{prompt}" not in account.command:
            argv.append(prompt)
        return argv, env
    if account.agent == "claude" or not account.command:
        argv = ["claude", "-p", prompt, *(extra_args or [])]
        cfg = os.path.expanduser(account.claude_config_dir or "")
        # only override for a non-default config dir (forcing the legacy default dir
        # makes claude ignore its normal auth store, e.g. the macOS keychain)
        if cfg and cfg != os.path.expanduser(LEGACY_CONFIG_DIR):
            env["CLAUDE_CONFIG_DIR"] = cfg
        return argv, env
    argv = [p.replace("{prompt}", prompt) for p in shlex.split(account.command)]
    if "{prompt}" not in account.command:
        argv.append(prompt)
    return argv, env


# ---- write (minimal TOML for our fixed schema) ----------------------------

def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _arr(vals: list[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in vals) + "]"


def write_project(project_root: Path | str, cfg: ProjectConfig) -> Path:
    lines = [f'profile = "{cfg.profile}"',
             f'worker_mode = "{cfg.worker_mode}"',
             f'capabilities = {_arr(cfg.capabilities)}']
    if cfg.account:
        lines.append(f'account = "{cfg.account}"')
    if cfg.name:
        lines.append(f'name = "{cfg.name}"')
    if cfg.provides:
        lines.append(f'provides = "{cfg.provides}"')
    if cfg.depends_on:
        lines.append(f'depends_on = {_arr(cfg.depends_on)}')
    path = Path(project_root) / PROJECT_DIR / "config.toml"
    _write(path, "\n".join(lines))
    return path


def write_profile(prof: Profile) -> Path:
    lines = [f'name = "{prof.name}"', f'vault = "{prof.vault}"',
             f'daily_run_budget = {prof.daily_run_budget}',
             f'max_parallel_per_repo = {prof.max_parallel_per_repo}',
             f'model = "{prof.model}"']
    if prof.max_parallel_gradle_builds:
        # Written only when set, so a profile that never used it is byte-identical.
        lines.append(f'max_parallel_gradle_builds = {prof.max_parallel_gradle_builds}')
    if prof.active_account:
        lines.append(f'active_account = "{prof.active_account}"')
    for a in prof.accounts:
        lines += ["", "[[account]]", f'name = "{a.name}"', f'agent = "{a.agent}"']
        if a.agent == "claude":
            lines.append(f'claude_config_dir = "{a.claude_config_dir}"')
        if a.priority != 100:
            lines.append(f'priority = {int(a.priority)}')
        if a.command:
            lines.append(f'command = "{a.command}"')
        if a.env:
            inner = ", ".join(f'{k} = "{v}"' for k, v in a.env.items())
            lines.append(f'env = {{ {inner} }}')
        # Both written only when set, so a profile that never used them is
        # byte-identical. `key_file` is a PATH; the key value never comes here.
        if a.key_file:
            lines.append(f'key_file = "{a.key_file}"')
        if a.models:
            inner = ", ".join(f'{k} = "{v}"' for k, v in a.models.items())
            lines.append(f'models = {{ {inner} }}')
    path = PROFILES / prof.name / "config.toml"
    _write(path, "\n".join(lines))
    return path


def set_default_profile(name: str) -> None:
    _write(GLOBAL_CONFIG, f'default_profile = "{name}"')
