"""``paths.engine_root()`` — the one resolver for engine-shipped non-Python files.

Public split 12 (P2). Until this leaf every module that needed ``scripts/`` or
``.claude/commands/`` recomputed the engine root on its own as
``Path(__file__).resolve().parents[N]`` — ``_ENGINE_ROOT`` in the worker /
orchestrator modules, ``_INSTALL_DIR`` in the ``scripts/*.py`` daemons. That is
right for a checkout and wrong for a wheel, where the package sits in
``site-packages`` with no ``scripts/`` beside it.

Two layouts, pinned through the ``package_dir`` seam:

* development checkout — ``<root>/todo_system`` beside ``<root>/scripts`` →
  ``<root>``, byte-identical to the legacy ``parents[N]`` constants;
* installed wheel — no ``scripts/`` beside the package → ``todo_system/_engine``,
  where ``pyproject.toml`` force-includes ``scripts/`` and ``.claude/commands/``.

The convergence tests (second half) pin that the four engine modules and the
seven exported engine scripts define their root *through* the resolver, so the
two layouts cannot drift apart again. The ``scripts/*.py`` daemons bootstrap
``sys.path`` from their own location *before* ``todo_system`` is importable, so
their first ``_INSTALL_DIR`` line cannot be the resolver; the ratchet pins the
shape "bootstrap, then re-define through ``paths.engine_root()``" instead.

The package conftest swaps ``worker.KAKAO_SEND_SCRIPT`` / ``human.KAKAO_SCRIPT``
in-process for every test, so the derived-constant checks run in a child
interpreter (same reason as ``test_worker_engine_root``).

No literal from the identifier gate's pattern may appear in this file: it sits
inside the scanned tree.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import paths

ENGINE = Path(__file__).resolve().parents[2]


# --- the resolver itself -------------------------------------------------------


def test_dev_checkout_is_byte_identical_to_legacy_parents():
    root = paths.engine_root()
    # The three spellings every module used before the resolver existed.
    assert root == Path(paths.__file__).resolve().parents[1]
    assert root == Path(paths.__file__).resolve().parent.parent
    assert root == ENGINE
    assert (root / "scripts").is_dir()
    assert (root / ".claude" / "commands").is_dir()
    assert (root / "todo_system" / "paths.py").is_file()


def test_engine_root_ignores_vault_redirect(tmp_path, monkeypatch):
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path))
    assert paths.engine_root() == Path(paths.__file__).resolve().parents[1]


def test_seam_dev_layout(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "todo_system").mkdir(parents=True)
    (checkout / "scripts").mkdir()
    assert paths.engine_root(package_dir=checkout / "todo_system") == checkout.resolve()


def test_seam_installed_layout(tmp_path):
    site = tmp_path / "site"
    pkg = site / "todo_system"
    (pkg / "_engine" / "scripts").mkdir(parents=True)
    assert not (site / "scripts").exists()
    assert paths.engine_root(package_dir=pkg) == (pkg / "_engine").resolve()


def test_seam_without_scripts_or_engine_dir_still_names_engine_dir(tmp_path):
    pkg = tmp_path / "site" / "todo_system"
    pkg.mkdir(parents=True)
    # Documented fallback: never the bare site dir, always the _engine slot.
    assert paths.engine_root(package_dir=pkg) == (pkg / "_engine").resolve()


def test_seam_scripts_file_not_dir_is_installed(tmp_path):
    site = tmp_path / "site"
    pkg = site / "todo_system"
    pkg.mkdir(parents=True)
    (site / "scripts").write_text("", encoding="utf-8")
    # A stray *file* named scripts is not a checkout: is_dir(), not exists().
    assert paths.engine_root(package_dir=pkg) == (pkg / "_engine").resolve()


def test_seam_relative_package_dir_is_resolved(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / "todo_system").mkdir(parents=True)
    (checkout / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    got = paths.engine_root(package_dir=Path("checkout") / "todo_system")
    assert got.is_absolute()
    assert got == checkout.resolve()


# --- convergence: every engine-root definition goes through the resolver ------

MODULE_SOURCES = {
    "worker": ENGINE / "todo_system" / "worker.py",
    "orchestrator/human": ENGINE / "todo_system" / "orchestrator" / "human.py",
    "orchestrator/stages": ENGINE / "todo_system" / "orchestrator" / "stages.py",
    "orchestrator/beta_publish": ENGINE / "todo_system" / "orchestrator" / "beta_publish.py",
}
# The exported engine scripts (all in the public export set) that bootstrap
# sys.path from their own location, import todo_system, then re-define
# _INSTALL_DIR through the resolver.
ENGINE_SCRIPTS = (
    "health_report.py",
    "macp_dashboard.py",
    "macp_monitor.py",
    "kakao_digest_send.py",
    "claude_auth_watch.py",
    "claude_model_autoswitch.py",
    "export_db_snapshot.py",
)
# Env that would redirect the child away from the layout under test.
_SCRUB = ("WORKER_HOST", "MAESTRO_VAULT_DIR", "KAKAO_VAULT_DIR", "MACP_VAULT_ROOT", "PYTHONPATH")


def _vault_skeleton(root: Path) -> Path:
    """Marker + Todo/ + Worker/, and no engine files (no todo_system/, scripts/)."""
    (root / ".macp").mkdir(parents=True)
    (root / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (root / "Todo").mkdir()
    (root / "Worker").mkdir()
    return root


def _definitions(src: str, name: str) -> list[str]:
    return [m.group(0) for m in re.finditer(rf"^{name}\s*=.*$", src, re.M)]


def _is_resolver_definition(line: str, name: str) -> bool:
    return re.fullmatch(rf"{name}\s*=\s*paths\.engine_root\(\)\s*(#.*)?", line) is not None


def _child(args: list[str], cwd: Path, vault: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["MACP_VAULT_ROOT"] = str(vault)
    env["PYTHONPATH"] = str(ENGINE)
    proc = subprocess.run(
        [sys.executable, *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


@pytest.mark.parametrize("module", sorted(MODULE_SOURCES))
def test_module_engine_root_definition_is_the_resolver(module):
    src = MODULE_SOURCES[module].read_text(encoding="utf-8")
    defs = _definitions(src, "_ENGINE_ROOT")
    assert len(defs) == 1, f"{module}: expected exactly one _ENGINE_ROOT definition, got {defs}"
    assert _is_resolver_definition(defs[0], "_ENGINE_ROOT"), f"{module}: {defs[0]}"


@pytest.mark.parametrize("script", ENGINE_SCRIPTS)
def test_script_install_dir_definition_is_the_resolver(script):
    src = (ENGINE / "scripts" / script).read_text(encoding="utf-8")
    defs = _definitions(src, "_INSTALL_DIR")
    assert len(defs) == 2, f"{script}: expected bootstrap + resolver definitions, got {defs}"
    bootstrap, resolver = defs
    assert re.match(r"_INSTALL_DIR\s*=\s*Path\(__file__\)\.resolve\(\)\.parents\[1\]", bootstrap), (
        f"{script}: first definition is not the sys.path bootstrap: {bootstrap}")
    assert _is_resolver_definition(resolver, "_INSTALL_DIR"), (
        f"{script}: last definition is not paths.engine_root(): {resolver}")
    # The bootstrap value is what goes on sys.path (not a recomputed parents[1]),
    # so the first definition is used before the resolver redefines the name.
    between = src[src.index(bootstrap) + len(bootstrap):src.index(resolver)]
    assert "sys.path.insert(0, str(_INSTALL_DIR))" in between, script
    assert "from todo_system import paths" in between, script


def test_engine_modules_converge_on_engine_root_in_process():
    from todo_system import worker
    from todo_system.orchestrator import beta_publish, human, stages

    root = paths.engine_root()
    assert worker._ENGINE_ROOT == root
    assert human._ENGINE_ROOT == root
    assert stages._ENGINE_ROOT == root
    assert beta_publish._ENGINE_ROOT == root
    assert worker.COMMANDS_DIR == root / "todo_system" / "prompts"
    assert stages.COMMANDS_DIR == root / "todo_system" / "prompts"
    assert worker.HEALTH_REPORT_SCRIPT == root / "scripts" / "health_report.py"
    assert beta_publish.LOCK_DIR == root / "Worker" / "_state" / "beta_locks"


_MODULES_PROBE = (
    "import json\n"
    "from todo_system import paths, worker\n"
    "from todo_system.orchestrator import beta_publish, human, stages\n"
    "vals = {\n"
    "    'root': paths.engine_root(),\n"
    "    'worker._ENGINE_ROOT': worker._ENGINE_ROOT,\n"
    "    'human._ENGINE_ROOT': human._ENGINE_ROOT,\n"
    "    'stages._ENGINE_ROOT': stages._ENGINE_ROOT,\n"
    "    'beta_publish._ENGINE_ROOT': beta_publish._ENGINE_ROOT,\n"
    "    'worker.COMMANDS_DIR': worker.COMMANDS_DIR,\n"
    "    'worker.KAKAO_SEND_SCRIPT': worker.KAKAO_SEND_SCRIPT,\n"
    "    'worker.HEALTH_REPORT_SCRIPT': worker.HEALTH_REPORT_SCRIPT,\n"
    "    'human.KAKAO_SCRIPT': human.KAKAO_SCRIPT,\n"
    "    'stages.COMMANDS_DIR': stages.COMMANDS_DIR,\n"
    "    'beta_publish.LOCK_DIR': beta_publish.LOCK_DIR,\n"
    "}\n"
    "print(json.dumps({k: str(v) for k, v in vals.items()}))\n"
)


def test_engine_modules_converge_on_engine_root_in_a_fresh_interpreter(tmp_path):
    vault = _vault_skeleton(tmp_path / "vault")
    out = _child(["-c", _MODULES_PROBE], cwd=tmp_path, vault=vault)
    got = {k: Path(v) for k, v in json.loads(out.splitlines()[-1]).items()}
    root = got.pop("root")
    assert root == paths.engine_root() == ENGINE
    assert got == {
        "worker._ENGINE_ROOT": root,
        "human._ENGINE_ROOT": root,
        "stages._ENGINE_ROOT": root,
        "beta_publish._ENGINE_ROOT": root,
        "worker.COMMANDS_DIR": root / "todo_system" / "prompts",
        "worker.KAKAO_SEND_SCRIPT": root / "scripts" / "kakao_send.sh",
        "worker.HEALTH_REPORT_SCRIPT": root / "scripts" / "health_report.py",
        "human.KAKAO_SCRIPT": root / "scripts" / "kakao_send.sh",
        "stages.COMMANDS_DIR": root / "todo_system" / "prompts",
        "beta_publish.LOCK_DIR": root / "Worker" / "_state" / "beta_locks",
    }


_SCRIPT_PROBE = (
    "import importlib.util, sys\n"
    "from todo_system import paths\n"
    "spec = importlib.util.spec_from_file_location('engine_script_under_test', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "sys.modules['engine_script_under_test'] = mod\n"
    "spec.loader.exec_module(mod)\n"
    "print(mod._INSTALL_DIR)\n"
    "print(paths.engine_root())\n"
)


@pytest.mark.parametrize("script", ENGINE_SCRIPTS)
def test_script_install_dir_is_engine_root_at_runtime(tmp_path, script):
    vault = _vault_skeleton(tmp_path / "vault")
    out = _child(["-c", _SCRIPT_PROBE, str(ENGINE / "scripts" / script)], cwd=tmp_path, vault=vault)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) >= 2, out
    install_dir, root = (Path(p) for p in lines[-2:])
    assert install_dir == root == paths.engine_root()
    assert install_dir != vault.resolve()


def test_health_report_install_dir_is_engine_root_in_process(tmp_path, monkeypatch):
    vault = _vault_skeleton(tmp_path / "vault")
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    name = "health_report_engine_root_12"
    spec = importlib.util.spec_from_file_location(name, ENGINE / "scripts" / "health_report.py")
    mod = importlib.util.module_from_spec(spec)
    # Registration precedes exec (@dataclass resolves via sys.modules); a
    # unique name keeps test_health_report's module untouched.
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    assert mod._INSTALL_DIR == paths.engine_root()
    assert Path(mod.VAULT).resolve() == vault.resolve()
    assert mod._INSTALL_DIR != Path(mod.VAULT)
    assert mod.KAKAO_SEND_SH == paths.engine_root() / "scripts" / "kakao_send.sh"
