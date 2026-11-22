"""Usage vs results on its two real paths: `/api/status` and the brief.

The unit tests (test_usage_value.py) pin the aggregation. This file pins the
WIRING — that the dashboard and the brief both go through it and show the same
table:

* `/api/status`: the real `serve()` server and the real `collect_status()` over
  a tmp vault. Only the collectors that would reach outside the vault are
  stubbed (queue, usage, accounts, process list), never the aggregation.
* the brief: the real `scripts/brief.sh` under zsh. `claude` is the only fake;
  the engine call `python -m todo_system.usage_value brief-section` runs for
  real against the same tmp vault.

The vault is built from the real-shaped cases in `usage_value_cases/`, with the
run dirs re-stamped relative to the test's own clock (an hour, two days, five
days and forty days ago), because both paths read the real clock. Expected
numbers are therefore derived with `usage_value.period_bounds`, never typed in.

The dashboard module is loaded under its own name so patches here never leak
into the other dashboard test modules. Every value is synthetic.
"""
from __future__ import annotations

import datetime as dt
import http.client
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from todo_system import usage_value

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "scripts" / "macp_dashboard.py"
_CASES = Path(__file__).resolve().parent / "usage_value_cases"
TOKEN = "usage-value-test-token"
_HANGUL = re.compile("[가-힣]")
_UV_MODULE = "todo_system.usage_value"

no_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="brief.sh is a zsh script")
no_homebrew_cli = pytest.mark.skipif(
    any(Path("/opt/homebrew/bin", n).exists() for n in ("claude", "uv")),
    reason="brief.sh puts /opt/homebrew/bin ahead of $HOME/.local/bin; "
           "the fakes would be shadowed by the real CLIs")


def _load():
    spec = importlib.util.spec_from_file_location("macp_dashboard_usage_value", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _content(value) -> str:
    """A fixture file: an object is JSON, a list is its lines, a string is itself."""
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, list):
        return "\n".join(value) + "\n"
    return value


# (case file, the run of it to install, how long ago it started)
_RUNS = (
    ("auto_five_stage_merged", "20261121T144046-engine-triage-cards", dt.timedelta(hours=1)),
    ("unmetered_runs", "20261120T154934-app-player-skip-wired", dt.timedelta(days=2)),
    ("quick_merged", "20261117T114215-app-doc-weekly-check", dt.timedelta(days=5)),
    ("discovery_run_and_child", "20261117T114214-app-discovery-20261117", dt.timedelta(days=40)),
)
# slug -> output tokens of its run, as the case files state them
_TOKENS = {"engine-triage-cards": 466_000, "app-player-skip-wired": 70_000,
           "app-doc-weekly-check": 12_000, "app-discovery-20261117": 55_000}


class _PathVault:
    """A tmp vault with three recent runs and one old one, named off `now`."""

    def __init__(self, root: Path):
        self.root = root
        self.now = dt.datetime.now().replace(microsecond=0)
        for d in ("Todo/items", "Worker/assignments", "Worker/agents", "Worker/runs",
                  "Worker/_state", "Memory/sessions", ".macp"):
            (root / d).mkdir(parents=True)
        (root / ".macp" / "config.toml").write_text("", encoding="utf-8")
        (root / "Worker/agents/registry.yaml").write_text(
            "agents:\n  - id: worker-default\n    kind: ai-local\n    projects: ['*']\n",
            encoding="utf-8")
        self.at: dict[str, dt.datetime] = {}
        for case_name, run_name, ago in _RUNS:
            case = json.loads((_CASES / f"{case_name}.json").read_text(encoding="utf-8"))
            run = next(r for r in case["runs"] if r["name"] == run_name)
            slug = run_name.split("-", 1)[1]
            started = self.now - ago
            rd = root / "Worker" / "runs" / f"{started.strftime('%Y%m%dT%H%M%S')}-{slug}"
            rd.mkdir()
            for fname, value in run["files"].items():
                (rd / fname).write_text(_content(value), encoding="utf-8")
            task = case["tasks"][slug]
            (root / "Todo" / "items" / f"{slug}.md").write_text(_content(task["md"]),
                                                               encoding="utf-8")
            (root / "Worker" / "assignments" / f"{slug}.json").write_text(
                json.dumps(task["ledger"]), encoding="utf-8")
            self.at[slug] = started
        self._todo_db()

    def _todo_db(self) -> None:
        from todo_system import cli
        con = cli.db_connect(self.root / "Todo" / "todo.db")
        cli.init_db(con)
        for slug in self.at:
            con.execute("INSERT INTO tasks (slug, title, path, status, worker_mode, imported_at, "
                        "tags) VALUES (?,?,?,'done','auto','2026-01-01T00:00:00','todo')",
                        (slug, slug, str(self.root / "Todo" / "items" / f"{slug}.md")))
        con.commit()
        con.close()

    def slugs_in(self, period: str) -> list[str]:
        """The installed runs' slugs that fall inside `period`, by the engine's own bounds."""
        start, end = usage_value.period_bounds(dt.datetime.now())[period]
        return [slug for slug, at in self.at.items() if start <= at <= end]

    def build(self) -> dict:
        return usage_value.build(vault_root=self.root)


