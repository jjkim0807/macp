"""``python -m todo_system.engine_script`` — the one entry point for engine shell
scripts in both layouts (public split 13a, P3).

Everything runs against fake engines under ``tmp_path`` through the
``package_dir`` seam (a checkout: ``<root>/todo_system`` beside
``<root>/scripts``; a wheel: ``<site>/todo_system/_engine/scripts``), plus two
cases on this checkout that only *resolve* (dry run) and never execute a real
engine script — ``orchestrator_tick.sh`` ignores its arguments and would run a
real tick. The real ``os.execv`` path is exercised in a child interpreter with
a ``#!/bin/sh`` fixture script, because exec replaces the process.

Nothing touches the live vault. No literal from the identifier gate's pattern
may appear in this file: it sits inside the scanned tree.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import engine_script, paths

ENGINE = Path(__file__).resolve().parents[2]
_SCRUB = ("MAESTRO_VAULT_DIR", "KAKAO_VAULT_DIR", "MACP_VAULT_ROOT",
          "PYTHONPATH", "PYTHONSAFEPATH")
_HELLO = "#!/bin/sh\nprintf 'hello %s\\n' \"$*\"\n"


def _chmod(path: Path, mode: int) -> None:
    os.chmod(path, mode)


@pytest.fixture(autouse=True)
def _restore_macp_python():
    """main() exports MACP_PYTHON right before the exec (13b). With the recorder
    seam the process survives the call, so the key would leak into every later
    test module; restore whatever was there before each case."""
    before = os.environ.get("MACP_PYTHON")
    yield
    if before is None:
        os.environ.pop("MACP_PYTHON", None)
    else:
        os.environ["MACP_PYTHON"] = before


def _populate(scripts: Path) -> None:
    scripts.mkdir(parents=True)
    (scripts / "hello.sh").write_text(_HELLO, encoding="utf-8")
    _chmod(scripts / "hello.sh", 0o755)
    (scripts / "tool.py").write_text("import sys\nprint('tool', sys.argv[1:])\n", encoding="utf-8")
    _chmod(scripts / "tool.py", 0o644)
    (scripts / "README.md").write_text("docs\n", encoding="utf-8")
    (scripts / "notes.txt").write_text("not a script\n", encoding="utf-8")
    (scripts / ".hidden.sh").write_text(_HELLO, encoding="utf-8")
    (scripts / "systemd").mkdir()
    (scripts / "systemd" / "gen.py").write_text("", encoding="utf-8")
    # No suffix but executable (the bash wrapper `macp` has this shape).
    (scripts / "wrapper").write_text(_HELLO, encoding="utf-8")
    _chmod(scripts / "wrapper", 0o755)


@pytest.fixture
def installed_pkg(tmp_path) -> Path:
    """Fake wheel: <site>/todo_system/_engine/scripts (plus a decoy site/scripts)."""
    site = tmp_path / "site"
    pkg = site / "todo_system"
    _populate(pkg / "_engine" / "scripts")
    (site / "scripts").mkdir()
    (site / "scripts" / "decoy.sh").write_text(_HELLO, encoding="utf-8")
    return pkg


@pytest.fixture
def checkout_pkg(tmp_path) -> Path:
    """Fake checkout: <root>/todo_system beside <root>/scripts."""
    root = tmp_path / "checkout"
    (root / "todo_system").mkdir(parents=True)
    _populate(root / "scripts")
    return root / "todo_system"


class _Recorder:
    def __init__(self, raise_exc: Exception | None = None):
        self.calls: list[tuple[str, list[str]]] = []
        self.raise_exc = raise_exc

    def __call__(self, file, argv):
        self.calls.append((file, list(argv)))
        if self.raise_exc is not None:
            raise self.raise_exc


# --- resolution ----------------------------------------------------------------


def test_scripts_dir_follows_engine_root_in_both_layouts(installed_pkg, checkout_pkg):
    assert engine_script.scripts_dir(installed_pkg) == (installed_pkg / "_engine" / "scripts").resolve()
    assert engine_script.scripts_dir(checkout_pkg) == (checkout_pkg.parent / "scripts").resolve()
    assert engine_script.scripts_dir() == paths.engine_root() / "scripts" == ENGINE / "scripts"


def test_available_lists_executables_and_script_suffixes(installed_pkg):
    # tool.py is 0644 but listed by suffix; wrapper has no suffix but is 0755;
    # docs, the hidden file and the subdirectory are not scripts.
    assert engine_script.available(installed_pkg) == ["hello.sh", "tool.py", "wrapper"]


def test_available_never_reads_the_decoy_beside_the_package(installed_pkg):
    assert "decoy.sh" not in engine_script.available(installed_pkg)


def test_available_on_this_checkout_names_the_tick_scripts():
    names = engine_script.available()
    assert "orchestrator_tick.sh" in names
    assert "kakao_send.sh" in names
    assert "macp" in names                       # executable, no suffix
    assert "KAKAO_ALERTS.md" not in names
    assert "public_export_exclude.txt" not in names
    assert "systemd" not in names


def test_available_missing_dir_is_empty(tmp_path):
    pkg = tmp_path / "nowhere" / "todo_system"
    pkg.mkdir(parents=True)
    assert engine_script.available(pkg) == []


@pytest.mark.parametrize("bad", ["", ".", "..", "../hello.sh", "a/hello.sh", "--dry-run",
                                 "-h", "systemd", "README.md", ".hidden.sh", "missing.sh"])
def test_resolve_rejects_paths_options_and_non_scripts(installed_pkg, bad):
    assert engine_script.resolve(bad, installed_pkg) is None


def test_resolve_returns_the_absolute_script_path(installed_pkg, checkout_pkg):
    got = engine_script.resolve("hello.sh", installed_pkg)
    assert got == (installed_pkg / "_engine" / "scripts" / "hello.sh").resolve()
    assert got.is_absolute() and got.is_file()
    assert engine_script.resolve("hello.sh", checkout_pkg) == \
        (checkout_pkg.parent / "scripts" / "hello.sh").resolve()


def test_exec_argv_python_uses_the_running_interpreter(installed_pkg):
    sh = engine_script.resolve("hello.sh", installed_pkg)
    py = engine_script.resolve("tool.py", installed_pkg)
    assert engine_script.exec_argv(sh) == [str(sh)]
    assert engine_script.exec_argv(py) == [sys.executable, str(py)]


# --- main(): usage, listing, errors ---------------------------------------------


def test_main_no_args_exits_2_with_usage_and_listing(installed_pkg, capsys):
    rc = engine_script.main([], package_dir=installed_pkg)
    err = capsys.readouterr().err
    assert rc == 2
    assert "usage:" in err and "hello.sh" in err and "tool.py" in err


def test_main_list_exits_0_and_prints_names(installed_pkg, capsys):
    rc = engine_script.main(["--list"], package_dir=installed_pkg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage:" in out
    assert str(installed_pkg / "_engine" / "scripts") in out
    assert "  hello.sh\n" in out and "  tool.py\n" in out and "  wrapper\n" in out


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_main_help_exits_0(installed_pkg, capsys, flag):
    assert engine_script.main([flag], package_dir=installed_pkg) == 0
    assert "usage:" in capsys.readouterr().out


def test_main_unknown_option_exits_2(installed_pkg, capsys):
    rc = engine_script.main(["--bogus", "hello.sh"], package_dir=installed_pkg)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown option: --bogus" in err


def test_main_unknown_name_exits_2_and_lists(installed_pkg, capsys):
    rec = _Recorder()
    rc = engine_script.main(["no-such.sh", "--help"], package_dir=installed_pkg, exec_fn=rec)
    err = capsys.readouterr().err
    assert rc == 2
    assert "no such engine script: no-such.sh" in err
    assert "hello.sh" in err
    assert rec.calls == []


def test_main_path_like_name_is_refused_not_resolved(installed_pkg, capsys):
    rec = _Recorder()
    rc = engine_script.main(["../scripts/decoy.sh"], package_dir=installed_pkg, exec_fn=rec)
    assert rc == 2
    assert "no such engine script" in capsys.readouterr().err
    assert rec.calls == []


# --- main(): dry run ------------------------------------------------------------


def _parse_dry_run(out: str) -> dict[str, str]:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 3, out
    return dict(ln.split("=", 1) for ln in lines)


def test_main_dry_run_prints_script_argv_executable(installed_pkg, capsys):
    rec = _Recorder()
    rc = engine_script.main(["--dry-run", "hello.sh", "--help", "x"],
                            package_dir=installed_pkg, exec_fn=rec)
    out = capsys.readouterr().out
    assert rc == 0
    got = _parse_dry_run(out)
    path = (installed_pkg / "_engine" / "scripts" / "hello.sh").resolve()
    assert got["script"] == str(path)
    assert json.loads(got["argv"]) == [str(path), "--help", "x"]
    assert got["executable"] == "true"
    assert rec.calls == []                        # nothing executed


def test_main_dry_run_py_uses_sys_executable(installed_pkg, capsys):
    assert engine_script.main(["--dry-run", "tool.py", "a"], package_dir=installed_pkg) == 0
    got = _parse_dry_run(capsys.readouterr().out)
    path = (installed_pkg / "_engine" / "scripts" / "tool.py").resolve()
    assert json.loads(got["argv"]) == [sys.executable, str(path), "a"]
    assert got["executable"] == "false"           # 0644 and never chmod'ed in a dry run


def test_main_dry_run_reports_missing_bit_and_does_not_chmod(installed_pkg, capsys):
    path = installed_pkg / "_engine" / "scripts" / "hello.sh"
    _chmod(path, 0o644)
    assert engine_script.main(["--dry-run", "hello.sh"], package_dir=installed_pkg) == 0
    assert _parse_dry_run(capsys.readouterr().out)["executable"] == "false"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_main_options_after_name_go_to_the_script(installed_pkg, capsys):
    # Only leading options belong to the wrapper; a later --list is script argv.
    assert engine_script.main(["--dry-run", "hello.sh", "--list", "--dry-run"],
                              package_dir=installed_pkg) == 0
    got = _parse_dry_run(capsys.readouterr().out)
    assert json.loads(got["argv"])[1:] == ["--list", "--dry-run"]


# --- main(): exec --------------------------------------------------------------


def test_main_exec_passes_argv_to_exec_fn(installed_pkg):
    rec = _Recorder()
    rc = engine_script.main(["hello.sh", "a", "b"], package_dir=installed_pkg, exec_fn=rec)
    path = (installed_pkg / "_engine" / "scripts" / "hello.sh").resolve()
    assert rc == 0
    assert rec.calls == [(str(path), [str(path), "a", "b"])]


def test_main_exec_python_script_via_interpreter(installed_pkg):
    rec = _Recorder()
    assert engine_script.main(["tool.py", "z"], package_dir=installed_pkg, exec_fn=rec) == 0
    path = (installed_pkg / "_engine" / "scripts" / "tool.py").resolve()
    assert rec.calls == [(sys.executable, [sys.executable, str(path), "z"])]


def test_main_chmod_fallback_before_exec(installed_pkg):
    path = installed_pkg / "_engine" / "scripts" / "hello.sh"
    _chmod(path, 0o644)
    assert not os.access(path, os.X_OK)
    rec = _Recorder()
    assert engine_script.main(["hello.sh"], package_dir=installed_pkg, exec_fn=rec) == 0
    assert os.access(path, os.X_OK)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert len(rec.calls) == 1


def test_main_chmod_fallback_skips_python_scripts(installed_pkg):
    path = installed_pkg / "_engine" / "scripts" / "tool.py"
    rec = _Recorder()
    assert engine_script.main(["tool.py"], package_dir=installed_pkg, exec_fn=rec) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_exec_failure_exits_126(installed_pkg, capsys):
    rec = _Recorder(raise_exc=OSError(8, "Exec format error"))
    rc = engine_script.main(["hello.sh"], package_dir=installed_pkg, exec_fn=rec)
    err = capsys.readouterr().err
    assert rc == 126
    assert "cannot execute" in err and "hello.sh" in err and "Exec format error" in err


def test_default_exec_is_os_execv(installed_pkg, monkeypatch):
    # exec_fn=None looks os.execv up at call time, so a monkeypatched os.execv
    # is what runs (the production path, minus the process replacement).
    rec = _Recorder()
    monkeypatch.setattr(os, "execv", rec)
    assert engine_script.main(["hello.sh", "q"], package_dir=installed_pkg) == 0
    path = (installed_pkg / "_engine" / "scripts" / "hello.sh").resolve()
    assert rec.calls == [(str(path), [str(path), "q"])]


# --- child interpreter: the real execv, and `python -m` on this checkout -------

_EXEC_SNIPPET = (
    "import sys\n"
    "from pathlib import Path\n"
    "from todo_system import engine_script\n"
    "sys.exit(engine_script.main(sys.argv[2:], package_dir=Path(sys.argv[1])))\n"
)


def _child(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["PYTHONPATH"] = str(ENGINE)
    return subprocess.run([sys.executable, *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=60)


def test_main_real_execv_runs_fake_script(installed_pkg, tmp_path):
    proc = _child(["-c", _EXEC_SNIPPET, str(installed_pkg), "hello.sh", "a", "b"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "hello a b"


def test_main_real_execv_propagates_exit_status(installed_pkg, tmp_path):
    path = installed_pkg / "_engine" / "scripts" / "fail.sh"
    path.write_text("#!/bin/sh\necho failing >&2\nexit 7\n", encoding="utf-8")
    _chmod(path, 0o755)
    proc = _child(["-c", _EXEC_SNIPPET, str(installed_pkg), "fail.sh"], cwd=tmp_path)
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert "failing" in proc.stderr


def test_main_real_execv_restores_lost_exec_bit(installed_pkg, tmp_path):
    path = installed_pkg / "_engine" / "scripts" / "hello.sh"
    _chmod(path, 0o644)
    proc = _child(["-c", _EXEC_SNIPPET, str(installed_pkg), "hello.sh", "bit"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "hello bit"
    assert os.access(path, os.X_OK)


def test_main_real_execv_python_script_sees_argv(installed_pkg, tmp_path):
    proc = _child(["-c", _EXEC_SNIPPET, str(installed_pkg), "tool.py", "x", "y"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "tool ['x', 'y']"


def test_module_runs_with_dash_m_in_dev_checkout():
    # Resolution only: orchestrator_tick.sh ignores argv, so --help would run a
    # real tick. The dry run proves the checkout layout resolves and no tick runs.
    proc = _child(["-m", "todo_system.engine_script", "--dry-run", "orchestrator_tick.sh", "--help"],
                  cwd=ENGINE)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = _parse_dry_run(proc.stdout)
    path = ENGINE / "scripts" / "orchestrator_tick.sh"
    assert Path(got["script"]) == path
    assert json.loads(got["argv"]) == [str(path), "--help"]
    assert got["executable"] == "true"


def test_module_unknown_name_with_dash_m_exits_2(tmp_path):
    proc = _child(["-m", "todo_system.engine_script", "no-such-script.sh"], cwd=ENGINE)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "no such engine script: no-such-script.sh" in proc.stderr
    assert "orchestrator_tick.sh" in proc.stderr
