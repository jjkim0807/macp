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
import re
import subprocess
import threading
from pathlib import Path
from unittest import mock

import pytest

from todo_system import usage_value

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "scripts" / "macp_dashboard.py"
_CASES = Path(__file__).resolve().parent / "usage_value_cases"
TOKEN = "usage-value-test-token"
_HANGUL = re.compile("[가-힣]")


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
                                       ("app", "other", "기타", 12_000, "12.0k")]
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