@pytest.fixture
def pv(tmp_path, monkeypatch):
    """The vault, with the dashboard module pointed at it."""
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    usage_value.clear_cache()
    vault = _PathVault(tmp_path / "vault")
    root = vault.root
    paths = {"VAULT": root, "ITEMS": root / "Todo" / "items",
             "ASSIGN": root / "Worker" / "assignments", "RUNS": root / "Worker" / "runs",
             "STATE": root / "Worker" / "_state", "TODO_DB": root / "Todo" / "todo.db",
             "SESSIONS": root / "Memory" / "sessions",
             "REGISTRY": root / "Worker" / "agents" / "registry.yaml"}
    for name, value in paths.items():
        monkeypatch.setattr(M, name, value)
    stubs = {"collect_queue": {k: [] for k in M.QUEUE_KEYS}, "collect_usage": None,
             "_worker_running": [], "collect_deps": [], "collect_account": {},
             "collect_account_runtime": {}, "collect_account_slots": {}}
    for name, value in stubs.items():
        monkeypatch.setattr(M, name, mock.Mock(return_value=value))
    monkeypatch.setattr("todo_system.macp.config.load_profile",
                        mock.Mock(side_effect=RuntimeError("no profile in tests")))
    yield vault
    usage_value.clear_cache()


@pytest.fixture
def served(pv):
    """`get()` -> the JSON of GET /api/status from a real serve() server on the vault."""
    srv = M.serve("127.0.0.1", 0, TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]

    def get() -> dict:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            c.request("GET", "/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
            r = c.getresponse()
            body = r.read()
        finally:
            c.close()
        assert r.status == 200, body[:300]
        return json.loads(body)

    yield get
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _expected_total(pv: _PathVault, period: str) -> dict:
    slugs = pv.slugs_in(period)
    return {"runs": len(slugs), "out_tokens": sum(_TOKENS[s] for s in slugs)}


# --- /api/status ------------------------------------------------------------------------


def test_status_carries_usage_value_with_both_periods(pv, served):
    model = served()["usage_value"]
    assert list(model) == ["title", "basis", "periods"]
    assert model["title"] == "사용량 대비 결과"
    assert [(p["id"], p["label"]) for p in model["periods"]] == [("week", "지난 7일"),
                                                               ("month", "이번 달")]
    week, month = model["periods"]
    # the three recent runs are always inside the last seven days
    assert [(r["project"], r["kind"], r["kind_label"], r["out_tokens"], r["out_tokens_text"])
            for r in week["rows"]] == [("macp", "self", "macp 자기개선", 466_000, "466k"),
                                       ("app", "bug", "버그", 70_000, "70.0k"),
                                       ("app", "ops", "운영", 12_000, "12.0k")]
    assert week["rows"][0]["text"] == "런 1 · 머지 PR 1 · 완료 1 · 취소 0 · 실패 0 · 머지당 466k"
    assert week["rows"][1]["text"].endswith(" · 미계측 런 1")
    total = week["total"]
    assert (total["runs"], total["runs_unmetered"], total["merged_prs"], total["done"]) == (3, 1, 2, 3)
    assert (total["out_tokens"], total["out_tokens_text"]) == (548_000, "548k")
    assert total["tokens_per_merge"] == 274_000
    # the month holds whichever of them started on or after the 1st — never the 40-day-old run
    want = _expected_total(pv, "month")
    assert (month["total"]["runs"], month["total"]["out_tokens"]) == (want["runs"], want["out_tokens"])
    assert "app-discovery-20261117" not in pv.slugs_in("month")
    assert not [r for p in model["periods"] for r in p["rows"] if r["kind"] == "discovery"]


def test_status_numbers_equal_the_engine_build(pv, served):
    # build() on either side of the request: equal unless midnight fell in between
    before = pv.build()
    over_http = served()["usage_value"]
    after = pv.build()
    assert over_http in (before, after)
    assert over_http["periods"][0]["total"]["runs"] == 3


def test_existing_status_keys_are_untouched(pv):
    with mock.patch.object(M, "collect_usage_value", return_value={}):
        without = M.collect_status()
    with_key = M.collect_status()
    assert set(with_key) - set(without) == {"usage_value"}
    assert set(without) - set(with_key) == set()
    for key in ("now", "worker_busy", "worker_state", "worker_state_detail", "paused",
                "runs_today", "active", "attention", "human", "human_later", "projects", "deps",
                "recent", "runs", "agents", "todo", "usage", "account", *M.QUEUE_KEYS):
        assert key in with_key, key
    for key in ("active", "attention", "recent", "runs"):
        assert [sorted(r) for r in with_key[key]] == [sorted(r) for r in without[key]], key
    assert with_key["recent"], "the fixture should give the ledger list some rows"


def test_usage_value_failure_omits_only_that_key(pv, served, caplog):
    caplog.set_level("WARNING", logger="macp.dashboard")
    with mock.patch.object(usage_value, "build", side_effect=RuntimeError("secret detail")):
        d = served()
    assert "usage_value" not in d
    for key in ("now", "active", "attention", "recent", "runs", "todo"):
        assert key in d, key
    lines = [r.getMessage() for r in caplog.records if r.name == "macp.dashboard"]
    assert lines == ["usage_value failed err=RuntimeError"]


def test_the_usage_value_path_runs_no_subprocess(pv):
    def refuse(*args, **kwargs):
        raise AssertionError(f"subprocess started: {args!r}")

    # fail-soft would swallow the AssertionError, so the key being there is the proof
    with mock.patch.object(subprocess, "run", refuse), mock.patch.object(subprocess, "Popen", refuse):
        got = M.collect_usage_value()
    assert got["usage_value"]["periods"][0]["total"]["runs"] == 3


# --- the web card (static page, no browser) ------------------------------------------------


class TestUsageValueCardShell:
    """The card renders what the server sends and nothing else. The live look (390 px,
    no horizontal scroll) is evidence for a run log, not a gate."""

    T = M._HTML_TEMPLATE

    def markup(self) -> str:
        start = self.T.index('<section class="card" id="c-uv"')
        return self.T[start:self.T.index("</section>", start)]

    def script(self) -> str:
        return self.T[self.T.index("// Usage vs results"):self.T.index("function update(d){")]

    def test_the_card_sits_between_the_queue_and_the_system_card(self):
        t = self.T
        assert t.index('id="c-queue"') < t.index('id="c-uv"') < t.index('id="c-recent"')
        assert t.count('id="c-uv"') == 1

    def test_the_card_is_hidden_and_collapsed_until_the_server_sends_rows(self):
        m = self.markup()
        assert m.startswith('<section class="card" id="c-uv" hidden>')
        assert re.search(r'<details id="d-uv"[^>]*>', m).group(0) == '<details id="d-uv">'
        for anchor in ('id="uv-title"', 'id="cnt-uv"', 'id="uv"'):
            assert anchor in m, anchor
        s = self.script()
        assert "if(!ps.some(p=>(p.rows||[]).length)){c.hidden=true;return}" in s
        assert "c.hidden=false" in s

    def test_update_calls_the_renderer_with_the_status_field(self):
        update = self.T[self.T.index("function update(d){"):self.T.index("// ---- tap-through detail")]
        assert update.count("setUsageValue(d.usage_value);") == 1
        assert self.T.count("setUsageValue(") == 2             # the definition and that one call

    def test_a_row_reads_only_the_four_server_fields(self):
        row = next(ln for ln in self.T.splitlines() if ln.startswith("const uvRow="))
        assert set(re.findall(r"\br\.([a-z_]+)", row)) == {"project", "kind_label", "text",
                                                           "out_tokens_text"}
        s = self.script()
        assert set(re.findall(r"\bu\.([a-z_]+)", s)) == {"periods", "title", "basis"}
        assert set(re.findall(r"\bp\.([a-z_]+)", s)) == {"rows", "label", "range", "total"}
        assert set(re.findall(r"\bt\.([a-z_]+)", s)) == {"out_tokens_text", "text"}

    def test_the_client_computes_nothing(self):
        s = self.script()
        for tok in (".sort(", "toFixed", "Math.", "parseInt", "parseFloat", "Number(", ".reduce(",
                    "toLocaleString", "/1000", "/1e"):
            assert tok not in s, tok
        # every server string goes through esc() / pchip(), never raw into the markup
        for raw in ("${r.", "${p.", "${u.", "${t."):
            assert raw not in s, raw

    def test_the_new_markup_and_script_add_no_text_url_or_colour(self):
        for block in (self.markup(), self.script()):
            assert not _HANGUL.search(block)
            assert "http://" not in block and "https://" not in block
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block)
            assert "style=" not in block
        # no new i18n marker: the one it uses already existed
        assert set(re.findall(r"@@([a-z0-9_.]+)@@", self.markup() + self.script())) == {"dash.js.none"}

    def test_the_open_state_is_kept_under_the_section_key(self):
        qsec = next(ln for ln in self.T.splitlines() if ln.startswith("const QSEC="))
        assert "'d-uv':'usage-value'" in qsec
        for old in ("'d-queue':'queue'", "'d-queue-upcoming':'queue-upcoming'",
                    "'d-queue-blocked':'queue-blocked'"):
            assert old in qsec, old

    def test_both_languages_serve_the_card(self):
        for lang in ("ko", "en"):
            page = M.render_html(lang)
            assert '<section class="card" id="c-uv" hidden>' in page
            assert "function setUsageValue(u){" in page
        assert not _HANGUL.search(M.render_html("en"))


