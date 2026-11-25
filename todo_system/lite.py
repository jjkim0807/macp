"""Lite (one-person) mode: ``macp init | add | up | status | dashboard | memory``.

Design: ``docs/design/lite-mode.md``. Three commands take a newcomer from an
existing git repository to a branch a real Claude Code worker committed to::

    macp init                     # vault under ~/.macp, register this repository
    macp add "<one sentence>"     # sentence -> validated task file
    macp up --once                # dispatch -> worker -> collect, then exit

Everything here is glue around the engine calls the HQ setup already runs —
``orchestrator._main`` (``auto`` / ``collect`` / ``status``), ``runner._main``
and ``Runner.poll_once``, ``task_validate.validate_file``, the ``cli`` markdown
import, ``gen_units.service``. No new pipeline, environment variable or
configuration key:

* The vault is resolved ONCE, before any engine module is imported, and put into
  ``MACP_VAULT_ROOT`` for this process and the stages it spawns: several engine
  modules freeze the vault into a constant at import time. A value already in the
  environment always wins (:func:`resolve_vault`).
* ``macp up --once`` sets ``USAGE_GUARD_DISABLE=1`` the same way, only when the
  caller has not set it: a run a person just started and watches needs no pacing.
  The loop and the service keep the guard on.

``todo_system._entry`` routes the six verbs here before ``todo_system.cli`` is
imported, so every other ``macp`` subcommand behaves exactly as before.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from . import i18n, paths
from ._entry import default_vault

VERBS = ("init", "add", "up", "status", "dashboard", "memory")

WORKER_AGENT = "worker-default"           # vault-template/Worker/agents/registry.yaml
ADD_TIMEOUT_S = 180                       # one `claude -p` call that writes a task
ADD_ATTEMPTS = 2                          # a broken JSON answer is asked again once
POLL_INTERVAL_S = 15.0                    # runner's default --interval
TICK_INTERVAL_S = 300.0                   # HQ orchestrator timer (gen_units)
HEARTBEAT_INTERVAL_S = 300.0              # Runner.run_forever's heartbeat_interval
UP_LEASE_TTL_S = 24 * 3600.0              # not renewed mid-run; a dead holder is
                                          # reclaimed at once on the same host
SERVICE_UNIT = "macp-up"
FALLBACK_GIT_NAME = "macp"
FALLBACK_GIT_EMAIL = "macp@localhost"
LISTING_LIMIT = 40
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
ADD_PROMPT = PROMPTS_DIR / "lite-add.md"
TASK_TEMPLATE = PROMPTS_DIR / "lite-task-template.md"
WATCHED_REPO_FILES = (".macp/config.toml", ".macp/memory/index.md", "CLAUDE.md", "AGENTS.md")

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TAG_RE = SLUG_RE
DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[mhd]$")
RUN_DIR_RE = r"^\d{8}T\d{6}-%s$"
PR_URL_RE = re.compile(r"https?://\S*?/pull/\d+")
PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# Host-local runtime state a one-host vault never shares. HQ vaults sync these
# between hosts through git, so this list lives here, written into a NEW lite
# vault only — never into vault-template.
VAULT_GITIGNORE = """\
# Written by `macp init`: runtime state of this one-host vault (not synced).
Todo/todo.db
Todo/todo.db-*
Todo/NOW.md
Todo/FOCUS.md
Todo/Dashboard.md
Todo/Orchestrator.md
Todo/items/*.md.part
Worker/_state/
Worker/agents/*/
Worker/assignments/
Worker/runs/
Worker/jobs/
Worker/*.lock
Worker/*.json
Memory/relations.sqlite*
logs/
"""


class LiteError(Exception):
    """A failure the user can act on. Its text is already one translated line."""


def _t(key: str, **fmt) -> str:
    return i18n.t(key, **fmt)


def _say(msg: str) -> None:
    print(msg, flush=True)


def _quiet(*_a, **_k) -> None:
    """`notify` for model_select: lite mode pages no one."""


# --- vault ----------------------------------------------------------------------


def is_vault(path: Path) -> bool:
    return (path / "Todo" / "items").is_dir()


def resolve_vault(start: Path | None = None) -> Path:
    """The vault for this command (design D4), in order:

    1. ``MACP_VAULT_ROOT`` when set — an existing vault, or the HQ.
    2. What ``paths.vault_root()``'s upward marker search finds from ``start``
       (default: the cwd), accepted only when it has ``Todo/items/``. A project
       marker without a ``vault_root`` key resolves to the project itself, and a
       global ``~/.macp/config.toml`` to ``$HOME`` — neither is a vault.
    3. :func:`todo_system._entry.default_vault` (``~/.macp/vault``).
    """
    env = os.environ.get("MACP_VAULT_ROOT")
    if env:
        return Path(env).expanduser()
    try:
        found = paths.vault_root(start=start)
    except paths.VaultRootError:
        found = None
    if found is not None and is_vault(found):
        return found
    return default_vault()


def bind_vault(vault: Path) -> Path:
    """Pin the vault for this process before any engine module is imported."""
    os.environ["MACP_VAULT_ROOT"] = str(vault)
    return vault


def _require_vault() -> Path:
    vault = resolve_vault()
    if not is_vault(vault):
        raise LiteError(_t("lite.common.no_vault", path=vault))
    return bind_vault(vault)


# --- git --------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(e))


def repo_root(cwd: Path | None = None) -> Path | None:
    r = _git(cwd or Path.cwd(), "rev-parse", "--show-toplevel")
    top = (r.stdout or "").strip() if r.returncode == 0 else ""
    return Path(top) if top else None


def _ensure_identity(repo: Path) -> None:
    """A clean HOME has no git identity and `git commit` fails. Fill the repo's
    LOCAL config only, and only what the effective config lacks."""
    for key, value in (("user.name", FALLBACK_GIT_NAME), ("user.email", FALLBACK_GIT_EMAIL)):
        if not (_git(repo, "config", "--get", key).stdout or "").strip():
            _git(repo, "config", "--local", key, value)


def _commit(repo: Path, pathspecs: list[str], message: str) -> bool:
    """Commit only ``pathspecs`` in ``repo``. False when there was nothing to commit
    (or ``repo`` is not a git repository)."""
    if not (repo / ".git").exists():
        return False
    _ensure_identity(repo)
    _git(repo, "add", "-A", "--", *pathspecs)
    if _git(repo, "diff", "--cached", "--quiet", "--", *pathspecs).returncode == 0:
        return False
    return _git(repo, "commit", "-q", "-m", message, "--", *pathspecs).returncode == 0


def _set_top_level_key(path: Path, key: str, value: str) -> None:
    """Set ``key = "value"`` at the top level of a small TOML file: replace the
    key's line when it is there, else insert it before the first ``[table]``
    (else append). Every other line, comment and key is kept as it was."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    new = f'{key} = "{value}"'
    key_re = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    table_at = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), None)
    top = lines if table_at is None else lines[:table_at]
    for i, ln in enumerate(top):
        if key_re.match(ln):
            lines[i] = new
            break
    else:
        if table_at is None:
            lines.append(new)
        else:
            lines.insert(table_at, new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _home_ref(path: Path) -> str:
    """``~/…`` for a path under HOME (a committed file then names no user), else
    the absolute path. ``paths`` expands ``~`` when it reads the key back."""
    try:
        rel = path.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return str(path)
    return "~" if rel == Path(".") else "~/" + rel.as_posix()


# --- macp init --------------------------------------------------------------------


def template_dir() -> Path:
    """The packaged ``vault-template/`` (wheel: ``_engine/vault-template``)."""
    return paths.engine_root() / "vault-template"


def ensure_vault(vault: Path) -> bool:
    """Create the vault from the template when it is missing. True when created.

    A git repository on ``main`` with one commit, no remote; its config gets the
    existing ``worker_root`` key so clones land under ``~/.macp/work`` rather than
    ``~/Worker``, and its ``.gitignore`` keeps host-local runtime state out.
    """
    if is_vault(vault):
        return False
    if vault.exists() and any(vault.iterdir()):
        raise LiteError(_t("lite.init.not_a_vault", path=vault))
    src = template_dir()
    if not (src / "Todo").is_dir():
        raise LiteError(_t("lite.init.no_template", path=src))
    shutil.copytree(src, vault, dirs_exist_ok=True)
    home = Path(os.environ.get("MACP_HOME") or (Path.home() / ".macp"))
    _set_top_level_key(vault / ".macp" / "config.toml", "worker_root", _home_ref(home / "work"))
    (vault / ".gitignore").write_text(VAULT_GITIGNORE, encoding="utf-8")
    if _git(vault, "init", "-q", "-b", "main").returncode != 0:
        _git(vault, "init", "-q")
        _git(vault, "symbolic-ref", "HEAD", "refs/heads/main")
    _ensure_identity(vault)
    _git(vault, "add", "-A")
    if _git(vault, "commit", "-q", "-m", "macp: new vault").returncode != 0:
        raise LiteError(_t("lite.init.git_failed", path=vault))
    return True


def register_repo(repo: Path, vault: Path) -> list[str]:
    """Mark ``repo`` as managed and point it at ``vault`` — the same three calls
    the project CLI's ``init`` makes, minus its missing-profile warning (lite mode
    makes no profile on purpose). Returns the repository files that changed."""
    from .macp import config as C
    from .macp import memory as MEM

    def snapshot() -> dict[str, str | None]:
        out = {}
        for rel in WATCHED_REPO_FILES:
            p = repo / rel
            out[rel] = p.read_text(encoding="utf-8") if p.is_file() else None
        return out

    before = snapshot()
    ref = _home_ref(vault)
    marker = repo / C.PROJECT_DIR / "config.toml"
    if not marker.is_file():
        C.write_project(repo, C.ProjectConfig(vault_root=ref))
    elif (C.load_project(repo) or C.ProjectConfig()).vault_root != ref:
        # Keep every other key of a marker someone already has (write_project
        # would rewrite it to its fixed schema).
        _set_top_level_key(marker, "vault_root", ref)
    MEM.ensure_memory(repo)
    MEM.link_agent_context(repo)
    after = snapshot()
    return [rel for rel in WATCHED_REPO_FILES if after[rel] != before[rel]]


def login_check() -> None:
    """Say whether Claude Code answers. Never blocks: without a login `macp add`
    fails a minute later with the same advice. One real 1-turn call, cached 24h by
    model_select, which `add` and `up` then reuse."""
    if shutil.which("claude") is None:
        _say(_t("lite.common.no_claude"))
        return
    from . import claude_auth
    from .orchestrator import model_select
    model = model_select.resolve_model(tier="light", notify=_quiet)
    ladder = model_select.LADDERS["light"]
    if os.environ.get("WORKER_MODEL") or model != ladder[-1]:
        _say(_t("lite.init.claude_ok", model=model,
                source=claude_auth.auth_source(claude_auth.claude_env())))
    else:
        _say(_t("lite.init.claude_unknown"))


def cmd_init(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="macp init", description=_t("lite.help.init")).parse_args(argv)
    vault = bind_vault(resolve_vault())
    created = ensure_vault(vault)
    _say(_t("lite.init.vault_created" if created else "lite.init.vault_exists", path=vault))
    repo = repo_root()
    if repo is not None and repo.resolve() == vault.resolve():
        repo = None                                  # the vault is not a project
    if repo is None:
        _say(_t("lite.init.no_repo"))
    else:
        changed = register_repo(repo, vault)
        if changed:
            _say(_t("lite.init.registered", repo=repo))
            _say(_t("lite.init.changed", files=", ".join(changed)))
            _say(_t("lite.init.commit_hint"))
        else:
            _say(_t("lite.init.already_registered", repo=repo))
    login_check()
    if repo is not None:
        _say(_t("lite.init.next"))
    return 0


# --- macp add ---------------------------------------------------------------------


def repo_spec(repo: Path) -> str:
    """The task's ``repo:`` value: ``origin`` when it is a URL, else the
    repository's absolute path (the runner clones either)."""
    from .orchestrator.stages import is_url_remote
    r = _git(repo, "remote", "get-url", "origin")
    url = (r.stdout or "").strip() if r.returncode == 0 else ""
    return url if is_url_remote(url) else str(repo.resolve())


def _registered(repo: Path) -> bool:
    from .macp import config as C
    cfg = C.load_project(repo)
    return bool(cfg and cfg.vault_root)


def _check_clone_slot(repo_value: str) -> None:
    """Two repositories with the same last path component share one clone
    directory, and an existing clone is reused without looking at its origin."""
    from .orchestrator import repos
    dest = repos.local_path(repo_value)
    if not (dest / ".git").exists():
        return
    other = (_git(dest, "remote", "get-url", "origin").stdout or "").strip()
    if other and other.rstrip("/") != repo_value.rstrip("/"):
        raise LiteError(_t("lite.add.clone_conflict", path=dest, other=other))


def _note_unpublished(repo: Path, repo_value: str) -> None:
    from .orchestrator.stages import is_url_remote
    if (_git(repo, "status", "--porcelain").stdout or "").strip():
        _say(_t("lite.add.dirty", repo=repo))
    if is_url_remote(repo_value):
        r = _git(repo, "rev-list", "--count", "@{u}..HEAD")
        ahead = (r.stdout or "").strip() if r.returncode == 0 else ""
        if ahead.isdigit() and int(ahead) > 0:
            _say(_t("lite.add.ahead", n=ahead))


def _top_level_entries(repo: Path) -> list[str]:
    r = _git(repo, "ls-files")
    seen: list[str] = []
    for line in (r.stdout or "").splitlines():
        head = line.split("/", 1)[0] + ("/" if "/" in line else "")
        if head and head not in seen:
            seen.append(head)
    return sorted(seen)[:LISTING_LIMIT]


def _context_files(repo: Path) -> list[str]:
    return [n for n in ("CLAUDE.md", "AGENTS.md") if (repo / n).is_file()]


def _fill(template: str, values: dict[str, str]) -> str:
    """One pass over ``{{name}}`` placeholders, so an inserted value is never
    scanned again."""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def build_add_prompt(sentence: str, repo: Path) -> str:
    from .orchestrator.stages import strip_frontmatter
    listing = _top_level_entries(repo)
    context = "\n".join([
        f"- name: {repo.name}",
        f"- top-level tracked entries: {', '.join(listing) or '(none yet)'}",
        f"- agent instruction files: {', '.join(_context_files(repo)) or 'none'}",
    ])
    body = strip_frontmatter(ADD_PROMPT.read_text(encoding="utf-8"))
    return _fill(body, {"repo_context": context, "request": sentence})


def extract_json(text: str) -> dict | None:
    """The JSON object in a model answer (a Markdown fence is tolerated)."""
    s = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.S)
    if fenced:
        s = fenced.group(1)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def ask_claude(prompt: str, *, timeout: float = ADD_TIMEOUT_S) -> dict:
    """One ``claude -p`` call shaped like model_select's probe (empty temp cwd,
    1 turn, the light tier's model and effort, the stages' credentials)."""
    if shutil.which("claude") is None:
        raise LiteError(_t("lite.common.no_claude"))
    from . import claude_auth
    from .macp.accounts import AUTH_MARKERS
    from .orchestrator import model_select, stages
    model = model_select.resolve_model(tier=stages.effective_tier("quick", "light"),
                                       notify=_quiet)
    effort = stages.stage_effort("quick", "light")
    argv = ["claude", "-p", prompt, "--model", model, "--effort", effort, "--max-turns", "1"]
    for _ in range(ADD_ATTEMPTS):
        with tempfile.TemporaryDirectory(prefix="macp-add-") as cwd:
            try:
                r = subprocess.run(argv, cwd=cwd, env=claude_auth.claude_env(),
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout)
            except FileNotFoundError:
                raise LiteError(_t("lite.common.no_claude")) from None
            except subprocess.TimeoutExpired:
                raise LiteError(_t("lite.add.timeout", seconds=int(timeout))) from None
        out = f"{r.stdout or ''}\n{r.stderr or ''}"
        obj = extract_json(r.stdout)
        if obj is not None and (r.returncode == 0 or "reached max turns" in out.lower()):
            return obj
        # No task came back: say why. Markers are read only now, so a request that
        # itself mentions "usage limit" is never mistaken for one.
        if any(mark in out.lower() for mark in AUTH_MARKERS):
            raise LiteError(_t("lite.add.not_logged_in"))
        if stages.is_capacity_limit(out):
            raise LiteError(_t("lite.add.limit"))
        if r.returncode != 0:
            detail = next((ln.strip() for ln in out.splitlines() if ln.strip()), "-")
            raise LiteError(_t("lite.add.failed", rc=r.returncode, detail=detail[:200]))
    raise LiteError(_t("lite.add.not_json"))


def check_task(obj: dict) -> dict:
    """The model's fields, typed and bounded, or a LiteError naming the field."""
    def fail(name: str, problem: str, **fmt):
        raise LiteError(_t("lite.add.schema", field=name,
                           problem=_t(f"lite.schema.{problem}", **fmt)))

    def line(name: str, max_len: int = 400) -> str:
        v = obj.get(name)
        if not isinstance(v, str) or not v.strip():
            fail(name, "missing")
        v = v.strip()
        if "\n" in v or "\r" in v:
            fail(name, "multiline")
        if len(v) > max_len:
            fail(name, "too_long", max=max_len)
        return v

    def items(name: str, lo: int, hi: int, pattern=None, optional: bool = False) -> list[str]:
        v = obj.get(name)
        if v is None and optional:
            return []
        if not isinstance(v, list):
            fail(name, "not_list")
        out: list[str] = []
        for x in v:
            if not isinstance(x, str) or not x.strip():
                fail(name, "empty_item")
            x = x.strip()
            if "\n" in x or "\r" in x:
                fail(name, "multiline")
            if pattern is not None and not pattern.match(x):
                fail(name, "bad_value", value=x[:40])
            out.append(x)
        if not lo <= len(out) <= hi:
            fail(name, "count", lo=lo, hi=hi)
        return out

    slug = line("slug", 50)
    if len(slug) < 3 or not SLUG_RE.match(slug):
        fail("slug", "bad_value", value=slug[:40])
    mode = obj.get("worker_mode")
    if mode not in ("quick", "auto"):
        fail("worker_mode", "bad_value", value=str(mode)[:40])
    estimates = {}
    for name in ("estimate_p50", "estimate_p90"):
        estimates[name] = line(name, 10)
        if not DURATION_RE.match(estimates[name]):
            fail(name, "bad_value", value=estimates[name])
    return {
        "slug": slug,
        "title": line("title", 120),
        "outcome": line("outcome"),
        "next_action": line("next_action"),
        "if_then": line("if_then"),
        "acceptance_criteria": items("acceptance_criteria", 1, 8),
        "worker_mode": mode,
        **estimates,
        "touches": items("touches", 1, 20),
        "tags": [x for x in items("tags", 0, 5, TAG_RE, optional=True) if x != "todo"],
    }


def unique_slug(slug: str, items_dir: Path) -> str:
    cand, n = slug, 2
    while (items_dir / f"{cand}.md").exists() or (items_dir / f"{cand}.md.part").exists():
        cand, n = f"{slug}-{n}", n + 1
    return cand


def _scalar(v: str) -> str:
    """A value for a double-quoted frontmatter string. The worker's line parser
    strips the quotes but never unescapes, so no escapes are written: ``"``
    becomes ``'`` and ``\\`` becomes ``/``; control characters go."""
    v = v.replace("\\", "/").replace('"', "'")
    return "".join(ch for ch in v if ch == "\t" or ch >= " ")


def render_task(fields: dict, *, slug: str, repo_value: str, request: str,
                references: list[str], today: str) -> str:
    def block(values: list[str]) -> str:
        return "\n".join(f'  - "{_scalar(x)}"' for x in values)

    refs = (f"- Read first: {', '.join(references)} (repository root).\n"
            if references else "")
    template = TASK_TEMPLATE.read_text(encoding="utf-8")
    return _fill(template, {
        "tags": "\n".join(f"  - {x}" for x in ["todo", *fields["tags"]]),
        "today": today,
        "repo": _scalar(repo_value),
        "worker_mode": fields["worker_mode"],
        "outcome": _scalar(fields["outcome"]),
        "next_action": _scalar(fields["next_action"]),
        "if_then": _scalar(fields["if_then"]),
        "estimate_p50": fields["estimate_p50"],
        "estimate_p90": fields["estimate_p90"],
        "acceptance_criteria": block(fields["acceptance_criteria"]),
        "touches": block(fields["touches"]),
        "title": " ".join(fields["title"].split()),
        "request": " ".join(request.split()),
        "references": refs,
    })


def import_tasks() -> int:
    """`cli import-md`'s three calls: md -> todo.db."""
    from . import cli
    con = cli.db_connect(cli.DB_PATH)
    try:
        cli.init_db(con)
        return cli.import_markdown_to_db(con)
    finally:
        con.close()


def cmd_add(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        _say(_t("lite.add.usage"))
        return 0 if argv else 2
    sentence = " ".join(argv).strip()
    if not sentence:
        _say(_t("lite.add.usage"))
        return 2
    vault = _require_vault()
    repo = repo_root()
    if repo is None or not _registered(repo):
        raise LiteError(_t("lite.add.not_registered"))
    repo_value = repo_spec(repo)
    _check_clone_slot(repo_value)
    _note_unpublished(repo, repo_value)

    _say(_t("lite.add.asking"))
    started = time.monotonic()
    fields = check_task(ask_claude(build_add_prompt(sentence, repo)))
    took = time.monotonic() - started

    items_dir = vault / "Todo" / "items"
    slug = unique_slug(fields["slug"], items_dir)
    text = render_task(fields, slug=slug, repo_value=repo_value, request=sentence,
                       references=_context_files(repo), today=dt.date.today().isoformat())
    path = items_dir / f"{slug}.md"
    part = items_dir / f"{slug}.md.part"
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)                 # never a half-written task on disk

    from . import task_validate
    issues = task_validate.validate_file(path)
    errors = [i for i in issues if i.level == task_validate.ERROR]
    if errors:
        path.unlink()
        detail = "; ".join(f"{i.field}: {i.message}" for i in errors)
        raise LiteError(_t("lite.add.invalid", detail=detail[:300]))
    import_tasks()
    _commit(vault, [f"Todo/items/{slug}.md"], f"macp add: {slug}")

    _say(_t("lite.add.created", slug=slug, mode=fields["worker_mode"],
            outcome=fields["outcome"]))
    _say(_t("lite.add.file", path=path))
    warnings = len(issues) - len(errors)
    if warnings:
        _say(_t("lite.add.warnings", n=warnings, slug=slug))
    _say(_t("lite.add.took", seconds=f"{took:.1f}"))
    _say(_t("lite.add.next"))
    return 0


# --- macp up ----------------------------------------------------------------------


class Engine:
    """The HQ processes' calls, made from one process with the HQ tick's
    arguments (scripts/orchestrator_tick.sh) and the runner unit's (gen_units)."""

    def __init__(self, vault: Path):
        self.vault = vault
        self.uri = f"file://{vault}/Worker/agents"
        self.db = vault / "Todo" / "todo.db"
        self.lock = vault / "Worker" / "orchestrator.lock"

    def tick(self) -> int:
        from .orchestrator import orchestrator
        return orchestrator._main(["--uri", self.uri, "--lease", str(self.lock),
                                   "auto", "--db", str(self.db)])

    def collect(self) -> int:
        from .orchestrator import orchestrator
        return orchestrator._main(["--uri", self.uri, "collect"])

    def run_worker_once(self) -> int:
        from .orchestrator import runner
        return runner._main(["--agent", WORKER_AGENT, "--mode", "live-staged",
                             "--uri", self.uri, "--once"])

    def make_runner(self):
        from .orchestrator import runner
        from .orchestrator.mailbox import open_mailbox
        agent = runner.resolve_agent(SimpleNamespace(agent=WORKER_AGENT, registry=None),
                                     self.vault)
        return runner.Runner(open_mailbox(self.uri, base=self.vault), agent,
                             runner.make_executor("live-staged"))

    def sync_tasks(self) -> int:
        return import_tasks()

    def ledger(self) -> list:
        from .orchestrator.assignment import Ledger
        from .orchestrator.orchestrator import DEFAULT_LEDGER
        return Ledger(self.vault / DEFAULT_LEDGER).all()

    def task_repo(self, slug: str) -> str | None:
        from .orchestrator import repos
        md = self.vault / "Todo" / "items" / f"{slug}.md"
        return repos.read_task_repo(md)[0] if md.is_file() else None

    def publish_local_branches(self) -> None:
        """A path-remote task waiting for review whose branch never reached the
        user's repository (the quick stage pushes it itself; the runner does not):
        fast-forward it there from the worker's shared clone."""
        from .orchestrator import repos, stages
        from .orchestrator.assignment import AWAITING_HUMAN
        for a in self.ledger():
            repo = self.task_repo(a.task_id) if a.state == AWAITING_HUMAN else None
            if not repo or stages.is_url_remote(repo) or not (Path(repo) / ".git").exists():
                continue
            branch = f"worker/{a.task_id}"
            if _git(Path(repo), "rev-parse", "-q", "--verify", f"refs/heads/{branch}").returncode == 0:
                continue
            clone = repos.local_path(repo)
            if _git(clone, "rev-parse", "-q", "--verify", f"refs/heads/{branch}").returncode != 0:
                continue
            if stages.checkpoint_push(clone, branch).get("pushed"):
                _say(_t("lite.up.pushed", branch=branch, repo=repo))

    def commit_tasks(self) -> None:
        """The orchestrator closes task files; HQ's maintenance commits (and
        pushes) them. A one-host vault has no remote, so commit the task
        directory only."""
        if (_git(self.vault, "status", "--porcelain", "--", "Todo/items").stdout or "").strip():
            _commit(self.vault, ["Todo/items"], "macp up: task state")


def up_once(vault: Path) -> int:
    eng = Engine(vault)
    eng.sync_tasks()
    _say(_t("lite.up.dispatch"))
    rc = eng.tick()
    if rc != 0:
        return rc
    _say(_t("lite.up.worker"))
    rc = eng.run_worker_once()
    # Collect BEFORE the next tick: the tick reaps in-progress rows with no news
    # first, and in one thread no tick ran while the stages did, so a long run's
    # row would be reaped as dead before its RESULT was read.
    _say(_t("lite.up.collect"))
    eng.collect()
    eng.tick()
    eng.publish_local_branches()
    eng.commit_tasks()
    _say("")
    print_status(vault)
    return rc


def up_loop(vault: Path, *, sleep=time.sleep, clock=time.monotonic,
            max_cycles: int | None = None) -> int:
    """`macp up`: the runner daemon's serial loop with the tick folded in. The
    tick runs when a task file changed, after the runner handled something
    (collect first, as in :func:`up_once`), and every TICK_INTERVAL_S."""
    from . import cli
    from .orchestrator import runner as runner_mod
    eng = Engine(vault)
    runner = eng.make_runner()
    _say(_t("lite.up.loop", poll=int(POLL_INTERVAL_S), tick=int(TICK_INTERVAL_S)))
    last_snap: dict | None = None
    last_tick: float | None = None
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            try:
                snap = cli.snapshot_mtime()
                due = last_tick is None or clock() - last_tick >= TICK_INTERVAL_S
                if snap != last_snap:
                    eng.sync_tasks()
                    last_snap, due = snap, True
                if due:
                    eng.tick()
                    last_tick = clock()
                handled = runner.poll_once()
                runner._announce(1, interval=HEARTBEAT_INTERVAL_S,
                                 min_gap=runner_mod.ANNOUNCE_MIN_GAP_S)
                if handled:
                    eng.collect()
                    eng.tick()
                    last_tick = clock()
                    eng.publish_local_branches()
                    eng.commit_tasks()
            except Exception as e:          # noqa: BLE001 — one bad cycle must not end the loop
                _say(_t("lite.up.cycle_error", kind=type(e).__name__, detail=str(e)[:200]))
            sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        _say(_t("lite.up.stopped"))
    return 0


def _load_gen_units():
    """scripts/systemd/gen_units.py is a script beside the package, not a module;
    it reads the vault from the environment at import (already bound here)."""
    path = paths.engine_root() / "scripts" / "systemd" / "gen_units.py"
    spec = importlib.util.spec_from_file_location("macp_gen_units", path)
    if spec is None or spec.loader is None:
        raise LiteError(_t("lite.service.no_renderer", path=path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def install_service(vault: Path, *, run=subprocess.run) -> int:
    """One systemd --user unit running `macp up` (design D12). The HQ unit set
    (`gen_units.build()`) is not touched."""
    if not sys.platform.startswith("linux") or shutil.which("systemctl") is None:
        raise LiteError(_t("lite.service.unsupported"))
    gen = _load_gen_units()
    gen.write_env()
    text = gen.service(SERVICE_UNIT, "MaestroACP lite loop (macp up)",
                       f"{sys.executable} -P -m todo_system._entry up",
                       cwd=vault, kind="simple", restart="always",
                       extra_lines=[f"Environment=MACP_VAULT_ROOT={vault}"])
    (vault / "logs").mkdir(exist_ok=True)
    gen.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit = gen.UNIT_DIR / f"{SERVICE_UNIT}.service"
    unit.write_text(text, encoding="utf-8")
    _say(_t("lite.service.wrote", path=unit))
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", unit.name]):
        r = run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            raise LiteError(_t("lite.service.systemctl_failed", cmd=" ".join(cmd),
                               detail=(detail[-1] if detail else "-")[:200]))
    _say(_t("lite.service.enabled", unit=unit.name))
    claude = shutil.which("claude")
    env_path = _env_file_path(gen.ENV_FILE)
    if claude and env_path is not None and str(Path(claude).parent) not in env_path.split(":"):
        _say(_t("lite.service.path_hint", dir=Path(claude).parent, env=gen.ENV_FILE))
    _say(_t("lite.service.linger"))
    _say(_t("lite.service.stop", unit=unit.name))
    return 0


def _env_file_path(env_file: Path) -> str | None:
    try:
        for ln in env_file.read_text(encoding="utf-8").splitlines():
            if ln.startswith("PATH="):
                return ln[len("PATH="):]
    except OSError:
        return None
    return None


def cmd_up(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="macp up", description=_t("lite.help.up"))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help=_t("lite.help.up_once"))
    g.add_argument("--service", action="store_true", help=_t("lite.help.up_service"))
    args = p.parse_args(argv)
    vault = _require_vault()
    if args.service:
        return install_service(vault)
    if args.once and "USAGE_GUARD_DISABLE" not in os.environ:
        os.environ["USAGE_GUARD_DISABLE"] = "1"
    os.chdir(vault)      # a few engine paths are still cwd-relative (runner, mailbox)
    from .orchestrator.lease import Lease
    lease = Lease(vault / "Worker" / "up.lock", ttl=UP_LEASE_TTL_S)
    if not lease.acquire():
        cur = lease.current()
        _say(_t("lite.up.busy", holder=cur.holder if cur else "?"))
        return 0
    try:
        return up_once(vault) if args.once else up_loop(vault)
    finally:
        lease.release()


# --- macp status / dashboard / memory --------------------------------------------


def latest_run_dir(vault: Path, slug: str) -> Path | None:
    runs = vault / "Worker" / "runs"
    if not runs.is_dir():
        return None
    pat = re.compile(RUN_DIR_RE % re.escape(slug))
    found = sorted(p for p in runs.iterdir() if p.is_dir() and pat.match(p.name))
    return found[-1] if found else None


def pr_url(run_dir: Path | None) -> str | None:
    """The PR a run recorded — report.md for the 3-stage lane, quick.md for quick
    (the runner reads report.md only, so a quick run's link lives here alone)."""
    if run_dir is None:
        return None
    for name in ("report.md", "quick.md"):
        p = run_dir / name
        if p.is_file():
            m = PR_URL_RE.search(p.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(0)
    return None


def review_hint(vault: Path, slug: str, repo: str | None) -> str:
    from .orchestrator.stages import is_url_remote
    url = pr_url(latest_run_dir(vault, slug))
    branch = f"worker/{slug}"
    if url:
        return _t("lite.status.review_pr", url=url)
    if repo and not is_url_remote(repo):
        return _t("lite.status.review_local", branch=branch, repo=repo, slug=slug)
    return _t("lite.status.review_branch", branch=branch, slug=slug)


def print_status(vault: Path) -> None:
    from .orchestrator.assignment import AWAITING_HUMAN
    from .orchestrator.orchestrator import status_line
    eng = Engine(vault)
    rows = eng.ledger()
    if not rows:
        _say(_t("lite.status.empty"))
        return
    _say(_t("lite.status.tasks"))
    for a in rows:
        _say("  " + status_line(a).rstrip())
        if a.state == AWAITING_HUMAN:
            _say("    -> " + review_hint(vault, a.task_id, eng.task_repo(a.task_id)))


def cmd_status(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="macp status", description=_t("lite.help.status")).parse_args(argv)
    vault = resolve_vault()
    if not is_vault(vault):
        _say(_t("lite.common.no_vault", path=vault))
        return 1
    bind_vault(vault)
    _say(_t("lite.status.vault", path=vault))
    repo = repo_root()
    if repo is not None and _registered(repo):
        _say(_t("lite.status.repo", repo=repo))
    else:
        _say(_t("lite.status.no_repo"))
    from . import claude_auth
    if shutil.which("claude") is None:
        _say(_t("lite.common.no_claude"))
    else:
        _say(_t("lite.status.auth", source=claude_auth.auth_source(claude_auth.claude_env())))
    print_status(vault)
    return 0


def cmd_dashboard(argv: list[str]) -> int:
    _require_vault()
    from . import engine_script
    return engine_script.main(["macp_dashboard.py", *argv])


def cmd_memory(argv: list[str]) -> int:
    from .macp.__main__ import main as project_cli
    return project_cli(["memory", *argv])


COMMANDS = {"init": cmd_init, "add": cmd_add, "up": cmd_up, "status": cmd_status,
            "dashboard": cmd_dashboard, "memory": cmd_memory}


def main(argv: list[str]) -> int:
    """``argv`` starts with one of :data:`VERBS` (``_entry`` checked)."""
    verb, rest = argv[0], argv[1:]
    try:
        return COMMANDS[verb](rest)
    except LiteError as e:
        print(f"macp {verb}: {e}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:      # noqa: BLE001 — one readable line, never a raw traceback
        tb = e.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        where = (f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
                 if tb is not None else "?")
        print(f"macp {verb}: " + _t("lite.common.unexpected", kind=type(e).__name__,
                                    detail=str(e)[:200], where=where),
              file=sys.stderr, flush=True)
        return 1
