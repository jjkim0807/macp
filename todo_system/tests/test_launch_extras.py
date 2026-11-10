"""Unit tests for ``launch_extras`` — the launch-intent extras loader.

Every case is self-contained under ``tmp_path``: the "vault" is a tmp dir and the
secret JSON is written by the test, so nothing reads the real vault and no case
depends on the orchestrator suite's ``vault-only`` skip marker.

Two invariants carry the most weight:

* **Unconfigured is byte-identical.** ``resolve`` on an empty/absent declaration
  yields no args and no note, and ``am_start_command`` returns ``None`` — which
  is what makes the callers keep their existing ``monkey`` command line.
* **A note never carries a value.** Every skip reason is asserted to contain the
  file path and/or field name and to NOT contain the secret value.

Only generic identifiers appear here (``com.example.app``, ``.MainActivity``,
``.secrets/demo-app.json``) — this file sits inside the identifier gate's tree.
"""

from __future__ import annotations

import json

from todo_system.orchestrator import launch_extras

_SECRET = "s3cret-session-token-value"
_PKG = "com.example.app"
_ACT = ".MainActivity"


def _write_secret(tmp_path, body=None, *, rel="/".join([".secrets", "demo-app.json"])):
    """Write a secret JSON into the fake vault and return its vault-relative path."""
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"session_token": _SECRET} if body is None else body),
                 encoding="utf-8")
    return rel


def _string_extra(rel):
    return [{"type": "string", "key": "session_token",
             "value_from_secret": f"{rel}#session_token"}]


# ---------------------------------------------------------------------------
# declared() — the three config positions
# ---------------------------------------------------------------------------

def test_declared_prefers_screen_check_then_smoke_then_top_level():
    item = [{"type": "bool", "key": "a", "value": True}]
    other = [{"type": "bool", "key": "b", "value": True}]
    assert launch_extras.declared({"screen_check": {"launch_extras": item},
                                   "smoke": {"launch_extras": other},
                                   "launch_extras": other}) == item
    assert launch_extras.declared({"smoke": {"launch_extras": item},
                                   "launch_extras": other}) == item
    # emulator_cli hands beta_smoke a repo scenario JSON — keys at the top level
    assert launch_extras.declared({"launch_extras": item}) == item


def test_declared_empty_for_every_unconfigured_shape():
    for cfg in (None, {}, "nope", 7, {"screen_check": {}}, {"launch_extras": "x"},
                {"screen_check": "x", "smoke": None}):
        assert launch_extras.declared(cfg) == []


def test_activity_read_from_the_same_three_positions():
    assert launch_extras.activity({"screen_check": {"activity": _ACT}}) == _ACT
    assert launch_extras.activity({"smoke": {"activity": _ACT}}) == _ACT
    assert launch_extras.activity({"activity": f"  {_ACT} "}) == _ACT
    assert launch_extras.activity({}) == ""
    assert launch_extras.activity(None) == ""
    assert launch_extras.activity({"activity": 5}) == ""


# ---------------------------------------------------------------------------
# resolve() — the three types, and the unconfigured no-op
# ---------------------------------------------------------------------------

def test_unconfigured_resolves_to_nothing_at_all(tmp_path):
    res = launch_extras.resolve({}, vault_root=tmp_path)
    assert res.args == []
    assert res.secrets == []
    assert res.note is None
    # …and with no args there is no am start command, so the caller keeps monkey
    assert launch_extras.am_start_command(_PKG, _ACT, res.args) is None


def test_three_types_render_their_own_flags(tmp_path):
    rel = _write_secret(tmp_path)
    cfg = {"screen_check": {"launch_extras": [
        {"type": "string", "key": "session_token", "value_from_secret": f"{rel}#session_token"},
        {"type": "bool", "key": "skip_onboarding", "value": True},
        {"type": "int", "key": "start_tab", "value": 2},
    ]}}
    res = launch_extras.resolve(cfg, vault_root=tmp_path)

    assert res.note is None
    assert res.args == ["--es", "session_token", _SECRET,
                        "--ez", "skip_onboarding", "true",
                        "--ei", "start_tab", "2"]
    assert res.secrets == [_SECRET]


