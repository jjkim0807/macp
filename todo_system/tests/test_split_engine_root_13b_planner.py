"""Installed-wheel shape without a wheel: planner_tick.sh's MACP_PYTHON branch.

Public split 13b residue. ``scripts/planner_tick.sh`` (bash, the planner unit)
was left on its single ``ROOT`` anchor by 13b. It now has the same two anchors
as the 13b daemon scripts (``ENGINE_DIR`` = its own checkout, ``VAULT_DIR`` =
``MAESTRO_VAULT_DIR`` / ``MACP_VAULT_ROOT`` / the checkout) and the same
installed branch: when ``$ENGINE_DIR`` has no ``pyproject.toml`` AND
``MACP_PYTHON`` is set, every engine module runs as ``"$MACP_PYTHON" -P -m
todo_system...`` and neither ``uv`` nor its ``command -v uv`` guard is touched.

Unlike the 13b scripts, ``$PY`` stays a scalar string here (``ENGINE_PY`` too):
``test_planner_mode.py`` pins the ``$PY -m ...`` literals and runs the mode
block with ``PY="bash fake.sh"``.

Each case copies the real script into a fake ``<site>/todo_system/_engine``
tree (with or without a pyproject marker) and runs it under bash with a fake
``MACP_PYTHON``, ``uv``, ``claude`` and ``sqlite3`` that only record their argv,
cwd and env, so nothing is imported, built or sent. The real installed layout
is ``test_installed_wheel_smoke_13b_planner.py``. No literal from the identifier
gate's pattern may appear in this file: it sits inside the scanned tree.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_PLANNER = "planner_tick.sh"
_SEND = "kakao_send.sh"
_BRANCH_LINE = 'if [[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]; then'
# The PATH tail every case shares: system tools, no uv (it lives in a user dir).
_SYSTEM_PATH = "/usr/bin:/bin"

_RECORD = '''import json, os, sys
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": TOOL, "argv": sys.argv[1:], "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT"),
                         "PYTHONPATH": os.environ.get("PYTHONPATH"),
                         "MACP_PYTHON": os.environ.get("MACP_PYTHON")}) + "\\n")
if os.environ.get("SEAM_USAGE_BLOCK") == "1" and "todo_system.usage_guard" in sys.argv and "check" in sys.argv:
    sys.exit(3)
'''


# MACP_PYTHON and uv: record and exit 0 with no output (every gate passes,
# every context injection is empty, the lease is "taken").
_FAKE_PY = f'#!{sys.executable}\nTOOL = "py"\n{_RECORD}'
_FAKE_UV = f'#!{sys.executable}\nTOOL = "uv"\n{_RECORD}'

# claude: record; with SEAM_PLANNER_HIT=1, move worker/x with a planner-subject
# commit so the post-verify alert fires.
_FAKE_CLAUDE = f'''#!{sys.executable}
import subprocess
TOOL = "claude"
{_RECORD}
if os.environ.get("SEAM_PLANNER_HIT") == "1":
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{{tree}}"], text=True).strip()
    sha = subprocess.check_output(["git", "commit-tree", tree, "-p", "refs/heads/worker/x",
                                   "-m", "chore: planner hit"], text=True).strip()
    subprocess.check_call(["git", "update-ref", "refs/heads/worker/x", sha])
'''

_FAKE_SQLITE = "#!/bin/sh\nexit 0\n"

_STUB_SEND = r'''#!/bin/sh
printf '{"tool":"kakao","script":"%s","cwd":"%s"}\n' "$0" "$PWD" >> "$SEAM_TEST_LOG"
'''

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "seam", "GIT_AUTHOR_EMAIL": "seam@example.invalid",
    "GIT_COMMITTER_NAME": "seam", "GIT_COMMITTER_EMAIL": "seam@example.invalid",
}


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class _Harness:
    def __init__(self, tmp: Path, *, with_uv: bool = True):
        self.tmp = tmp
        self.log = tmp / "calls.jsonl"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        self.home.mkdir()
        if with_uv:
            _write_exec(self.bin / "uv", _FAKE_UV)
        self.fakepy = _write_exec(self.bin / "fakepy", _FAKE_PY)
        _write_exec(self.bin / "claude", _FAKE_CLAUDE)
        _write_exec(self.bin / "sqlite3", _FAKE_SQLITE)
        self.vault = tmp / "vault"
        (self.vault / ".macp").mkdir(parents=True)
        (self.vault / ".macp" / "config.toml").write_text("", encoding="utf-8")

    def engine(self, *, pyproject: bool) -> Path:
        """<site>/todo_system/_engine with a copy of the real script and a stub
        kakao_send.sh; the pyproject marker makes it look like a checkout."""
        root = self.tmp / "site" / "todo_system" / "_engine"
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(_REAL_ENGINE / "scripts" / _PLANNER, scripts / _PLANNER)
        _write_exec(scripts / _SEND, _STUB_SEND)
        if pyproject:
            (root / "pyproject.toml").write_text("# checkout marker\n", encoding="utf-8")
        return root

    def run(self, engine: Path, *, macp_python: bool, vault: bool = True,
            **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_VAULT_ROOT / PYTHONPATH / MACP_PYTHON,
        # and git cannot climb out of tmp into the checkout running the tests.
        full = {
            "PATH": f"{self.bin}{os.pathsep}{_SYSTEM_PATH}",
            "HOME": str(self.home),
            "SEAM_TEST_LOG": str(self.log),
            "GIT_CEILING_DIRECTORIES": str(self.tmp),
            "PYTHONUTF8": "1",
            "TZ": "UTC",
            **_GIT_ENV,
            **env,
        }
        if vault:
            full["MACP_VAULT_ROOT"] = str(self.vault)
        if macp_python:
            full["MACP_PYTHON"] = str(self.fakepy)
        return subprocess.run(["bash", str(engine / "scripts" / _PLANNER)], env=full,
                              capture_output=True, text=True, errors="replace", timeout=60)

    def calls(self, tool: str) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [r for r in rows if r["tool"] == tool]


def _modules(calls: list[dict]) -> set[str]:
    return {c["argv"][c["argv"].index("-m") + 1] for c in calls if "-m" in c["argv"]}


_ENGINE_MODULES = {
    "todo_system.orchestrator.hq_host", "todo_system.usage_guard", "todo_system.task_deps",
    "todo_system.orchestrator.planner_mode", "todo_system.orchestrator.model_select",
    "todo_system.orchestrator.followups", "todo_system.orchestrator.discovery",
    "todo_system.orchestrator.lease", "todo_system.orchestrator.usage_probe",
    "todo_system.cli",
}

# The four the AC names, with their exact installed argv.
_AC_ARGVS = (
    ["-P", "-m", "todo_system.orchestrator.hq_host", "--check"],
    ["-P", "-m", "todo_system.usage_guard", "check"],
    ["-P", "-m", "todo_system.orchestrator.model_select"],
    ["-P", "-m", "todo_system.cli", "import-md"],
)


# --- installed branch on ---------------------------------------------------------


def test_installed_layout_uses_macp_python_and_never_uv(tmp_path):
    h = _Harness(tmp_path)
    engine = h.engine(pyproject=False)

    r = h.run(engine, macp_python=True)

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert h.calls("uv") == [], (h.calls("uv"), r.stdout, r.stderr)
    py = h.calls("py")
    argvs = [c["argv"] for c in py]
    for want in _AC_ARGVS:
        assert want in argvs, (want, argvs)
    assert _modules(py) == _ENGINE_MODULES, argvs
    for call in py:
        assert call["argv"][:2] == ["-P", "-m"], call
        assert call["cwd"] == str(h.vault), call
        assert call["MACP_VAULT_ROOT"] == str(h.vault), call
        assert call["PYTHONPATH"] is None, call                 # _engine never on PYTHONPATH
    claude = h.calls("claude")
    assert len(claude) == 1 and claude[0]["cwd"] == str(h.vault), claude
    assert "[planner-tick] done" in r.stdout, r.stdout
    assert (h.vault / "Worker" / "_state" / "planner_tick_state.json").is_file()
    assert not (engine / "Worker").exists()


def test_installed_layout_skips_the_uv_guard(tmp_path):
    """No uv anywhere on PATH: before, every `command -v uv` gate skipped the
    engine call (hq_host, usage guard) or fell to a bare python3 that cannot
    import from _engine. The installed branch runs them all under MACP_PYTHON."""
    h = _Harness(tmp_path, with_uv=False)
    engine = h.engine(pyproject=False)
    assert shutil.which("uv", path=f"{h.bin}{os.pathsep}{_SYSTEM_PATH}") is None

    r = h.run(engine, macp_python=True)

    assert r.returncode == 0, (r.stdout, r.stderr)
    argvs = [c["argv"] for c in h.calls("py")]
    for want in _AC_ARGVS:
        assert want in argvs, (want, argvs, r.stdout, r.stderr)
    assert _modules(h.calls("py")) == _ENGINE_MODULES, argvs
    assert len(h.calls("claude")) == 1


def test_installed_layout_alerts_through_the_engine_kakao_script(tmp_path):
    """The post-verify alert: cwd is the vault, which has no scripts/, so the
    engine's own kakao_send.sh is named by path."""
    h = _Harness(tmp_path)
    engine = h.engine(pyproject=False)
    env = {**_GIT_ENV, "GIT_CEILING_DIRECTORIES": str(tmp_path), "HOME": str(h.home)}
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "base"],
                 ["branch", "worker/x"]):
        subprocess.run(["git", "-C", str(h.vault), *args], env={**os.environ, **env}, check=True)

    r = h.run(engine, macp_python=True, SEAM_PLANNER_HIT="1")

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "WARN planner commit landed on worker/*" in r.stdout, r.stdout
    kakao = h.calls("kakao")
    assert len(kakao) == 1, (kakao, r.stdout, r.stderr)
    assert kakao[0]["script"] == f"{engine}/scripts/{_SEND}", kakao
    assert kakao[0]["cwd"] == str(h.vault), kakao
    assert h.calls("uv") == []