# --- the brief: the real brief.sh, a fake claude, the real engine call ---------------------

# Stands in for MACP_PYTHON. It records every call; the usage_value call alone is
# handed to the real interpreter (with this checkout on PYTHONPATH), so the
# section in the brief is what the engine really printed. SEAM_UV_CRASH makes
# that one call die instead.
_FAKE_PY = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "py", "argv": argv, "cwd": os.getcwd()}}) + "\\n")
if "{_UV_MODULE}" in argv:
    if os.environ.get("SEAM_UV_CRASH"):
        sys.stderr.write("boom\\n")
        sys.exit(1)
    os.execve(sys.executable, [sys.executable, *argv],
              {{**os.environ, "PYTHONPATH": {str(_REPO)!r}}})
'''

# The brief agent: writes the brief for yesterday, today and tomorrow, so the
# file brief.sh looks for exists whichever side of midnight either clock is on.
_FAKE_CLAUDE = r'''#!/usr/bin/env python3
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

# Records that the notification ran, and how many briefs carried the table then.
_NOTIFY = r'''#!/bin/sh
n=$(grep -l 'macp:usage-value' "$PWD"/Todo/Briefings/*.md 2>/dev/null | wc -l | tr -d ' ')
printf '{"tool":"notify","marked":%s}\n' "$n" >> "$SEAM_TEST_LOG"
'''

