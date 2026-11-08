"""Shared fixtures for the todo_system test suite.

Why this file exists
--------------------
``graph_memory`` and ``session_log`` both resolve their vault paths from
``__file__`` **at import time**, so handing a test a ``tmp_path`` is not enough:
the modules keep pointing at the developer's real vault. Every test that
exercises them has to re-bind the module globals, which is what
``isolated_vault`` does.

The dangerous part is that those globals come in two flavours with *different*
monkeypatch semantics:

* **Looked up in a function body** — graph_memory's ``ROOT`` and ``INDEX_PATH``,
  and session_log's ``ROOT`` and ``SESSIONS_DIR``. Resolved at call time, so
  ``monkeypatch.setattr`` works.
* **Frozen into a default argument** — ``init_relations_db(db_path=DB_PATH)``,
  ``iter_markdown_files(vault_root=ROOT)``, ``edges_from_file(..., vault_root=ROOT)``,
  ``scan_vault_and_index(vault_root=ROOT)``. The value is bound when the module is
  imported, so ``monkeypatch.setattr`` **cannot** reach it.

graph_memory's ``recent_episodes`` / ``recent_sessions`` / ``scan_unlinked_mentions`` /
``active_decisions`` / ``render_index_markdown`` used to straddle both groups: a frozen
``vault_root=ROOT`` default that the body then ignored anyway, globbing the derived
``EPISODES_DIR`` / ``SESSIONS_DIR`` / ``ENTITIES_DIR`` / ``DECISIONS_DIR`` constants
instead. They now take ``vault_root: Path | None = None`` and resolve ``ROOT`` at call
time, so they honour an explicit argument *and* stay patchable without one. Those four
derived constants consequently have no function-body reader left in graph_memory;
``isolated_vault`` still redirects them below, harmlessly, because the fixture's wiring
is deliberately exhaustive.

⚠ The trap this used to describe is now narrower: ``scan_vault_and_index`` takes a
``db_path`` that defaults to ``vault_root``'s own DB, so passing ``vault_root=`` is
enough to stay off the real ``Memory/relations.sqlite``. It is still reachable by
omitting ``con=`` **and** ``vault_root=`` together, since ``vault_root=ROOT`` remains
a frozen default that ``monkeypatch.setattr`` cannot touch. Passing an explicit
``con=`` (use :meth:`IsolatedVault.connect`) remains the habit to keep for
library-level tests.

graph_memory's CLI commands *are* now exercised, at the bottom of
``test_graph_memory.py``: they take ``--vault-root`` and are driven through
``main()`` against a plain ``tmp_path``. Those tests deliberately do **not** use
``isolated_vault`` — it patches ``ROOT``/``DB_PATH``, which would make their
"the real vault was not touched" assertions vacuous.

The installed-wheel fixture
---------------------------
``installed_wheel`` builds the wheel, makes an empty venv with the locked
dependencies and installs the wheel into it — once per pytest session. The
seven ``test_installed_wheel_smoke*`` modules used to repeat that in their own
module-scope ``installed`` fixtures (a module fixture pulled from a sibling
module is rebuilt anyway); each now keeps ``installed`` as a thin alias of this
one. All offline (``UV_OFFLINE=1``), all under pytest's tmp dirs. The venv is
read-only, enforced: tests run its interpreter and inspect its files, but must
not install into it or write under it, since every smoke module shares it. The
fixture compares a venv manifest before and after each test session; any
changed path (excluding ``__pycache__``/``*.pyc``, which running the venv's own
interpreter creates harmlessly) fails the teardown with the offending paths.
``test_installed_wheel_session_fixture.py`` pins the one-build-per-session count.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system import graph_memory, session_log

# Captured at import time — before any test can monkeypatch the modules — so the
# safety net below always inspects the real vault, never a redirected one.
REAL_VAULT = Path(__file__).resolve().parents[2]
REAL_DB = REAL_VAULT / "Memory" / "relations.sqlite"
REAL_INDEX = REAL_VAULT / "Memory" / "index.md"
REAL_SESSIONS = REAL_VAULT / "Memory" / "sessions"

# graph_memory globals that must be redirected, mapped to their tmp-vault path.
_GRAPH_MEMORY_ATTRS = (
    "ROOT",
    "MEMORY_DIR",
    "ENTITIES_DIR",
    "EPISODES_DIR",
    "SESSIONS_DIR",
    "DECISIONS_DIR",
    "TODO_ITEMS_DIR",
    "NOTES_DIR",
    "INDEX_PATH",
    "DB_PATH",
)
_SESSION_LOG_ATTRS = ("ROOT", "SESSIONS_DIR")


def _sessions_snapshot() -> frozenset[str]:
    if not REAL_SESSIONS.is_dir():
        return frozenset()
    return frozenset(p.name for p in REAL_SESSIONS.iterdir())


@pytest.fixture(scope="session", autouse=True)
def _real_vault_untouched():
    """Fail the whole run if a test wrote into the real vault's memory layer.

    Deliberately checks *existence flips* and *new session filenames* rather than
    mtime or size: the host runs todo-sync on a timer, which legitimately
    regenerates ``Memory/index.md`` and ``relations.sqlite`` while pytest is
    running, and an mtime check would flag that as a test failure.

    The existence check is sharpest exactly where it matters most — in CI
    ``relations.sqlite`` is gitignored and therefore absent at session start, so
    a stray ``scan_vault_and_index()`` with no ``con=`` creates it and is caught
    for certain.
    """
    before = (REAL_DB.exists(), REAL_INDEX.exists(), _sessions_snapshot())
    yield
    after = (REAL_DB.exists(), REAL_INDEX.exists(), _sessions_snapshot())
    assert after == before, (
        "a test touched the real vault: "
        f"relations.sqlite exists {before[0]}->{after[0]}, "
        f"index.md exists {before[1]}->{after[1]}, "
        f"new session files {sorted(after[2] - before[2])}, "
        f"removed session files {sorted(before[2] - after[2])}"
    )


@dataclass(frozen=True)
class IsolatedVault:
    """A throwaway vault tree that graph_memory / session_log are pointed at."""

    root: Path
    memory: Path
    entities: Path
    episodes: Path
    sessions: Path
    decisions: Path
    notes: Path
    todo_items: Path
    index_path: Path
    db_path: Path

    def write(self, rel: str, content: str) -> Path:
        """Write ``content`` to a vault-relative path, creating parents."""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def connect(self) -> sqlite3.Connection:
        """Open the tmp vault's edge DB.

        Always hand the result to ``scan_vault_and_index(con=...)``. Omitting it
        is only safe when ``vault_root=`` is passed instead; omitting both still
        rebuilds the real ``Memory/relations.sqlite`` (see module docstring).
        """
        return graph_memory.init_relations_db(self.db_path)


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch) -> IsolatedVault:
    root = tmp_path / "vault"
    memory = root / "Memory"
    vault = IsolatedVault(
        root=root,
        memory=memory,
        entities=memory / "entities",
        episodes=memory / "episodes",
        sessions=memory / "sessions",
        decisions=root / "Decisions",
        notes=root / "Notes",
        todo_items=root / "Todo" / "items",
        index_path=memory / "index.md",
        db_path=memory / "relations.sqlite",
    )
    for directory in (
        vault.entities,
        vault.episodes,
        vault.sessions,
        vault.decisions,
        vault.notes,
        vault.todo_items,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    targets = {
        "ROOT": vault.root,
        "MEMORY_DIR": vault.memory,
        "ENTITIES_DIR": vault.entities,
        "EPISODES_DIR": vault.episodes,
        "SESSIONS_DIR": vault.sessions,
        "DECISIONS_DIR": vault.decisions,
        "TODO_ITEMS_DIR": vault.todo_items,
        "NOTES_DIR": vault.notes,
        "INDEX_PATH": vault.index_path,
        "DB_PATH": vault.db_path,
    }
    for name in _GRAPH_MEMORY_ATTRS:
        monkeypatch.setattr(graph_memory, name, targets[name])
    for name in _SESSION_LOG_ATTRS:
        monkeypatch.setattr(session_log, name, targets[name])

    # Self-check: every redirected global really points inside tmp_path. Catches a
    # typo'd attribute name (monkeypatch.setattr would happily create a new one)
    # before the test body gets a chance to write somewhere real.
    for module, names in ((graph_memory, _GRAPH_MEMORY_ATTRS), (session_log, _SESSION_LOG_ATTRS)):
        for name in names:
            value = getattr(module, name)
            assert isinstance(value, Path) and value.is_relative_to(tmp_path), (
                f"{module.__name__}.{name} escaped the tmp vault: {value}"
            )

    return vault


# --- combined layout: the engine checkout that is also the vault -------------------
# Before the engine split the vault and the engine were one tree, with
# .macp/config.toml at its root; vault_root()'s step-4 fallback, engine_root() and
# vault_search_start() all anchored at that root. The engine checkout no longer
# carries the marker, so the tests that pin that behaviour build this throwaway tree
# and point paths.__file__ into it (inside their own monkeypatch context).


@pytest.fixture
def combined_layout(tmp_path_factory) -> Path:
    """A throwaway combined layout: ``.macp/config.toml`` beside ``todo_system/`` and
    ``scripts/`` (no ``todo_system/_engine`` marker, so it reads as a checkout).

    Built under its own ``mktemp`` dir — a sibling of the test's ``tmp_path``, never
    an ancestor — so a bogus ``.macp`` a test writes into ``tmp_path`` (and a start
    dir under it) never walks up into this marker. The config carries no
    ``vault_root`` key, so the marker directory itself is the vault root."""
    root = tmp_path_factory.mktemp("combined").resolve()
    (root / "todo_system").mkdir()
    (root / "scripts").mkdir()
    (root / ".macp").mkdir()
    (root / ".macp" / "config.toml").write_text("profile = 'test-host'\n", encoding="utf-8")
    return root


# --- installed wheel, built once per session ---------------------------------------

_UV = os.environ.get("UV") or shutil.which("uv")
# Env that would point uv back at this checkout / the live vault — the widest of
# the smoke modules' own scrub lists, so the build sees no more than any of them did.
_WHEEL_UV_SCRUB = (
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
    "WORKER_MODEL",
    "MACP_STAGE_USAGE",
    "MACP_HOST_ID",
    "MAESTRO_DAILY_RUN_BUDGET",
    "MAESTRO_PLANNER_AGGRESSIVE",
    "MAESTRO_DISCOVERY",
    "MAESTRO_DISCOVERY_INTERVAL_DAYS",
)
# One entry (the venv) per build in this process; the session test asserts len 1.
_WHEEL_BUILDS: list[Path] = []


def _wheel_uv_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _WHEEL_UV_SCRUB}
    env["UV_OFFLINE"] = "1"
    env.update(extra)
    return env


def _wheel_base_python() -> str:
    """The interpreter the empty venv is created from: the one under this
    venv (pytest runs inside uv's project env), never the venv python itself."""
    base = getattr(sys, "_base_executable", None)
    if base and Path(base).is_file():
        return base
    for name in (f"python{sys.version_info.major}.{sys.version_info.minor}", "python3"):
        candidate = Path(sys.base_prefix) / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _wheel_run(args, *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(a) for a in args], cwd=str(cwd), env=env,
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )


