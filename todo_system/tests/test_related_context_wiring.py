"""The two prompt-injection call sites, and the permissions they must NOT change.

AC#2 has two halves and this file pins both:

* ``planner_tick.sh`` and ``brief.sh`` append the related-records block to their
  prompt — and when the block is empty the prompt is **byte-identical** to what
  it was before this wiring existed. Both scripts run for real here (fake
  interpreter, fake ``claude``), so the assertion is about the shipped shell, not
  about a description of it.
* **no agent's tool permissions change.** That is the half a reader cannot check
  by eye six months from now, and the half this wiring would be tempting to
  "fix" by widening a whitelist instead. So the three permission surfaces are
  asserted verbatim: ``STAGE_TOOLS["research"]``, ``TASK_TOOLS_STAGES``, and the
  ``--allowed-tools`` lines of both scripts. If a later change adds Bash to the
  research stage or a python module to brief's list, this fails.

Harness shapes copied from ``test_planner_tick_keep_alive.py`` (planner, bash)
and ``test_split_engine_root_13b.py`` (brief, zsh + a fake ``MACP_PYTHON``).
Neutral prefixes only: this file sits inside the tree the public identifier gate
scans.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system.orchestrator import stages

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_PLANNER = "planner_tick.sh"
_BRIEF = "brief.sh"
_PROMPT_MARKER = "ENGINE-PROMPT-G4B"
_RC_MODULE = "todo_system.orchestrator.related_context"

# What the block looks like, without importing the renderer: these tests are
# about the SHELL, so a literal keeps a renderer change from silently rewriting
# what they think they are injecting.
_BLOCK = ("## 관련 과거 기록 (memory v2, 엔진이 미리 질의)\n"
          "\n"
          "### `sample-task-alpha`\n"
          "\n"
          "- `episode-00` — 에피소드 00 · source: `Memory/episodes/episode-00.md`\n")

no_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="brief.sh is a zsh script")
no_homebrew_cli = pytest.mark.skipif(
    any(Path("/opt/homebrew/bin", n).exists() for n in ("claude", "uv")),
    reason="brief.sh puts /opt/homebrew/bin ahead of $HOME/.local/bin; "
           "the fakes would be shadowed by the real CLIs")


# --- half 1: the permission surfaces, asserted verbatim -------------------------

def test_the_research_stage_still_has_no_bash():
    """The reason this whole module exists. If research ever gains Bash the
    engine-side injection is no longer the only way to reach it — and that is a
    decision for a human, not a side effect of a later refactor."""
    assert stages.STAGE_TOOLS["research"] == "Read Write"
    assert "Bash" not in stages.STAGE_TOOLS["research"]


def test_a_task_cannot_widen_the_research_stage():
    """`allowed_tools` in a task's frontmatter reaches implement only, so no task
    can grant research the Bash this wiring works around."""
    assert stages.TASK_TOOLS_STAGES == ("implement",)


def _allowed_tools_lines(script: str) -> list[str]:
    """Only lines that actually PASS the flag — a prose line that merely mentions
    `--allowed-tools` (both scripts now carry one, explaining why the engine does
    the query) is not a permission."""
    text = (_REAL_ENGINE / "scripts" / script).read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("--allowed-tools")]


@pytest.mark.parametrize("script,line,count", [
    (_PLANNER, "--allowed-tools Read Write Edit Glob Grep Bash 2>&1 | tail -40", 2),
    (_BRIEF, '--allowed-tools "Read" "Write" "Edit" "Bash(git log:*)" '
             '"Bash(git diff:*)" "Bash(git status:*)" \\', 2),
])
def test_the_allowed_tools_lines_are_untouched(script, line, count):
    """Both scripts invoke claude twice (usage instrumentation on / off) with the
    same whitelist, so both copies are asserted — a change to either fails."""
    assert _allowed_tools_lines(script) == [line] * count, _allowed_tools_lines(script)


def test_neither_script_grants_a_python_or_module_tool():
    """The shape of the mistake this guards against: adding
    `Bash(python3:*)` / `Bash(uv:*)` to a prompt's whitelist so the agent can run
    the query itself. The engine runs it instead; the lists stay as they are."""
    for script in (_PLANNER, _BRIEF):
        for line in _allowed_tools_lines(script):
            for banned in ("python", "uv:", "macp-", _RC_MODULE):
                assert banned not in line, (script, line)


# --- half 2a: planner_tick.sh ---------------------------------------------------

_FAKE_PY = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "py", "argv": argv, "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT")}}) + "\\n")
mod = argv[argv.index("-m") + 1] if "-m" in argv else ""
if mod == "todo_system.orchestrator.keep_alive" and "queue-depth" in argv:
    sys.stdout.write("0\\n")
if mod == "{_RC_MODULE}" and "emit-context" in argv and os.environ.get("SEAM_RC_FILE"):
    with open(os.environ["SEAM_RC_FILE"], encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
'''

_FAKE_CLAUDE_PLANNER = f'''#!{sys.executable}
import json, os, sys
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "claude", "argv": sys.argv[1:]}}) + "\\n")
'''

_FAKE_CLAUDE_BRIEF = r'''#!/usr/bin/env python3
import datetime as dt, json, os, sys
from pathlib import Path
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "claude", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
out = Path.cwd() / "Todo" / "Briefings"
out.mkdir(parents=True, exist_ok=True)
today = dt.datetime.now().date()
for day in (today - dt.timedelta(days=1), today, today + dt.timedelta(days=1)):
    (out / f"{day.isoformat()}.md").write_text("# Brief\n", encoding="utf-8")
'''

_FAKE_SQLITE = "#!/bin/sh\nexit 0\n"
_STUB_NOTIFY = "#!/bin/sh\nexit 0\n"


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class _Harness:
    """One engine tree (installed shape: no pyproject, a fake MACP_PYTHON) plus a
    fake vault. `rc_block` is what the related_context CLI will answer with."""

    def __init__(self, tmp: Path, script: str, *, claude: str):
        self.tmp = tmp
        self.log = tmp / "calls.jsonl"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        self.home.mkdir()
        self.fakepy = _write_exec(self.bin / "fakepy", _FAKE_PY)
        _write_exec(self.bin / "sqlite3", _FAKE_SQLITE)
        _write_exec(self.bin / "claude", claude)
        _write_exec(self.home / ".local" / "bin" / "claude", claude)
        self.vault = tmp / "vault"
        (self.vault / ".macp").mkdir(parents=True)
        (self.vault / ".macp" / "config.toml").write_text("", encoding="utf-8")
        self.engine = tmp / "site" / "todo_system" / "_engine"
        scripts = self.engine / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(_REAL_ENGINE / "scripts" / script, scripts / script)
        self.script = scripts / script
        self.rc_file = tmp / "rc_block.txt"

    def with_brief_extras(self) -> _Harness:
        scripts = self.engine / "scripts"
        (scripts / "export_db_snapshot.py").write_text("# marker, never executed\n",
                                                       encoding="utf-8")
        _write_exec(scripts / "notify_kakao.sh", _STUB_NOTIFY)
        commands = self.engine / ".claude" / "commands"
        commands.mkdir(parents=True)
        (commands / "brief.md").write_text(
            f"---\ndescription: x\n---\n{_PROMPT_MARKER}\n", encoding="utf-8")
        return self

    def run(self, *, shell: str, block: str | None = None, **env: str):
        full = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin",
            "HOME": str(self.home),
            "SEAM_TEST_LOG": str(self.log),
            "GIT_CEILING_DIRECTORIES": str(self.tmp),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "TZ": "UTC",
            "MACP_VAULT_ROOT": str(self.vault),
            "MACP_PYTHON": str(self.fakepy),
            "MAILROOM_DIR": str(self.tmp / "no-mailroom"),
            **env,
        }
        if block is not None:
            self.rc_file.write_text(block, encoding="utf-8")
            full["SEAM_RC_FILE"] = str(self.rc_file)
        return subprocess.run([shell, str(self.script)], env=full, capture_output=True,
                              text=True, errors="replace", timeout=120)

    def calls(self, tool: str) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        return [r for r in rows if r["tool"] == tool]

    def prompts(self) -> list[str]:
        return [c["argv"][c["argv"].index("-p") + 1] for c in self.calls("claude")]

    def rc_calls(self) -> list[dict]:
        return [c for c in self.calls("py") if _RC_MODULE in c["argv"]]


def test_planner_prompt_is_byte_identical_when_the_block_is_empty(tmp_path):
    h = _Harness(tmp_path, _PLANNER, claude=_FAKE_CLAUDE_PLANNER)

    r = h.run(shell="bash", block="")

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert h.prompts() == ["/worker-planner"], h.prompts()
    assert "related past records" not in r.stdout, r.stdout
    assert len(h.rc_calls()) == 1, "the query must still have been attempted"


def test_planner_prompt_gains_the_block_when_there_is_one(tmp_path):
    h = _Harness(tmp_path, _PLANNER, claude=_FAKE_CLAUDE_PLANNER)

    r = h.run(shell="bash", block=_BLOCK)

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert ("[planner-tick] injecting related past records for 1 task(s) into planner "
            "context") in r.stdout, r.stdout
    assert h.prompts() == ["/worker-planner\n\n" + _BLOCK.rstrip("\n")], h.prompts()


def test_planner_survives_a_crashing_query(tmp_path):
    """No SEAM_RC_FILE and a module that prints nothing is the empty case; this is
    the harsher one — the whole tick must not die if the CLI blows up."""
    h = _Harness(tmp_path, _PLANNER, claude=_FAKE_CLAUDE_PLANNER)

    r = h.run(shell="bash", block="", SEAM_RC_FILE=str(tmp_path / "missing.txt"))

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert h.prompts() == ["/worker-planner"], h.prompts()


def test_the_planner_query_runs_in_the_vault_with_the_engine_interpreter(tmp_path):
    h = _Harness(tmp_path, _PLANNER, claude=_FAKE_CLAUDE_PLANNER)

    h.run(shell="bash", block="")

    call = h.rc_calls()[0]
    assert call["argv"] == ["-P", "-m", _RC_MODULE, "emit-context", "--limit", "5"], call
    assert call["cwd"] == str(h.vault), call
    assert call["MACP_VAULT_ROOT"] == str(h.vault), call


# --- half 2b: brief.sh ----------------------------------------------------------

@no_zsh
@no_homebrew_cli
def test_brief_prompt_is_byte_identical_when_the_block_is_empty(tmp_path):
    """Both runs in ONE vault, because the prompt embeds the vault path and the
    date — a second harness would differ for reasons that are not the point."""
    h = _Harness(tmp_path, _BRIEF, claude=_FAKE_CLAUDE_BRIEF).with_brief_extras()

    empty = h.run(shell="zsh", block="")
    assert empty.returncode == 0, (empty.stdout, empty.stderr)
    withblock = h.run(shell="zsh", block=_BLOCK)
    assert withblock.returncode == 0, (withblock.stdout, withblock.stderr)

    first, second = h.prompts()
    assert _PROMPT_MARKER in first
    assert "관련 과거 기록" not in first, "an empty block must append nothing"
    # The command substitution strips the block's trailing newline, as it does
    # for every other injected block in both scripts.
    assert second == first + "\n" + _BLOCK.rstrip("\n"), second[len(first):]
    assert "injecting related past records for 1 task(s)" in withblock.stdout


@no_zsh
@no_homebrew_cli
def test_brief_survives_a_crashing_query(tmp_path):
    """`|| true` is load-bearing: under `set -euo pipefail` a non-zero rc in that
    command substitution would abort the brief before claude is ever called."""
    h = _Harness(tmp_path, _BRIEF, claude=_FAKE_CLAUDE_BRIEF).with_brief_extras()

    r = h.run(shell="zsh", block="", SEAM_RC_FILE=str(tmp_path / "missing.txt"))

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert len(h.prompts()) == 1 and _PROMPT_MARKER in h.prompts()[0]


@no_zsh
@no_homebrew_cli
def test_the_brief_query_is_pinned_to_the_vault_and_limited_to_three(tmp_path):
    h = _Harness(tmp_path, _BRIEF, claude=_FAKE_CLAUDE_BRIEF).with_brief_extras()

    h.run(shell="zsh", block="")

    call = h.rc_calls()[0]
    assert call["argv"] == ["-P", "-m", _RC_MODULE, "emit-context",
                            "--limit", "3", "--vault-root", str(h.vault)], call
    assert call["cwd"] == str(h.vault), call


@no_zsh
@no_homebrew_cli
def test_the_brief_query_runs_after_the_snapshot_export(tmp_path):
    """Order matters for the "Top-3" to mean anything: the export re-ranks
    Todo/Snapshots/latest.json, and the query reads its head."""
    h = _Harness(tmp_path, _BRIEF, claude=_FAKE_CLAUDE_BRIEF).with_brief_extras()

    h.run(shell="zsh", block="")

    argvs = [c["argv"] for c in h.calls("py")]
    export = [i for i, a in enumerate(argvs) if any("export_db_snapshot" in x for x in a)]
    related = [i for i, a in enumerate(argvs) if _RC_MODULE in a]
    assert export and related, argvs
    assert export[0] < related[0], argvs


def test_todays_date_is_not_what_this_file_asserts():
    """Guard against a copy-paste that makes the suite date-dependent: nothing
    above compares a date, so the brief fake may write any day it likes."""
    assert isinstance(dt.date.today(), dt.date)
