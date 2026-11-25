"""Split install: brief.sh finds code in the engine checkout and data in the vault.

Notes/macp-public-split-inventory.md §11, leaf 11f (follow-up to 11e).

brief.sh used one anchor for both code and data: VAULT_DIR = MAESTRO_VAULT_DIR
or the parent of the script directory. In a split install (engine checkout !=
vault) the prompt (.claude/commands/brief.md), export_db_snapshot.py,
notify_kakao.sh and the `uv run python -m todo_system...` calls were looked up
in the vault, which has none of them; with MACP_VAULT_ROOT alone the data went
into the engine instead. brief.sh now has two anchors: ENGINE_DIR (script
parent) for code, and VAULT_DIR = MAESTRO_VAULT_DIR > MACP_VAULT_ROOT >
ENGINE_DIR for data. In a split install it also exports MACP_VAULT_ROOT so its
children (which run with the engine as cwd) find the same vault. The combined
layout leaves the env untouched.

Every case runs a *copy* of brief.sh under tmp_path with a fake claude, a fake
uv and a stub notify_kakao.sh, never the real CLIs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_PROMPT_MARKER = "ENGINE-PROMPT-11F"

pytestmark = [
    pytest.mark.skipif(shutil.which("zsh") is None, reason="brief.sh is a zsh script"),
    pytest.mark.skipif(
        any(Path("/opt/homebrew/bin", n).exists() for n in ("claude", "uv")),
        reason="brief.sh puts /opt/homebrew/bin ahead of $HOME/.local/bin; "
               "the fakes would be shadowed by the real CLIs",
    ),
]

_FAKE_UV = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
eff = os.getcwd()
if "--directory" in argv:
    eff = argv[argv.index("--directory") + 1]
with open(os.environ["BRIEF_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "uv", "argv": argv, "cwd": os.getcwd(),
                         "effective_dir": eff,
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT")}) + "\n")
'''

_FAKE_CLAUDE = r'''#!/usr/bin/env python3
import datetime as dt, json, os, sys
from pathlib import Path
with open(os.environ["BRIEF_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "claude", "argv": sys.argv[1:], "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT")}) + "\n")
# Relative to cwd: proves `cd "$VAULT_DIR"` still lands in the vault. UTC today
# and tomorrow, so a run that straddles midnight still finds its file.
out = Path.cwd() / "Todo" / "Briefings"
today = dt.datetime.now(dt.timezone.utc).date()
for day in (today, today + dt.timedelta(days=1)):
    (out / f"{day.isoformat()}.md").write_text("# Brief\n", encoding="utf-8")
'''

_STUB_NOTIFY = r'''#!/bin/sh
printf '{"tool":"notify","script":"%s","cwd":"%s","MACP_VAULT_ROOT":"%s","KAKAO_VAULT_DIR":"%s"}\n' \
  "$0" "$PWD" "${MACP_VAULT_ROOT:--}" "${KAKAO_VAULT_DIR:--}" >> "$BRIEF_TEST_LOG"
'''


def _engine(root: Path) -> Path:
    """Code only: a brief.sh copy, the two scripts it calls, the prompt."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(_REAL_ENGINE / "scripts" / "brief.sh", scripts / "brief.sh")
    (scripts / "export_db_snapshot.py").write_text("# marker, never executed\n", encoding="utf-8")
    (scripts / "notify_kakao.sh").write_text(_STUB_NOTIFY, encoding="utf-8")
    (scripts / "notify_kakao.sh").chmod(0o755)
    commands = root / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "brief.md").write_text(
        f"---\ndescription: x\n---\n{_PROMPT_MARKER}\n", encoding="utf-8")
    return root


def _vault(root: Path, *, token: bool = True) -> Path:
    """Data only. brief.sh must create logs/ and Todo/Briefings/ itself."""
    root.mkdir(parents=True, exist_ok=True)
    if token:
        (root / ".secrets").mkdir()
        (root / ".secrets" / "kakao-token.json").write_text("{}", encoding="utf-8")
    return root


def _utc_today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


class _Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.log = tmp / "calls.jsonl"
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        for name, body in (("uv", _FAKE_UV), ("claude", _FAKE_CLAUDE)):
            (bin_dir / name).write_text(body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)

    def run(self, script: Path, **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_VAULT_ROOT / MAESTRO_VAULT_DIR /
        # KAKAO_VAULT_DIR, so the SID/usage_probe branch runs.
        full = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "TZ": "UTC",
            "MAILROOM_DIR": str(self.tmp / "no-mailroom"),
            "BRIEF_TEST_LOG": str(self.log),
            "PYTHONUTF8": "1",
            **env,
        }
        return subprocess.run(["zsh", str(script)], env=full, capture_output=True,
                              text=True, timeout=60)

    def calls(self, tool: str) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [r for r in rows if r["tool"] == tool]

    def uv_call(self, keyword: str) -> dict:
        hits = [c for c in self.calls("uv") if any(keyword in a for a in c["argv"])]
        assert len(hits) == 1, (keyword, self.calls("uv"))
        return hits[0]


def _unset(value) -> bool:
    return value in (None, "-")


def _prompt(call: dict) -> str:
    argv = call["argv"]
    return argv[argv.index("-p") + 1]


def _assert_engine_clean(engine: Path) -> None:
    for name in ("logs", "Todo", ".secrets"):
        assert not (engine / name).exists(), name


