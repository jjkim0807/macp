"""`macp` CLI — manage MaestroACP project + profile isolation (venv-style).

  python -m todo_system.macp init [--profile P] [--worker-mode M] [--capabilities a,b]
  python -m todo_system.macp status
  python -m todo_system.macp profile list
  python -m todo_system.macp profile add NAME [--vault DIR] [--budget N] [--model M]
  python -m todo_system.macp account add NAME [--profile P] [--agent V] [--config-dir DIR] [--command T]
  python -m todo_system.macp account env [NAME] [--profile P]   # eval "$(…)" → CLAUDE_CONFIG_DIR
  python -m todo_system.macp account use NAME [--profile P]     # set the profile's active_account
  python -m todo_system.macp account list [--profile P]        # one detailed line per account
  python -m todo_system.macp account status [--profile P]      # resolved active account + runtime state
  python -m todo_system.macp account remove NAME [--profile P] # drop from the profile (dir left on disk)
  python -m todo_system.macp use PROFILE            # set the global default profile

`account add` for a claude account without --config-dir creates
<accounts_root>/NAME with the shared layout (todo_system/macp/accounts.py):
auth files stay per account, everything else is a symlink into the shared root.

`account use` switches the profile's `active_account` — what the runner, claude_auth,
the usage guard and the transcript root follow (macp.config.current_account) — clears
the account's runtime unavailable mark and the runtime override (leaf 4), drops
the model-choice cache, re-points the legacy single config dir and its sibling
settings file at the new account dir ONLY when each is already a symlink (the
post-migration layout), and prints a Remote Control restart hint. It never restarts
anything and never deletes or moves a file.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import accounts as ACC
from . import config as C


def _cmd_init(a) -> int:
    root = Path.cwd()
    cfg = C.ProjectConfig(profile=a.profile, worker_mode=a.worker_mode,
                          capabilities=[c for c in (a.capabilities or "").split(",") if c],
                          account=a.account, provides=a.provides,
                          depends_on=[c for c in (a.depends_on or "").split(",") if c])
    if cfg.worker_mode not in C.WORKER_MODES:
        print(f"error: worker_mode must be one of {C.WORKER_MODES}")
        return 2
    if not C.load_profile(cfg.profile):
        print(f"warning: profile '{cfg.profile}' does not exist yet "
              f"(create it: macp profile add {cfg.profile})")
    path = C.write_project(root, cfg)
    from . import memory as MEM
    MEM.ensure_memory(root)
    actions = MEM.link_agent_context(root)   # wire memory into CLAUDE.md/AGENTS.md
    print(f"initialized MaestroACP project → {path}")
    print(f"  profile={cfg.profile} worker_mode={cfg.worker_mode} "
          f"capabilities={cfg.capabilities}")
    print(f"  memory: {MEM.memory_dir(root)}  (git-tracked → portable across envs)")
    if actions:
        print(f"  agent context linked: {', '.join(actions)}")
    ctx = MEM.discover_context(root)
    found = [k for k in ("claude_md", "agents_md", "claude_dir") if ctx.get(k)]
    if found:
        print(f"  detected: {', '.join(found)}")
    return 0


def _cmd_status(a) -> int:
    ctx = C.resolve()
    print(f"MACP_HOME: {C.HOME}")
    print(f"default profile: {C.default_profile()}")
    if ctx["project_root"] and ctx["project"]:
        p = ctx["project"]
        print(f"project: {ctx['project_root']}")
        print(f"  profile={p.profile} worker_mode={p.worker_mode} "
              f"capabilities={p.capabilities} account={p.account or '(round-robin)'}")
        from . import memory as MEM
        dc = MEM.discover_context(ctx["project_root"])
        print(f"  memory: {len(dc['memory_files'])} file(s) in {dc['memory_dir'] or '(none)'}")
        ctxf = [k.replace('_', '.') for k in ('claude_md', 'agents_md', 'claude_dir') if dc.get(k)]
        print(f"  agent context: {', '.join(ctxf) or '(none)'}")
    else:
        print("project: (cwd is not inside a .macp-managed project)")
    prof = ctx["profile"]
    if prof:
        print(f"resolved profile '{prof.name}': vault={prof.vault or '-'} "
              f"budget={prof.daily_run_budget}/day model={prof.model} "
              f"accounts={[x.name for x in prof.accounts] or '-'}")
    else:
        print(f"resolved profile '{ctx['profile_name']}': (not defined)")
    return 0


def _cmd_profile(a) -> int:
    if a.action == "list":
        for name in C.list_profiles():
            prof = C.load_profile(name)
            mark = " (default)" if name == C.default_profile() else ""
            print(f"{name}{mark}: budget={prof.daily_run_budget} model={prof.model} "
                  f"accounts={len(prof.accounts)} vault={prof.vault or '-'}")
        return 0
    if a.action == "add":
        if not a.name:
            print("usage: macp profile add NAME [--vault DIR] [--budget N] [--model M]")
            return 2
        prof = C.load_profile(a.name) or C.Profile(name=a.name)
        prof.name = a.name
        if a.vault:
            prof.vault = str(Path(a.vault).expanduser())
        if a.budget is not None:
            prof.daily_run_budget = a.budget
        if a.model:
            prof.model = a.model
        path = C.write_profile(prof)
        if not C.list_profiles() or C.default_profile() not in C.list_profiles():
            C.set_default_profile(a.name)
        print(f"profile '{a.name}' → {path}")
        return 0
    print("usage: macp profile [list|add NAME ...]")
    return 2


def _fmt_pct(v) -> str:
    """A raw utilization number → ``"NN%"``; ``"-"`` for anything non-numeric
    (``bool`` is not a number here — ``True`` must never read as 100%)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "-"
    return f"{float(v):.0f}%"


