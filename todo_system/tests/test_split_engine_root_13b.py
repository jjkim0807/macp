"""Installed-wheel shape without a wheel: the daemon scripts' MACP_PYTHON branch.

Public split 13b, P3. In an installed wheel ``$ENGINE_DIR`` is
``site-packages/todo_system/_engine``: no ``pyproject.toml`` for ``uv run
--project/--directory``, not a package for ``PYTHONPATH``. ``engine_script``
hands the caller's interpreter down as ``MACP_PYTHON`` and the scripts run
``"$MACP_PYTHON" -P -m todo_system...`` (or ``-c`` / a script path) only when
BOTH hold — no pyproject beside the engine AND ``MACP_PYTHON`` set. Everything
else (a checkout with pyproject, a bare path run with the variable unset) keeps
the uv / ``/usr/bin/python3`` command lines of 11f–11h byte for byte.

Each case copies the real script into a fake ``<site>/todo_system/_engine`` tree
(with or without a pyproject marker) and runs it under zsh with a fake
``MACP_PYTHON`` and a fake ``uv`` that only record their argv, cwd and env, so
nothing is imported, built or sent. The real installed layout is covered by
``test_installed_wheel_smoke_13b.py``; this file is the fast regression net for
the shell branch itself. No literal from the identifier gate's pattern may
appear in this file: it sits inside the scanned tree.
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

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_TICK = "orchestrator_tick.sh"
_RUN_NEXT = "worker_run_next.sh"
_BRIEF = "brief.sh"
_SEND = "kakao_send.sh"
_NOTIFY = "notify_kakao.sh"
_DAEMON_SCRIPTS = (_TICK, _RUN_NEXT, _BRIEF, _SEND, _NOTIFY)
_UNTOUCHED_SCRIPTS = ("kakao_token_refresh.sh", "kakao_oauth_helper.sh")
_BRANCH_LINE = 'if [[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]; then'
_FAKE_SLUG = "acme/fake-engine"
_PROMPT_MARKER = "ENGINE-PROMPT-13B"

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="the daemon scripts are zsh scripts")
needs_system_python = pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(),
    reason="the kakao scripts keep their stdlib helpers on /usr/bin/python3")
no_homebrew_cli = pytest.mark.skipif(
    any(Path("/opt/homebrew/bin", n).exists() for n in ("claude", "uv")),
    reason="brief.sh puts /opt/homebrew/bin ahead of $HOME/.local/bin; "
           "the fakes would be shadowed by the real CLIs")

# Stands in for MACP_PYTHON: records the call; answers the repo-slug `-c` probe.
_FAKE_PY = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "py", "argv": argv, "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT"),
                         "PYTHONPATH": os.environ.get("PYTHONPATH"),
                         "MACP_PYTHON": os.environ.get("MACP_PYTHON")}}) + "\\n")
if "-c" in argv:
    print("{_FAKE_SLUG}")
'''

# Records the call and exits 0 — the tripwire for the fallback path.
_FAKE_UV = f'''#!{sys.executable}
import json, os, sys
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "uv", "argv": sys.argv[1:], "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT"),
                         "PYTHONPATH": os.environ.get("PYTHONPATH"),
                         "MACP_PYTHON": os.environ.get("MACP_PYTHON")}}) + "\\n")
'''

_FAKE_CLAUDE = r'''#!/usr/bin/env python3
import datetime as dt, json, os, sys
from pathlib import Path
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "claude", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
out = Path.cwd() / "Todo" / "Briefings"
today = dt.datetime.now(dt.timezone.utc).date()
for day in (today, today + dt.timedelta(days=1)):
    (out / f"{day.isoformat()}.md").write_text("# Brief\n", encoding="utf-8")
'''

_STUB_NOTIFY = r'''#!/bin/sh
printf '{"tool":"notify","script":"%s","cwd":"%s"}\n' "$0" "$PWD" >> "$SEAM_TEST_LOG"
'''

# Stand-in for curl: one argv element per line, then a canned 200.
_FAKE_CURL = r'''#!/bin/sh
for a in "$@"; do printf '%s\n' "$a"; done >> "$SEAM_CURL_LOG"
printf -- '--END--\n' >> "$SEAM_CURL_LOG"
printf '{}\nHTTP_CODE:200'
'''


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class _Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.log = tmp / "calls.jsonl"
        self.curl_log = tmp / "curl.log"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        # The fake uv sits both at the front of PATH and in $HOME/.local/bin
        # (worker_run_next.sh / brief.sh prepend the latter themselves).
        for d in (self.bin, self.home / ".local" / "bin"):
            _write_exec(d / "uv", _FAKE_UV)
        self.fakepy = _write_exec(self.bin / "fakepy", _FAKE_PY)
        _write_exec(self.bin / "curl", _FAKE_CURL)
        _write_exec(self.home / ".local" / "bin" / "claude", _FAKE_CLAUDE)
        self.vault = tmp / "vault"
        (self.vault / ".macp").mkdir(parents=True)
        (self.vault / ".macp" / "config.toml").write_text("", encoding="utf-8")

    def engine(self, names, *, pyproject: bool) -> Path:
        """<site>/todo_system/_engine with copies of the real scripts; the
        pyproject marker makes it look like a checkout instead."""
        root = self.tmp / "site" / "todo_system" / "_engine"
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        for name in names:
            shutil.copy2(_REAL_ENGINE / "scripts" / name, scripts / name)
        if pyproject:
            (root / "pyproject.toml").write_text("# checkout marker\n", encoding="utf-8")
        return root

    def run(self, script: Path, *, macp_python: bool, vault: bool = True,
            args: tuple[str, ...] = (), **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_VAULT_ROOT / PYTHONPATH / MACP_PYTHON.
        full = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "SEAM_TEST_LOG": str(self.log),
            "SEAM_CURL_LOG": str(self.curl_log),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "TZ": "UTC",
            **env,
        }
        if vault:
            full["MACP_VAULT_ROOT"] = str(self.vault)
        if macp_python:
            full["MACP_PYTHON"] = str(self.fakepy)
        return subprocess.run(["zsh", str(script), *args], env=full, capture_output=True,
                              text=True, errors="replace", timeout=60)

    def calls(self, tool: str) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [r for r in rows if r["tool"] == tool]

    def curl_calls(self) -> list[list[str]]:
        if not self.curl_log.exists():
            return []
        calls, current = [], []
        for line in self.curl_log.read_text(encoding="utf-8", errors="replace").splitlines():
            if line == "--END--":
                calls.append(current)
                current = []
            else:
                current.append(line)
        return calls

    def sent_template(self) -> dict:
        memo = [c for c in self.curl_calls() if any("talk/memo/default/send" in a for a in c)]
        assert len(memo) == 1, memo
        for i, arg in enumerate(memo[0]):
            if arg == "--data-urlencode" and i + 1 < len(memo[0]):
                key, _, value = memo[0][i + 1].partition("=")
                if key == "template_object":
                    return json.loads(value)
        raise AssertionError(memo)


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _after(argv: list[str], word: str, flag: str) -> str | None:
    return _opt(argv[argv.index(word) + 1:], flag)


def _expected_uv_calls(name: str) -> int:
    return 2 if name == _TICK else 1


# --- the two tick scripts: installed branch on, fallback, checkout ----------------


@pytest.mark.parametrize("name", [_TICK, _RUN_NEXT])
def test_installed_layout_uses_macp_python_and_never_uv(tmp_path, name):
    h = _Harness(tmp_path)
    engine = h.engine([name], pyproject=False)

    r = h.run(engine / "scripts" / name, macp_python=True)

    assert h.calls("uv") == [], (h.calls("uv"), r.stdout, r.stderr)
    py = h.calls("py")
    assert len(py) == _expected_uv_calls(name), (py, r.stdout, r.stderr)
    for call in py:
        assert call["cwd"] == str(h.vault), call
        assert call["MACP_VAULT_ROOT"] == str(h.vault), call
        assert call["PYTHONPATH"] is None, call                 # _engine never on PYTHONPATH
    if name == _TICK:
        auto = [c for c in py if "auto" in c["argv"]]
        dash = [c for c in py if "dashboard" in c["argv"]]
        assert len(auto) == 1 and len(dash) == 1, py
        for call in (auto[0], dash[0]):
            assert call["argv"][:3] == ["-P", "-m", "todo_system.orchestrator.orchestrator"], call
            assert _opt(call["argv"], "--uri") == f"file://{h.vault}/Worker/agents", call
        assert _opt(auto[0]["argv"], "--lease") == f"{h.vault}/Worker/orchestrator.lock"
        assert _after(auto[0]["argv"], "auto", "--db") == f"{h.vault}/Todo/todo.db"
        assert (h.vault / "logs" / "orchestrator.out.log").is_file()
    else:
        assert py[0]["argv"] == ["-P", "-m", "todo_system.worker", "maintenance"], py
        log = (h.vault / "logs" / "worker.out.log").read_text(encoding="utf-8")
        assert "worker_run_next: maintenance" in log
        assert "ERROR uv not on PATH" not in log               # the uv guard is skipped
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert not (engine / "logs").exists()


@pytest.mark.parametrize("name", [_TICK, _RUN_NEXT])
def test_installed_layout_without_macp_python_falls_back_to_uv(tmp_path, name):
    """No pyproject but no MACP_PYTHON either (a bare path run of the wheel, or
    the 11g/11h fake engines): the pre-13b split command line, unchanged."""
    h = _Harness(tmp_path)
    engine = h.engine([name], pyproject=False)

    r = h.run(engine / "scripts" / name, macp_python=False)

    assert h.calls("py") == []
    uv = h.calls("uv")
    assert len(uv) == _expected_uv_calls(name), (uv, r.stdout, r.stderr)
    for call in uv:
        assert call["argv"][:3] == ["run", "--project", str(engine)], call
        assert "-P" in call["argv"], call
        assert "--directory" not in call["argv"], call
        assert call["cwd"] == str(h.vault), call
        assert call["MACP_VAULT_ROOT"] == str(h.vault), call
        assert (call["PYTHONPATH"] or "").split(os.pathsep)[0] == str(engine), call
        assert call["MACP_PYTHON"] is None, call


@pytest.mark.parametrize("name", [_TICK, _RUN_NEXT])
def test_checkout_layout_with_macp_python_still_uses_uv(tmp_path, name):
    """pyproject present: a checkout launched through engine_script inherits
    MACP_PYTHON but reads it nowhere — the split uv command line as before."""
    h = _Harness(tmp_path)
    engine = h.engine([name], pyproject=True)

    r = h.run(engine / "scripts" / name, macp_python=True)

    assert h.calls("py") == [], h.calls("py")
    uv = h.calls("uv")
    assert len(uv) == _expected_uv_calls(name), (uv, r.stdout, r.stderr)
    for call in uv:
        assert call["argv"][:3] == ["run", "--project", str(engine)], call
        assert "-P" in call["argv"], call
        assert call["cwd"] == str(h.vault), call
        assert (call["PYTHONPATH"] or "").split(os.pathsep)[0] == str(engine), call
        assert call["MACP_PYTHON"] == str(h.fakepy), call     # inherited, unread


@pytest.mark.parametrize("name", [_TICK, _RUN_NEXT])
def test_combined_checkout_with_macp_python_leaves_env_alone(tmp_path, name):
    """Engine == vault with pyproject: nothing exported, no -P, uv as before —
    even with MACP_PYTHON in the environment."""
    h = _Harness(tmp_path)
    engine = h.engine([name], pyproject=True)

    r = h.run(engine / "scripts" / name, macp_python=True, vault=False)

    assert h.calls("py") == [], h.calls("py")
    uv = h.calls("uv")
    assert len(uv) == _expected_uv_calls(name), (uv, r.stdout, r.stderr)
    for call in uv:
        assert call["argv"][:3] == ["run", "--project", str(engine)], call
        assert "-P" not in call["argv"], call
        assert call["cwd"] == str(engine), call
        assert call["MACP_VAULT_ROOT"] is None and call["PYTHONPATH"] is None, call


# --- brief.sh: three engine calls on the installed branch ------------------------


def _brief_engine(h: _Harness) -> Path:
    engine = h.engine([_BRIEF], pyproject=False)
    scripts = engine / "scripts"
    (scripts / "export_db_snapshot.py").write_text("# marker, never executed\n", encoding="utf-8")
    _write_exec(scripts / "notify_kakao.sh", _STUB_NOTIFY)
    commands = engine / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "brief.md").write_text(f"---\ndescription: x\n---\n{_PROMPT_MARKER}\n",
                                       encoding="utf-8")
    (h.vault / ".secrets").mkdir()
    (h.vault / ".secrets" / "kakao-token.json").write_text("{}", encoding="utf-8")
    return engine


@no_homebrew_cli
def test_installed_layout_brief_uses_macp_python(tmp_path):
    h = _Harness(tmp_path)
    engine = _brief_engine(h)

    r = h.run(engine / "scripts" / _BRIEF, macp_python=True,
              MAILROOM_DIR=str(tmp_path / "no-mailroom"))

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert h.calls("uv") == [], h.calls("uv")
    py = h.calls("py")
    argvs = [c["argv"] for c in py]
    assert ["-P", "-m", "todo_system.worker", "scan-queue"] in argvs, argvs
    assert ["-P", f"{engine}/scripts/export_db_snapshot.py"] in argvs, argvs
    probe = [a for a in argvs if "todo_system.orchestrator.usage_probe" in a]
    assert len(probe) == 1 and len(py) == 4, argvs
    assert probe[0][:5] == ["-P", "-m", "todo_system.orchestrator.usage_probe", "record",
                            "--source"], probe
    assert _opt(probe[0], "--dest") == f"{h.vault}/Worker/_state/brief_usage.jsonl", probe
    # The fourth call: the related-records prompt section (memory v2 wiring). The
    # engine computes it because this agent's --allowed-tools cannot run a query.
    related = [a for a in argvs if "todo_system.orchestrator.related_context" in a]
    assert len(related) == 1, argvs
    assert related[0][:4] == ["-P", "-m", "todo_system.orchestrator.related_context",
                              "emit-context"], related
    assert _opt(related[0], "--vault-root") == str(h.vault), related
    for call in py:
        assert call["cwd"] == str(h.vault), call
        assert call["MACP_VAULT_ROOT"] == str(h.vault), call
        assert call["PYTHONPATH"] is None, call
    claude = h.calls("claude")
    assert len(claude) == 1 and claude[0]["cwd"] == str(h.vault), claude
    assert _PROMPT_MARKER in claude[0]["argv"][claude[0]["argv"].index("-p") + 1]
    notify = h.calls("notify")
    assert len(notify) == 1 and notify[0]["script"] == f"{engine}/scripts/notify_kakao.sh", notify
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert (h.vault / "Todo" / "Briefings" / f"{today}.md").is_file()
    for name in ("logs", "Todo", ".secrets"):
        assert not (engine / name).exists(), name


@no_homebrew_cli
def test_installed_layout_brief_without_macp_python_falls_back_to_uv(tmp_path):
    h = _Harness(tmp_path)
    engine = _brief_engine(h)

    r = h.run(engine / "scripts" / _BRIEF, macp_python=False,
              MAILROOM_DIR=str(tmp_path / "no-mailroom"))

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert h.calls("py") == []
    uv = h.calls("uv")
    # Four: scan-queue, export_db_snapshot.py, related_context emit-context
    # (memory v2 wiring) and usage_probe.
    assert len(uv) == 4, uv
    for call in uv:
        assert call["argv"][:3] == ["run", "--directory", str(engine)], call
        assert call["MACP_VAULT_ROOT"] == str(h.vault), call
    assert len([c for c in uv
                if "todo_system.orchestrator.related_context" in c["argv"]]) == 1, uv


# --- kakao_send.sh / notify_kakao.sh: the resolver runs under MACP_PYTHON --------


@needs_system_python
def test_installed_layout_kakao_send_resolver_and_router_use_macp_python(tmp_path):
    h = _Harness(tmp_path)
    engine = h.engine([_SEND], pyproject=False)
    shutil.copy2(_REAL_ENGINE / "scripts" / "kakao_route.py", engine / "scripts" / "kakao_route.py")
    (h.vault / ".secrets").mkdir()
    (h.vault / ".secrets" / "kakao-token.json").write_text(
        json.dumps({"access_token": "AT-13b"}), encoding="utf-8")

    r = h.run(engine / "scripts" / _SEND, macp_python=True, args=("hello",), KAKAO_LEVEL="digest")

    assert r.returncode == 0, (r.stdout, r.stderr)
    py = h.calls("py")
    assert len(py) == 2, (py, r.stderr)
    resolver, router = py
    assert resolver["argv"][:2] == ["-P", "-c"], resolver
    assert resolver["argv"][-2:] == [str(engine), str(h.vault)], resolver
    assert router["argv"] == ["-P", f"{engine}/scripts/kakao_route.py", "hello"], router
    for call in py:
        assert call["PYTHONPATH"] is None, call
    # The fake router answers nothing, so the script delivers (fail-open) and the
    # card link is what the fake resolver printed, not the placeholder.
    assert "route verdict unreadable" in r.stderr, r.stderr
    template = h.sent_template()
    assert template["link"]["web_url"] == f"https://github.com/{_FAKE_SLUG}", template


@needs_system_python
def test_installed_layout_notify_kakao_resolver_uses_macp_python(tmp_path):
    h = _Harness(tmp_path)
    engine = h.engine([_NOTIFY], pyproject=False)
    shutil.copy2(_REAL_ENGINE / "scripts" / "_build_brief_message.py",
                 engine / "scripts" / "_build_brief_message.py")
    (h.vault / ".secrets").mkdir()
    (h.vault / ".secrets" / "kakao-token.json").write_text(
        json.dumps({"access_token": "AT-13b"}), encoding="utf-8")
    briefs = h.vault / "Todo" / "Briefings"
    briefs.mkdir(parents=True)
    today = dt.datetime.now(dt.timezone.utc).date()
    for day in (today, today + dt.timedelta(days=1)):
        (briefs / f"{day.isoformat()}.md").write_text("# Brief\n\n- 요약: 13b smoke\n",
                                                      encoding="utf-8")

    r = h.run(engine / "scripts" / _NOTIFY, macp_python=True)

    assert r.returncode == 0, (r.stdout, r.stderr)
    py = h.calls("py")
    assert len(py) == 1, py
    assert py[0]["argv"][:2] == ["-P", "-c"], py
    assert py[0]["argv"][-2:] == [str(engine), str(h.vault)], py
    assert py[0]["PYTHONPATH"] is None, py
    template = h.sent_template()
    assert template["link"]["web_url"] == f"https://github.com/{_FAKE_SLUG}", template
    assert "Authorization: Bearer AT-13b" in h.curl_calls()[0]


# --- the script bodies ------------------------------------------------------------


def test_scripts_declare_the_installed_branch_once():
    for name in _DAEMON_SCRIPTS:
        text = (_REAL_ENGINE / "scripts" / name).read_text(encoding="utf-8")
        assert text.count(_BRANCH_LINE) == 1, name
    for name in (_SEND, _NOTIFY):
        text = (_REAL_ENGINE / "scripts" / name).read_text(encoding="utf-8")
        assert "ENGINE_PY=(/usr/bin/python3)" in text, name
        assert 'ENGINE_PY=("$MACP_PYTHON" -P)' in text, name
        assert '"${ENGINE_PY[@]}"' in text, name
    for name in _UNTOUCHED_SCRIPTS:
        text = (_REAL_ENGINE / "scripts" / name).read_text(encoding="utf-8")
        assert "MACP_PYTHON" not in text, name