def _assert_split(h: _Harness, r, engine: Path, vault: Path) -> None:
    assert r.returncode == 0, r.stdout + r.stderr

    claude = h.calls("claude")
    assert len(claude) == 1, h.calls("claude")
    assert _PROMPT_MARKER in _prompt(claude[0])
    assert f"Working directory: {vault}" in _prompt(claude[0])
    assert claude[0]["cwd"] == str(vault)

    for keyword in ("scan-queue", "export_db_snapshot.py", "usage_probe"):
        call = h.uv_call(keyword)
        assert call["effective_dir"] == str(engine), (keyword, call)
        assert call["MACP_VAULT_ROOT"] == str(vault), (keyword, call)

    assert f"{engine}/scripts/export_db_snapshot.py" in h.uv_call("export_db_snapshot.py")["argv"]
    probe = h.uv_call("usage_probe")["argv"]
    assert probe[probe.index("--dest") + 1] == f"{vault}/Worker/_state/brief_usage.jsonl"
    assert probe[probe.index("--source") + 1] == "brief"

    notify = h.calls("notify")
    assert len(notify) == 1, notify
    assert notify[0]["script"] == f"{engine}/scripts/notify_kakao.sh"
    assert notify[0]["MACP_VAULT_ROOT"] == str(vault)

    assert (vault / "logs" / "brief.out.log").is_file()
    assert (vault / "Todo" / "Briefings" / f"{_utc_today()}.md").is_file()
    _assert_engine_clean(engine)


def test_split_maestro_vault_dir_code_from_engine_data_in_vault(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")

    r = h.run(engine / "scripts" / "brief.sh", MAESTRO_VAULT_DIR=str(vault))

    _assert_split(h, r, engine, vault)


def test_split_macp_vault_root_alone_selects_vault(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")

    r = h.run(engine / "scripts" / "brief.sh", MACP_VAULT_ROOT=str(vault))

    _assert_split(h, r, engine, vault)


def test_maestro_vault_dir_beats_macp_vault_root(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault_a = _vault(tmp_path / "vault-a")
    vault_b = _vault(tmp_path / "vault-b")

    r = h.run(engine / "scripts" / "brief.sh",
              MAESTRO_VAULT_DIR=str(vault_b), MACP_VAULT_ROOT=str(vault_a))

    _assert_split(h, r, engine, vault_b)
    for tool in ("uv", "claude", "notify"):
        for call in h.calls(tool):
            assert call["MACP_VAULT_ROOT"] == str(vault_b), (tool, call)
    assert not (vault_a / "logs").exists()
    assert not (vault_a / "Todo").exists()


def test_split_without_vault_token_skips_push(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault", token=False)

    r = h.run(engine / "scripts" / "brief.sh", MAESTRO_VAULT_DIR=str(vault))

    assert r.returncode == 0, r.stdout + r.stderr
    assert h.calls("notify") == []
    assert "kakao token not configured" in r.stdout
    assert (vault / "Todo" / "Briefings" / f"{_utc_today()}.md").is_file()
    _assert_engine_clean(engine)


@pytest.mark.parametrize("env_mode", ["unset", "maestro_same_dir"])
def test_combined_layout_resolves_everything_to_script_parent(tmp_path, env_mode):
    """Engine == vault: every path resolves to the script parent and the
    children's env is left alone, as before the split."""
    h = _Harness(tmp_path)
    combined = _vault(_engine(tmp_path / "combined"))
    env = {"MAESTRO_VAULT_DIR": str(combined)} if env_mode == "maestro_same_dir" else {}

    r = h.run(combined / "scripts" / "brief.sh", **env)

    assert r.returncode == 0, r.stdout + r.stderr
    claude = h.calls("claude")
    assert len(claude) == 1, claude
    assert claude[0]["cwd"] == str(combined)
    assert _PROMPT_MARKER in _prompt(claude[0])
    assert f"Working directory: {combined}" in _prompt(claude[0])
    for keyword in ("scan-queue", "export_db_snapshot.py", "usage_probe"):
        assert h.uv_call(keyword)["effective_dir"] == str(combined), keyword
    assert f"{combined}/scripts/export_db_snapshot.py" in h.uv_call("export_db_snapshot.py")["argv"]
    probe = h.uv_call("usage_probe")["argv"]
    assert probe[probe.index("--dest") + 1] == f"{combined}/Worker/_state/brief_usage.jsonl"
    notify = h.calls("notify")
    assert len(notify) == 1, notify
    assert Path(notify[0]["script"]).parent.parent == combined
    for tool in ("uv", "claude", "notify"):
        for call in h.calls(tool):
            assert _unset(call["MACP_VAULT_ROOT"]), (tool, call)
    assert (combined / "logs" / "brief.out.log").is_file()
    assert (combined / "Todo" / "Briefings" / f"{_utc_today()}.md").is_file()


def test_brief_sh_declares_two_anchors():
    text = (_REAL_ENGINE / "scripts" / "brief.sh").read_text(encoding="utf-8")
    for needle in (
        'ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"',
        'VAULT_DIR="${MAESTRO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"',
        'export MACP_VAULT_ROOT="$VAULT_DIR"',
        'PROMPT_FILE="$ENGINE_DIR/.claude/commands/brief.md"',
        '"$ENGINE_DIR/scripts/notify_kakao.sh"',
        '"$ENGINE_DIR/scripts/export_db_snapshot.py"',
        'uv run --directory "$ENGINE_DIR" python -m todo_system.worker scan-queue',
        'uv run --directory "$ENGINE_DIR" python -m todo_system.orchestrator.usage_probe record',
    ):
        assert needle in text, needle
    assert "$VAULT_DIR/scripts/" not in text
    assert "$VAULT_DIR/.claude/" not in text
