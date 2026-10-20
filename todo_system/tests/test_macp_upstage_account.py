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
import io
import json
import os
import socket
import urllib.error
from contextlib import redirect_stdout
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


# ---- Phase 2: profile schema, tier mapping, selection invariance ------------

def test_profile_roundtrip_carries_key_file_and_models_not_the_key(C, tmp_path):
    f = tmp_path / "up.env"
    prof = C.Profile(name="p", accounts=[
        C.Account(name="cc", agent="claude"),
        C.Account(name="solar", agent="upstage", priority=150, key_file=str(f),
                  models={"light": "solar-mini4-pinned"}),
    ])
    path = C.write_profile(prof)
    text = path.read_text(encoding="utf-8")
    assert f'key_file = "{f}"' in text
    assert 'models = { light = "solar-mini4-pinned" }' in text
    assert SENTINEL not in text                    # the path is stored, never a key
    loaded = C.load_profile("p")
    solar = loaded.accounts[1]
    assert (solar.agent, solar.priority, solar.key_file) == ("upstage", 150, str(f))
    assert solar.models == {"light": "solar-mini4-pinned"}
    assert loaded.accounts[0].key_file == "" and loaded.accounts[0].models == {}


def test_claude_only_profile_output_is_byte_identical(C):
    # the new fields are written only when set, so an existing profile's TOML
    # does not change at all (the historical text the schema test pins)
    path = C.write_profile(C.Profile(name="p", accounts=[C.Account(name="x")]))
    assert path.read_text(encoding="utf-8") == "\n".join([
        'name = "p"', 'vault = ""', 'daily_run_budget = 40',
        'max_parallel_per_repo = 2', 'model = "auto"', '', '[[account]]',
        'name = "x"', 'agent = "claude"',
        f'claude_config_dir = "{ACC.LEGACY_CONFIG_DIR}"', '',
    ])


def test_solar_model_for_defaults_are_measured_pins(C):
    assert C.solar_model_for(None, "light") == "solar-mini4-260922"
    assert C.solar_model_for(None, "standard") == "solar-pro4-260806"
    assert C.solar_model_for(None, "heavy") == "solar-pro4-260806"
    # an unknown tier degrades to standard instead of raising
    assert C.solar_model_for(None, "brand-new-tier") == C.SOLAR_TIER_MODELS["standard"]


def test_solar_model_mapping_is_configurable_per_account(C):
    acct = C.Account(name="solar", agent="upstage",
                     models={"light": "solar-mini-next", "heavy": "solar-pro-next"})
    assert C.solar_model_for(acct, "light") == "solar-mini-next"
    assert C.solar_model_for(acct, "heavy") == "solar-pro-next"
    # a tier the override omits still gets the default pin
    assert C.solar_model_for(acct, "standard") == C.SOLAR_TIER_MODELS["standard"]
    # and the override survives a write → load round-trip (config, not code)
    C.write_profile(C.Profile(name="p", accounts=[acct]))
    assert C.solar_model_for(C.load_profile("p").accounts[0], "light") == "solar-mini-next"


def test_account_for_round_robin_unchanged_by_an_upstage_account(C):
    """AC #4: the claude mapping is `hash(task_id) % len(candidates)`, so adding
    one non-claude account must not re-shuffle it."""
    claude = [C.Account(name="school", agent="claude"),
              C.Account(name="dev", agent="claude")]
    before = C.Profile(name="before", accounts=list(claude))
    after = C.Profile(name="after", accounts=[*claude,
                                              C.Account(name="solar", agent="upstage")])
    for task in ("task-1", "t", "macp-upstage-solar-worker-account", "x" * 40):
        assert C.account_for(before, None, task).name == C.account_for(after, None, task).name
    assert C.account_for(after, None).name == "school"            # no task_id → first claude
    pinned = C.ProjectConfig(profile="after", account="dev")
    assert C.account_for(after, pinned).name == "dev"             # pin still wins
    # an upstage account is never returned on the claude path, even pinned…
    assert C.account_for(after, C.ProjectConfig(account="solar"), "t").agent == "claude"
    # …but is reachable by asking for it
    assert C.account_for(after, None, "t", agent="upstage").name == "solar"
    assert C.account_for(C.Profile(name="only-upstage", accounts=[
        C.Account(name="solar", agent="upstage")]), None, "t") is None