def _venv_manifest(root: Path) -> dict[str, tuple[int, int]]:
    """Map of relative path -> (size, mtime_ns), excluding ``__pycache__``/``*.pyc``."""
    manifest: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        stat = path.stat()
        manifest[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return manifest


def _manifest_diff(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    """Paths added, removed, or changed between two manifests, sorted."""
    changed = set(before) ^ set(after)
    changed |= {path for path in before.keys() & after.keys() if before[path] != after[path]}
    return sorted(changed)


def _wheel_ok(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    assert proc.returncode == 0, (
        f"{what}: rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


@pytest.fixture(scope="session")
def installed_wheel_builds() -> list[Path]:
    """The venvs ``installed_wheel`` built in this session (expected: exactly one)."""
    return _WHEEL_BUILDS


@pytest.fixture(scope="session")
def installed_wheel(tmp_path_factory, installed_wheel_builds):
    """Build the wheel, make an empty venv with the locked dependencies, install
    the wheel into it with --no-deps. One build per session; read-only by contract."""
    assert _UV, "uv is required to build and install the wheel (no skip by design)"
    assert not installed_wheel_builds, f"installed_wheel built twice: {installed_wheel_builds}"
    base = tmp_path_factory.mktemp("installed-wheel")
    out = base / "dist"
    _wheel_ok(_wheel_run([_UV, "build", "--out-dir", out], cwd=REAL_VAULT, env=_wheel_uv_env(),
                         timeout=180), "uv build")
    wheels = sorted(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        infos = zf.infolist()

    venv = base / "venv"
    _wheel_ok(_wheel_run([_UV, "sync", "--frozen", "--no-install-project", "--no-dev",
                          "--python", _wheel_base_python()],
                         cwd=REAL_VAULT, env=_wheel_uv_env(UV_PROJECT_ENVIRONMENT=str(venv)),
                         timeout=120),
              "uv sync (dependencies only, into the empty venv)")
    _wheel_ok(_wheel_run([_UV, "pip", "install", "--python", venv / "bin" / "python", "--no-deps",
                          "--offline", wheel],
                         cwd=base, env=_wheel_uv_env(), timeout=60),
              "uv pip install --no-deps <wheel>")
    assert (venv / "bin" / "python").exists(), venv
    assert (venv / "bin" / "macp").is_file(), sorted(p.name for p in (venv / "bin").iterdir())
    installed_wheel_builds.append(venv)
    print(f"[installed_wheel] uv build #{len(installed_wheel_builds)} -> {wheel.name}")  # -rA shows it
    before = _venv_manifest(venv)
    yield SimpleNamespace(wheel=wheel, venv=venv, python=venv / "bin" / "python",
                          names=names, infos=infos)
    changed = _manifest_diff(before, _venv_manifest(venv))
    if changed:
        pytest.fail(
            "installed_wheel venv was modified by a test (read-only by contract): "
            + ", ".join(changed))