# --- split checkout / fallback / combined -----------------------------------------


@pytest.mark.parametrize("pyproject,macp_python", [(False, False), (True, True)],
                         ids=["no-pyproject-no-macp-python", "checkout-with-macp-python"])
def test_split_layout_without_the_installed_branch_uses_uv_project(tmp_path, pyproject,
                                                                    macp_python):
    """Engine != vault but the branch is off (no MACP_PYTHON, or a checkout
    with pyproject that merely inherits it): uv run --project <engine> python -P,
    the engine on PYTHONPATH, the vault exported, as in the 13b scripts."""
    h = _Harness(tmp_path)
    engine = h.engine(pyproject=pyproject)

    r = h.run(engine, macp_python=macp_python)

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert h.calls("py") == [], h.calls("py")
    uv = h.calls("uv")
    assert _modules(uv) == _ENGINE_MODULES, [c["argv"] for c in uv]
    for call in uv:
        assert call["argv"][:5] == ["run", "--project", str(engine), "python", "-P"], call
        assert call["cwd"] == str(h.vault), call
        assert call["MACP_VAULT_ROOT"] == str(h.vault), call
        assert (call["PYTHONPATH"] or "").split(os.pathsep)[0] == str(engine), call
        assert call["MACP_PYTHON"] == (str(h.fakepy) if macp_python else None), call
    assert not (engine / "Worker").exists()


