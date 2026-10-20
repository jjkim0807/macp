"""Upstage (Solar) as a worker account — key handling, profile schema, CLI, probe.

Task ``macp-upstage-solar-worker-account``. The whole point of this layer is that
an Upstage account carries *where* its key lives and never the key itself, so most
assertions here are negative: a sentinel key string is planted in the env / a key
file and must not appear in the profile TOML, in ``account list`` output, in
``account probe`` output, or in a returned error. Hermetic: ``MACP_HOME``, ``HOME``
and the key-file env all point into ``tmp_path``, and the HTTP client is reached
only through a stubbed ``urlopen`` with the base URL pointed at a dead port, so no
test can touch the real endpoint or the live ``~/.config/macp/upstage.env``.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from todo_system.macp import accounts as ACC

SENTINEL = "up_SENTINELKEY0123456789"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp HOME + MACP_HOME with the key env cleared, so nothing resolves to
    the real profile or the real key file. Returns the home dir."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("MACP_HOME", str(tmp_path / "macp-home"))
    monkeypatch.delenv(ACC.UPSTAGE_KEY_ENV, raising=False)
    monkeypatch.delenv(ACC.UPSTAGE_KEY_FILE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return h


@pytest.fixture
def C(home):
    """``macp.config`` reloaded against the tmp MACP_HOME (it caches HOME at
    import, as the other macp tests do)."""
    from todo_system.macp import config as mod
    importlib.reload(mod)
    yield mod
    importlib.reload(mod)


def _key_file(path: Path, body: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


# ---- Phase 1: key resolution (env → file), value never surfaced -------------

def test_key_read_from_env_first(home, tmp_path, monkeypatch):
    f = _key_file(tmp_path / "k.env", f"{ACC.UPSTAGE_KEY_ENV}=from-file\n")
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(f))
    monkeypatch.setenv(ACC.UPSTAGE_KEY_ENV, SENTINEL)
    assert ACC.read_api_key() == SENTINEL
    assert ACC.key_source() == "env"


def test_key_falls_back_to_file_then_missing(home, tmp_path, monkeypatch):
    f = _key_file(tmp_path / "k.env", f"{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n")
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(f))
    assert ACC.read_api_key() == SENTINEL          # no env var → the file answers
    assert ACC.key_source() == "file"
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(tmp_path / "absent.env"))
    assert ACC.read_api_key() is None              # neither → None, no raise
    assert ACC.key_source() == "missing"


def test_empty_env_value_does_not_shadow_the_file(home, tmp_path, monkeypatch):
    f = _key_file(tmp_path / "k.env", f"{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n")
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(f))
    monkeypatch.setenv(ACC.UPSTAGE_KEY_ENV, "   ")
    assert ACC.read_api_key() == SENTINEL
    assert ACC.key_source() == "file"


@pytest.mark.parametrize("body", [
    f"{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n",
    f"  {ACC.UPSTAGE_KEY_ENV} = {SENTINEL}  \n",
    f"export {ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n",
    f'{ACC.UPSTAGE_KEY_ENV}="{SENTINEL}"\n',
    f"{ACC.UPSTAGE_KEY_ENV}='{SENTINEL}'\n",
    f"# a comment\n\nOTHER=x\n{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\nTRAILING=y\n",
])
def test_key_file_line_parsing(home, tmp_path, body):
    f = _key_file(tmp_path / "k.env", body)
    assert ACC.read_key_file(f) == SENTINEL


@pytest.mark.parametrize("body", [
    "",
    "# only a comment\n",
    "OTHER_KEY=x\n",
    f"#{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n",      # commented out = absent
    f"{ACC.UPSTAGE_KEY_ENV}=\n",                  # present but empty
])
def test_key_file_without_the_variable_is_none(home, tmp_path, body):
    f = _key_file(tmp_path / "k.env", body)
    assert ACC.read_key_file(f) is None


def test_key_file_path_resolution_order(home, tmp_path, monkeypatch):
    class _A:                                      # key_file is duck-typed
        key_file = str(tmp_path / "on-account.env")

    # 3) nothing anywhere → the documented default, expanded against HOME
    assert ACC.upstage_key_file(start=tmp_path) == \
        Path(os.path.expanduser(ACC.DEFAULT_UPSTAGE_KEY_FILE))
    # 2) a project config key beats the default (relative to the config's dir)
    proj = tmp_path / "proj"
    (proj / ".macp").mkdir(parents=True)
    (proj / ".macp" / "config.toml").write_text(
        'upstage_key_file = "secrets/up.env"\n', encoding="utf-8")
    assert ACC.upstage_key_file(start=proj) == proj / "secrets" / "up.env"
    # 1.5) the account's own key_file beats the project config
    assert ACC.upstage_key_file(_A(), start=proj) == Path(_A.key_file)
    # 1) the env beats everything
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(tmp_path / "env.env"))
    assert ACC.upstage_key_file(_A(), start=proj) == tmp_path / "env.env"


def test_loose_key_file_warns_on_stderr_without_the_value(home, tmp_path, capsys):
    f = _key_file(tmp_path / "k.env", f"{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n", mode=0o644)
    assert ACC.read_key_file(f) == SENTINEL        # still readable
    err = capsys.readouterr().err
    assert "group/other-readable" in err and str(f) in err
    assert SENTINEL not in err                    # the path, never the key


def test_key_helpers_accept_an_explicit_env_mapping(home, tmp_path, monkeypatch):
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(tmp_path / "absent.env"))
    env = {ACC.UPSTAGE_KEY_ENV: SENTINEL}
    assert ACC.read_api_key(env=env) == SENTINEL   # os.environ is not consulted
    assert ACC.key_source(env=env) == "env"
    assert ACC.key_source(env={}) == "missing"
