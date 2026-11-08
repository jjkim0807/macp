"""Layout detection for the installed wheel (public split 13a, P3 first half).

Leaf 12 made ``paths.engine_root()`` tell a checkout from a wheel by whether a
``scripts/`` directory sits beside the package. That test is one-sided: any
site-packages that happens to carry a top-level ``scripts/`` directory (a
third-party distribution, a stray install artefact) flips an installed wheel
back to "checkout", and every engine file is then looked up under
site-packages. The resolver now keys on the marker the wheel itself ships —
``todo_system/_engine/``, the force-include target in ``pyproject.toml`` — and
only falls back to the sibling-``scripts/`` test when that marker is absent.

Two helpers come with it:

* ``paths.is_installed()`` — the layout predicate (marker present).
* ``paths.vault_search_start()`` — the anchor the engine modules hand to
  ``vault_root(start=...)``. In a checkout it is the engine root, byte-identical
  to the ``Path(__file__).resolve().parents[N]`` every module used to spell; in
  a wheel it is ``None``, so the resolver's documented order (env, then the
  cwd-upward marker search) applies instead of anchoring on site-packages,
  where no vault marker can ever be.

The dev-checkout cases run against this repo; the installed cases use fake
layouts under ``tmp_path`` through the ``package_dir`` seam. The combined-layout
cases (a checkout that is also the vault, marker at its root) run against a
throwaway tree: in-process through the ``combined_layout`` fixture with
``paths.__file__`` pointed into it, and in a fresh interpreter against a real
copy of the engine tree with the marker added. Nothing touches the live vault. No literal from the identifier gate's pattern may appear in
this file: it sits inside the scanned tree.
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

from todo_system import paths

ENGINE = Path(__file__).resolve().parents[2]

# Byte-identity through vault_root() needs the vault marker at the checkout root
# (the combined layout); this checkout has none, so those cases build one — the
# combined_layout fixture (tests/conftest.py) in-process, combined_copy below for
# the fresh-interpreter probes.


def _install_engine_at(m, root):
    """Inside a monkeypatch context ``m``: make ``paths`` resolve as if the engine
    were checked out at ``root`` (``root/todo_system/paths.py``)."""
    m.setattr(paths, "__file__", str(root / "todo_system" / "paths.py"))


@pytest.fixture(scope="module")
def combined_copy(tmp_path_factory) -> Path:
    """A real combined layout: a copy of the engine tree (the sdist set minus the
    tests) with a vault marker at its root, so every module ``__file__`` really
    lives under it. A copy, not a symlink: ``resolve()`` would lead back here."""
    root = tmp_path_factory.mktemp("combined-copy").resolve()
    shutil.copytree(ENGINE / "todo_system", root / "todo_system",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", "_engine"))
    shutil.copytree(ENGINE / "scripts", root / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if (ENGINE / ".claude" / "commands").is_dir():
        shutil.copytree(ENGINE / ".claude" / "commands", root / ".claude" / "commands")
    (root / ".macp").mkdir()
    (root / ".macp" / "config.toml").write_text("", encoding="utf-8")
    return root


def _fake_checkout(root: Path) -> Path:
    """<root>/todo_system beside <root>/scripts, no marker: the dev layout."""
    (root / "todo_system").mkdir(parents=True)
    (root / "scripts").mkdir()
    return root / "todo_system"


def _fake_install(site: Path) -> Path:
    """<site>/todo_system/_engine/scripts: the wheel layout (force-include)."""
    pkg = site / "todo_system"
    (pkg / "_engine" / "scripts").mkdir(parents=True)
    return pkg


# --- engine_root(): the package-internal marker decides ------------------------


def test_engine_root_prefers_engine_marker_over_sibling_scripts(tmp_path):
    # AC6: a site-packages that also carries a top-level scripts/ directory.
    site = tmp_path / "site"
    pkg = _fake_install(site)
    (site / "scripts").mkdir()
    assert (site / "scripts").is_dir()
    assert paths.engine_root(package_dir=pkg) == (pkg / "_engine").resolve()


def test_engine_root_marker_only_wins(tmp_path):
    pkg = _fake_install(tmp_path / "site")
    assert not (tmp_path / "site" / "scripts").exists()
    assert paths.engine_root(package_dir=pkg) == (pkg / "_engine").resolve()


def test_engine_root_dev_checkout_without_marker_is_the_checkout(tmp_path):
    pkg = _fake_checkout(tmp_path / "checkout")
    assert not (pkg / "_engine").exists()
    assert paths.engine_root(package_dir=pkg) == (tmp_path / "checkout").resolve()


def test_engine_root_marker_must_be_a_directory(tmp_path):
    # A stray *file* named _engine is not the wheel marker (mirrors the
    # scripts-file case in test_paths_engine_root): the sibling test still runs.
    checkout = tmp_path / "checkout"
    pkg = _fake_checkout(checkout)
    (pkg / "_engine").write_text("", encoding="utf-8")
    assert paths.engine_root(package_dir=pkg) == checkout.resolve()


def test_this_checkout_carries_no_marker():
    # The .gitignore entry keeps a stray copy out of the tree; if one ever
    # appears the resolver would call this checkout a wheel.
    assert not (ENGINE / "todo_system" / "_engine").exists()
    assert paths.engine_root() == ENGINE


# --- is_installed() ------------------------------------------------------------


def test_is_installed_both_layouts(tmp_path):
    assert paths.is_installed(package_dir=_fake_checkout(tmp_path / "checkout")) is False
    assert paths.is_installed(package_dir=_fake_install(tmp_path / "site")) is True
    assert paths.is_installed() is False  # this repo


def test_is_installed_marker_file_is_not_a_marker(tmp_path):
    pkg = _fake_checkout(tmp_path / "checkout")
    (pkg / "_engine").write_text("", encoding="utf-8")
    assert paths.is_installed(package_dir=pkg) is False


def test_is_installed_ignores_sibling_scripts(tmp_path):
    site = tmp_path / "site"
    pkg = _fake_install(site)
    (site / "scripts").mkdir()
    assert paths.is_installed(package_dir=pkg) is True


def test_is_installed_relative_package_dir_is_resolved(tmp_path, monkeypatch):
    _fake_install(tmp_path / "site")
    monkeypatch.chdir(tmp_path)
    assert paths.is_installed(package_dir=Path("site") / "todo_system") is True


# --- vault_search_start() --------------------------------------------------------


def test_vault_search_start_dev_checkout_is_engine_root():
    got = paths.vault_search_start()
    assert got == paths.engine_root()
    assert got == Path(paths.__file__).resolve().parents[1]
    assert got == ENGINE


def test_vault_search_start_fake_checkout_is_its_root(tmp_path):
    pkg = _fake_checkout(tmp_path / "checkout")
    assert paths.vault_search_start(package_dir=pkg) == (tmp_path / "checkout").resolve()


def test_vault_search_start_installed_is_none(tmp_path):
    site = tmp_path / "site"
    pkg = _fake_install(site)
    assert paths.vault_search_start(package_dir=pkg) is None
    # ... even with a decoy scripts/ beside the package.
    (site / "scripts").mkdir()
    assert paths.vault_search_start(package_dir=pkg) is None


def test_vault_search_start_ignores_vault_redirect(tmp_path, monkeypatch):
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path))
    assert paths.vault_search_start() == ENGINE
    monkeypatch.chdir(tmp_path)
    assert paths.vault_search_start() == ENGINE


def test_vault_root_via_search_start_is_byte_identical_in_checkout(monkeypatch, combined_layout):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    _install_engine_at(monkeypatch, combined_layout)
    legacy = paths.vault_root(start=Path(paths.__file__).resolve().parents[1])
    assert paths.vault_root(start=paths.vault_search_start()) == legacy == combined_layout
    assert paths.engine_root() == combined_layout


def test_vault_root_via_search_start_pins_engine_not_cwd(tmp_path, monkeypatch, combined_layout):
    # The reason start= is passed at all: a bogus marker in the cwd must not win
    # in a checkout, where the anchor is the engine root.
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    _install_engine_at(monkeypatch, combined_layout)
    got = paths.vault_root(start=paths.vault_search_start())
    assert got == combined_layout
    assert got == paths.engine_root()
    assert got != Path("/bogus")


def test_vault_root_with_none_start_searches_from_cwd(tmp_path, monkeypatch):
    # What an installed wheel gets: start=None means the cwd-upward search, so a
    # process started inside a vault resolves it without any env.
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (vault / "Todo").mkdir()
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    monkeypatch.chdir(vault / "Todo")
    assert paths.vault_root(start=None) == vault.resolve()


def test_gitignore_blocks_a_stray_engine_marker_in_the_checkout():
    gitignore = ENGINE / ".gitignore"
    if not gitignore.is_file():
        pytest.skip("engine-only tree without a .gitignore")
    lines = [ln.strip() for ln in gitignore.read_text(encoding="utf-8").splitlines()]
    assert "todo_system/_engine/" in lines
    proc = subprocess.run(
        ["git", "-C", str(ENGINE), "check-ignore", "-q", "todo_system/_engine/scripts/x.sh"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- fresh-interpreter: the helpers never depend on in-process state ----------

_SCRUB = ("WORKER_HOST", "MAESTRO_VAULT_DIR", "KAKAO_VAULT_DIR", "MACP_VAULT_ROOT", "PYTHONPATH")

_PROBE = (
    "import json\n"
    "from todo_system import paths\n"
    "print(json.dumps({\n"
    "    'installed': paths.is_installed(),\n"
    "    'engine_root': str(paths.engine_root()),\n"
    "    'search_start': str(paths.vault_search_start()),\n"
    "}))\n"
)


def test_helpers_in_a_fresh_interpreter_with_redirected_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["MACP_VAULT_ROOT"] = str(vault)
    env["PYTHONPATH"] = str(ENGINE)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=str(tmp_path), env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got == {"installed": False, "engine_root": str(ENGINE), "search_start": str(ENGINE)}


def test_resolver_source_keys_on_the_marker_before_sibling_scripts():
    # Order ratchet: the marker test must come before the sibling-scripts test
    # inside engine_root(), or AC6 silently regresses to leaf 12's one-sided rule.
    src = (ENGINE / "todo_system" / "paths.py").read_text(encoding="utf-8")
    body = src[src.index("def engine_root("):]
    body = body[:body.index("\ndef ", 1)]
    marker = re.search(r'"_engine"', body)
    sibling = re.search(r'"scripts"', body)
    assert marker and sibling, body
    assert marker.start() < sibling.start(), body


# --- Phase 3: every import-time vault_root(start=...) goes through the helper --

# (module path, attribute) of every engine module that resolves a vault root into
# a constant at import. The per-call sites (kakao_format._vault_root,
# Orchestrator._task_title, trace.render) are covered by the source ratchet only.
_MODULE_ROOTS = (
    ("todo_system.worker", "ROOT"),
    ("todo_system.orchestrator.human", "_VAULT_DATA_ROOT"),
    ("todo_system.orchestrator.stages", "_VAULT_DATA_ROOT"),
    ("todo_system.orchestrator.beta_publish", "_VAULT_DATA_ROOT"),
    ("todo_system.orchestrator.metrics", "_ROOT"),
    ("todo_system.orchestrator.discovery", "_ROOT"),
    ("todo_system.orchestrator.followups", "_ROOT"),
    ("todo_system.orchestrator.jobs", "REPO_ROOT"),
    ("todo_system.orchestrator.model_select", "_ROOT"),
    ("todo_system.orchestrator.planner_mode", "_ROOT"),
    ("todo_system.macp.deps", "VAULT_ROOT"),
    ("todo_system.session_end", "ROOT"),
    ("todo_system.usage_guard", "ROOT"),
    ("todo_system.task_export", "DEFAULT_VAULT_ROOT"),
    ("todo_system.advote_bridge", "_VAULT_ROOT"),
)
# Source files whose vault anchor used to be the engine (parents[N] / _INSTALL_DIR).
_ANCHORED_SOURCES = (
    "todo_system/worker.py",
    "todo_system/orchestrator/human.py",
    "todo_system/orchestrator/stages.py",
    "todo_system/orchestrator/beta_publish.py",
    "todo_system/orchestrator/metrics.py",
    "todo_system/orchestrator/discovery.py",
    "todo_system/orchestrator/followups.py",
    "todo_system/orchestrator/jobs.py",
    "todo_system/orchestrator/model_select.py",
    "todo_system/orchestrator/planner_mode.py",
    "todo_system/orchestrator/orchestrator.py",
    "todo_system/orchestrator/trace.py",
    "todo_system/macp/deps.py",
    "todo_system/session_end.py",
    "todo_system/usage_guard.py",
    "todo_system/task_export.py",
    "todo_system/kakao_format.py",
    "scripts/health_report.py",
    "scripts/macp_dashboard.py",
    "scripts/kakao_digest_send.py",
    "scripts/macp_monitor.py",
    "scripts/claude_auth_watch.py",
    "scripts/claude_model_autoswitch.py",
    "scripts/export_db_snapshot.py",
    # claude_remote_control_watchdog.py and claude_upgrade.py were here until the
    # 2026-10-19 engine split moved both to the vault's ops/ — they are no longer
    # part of this tree, so there is nothing here left to ratchet.
    "todo_system/advote_bridge.py",
)
# Scripts that looked config keys up at the engine instead of the vault.
_CONFIG_KEY_SOURCES = (
    "scripts/health_report.py",
    "scripts/macp_monitor.py",
    "scripts/claude_auth_watch.py",
)


@pytest.mark.parametrize("source", _ANCHORED_SOURCES)
def test_no_engine_anchored_vault_root_left(source):
    # Every listed source lives in this tree; a move must update the list, not skip.
    assert (ENGINE / source).is_file(), source
    src = (ENGINE / source).read_text(encoding="utf-8")
    assert re.search(r"vault_root\(start=(Path\(__file__\)|_INSTALL_DIR)", src) is None, source
    assert "paths.vault_root(start=paths.vault_search_start())" in src, source


@pytest.mark.parametrize("source", _CONFIG_KEY_SOURCES)
def test_config_key_lookups_anchor_at_the_vault(source):
    src = (ENGINE / source).read_text(encoding="utf-8")
    assert re.search(r"(repo_slug|label_prefix)\(start=_INSTALL_DIR\)", src) is None, source


def test_module_vault_constants_converge_in_process():
    import importlib
    expected = paths.vault_root(start=paths.vault_search_start())
    got = {f"{m}.{a}": getattr(importlib.import_module(m), a) for m, a in _MODULE_ROOTS}
    assert all(v == expected for v in got.values()), {k: str(v) for k, v in got.items()}


_PROBE_MODULES = (
    "import json\n"
    "import importlib\n"
    "from todo_system import paths\n"
    "from todo_system.orchestrator import beta_publish\n"
    f"roots = {list(_MODULE_ROOTS)!r}\n"
    "vals = {f'{m}.{a}': str(getattr(importlib.import_module(m), a)) for m, a in roots}\n"
    "vals['beta_publish._SCRATCH_ROOT'] = str(beta_publish._SCRATCH_ROOT)\n"
    "vals['beta_publish.LOCK_DIR'] = str(beta_publish.LOCK_DIR)\n"
    "vals['beta_publish.SWEEP_STATE_PATH'] = str(beta_publish.SWEEP_STATE_PATH)\n"
    "vals['beta_publish.SWEEP_WORKTREE_DIR'] = str(beta_publish.SWEEP_WORKTREE_DIR)\n"
    "vals['beta_publish.SWEEP_LOG_PATH'] = str(beta_publish.SWEEP_LOG_PATH)\n"
    "vals['search_start'] = str(paths.vault_search_start())\n"
    "print(json.dumps(vals))\n"
)


def _probe_modules(cwd: Path, env_vault: Path | None, engine: Path = ENGINE) -> dict[str, Path]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    if env_vault is not None:
        env["MACP_VAULT_ROOT"] = str(env_vault)
    env["PYTHONPATH"] = str(engine)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE_MODULES], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return {k: Path(v) for k, v in json.loads(proc.stdout.strip().splitlines()[-1]).items()}


def _expect_scratch_under(got: dict[str, Path], root: Path) -> None:
    assert got["beta_publish._SCRATCH_ROOT"] == root
    assert got["beta_publish.LOCK_DIR"] == root / "Worker" / "_state" / "beta_locks"
    assert got["beta_publish.SWEEP_STATE_PATH"] == root / "Worker" / "_state" / "beta_sweep.json"
    assert got["beta_publish.SWEEP_WORKTREE_DIR"] == root / "Worker" / "_state" / "beta_worktrees"
    assert got["beta_publish.SWEEP_LOG_PATH"] == root / "Worker" / "_state" / "beta_sweep.log"


def test_module_vault_constants_converge_in_a_fresh_interpreter(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (vault / "Todo").mkdir()
    got = _probe_modules(cwd=tmp_path, env_vault=vault)
    assert got.pop("search_start") == ENGINE
    _expect_scratch_under(got, ENGINE)          # checkout scratch stays on the engine
    for key in (f"{m}.{a}" for m, a in _MODULE_ROOTS):
        assert got[key] == vault, (key, got[key])


def test_checkout_modules_ignore_cwd_vault_without_env(tmp_path, combined_copy):
    # No env, cwd inside a *different* vault: in a checkout the anchor is the
    # engine root, so the modules still resolve that checkout (never the cwd
    # vault). The checkout is a combined-layout copy of this engine tree.
    assert combined_copy != ENGINE
    other = tmp_path / "other-vault"
    (other / ".macp").mkdir(parents=True)
    (other / ".macp" / "config.toml").write_text("", encoding="utf-8")
    got = _probe_modules(cwd=other, env_vault=None, engine=combined_copy)
    assert got.pop("search_start") == combined_copy
    _expect_scratch_under(got, combined_copy)
    for key in (f"{m}.{a}" for m, a in _MODULE_ROOTS):
        assert got[key] == combined_copy, (key, got[key])


def test_checkout_modules_resolve_without_env_from_a_bare_cwd(tmp_path, combined_copy):
    assert combined_copy != ENGINE
    got = _probe_modules(cwd=tmp_path, env_vault=None, engine=combined_copy)
    for key in (f"{m}.{a}" for m, a in _MODULE_ROOTS):
        assert got[key] == combined_copy, (key, got[key])
