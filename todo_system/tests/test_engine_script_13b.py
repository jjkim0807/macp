"""``engine_script`` hands the caller's interpreter down as ``MACP_PYTHON``
(public split 13b, P3 — the daemon shell scripts' installed-wheel seam).

In an installed wheel ``$ENGINE_DIR`` (``todo_system/_engine``) ships no
``pyproject.toml`` for ``uv run --project`` and is not a package for
``PYTHONPATH``, so the shell scripts cannot reach the engine the checkout way.
``main()`` therefore sets ``MACP_PYTHON`` to ``sys.executable`` right before the
exec — never overwriting a caller's value, never on a dry run, never on a usage
or unknown-name exit — and ``os.execv`` hands ``os.environ`` down. The scripts
read it only when no pyproject sits beside them.

The recorder-seam cases mirror ``test_engine_script.py`` (fixtures duplicated:
``todo_system/tests`` is not a package, so a sibling import would lean on
pytest's import mode). Because ``main()`` touches ``os.environ`` and the recorder
seam survives the call, every case restores the variable afterwards. The real
``os.execv`` path runs in a child interpreter with a ``#!/bin/sh`` script that
prints the variable. Nothing touches the live vault, and no engine shell script
is executed. No literal from the identifier gate's pattern may appear in this
file: it sits inside the scanned tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import engine_script

ENGINE = Path(__file__).resolve().parents[2]
_SCRUB = ("WORKER_HOST", "MAESTRO_VAULT_DIR", "KAKAO_VAULT_DIR", "MACP_VAULT_ROOT",
          "PYTHONPATH", "PYTHONSAFEPATH", "MACP_PYTHON")
_HELLO = "#!/bin/sh\nprintf 'hello %s\\n' \"$*\"\n"
# Prints the variable the daemon scripts will read, or "unset".
_ENV_SH = "#!/bin/sh\nprintf '%s\\n' \"${MACP_PYTHON:-unset}\"\n"
_EXEC_SNIPPET = (
    "import sys\n"
    "from pathlib import Path\n"
    "from todo_system import engine_script\n"
    "sys.exit(engine_script.main(sys.argv[2:], package_dir=Path(sys.argv[1])))\n"
)


@pytest.fixture(autouse=True)
def _restore_macp_python():
    """main() exports MACP_PYTHON right before the exec. With the recorder seam
    the process survives the call, so the key would leak into every later test
    module; restore whatever was there before each case."""
    before = os.environ.get("MACP_PYTHON")
    yield
    if before is None:
        os.environ.pop("MACP_PYTHON", None)
    else:
        os.environ["MACP_PYTHON"] = before


def _populate(scripts: Path) -> None:
    scripts.mkdir(parents=True)
    (scripts / "hello.sh").write_text(_HELLO, encoding="utf-8")
    os.chmod(scripts / "hello.sh", 0o755)
    (scripts / "env.sh").write_text(_ENV_SH, encoding="utf-8")
    os.chmod(scripts / "env.sh", 0o755)
    (scripts / "tool.py").write_text("import sys\nprint('tool', sys.argv[1:])\n", encoding="utf-8")
    os.chmod(scripts / "tool.py", 0o644)


@pytest.fixture
def installed_pkg(tmp_path) -> Path:
    """Fake wheel: <site>/todo_system/_engine/scripts."""
    pkg = tmp_path / "site" / "todo_system"
    _populate(pkg / "_engine" / "scripts")
    return pkg


class _Recorder:
    """exec_fn seam: records argv and the value of MACP_PYTHON *at call time*,
    which is what the exec'd script would inherit."""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []
        self.seen: list[str | None] = []

    def __call__(self, file, argv):
        self.calls.append((file, list(argv)))
        self.seen.append(os.environ.get("MACP_PYTHON"))


# --- recorder seam: set, not overwritten, not on dry run / usage -----------------


def test_exec_sets_macp_python_when_unset(installed_pkg, monkeypatch):
    monkeypatch.delenv("MACP_PYTHON", raising=False)
    rec = _Recorder()
    rc = engine_script.main(["hello.sh", "a"], package_dir=installed_pkg, exec_fn=rec)
    path = (installed_pkg / "_engine" / "scripts" / "hello.sh").resolve()
    assert rc == 0
    assert os.environ["MACP_PYTHON"] == sys.executable
    assert rec.calls == [(str(path), [str(path), "a"])]       # argv unchanged by the seam