def test_bool_and_int_accept_string_forms_and_reject_junk(tmp_path):
    def one(kind, value):
        return launch_extras.resolve(
            {"launch_extras": [{"type": kind, "key": "k", "value": value}]},
            vault_root=tmp_path)

    assert one("bool", "TRUE").args == ["--ez", "k", "true"]
    assert one("bool", "false").args == ["--ez", "k", "false"]
    assert one("int", "42").args == ["--ei", "k", "42"]
    # junk is a skip with a note, never a crash and never a guessed value
    for kind, value in (("bool", "yes"), ("bool", 1), ("int", "1.5"), ("int", True)):
        res = one(kind, value)
        assert res.args == [], (kind, value)
        assert res.note and "k" in res.note


def test_default_type_is_string(tmp_path):
    res = launch_extras.resolve({"launch_extras": [{"key": "mode", "value": "demo"}]},
                                vault_root=tmp_path)
    assert res.args == ["--es", "mode", "demo"]


def test_unknown_type_is_a_skip_not_a_guess(tmp_path):
    res = launch_extras.resolve(
        {"launch_extras": [{"type": "float", "key": "k", "value": 1}]},
        vault_root=tmp_path)
    assert res.args == []
    assert res.note and "float" in res.note


def test_a_value_with_shell_metacharacters_is_quoted(tmp_path):
    rel = _write_secret(tmp_path, {"session_token": "a b; rm -rf /"})
    res = launch_extras.resolve({"launch_extras": _string_extra(rel)}, vault_root=tmp_path)
    # quoted exactly once, so the node shell and the device `sh -c` see one token
    assert res.args[:2] == ["--es", "session_token"]
    assert res.args[2] == "'a b; rm -rf /'"


def test_one_bad_item_drops_the_whole_set(tmp_path):
    rel = _write_secret(tmp_path)
    cfg = {"launch_extras": [
        {"type": "string", "key": "session_token", "value_from_secret": f"{rel}#session_token"},
        {"type": "bool", "key": "broken", "value": "maybe"},
    ]}
    res = launch_extras.resolve(cfg, vault_root=tmp_path)
    # all-or-nothing: a half-applied set would launch a state no one declared
    assert res.args == []
    assert res.note
    assert _SECRET not in res.note


# ---------------------------------------------------------------------------
# Secrets: clean skip + no value in any note
# ---------------------------------------------------------------------------

def test_missing_secret_file_is_a_clean_skip_with_a_note(tmp_path):
    rel = "/".join([".secrets", "demo-app.json"])          # never written
    res = launch_extras.resolve({"launch_extras": _string_extra(rel)}, vault_root=tmp_path)

    assert res.args == []                                   # → caller keeps monkey
    assert res.secrets == []
    assert res.note and rel in res.note and "session_token" in res.note


def test_missing_absent_blank_and_non_string_fields_all_skip(tmp_path):
    for body in ({}, {"session_token": ""}, {"session_token": "   "},
                 {"session_token": 5}, {"other": _SECRET}):
        rel = _write_secret(tmp_path, body)
        res = launch_extras.resolve({"launch_extras": _string_extra(rel)},
                                    vault_root=tmp_path)
        assert res.args == [], body
        assert res.note and "session_token" in res.note
        assert _SECRET not in res.note


def test_unreadable_and_non_object_secret_json_skip(tmp_path):
    rel = "/".join([".secrets", "demo-app.json"])
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)

    p.write_text("not json at all", encoding="utf-8")
    res = launch_extras.resolve({"launch_extras": _string_extra(rel)}, vault_root=tmp_path)
    assert res.args == [] and res.note and rel in res.note

    p.write_text('["a"]', encoding="utf-8")
    res = launch_extras.resolve({"launch_extras": _string_extra(rel)}, vault_root=tmp_path)
    assert res.args == [] and res.note and rel in res.note