_AGENT_BRIEF = "# Brief\n"


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class _BriefHarness:
    """An installed-shape engine tree (no pyproject, MACP_PYTHON set) holding a copy
    of the real brief.sh, run against an existing vault."""

    def __init__(self, tmp: Path, vault: Path):
        self.tmp = tmp
        self.vault = vault
        self.log = tmp / "calls.jsonl"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        self.home.mkdir(parents=True)
        self.fakepy = _write_exec(self.bin / "fakepy", _FAKE_PY)
        _write_exec(self.bin / "sqlite3", "#!/bin/sh\nexit 0\n")
        for d in (self.bin, self.home / ".local" / "bin"):
            _write_exec(d / "claude", _FAKE_CLAUDE)
        self.engine = tmp / "site" / "todo_system" / "_engine"
        scripts = self.engine / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(_REPO / "scripts" / "brief.sh", scripts / "brief.sh")
        (scripts / "export_db_snapshot.py").write_text("# marker, never executed\n", encoding="utf-8")
        _write_exec(scripts / "notify_kakao.sh", _NOTIFY)
        commands = self.engine / ".claude" / "commands"
        commands.mkdir(parents=True)
        (commands / "brief.md").write_text("---\ndescription: x\n---\nPROMPT-BODY\n", encoding="utf-8")
        self.briefings = vault / "Todo" / "Briefings"

    def run(self, **env: str) -> subprocess.CompletedProcess:
        # From scratch, and TZ is NOT pinned: brief.sh, the engine call and this
        # test process must agree on what day and month it is.
        full = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin",
            "HOME": str(self.home),
            "SEAM_TEST_LOG": str(self.log),
            "GIT_CEILING_DIRECTORIES": str(self.tmp),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "MACP_VAULT_ROOT": str(self.vault),
            "MACP_PYTHON": str(self.fakepy),
            "MACP_LANGUAGE": "ko",
            "MAILROOM_DIR": str(self.tmp / "no-mailroom"),
            **({"TZ": os.environ["TZ"]} if "TZ" in os.environ else {}),
            **env,
        }
        return subprocess.run(["zsh", str(self.engine / "scripts" / "brief.sh")], env=full,
                              capture_output=True, text=True, errors="replace", timeout=180)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()
                if ln.strip()]

    def uv_calls(self) -> list[dict]:
        return [c for c in self.calls() if c["tool"] == "py" and _UV_MODULE in c["argv"]]

    def briefs(self) -> dict[str, str]:
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(self.briefings.glob("*.md"))}

    def with_table(self) -> list[str]:
        return [text for text in self.briefs().values() if usage_value.BRIEF_MARKER in text]