def test_build_invocation_never_launches_claude_for_an_upstage_account(C, tmp_path,
                                                                      monkeypatch):
    acct = C.Account(name="solar", agent="upstage")
    with pytest.raises(ValueError, match="no CLI command"):
        C.build_invocation(acct, "do it")
    # with a command it takes the vendor branch and the key arrives at CALL time
    f = _key_file(tmp_path / "up.env", f"{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n")
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(f))
    cli = C.Account(name="solar", agent="upstage", command="solar-cli {prompt}")
    argv, env = C.build_invocation(cli, "do it", ["--model", "x"])
    assert argv == ["solar-cli", "do it"]                 # claude flags ignored
    assert env[ACC.UPSTAGE_KEY_ENV] == SENTINEL
    assert "CLAUDE_CONFIG_DIR" not in env
    # no key anywhere → no variable at all (not an empty one)
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(tmp_path / "absent.env"))
    _, env2 = C.build_invocation(cli, "do it")
    assert ACC.UPSTAGE_KEY_ENV not in env2
    # the key is never written back onto the account or its profile
    assert cli.env == {} and cli.key_file == ""


def test_build_invocation_claude_path_unchanged(C):
    argv, env = C.build_invocation(C.Account(name="x"), "hi")
    assert argv == ["claude", "-p", "hi"] and "CLAUDE_CONFIG_DIR" not in env
    cx = C.Account(name="cx", agent="codex", command="codex exec {prompt}")
    assert C.build_invocation(cx, "hi", ["--model", "m"])[0] == ["codex", "exec", "hi"]


# ---- Phase 3: CLI ------------------------------------------------------------

@pytest.fixture
def cli(C, monkeypatch):
    """``macp.__main__.main(argv)`` → (rc, stdout). The CLI module caches nothing,
    but it reads the reloaded config module, so the fixture depends on ``C``."""
    from todo_system.macp import __main__ as M
    importlib.reload(M)

    def run(*argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = M.main(list(argv))
        return rc, out.getvalue()
    return run


def test_account_add_upstage_registers_and_lists(cli, C, tmp_path):
    f = _key_file(tmp_path / "up.env", f"{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n")
    assert cli("profile", "add", "work")[0] == 0
    rc, out = cli("account", "add", "solar", "--profile", "work", "--agent", "upstage",
                  "--priority", "150", "--key-file", str(f),
                  "--models", "light=solar-mini4-260922")
    assert rc == 0
    assert "added account 'solar' [upstage]" in out
    assert SENTINEL not in out                     # AC #2 — not even on the add line

    # the profile TOML holds the metadata and NOT the key
    text = (C.PROFILES / "work" / "config.toml").read_text(encoding="utf-8")
    assert 'agent = "upstage"' in text and "priority = 150" in text
    assert f'key_file = "{f}"' in text
    assert SENTINEL not in text

    rc, out = cli("account", "list", "--profile", "work")
    assert rc == 0
    line = next(ln for ln in out.splitlines() if "solar [upstage]" in ln)
    assert "priority=150" in line
    assert "key=file" in line                      # origin only…
    assert SENTINEL not in out                     # …never the value
    assert str(f) not in out                       # nor the key file's path
    assert "models=solar-pro4-260806/solar-pro4-260806/solar-mini4-260922" in line


def test_account_list_reports_key_from_env_and_missing(cli, C, tmp_path, monkeypatch):
    assert cli("profile", "add", "work")[0] == 0
    assert cli("account", "add", "solar", "--profile", "work", "--agent", "upstage")[0] == 0
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(tmp_path / "absent.env"))
    assert "key=missing" in cli("account", "list", "--profile", "work")[1]
    monkeypatch.setenv(ACC.UPSTAGE_KEY_ENV, SENTINEL)
    rc, out = cli("account", "list", "--profile", "work")
    assert rc == 0 and "key=env" in out and SENTINEL not in out


def test_upstage_priority_sorts_behind_the_claude_default(cli, C):
    """if_then: start the Upstage account BELOW the default — and selection is
    lowest-number-first, so 150 > the claude default 100 is what that means."""
    assert cli("profile", "add", "work")[0] == 0
    assert cli("account", "add", "cc", "--profile", "work",
               "--config-dir", "/tmp/cc-cfg")[0] == 0
    assert cli("account", "add", "solar", "--profile", "work", "--agent", "upstage",
               "--priority", "150")[0] == 0
    prof = C.load_profile("work")
    assert [(x.name, x.priority) for x in prof.accounts] == [("cc", 100), ("solar", 150)]
    # across every account, and among claude accounts, the upstage one is last
    assert ACC.pick_account(prof.accounts, agent=None).name == "cc"
    assert ACC.pick_account(prof.accounts).name == "cc"


def test_account_add_priority_defaults_to_100(cli, C):
    assert cli("profile", "add", "work")[0] == 0
    assert cli("account", "add", "solar", "--profile", "work", "--agent", "upstage")[0] == 0
    acct = C.load_profile("work").accounts[0]
    assert acct.priority == 100 and acct.key_file == "" and acct.models == {}