def test_secret_path_cannot_escape_the_vault(tmp_path):
    outside = tmp_path.parent / "outside-secret.json"
    outside.write_text(json.dumps({"session_token": _SECRET}), encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()

    res = launch_extras.resolve(
        {"launch_extras": [{"type": "string", "key": "session_token",
                            "value_from_secret": "../outside-secret.json#session_token"}]},
        vault_root=vault)

    assert res.args == []
    assert res.note and "escapes the vault" in res.note
    assert _SECRET not in res.note


def test_malformed_secret_spec_and_missing_value_skip(tmp_path):
    for item in ({"type": "string", "key": "k", "value_from_secret": "no-hash-here"},
                 {"type": "string", "key": "k", "value_from_secret": "#field"},
                 {"type": "string", "key": "k", "value_from_secret": "file.json#"},
                 {"type": "string", "key": "k"}):
        res = launch_extras.resolve({"launch_extras": [item]}, vault_root=tmp_path)
        assert res.args == [], item
        assert res.note


# ---------------------------------------------------------------------------
# Identifier allow-lists (key / pkg / activity reach two shells)
# ---------------------------------------------------------------------------

def test_bad_keys_are_rejected(tmp_path):
    for key in ("", "  ", "has space", "quo'te", "semi;colon", "$(id)", "9leading",
                "dash-key", 5, None, "a/b"):
        res = launch_extras.resolve(
            {"launch_extras": [{"type": "string", "key": key, "value": "v"}]},
            vault_root=tmp_path)
        assert res.args == [], key
        assert res.note


def test_non_object_item_is_rejected(tmp_path):
    res = launch_extras.resolve({"launch_extras": ["session_token"]}, vault_root=tmp_path)
    assert res.args == [] and res.note


def test_am_start_command_shape():
    args = ["--es", "session_token", "'tok'", "--ez", "skip_onboarding", "true"]
    assert launch_extras.am_start_command(_PKG, _ACT, args) == (
        f"am start -n {_PKG}/{_ACT} --es session_token 'tok' --ez skip_onboarding true")
    # a fully qualified activity works too
    assert launch_extras.am_start_command(
        _PKG, "com.example.app.MainActivity", args).startswith(
            f"am start -n {_PKG}/com.example.app.MainActivity ")


def test_am_start_command_is_none_whenever_anything_is_off():
    args = ["--ez", "k", "true"]
    assert launch_extras.am_start_command(_PKG, _ACT, []) is None       # no extras
    assert launch_extras.am_start_command(_PKG, "", args) is None       # no activity
    assert launch_extras.am_start_command("", _ACT, args) is None       # no pkg
    for bad in ("has space", "a/b", "quo'te", "$(id)", "..Main", "-Main"):
        assert launch_extras.am_start_command(_PKG, bad, args) is None, bad
        assert launch_extras.am_start_command(bad, _ACT, args) is None, bad


def test_extras_args_joins_resolved_tokens(tmp_path):
    rel = _write_secret(tmp_path)
    s = launch_extras.extras_args({"launch_extras": _string_extra(rel)}, vault_root=tmp_path)
    assert s == f"--es session_token {_SECRET}"
    assert launch_extras.extras_args({}, vault_root=tmp_path) == ""


# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------

def test_redact_replaces_every_occurrence():
    text = f"am start … {_SECRET} … failed, retried with {_SECRET}"
    out = launch_extras.redact(text, [_SECRET])
    assert _SECRET not in out
    assert out.count("***") == 2


def test_redact_ignores_empty_and_single_char_secrets():
    text = "a b c install: Success"
    # a 1-char secret would turn the whole log into ***
    assert launch_extras.redact(text, ["a", "", None]) == text


def test_redact_tolerates_empty_inputs():
    assert launch_extras.redact("", [_SECRET]) == ""
    assert launch_extras.redact("x", None) == "x"
    assert launch_extras.redact("x", []) == "x"
