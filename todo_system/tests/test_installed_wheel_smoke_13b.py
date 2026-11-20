"""Installed-wheel smoke for 13b: the daemon shell scripts run from site-packages.

Extends the 13a smoke (``test_installed_wheel_smoke_13a.py``) without touching
it. The wheel comes from the session-scoped ``installed_wheel`` in
``conftest.py`` (built once per pytest session, offline with ``UV_OFFLINE=1``),
everything under pytest's tmp dirs — the build goes to ``--out-dir``, the venv,
the vault and ``HOME`` are tmp, the real vault is never named.

What it pins, from site-packages, with ``python -P`` and ``PYTHONPATH`` /
``MACP_PYTHON`` scrubbed so nothing can quietly import this checkout:

* ``todo_system/_engine`` ships no ``pyproject.toml`` — the premise of the
  scripts' layout test — and the installed scripts carry the 13b branch.
* ``python -m todo_system.engine_script worker_run_next.sh`` **really runs** to
  rc 0 (AC2). The script ignores its arguments and runs ``maintenance`` against
  the throwaway vault (primed with ``cli import-md`` so the queue readers find
  a ``todo.db``). ``HOME`` is tmp because the script prepends
  ``$HOME/.local/bin`` to ``PATH``, where a
  ``uv`` tripwire sits — if the installed branch were not taken the old
  ``uv run --project .../_engine`` line would hit the tripwire (rc 97) instead
  of the real uv, so "no tripwire log" proves the branch, not just the rc.
* ``engine_script kakao_send.sh`` resolves the card link through the
  site-packages ``paths.repo_slug`` under ``MACP_PYTHON`` (the positive
  control): before 13b the ``/usr/bin/python3`` call could not import
  ``todo_system`` from ``_engine`` and the link degraded to the neutral
  placeholder. Likewise ``KAKAO_LEVEL=digest`` reaches ``kakao_route.py``'s
  real router (queued, nothing sent) instead of its import-failure fail-open
  (sent anyway). The network is a fake ``curl`` that records its argv.

The two script-running cases need zsh (the scripts are ``#!/bin/zsh``); the
build and layout cases have no skip, the leaf-12 stance. No literal from the
identifier gate's pattern may appear in this file: it sits inside the scanned
tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# Env that would point uv or the children back at this checkout / the live vault,
# or pre-decide the scripts' interpreter.
_SCRUB = (
    "PYTHONPATH",
    "PYTHONSAFEPATH",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "WORKER_HOST",
    "MAESTRO_VAULT_DIR",
    "KAKAO_VAULT_DIR",
    "MACP_VAULT_ROOT",
    "MACP_PYTHON",
    "KAKAO_LEVEL",
    "KAKAO_EVENT_KEY",
    "KAKAO_PROFILE",
    "MACP_REPO_SLUG",
)
_REPO_SLUG = "acme/smoke-13b"
_BRANCH = '[[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]'
_DAEMON_SCRIPTS = ("orchestrator_tick.sh", "worker_run_next.sh", "brief.sh",
                   "kakao_send.sh", "notify_kakao.sh")
_UNTOUCHED_SCRIPTS = ("kakao_token_refresh.sh", "kakao_oauth_helper.sh")
_SAMPLE_TASK = """---
tags:
  - todo
  - infra
status: open
due: "-"
updated: 2026-10-08
outcome: "installed-wheel smoke 13b — daemon shell scripts from site-packages"
next_action: "none; fixture only"
if_then: "used only inside the throwaway vault"
estimate_p50: 5m
estimate_p90: 10m
review_cycle: "-"
---

# installed-wheel smoke 13b sample task

- [ ] exists so Todo/items is a real vault tree (5m)

## Progress Log

- 2026-10-08: created by test_installed_wheel_smoke_13b.py
"""
# Tripwire in $HOME/.local/bin: the old uv line would land here first.
_UV_TRIPWIRE = """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$SMOKE_UV_LOG"
exit 97
"""
# Stand-in for curl: one argv element per line, then a canned 200.
_FAKE_CURL = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done >> "$SMOKE_CURL_LOG"
printf -- '--END--\\n' >> "$SMOKE_CURL_LOG"
printf '{}\\nHTTP_CODE:200'
"""

needs_zsh = pytest.mark.skipif(shutil.which("zsh") is None,
                               reason="the daemon scripts are #!/bin/zsh")
needs_system_python = pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(),
    reason="the kakao scripts keep their stdlib helpers on /usr/bin/python3")


def _base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _SCRUB}


def _child_env(vault: Path) -> dict[str, str]:
    env = _base_env()
    env["MACP_VAULT_ROOT"] = str(vault)
    env["SMOKE_REPO"] = str(REPO)
    return env