def test_combined_checkout_keeps_the_old_command_lines(tmp_path):
    """Engine == vault with pyproject: nothing exported, no -P / --project, the
    pre-change `uv run python -m ...` lines — even with MACP_PYTHON set."""
    h = _Harness(tmp_path)
    engine = h.engine(pyproject=True)

    r = h.run(engine, macp_python=True, vault=False)

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert h.calls("py") == [], h.calls("py")
    uv = h.calls("uv")
    assert _modules(uv) == _ENGINE_MODULES, [c["argv"] for c in uv]
    assert ["run", "python", "-m", "todo_system.orchestrator.hq_host", "--check"] in [
        c["argv"] for c in uv]
    for call in uv:
        assert call["argv"][:3] == ["run", "python", "-m"], call
        assert "-P" not in call["argv"] and "--project" not in call["argv"], call
        assert call["cwd"] == str(engine), call
        assert call["MACP_VAULT_ROOT"] is None and call["PYTHONPATH"] is None, call
    assert (engine / "Worker" / "_state" / "planner_tick_state.json").is_file()


def test_maestro_vault_dir_wins_over_macp_vault_root(tmp_path):
    h = _Harness(tmp_path)
    engine = h.engine(pyproject=False)
    other = tmp_path / "vault2"
    other.mkdir()

    r = h.run(engine, macp_python=True, MAESTRO_VAULT_DIR=str(other))

    assert r.returncode == 0, (r.stdout, r.stderr)
    py = h.calls("py")
    assert py, (r.stdout, r.stderr)
    for call in py:
        assert call["cwd"] == str(other), call
        assert call["MACP_VAULT_ROOT"] == str(other), call
    assert (other / "Worker" / "_state" / "planner_tick_state.json").is_file()
    assert not (h.vault / "Worker").exists()


# --- the script body --------------------------------------------------------------


def test_planner_tick_declares_the_installed_branch_once():
    text = (_REAL_ENGINE / "scripts" / _PLANNER).read_text(encoding="utf-8")
    assert text.count(_BRANCH_LINE) == 1
    assert 'ENGINE_PY="$MACP_PYTHON -P"' in text
    assert 'KAKAO_SEND="$ENGINE_DIR/scripts/kakao_send.sh"' in text
    assert 'bash "$KAKAO_SEND"' in text
    assert subprocess.run(["bash", "-n", str(_REAL_ENGINE / "scripts" / _PLANNER)]).returncode == 0


def test_dependency_unblock_runs_even_while_the_usage_guard_blocks(tmp_path):
    """2026-10-20: a `bypass_guard: true` follow-up sat `blocked` for over an hour
    after its upstream finished, because the task_deps step sat behind the
    usage-guard gate (exit 3 = blocked -> `exit 0` before chaining). Chaining is a
    cheap markdown rewrite with no LLM call, so it must run before that gate."""
    h = _Harness(tmp_path)
    engine = h.engine(pyproject=False)

    r = h.run(engine, macp_python=True, SEAM_USAGE_BLOCK="1")

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "[planner-tick] usage guard blocked; skip planning" in r.stdout, r.stdout
    mods = [c["argv"][c["argv"].index("-m") + 1] for c in h.calls("py") if "-m" in c["argv"]]
    assert "todo_system.task_deps" in mods, mods
    assert mods.index("todo_system.task_deps") < mods.index("todo_system.usage_guard"), mods
    assert h.calls("claude") == [], h.calls("claude")   # no planner LLM run during the hold