def _usage_pcts(name: str) -> tuple[str, str]:
    """``(session%, weekly%)`` for a claude account read from the usage-guard
    state file's raw ``accounts.<name>.usage.{five_hour,seven_day}.utilization``
    (the active account is mirrored there under its own name). ``("-", "-")``
    when the file, the account, or either window is absent. Read-only, never
    raises — the CLI must list accounts even with no usage snapshot yet."""
    try:
        from .. import usage_guard as UG
        ent = (UG._load_state().get("accounts") or {}).get(name) or {}
        usage = ent.get("usage") or {}
        sess = (usage.get("five_hour") or {}).get("utilization")
        week = (usage.get("seven_day") or {}).get("utilization")
    except Exception:
        return "-", "-"
    return _fmt_pct(sess), _fmt_pct(week)


def _slots_for(name: str) -> str:
    """``used/max`` parallel slots for a claude account from the runner's live
    snapshot (``usage_guard.read_runner_slots``; the runner is the source of
    truth for live slots), or ``-/-`` when no runner has reported it. Read-only,
    never raises — the CLI must list accounts even with no runner running."""
    try:
        from .. import usage_guard as UG
        per = (UG.read_runner_slots().get("per_account") or {}).get(name) or {}
        used, mx = per.get("used"), per.get("max")
        u = str(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else "-"
        m = str(mx) if isinstance(mx, (int, float)) and not isinstance(mx, bool) else "-"
        return f"{u}/{m}"
    except Exception:
        return "-/-"


def _account_line(x, active_name, state) -> str:
    """One ``account list`` line. A claude account carries the full detail —
    active mark, config dir, priority, whether ``.credentials.json`` /
    ``automation-token`` EXIST (existence only; their contents are never read or
    printed), availability/reason, the usage-guard session/weekly utilization,
    and the live parallel ``slots=used/max``. A non-claude account keeps the
    short ``name [agent]: command`` form. The config dir stays immediately after
    ``[agent]: `` so the terse form remains a prefix of the rich one, and
    ``slots`` is last so appending it never disturbs the earlier tokens."""
    mark = "* " if x.name == active_name else "  "
    if x.agent != "claude":
        return f"{mark}{x.name} [{x.agent}]: {x.command or '(no command)'}"
    cfg = x.claude_config_dir or ACC.LEGACY_CONFIG_DIR   # shown as configured; existence uses the resolved dir
    creds = "yes" if ACC.credentials_file_for(x).exists() else "no"
    token = "yes" if ACC.token_file_for(x).exists() else "no"
    status = ACC.unavailable_reason(x, state) or "available"
    sess, week = _usage_pcts(x.name)
    return (f"{mark}{x.name} [{x.agent}]: {cfg}  priority={getattr(x, 'priority', 100)} "
            f"creds={creds} token={token} status={status} session={sess} weekly={week} "
            f"slots={_slots_for(x.name)}")


def _cmd_account(a) -> int:
    if a.action == "env":
        return _cmd_account_env(a)
    if a.action == "use":
        return _cmd_account_use(a)
    if a.action == "status":
        return _cmd_account_status(a)
    if a.action == "remove":
        return _cmd_account_remove(a)
    prof = C.load_profile(a.profile)
    if not prof:
        print(f"error: profile '{a.profile}' not found (macp profile add {a.profile})")
        return 2
    if a.action == "list":
        if not prof.accounts:
            print("(no accounts)")
            return 0
        state = ACC.load_runtime_state()
        active, _src = ACC.resolve_active(prof, None, state)
        active_name = active.name if active is not None else None
        for x in prof.accounts:
            print(_account_line(x, active_name, state))
        return 0
    if not a.name:
        print("usage: macp account add NAME [--agent V] [--profile P] [--config-dir D] [--command T]")
        return 2
    if any(x.name == a.name for x in prof.accounts):
        print(f"account '{a.name}' already exists in profile '{a.profile}'")
        return 0
    login_hint = None
    seeded_from = None
    warnings: list[str] = []
    if a.agent == "claude" and a.config_dir is None:
        # Default claude account dir = <accounts_root>/NAME with the shared layout.
        try:
            acct_dir = ACC.create_account_layout(a.name)
        except (ValueError, OSError) as e:
            print(f"error: cannot create the account layout for '{a.name}': {e}")
            return 2
        # Seed .claude.json (non-auth keys only) from the profile's current
        # account — chosen BEFORE the new one is appended, so the first account
        # of a profile is never seeded (the legacy fallback belongs to migration).
        src = ACC.pick_account(prof.accounts, prof.active_account)
        if src is not None and ACC.seed_claude_json(
                ACC.claude_json_path(src.claude_config_dir or ACC.LEGACY_CONFIG_DIR),
                acct_dir / ".claude.json"):
            seeded_from = src.name
        warnings = ACC.layout_drift(acct_dir)
        config_dir = str(acct_dir)
        login_hint = f"CLAUDE_CONFIG_DIR={acct_dir} claude /login"
    else:
        # Explicit --config-dir (register only, as before) or a non-claude agent.
        config_dir = a.config_dir if a.config_dir is not None else ACC.LEGACY_CONFIG_DIR
    prof.accounts.append(C.Account(name=a.name, agent=a.agent,
                                   claude_config_dir=config_dir, command=a.command or ""))
    C.write_profile(prof)
    detail = config_dir if a.agent == "claude" else a.command
    print(f"added account '{a.name}' [{a.agent}] ({detail}) to profile '{a.profile}'")
    print(f"  profile '{a.profile}' now has {len(prof.accounts)} account(s) "
          f"= parallel capacity {len(prof.accounts)}x")
    for w in warnings:
        print(f"  warning: layout drift — {w}")
    if seeded_from:
        print(f"  seeded .claude.json (non-auth keys only) from account '{seeded_from}'")
    if login_hint:
        print(f"  next: {login_hint}")
    return 0


def _cmd_account_env(a) -> int:
    """stdout = exactly one line `export CLAUDE_CONFIG_DIR='<dir>'` for `eval`,
    or NOTHING (rc 0) when the account lives in the legacy single dir — the
    CLI must then keep its default config dir and Keychain login.
    Everything else (errors) goes to stderr so an eval never executes it."""
    prof = C.load_profile(a.profile)
    if not prof:
        print(f"error: profile '{a.profile}' not found (macp profile add {a.profile})",
              file=sys.stderr)
        return 2
    if a.name:
        acct = next((x for x in prof.accounts if x.name == a.name and x.agent == "claude"), None)
    else:
        acct = ACC.pick_account(prof.accounts, prof.active_account)
    if acct is None:
        what = f"claude account '{a.name}'" if a.name else "claude account"
        print(f"error: no {what} in profile '{a.profile}' to export", file=sys.stderr)
        return 2
    d = os.path.expanduser(acct.claude_config_dir or ACC.LEGACY_CONFIG_DIR)
    if ACC.is_legacy_config_dir(d):
        # Same rule as accounts.claude_env_for: the legacy single dir is left
        # UNSET. An explicit CLAUDE_CONFIG_DIR — even one spelling the default
        # path — makes the CLI key its credential store differently and skip
        # the macOS Keychain login. 2026-10-19 16:40 the remote-control wrapper
        # eval'd `export CLAUDE_CONFIG_DIR='/Users/jjkim/.claude'` from here and
        # the daemon died with "You must be logged in to use Remote Control"
        # every 30 s although `claude auth status` (no env) said logged in.
        return 0
    print("export CLAUDE_CONFIG_DIR='" + d.replace("'", "'\\''") + "'")
    return 0


def _cmd_account_status(a) -> int:
    """``account status`` — the resolved active claude account of the profile and
    the rule that chose it (``accounts.resolve_active`` source: pin / override /
    profile / priority / none), the runtime ``paused_since`` marker, the last
    auto-switch the runner recorded, and whether the active account's shared
    layout is intact (``accounts.layout_drift``). Pure read; rc 2 only for a
    missing profile."""
    prof = C.load_profile(a.profile)
    if not prof:
        print(f"error: profile '{a.profile}' not found (macp profile add {a.profile})")
        return 2
    state = ACC.load_runtime_state()
    active, source = ACC.resolve_active(prof, None, state)
    print(f"active account: {active.name if active is not None else '-'}")
    print(f"source: {source}")
    print(f"paused: {state.get('paused_since') or 'no'}")
    sw = state.get("last_switch")
    if isinstance(sw, dict):
        print(f"last switch: {sw.get('from')} → {sw.get('to')} "
              f"({sw.get('reason')}) at {sw.get('ts')}")
    else:
        print("last switch: -")
    if active is None:
        print("layout drift: n/a (no active claude account)")
    else:
        drift = ACC.layout_drift(ACC.config_dir_for(active))
        if drift:
            print("layout drift:")
            for d in drift:
                print(f"  {d}")
        else:
            print("layout drift: intact")
    return 0


def _cmd_account_remove(a) -> int:
    """``account remove NAME`` — drop NAME from the profile's ``[[account]]`` list
    ONLY. Refuses (rc 2, profile untouched) when NAME is the active account —
    the resolved one OR the profile's ``active_account`` pin — so automation
    never loses the identity it is running as; switch first with
    ``macp account use <other>``. Never deletes or moves the account's config dir
    (it may hold the only copy of a login): it prints the path left in place,
    with no delete command for the legacy shared dir."""
    if not a.name:
        print("usage: macp account remove NAME [--profile P]")
        return 2
    prof = C.load_profile(a.profile)
    if not prof:
        print(f"error: profile '{a.profile}' not found (macp profile add {a.profile})")
        return 2
    acct = next((x for x in prof.accounts if x.name == a.name), None)
    if acct is None:
        print(f"error: no account '{a.name}' in profile '{a.profile}' "
              f"(macp account list --profile {a.profile})")
        return 2
    active, _source = ACC.resolve_active(prof, None, ACC.load_runtime_state())
    active_name = active.name if active is not None else None
    if a.name == active_name or a.name == (prof.active_account or ""):
        print(f"error: '{a.name}' is the active account of profile '{a.profile}' — "
              f"switch first (macp account use <other>), then remove (profile unchanged)")
        return 2
    prof.accounts = [x for x in prof.accounts if x.name != a.name]
    C.write_profile(prof)
    print(f"removed account '{a.name}' [{acct.agent}] from profile '{a.profile}' "
          f"(profile only — nothing on disk is deleted)")
    if acct.agent == "claude":
        cfg = ACC.config_dir_for(acct)
        if ACC.is_legacy_config_dir(cfg):
            print(f"  config dir left in place: {cfg} (legacy shared dir — do NOT delete)")
        else:
            print(f"  config dir left in place (delete it yourself if you mean to): {cfg}")
    return 0


def _repoint_legacy_links(name: str, acct_dir: Path) -> None:
    """Re-point the legacy single config dir and its sibling settings file at
    ``acct_dir`` (and the ``.claude.json`` inside it) — each ONLY when it is
    already a symlink: the layout migration leaves both as symlinks, while a
    regular directory/file is the pre-migration layout and is left alone.
    ``unlink()`` removes the link itself, never its target; nothing is deleted or
    moved. Skipped for an account that lives on the legacy dir itself (a link to
    itself would be a loop) and for a missing account dir (a dangling link would
    break every interactive session)."""
    if ACC.is_legacy_config_dir(acct_dir):
        print(f"  {ACC.LEGACY_CONFIG_DIR}: account '{name}' lives there itself — nothing to re-point")
        return
    links = ((ACC.legacy_config_dir(), acct_dir, ACC.LEGACY_CONFIG_DIR, True),
             (ACC.legacy_claude_json(), ACC.claude_json_path(acct_dir), ACC.LEGACY_CLAUDE_JSON, False))
    for link, target, label, is_dir in links:
        if not link.is_symlink():
            print(f"  {label}: not a symlink — left as is (pre-migration layout)")
            continue
        if not acct_dir.is_dir():
            print(f"  warning: {label} left as is — account dir {acct_dir} does not exist "
                  f"(macp account add {name})")
            continue
        current = Path(os.path.normpath(os.path.join(link.parent, os.readlink(link))))
        if current == target:
            print(f"  {label}: already → {target}")
            continue
        try:
            link.unlink()
            link.symlink_to(target, target_is_directory=is_dir)
        except OSError as e:
            print(f"  warning: could not re-point {label} → {target}: {e}")
            continue
        print(f"  re-pointed {label} → {target}")


def _cmd_account_use(a) -> int:
    """Make NAME the profile's active claude account (`active_account`), which the
    runner, claude_auth, the usage guard and the transcript root follow from the
    next call on (macp.config.current_account). Then: clear the account's
    runtime unavailable mark, the runtime override and the PAUSED markers
    (`accounts.clear_unavailable`, leaf 4 — a human choosing an account
    overrides what the auto-switch recorded; printed only when something
    changed), drop the model-choice cache (it is account-agnostic — a model
    exhausted on the old account must not stay cached as dead for the new one),
    re-point the legacy symlinks (`_repoint_legacy_links`) and print a Remote
    Control restart hint. Never restarts anything: that kills every remote
    session, possibly the one issuing this command. rc 2 (profile untouched)
    for a missing name, an unknown or non-claude account, or a missing profile."""
    if not a.name:
        print("usage: macp account use NAME [--profile P]")
        return 2
    prof = C.load_profile(a.profile)
    if not prof:
        print(f"error: profile '{a.profile}' not found (macp profile add {a.profile})")
        return 2
    acct = next((x for x in prof.accounts if x.name == a.name and x.agent == "claude"), None)
    if acct is None:
        print(f"error: no claude account '{a.name}' in profile '{a.profile}' "
              f"(macp account list --profile {a.profile})")
        return 2
    prof.active_account = a.name
    C.write_profile(prof)
    acct_dir = ACC.config_dir_for(acct)
    print(f"active account of profile '{a.profile}' → '{a.name}' ({acct_dir})")
    try:
        if ACC.clear_unavailable(a.name, config_dir=acct_dir):
            print(f"  runtime state: cleared unavailable mark / override for '{a.name}'")
    except Exception as e:          # the state file is best-effort; never block the switch
        print(f"  runtime state: skipped ({type(e).__name__})")
    try:
        from ..orchestrator import model_select as MS
        dropped = MS.invalidate_cache()
        print("  model cache: invalidated" if dropped else "  model cache: nothing cached")
    except Exception as e:          # e.g. a clone without a vault marker: the import raises
        print(f"  model cache: skipped ({type(e).__name__})")
    _repoint_legacy_links(a.name, acct_dir)
    if sys.platform == "darwin":
        from .. import paths
        restart = f"launchctl kickstart -k gui/$UID/{paths.label_prefix()}claude-remote-control"
    else:
        restart = "systemctl --user restart claude-remote-control.service"
    print(f"  hint: Remote Control keeps the account it started with — restart it to switch: "
          f"{restart} (not done here: it kills every remote session, possibly this one)")
    return 0


def _cmd_memory(a) -> int:
    from . import memory as MEM
    root = C.find_project_root()
    if not root:
        print("error: not inside a .macp-managed project (run: macp init)")
        return 2
    if a.action == "list":
        files = MEM.discover_context(root)["memory_files"]
        print("\n".join(files) if files else "(no memory files)")
        return 0
    if a.action == "show":
        print(MEM.read_memory(root) or "(empty)")
        return 0
    if not a.name:
        print("usage: macp memory add NAME --content '...'")
        return 2
    print(f"wrote {MEM.write_memory(root, a.name, a.content)}")
    return 0


def _cmd_use(a) -> int:
    if not C.load_profile(a.profile):
        print(f"error: profile '{a.profile}' not found")
        return 2
    C.set_default_profile(a.profile)
    print(f"default profile set to '{a.profile}'")
    return 0


def _scan_roots(a):
    from pathlib import Path as _P
    return [_P(r).expanduser() for r in a.root] if a.root else None


def _cmd_deps(a) -> int:
    from . import deps as D
    nodes = D.scan(_scan_roots(a))
    if not nodes:
        print("no .macp-managed projects found (roots: "
              f"{[str(x) for x in (_scan_roots(a) or D.DEFAULT_ROOTS)]})")
        return 0
    down = D.downstream_edges(nodes)
    print(f"cross-project dependency graph ({len(nodes)} projects):")
    for name in sorted(nodes):
        n = nodes[name]
        prov = f"  provides={n.provides}" if n.provides else ""
        print(f"  {name}{prov}")
        if n.depends_on:
            print(f"     ← depends on: {', '.join(n.depends_on)}")
        consumers = sorted(down.get(name, set()))
        if consumers:
            print(f"     → consumed by: {', '.join(consumers)}")
    missing = {u for n in nodes.values() for u in n.depends_on if u not in nodes}
    if missing:
        print(f"  ⚠ referenced but not scanned: {', '.join(sorted(missing))}")
    return 0


def _cmd_impact(a) -> int:
    from . import deps as D
    nodes = D.scan(_scan_roots(a))
    changed = a.project
    if changed not in nodes:
        print(f"note: '{changed}' not among scanned projects "
              f"({', '.join(sorted(nodes)) or 'none'}) — showing declared consumers anyway")
    order = D.impacted(changed, nodes)
    if not order:
        print(f"'{changed}' 변경 → 영향받는 하류 프로젝트 없음")
        return 0
    print(f"'{changed}' 변경 → 영향 {len(order)}개 프로젝트 (전파순): {', '.join(order)}")
    print("제안 통합검증 태스크:")
    for spec in D.integration_task_specs(changed, nodes, reason=a.reason):
        print(f"  - [{spec['project']}] {spec['title']}  (slug: {spec['slug']}, "
              f"worker_mode={spec['worker_mode']})")
    return 0


def _cmd_link(a) -> int:
    root = C.find_project_root()
    if not root:
        print("error: not inside a .macp-managed project (run: macp init)")
        return 2
    cfg = C.load_project(root)
    added = [u for u in a.upstream if u not in cfg.depends_on]
    cfg.depends_on = sorted(set(cfg.depends_on) | set(a.upstream))
    C.write_project(root, cfg)
    print(f"{Path(root).name}: depends_on = {cfg.depends_on}"
          + (f"  (+{', '.join(added)})" if added else "  (no change)"))
    return 0


def _cmd_propagate(a) -> int:
    from . import deps as D
    nodes = D.scan(_scan_roots(a))
    specs = D.integration_task_specs(a.project, nodes, reason=a.reason)
    if not specs:
        print(f"'{a.project}' 하류 소비 프로젝트 없음 — 생성할 통합검증 태스크 없음")
        return 0
    if not a.create:
        print(f"'{a.project}' 변경 → 하류 통합검증 태스크 {len(specs)}개 (dry-run):")
        for s in specs:
            print(f"  - [{s['project']}] {s['slug']}: {s['title']}")
        print("실제 생성: --create")
        return 0
    for s in specs:
        path = D.write_integration_task(s)
        print(f"  created {path.relative_to(D.VAULT_ROOT)}")
    print(f"{len(specs)}개 생성 — 반영: uv run python -m todo_system.cli import-md")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="macp",
                                description="MaestroACP project/profile isolation")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="mark cwd as a MaestroACP-managed project (.macp/)")
    pi.add_argument("--profile", default="default")
    pi.add_argument("--worker-mode", default="auto")
    pi.add_argument("--capabilities", default="")
    pi.add_argument("--account", default=None)
    pi.add_argument("--provides", default="", help="interface/lib this project exposes")
    pi.add_argument("--depends-on", default="", help="upstream project names (comma-sep)")
    pi.set_defaults(fn=_cmd_init)

    sub.add_parser("status", help="show resolved project + profile").set_defaults(fn=_cmd_status)

    pp = sub.add_parser("profile", help="manage profiles")
    pp.add_argument("action", choices=["list", "add"])
    pp.add_argument("name", nargs="?")
    pp.add_argument("--vault")
    pp.add_argument("--budget", type=int)
    pp.add_argument("--model")
    pp.set_defaults(fn=_cmd_profile)

    pa = sub.add_parser("account", help="manage a profile's agent accounts (multi-vendor, parallel)")
    pa.add_argument("action", choices=["add", "list", "env", "use", "status", "remove"])
    pa.add_argument("name", nargs="?")
    pa.add_argument("--profile", default="default")
    pa.add_argument("--agent", default="claude",
                    help="vendor: claude | codex | cursor | gemini | aider | custom")
    pa.add_argument("--config-dir", default=None,
                    help="CLAUDE_CONFIG_DIR for a claude account (default: "
                         "<accounts_root>/NAME created with the shared layout)")
    pa.add_argument("--command", default="", help="invocation template for non-claude ('{prompt}')")
    pa.set_defaults(fn=_cmd_account)

    pm = sub.add_parser("memory", help="manage this project's .macp/memory/")
    pm.add_argument("action", choices=["add", "list", "show"])
    pm.add_argument("name", nargs="?")
    pm.add_argument("--content", default="")
    pm.set_defaults(fn=_cmd_memory)

    pu = sub.add_parser("use", help="set the global default profile")
    pu.add_argument("profile")
    pu.set_defaults(fn=_cmd_use)

    pdp = sub.add_parser("deps", help="show the cross-project dependency graph")
    pdp.add_argument("--root", action="append", help="scan root (repeatable)")
    pdp.set_defaults(fn=_cmd_deps)

    pim = sub.add_parser("impact", help="what breaks downstream if a project changes")
    pim.add_argument("project")
    pim.add_argument("--root", action="append")
    pim.add_argument("--reason", default="")
    pim.set_defaults(fn=_cmd_impact)

    pln = sub.add_parser("link", help="declare this project depends on upstream(s)")
    pln.add_argument("upstream", nargs="+")
    pln.set_defaults(fn=_cmd_link)

    ppr = sub.add_parser("propagate",
                         help="create downstream integration-check tasks for an upstream change")
    ppr.add_argument("project")
    ppr.add_argument("--create", action="store_true", help="write the Todo tasks (default: dry-run)")
    ppr.add_argument("--root", action="append")
    ppr.add_argument("--reason", default="")
    ppr.set_defaults(fn=_cmd_propagate)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