def _script_env(vault: Path, home: Path, *, extra_path: Path | None = None) -> dict[str, str]:
    """Env for a daemon script run: throwaway vault, tmp HOME (tripwire +
    migration check), the fakes' log paths, optionally a bin dir ahead of PATH."""
    env = _child_env(vault)
    env["HOME"] = str(home)
    env["SMOKE_UV_LOG"] = str(home / "uv-tripwire.log")
    env["SMOKE_CURL_LOG"] = str(home / "curl-argv.log")
    env["PYTHONUTF8"] = "1"
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return env


def _run(args, *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(a) for a in args], cwd=str(cwd), env=env,
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )


def _ok(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    assert proc.returncode == 0, (
        f"{what}: rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def _py(installed, vault: Path, cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return _run([installed.venv / "bin" / "python", "-P", *args],
                cwd=cwd, env=_child_env(vault), timeout=timeout)


def _engine_script(installed, cwd: Path, env: dict[str, str], *args: str,
                   timeout: int = 180) -> subprocess.CompletedProcess:
    return _run([installed.venv / "bin" / "python", "-P", "-m", "todo_system.engine_script", *args],
                cwd=cwd, env=env, timeout=timeout)


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture(scope="module")
def installed(installed_wheel):
    """The session's single wheel install (conftest ``installed_wheel``)."""
    return installed_wheel


@pytest.fixture(scope="module")
def vault(tmp_path_factory) -> Path:
    """Throwaway vault: the isolation script's skeleton, the .macp marker with a
    repo_slug (what the kakao card link resolves to), a kakao token for the
    send script. The scripts create logs/ and Worker/_state themselves."""
    root = tmp_path_factory.mktemp("smoke-vault-13b")
    for rel in ("Todo/items", "Memory/sessions", "Memory/episodes", "Notes",
                "Decisions", "Worker", ".macp", ".secrets"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / ".macp" / "config.toml").write_text(f'repo_slug = "{_REPO_SLUG}"\n', encoding="utf-8")
    (root / "Todo" / "items" / "sample-installed-smoke-13b.md").write_text(
        _SAMPLE_TASK, encoding="utf-8")
    (root / ".secrets" / "kakao-token.json").write_text(
        json.dumps({"access_token": "AT-smoke-13b", "refresh_token": "RT-smoke-13b"}),
        encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def empty_cwd(tmp_path_factory) -> Path:
    """Legs that must not depend on the cwd run from here: nothing above it is a vault."""
    return tmp_path_factory.mktemp("smoke-cwd-13b")


@pytest.fixture(scope="module")
def home(tmp_path_factory) -> Path:
    """Tmp HOME with the uv tripwire where worker_run_next.sh looks first."""
    root = tmp_path_factory.mktemp("smoke-home-13b")
    _write_exec(root / ".local" / "bin" / "uv", _UV_TRIPWIRE)
    return root


@pytest.fixture(scope="module")
def fake_bin(tmp_path_factory) -> Path:
    """A bin dir holding the fake curl, to be put ahead of PATH for kakao runs."""
    root = tmp_path_factory.mktemp("smoke-bin-13b")
    _write_exec(root / "curl", _FAKE_CURL)
    return root


@pytest.fixture(scope="module")
def site_engine(installed, vault, empty_cwd) -> Path:
    """site-packages/todo_system/_engine of the installed wheel, from the child."""
    proc = _ok(_py(installed, vault, empty_cwd, "-c",
                   "from todo_system import paths; print(paths.engine_root())"),
               "engine_root probe")
    root = Path(proc.stdout.strip().splitlines()[-1])
    assert root.parts[-3:] == ("site-packages", "todo_system", "_engine"), root
    assert root.is_relative_to(installed.venv.resolve()), (root, installed.venv)
    return root


@pytest.fixture(scope="module")
def primed_vault(installed, vault, empty_cwd) -> Path:
    # maintenance's queue readers go straight to the DB, so import-md builds
    # Todo/todo.db first (the isolation script's prime step, as in leaf 12).
    _ok(_py(installed, vault, empty_cwd, "-m", "todo_system.cli", "import-md"), "cli import-md")
    assert (vault / "Todo" / "todo.db").is_file()
    return vault


# --- the layout premise and the wheel's script bodies ----------------------------


def test_installed_engine_dir_is_not_a_project(site_engine):
    # The scripts' layout test: no pyproject beside an installed _engine.
    assert not (site_engine / "pyproject.toml").exists(), site_engine
    for name in _DAEMON_SCRIPTS:
        assert (site_engine / "scripts" / name).is_file(), name


def test_wheel_scripts_carry_the_installed_branch(site_engine):
    # `uv build` reads the working tree, so this pins what shipped, not the checkout.
    for name in _DAEMON_SCRIPTS:
        text = (site_engine / "scripts" / name).read_text(encoding="utf-8")
        assert text.count(_BRANCH) == 1, name
    for name in _UNTOUCHED_SCRIPTS:
        text = (site_engine / "scripts" / name).read_text(encoding="utf-8")
        assert "MACP_PYTHON" not in text, name


def _dry_run(proc: subprocess.CompletedProcess) -> dict[str, str]:
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 3, proc.stdout
    return dict(ln.split("=", 1) for ln in lines)


def test_engine_script_dry_run_worker_run_next_is_unchanged(installed, vault, empty_cwd):
    proc = _ok(_py(installed, vault, empty_cwd, "-m", "todo_system.engine_script",
                   "--dry-run", "worker_run_next.sh"),
               "engine_script --dry-run worker_run_next.sh")
    got = _dry_run(proc)
    path = Path(got["script"])
    assert path.parts[-4:] == ("todo_system", "_engine", "scripts", "worker_run_next.sh"), path
    assert path.is_relative_to(installed.venv.resolve()), (path, installed.venv)
    assert json.loads(got["argv"]) == [str(path)]
    assert got["executable"] == "true", got


# --- AC2: engine_script worker_run_next.sh really runs from site-packages --------


@needs_zsh
def test_engine_script_runs_worker_run_next_from_site_packages(installed, primed_vault,
                                                               empty_cwd, home, site_engine):
    env = _script_env(primed_vault, home)
    proc = _engine_script(installed, empty_cwd, env, "worker_run_next.sh")
    out = proc.stdout + proc.stderr
    print(f"--- engine_script worker_run_next.sh rc={proc.returncode} ---\n{out}")  # -rA shows it
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}"

    # The script's own log line, written from the vault's logs/.
    log = (primed_vault / "logs" / "worker.out.log").read_text(encoding="utf-8", errors="replace")
    assert "worker_run_next: maintenance" in log, log
    # The installed branch was taken: the tripwire never fired, uv never named.
    assert not Path(env["SMOKE_UV_LOG"]).exists(), Path(env["SMOKE_UV_LOG"]).read_text()
    assert "uv run" not in out, out
    assert "ERROR uv not on PATH" not in out, out
    # No side effects outside the vault: no venv in the vault, no logs in the engine.
    assert not (primed_vault / ".venv").exists()
    assert not (site_engine / "logs").exists()
    assert not (home / "Projects").exists()


# --- positive control: the kakao script imports todo_system from site-packages ----


def _curl_calls(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    calls, current = [], []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line == "--END--":
            calls.append(current)
            current = []
        else:
            current.append(line)
    return calls


def _sent_template(call: list[str]) -> dict:
    for i, arg in enumerate(call):
        if arg == "--data-urlencode" and i + 1 < len(call):
            key, _, value = call[i + 1].partition("=")
            if key == "template_object":
                return json.loads(value)
    raise AssertionError(f"no template_object in {call}")


@needs_zsh
@needs_system_python
def test_engine_script_runs_kakao_send_with_site_packages_resolver(installed, vault, empty_cwd,
                                                                   home, fake_bin):
    env = _script_env(vault, home, extra_path=fake_bin)
    proc = _engine_script(installed, empty_cwd, env, "kakao_send.sh", "smoke 13b")
    out = proc.stdout + proc.stderr
    print(f"--- engine_script kakao_send.sh rc={proc.returncode} ---\n{out}")
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}"
    assert "sent (200)" in out, out

    memo = [c for c in _curl_calls(Path(env["SMOKE_CURL_LOG"]))
            if any("talk/memo/default/send" in a for a in c)]
    assert len(memo) == 1, memo
    assert "Authorization: Bearer AT-smoke-13b" in memo[0], memo[0]
    template = _sent_template(memo[0])
    assert template["text"] == "smoke 13b"
    # RED before 13b: /usr/bin/python3 could not import todo_system from
    # _engine, so the link fell back to the neutral placeholder.
    assert template["link"]["web_url"] == f"https://github.com/{_REPO_SLUG}", template


@needs_zsh
@needs_system_python
def test_engine_script_kakao_digest_routes_through_site_packages(installed, vault, empty_cwd,
                                                                 home, fake_bin):
    env = _script_env(vault, home, extra_path=fake_bin)
    env["KAKAO_LEVEL"] = "digest"
    before = len(_curl_calls(Path(env["SMOKE_CURL_LOG"])))
    proc = _engine_script(installed, empty_cwd, env, "kakao_send.sh", "digest 13b")
    out = proc.stdout + proc.stderr
    print(f"--- engine_script kakao_send.sh (digest) rc={proc.returncode} ---\n{out}")
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}"
    # RED before 13b: kakao_route.py failed its import, printed SEND (fail-open)
    # and the memo went out. Routed for real, it is queued and nothing is sent.
    assert "route: QUEUED" in out, out
    assert len(_curl_calls(Path(env["SMOKE_CURL_LOG"]))) == before
    queue = vault / "logs" / "kakao_digest.jsonl"
    assert queue.is_file(), sorted(p.name for p in (vault / "logs").iterdir())
    assert "digest 13b" in queue.read_text(encoding="utf-8")
