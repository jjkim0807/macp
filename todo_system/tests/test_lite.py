"""Lite mode — ``macp init | add | up | status`` (todo_system/lite.py).

The path test at the bottom is the task's first acceptance criterion: a clean
HOME, a throwaway git repository with no remote and a fake ``claude``
(``fixtures/lite_fake_claude/claude``), then exactly ``macp init`` →
``macp add "…"`` → ``macp up --once`` through the real orchestrator and runner
(``live-staged``, quick lane). Everything else pins one piece: vault resolution,
the ``_entry`` routing, the ``add`` schema and its human errors, the rendered
task against ``cli validate``'s checker, the order of the calls ``up`` makes,
and the ``--service`` unit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import _entry, lite, task_validate
from todo_system import worker as worker_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_SRC = Path(__file__).resolve().parent / "fixtures" / "lite_fake_claude" / "claude"
DROP_PREFIXES = ("MACP_", "USAGE_GUARD_", "CLAUDE_", "ANTHROPIC_", "KAKAO_", "MAESTRO_",
                 "GIT_", "XDG_", "WORKER_", "SLACK_")
DROP_NAMES = ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONSAFEPATH", "HOME")


# --- helpers ----------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _fake_bin(tmp_path: Path) -> Path:
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    dst = d / "claude"
    shutil.copyfile(FAKE_SRC, dst)
    dst.chmod(0o755)
    return d


def _clean_env(tmp_path: Path, **extra: str) -> tuple[dict, Path]:
    """What a newcomer's fresh shell has: a new HOME, no MACP_* / USAGE_GUARD_* /
    CLAUDE_* (nor git/XDG redirects), and the fake `claude` first on PATH."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(DROP_PREFIXES) and k not in DROP_NAMES}
    env.update(HOME=str(home), PYTHONPATH=str(REPO_ROOT),
               PATH=f"{_fake_bin(tmp_path)}{os.pathsep}{os.environ.get('PATH', '')}",
               FAKE_CLAUDE_LOG=str(tmp_path / "fake-claude.log"))
    env.update(extra)
    return env, home


def _user_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "-c", "user.name=someone", "-c", "user.email=someone@example.invalid",
         "commit", "-q", "-m", "init")
    return path


def _macp(args: list[str], cwd: Path, env: dict, timeout: int = 900):
    return subprocess.run([sys.executable, "-m", "todo_system._entry", *args], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=timeout)


def _task_fields(**over) -> dict:
    base = {
        "slug": "fix-usage-typo", "title": "Fix the usage typo",
        "outcome": "The usage text is spelled right",
        "next_action": "Edit src/cli.py", "if_then": "When opened -> edit src/cli.py",
        "acceptance_criteria": ["`python -m pytest -q` passes"],
        "worker_mode": "quick", "estimate_p50": "10m", "estimate_p90": "20m",
        "touches": ["src/cli.py"], "tags": ["docs"],
    }
    base.update(over)
    return base


# --- vault resolution -------------------------------------------------------------


def test_resolve_vault_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path / "v"))
    assert lite.resolve_vault(start=tmp_path) == tmp_path / "v"


def test_resolve_vault_accepts_a_marker_only_when_it_is_a_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    monkeypatch.setenv("MACP_HOME", str(tmp_path / "mh"))
    plain = tmp_path / "project"
    (plain / ".macp").mkdir(parents=True)
    (plain / ".macp" / "config.toml").write_text('profile = "default"\n')
    # a project marker without vault_root resolves to the project itself: not a vault
    assert lite.resolve_vault(start=plain) == tmp_path / "mh" / "vault"
    (plain / "Todo" / "items").mkdir(parents=True)
    assert lite.resolve_vault(start=plain) == plain


def test_default_vault_follows_macp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MACP_HOME", str(tmp_path / "mh"))
    assert _entry.default_vault() == tmp_path / "mh" / "vault"
    monkeypatch.delenv("MACP_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _entry.default_vault() == tmp_path / ".macp" / "vault"


# --- macp init pieces -------------------------------------------------------------


def test_ensure_vault_creates_a_committed_main_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MACP_HOME", raising=False)
    vault = tmp_path / "home" / ".macp" / "vault"
    assert lite.ensure_vault(vault) is True
    assert (vault / "Todo" / "items").is_dir() and (vault / "CLAUDE.md").is_file()
    assert _git(vault, "branch", "--show-current") == "main"
    assert _git(vault, "rev-list", "--count", "HEAD") == "1"
    assert _git(vault, "status", "--porcelain") == ""
    cfg = (vault / ".macp" / "config.toml").read_text()
    assert cfg.rstrip().endswith('worker_root = "~/.macp/work"')
    assert "Worker/runs/" in (vault / ".gitignore").read_text()
    assert lite.ensure_vault(vault) is False          # idempotent


def test_ensure_vault_refuses_a_foreign_directory(tmp_path):
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / "something.txt").write_text("x")
    with pytest.raises(lite.LiteError):
        lite.ensure_vault(odd)


