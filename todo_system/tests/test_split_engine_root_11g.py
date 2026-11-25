"""Split install: orchestrator_tick.sh imports code from the engine, keeps data in the vault.

Notes/macp-public-split-inventory.md §11, leaf 11g (follow-up to 11f).

orchestrator_tick.sh used one anchor for both code and data: VAULT_DIR =
MAESTRO_VAULT_DIR or the parent of the script directory, then `cd` there and
`python -m todo_system.orchestrator.orchestrator`. todo_system is not installed
(package = false), so the import came from the cwd: in a split install (engine
checkout != vault) the vault has no package and the tick died on the import,
or a stale copy in the vault ran instead of the engine; with MACP_VAULT_ROOT
alone the logs and mailbox went into the engine. The script now has two
anchors: ENGINE_DIR (script parent) for code, and VAULT_DIR = MAESTRO_VAULT_DIR
> MACP_VAULT_ROOT > ENGINE_DIR for data. The cwd stays the vault (the
orchestrator resolves its defaults, the hq-host marker and the git pull against
it) and `uv run --project "$ENGINE_DIR"` picks the engine without moving it. A
split install exports MACP_VAULT_ROOT and puts the engine on PYTHONPATH with
`python -P`; the combined layout leaves env and argv alone.

These cases judge by the call log, not the exit code (that is covered by
test_orchestrator_tick_exit_status.py). Each case runs a *copy* of the script under
tmp_path with a fake uv and a stub orchestrator, never the real ones.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_ORCH_REL = Path("todo_system") / "orchestrator" / "orchestrator.py"

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="orchestrator_tick.sh is a zsh script")

# Records the call, then runs the real interpreter (the one running pytest) so
# the import is resolved exactly as it would be: from the cwd, PYTHONPATH, -P.
_FAKE_UV = f'''#!{sys.executable}
import json, os, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["TICK_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "uv", "argv": argv, "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT"),
                         "PYTHONPATH": os.environ.get("PYTHONPATH")}}) + "\\n")
assert argv and argv[0] == "run", argv
rest = argv[1:]
while rest and rest[0].startswith("--"):
    flag, value, rest = rest[0], rest[1], rest[2:]
    if flag == "--directory":
        os.chdir(value)
assert rest and rest[0] == "python", rest
sys.exit(subprocess.run([sys.executable, *rest[1:]], env=os.environ).returncode)
'''

_STUB_ORCH = '''import json, os, sys
from pathlib import Path
with open(os.environ["TICK_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "orch", "file": str(Path(__file__).resolve()),
                         "argv": sys.argv[1:], "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT"),
                         "PYTHONPATH": os.environ.get("PYTHONPATH")}) + "\\n")
print("stub-orch", sys.argv[1:])
'''


def _package(root: Path) -> None:
    pkg = root / "todo_system" / "orchestrator"
    pkg.mkdir(parents=True)
    (root / "todo_system" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (root / _ORCH_REL).write_text(_STUB_ORCH, encoding="utf-8")


def _engine(root: Path) -> Path:
    """Code only: an orchestrator_tick.sh copy and a stub orchestrator."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(_REAL_ENGINE / "scripts" / "orchestrator_tick.sh",
                 scripts / "orchestrator_tick.sh")
    _package(root)
    return root


def _vault(root: Path, *, decoy: bool = False) -> Path:
    """Data only. decoy=True leaves a stale package copy that must not win."""
    root.mkdir(parents=True, exist_ok=True)
    if decoy:
        _package(root)
    return root


class _Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.log = tmp / "calls.jsonl"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "uv").write_text(_FAKE_UV, encoding="utf-8")
        (self.bin / "uv").chmod(0o755)
        (tmp / "home").mkdir()

    def run(self, script: Path, **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_VAULT_ROOT / MAESTRO_VAULT_DIR /
        # PYTHONPATH. The script does not touch PATH, so the fake uv wins.
        full = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.tmp / "home"),
            "TICK_TEST_LOG": str(self.log),
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


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _resolved(value: str, cwd: str) -> str:
    path = value.removeprefix("file://")
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def _auto_and_dashboard(h: _Harness, r) -> tuple[dict, dict]:
    # Non-empty first: a broken import leaves only uv rows.
    orch = h.calls("orch")
    assert len(orch) == 2, (h.calls("uv"), orch, r.stdout, r.stderr)
    auto = [c for c in orch if "auto" in c["argv"]]
    dash = [c for c in orch if "dashboard" in c["argv"]]
    assert len(auto) == 1 and len(dash) == 1, orch
    return auto[0], dash[0]


def _after(argv: list[str], word: str, flag: str) -> str | None:
    return _opt(argv[argv.index(word) + 1:], flag)