@pytest.mark.parametrize("spec", ["light", "=x", "light=", "nosuchtier=m"])
def test_account_add_rejects_a_bad_models_spec(cli, C, spec):
    assert cli("profile", "add", "work")[0] == 0
    rc, out = cli("account", "add", "solar", "--profile", "work",
                  "--agent", "upstage", "--models", spec)
    assert rc == 2
    assert "--models" in out
    assert C.load_profile("work").accounts == []   # nothing written on a bad spec


def test_account_list_non_claude_short_form_unchanged(cli, C):
    assert cli("profile", "add", "work")[0] == 0
    assert cli("account", "add", "cx", "--profile", "work", "--agent", "codex",
               "--command", "codex exec {prompt}")[0] == 0
    assert "cx [codex]: codex exec {prompt}" in cli("account", "list", "--profile", "work")[1]


# ---- Phase 4: the HTTP client + `account probe` ------------------------------
#
# Double-defended against hitting the real endpoint: urlopen is replaced in
# every case AND the base URL points at a port nothing listens on, so a stub
# that fails to install produces a connection error, not a live API call.

class _Resp:
    """The context-manager shape `urllib.request.urlopen` returns."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


OK_PAYLOAD = {
    "model": "solar-mini4-260922",
    "choices": [{"message": {"role": "assistant", "content": "pong"}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
}


@pytest.fixture
def UP(home, monkeypatch):
    """``macp.upstage`` with the base URL pinned to a dead port."""
    from todo_system.macp import upstage as mod
    monkeypatch.setenv(mod.API_BASE_ENV, "http://127.0.0.1:1/v1")
    return mod


def _stub_urlopen(monkeypatch, UP, handler):
    """Replace urlopen and record the single request the client makes."""
    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.data.decode())
        seen["timeout"] = timeout
        return handler()

    monkeypatch.setattr(UP.urllib.request, "urlopen", fake)
    return seen


def test_chat_success_reports_model_tokens_and_elapsed(UP, monkeypatch):
    seen = _stub_urlopen(monkeypatch, UP, lambda: _Resp(OK_PAYLOAD))
    res = UP.chat("ping", api_key=SENTINEL, tier="light")
    assert res["ok"] is True and res["error_kind"] is None
    assert res["model"] == "solar-mini4-260922"
    assert (res["prompt_tokens"], res["completion_tokens"], res["total_tokens"]) == (7, 3, 10)
    assert isinstance(res["elapsed_s"], float) and res["elapsed_s"] >= 0
    # the request itself: OpenAI-compatible shape, key in the header only
    assert seen["url"] == "http://127.0.0.1:1/v1/chat/completions"
    assert seen["body"]["model"] == "solar-mini4-260922"
    assert seen["body"]["messages"] == [{"role": "user", "content": "ping"}]
    assert seen["headers"]["Authorization"] == f"Bearer {SENTINEL}"
    assert SENTINEL not in json.dumps(seen["body"])


def test_chat_tier_and_account_override_pick_the_model(UP, C, monkeypatch):
    _stub_urlopen(monkeypatch, UP, lambda: _Resp(dict(OK_PAYLOAD, model="")))
    assert UP.chat("x", api_key=SENTINEL, tier="heavy")["model"] == "solar-pro4-260806"
    acct = C.Account(name="solar", agent="upstage", models={"light": "solar-override"})
    assert UP.chat("x", account=acct, api_key=SENTINEL)["model"] == "solar-override"


@pytest.mark.parametrize("status,kind", [
    (401, "auth"), (403, "auth"), (429, "rate_limit"), (500, "http"), (404, "http"),
])
def test_chat_classifies_http_failures(UP, monkeypatch, status, kind):
    def raise_http():
        raise urllib.error.HTTPError(
            "http://x/v1/chat/completions", status, "nope", {},
            io.BytesIO(b'{"error":"denied"}'))

    _stub_urlopen(monkeypatch, UP, raise_http)
    res = UP.chat("ping", api_key=SENTINEL)
    assert res["ok"] is False and res["error_kind"] == kind
    assert str(status) in res["error"]
    assert res["total_tokens"] is None


def test_chat_classifies_timeout_and_network(UP, monkeypatch):
    def raise_timeout():
        raise socket.timeout("timed out")

    _stub_urlopen(monkeypatch, UP, raise_timeout)
    assert UP.chat("ping", api_key=SENTINEL, timeout=5)["error_kind"] == "timeout"

    def raise_wrapped_timeout():
        raise urllib.error.URLError(socket.timeout("timed out"))

    _stub_urlopen(monkeypatch, UP, raise_wrapped_timeout)
    assert UP.chat("ping", api_key=SENTINEL)["error_kind"] == "timeout"

    def raise_refused():
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    _stub_urlopen(monkeypatch, UP, raise_refused)
    assert UP.chat("ping", api_key=SENTINEL)["error_kind"] == "network"


def test_chat_flags_an_unexpected_body(UP, monkeypatch):
    _stub_urlopen(monkeypatch, UP, lambda: _Resp({"unexpected": True}))
    res = UP.chat("ping", api_key=SENTINEL)
    assert res["ok"] is False and res["error_kind"] == "bad_response"


def test_chat_without_a_key_calls_nothing(UP, tmp_path, monkeypatch):
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(tmp_path / "absent.env"))

    def boom(req, timeout=None):                   # must not be reached
        raise AssertionError("urlopen called without a key")

    monkeypatch.setattr(UP.urllib.request, "urlopen", boom)
    res = UP.chat("ping")
    assert res["error_kind"] == "no_key" and res["ok"] is False
    assert ACC.UPSTAGE_KEY_ENV in res["error"]


def test_chat_scrubs_the_key_out_of_an_echoing_server(UP, monkeypatch):
    """A server that quotes the credential it rejected must not turn the key
    into a return value — the result is printed as JSON by `account probe`."""
    def raise_echo():
        raise urllib.error.HTTPError(
            "http://x", 401, "unauthorized", {},
            io.BytesIO(json.dumps({"error": f"bad key {SENTINEL}"}).encode()))

    _stub_urlopen(monkeypatch, UP, raise_echo)
    res = UP.chat("ping", api_key=SENTINEL)
    assert res["error_kind"] == "auth"
    assert SENTINEL not in json.dumps(res)
    assert UP.REDACTED in res["error"]
    # and on the success path, a reply that echoes the key is not returned at all
    _stub_urlopen(monkeypatch, UP, lambda: _Resp(dict(
        OK_PAYLOAD, choices=[{"message": {"content": f"your key is {SENTINEL}"}}])))
    assert SENTINEL not in json.dumps(UP.chat("ping", api_key=SENTINEL))


def test_probe_skips_with_rc3_and_names_only_the_path(cli, C, UP, tmp_path, monkeypatch):
    keyfile = tmp_path / "nowhere" / "upstage.env"
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(keyfile))
    assert cli("profile", "add", "work")[0] == 0
    assert cli("account", "add", "solar", "--profile", "work", "--agent", "upstage")[0] == 0
    rc, out = cli("account", "probe", "solar", "--profile", "work", "--tier", "light")
    assert rc == 3
    assert "probe: skipped" in out and str(keyfile) in out
    assert SENTINEL not in out


def test_probe_prints_one_json_line_on_success(cli, C, UP, tmp_path, monkeypatch):
    _key_file(tmp_path / "up.env", f"{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n")
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(tmp_path / "up.env"))
    _stub_urlopen(monkeypatch, UP, lambda: _Resp(OK_PAYLOAD))
    assert cli("profile", "add", "work")[0] == 0
    assert cli("account", "add", "solar", "--profile", "work", "--agent", "upstage")[0] == 0
    rc, out = cli("account", "probe", "solar", "--profile", "work")
    assert rc == 0
    line = out.strip().splitlines()[-1]
    got = json.loads(line)                         # exactly one JSON object
    assert got["ok"] is True and got["tier"] == "light" and got["account"] == "solar"
    assert got["total_tokens"] == 10 and got["error_kind"] is None
    assert SENTINEL not in out


def test_probe_rc1_on_a_failed_call(cli, C, UP, tmp_path, monkeypatch):
    _key_file(tmp_path / "up.env", f"{ACC.UPSTAGE_KEY_ENV}={SENTINEL}\n")
    monkeypatch.setenv(ACC.UPSTAGE_KEY_FILE_ENV, str(tmp_path / "up.env"))

    def raise_429():
        raise urllib.error.HTTPError("http://x", 429, "slow down", {}, io.BytesIO(b"{}"))

    _stub_urlopen(monkeypatch, UP, raise_429)
    assert cli("profile", "add", "work")[0] == 0
    assert cli("account", "add", "solar", "--profile", "work", "--agent", "upstage")[0] == 0
    rc, out = cli("account", "probe", "solar", "--profile", "work")
    assert rc == 1
    assert json.loads(out.strip().splitlines()[-1])["error_kind"] == "rate_limit"


def test_probe_rejects_a_non_upstage_or_unknown_account(cli, C):
    assert cli("profile", "add", "work")[0] == 0
    assert cli("account", "add", "cc", "--profile", "work",
               "--config-dir", "/tmp/cc-cfg")[0] == 0
    rc, out = cli("account", "probe", "cc", "--profile", "work")
    assert rc == 2 and "needs an upstage account" in out
    rc, out = cli("account", "probe", "ghost", "--profile", "work")
    assert rc == 2 and "no account 'ghost'" in out