def test_exec_sets_env_before_calling_exec(installed_pkg, monkeypatch):
    monkeypatch.delenv("MACP_PYTHON", raising=False)
    rec = _Recorder()
    assert engine_script.main(["hello.sh"], package_dir=installed_pkg, exec_fn=rec) == 0
    assert rec.seen == [sys.executable]                        # visible *at* the exec


def test_exec_keeps_callers_macp_python(installed_pkg, monkeypatch):
    monkeypatch.setenv("MACP_PYTHON", "/opt/custom/bin/python")
    rec = _Recorder()
    assert engine_script.main(["hello.sh"], package_dir=installed_pkg, exec_fn=rec) == 0
    assert os.environ["MACP_PYTHON"] == "/opt/custom/bin/python"
    assert rec.seen == ["/opt/custom/bin/python"]


def test_exec_empty_callers_value_is_kept_as_is(installed_pkg, monkeypatch):
    # setdefault semantics: an explicitly empty value is the caller's choice and
    # means "fall back to the checkout path" in the scripts (`-n` test fails).
    monkeypatch.setenv("MACP_PYTHON", "")
    rec = _Recorder()
    assert engine_script.main(["hello.sh"], package_dir=installed_pkg, exec_fn=rec) == 0
    assert os.environ["MACP_PYTHON"] == ""


def test_python_script_exec_also_sets_macp_python(installed_pkg, monkeypatch):
    monkeypatch.delenv("MACP_PYTHON", raising=False)
    rec = _Recorder()
    assert engine_script.main(["tool.py", "z"], package_dir=installed_pkg, exec_fn=rec) == 0
    path = (installed_pkg / "_engine" / "scripts" / "tool.py").resolve()
    assert rec.calls == [(sys.executable, [sys.executable, str(path), "z"])]
    assert rec.seen == [sys.executable]


def test_dry_run_sets_nothing_and_prints_three_lines(installed_pkg, monkeypatch, capsys):
    monkeypatch.delenv("MACP_PYTHON", raising=False)
    rc = engine_script.main(["--dry-run", "hello.sh", "x"], package_dir=installed_pkg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "MACP_PYTHON" not in os.environ
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 3, out                                # no extra "python=" line
    assert [ln.split("=", 1)[0] for ln in lines] == ["script", "argv", "executable"]


def test_unknown_name_and_usage_set_nothing(installed_pkg, monkeypatch, capsys):
    monkeypatch.delenv("MACP_PYTHON", raising=False)
    rec = _Recorder()
    assert engine_script.main(["no-such.sh"], package_dir=installed_pkg, exec_fn=rec) == 2
    assert engine_script.main([], package_dir=installed_pkg, exec_fn=rec) == 2
    assert engine_script.main(["--list"], package_dir=installed_pkg, exec_fn=rec) == 0
    capsys.readouterr()
    assert "MACP_PYTHON" not in os.environ
    assert rec.calls == []


# --- child interpreter: the real os.execv hands the env down --------------------


def _child(args: list[str], cwd: Path, **env_extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["PYTHONPATH"] = str(ENGINE)
    env.update(env_extra)
    return subprocess.run([sys.executable, *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=60)


def test_real_execv_child_script_sees_callers_interpreter(installed_pkg, tmp_path):
    proc = _child(["-c", _EXEC_SNIPPET, str(installed_pkg), "env.sh"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == sys.executable


def test_real_execv_child_keeps_preset_value(installed_pkg, tmp_path):
    proc = _child(["-c", _EXEC_SNIPPET, str(installed_pkg), "env.sh"], cwd=tmp_path,
                  MACP_PYTHON="/x/y/python")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "/x/y/python"


def test_real_execv_script_argv_is_untouched(installed_pkg, tmp_path):
    proc = _child(["-c", _EXEC_SNIPPET, str(installed_pkg), "hello.sh", "a", "b"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "hello a b"