@no_zsh
@no_homebrew_cli
def test_brief_and_status_show_the_same_table(pv, served, tmp_path):
    """AC: the real brief path and the dashboard's /api/status carry the same
    numbers. The brief's section is, character for character, the engine's
    rendering of the model the dashboard served."""
    for attempt in (1, 2):
        day = dt.date.today()
        model = served()["usage_value"]                       # (a) the real /api/status
        h = _BriefHarness(tmp_path / f"run{attempt}", pv.root)
        r = h.run()                                           # (b) the real brief.sh
        if dt.date.today() == day:
            break
        # Midnight fell between the two: both sides moved on. Once more, from a
        # clean briefings dir and past the status cache's TTL.
        shutil.rmtree(h.briefings)
        time.sleep(M.STATUS_TTL_S + 0.5)
    assert r.returncode == 0, (r.stdout, r.stderr)

    marked = h.with_table()
    assert len(marked) == 1, h.briefs()
    assert marked[0] == _AGENT_BRIEF + "\n" + usage_value.render_brief(model) + "\n"
    assert "brief.sh: appended the weekly usage-vs-results table" in r.stdout

    # the same numbers, cell by cell, as the dashboard rows
    week = model["periods"][0]
    assert week["total"]["runs"] > 0
    lines = marked[0].split("\n")
    table = lines[lines.index(f"**{week['label']}** ({week['range']})"):]
    for row in week["rows"]:
        line = next(ln for ln in table if ln.startswith(f"| {row['project']} · {row['kind_label']} |"))
        cells = [c.strip() for c in line.strip("|").split("|")]
        assert cells[1] == row["out_tokens_text"], line
        assert cells[2].split(" ")[0] == str(row["runs"]), line
        assert cells[3] == str(row["merged_prs"]), line
        assert cells[5] == row["tokens_per_merge_text"], line
    total = next(ln for ln in table if ln.startswith("| **합계** |"))
    assert f"| {week['total']['out_tokens_text']} | {week['total']['runs']}" in total

    # the engine call: once, in the vault, pinned to it — and nothing of it reached the agent
    (call,) = h.uv_calls()
    assert call["argv"] == ["-P", "-m", _UV_MODULE, "brief-section", "--vault-root", str(pv.root)]
    assert call["cwd"] == str(pv.root)
    (claude,) = [c for c in h.calls() if c["tool"] == "claude"]
    prompt = claude["argv"][claude["argv"].index("-p") + 1]
    assert "PROMPT-BODY" in prompt
    assert "macp:usage-value" not in prompt and "usage_value" not in prompt