def _assert_split(h: _Harness, r, engine: Path, vault: Path) -> None:
    auto, dash = _auto_and_dashboard(h, r)
    for call in (auto, dash):
        assert call["file"] == str(engine / _ORCH_REL), call
        assert call["cwd"] == str(vault), call
        assert call["MACP_VAULT_ROOT"] == str(vault), call
        assert (call["PYTHONPATH"] or "").split(os.pathsep)[0] == str(engine), call
        assert _opt(call["argv"], "--uri") == f"file://{vault}/Worker/agents", call
    assert _opt(auto["argv"], "--lease") == f"{vault}/Worker/orchestrator.lock", auto
    assert _after(auto["argv"], "auto", "--db") == f"{vault}/Todo/todo.db", auto

    uv = h.calls("uv")
    assert len(uv) == 2, uv
    for call in uv:
        assert _opt(call["argv"], "--project") == str(engine), call
        assert "--directory" not in call["argv"], call
        assert call["cwd"] == str(vault), call

    assert "stub-orch" in r.stdout, (r.stdout, r.stderr)
    for name in ("logs", "Worker", "Todo"):
        assert not (engine / name).exists(), name


def test_split_maestro_vault_dir_code_from_engine_data_in_vault(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")

    r = h.run(engine / "scripts" / "orchestrator_tick.sh", MAESTRO_VAULT_DIR=str(vault))

    _assert_split(h, r, engine, vault)


def test_split_macp_vault_root_alone_selects_vault(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")

    r = h.run(engine / "scripts" / "orchestrator_tick.sh", MACP_VAULT_ROOT=str(vault))

    _assert_split(h, r, engine, vault)


def test_maestro_vault_dir_beats_macp_vault_root(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault_a = _vault(tmp_path / "vault-a")
    vault_b = _vault(tmp_path / "vault-b")

    r = h.run(engine / "scripts" / "orchestrator_tick.sh",
              MAESTRO_VAULT_DIR=str(vault_b), MACP_VAULT_ROOT=str(vault_a))

    _assert_split(h, r, engine, vault_b)
    for tool in ("uv", "orch"):
        for call in h.calls(tool):
            assert call["MACP_VAULT_ROOT"] == str(vault_b), (tool, call)
    assert not (vault_a / "logs").exists()


def test_split_vault_copy_of_package_does_not_shadow_engine(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault", decoy=True)

    r = h.run(engine / "scripts" / "orchestrator_tick.sh", MAESTRO_VAULT_DIR=str(vault))

    _assert_split(h, r, engine, vault)


@pytest.mark.parametrize("env_mode", ["unset", "maestro_same_dir"])
def test_combined_layout_resolves_everything_to_script_parent(tmp_path, env_mode):
    """Engine == vault: every path resolves to the script parent and the
    orchestrator's env is left alone, as before the split."""
    h = _Harness(tmp_path)
    combined = _engine(tmp_path / "combined")
    env = {"MAESTRO_VAULT_DIR": str(combined)} if env_mode == "maestro_same_dir" else {}

    r = h.run(combined / "scripts" / "orchestrator_tick.sh", **env)

    auto, dash = _auto_and_dashboard(h, r)
    for call in (auto, dash):
        assert call["file"] == str(combined / _ORCH_REL), call
        assert call["cwd"] == str(combined), call
        assert call["MACP_VAULT_ROOT"] is None, call
        assert call["PYTHONPATH"] is None, call
        assert _resolved(_opt(call["argv"], "--uri"), call["cwd"]) == \
            f"{combined}/Worker/agents", call
    assert _resolved(_opt(auto["argv"], "--lease"), auto["cwd"]) == \
        f"{combined}/Worker/orchestrator.lock", auto
    assert _resolved(_after(auto["argv"], "auto", "--db"), auto["cwd"]) == \
        f"{combined}/Todo/todo.db", auto
    uv = h.calls("uv")
    assert len(uv) == 2, uv
    for call in uv:
        assert call["cwd"] == str(combined), call
        assert "-P" not in call["argv"], call
        assert call["MACP_VAULT_ROOT"] is None and call["PYTHONPATH"] is None, call
    assert "stub-orch" in r.stdout, (r.stdout, r.stderr)


def test_orchestrator_tick_declares_two_anchors():
    text = (_REAL_ENGINE / "scripts" / "orchestrator_tick.sh").read_text(encoding="utf-8")
    for needle in (
        'ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"',
        'VAULT_DIR="${MAESTRO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"',
        'export MACP_VAULT_ROOT="$VAULT_DIR"',
        'uv run --project "$ENGINE_DIR"',
        '"file://$VAULT_DIR/Worker/agents"',
        '"$VAULT_DIR/Worker/orchestrator.lock"',
        '"$VAULT_DIR/Todo/todo.db"',
        'cd "$VAULT_DIR"',
    ):
        assert needle in text, needle
    assert "uv run --directory" not in text
    assert "${=M}" not in text