def test_register_repo_keeps_other_keys_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = _user_repo(tmp_path / "r")
    (repo / ".macp").mkdir()
    (repo / ".macp" / "config.toml").write_text(
        'profile = "work"\nlanguage = "ko"\n\n[extra]\nk = "v"\n')
    vault = tmp_path / ".macp" / "vault"
    changed = lite.register_repo(repo, vault)
    text = (repo / ".macp" / "config.toml").read_text()
    assert 'language = "ko"' in text and '[extra]' in text
    assert text.index('vault_root = "~/.macp/vault"') < text.index("[extra]")
    assert ".macp/config.toml" in changed and "AGENTS.md" in changed
    assert lite.register_repo(repo, vault) == []


# --- macp add: schema, rendering, errors -------------------------------------------


def test_extract_json_tolerates_prose_and_fences():
    assert lite.extract_json('sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert lite.extract_json('x {"a": [1, 2]} y') == {"a": [1, 2]}
    assert lite.extract_json("no json here") is None
    assert lite.extract_json("[1, 2]") is None


@pytest.mark.parametrize("over,field", [
    ({"slug": "Bad Slug"}, "slug"),
    ({"worker_mode": "manual"}, "worker_mode"),
    ({"outcome": "two\nlines"}, "outcome"),
    ({"touches": []}, "touches"),
    ({"acceptance_criteria": "not a list"}, "acceptance_criteria"),
    ({"estimate_p50": "soon"}, "estimate_p50"),
    ({"next_action": ""}, "next_action"),
])
def test_check_task_names_the_bad_field(over, field, monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    with pytest.raises(lite.LiteError) as e:
        lite.check_task(_task_fields(**over))
    assert f"({field}:" in str(e.value)


def test_schema_error_reads_in_korean_too(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    with pytest.raises(lite.LiteError) as e:
        lite.check_task(_task_fields(worker_mode="x"))
    assert "올바르지 않아요" in str(e.value) and "worker_mode" in str(e.value)


def test_rendered_task_passes_the_validator(tmp_path):
    fields = lite.check_task(_task_fields(
        outcome='Prints "hello" # not a comment', next_action="Use C:\\path"))
    text = lite.render_task(fields, slug="fix-usage-typo", repo_value=str(tmp_path / "r"),
                            request="고쳐 줘 \"usage\"", references=["CLAUDE.md"],
                            today="2026-11-26")
    path = tmp_path / "fix-usage-typo.md"
    path.write_text(text, encoding="utf-8")
    assert [i for i in task_validate.validate_file(path) if i.level == task_validate.ERROR] == []
    fm, body = worker_mod.parse_frontmatter(text)
    assert fm["outcome"] == "Prints 'hello' # not a comment"
    assert fm["next_action"] == "Use C:/path"
    assert fm["auto_merge"] in (False, "false")
    assert fm["worker_mode"] == "quick" and fm["touches"] == ["src/cli.py"]
    assert fm["repo"] == str(tmp_path / "r")
    assert "allowed_tools" not in fm
    assert "Read first: CLAUDE.md" in body and "not a safety or finance marker" in body


def test_unique_slug_appends_a_number(tmp_path):
    (tmp_path / "x-y.md").write_text("")
    assert lite.unique_slug("x-y", tmp_path) == "x-y-2"
    assert lite.unique_slug("z-z", tmp_path) == "z-z"


@pytest.fixture
def fake_claude_on_path(tmp_path, monkeypatch):
    from todo_system.orchestrator import model_select
    monkeypatch.setenv("PATH", f"{_fake_bin(tmp_path)}{os.pathsep}{os.environ['PATH']}")
    # resolve_model would probe and cache into the test vault (vault-template)
    monkeypatch.setattr(model_select, "resolve_model", lambda **_k: "fake-model")
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    return monkeypatch


def test_ask_claude_returns_the_task(fake_claude_on_path):
    obj = lite.ask_claude("x\n## The request (verbatim)\ny")
    assert obj["slug"] == "readme-install-line"


def test_ask_claude_does_not_read_a_task_about_limits_as_a_limit(fake_claude_on_path):
    reply = json.dumps(_task_fields(outcome="Raise the API usage limit to 10"))
    fake_claude_on_path.setenv("FAKE_CLAUDE_ADD_REPLY", reply)
    assert lite.ask_claude("## The request (verbatim)\n")["outcome"].startswith("Raise")


@pytest.mark.parametrize("reply,rc,needle", [
    ("I could not do that.", "0", "was not a task"),
    ("Error: not logged in · please run /login", "1", "not logged in"),
    ("boom", "3", "exited with code 3"),
])
def test_ask_claude_failures_are_one_readable_line(fake_claude_on_path, reply, rc, needle):
    fake_claude_on_path.setenv("FAKE_CLAUDE_ADD_REPLY", reply)
    fake_claude_on_path.setenv("FAKE_CLAUDE_ADD_RC", rc)
    with pytest.raises(lite.LiteError) as e:
        lite.ask_claude("## The request (verbatim)\n")
    assert needle in str(e.value) and "\n" not in str(e.value)


def test_ask_claude_without_claude_says_how_to_install(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    with pytest.raises(lite.LiteError) as e:
        lite.ask_claude("p")
    assert "`claude` is not on PATH" in str(e.value)


# --- macp up: the call order --------------------------------------------------------


def test_up_once_collects_before_the_closing_tick(monkeypatch, tmp_path):
    calls: list[str] = []
    for name in ("sync_tasks", "tick", "run_worker_once", "collect",
                 "publish_local_branches", "commit_tasks"):
        monkeypatch.setattr(lite.Engine, name,
                            lambda self, _n=name: calls.append(_n) or 0)
    monkeypatch.setattr(lite, "print_status", lambda vault: calls.append("status"))
    assert lite.up_once(tmp_path) == 0
    assert calls == ["sync_tasks", "tick", "run_worker_once", "collect", "tick",
                     "publish_local_branches", "commit_tasks", "status"]


def test_up_loop_ticks_after_a_handled_message_and_survives_errors(monkeypatch, tmp_path):
    calls: list[str] = []

    class FakeRunner:
        n = iter([1, RuntimeError("bad message")])

        def poll_once(self):
            nxt = next(self.n)
            if isinstance(nxt, Exception):
                raise nxt
            calls.append("poll")
            return nxt

        def _announce(self, slots, **_k):
            calls.append(f"announce{slots}")

    from todo_system import cli
    monkeypatch.setattr(lite.Engine, "make_runner", lambda self: FakeRunner())
    for name in ("sync_tasks", "tick", "collect", "publish_local_branches", "commit_tasks"):
        monkeypatch.setattr(lite.Engine, name, lambda self, _n=name: calls.append(_n) or 0)
    monkeypatch.setattr(cli, "snapshot_mtime", lambda: {"a": 1.0})
    sleeps: list = []
    assert lite.up_loop(tmp_path, sleep=sleeps.append, clock=lambda: 0.0, max_cycles=2) == 0
    assert calls == ["sync_tasks", "tick", "poll", "announce1", "collect", "tick",
                     "publish_local_branches", "commit_tasks"]
    assert len(sleeps) == 2


# --- macp up --service --------------------------------------------------------------


def test_service_renders_one_unit_with_the_existing_renderer(tmp_path, monkeypatch):
    if shutil.which("systemctl") is None or not sys.platform.startswith("linux"):
        pytest.skip("systemd host only")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    (vault / "Todo" / "items").mkdir(parents=True)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    ran: list = []

    def fake_run(cmd, **_k):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert lite.install_service(vault, run=fake_run) == 0
    unit = tmp_path / "home" / ".config" / "systemd" / "user" / "macp-up.service"
    text = unit.read_text()
    assert f"ExecStart={sys.executable} -P -m todo_system._entry up" in text
    assert f"Environment=MACP_VAULT_ROOT={vault}" in text
    assert f"WorkingDirectory={vault}" in text
    assert "Restart=always" in text and "WantedBy=default.target" in text
    assert (tmp_path / "home" / ".config" / "macp" / "env").is_file()
    assert ran == [["systemctl", "--user", "daemon-reload"],
                   ["systemctl", "--user", "enable", "--now", "macp-up.service"]]


# --- _entry routing (subprocesses: a fresh interpreter each) -------------------------


def test_non_lite_argv_never_imports_lite(tmp_path):
    vault = tmp_path / "vault"
    shutil.copytree(REPO_ROOT / "vault-template", vault)
    env, _ = _clean_env(tmp_path, MACP_VAULT_ROOT=str(vault))
    code = ("import sys\nfrom unittest import mock\nfrom todo_system import _entry, cli\n"
            "with mock.patch.object(cli, 'main', return_value=0):\n"
            "    rc = _entry.main(['list'])\n"
            "print(rc, 'todo_system.lite' in sys.modules)\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0 False"


def test_no_vault_anywhere_points_at_macp_init(tmp_path):
    env, _ = _clean_env(tmp_path)
    r = _macp(["list"], tmp_path, env, timeout=300)
    assert r.returncode == 2
    assert "macp init" in r.stderr and "Traceback" not in r.stderr


def test_existing_verbs_fall_back_to_the_default_vault(tmp_path):
    env, home = _clean_env(tmp_path)
    shutil.copytree(REPO_ROOT / "vault-template", home / ".macp" / "vault")
    r = _macp(["import-md"], tmp_path, env, timeout=300)
    assert r.returncode == 0, r.stderr
    assert "imported tasks: 0" in r.stdout
    assert (home / ".macp" / "vault" / "Todo" / "todo.db").is_file()


def test_add_failure_is_readable_and_saves_nothing(tmp_path):
    env, home = _clean_env(tmp_path, FAKE_CLAUDE_ADD_REPLY="sorry, no JSON today")
    repo = _user_repo(tmp_path / "proj")
    assert _macp(["init"], repo, env).returncode == 0
    r = _macp(["add", "do something"], repo, env)
    assert r.returncode == 1
    assert "was not a task (JSON)" in r.stderr and "Traceback" not in r.stderr
    assert list((home / ".macp" / "vault" / "Todo" / "items").glob("*.md")) == []


def test_add_outside_a_registered_repo_says_init_first(tmp_path):
    env, _ = _clean_env(tmp_path)
    repo = _user_repo(tmp_path / "proj")
    assert _macp(["init"], tmp_path, env).returncode == 0       # vault only
    r = _macp(["add", "x"], repo, env)
    assert r.returncode == 1 and "`macp init` there first" in r.stderr


# --- the path test (acceptance criterion 1) ------------------------------------------


def test_three_commands_take_a_fresh_repo_to_a_worker_branch(tmp_path):
    env, home = _clean_env(tmp_path)
    repo = _user_repo(tmp_path / "proj")
    engine_vars = sorted(k for k in env if k.startswith(("MACP_", "USAGE_GUARD_")))
    assert engine_vars == []

    init = _macp(["init"], repo, env)
    assert init.returncode == 0, init.stdout + init.stderr
    add = _macp(["add", "README에 설치 한 줄 추가"], repo, env)
    assert add.returncode == 0, add.stdout + add.stderr
    up = _macp(["up", "--once"], repo, env)
    assert up.returncode == 0, up.stdout + up.stderr

    vault = home / ".macp" / "vault"
    # init: the marker points at the vault (no user name: `~`)
    assert 'vault_root = "~/.macp/vault"' in (repo / ".macp" / "config.toml").read_text()
    # add: one task file, valid for `cli validate`
    tasks = sorted((vault / "Todo" / "items").glob("*.md"))
    assert [p.stem for p in tasks] == ["readme-install-line"]
    slug = tasks[0].stem
    val = _macp(["validate", slug], repo, {**env, "MACP_VAULT_ROOT": str(vault)})
    assert val.returncode == 0 and "worker contract OK" in val.stdout, val.stdout + val.stderr
    # up: the worker's commit is on worker/<slug> in the user's own repository …
    branch = f"worker/{slug}"
    subjects = _git(repo, "log", "--format=%s", f"main..{branch}")
    assert subjects == f"chore(worker): {slug} — add an install line"
    assert "Install:" in _git(repo, "show", f"{branch}:README.md")
    # … whose checkout and branch did not move (init's own files are the only change)
    assert _git(repo, "branch", "--show-current") == "main"
    dirty = {ln[3:] for ln in _git(repo, "status", "--porcelain").splitlines()}
    assert dirty == {".macp/", "AGENTS.md"}
    # the ledger row waits for review, and status says where to look
    assert re.search(rf"awaiting_human\s+{slug}\s+worker-default", up.stdout), up.stdout
    assert f"branch {branch} in {repo}" in up.stdout
    # collect ran after the worker and before the closing tick
    out = up.stdout
    assert out.index("processed 1 message(s)") < out.index("== collect results ==") \
        < out.rindex("candidates=")
    # the real pipeline ran: probe, add, one quick stage — and nothing else
    calls = (tmp_path / "fake-claude.log").read_text().split("\n")
    assert [c for c in calls if c] == ["probe", "add", "stage quick"]
    # the vault keeps its runtime state out of git; the task file is committed
    assert _git(vault, "status", "--porcelain") == ""
    assert _git(vault, "log", "-1", "--format=%s") in (f"macp add: {slug}",
                                                       "macp up: task state")