@no_zsh
@no_homebrew_cli
def test_brief_is_untouched_when_a_recent_brief_has_the_table(pv, tmp_path):
    h = _BriefHarness(tmp_path / "run", pv.root)
    h.briefings.mkdir(parents=True)
    earlier = dt.date.today() - dt.timedelta(days=3)
    seeded = "# Brief\n\n" + usage_value.BRIEF_MARKER + "\n## earlier table\n"
    (h.briefings / f"{earlier.isoformat()}.md").write_text(seeded, encoding="utf-8")

    r = h.run()

    assert r.returncode == 0, (r.stdout, r.stderr)
    briefs = h.briefs()
    assert briefs.pop(f"{earlier.isoformat()}.md") == seeded
    assert briefs and set(briefs.values()) == {_AGENT_BRIEF}     # the agent's bytes, as written
    assert "appended the weekly" not in r.stdout
    assert len(h.uv_calls()) == 1                                # asked, and told "not due"


@no_zsh
@no_homebrew_cli
def test_brief_survives_a_crashing_usage_value_cli(pv, tmp_path):
    """`|| true` is load-bearing: under `set -euo pipefail` a non-zero rc in the
    command substitution would end the script before the notification."""
    h = _BriefHarness(tmp_path / "run", pv.root)
    (pv.root / ".secrets").mkdir()
    (pv.root / ".secrets" / "kakao-token.json").write_text("{}", encoding="utf-8")

    r = h.run(SEAM_UV_CRASH="1")

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert len(h.uv_calls()) == 1
    assert set(h.briefs().values()) == {_AGENT_BRIEF}
    assert "appended the weekly" not in r.stdout
    assert "brief.sh: kakao push OK" in r.stdout               # the rest of the script still ran
    assert [c for c in h.calls() if c["tool"] == "notify"] == [{"tool": "notify", "marked": 0}]


@no_zsh
@no_homebrew_cli
def test_table_is_appended_after_the_agent_and_before_the_notification(pv, tmp_path):
    h = _BriefHarness(tmp_path / "run", pv.root)
    (pv.root / ".secrets").mkdir()
    (pv.root / ".secrets" / "kakao-token.json").write_text("{}", encoding="utf-8")

    r = h.run()

    assert r.returncode == 0, (r.stdout, r.stderr)
    order = ["claude" if c["tool"] == "claude" else "notify" if c["tool"] == "notify"
             else "usage_value" if _UV_MODULE in c["argv"] else "py" for c in h.calls()]
    assert [o for o in order if o != "py"] == ["claude", "usage_value", "notify"], order
    # the notification read a brief that already had the table
    assert [c for c in h.calls() if c["tool"] == "notify"] == [{"tool": "notify", "marked": 1}]
    assert len(h.with_table()) == 1
