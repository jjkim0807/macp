"""Unit tests for ``todo_system.paths.vault_root()`` resolution order.

Covers all four branches — env var, config ``vault_root`` key, the ``.macp``
marker directory, and the marker-gated legacy repo-root fallback — plus the
fallback's byte-identity guarantee: with neither ``MACP_VAULT_ROOT`` nor a
``.macp`` marker on the cwd path, ``vault_root()`` returns exactly the
pre-resolver value when the install location carries a ``.macp/config.toml``
marker (so the live daemon keeps resolving the same root), and otherwise raises
``VaultRootError`` naming the two fixes.

Every case drives the ``start=`` seam against a throwaway ``tmp_path`` (or sets
the env var to one), so none of them walk into or write the real vault. The
session-scoped ``_real_vault_untouched`` guard in ``tests/conftest.py`` backs
that up.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

from todo_system import paths

requires_tomllib = pytest.mark.skipif(
    importlib.util.find_spec("tomllib") is None,
    reason="config-key resolution needs the stdlib tomllib (Python 3.11+)",
)

# The byte-identity / install-pin invariants below hold only when the engine is
# installed *inside* a vault — the "combined layout" that carries a
# .macp/config.toml marker at the repo root (exactly what vault_root()'s step-4
# fallback checks). In the public engine-only export tree that marker is absent,
# so vault_root() raises VaultRootError by design and these cases would fail
# spuriously; they skip there instead (public-split-10 AC2 vault-only skip / AC4:
# "assert only when the engine is inside the vault", never delete the assertion).
# The live vault (and any worktree of it) has the marker, so nothing skips there —
# the byte-identity assertions run exactly as before.
_vault_only_byte_identity = pytest.mark.skipif(
    not (Path(paths.__file__).resolve().parents[1] / ".macp" / "config.toml").is_file(),
    reason="vault-only: byte-identity/install-pin holds only in the combined vault "
           "layout (.macp/config.toml at the repo root); the engine-only export tree "
           "has no marker and vault_root() raises by design.",
)


def test_env_var_wins_over_config(monkeypatch, tmp_path):
    # A config that would otherwise resolve elsewhere must be ignored once the
    # env var is set — env is the highest-priority branch.
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/should/not/win'\n", encoding="utf-8")
    target = tmp_path / "env-root"
    monkeypatch.setenv("MACP_VAULT_ROOT", str(target))
    assert paths.vault_root(start=tmp_path) == target


@requires_tomllib
def test_config_vault_root_key_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    key_target = tmp_path / "declared-elsewhere"
    macp = tmp_path / ".macp"
    macp.mkdir()
    # TOML literal string (single quotes): a POSIX path needs no escaping.
    (macp / "config.toml").write_text(f"vault_root = '{key_target}'\n", encoding="utf-8")
    assert paths.vault_root(start=tmp_path) == key_target


def test_marker_directory_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    # No vault_root key → the directory holding the .macp marker is the root,
    # found by walking up from a nested start directory.
    (macp / "config.toml").write_text("profile = 'test-host'\n", encoding="utf-8")
    deep = tmp_path / "sub" / "deep"
    deep.mkdir(parents=True)
    # vault_root resolves the start dir, so compare against the resolved root.
    assert paths.vault_root(start=deep) == tmp_path.resolve()


@_vault_only_byte_identity
def test_fallback_is_byte_identical_repo_root(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    # No env var and no .macp marker anywhere above start → legacy fallback,
    # which must equal parents[1] of this module (the pre-resolver value).
    start = tmp_path / "no" / "macp" / "here"
    start.mkdir(parents=True)
    expected = Path(paths.__file__).resolve().parents[1]
    assert paths.vault_root(start=start) == expected


def test_install_marker_present_returns_repo_root(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    # Step 4 with the install location carrying the marker: parents[1] of the
    # (monkeypatched) module path holds a .macp/config.toml, so it is returned
    # unchanged even though the start dir walks up to no marker of its own. The
    # sibling install/ tree is never on the upward path from start, so only the
    # __file__-anchored step 4 can find it.
    install = tmp_path / "install"
    (install / "todo_system").mkdir(parents=True)
    (install / ".macp").mkdir()
    (install / ".macp" / "config.toml").write_text("profile = 'test-host'\n", encoding="utf-8")
    monkeypatch.setattr(paths, "__file__", str(install / "todo_system" / "paths.py"))
    start = tmp_path / "elsewhere"
    start.mkdir()
    assert paths.vault_root(start=start) == install.resolve()


def test_fallback_raises_when_no_install_marker(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    # Step 4 with no marker anywhere: env unset, start walks up to no .macp, and
    # the install location (parents[1] of the monkeypatched module path) has no
    # .macp/config.toml either → raise rather than return a bogus parent dir.
    install = tmp_path / "install"
    (install / "todo_system").mkdir(parents=True)
    monkeypatch.setattr(paths, "__file__", str(install / "todo_system" / "paths.py"))
    start = tmp_path / "elsewhere"
    start.mkdir()
    with pytest.raises(paths.VaultRootError) as excinfo:
        paths.vault_root(start=start)
    message = str(excinfo.value)
    # Both documented fixes must be named so the operator knows how to recover.
    assert "MACP_VAULT_ROOT" in message
    assert ".macp/config.toml" in message


@requires_tomllib
def test_nonstring_key_falls_back_to_marker_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    # A vault_root that parses but is not a (non-empty) string is ignored, so the
    # marker directory wins — exercises the isinstance/truthiness guard, not the
    # tomllib-missing path (hence the gate).
    (macp / "config.toml").write_text("vault_root = 123\n", encoding="utf-8")
    assert paths.vault_root(start=tmp_path) == tmp_path.resolve()


# --- label_prefix(): env -> config key -> byte-identical default -------------


def test_label_prefix_env_wins(monkeypatch, tmp_path):
    # Env is the highest-priority branch, ahead of any config key.
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("label_prefix = 'should.not.win.'\n", encoding="utf-8")
    monkeypatch.setenv("MACP_LABEL_PREFIX", "acme.")
    assert paths.label_prefix(start=tmp_path) == "acme."


@requires_tomllib
def test_label_prefix_config_key_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_LABEL_PREFIX", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("label_prefix = 'acme.'\n", encoding="utf-8")
    assert paths.label_prefix(start=tmp_path) == "acme."


def test_label_prefix_default_when_env_and_config_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_LABEL_PREFIX", raising=False)
    # No env var and no .macp marker above start → the neutral default org prefix.
    # An installed vault pins its real prefix via the config key, so neutralizing this
    # default does not touch that vault's live labels (public-split leaf 7).
    start = tmp_path / "no" / "macp" / "here"
    start.mkdir(parents=True)
    assert paths.label_prefix(start=start) == "com.macp."


def test_label_prefix_marker_without_key_falls_through_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_LABEL_PREFIX", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    # A .macp marker with no label_prefix key yields the neutral default — unlike
    # vault_root, a prefix cannot be derived from the marker directory itself.
    (macp / "config.toml").write_text("profile = 'test-host'\n", encoding="utf-8")
    assert paths.label_prefix(start=tmp_path) == "com.macp."


# --- primary_calendar_id(): env -> config key -> neutral empty default --------
# The default is a neutral empty string (not a personal calendar): a fresh engine
# clone must carry no personal identity, and callers omit --calendar-id when empty.


def test_primary_calendar_id_env_wins(monkeypatch, tmp_path):
    # Env is the highest-priority branch, ahead of any config key.
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("calendar_id = 'config@example.com'\n", encoding="utf-8")
    monkeypatch.setenv("MACP_CALENDAR_ID", "env@example.com")
    assert paths.primary_calendar_id(start=tmp_path) == "env@example.com"


@requires_tomllib
def test_primary_calendar_id_config_key_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_CALENDAR_ID", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("calendar_id = 'config@example.com'\n", encoding="utf-8")
    assert paths.primary_calendar_id(start=tmp_path) == "config@example.com"


def test_primary_calendar_id_default_empty_when_env_and_config_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_CALENDAR_ID", raising=False)
    # No env var and no .macp marker above start → the neutral empty default, so a
    # fresh engine clone carries no personal calendar id.
    start = tmp_path / "no" / "macp" / "here"
    start.mkdir(parents=True)
    assert paths.primary_calendar_id(start=start) == ""


def test_primary_calendar_id_marker_without_key_falls_through_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_CALENDAR_ID", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    # A .macp marker with no calendar_id key yields the empty default.
    (macp / "config.toml").write_text("profile = 'test-host'\n", encoding="utf-8")
    assert paths.primary_calendar_id(start=tmp_path) == ""


# --- repo_slug(): env -> config key -> neutral placeholder default ------------
# The default is a neutral placeholder owner/repo, never a personal repo slug.


def test_repo_slug_env_wins(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("repo_slug = 'config-org/repo'\n", encoding="utf-8")
    monkeypatch.setenv("MACP_REPO_SLUG", "env-org/repo")
    assert paths.repo_slug(start=tmp_path) == "env-org/repo"


@requires_tomllib
def test_repo_slug_config_key_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_REPO_SLUG", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("repo_slug = 'config-org/repo'\n", encoding="utf-8")
    assert paths.repo_slug(start=tmp_path) == "config-org/repo"


def test_repo_slug_default_placeholder_when_env_and_config_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_REPO_SLUG", raising=False)
    # No env var and no .macp marker above start → the neutral placeholder default,
    # so a fresh engine clone carries no personal repo owner.
    start = tmp_path / "no" / "macp" / "here"
    start.mkdir(parents=True)
    assert paths.repo_slug(start=start) == "example-org/MaestroACP"


def test_repo_slug_marker_without_key_falls_through_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_REPO_SLUG", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    # A .macp marker with no repo_slug key yields the placeholder default.
    (macp / "config.toml").write_text("profile = 'test-host'\n", encoding="utf-8")
    assert paths.repo_slug(start=tmp_path) == "example-org/MaestroACP"


# --- state_backend(): env > config > default "git"; invalid value raises -----

def test_state_backend_env_wins(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("state_backend = 'external'\n", encoding="utf-8")
    monkeypatch.setenv("MACP_STATE_BACKEND", "git")
    assert paths.state_backend(start=tmp_path) == "git"


@requires_tomllib
def test_state_backend_config_key_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_STATE_BACKEND", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("state_backend = 'external'\n", encoding="utf-8")
    assert paths.state_backend(start=tmp_path) == "external"


def test_state_backend_default_git_when_env_and_config_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_STATE_BACKEND", raising=False)
    start = tmp_path / "no" / "macp" / "here"
    start.mkdir(parents=True)
    assert paths.state_backend(start=start) == "git"


def test_state_backend_marker_without_key_falls_through_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_STATE_BACKEND", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("profile = 'test-host'\n", encoding="utf-8")
    assert paths.state_backend(start=tmp_path) == "git"


def test_state_backend_env_invalid_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_STATE_BACKEND", "bogus")
    with pytest.raises(ValueError):
        paths.state_backend(start=tmp_path)


@requires_tomllib
def test_state_backend_config_invalid_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_STATE_BACKEND", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("state_backend = 'bogus'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        paths.state_backend(start=tmp_path)


# --- state_sqlite_path(): env > config > XDG default -------------------------

def test_state_sqlite_path_env_wins(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text(
        f"state_sqlite_path = '{(tmp_path / 'config.sqlite').as_posix()}'\n", encoding="utf-8"
    )
    env_path = tmp_path / "env.sqlite"
    monkeypatch.setenv("MACP_STATE_SQLITE_PATH", str(env_path))
    assert paths.state_sqlite_path(start=tmp_path) == env_path


@requires_tomllib
def test_state_sqlite_path_config_key_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_STATE_SQLITE_PATH", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    config_path = tmp_path / "config.sqlite"
    (macp / "config.toml").write_text(
        f"state_sqlite_path = '{config_path.as_posix()}'\n", encoding="utf-8"
    )
    assert paths.state_sqlite_path(start=tmp_path) == config_path


def test_state_sqlite_path_default_respects_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_STATE_SQLITE_PATH", raising=False)
    start = tmp_path / "no" / "macp" / "here"
    start.mkdir(parents=True)
    xdg = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    result = paths.state_sqlite_path(start=start)
    assert result == xdg / "macp" / "state.sqlite"
    assert not str(result).startswith(str(tmp_path / "no"))


def test_state_sqlite_path_default_falls_back_to_local_share_without_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_STATE_SQLITE_PATH", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    start = tmp_path / "no" / "macp" / "here"
    start.mkdir(parents=True)
    assert paths.state_sqlite_path(start=start) == Path(
        "~/.local/share/macp/state.sqlite"
    ).expanduser()


def test_state_sqlite_path_expanduser(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_STATE_SQLITE_PATH", "~/state.sqlite")
    assert paths.state_sqlite_path(start=tmp_path) == Path("~/state.sqlite").expanduser()


# --- tomllib-less interpreters (macOS launchd /usr/bin/python3 = 3.9) ---------
# Without the fallback a 3.9 launchd daemon resolved every config key to None and
# took the neutral default — a silent label change on the Mac. These cases hide
# tomllib (sys.modules entry None → ImportError) so they run on any interpreter.

_VAULT_SHAPED_CONFIG = (
    'profile = "default"\n'
    'worker_mode = "auto"\n'
    "capabilities = []\n"
    "\n"
    "# comment with = sign and 'quotes'\n"
    'label_prefix = "com.example."\n'
    "calendar_id = 'cal@example.com'   # trailing comment\n"
    'repo_slug = "acme/MaestroACP"\n'
    "port = 8080\n"
    "\n"
    "[table]\n"
    'label_prefix = "com.nested."\n'
)


def _hide_tomllib(monkeypatch):
    monkeypatch.setitem(sys.modules, "tomllib", None)


def test_no_tomllib_reads_top_level_string_keys(monkeypatch, tmp_path):
    for var in ("MACP_LABEL_PREFIX", "MACP_CALENDAR_ID", "MACP_REPO_SLUG"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(_VAULT_SHAPED_CONFIG, encoding="utf-8")
    _hide_tomllib(monkeypatch)
    assert paths.label_prefix(start=tmp_path) == "com.example."
    assert paths.primary_calendar_id(start=tmp_path) == "cal@example.com"
    assert paths.repo_slug(start=tmp_path) == "acme/MaestroACP"


def test_no_tomllib_ignores_non_strings_and_nested_tables(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(_VAULT_SHAPED_CONFIG, encoding="utf-8")
    _hide_tomllib(monkeypatch)
    assert paths._read_config_key(cfg, "port") is None
    assert paths._read_config_key(cfg, "capabilities") is None
    assert paths._read_config_key(cfg, "missing") is None
    only_nested = tmp_path / "nested.toml"
    only_nested.write_text('[t]\nlabel_prefix = "com.nested."\n', encoding="utf-8")
    assert paths._read_config_key(only_nested, "label_prefix") is None


@requires_tomllib
def test_no_tomllib_fallback_agrees_with_tomllib_on_the_real_vault_config():
    """Byte-identity on this vault's own config (read-only): a 3.9 launchd daemon
    must resolve exactly what the 3.11+ runtime does for every pinned key."""
    cfg = Path(paths.__file__).resolve().parents[1] / ".macp" / "config.toml"
    if not cfg.is_file():
        pytest.skip("engine-only checkout: no vault config")
    for key in ("label_prefix", "calendar_id", "repo_slug", "profile", "vault_root"):
        assert paths._read_config_key_minimal(cfg, key) == paths._read_config_key(cfg, key), key


# --- worker.ROOT wiring honors the resolver (public-engine / vault split follow-up) --
# worker.py computes ROOT via paths.vault_root(start=install-dir) at import time, and
# its TODO_DIR/ITEMS_DIR/WORKER_DIR derive from ROOT. worker is imported locally (it
# pulls in the heavy project graph, kept out of the lightweight paths tests above) and
# reloaded so the branch under test takes effect; a trailing clean reload restores the
# real-root ROOT. `importlib.reload` comes from the module-level `import importlib.util`.


@_vault_only_byte_identity
def test_worker_root_no_env_is_byte_identical_repo_root(monkeypatch):
    import todo_system.worker as worker
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(worker)
        assert mod.ROOT == Path(mod.__file__).resolve().parents[1]
        # derived path constants follow ROOT
        assert mod.TODO_DIR == mod.ROOT / "Todo"
        assert mod.WORKER_DIR == mod.ROOT / "Worker"
    importlib.reload(worker)


def test_worker_root_env_redirects_module_and_derived_dirs(monkeypatch, tmp_path):
    import todo_system.worker as worker
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(worker)
        assert mod.ROOT == tmp_path
        # the redirect flows into every derived path constant, not just ROOT
        assert mod.TODO_DIR == tmp_path / "Todo"
        assert mod.ITEMS_DIR == tmp_path / "Todo" / "items"
        assert mod.WORKER_DIR == tmp_path / "Worker"
    importlib.reload(worker)


@_vault_only_byte_identity
def test_worker_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    # A bogus .macp/config.toml in the cwd must NOT capture ROOT: start= pins the
    # install dir, so resolution ignores the cwd-upward search. If it resolved from
    # cwd instead, ROOT would become '/bogus'.
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.worker as worker
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(worker)
        assert mod.ROOT == Path(mod.__file__).resolve().parents[1]
        assert mod.ROOT != Path("/bogus")
    importlib.reload(worker)


# --- orchestrator/macp module roots honor the resolver (public-split step 3 follow-up) --
# jobs.REPO_ROOT, metrics._ROOT, model_select._ROOT and macp.deps.VAULT_ROOT each computed
# the vault root as Path(__file__).resolve().parents[2] at import time and derived their
# Worker/Todo path constants from it. They now resolve via paths.vault_root(start=<install
# dir>) — depth parents[2] because these modules sit one package deeper than worker.py.
# Each module is imported locally (they pull in heavier orchestrator/macp graphs, kept out
# of the lightweight paths tests above) and reloaded so the import-time branch under test
# takes effect; a trailing clean reload (env restored, cwd normal) puts the real root back.


@_vault_only_byte_identity
def test_jobs_root_no_env_is_byte_identical_repo_root(monkeypatch):
    import todo_system.orchestrator.jobs as jobs
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(jobs)
        assert mod.REPO_ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod.JOBS_DIR == mod.REPO_ROOT / "Worker" / "jobs"
    importlib.reload(jobs)


def test_jobs_root_env_redirects_module_and_derived_dirs(monkeypatch, tmp_path):
    import todo_system.orchestrator.jobs as jobs
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(jobs)
        assert mod.REPO_ROOT == tmp_path
        assert mod.JOBS_DIR == tmp_path / "Worker" / "jobs"
    importlib.reload(jobs)


@_vault_only_byte_identity
def test_jobs_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.orchestrator.jobs as jobs
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(jobs)
        assert mod.REPO_ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod.REPO_ROOT != Path("/bogus")
    importlib.reload(jobs)


@_vault_only_byte_identity
def test_metrics_root_no_env_is_byte_identical_repo_root(monkeypatch):
    import todo_system.orchestrator.metrics as metrics
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(metrics)
        assert mod._ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod.LEDGER == mod._ROOT / "Worker" / "assignments"
        assert mod.RUNS == mod._ROOT / "Worker" / "runs"
        assert mod.DB == mod._ROOT / "Todo" / "todo.db"
        assert mod.DEFAULT_OUT == mod._ROOT / "Todo" / "Metrics.md"
    importlib.reload(metrics)


def test_metrics_root_env_redirects_module_and_derived_dirs(monkeypatch, tmp_path):
    import todo_system.orchestrator.metrics as metrics
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(metrics)
        assert mod._ROOT == tmp_path
        assert mod.LEDGER == tmp_path / "Worker" / "assignments"
        assert mod.RUNS == tmp_path / "Worker" / "runs"
        assert mod.DB == tmp_path / "Todo" / "todo.db"
        assert mod.DEFAULT_OUT == tmp_path / "Todo" / "Metrics.md"
    importlib.reload(metrics)


@_vault_only_byte_identity
def test_metrics_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.orchestrator.metrics as metrics
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(metrics)
        assert mod._ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod._ROOT != Path("/bogus")
    importlib.reload(metrics)


@_vault_only_byte_identity
def test_model_select_root_no_env_is_byte_identical_repo_root(monkeypatch):
    import todo_system.orchestrator.model_select as model_select
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(model_select)
        assert mod._ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod._CACHE == mod._ROOT / "Worker" / "_state" / "model_pref.json"
    importlib.reload(model_select)


def test_model_select_root_env_redirects_module_and_derived_dirs(monkeypatch, tmp_path):
    import todo_system.orchestrator.model_select as model_select
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(model_select)
        assert mod._ROOT == tmp_path
        assert mod._CACHE == tmp_path / "Worker" / "_state" / "model_pref.json"
    importlib.reload(model_select)


@_vault_only_byte_identity
def test_model_select_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.orchestrator.model_select as model_select
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(model_select)
        assert mod._ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod._ROOT != Path("/bogus")
    importlib.reload(model_select)


@_vault_only_byte_identity
def test_deps_vault_root_no_env_is_byte_identical_repo_root(monkeypatch):
    import todo_system.macp.deps as deps
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(deps)
        expected = Path(mod.__file__).resolve().parents[2]
        assert mod.VAULT_ROOT == expected
        # write_integration_task() builds Todo/items from VAULT_ROOT at call time.
        assert mod.VAULT_ROOT / "Todo" / "items" == expected / "Todo" / "items"
    importlib.reload(deps)


def test_deps_vault_root_env_redirects_module_and_derived_dirs(monkeypatch, tmp_path):
    import todo_system.macp.deps as deps
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(deps)
        assert mod.VAULT_ROOT == tmp_path
        assert mod.VAULT_ROOT / "Todo" / "items" == tmp_path / "Todo" / "items"
    importlib.reload(deps)


@_vault_only_byte_identity
def test_deps_vault_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.macp.deps as deps
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(deps)
        assert mod.VAULT_ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod.VAULT_ROOT != Path("/bogus")
    importlib.reload(deps)


# --- orchestrator/beta_publish: engine root immune, vault-data root redirectable --
# beta_publish splits its former single _VAULT_ROOT in two. _ENGINE_ROOT (repo/
# install root, __file__-anchored) anchors ONLY the runtime beta_locks (LOCK_DIR)
# — per-install scratch, not data — so it must NOT move under MACP_VAULT_ROOT.
# _VAULT_DATA_ROOT resolves via paths.vault_root(start=parents[2]) and is the
# default vault_root= of the three copy-source functions (stage_build_files,
# build_prop_args, maybe_publish) AND the root of the config/watermark canon in
# .macp/, so all of those follow the env. The canon moved out of the engine
# package in split-8: it is operator-owned data, so a redirected vault must
# publish from its own app list, not from whatever the engine shipped with.
# Reloaded like the jobs/metrics cases; a trailing clean reload restores the root.
# inspect.signature pins the function-default wiring, not just the module constant.


@_vault_only_byte_identity
def test_beta_publish_roots_no_env_byte_identical_and_wired(monkeypatch):
    import todo_system.orchestrator.beta_publish as beta_publish
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(beta_publish)
        root = Path(mod.__file__).resolve().parents[2]
        # engine root + its runtime lock dir
        assert mod._ENGINE_ROOT == root
        assert mod.LOCK_DIR == root / "Worker" / "_state" / "beta_locks"
        # the canon is vault data; with no env it collapses onto the same root
        assert mod.CONFIG_PATH == root / ".macp" / "beta_publish_config.json"
        assert mod.WATERMARK_PATH == root / ".macp" / "beta_watermarks.json"
        # vault-data root is byte-identical to the pre-split _VAULT_ROOT when unset
        assert mod._VAULT_DATA_ROOT == root
        # and it is wired as the default vault_root= of all three copy-source fns
        for fn in (mod.stage_build_files, mod.build_prop_args, mod.maybe_publish):
            assert inspect.signature(fn).parameters["vault_root"].default == mod._VAULT_DATA_ROOT
    importlib.reload(beta_publish)


def test_beta_publish_env_redirects_data_root_only_engine_immune(monkeypatch, tmp_path):
    import todo_system.orchestrator.beta_publish as beta_publish
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(beta_publish)
        root = Path(mod.__file__).resolve().parents[2]
        # vault-data root AND the three function defaults follow the redirect
        assert mod._VAULT_DATA_ROOT == tmp_path
        for fn in (mod.stage_build_files, mod.build_prop_args, mod.maybe_publish):
            assert inspect.signature(fn).parameters["vault_root"].default == tmp_path
        # the config/watermark canon follows the redirect too (vault data)
        assert mod.CONFIG_PATH == tmp_path / ".macp" / "beta_publish_config.json"
        assert mod.WATERMARK_PATH == tmp_path / ".macp" / "beta_watermarks.json"
        # but the engine root and the runtime lock dir stay pinned to the install
        assert mod._ENGINE_ROOT == root
        assert mod._ENGINE_ROOT != tmp_path
        assert mod.LOCK_DIR == root / "Worker" / "_state" / "beta_locks"
    importlib.reload(beta_publish)


@_vault_only_byte_identity
def test_beta_publish_data_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.orchestrator.beta_publish as beta_publish
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(beta_publish)
        # start= pins parents[2]; the cwd-upward .macp (vault_root='/bogus') must
        # not capture the data root — that is the whole reason start= is passed.
        assert mod._VAULT_DATA_ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod._VAULT_DATA_ROOT != Path("/bogus")
    importlib.reload(beta_publish)


# --- orchestrator/human + stages: engine root immune, vault-data root redirectable --
# Both modules split their former single REPO_ROOT the way beta_publish did.
# _ENGINE_ROOT (repo/install root, __file__-anchored) anchors scripts/kakao_send.sh
# + the subprocess cwd (human) and .claude/commands (stages) — engine files that
# must NOT move under MACP_VAULT_ROOT. _VAULT_DATA_ROOT resolves via
# paths.vault_root(start=parents[2]) and anchors the Worker/assignments ledger
# (human._ledger_corr) and Worker/runs (stages.RUNS_DIR), which the inventory
# (Notes/macp-public-split-inventory.md §2.1) classifies as vault-data and which
# metrics._ROOT already reads from the vault (PR #168) — so they follow the env.
# Routing them through the resolver is what closes the human/stages↔metrics
# split-brain on those two dirs. (stages once exposed a `REPO_ROOT = _ENGINE_ROOT`
# compat alias for out-of-scope consumers; they have all migrated to the explicit
# roots and the alias is gone — macp-vault-root-stages-alias-removal.)
#
# These REPLACE the pre-#168 "REPO_ROOT is immune, no-redirect" cases: that premise
# treated the ledger + runs as engine/runtime, which the inventory §2.1 reversed
# (they are the vault-data SoT). The assertions are strengthened, not weakened —
# each env case now pins BOTH the redirect AND the engine immunity together, the
# beta_publish shape. human pulls PyYAML via .registry (imported locally like
# worker); stages is stdlib-only at import (model_select imports are function-local;
# paths is stdlib-only too, so the new import keeps that guarantee).


@_vault_only_byte_identity
def test_human_roots_no_env_byte_identical_and_wired(monkeypatch):
    import todo_system.orchestrator.human as human
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(human)
        root = Path(mod.__file__).resolve().parents[2]
        # engine root + the kakao script it anchors (immune, __file__-anchored)
        assert mod._ENGINE_ROOT == root
        assert mod.KAKAO_SCRIPT == root / "scripts" / "kakao_send.sh"
        # vault-data root byte-identical to the engine root when the env is unset;
        # it anchors the Worker/assignments ledger _ledger_corr reads.
        assert mod._VAULT_DATA_ROOT == root
        assert mod._VAULT_DATA_ROOT / "Worker" / "assignments" == \
            root / "Worker" / "assignments"
    importlib.reload(human)


def test_human_env_redirects_ledger_only_engine_immune(monkeypatch, tmp_path):
    import todo_system.orchestrator.human as human
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(human)
        root = Path(mod.__file__).resolve().parents[2]
        # vault-data root AND the ledger dir follow the redirect
        assert mod._VAULT_DATA_ROOT == tmp_path
        assert mod._VAULT_DATA_ROOT / "Worker" / "assignments" == \
            tmp_path / "Worker" / "assignments"
        # but the engine root and the kakao script stay pinned to the install
        assert mod._ENGINE_ROOT == root
        assert mod._ENGINE_ROOT != tmp_path
        assert mod.KAKAO_SCRIPT == root / "scripts" / "kakao_send.sh"
    importlib.reload(human)


@_vault_only_byte_identity
def test_human_data_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.orchestrator.human as human
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(human)
        # start= pins parents[2]; the cwd-upward .macp (vault_root='/bogus') must
        # not capture the data root, and the engine root is never cwd-derived.
        assert mod._VAULT_DATA_ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod._VAULT_DATA_ROOT != Path("/bogus")
        assert mod._ENGINE_ROOT == Path(mod.__file__).resolve().parents[2]
    importlib.reload(human)


@_vault_only_byte_identity
def test_stages_roots_no_env_byte_identical_and_wired(monkeypatch):
    import todo_system.orchestrator.stages as stages
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(stages)
        root = Path(mod.__file__).resolve().parents[2]
        # engine root + the .claude/commands prompts it anchors (immune),
        # all byte-identical to the engine root.
        assert mod._ENGINE_ROOT == root
        assert mod.COMMANDS_DIR == root / ".claude" / "commands"
        # vault-data root byte-identical to the engine root when unset; it anchors
        # Worker/runs, which metrics.RUNS reads from the vault too.
        assert mod._VAULT_DATA_ROOT == root
        assert mod.RUNS_DIR == root / "Worker" / "runs"
    importlib.reload(stages)


def test_stages_env_redirects_runs_only_engine_immune(monkeypatch, tmp_path):
    import todo_system.orchestrator.stages as stages
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(stages)
        root = Path(mod.__file__).resolve().parents[2]
        # vault-data root AND RUNS_DIR follow the redirect — this is the metrics
        # split-brain being closed (metrics.RUNS follows the env, so RUNS_DIR must).
        assert mod._VAULT_DATA_ROOT == tmp_path
        assert mod.RUNS_DIR == tmp_path / "Worker" / "runs"
        # but the engine root and the commands dir stay pinned
        assert mod._ENGINE_ROOT == root
        assert mod._ENGINE_ROOT != tmp_path
        assert mod.COMMANDS_DIR == root / ".claude" / "commands"
    importlib.reload(stages)


@_vault_only_byte_identity
def test_stages_data_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.orchestrator.stages as stages
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(stages)
        # start= pins parents[2]; the cwd-upward .macp must not capture the data
        # root, and COMMANDS_DIR / the engine root are never cwd-derived.
        assert mod._VAULT_DATA_ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod._VAULT_DATA_ROOT != Path("/bogus")
        assert mod.COMMANDS_DIR == \
            Path(mod.__file__).resolve().parents[2] / ".claude" / "commands"
        assert mod._ENGINE_ROOT == Path(mod.__file__).resolve().parents[2]
    importlib.reload(stages)


# --- scripts/*.py vault-root constants (묶음 A + B) -------------------------
# The four daemons + export_db_snapshot (묶음 A), plus health_report,
# macp_dashboard and macp_monitor (묶음 B), resolve their data root
# through paths.vault_root(start=<install dir>) too. They are standalone scripts, not
# importable package modules, so they load via spec_from_file_location rather than
# importlib.reload — but the three assertions mirror the metrics/model_select
# precedent above: no-env byte-identity, MACP_VAULT_ROOT redirect, and start=
# pinning the install dir (never cwd), the direct guard against an isolated
# worktree misreading its root.

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_SCRIPT_ROOT_ATTRS = [
    ("claude_auth_watch", "VAULT"),
    ("claude_model_autoswitch", "ROOT"),
    # claude_remote_control_watchdog and claude_upgrade were here until 공개 분리 10:
    # both scripts are on scripts/public_export_exclude.txt (the remote-control stack
    # moves to the vault in step 7), so they are absent from the engine export tree and
    # loading them there raises FileNotFoundError. Dropping them from this shared trio
    # is §10.6① / §11.6 (b1) brought forward to export time; the vault checkout keeps
    # its own coverage of those scripts elsewhere.
    ("export_db_snapshot", "VAULT_ROOT"),
    # 묶음 B. kakao_digest_send is deliberately absent: its KAKAO_VAULT_DIR override
    # breaks the plain-MACP_VAULT_ROOT contract this shared trio assumes, so it gets
    # its own test_kakao_digest_send_vault_dir_priority below. (find_stale_worker_tasks
    # was here until 공개 분리 9 deleted the script — 참조 0, v2 원장으로 대체.)
    ("health_report", "VAULT"),
    ("macp_dashboard", "VAULT"),
    ("macp_monitor", "ROOT"),
]


def _load_script_module(stem):
    """Execute scripts/<stem>.py fresh under the current environment and return it.

    Pop every script stem from sys.modules first: the watchdog and upgrade scripts
    do ``from claude_auth_watch import …`` / ``from claude_upgrade import …``, so a
    stale cached sibling would still carry a previous case's env — popping forces
    the sibling to re-exec under the env this case set. Register the freshly created
    module under its bare stem (not a dotted test name) so those sibling imports
    resolve to it. ``__name__`` becomes the stem, so the ``if __name__ ==
    "__main__"`` daemon body never runs.
    """
    for other, _ in _SCRIPT_ROOT_ATTRS:
        sys.modules.pop(other, None)
    path = _SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[stem] = mod
    spec.loader.exec_module(mod)
    return mod


@_vault_only_byte_identity
@pytest.mark.parametrize("stem,attr", _SCRIPT_ROOT_ATTRS)
def test_script_root_no_env_is_byte_identical_install_dir(monkeypatch, stem, attr):
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = _load_script_module(stem)
        assert getattr(mod, attr) == Path(mod.__file__).resolve().parents[1]


@pytest.mark.parametrize("stem,attr", _SCRIPT_ROOT_ATTRS)
def test_script_root_env_redirects_constant(monkeypatch, tmp_path, stem, attr):
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = _load_script_module(stem)
        assert getattr(mod, attr) == tmp_path


@_vault_only_byte_identity
@pytest.mark.parametrize("stem,attr", _SCRIPT_ROOT_ATTRS)
def test_script_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path, stem, attr):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = _load_script_module(stem)
        assert getattr(mod, attr) == Path(mod.__file__).resolve().parents[1]
        assert getattr(mod, attr) != Path("/bogus")


def test_claude_auth_watch_rc_label_follows_label_prefix(monkeypatch):
    """RC_LABEL is resolved (env → install config → neutral), not a literal —
    the watchdog/upgrade siblings re-declare their own, auth_watch's is the one
    its restart/status hints use."""
    with monkeypatch.context() as m:
        m.setenv("MACP_LABEL_PREFIX", "com.example.")
        mod = _load_script_module("claude_auth_watch")
        assert mod.RC_LABEL == "com.example.claude-remote-control"
    with monkeypatch.context() as m:
        m.delenv("MACP_LABEL_PREFIX", raising=False)
        mod = _load_script_module("claude_auth_watch")
        install = Path(mod.__file__).resolve().parents[1]
        assert mod.RC_LABEL == f"{paths.label_prefix(start=install)}claude-remote-control"


# --- kakao_digest_send: KAKAO_VAULT_DIR keeps priority over the resolver (묶음 B) --
# Unlike the daemons above, kakao_digest_send historically honoured a KAKAO_VAULT_DIR
# override ahead of everything else and only fell back to Path(__file__).parents[1].
# The resolver now supplies that fallback, but the override must still win — so this
# script gets a dedicated three-case contract instead of joining _SCRIPT_ROOT_ATTRS.


@_vault_only_byte_identity
def test_kakao_digest_send_vault_dir_priority(monkeypatch, tmp_path):
    """KAKAO_VAULT_DIR outranks MACP_VAULT_ROOT; the resolver only owns the fallback.

    (a) neither env set          -> byte-identical install-dir fallback (live default);
    (b) both env set             -> KAKAO_VAULT_DIR wins over MACP_VAULT_ROOT;
    (c) only MACP_VAULT_ROOT set -> the resolver's env branch supplies the root.

    Each case delenvs KAKAO_VAULT_DIR explicitly so an ambient value in the runner's
    environment cannot leak into (a)/(c). _load_script_module re-execs the module
    fresh under the case's env, so VAULT is recomputed every time.
    """
    kakao_root = tmp_path / "kakao-root"
    macp_root = tmp_path / "macp-root"

    # (a) neither env set -> byte-identical to the former parents[1] fallback.
    with monkeypatch.context() as m:
        m.delenv("KAKAO_VAULT_DIR", raising=False)
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = _load_script_module("kakao_digest_send")
        assert mod.VAULT == Path(mod.__file__).resolve().parents[1]

    # (b) both set -> KAKAO_VAULT_DIR takes precedence over MACP_VAULT_ROOT.
    with monkeypatch.context() as m:
        m.setenv("KAKAO_VAULT_DIR", str(kakao_root))
        m.setenv("MACP_VAULT_ROOT", str(macp_root))
        mod = _load_script_module("kakao_digest_send")
        assert mod.VAULT == kakao_root

    # (c) only MACP_VAULT_ROOT set -> resolver env branch supplies the root.
    with monkeypatch.context() as m:
        m.delenv("KAKAO_VAULT_DIR", raising=False)
        m.setenv("MACP_VAULT_ROOT", str(macp_root))
        mod = _load_script_module("kakao_digest_send")
        assert mod.VAULT == macp_root


# --- orchestrator/discovery + followups + orchestrator + trace: vault-data root
# --- migrated off the stages.REPO_ROOT compat alias --------------------------
# The public-split inventory (Notes/macp-public-split-inventory.md §2.1) classes
# Worker/_state, Worker/planner_state.json, Todo/items and Memory/episodes as
# vault-DATA: they must follow MACP_VAULT_ROOT, exactly as metrics._ROOT (PR #168)
# and the stages/human split above already do. These four consumers still built
# those paths from stages.REPO_ROOT — the engine-root compat alias that is
# deliberately env-IMMUNE (it stays pinned so runner.py's git ops keep hitting the
# checkout, an OUT-OF-SCOPE reader this leaf must not disturb). Under
# MACP_VAULT_ROOT they therefore read the wrong tree. This leaf reroutes each
# through paths.vault_root(start=parents[2]): byte-identical when the env is unset,
# redirected when it is set.
#
# discovery/followups resolve their root into a MODULE CONSTANT at import, so the
# env-redirect case reloads the module (the metrics/stages/human shape above).
# orchestrator._task_title and trace.render resolve PER CALL — the first reads it on
# every alert, the second documents a per-call contract for the long-lived daemon —
# so those two are driven through the function, no reload. None of these read or
# write the live vault: the module-const cases only compare Path values, and the
# per-call cases pin the root through a tmp file / a spy on the injected lookup, so
# the byte-equivalence RED control (run pre-migration) only ever attempts to open a
# guaranteed-absent unique slug under the engine tree, never a real vault file.


@_vault_only_byte_identity
def test_discovery_roots_no_env_byte_identical(monkeypatch):
    import todo_system.orchestrator.discovery as discovery
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(discovery)
        root = Path(mod.__file__).resolve().parents[2]
        # vault-data root byte-identical to the engine root when the env is unset;
        # all five constants derive from it.
        assert mod._ROOT == root
        assert mod.STATE_PATH == root / "Worker" / "_state" / "discovery_state.json"
        assert mod.REJECTIONS_PATH == root / "Worker" / "_state" / "discovery_rejected.jsonl"
        assert mod.PLANNER_STATE_PATH == root / "Worker" / "planner_state.json"
        assert mod.ITEMS_DIR == root / "Todo" / "items"
        assert mod.EPISODES_DIR == root / "Memory" / "episodes"
    importlib.reload(discovery)


def test_discovery_env_redirects_all_vault_data(monkeypatch, tmp_path):
    import todo_system.orchestrator.discovery as discovery
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(discovery)
        root = Path(mod.__file__).resolve().parents[2]
        # RED control: pre-migration these were stages.REPO_ROOT-anchored (env-
        # immune) and stayed at `root`; the redirect is the whole deliverable.
        assert mod.STATE_PATH == tmp_path / "Worker" / "_state" / "discovery_state.json"
        assert mod.REJECTIONS_PATH == tmp_path / "Worker" / "_state" / "discovery_rejected.jsonl"
        assert mod.PLANNER_STATE_PATH == tmp_path / "Worker" / "planner_state.json"
        assert mod.ITEMS_DIR == tmp_path / "Todo" / "items"
        assert mod.EPISODES_DIR == tmp_path / "Memory" / "episodes"
        assert mod._ROOT == tmp_path
        assert mod._ROOT != root
    importlib.reload(discovery)


@_vault_only_byte_identity
def test_discovery_data_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.orchestrator.discovery as discovery
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(discovery)
        # start= pins parents[2]; the cwd-upward .macp (vault_root='/bogus') must
        # not capture the data root — that is the whole reason start= is passed.
        assert mod._ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod._ROOT != Path("/bogus")
    importlib.reload(discovery)


@_vault_only_byte_identity
def test_followups_roots_no_env_byte_identical(monkeypatch):
    import todo_system.orchestrator.followups as followups
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(followups)
        root = Path(mod.__file__).resolve().parents[2]
        assert mod._ROOT == root
        assert mod.FOLLOWUPS_PATH == root / "Worker" / "_state" / "followups.jsonl"
        assert mod.REJECTIONS_PATH == root / "Worker" / "_state" / "followups_rejected.jsonl"
        assert mod.ITEMS_DIR == root / "Todo" / "items"
    importlib.reload(followups)


def test_followups_env_redirects_all_vault_data(monkeypatch, tmp_path):
    import todo_system.orchestrator.followups as followups
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(followups)
        root = Path(mod.__file__).resolve().parents[2]
        # RED control: pre-migration these stayed at the engine `root`.
        assert mod.FOLLOWUPS_PATH == tmp_path / "Worker" / "_state" / "followups.jsonl"
        assert mod.REJECTIONS_PATH == tmp_path / "Worker" / "_state" / "followups_rejected.jsonl"
        assert mod.ITEMS_DIR == tmp_path / "Todo" / "items"
        assert mod._ROOT == tmp_path
        assert mod._ROOT != root
    importlib.reload(followups)


@_vault_only_byte_identity
def test_followups_data_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    import todo_system.orchestrator.followups as followups
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(followups)
        assert mod._ROOT == Path(mod.__file__).resolve().parents[2]
        assert mod._ROOT != Path("/bogus")
    importlib.reload(followups)


# A slug that cannot exist in the live Todo/items — so the per-call RED control
# (run pre-migration, when the read still hit the engine tree) only ever attempts a
# guaranteed-absent file, never a real vault task.
_PROBE_SLUG = "zzz-macp-vault-root-per-call-probe-slug-do-not-create"


def test_orchestrator_task_title_reads_from_env_vault_per_call(monkeypatch, tmp_path):
    from todo_system.orchestrator.orchestrator import Orchestrator
    items = tmp_path / "Todo" / "items"
    items.mkdir(parents=True)
    (items / f"{_PROBE_SLUG}.md").write_text("# 제목 T\n\n본문\n", encoding="utf-8")
    # _task_title never touches self, so an unbound call keeps the test light and
    # avoids constructing an Orchestrator (needs a lease/mailbox/registry).
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        # RED control: pre-migration this read stages.REPO_ROOT (env-immune), opened
        # <engine>/Todo/items/<probe>.md — absent — and returned the slug, not "제목 T".
        assert Orchestrator._task_title(None, _PROBE_SLUG) == "제목 T"
    with monkeypatch.context() as m:
        # env points at an empty tree -> the md is absent -> slug is the fallback,
        # proving the read follows MACP_VAULT_ROOT and never the pinned engine root.
        m.setenv("MACP_VAULT_ROOT", str(tmp_path / "empty"))
        assert Orchestrator._task_title(None, _PROBE_SLUG) == _PROBE_SLUG


def _trace_captured_repo_dir(runs_dir):
    """Run trace.render with a spy branch_lookup and return the repo_dir it saw.

    ledger=None short-circuits the ledger section and runs_dir is an empty tmp dir,
    so nothing reads the live vault; repo_dir=None forces the fallback under test."""
    from todo_system.orchestrator import trace
    captured = []

    def spy_branch(repo_dir, branch):
        captured.append(repo_dir)
        return None

    trace.render("any-slug", ledger=None, runs_dir=runs_dir, repo_dir=None,
                 pr_lookup=lambda b: None, branch_lookup=spy_branch)
    return captured


def test_trace_repo_dir_fallback_reads_from_env_vault_per_call(monkeypatch, tmp_path):
    empty_runs = tmp_path / "runs"
    empty_runs.mkdir()
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        captured = _trace_captured_repo_dir(empty_runs)
    # repo_dir=None falls back to paths.vault_root(); under the env that is tmp_path.
    # RED control: pre-migration the fallback was stages.REPO_ROOT (engine root).
    assert captured == [tmp_path]


@_vault_only_byte_identity
def test_trace_repo_dir_fallback_no_env_is_engine_root(monkeypatch, tmp_path):
    from todo_system.orchestrator import trace
    empty_runs = tmp_path / "runs"
    empty_runs.mkdir()
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        captured = _trace_captured_repo_dir(empty_runs)
    # byte-identical to the pre-migration fallback (stages.REPO_ROOT) when unset.
    assert captured == [Path(trace.__file__).resolve().parents[2]]


# --- vault_root_or_cwd(): the base for relative CLI arguments ------------------
# The orchestrator/runner used to resolve --uri / --ledger / --db against
# Path.cwd() implicitly. vault_root_or_cwd() moves that base to the vault while
# keeping the cwd as the *fallback*, so an unresolvable install behaves exactly
# as it did before the change rather than failing at argument assembly.


def test_vault_root_or_cwd_returns_the_resolved_vault(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path))
    assert paths.vault_root_or_cwd() == tmp_path


def test_vault_root_or_cwd_falls_back_to_cwd_when_unresolvable(monkeypatch, tmp_path):
    def boom(start=None):
        raise paths.VaultRootError("no vault")
    monkeypatch.setattr(paths, "vault_root", boom)
    monkeypatch.chdir(tmp_path)
    # The fallback is what makes the migration byte-identical in the worst case.
    assert paths.vault_root_or_cwd() == Path.cwd()


def test_vault_root_or_cwd_propagates_other_errors(monkeypatch):
    def boom(start=None):
        raise OSError("disk")
    monkeypatch.setattr(paths, "vault_root", boom)
    # Only "cannot resolve" is a fallback case; real failures must stay loud.
    with pytest.raises(OSError):
        paths.vault_root_or_cwd()


# --- worker_root(): the external-repo clone root (was hard-coded ~/Worker) -----


def test_paths_worker_root_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_WORKER_ROOT", str(tmp_path / "w"))
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(
        "worker_root = '/from-config'\n", encoding="utf-8")
    assert paths.worker_root(start=tmp_path) == tmp_path / "w"


@requires_tomllib
def test_paths_worker_root_config_key_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_WORKER_ROOT", raising=False)
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(
        'worker_root = "/srv/clones"\n', encoding="utf-8")
    assert paths.worker_root(start=tmp_path) == Path("/srv/clones")


def test_paths_worker_root_marker_without_key_falls_through_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_WORKER_ROOT", raising=False)
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text("profile = 'test'\n", encoding="utf-8")
    # The historical default, byte-identical to the deleted DEFAULT_WORKER_ROOT.
    assert paths.worker_root(start=tmp_path) == Path.home() / "Worker"


def test_paths_worker_root_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_WORKER_ROOT", "~/clones")
    assert paths.worker_root(start=tmp_path) == Path.home() / "clones"


def test_paths_worker_root_anchors_on_vault_not_cwd(monkeypatch, tmp_path):
    """AC1 + AC3 together: resolvable from a cwd that is not the vault.

    A cwd-upward search (what label_prefix() does) would find no .macp marker
    under /tmp and silently take the default; anchoring on vault_root_or_cwd()
    reads the vault's key instead.
    """
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text(
        "worker_root = '/srv/clones'\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("MACP_WORKER_ROOT", raising=False)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    monkeypatch.chdir(elsewhere)
    assert paths.worker_root() == Path("/srv/clones")


# --- timezone(): the calendar zone (was four hard-coded "Asia/Seoul") ---------


def test_paths_timezone_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_TIMEZONE", "Europe/Berlin")
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(
        "timezone = 'Asia/Seoul'\n", encoding="utf-8")
    assert paths.timezone(start=tmp_path) == "Europe/Berlin"


@requires_tomllib
def test_paths_timezone_config_key_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_TIMEZONE", raising=False)
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(
        'timezone = "Asia/Seoul"\n', encoding="utf-8")
    assert paths.timezone(start=tmp_path) == "Asia/Seoul"


def test_paths_timezone_falls_back_to_host_tz_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_TIMEZONE", raising=False)
    monkeypatch.setenv("TZ", "America/New_York")
    assert paths.timezone(start=tmp_path) == "America/New_York"


def test_paths_timezone_ignores_posix_rule_tz(monkeypatch, tmp_path):
    """A TZ holding a POSIX rule string is not an IANA name — Google rejects it."""
    monkeypatch.delenv("MACP_TIMEZONE", raising=False)
    monkeypatch.setenv("TZ", "KST-9")
    monkeypatch.setattr(paths, "_LOCALTIME_PATH", tmp_path / "missing-localtime")
    assert paths.timezone(start=tmp_path) is None


def test_paths_timezone_reads_localtime_symlink(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    zoneinfo_dir = tmp_path / "zoneinfo"
    (zoneinfo_dir / "Europe").mkdir(parents=True)
    zone = zoneinfo_dir / "Europe" / "Berlin"
    zone.write_bytes(b"TZif")
    localtime = tmp_path / "localtime"
    localtime.symlink_to(zone)
    monkeypatch.setattr(paths, "_LOCALTIME_PATH", localtime)
    monkeypatch.setattr(paths, "_ZONEINFO_DIR", zoneinfo_dir)
    assert paths.timezone(start=tmp_path) == "Europe/Berlin"


def test_paths_timezone_is_none_when_nothing_answers(monkeypatch, tmp_path):
    """AC2's "no key → host local": observed as None, which calendar_sync turns
    into an *absent* timeZone key so Google applies the calendar's own zone."""
    monkeypatch.delenv("MACP_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(paths, "_LOCALTIME_PATH", tmp_path / "missing-localtime")
    assert paths.timezone(start=tmp_path) is None


def test_paths_timezone_none_when_localtime_is_outside_zoneinfo(monkeypatch, tmp_path):
    """A symlink pointing outside the tzdata tree yields no name, not a crash."""
    monkeypatch.delenv("MACP_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    other = tmp_path / "elsewhere"
    other.write_bytes(b"TZif")
    localtime = tmp_path / "localtime"
    localtime.symlink_to(other)
    monkeypatch.setattr(paths, "_LOCALTIME_PATH", localtime)
    monkeypatch.setattr(paths, "_ZONEINFO_DIR", tmp_path / "zoneinfo")
    assert paths.timezone(start=tmp_path) is None


def test_paths_timezone_anchors_on_vault_not_cwd(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text(
        "timezone = 'Europe/Berlin'\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("MACP_TIMEZONE", raising=False)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    monkeypatch.chdir(elsewhere)
    assert paths.timezone() == "Europe/Berlin"


# --- ui_language(): engine log/CLI text language (ko | en) -------------------


def _language_config(tmp_path, text):
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(text, encoding="utf-8")


def test_ui_language_env_wins_over_config(monkeypatch, tmp_path):
    _language_config(tmp_path, 'language = "en"\n')
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert paths.ui_language(start=tmp_path) == "ko"


@requires_tomllib
def test_ui_language_env_en_wins_over_config_ko(monkeypatch, tmp_path):
    _language_config(tmp_path, 'language = "ko"\n')
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert paths.ui_language(start=tmp_path) == "en"


@requires_tomllib
def test_ui_language_config_key_used_without_env(monkeypatch, tmp_path):
    _language_config(tmp_path, 'language = "ko"\n')
    monkeypatch.delenv("MACP_LANGUAGE", raising=False)
    assert paths.ui_language(start=tmp_path) == "ko"


def test_ui_language_default_is_en(monkeypatch, tmp_path):
    # The marker alone (no `language` key) stops the walk and takes the default;
    # this is the vault-template shape.
    monkeypatch.delenv("MACP_LANGUAGE", raising=False)
    _language_config(tmp_path, "profile = 'test'\n")
    assert paths.ui_language(start=tmp_path) == "en"


def test_ui_language_default_when_no_config_anywhere(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_LANGUAGE", raising=False)
    monkeypatch.setattr(paths, "_read_config_key",
                        lambda *a: pytest.fail("no config.toml should be found"))
    monkeypatch.setattr(paths.Path, "is_file", lambda self: False)
    assert paths.ui_language(start=tmp_path) == "en"


@requires_tomllib
def test_ui_language_unknown_value_is_en(monkeypatch, tmp_path):
    _language_config(tmp_path, 'language = "ko"\n')
    monkeypatch.setenv("MACP_LANGUAGE", "fr")
    assert paths.ui_language(start=tmp_path) == "en"   # env wins first, then the whitelist

    other = tmp_path / "other"
    other.mkdir()
    _language_config(other, 'language = "jp"\n')
    monkeypatch.delenv("MACP_LANGUAGE", raising=False)
    assert paths.ui_language(start=other) == "en"


def test_ui_language_normalises_case_and_space(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", " KO ")
    assert paths.ui_language(start=tmp_path) == "ko"


def test_ui_language_bad_toml_falls_back_to_en(monkeypatch, tmp_path):
    _language_config(tmp_path, "language = [\n")
    monkeypatch.delenv("MACP_LANGUAGE", raising=False)
    assert paths.ui_language(start=tmp_path) == "en"


@requires_tomllib
def test_ui_language_anchors_at_vault_root_not_cwd(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _language_config(vault, 'language = "ko"\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("MACP_LANGUAGE", raising=False)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    monkeypatch.chdir(elsewhere)
    assert paths.ui_language() == "ko"


def test_ui_language_has_no_import_time_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert paths.ui_language(start=tmp_path) == "ko"
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert paths.ui_language(start=tmp_path) == "en"
