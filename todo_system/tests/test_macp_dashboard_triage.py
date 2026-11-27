"""Dashboard wiring of the attention triage (orchestrator/triage.py).

`/api/status` attaches `triage` to attention rows and human parked rows, and
`POST /api/action` runs a card's action — both through the real `serve()`
server and the real `collect_status()` over a tmp vault (todo.db with the real
schema, ledger rows and run dirs taken from the real-case fixtures, a real git
repo for the md commits). Only the collectors that would reach outside the
vault (queue, usage, accounts, process list) are stubbed, and only `gh` is
faked (through the `_ACTION_RUN` seam); git runs for real.

The dashboard module is loaded under its own name so patches here never leak
into test_macp_dashboard's instance.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import resume

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "scripts" / "macp_dashboard.py"
_CASES = _REPO / "todo_system" / "orchestrator" / "tests" / "triage_cases"
TOKEN = "triage-test-token"


def _load():
    spec = importlib.util.spec_from_file_location("macp_dashboard_triage", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _case(name: str) -> dict:
    return json.loads((_CASES / name).read_text(encoding="utf-8"))


class _TriageVault(unittest.TestCase):
    """A tmp vault with two parked rows from the real cases (① review PR, ④ orphan
    redelivery), one in-flight row and one deferred row, served by serve()."""

    REVIEW = "demo-review-pr"
    ORPHAN = "app-public-1-orphan"
    ACTIVE = "demo-running"
    DEFERRED = "demo-deferred"

    def setUp(self):
        from todo_system import cli
        from todo_system.orchestrator import triage
        p = mock.patch.dict(os.environ, {"MACP_LANGUAGE": "ko"})
        p.start()
        self.addCleanup(p.stop)
        triage._CACHE.clear()
        self.addCleanup(triage._CACHE.clear)
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for d in ("Todo/items", "Worker/assignments", "Worker/agents", "Worker/runs",
                  "Worker/_state", "Memory/sessions"):
            (self.root / d).mkdir(parents=True)
        (self.root / "Worker/agents/registry.yaml").write_text(
            "agents:\n  - id: owner\n    kind: human\n    projects: ['prep-*']\n"
            "  - id: worker-default\n    kind: ai-local\n    projects: ['*']\n",
            encoding="utf-8")
        self.items = self.root / "Todo" / "items"
        self.assign = self.root / "Worker" / "assignments"
        for name in ("review_pr.json", "orphan_redelivery.json"):
            self._install_case(_case(name))
        self._row(self.ACTIVE, state="in_progress")
        self._row(self.DEFERRED, state="failed", checkout_busy=True)
        for slug in (self.ACTIVE, self.DEFERRED):
            self._md(slug, "---\nstatus: open\nworker_mode: auto\n---\n\n# x\n")
        self.db = self.root / "Todo" / "todo.db"
        con = cli.db_connect(self.db)
        cli.init_db(con)
        for slug in (self.REVIEW, self.ORPHAN, self.ACTIVE, self.DEFERRED):
            con.execute("INSERT INTO tasks (slug, title, path, status, worker_mode, imported_at, "
                        "tags) VALUES (?,?,?,'open','auto','2026-01-01T00:00:00','todo')",
                        (slug, slug, str(self.items / f"{slug}.md")))
        con.commit()
        con.close()
        paths = {"VAULT": self.root, "ITEMS": self.items, "ASSIGN": self.assign,
                 "RUNS": self.root / "Worker/runs", "STATE": self.root / "Worker/_state",
                 "TODO_DB": self.db, "SESSIONS": self.root / "Memory/sessions",
                 "REGISTRY": self.root / "Worker/agents/registry.yaml"}
        stubs = {"collect_queue": {k: [] for k in M.QUEUE_KEYS}, "collect_usage": None,
                 "_worker_running": [], "collect_deps": [], "collect_account": {},
                 "collect_account_runtime": {}, "collect_account_slots": {}}
        patches = [mock.patch.object(M, k, v) for k, v in paths.items()]
        patches += [mock.patch.object(M, k, return_value=v) for k, v in stubs.items()]
        patches.append(mock.patch("todo_system.macp.config.load_profile",
                                  side_effect=RuntimeError("no profile in tests")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- vault helpers ---------------------------------------------------------
    def _md(self, slug: str, text: str) -> Path:
        p = self.items / f"{slug}.md"
        p.write_text(text, encoding="utf-8")
        return p

    def _row(self, slug: str, **kw) -> None:
        row = {"task_id": slug, "agent_id": "worker-default", "corr_id": "c", "kind": "ai-local",
               "state": "awaiting_human", "created_ts": "2026-11-20T00:00:00+09:00",
               "updated_ts": "2026-11-20T00:00:00+09:00", "history": [],
               "payload": {"worker_mode": "auto"}}
        row.update(kw)
        (self.assign / f"{slug}.json").write_text(json.dumps(row), encoding="utf-8")

    def _install_case(self, case: dict) -> None:
        slug = case["slug"]
        (self.assign / f"{slug}.json").write_text(json.dumps(case["ledger"], ensure_ascii=False),
                                                  encoding="utf-8")
        self._md(slug, case["task_md"])
        if case.get("run"):
            rd = self.root / "Worker/runs" / case["run"]["name"]
            rd.mkdir()
            for name, text in case["run"]["files"].items():
                (rd / name).write_text(text, encoding="utf-8")

    # -- HTTP ------------------------------------------------------------------
    def serve(self) -> int:
        srv = M.serve("127.0.0.1", 0, TOKEN)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def request(self, port: int, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None) -> tuple[int, bytes]:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def status(self, port: int | None = None) -> dict:
        code, body = self.request(port or self.serve(), "GET", "/api/status",
                                  headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(code, 200)
        return json.loads(body)


class StatusCarriesTriage(_TriageVault):
    def test_attention_and_parked_rows_carry_the_same_card(self):
        d = self.status()
        attn = {r["task"]: r for r in d["attention"]}
        parked = {r["slug"]: r for r in d["human"] if r["why"] == "parked"}
        self.assertEqual(set(attn), {self.REVIEW, self.ORPHAN})
        self.assertEqual(set(parked), {self.REVIEW, self.ORPHAN})
        card = attn[self.REVIEW]["triage"]
        self.assertEqual(card["rule"], "review_pr")
        self.assertEqual([a["id"] for a in card["actions"]], ["merge", "instruct", "cancel"])
        self.assertEqual([a["label"] for a in card["actions"]], ["머지", "지시 추가", "취소"])
        self.assertIn("머지만 남았", card["reason"])
        self.assertEqual(parked[self.REVIEW]["triage"], card)
        self.assertEqual(attn[self.ORPHAN]["triage"]["rule"], "orphan_redelivery")
        self.assertEqual(parked[self.ORPHAN]["triage"], attn[self.ORPHAN]["triage"])
        # recent shares the attention row objects, so the card rides along
        recent = {r["task"]: r for r in d["recent"]}
        self.assertEqual(recent[self.REVIEW]["triage"], card)

    def test_existing_fields_are_untouched(self):
        d = self.status()
        row = {r["task"]: r for r in d["attention"]}[self.REVIEW]
        for key in ("task", "state", "agent", "note", "age", "refail", "group", "title"):
            self.assertIn(key, row)
        self.assertEqual(row["state"], "awaiting_human")
        parked = {r["slug"]: r for r in d["human"] if r["why"] == "parked"}[self.REVIEW]
        self.assertEqual(sorted(k for k in parked if k != "triage"), sorted(
            ["slug", "title", "category", "action", "why", "project", "due", "not_before",
             "urgent", "later", "waiting_on", "ready_since", "group"]))

    def test_active_and_deferred_rows_have_no_card(self):
        d = self.status()
        self.assertEqual([r["task"] for r in d["active"]], [self.ACTIVE])
        self.assertNotIn("triage", d["active"][0])
        recent = {r["task"]: r for r in d["recent"]}
        self.assertEqual(recent[self.DEFERRED]["state"], "deferred")
        self.assertNotIn("triage", recent[self.DEFERRED])
        self.assertNotIn(self.DEFERRED, {r["task"] for r in d["attention"]})

    def test_one_failing_diagnosis_leaves_the_others(self):
        from todo_system.orchestrator import triage
        real = triage.diagnose

        def flaky(facts):
            if facts and facts.get("slug") == self.REVIEW:
                raise RuntimeError("boom")
            return real(facts)

        with mock.patch.object(triage, "diagnose", flaky), \
                self.assertLogs("macp.dashboard", level="WARNING") as logs:
            d = self.status()
        attn = {r["task"]: r for r in d["attention"]}
        self.assertNotIn("triage", attn[self.REVIEW])
        self.assertEqual(attn[self.ORPHAN]["triage"]["rule"], "orphan_redelivery")
        self.assertTrue(any(f"triage failed task={self.REVIEW} err=RuntimeError" in line
                            for line in logs.output), logs.output)

    def test_a_closed_md_drops_the_card_before_the_db_catches_up(self):
        md = self.items / f"{self.REVIEW}.md"
        md.write_text(md.read_text(encoding="utf-8").replace("status: open", "status: done"),
                      encoding="utf-8")
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT status FROM tasks WHERE slug=?",
                                     (self.REVIEW,)).fetchone(), ("open",))
        con.close()
        attn = {r["task"]: r for r in self.status()["attention"]}
        self.assertIn(self.REVIEW, attn)                 # todo.db still says open
        self.assertNotIn("triage", attn[self.REVIEW])    # the md already says done

    def test_the_triage_path_runs_no_subprocess(self):
        # /api/status stays a local read: no gh, git or network on the triage path
        attention, parked = [{"task": self.REVIEW}], [{"slug": self.ORPHAN, "why": "parked"}]
        with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess")):
            M._attach_triage(attention, parked)
        self.assertEqual(attention[0]["triage"]["rule"], "review_pr")
        self.assertEqual(parked[0]["triage"]["rule"], "orphan_redelivery")


# --- POST /api/action ---------------------------------------------------------------

_HANGUL_RE = re.compile("[가-힣]")
_RAW_RE = re.compile(r"HTTP \d{3}|http_\d|rc=|Traceback|GraphQL|stderr|/tmp/")


class _GitTriageVault(_TriageVault):
    """The same vault as a git repo on main (todo.db, runs and _state ignored, as
    in the real vault), served once per test; gh answers through _ACTION_RUN."""

    def setUp(self):
        super().setUp()
        (self.root / ".gitignore").write_text("Todo/todo.db\nWorker/_state/\nWorker/runs/\n",
                                              encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.gh_calls: list[list[str]] = []
        real_run = subprocess.run

        def fake_run(argv, **kw):
            if argv and argv[0] == "gh":
                self.gh_calls.append(list(argv))
                if argv[:3] == ["gh", "pr", "view"]:
                    out = json.dumps({"state": "OPEN", "headRefName": f"worker/{self.REVIEW}"})
                    return subprocess.CompletedProcess(argv, 0, out, "")
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_run(argv, **kw)

        p = mock.patch.object(M, "_ACTION_RUN", fake_run)
        p.start()
        self.addCleanup(p.stop)
        self.port = self.serve()

    def git(self, *args) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True,
                              text=True, check=True).stdout

    def post(self, body=None, *, token=TOKEN, ctype="application/json", raw=None):
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": ctype} if ctype else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        code, out = self.request(self.port, "POST", "/api/action", body=data, headers=headers)
        try:
            return code, json.loads(out)
        except ValueError:
            return code, out

    def assertSentence(self, res: dict, ok: bool):
        self.assertEqual(sorted(res), ["message", "ok"])
        self.assertIs(res["ok"], ok)
        self.assertTrue(_HANGUL_RE.search(res["message"]), res)
        self.assertIsNone(_RAW_RE.search(res["message"]), res)

    def head_files(self) -> list[str]:
        return [f for f in self.git("log", "-1", "--format=", "--name-only").splitlines() if f]

    def card(self, slug: str, port: int | None = None) -> dict | None:
        rows = {r["task"]: r for r in self.status(port)["attention"]}
        return rows[slug].get("triage")


class ActionRoute(_GitTriageVault):
    def nothing_ran(self):
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-list", "--count", "HEAD").strip(), "1")
        for slug in (self.REVIEW, self.ORPHAN):
            self.assertFalse(resume.record_path(self.root, slug).exists(), slug)

    def test_refusals_run_nothing(self):
        review, orphan = self.REVIEW, self.ORPHAN
        cases = [
            (dict(body={"task": review, "action": "merge"}, token=None), 401),
            (dict(body={"task": review, "action": "merge"}, token="wrong"), 401),
            (dict(body={"task": review, "action": "merge"}, ctype="text/plain"), 415),
            (dict(raw=b"x" * 4097), 413),
            (dict(raw=b""), 400),
            (dict(raw=b"{not json"), 400),
            (dict(raw=b"[1]"), 400),
            (dict(body={"task": "../etc", "action": "merge"}), 400),
            (dict(body={"task": review}), 400),
            # irreversible steps have no action id at all
            (dict(body={"task": review, "action": "deploy"}), 400),
            (dict(body={"task": review, "action": "submit"}), 400),
            (dict(body={"task": review, "action": "publish"}), 400),
            (dict(body={"task": review, "action": "delete"}), 400),
            (dict(body={"task": "no-such-task", "action": "cancel"}), 404),
            # actions the fresh card does not offer
            (dict(body={"task": orphan, "action": "merge"}), 409),
            (dict(body={"task": review, "action": "resume"}), 409),
            (dict(body={"task": self.ACTIVE, "action": "cancel"}), 409),
            # an instruction is one line of 1..500 chars
            (dict(body={"task": review, "action": "instruct"}), 400),
            (dict(body={"task": review, "action": "instruct", "text": "  "}), 400),
            (dict(body={"task": review, "action": "instruct", "text": "a\nb"}), 400),
            (dict(body={"task": review, "action": "instruct", "text": "x" * 501}), 400),
            (dict(body={"task": review, "action": "instruct", "text": 5}), 400),
        ]
        for kw, want in cases:
            code, res = self.post(**kw)
            self.assertEqual(code, want, kw)
            if code != 401:                      # the gate's own reply is unchanged
                self.assertSentence(res, ok=False)
        self.nothing_ran()

    def test_an_umbrella_in_progress_is_refused(self):
        with mock.patch.object(M, "_umbrellas_in_progress", return_value={self.ORPHAN}):
            code, res = self.post({"task": self.ORPHAN, "action": "cancel"})
        self.assertEqual(code, 409)
        self.assertSentence(res, ok=False)
        self.nothing_ran()

    def test_merge_is_gh_squash_on_the_cards_pr(self):
        with self.assertLogs("macp.dashboard", level="INFO") as logs:
            code, res = self.post({"task": self.REVIEW, "action": "merge"})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        self.assertIn("머지했어요", res["message"])
        self.assertEqual(self.gh_calls, [
            ["gh", "pr", "view", "312", "--repo", "example/app", "--json", "state,headRefName"],
            ["gh", "pr", "merge", "312", "--repo", "example/app", "--squash"]])
        self.assertEqual(self.git("status", "--porcelain"), "")    # reconcile_merged closes it
        self.assertTrue(any(f"dashboard action task={self.REVIEW} action=merge rule=review_pr "
                            "ok=True" in line for line in logs.output), logs.output)

    def test_resume_reserves_like_cli_resume(self):
        code, res = self.post({"task": self.ORPHAN, "action": "resume"})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        self.assertIn("재개를 예약했어요", res["message"])
        record = json.loads(resume.record_path(self.root, self.ORPHAN).read_text(encoding="utf-8"))
        self.assertEqual((record["note"], record["after_commit"]), ("dashboard", ""))
        card = self.card(self.ORPHAN, self.port)
        self.assertNotIn("resume", [a["id"] for a in card["actions"]])   # not offered twice
        self.assertEqual(self.post({"task": self.ORPHAN, "action": "resume"})[0], 409)

    def test_cancel_commits_the_md_and_the_next_poll_drops_the_card(self):
        self.assertEqual(self.card(self.ORPHAN, self.port)["rule"], "orphan_redelivery")
        code, res = self.post({"task": self.ORPHAN, "action": "cancel"})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        self.assertIn("취소했어요", res["message"])
        md = (self.items / f"{self.ORPHAN}.md").read_text(encoding="utf-8")
        self.assertIn("\nstatus: cancelled\n", md)
        self.assertIn("대시보드에서 취소(사용자)", md)
        self.assertEqual(self.head_files(), [f"Todo/items/{self.ORPHAN}.md"])
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(),
                         f"chore: cancel {self.ORPHAN} (dashboard)")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.gh_calls, [])
        # the POST invalidated the status cache: this poll is recomputed at once
        self.assertIsNone(self.card(self.ORPHAN, self.port))

    def test_instruct_commits_the_user_instruction_then_reserves_resume(self):
        with self.assertLogs("macp.dashboard", level="INFO") as logs:
            code, res = self.post({"task": self.REVIEW, "action": "instruct",
                                   "text": "  비밀 지시: 리베이스하고 다시 돌려  "})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        self.assertIn("지시를 남기고", res["message"])
        md = (self.items / f"{self.REVIEW}.md").read_text(encoding="utf-8")
        self.assertRegex(md, r"\n- \d{4}-\d{2}-\d{2}: 사용자 지시\(대시보드\): "
                             r"비밀 지시: 리베이스하고 다시 돌려\n$")
        self.assertEqual(self.head_files(), [f"Todo/items/{self.REVIEW}.md"])
        record = json.loads(resume.record_path(self.root, self.REVIEW).read_text(encoding="utf-8"))
        self.assertEqual(record["after_commit"], self.git("rev-parse", "HEAD").strip())
        self.assertEqual(self.gh_calls, [])
        # the log keeps the length, never the instruction itself
        self.assertTrue(any("action=instruct" in line and "text_len=" in line
                            for line in logs.output), logs.output)
        self.assertFalse(any("비밀 지시" in line for line in logs.output), logs.output)

    def test_human_gate_leaves_only_cancel(self):
        md = self.items / f"{self.REVIEW}.md"
        md.write_text(md.read_text(encoding="utf-8").replace(
            "status: open\n", "status: open\nhuman_gate: true\n"), encoding="utf-8")
        self.git("commit", "-q", "-am", "human gate")
        card = self.card(self.REVIEW, self.port)
        self.assertEqual([a["id"] for a in card["actions"]], ["cancel"])
        for action, text in (("merge", None), ("resume", None), ("instruct", "go on")):
            body = {"task": self.REVIEW, "action": action, **({"text": text} if text else {})}
            code, res = self.post(body)
            self.assertEqual(code, 409, action)
            self.assertSentence(res, ok=False)
        self.assertEqual(self.gh_calls, [])
        code, res = self.post({"task": self.REVIEW, "action": "cancel"})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)

    def test_a_failed_action_is_a_sentence_too(self):
        self.git("checkout", "-q", "-b", "worker/other")         # live vault off main
        code, res = self.post({"task": self.ORPHAN, "action": "cancel"})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=False)
        self.assertIn("main이 아니라", res["message"])
        self.assertIn("status: open", (self.items / f"{self.ORPHAN}.md").read_text(encoding="utf-8"))


# --- decision cards: /api/status `decision` + POST /api/action answer ------------------

_ROW_KEYS = ["slug", "title", "category", "action", "why", "project", "due", "not_before",
             "urgent", "later", "waiting_on", "ready_since", "group"]


class _DecisionVault(_GitTriageVault):
    """_GitTriageVault plus decision tasks (all synthetic), committed on main:

    PICK      needs_decision + three options + a time block still ahead; no ledger row
    TAGGED    the `decision` tag only (also in todo.db), no options
    HUMAN     assigned to the human agent `owner` (ledger kind human, awaiting_human)
    GROUPED   a decision inside a project (`demo`), next to GROUPMATE, a plain task
    PLAIN     an ordinary manual task
    LATER     a decision whose not_before is still ahead
    BLOCKED   a decision that is worker_mode: blocked
    """

    PICK = "pick-budget"
    TAGGED = "pick-tagged"
    HUMAN = "prep-pick"
    GROUPED = "demo-1-pick"
    GROUPMATE = "demo-2-plain"
    PLAIN = "plain-manual"
    LATER = "pick-later"
    BLOCKED = "pick-blocked"
    OPTIONS = ["(a) raise the cap to 30", "(b) split a separate budget: API only", "(c) hold"]
    HUMAN_OPTIONS = ["(a) delete it now", "(b) keep it"]

    def setUp(self):
        super().setUp()
        opts = "decision_options:\n" + "".join(f'  - "{o}"\n' for o in self.OPTIONS)
        human_opts = "decision_options:\n" + "".join(f'  - "{o}"\n' for o in self.HUMAN_OPTIONS)
        self._task(self.PICK, "manual",
                   'needs_decision: true\nnext_action: "Pick a budget plan"\n'
                   f'scheduled: "2099-01-05 10:00~10:30"\n{opts}',
                   next_action="Pick a budget plan")
        self._task(self.TAGGED, "manual", 'next_action: "Pick a colour"\n',
                   tags=("todo", "decision"), next_action="Pick a colour")
        self._task(self.HUMAN, "delegate",
                   f'needs_decision: true\nnext_action: "Pick what to do with the export"\n'
                   f"{human_opts}", next_action="Pick what to do with the export")
        self._row(self.HUMAN, agent_id="owner", kind="human", corr_id="c-7",
                  payload={"worker_mode": "delegate"})
        self._task(self.GROUPED, "manual", 'needs_decision: true\nnext_action: "Pick one"\n',
                   next_action="Pick one")
        self._task(self.GROUPMATE, "manual", 'next_action: "Do it"\n', next_action="Do it")
        self._task(self.PLAIN, "manual", 'next_action: "Do it"\n', next_action="Do it")
        self._task(self.LATER, "manual",
                   f'needs_decision: true\nnot_before: 2099-01-01\nnext_action: "Pick later"\n{opts}',
                   not_before="2099-01-01")
        self._task(self.BLOCKED, "blocked", f"needs_decision: true\n{opts}")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "decision tasks")
        self.commits = int(self.git("rev-list", "--count", "HEAD").strip())

    def _task(self, slug: str, mode: str, front: str, *, tags=("todo",), next_action: str = "",
              not_before: str | None = None) -> None:
        tag_lines = "".join(f"  - {t}\n" for t in tags)
        self._md(slug, f"---\ntags:\n{tag_lines}status: open\nupdated: 2026-11-20\n"
                       f"worker_mode: {mode}\n{front}---\n\n# {slug}\n\n- [ ] step\n\n"
                       "## Progress Log\n\n- 2026-11-20: created\n")
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tasks (slug, title, path, status, worker_mode, imported_at, tags, "
                    "next_action, not_before) VALUES (?,?,?,'open',?,'2026-01-01T00:00:00',?,?,?)",
                    (slug, slug, str(self.items / f"{slug}.md"), mode, ",".join(tags),
                     next_action, not_before))
        con.commit()
        con.close()

    def human(self, port: int | None = None) -> dict:
        d = self.status(port)
        self.last = d
        return {r["slug"]: r for r in d["human"]}

    def md_text(self, slug: str) -> str:
        return (self.items / f"{slug}.md").read_text(encoding="utf-8")


class DecisionStatus(_DecisionVault):
    def test_an_owned_decision_row_carries_question_and_options(self):
        row = self.human()[self.PICK]
        self.assertEqual(row["why"], "owned")
        self.assertEqual(row["decision"], {"question": "Pick a budget plan",
                                           "options": self.OPTIONS})
        # urgent whatever the due date or the time block still ahead, and never folded
        self.assertIs(row["urgent"], True)
        self.assertEqual(row["due"], "")
        self.assertEqual(row["group"], "")
        self.assertEqual(sorted(k for k in row if k != "decision"), sorted(_ROW_KEYS))

    def test_decision_rows_come_first(self):
        self.human()
        human = self.last["human"]
        flags = ["decision" in r for r in human]
        n = flags.count(True)
        self.assertEqual(n, 4)                                   # PICK, TAGGED, HUMAN, GROUPED
        self.assertEqual(flags, [True] * n + [False] * (len(human) - n))
        order = [r["slug"] for r in human]
        for parked in (self.REVIEW, self.ORPHAN):                # parked rows used to lead
            self.assertLess(order.index(self.PICK), order.index(parked))
        self.assertTrue(all(r["urgent"] for r in human[:n]))
        # the rows behind them keep the order collect_human gave them
        rest = human[n:]
        self.assertEqual(rest, sorted(rest, key=M._human_sort_key))

    def test_a_tag_only_decision_has_no_options(self):
        row = self.human()[self.TAGGED]
        self.assertEqual(row["why"], "owned")
        self.assertEqual(row["decision"], {"question": "Pick a colour", "options": []})

    def test_a_human_assigned_decision_carries_both_cards(self):
        row = self.human()[self.HUMAN]
        self.assertEqual(row["why"], "parked")
        self.assertIn("rule", row["triage"])
        self.assertEqual(row["decision"], {"question": "Pick what to do with the export",
                                           "options": self.HUMAN_OPTIONS})
        self.assertEqual(sorted(k for k in row if k not in ("decision", "triage")),
                         sorted(_ROW_KEYS))

    def test_a_decision_in_a_project_is_not_folded_into_its_group(self):
        human = self.human()
        self.assertEqual((human[self.GROUPED]["project"], human[self.GROUPED]["group"]),
                         ("demo", ""))
        self.assertIn("decision", human[self.GROUPED])
        self.assertEqual((human[self.GROUPMATE]["project"], human[self.GROUPMATE]["group"]),
                         ("demo", "demo"))

    def test_rows_that_are_not_answerable_decisions_have_no_card(self):
        human = self.human()
        for slug in (self.REVIEW, self.ORPHAN, self.PLAIN, self.GROUPMATE, self.BLOCKED):
            self.assertNotIn("decision", human[slug], slug)
        self.assertEqual(human[self.BLOCKED]["why"], "blocked")
        self.assertIs(human[self.BLOCKED]["urgent"], False)
        self.assertIs(human[self.PLAIN]["urgent"], False)
        later = {r["slug"]: r for r in self.last["human_later"]}
        self.assertEqual(set(later), {self.LATER})
        self.assertNotIn("decision", later[self.LATER])
        self.assertNotIn(self.LATER, human)
        # the card rides on human rows only
        for key in ("attention", "active", "recent", "projects"):
            for r in self.last[key]:
                self.assertNotIn("decision", r, key)

    def test_the_md_decides_before_the_db_catches_up(self):
        # answered (or un-tagged) in the md while todo.db still says `decision`
        md = self.items / f"{self.TAGGED}.md"
        md.write_text(self.md_text(self.TAGGED).replace("  - decision\n", ""), encoding="utf-8")
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT tags FROM tasks WHERE slug=?",
                                     (self.TAGGED,)).fetchone(), ("todo,decision",))
        con.close()
        row = self.human()[self.TAGGED]            # a fresh server: nothing cached
        self.assertEqual(row["why"], "owned")      # todo.db still says so
        self.assertNotIn("decision", row)          # the md no longer does
        self.assertIs(row["urgent"], False)
        self.assertIn("decision", self.human()[self.PICK])

    def test_one_failing_card_leaves_the_others(self):
        from todo_system.orchestrator import triage
        real = triage.decision_card

        def flaky(slug, **kw):
            if slug == self.PICK:
                raise RuntimeError("boom")
            return real(slug, **kw)

        with mock.patch.object(triage, "decision_card", flaky), \
                self.assertLogs("macp.dashboard", level="WARNING") as logs:
            human = self.human()
        self.assertNotIn("decision", human[self.PICK])
        self.assertIs(human[self.PICK]["urgent"], False)        # as collect_human left it
        self.assertIn("decision", human[self.TAGGED])
        self.assertIn("decision", human[self.HUMAN])
        self.assertTrue(any(f"decision card failed task={self.PICK} err=RuntimeError" in line
                            for line in logs.output), logs.output)

    def test_the_decision_path_runs_no_subprocess_and_writes_nothing(self):
        rows = [{"slug": self.PICK, "why": "owned", "urgent": False},
                {"slug": self.PLAIN, "why": "manual", "urgent": False},
                {"slug": self.HUMAN, "why": "parked", "urgent": True},
                {"slug": self.BLOCKED, "why": "blocked", "urgent": False},
                {"slug": self.LATER, "why": "owned", "urgent": False}]
        with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess")):
            M._attach_decisions(rows)
        self.assertEqual([r["slug"] for r in rows],
                         [self.PICK, self.HUMAN, self.PLAIN, self.BLOCKED, self.LATER])
        self.assertEqual([("decision" in r, r["urgent"]) for r in rows],
                         [(True, True), (True, True), (False, False), (False, False),
                          (False, False)])
        self.assertEqual(self.git("status", "--porcelain"), "")


class AnswerRoute(_DecisionVault):
    """POST /api/action {task, action: "answer", option | text} through the real
    server: the choice is recorded in the task md (one commit), a waiting human
    assignment gets one reply, the status cache is dropped — and nothing else runs."""

    def setUp(self):
        super().setUp()
        self.inbox = self.root / "Worker" / "agents" / "orchestrator" / "inbox"

    def envelopes(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.inbox.glob("*.json"))]

    def commit_count(self) -> int:
        return int(self.git("rev-list", "--count", "HEAD").strip())

    def nothing_ran(self):
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.commit_count(), self.commits)
        self.assertFalse(self.inbox.exists())
        for slug in (self.PICK, self.TAGGED, self.HUMAN, self.REVIEW):
            self.assertFalse(resume.record_path(self.root, slug).exists(), slug)

    def serve_cached(self) -> int:
        """A server whose status body does not expire by age within a test (TTL
        one minute instead of two seconds), so a changed /api/status can only
        come from an invalidation. Not longer: invalidate() zeroes the
        monotonic timestamp, which only reads as expired once the host has
        been up longer than the TTL."""
        srv = M.serve("127.0.0.1", 0, TOKEN)
        srv.RequestHandlerClass._status_cache.ttl = 60.0
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_status_carries_options_then_answer_option_2_records_it(self):
        self.port = self.serve_cached()
        # 1. the decision row carries the options (this GET fills the cache)
        row = self.human(self.port)[self.PICK]
        self.assertEqual(row["decision"]["options"], self.OPTIONS)
        # the cache really holds: an md edit made behind the server's back is not seen
        tagged = self.items / f"{self.TAGGED}.md"
        tagged.write_text(self.md_text(self.TAGGED).replace("  - decision\n", ""), encoding="utf-8")
        self.git("commit", "-q", "-am", "untag by hand")
        self.assertIn("decision", self.human(self.port)[self.TAGGED])
        # 2. answer with option 2
        with self.assertLogs("macp.dashboard", level="INFO") as logs:
            code, res = self.post({"task": self.PICK, "action": "answer", "option": 2})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        self.assertIn(self.OPTIONS[1], res["message"])
        # 3. the md: the chosen option verbatim under a dated Progress Log line, marker down
        md = self.md_text(self.PICK)
        self.assertRegex(md, r"\n- \d{4}-\d{2}-\d{2}: 결정\(대시보드\): "
                             + re.escape(self.OPTIONS[1]) + r"\n$")
        self.assertIn("\nneeds_decision: false\n", md)
        self.assertNotIn("decision_options", md)
        self.assertNotIn(self.OPTIONS[0], md)
        self.assertIn(f'\nnext_action: "결정 반영 — {self.OPTIONS[1]}"\n', md)
        self.assertIn("\nstatus: open\n", md)
        self.assertEqual(self.head_files(), [f"Todo/items/{self.PICK}.md"])
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(),
                         f"chore: record decision for {self.PICK} (dashboard)")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.gh_calls, [])
        self.assertTrue(any(f"dashboard action task={self.PICK} action=answer rule=decision "
                            "ok=True" in line for line in logs.output), logs.output)
        # 4. the POST dropped the cached status: the same server recomputes at once
        human = self.human(self.port)
        self.assertNotIn("decision", human[self.PICK])
        self.assertNotIn("decision", human[self.TAGGED])

    def test_a_text_answer_is_trimmed_and_kept_out_of_the_log(self):
        with self.assertLogs("macp.dashboard", level="INFO") as logs:
            code, res = self.post({"task": self.PICK, "action": "answer",
                                   "text": "  둘 다 아님 — 12월에   다시  "})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        self.assertRegex(self.md_text(self.PICK),
                         r"\n- \d{4}-\d{2}-\d{2}: 결정\(대시보드\): 둘 다 아님 — 12월에 다시\n$")
        self.assertTrue(any("action=answer" in line and "text_len=" in line
                            for line in logs.output), logs.output)
        self.assertFalse(any("둘 다 아님" in line for line in logs.output), logs.output)
        # a tag-only decision has no options, so a line of text is the way to answer it
        code, res = self.post({"task": self.TAGGED, "action": "answer", "text": "blue"})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        md = self.md_text(self.TAGGED)
        self.assertNotIn("  - decision\n", md)
        self.assertIn("\nneeds_decision: false\n", md)

    def test_answer_refusals_run_nothing(self):
        pick = self.PICK

        def ans(task, **kw):
            return {"task": task, "action": "answer", **kw}

        cases = [
            (dict(body=ans(pick, option=1), token=None), 401),
            # not a decision anyone can answer now
            (dict(body=ans(self.REVIEW, option=1)), 409),
            (dict(body=ans(self.ACTIVE, text="go")), 409),
            (dict(body=ans(self.PLAIN, text="go")), 409),
            (dict(body=ans(self.BLOCKED, option=1)), 409),
            (dict(body=ans(self.LATER, option=1)), 409),
            (dict(body=ans("no-such-task", option=1)), 404),
            (dict(body=ans("../etc", option=1)), 400),
            # an option is a position in the list the server sent: 1..3 here
            (dict(body=ans(pick, option=4)), 400),
            (dict(body=ans(pick, option=0)), 400),
            (dict(body=ans(pick, option=-1)), 400),
            (dict(body=ans(pick, option="2")), 400),
            (dict(body=ans(pick, option=2.5)), 400),
            (dict(body=ans(pick, option=True)), 400),
            (dict(body=ans(pick, option=[2])), 400),
            (dict(body=ans(self.TAGGED, option=1)), 400),          # a card with no options
            # exactly one of option / text
            (dict(body=ans(pick, option=2, text="b")), 400),
            (dict(body=ans(pick)), 400),
            (dict(body=ans(pick, option=None, text=None)), 400),
            # a text answer is one line of 1..500 chars
            (dict(body=ans(pick, text="")), 400),
            (dict(body=ans(pick, text="   ")), 400),
            (dict(body=ans(pick, text="a\nb")), 400),
            (dict(body=ans(pick, text="x" * 501)), 400),
            (dict(body=ans(pick, text=5)), 400),
        ]
        for kw, want in cases:
            code, res = self.post(**kw)
            self.assertEqual(code, want, kw)
            if code != 401:
                self.assertSentence(res, ok=False)
        self.nothing_ran()
        # 500 chars is still one answer
        code, res = self.post(ans(pick, text="y" * 500))
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)

    def test_the_same_answer_twice_records_once(self):
        code, res = self.post({"task": self.PICK, "action": "answer", "option": 1})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        code, res = self.post({"task": self.PICK, "action": "answer", "option": 1})
        self.assertEqual(code, 409)
        self.assertSentence(res, ok=False)
        self.assertEqual(self.md_text(self.PICK).count("결정(대시보드)"), 1)
        self.assertEqual(self.commit_count(), self.commits + 1)

    def test_human_assignment_answer_sends_one_reply(self):
        ledger = self.assign / f"{self.HUMAN}.json"
        before = ledger.read_bytes()
        code, res = self.post({"task": self.HUMAN, "action": "answer", "option": 2})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        (env,) = self.envelopes()
        self.assertEqual((env["type"], env["to"], env["from"], env["task_id"], env["corr_id"]),
                         ("NEEDS_INPUT", "orchestrator", "owner", self.HUMAN, "c-7"))
        self.assertEqual(env["payload"], {"note": f"answered via dashboard: {self.HUMAN_OPTIONS[1]}"})
        self.assertEqual(ledger.read_bytes(), before)          # the ledger is the orchestrator's
        self.assertIn(f"결정(대시보드): {self.HUMAN_OPTIONS[1]}", self.md_text(self.HUMAN))
        self.assertEqual(self.head_files(), [f"Todo/items/{self.HUMAN}.md"])
        # a second click is refused before anything is sent again
        code, res = self.post({"task": self.HUMAN, "action": "answer", "option": 1})
        self.assertEqual(code, 409)
        self.assertSentence(res, ok=False)
        self.assertEqual(len(self.envelopes()), 1)
        # an answer to a task nobody was assigned sends nothing
        code, _ = self.post({"task": self.PICK, "action": "answer", "option": 1})
        self.assertEqual(code, 200)
        self.assertEqual(len(self.envelopes()), 1)
        # with the marker down the parked row offers a resume: handing it to a
        # worker stays a separate click
        row = self.human(self.port)[self.HUMAN]
        self.assertNotIn("decision", row)
        self.assertEqual(row["triage"]["rule"], "human_misroute")
        self.assertIn("resume", [a["id"] for a in row["triage"]["actions"]])
        self.assertFalse(resume.record_path(self.root, self.HUMAN).exists())

    def test_answer_only_records(self):
        # The boundary of the button: it writes the choice down. A choice that names
        # an irreversible step (delete, pay, deploy) is recorded and nothing runs.
        ran: list[list[str]] = []
        real_run = subprocess.run

        def recording(argv, **kw):
            ran.append(list(argv))
            return real_run(argv, **kw)

        files_before = sorted(p.name for p in self.items.iterdir())
        ledger_before = {p.name: p.read_bytes() for p in self.assign.iterdir()}
        with mock.patch.object(M, "_ACTION_RUN", recording):
            code, res = self.post({"task": self.HUMAN, "action": "answer", "option": 1})
            self.assertEqual(code, 200)
            self.assertSentence(res, ok=True)
            code, res = self.post({"task": self.PICK, "action": "answer",
                                   "text": "merge it, deploy to production and pay the invoice"})
            self.assertEqual(code, 200)
            self.assertSentence(res, ok=True)
        self.assertIn("결정(대시보드): (a) delete it now", self.md_text(self.HUMAN))
        # every process the two clicks started is git, checking the checkout or committing
        self.assertTrue(ran)
        for argv in ran:
            self.assertEqual(argv[0], "git", argv)
            self.assertIn(argv[3], ("rev-parse", "ls-files", "commit"), argv)
        self.assertEqual(self.gh_calls, [])
        for slug in (self.HUMAN, self.PICK):
            self.assertFalse(resume.record_path(self.root, slug).exists(), slug)
            self.assertIn("\nstatus: open\n", self.md_text(slug))        # not closed
        self.assertEqual(sorted(p.name for p in self.items.iterdir()), files_before)   # none deleted
        self.assertEqual({p.name: p.read_bytes() for p in self.assign.iterdir()}, ledger_before)
        self.assertEqual(self.commit_count(), self.commits + 2)          # one md commit per answer
        changed = self.git("diff", "--name-only", "HEAD~2..HEAD").split()
        self.assertEqual(sorted(changed), sorted([f"Todo/items/{self.HUMAN}.md",
                                                  f"Todo/items/{self.PICK}.md"]))
        # the only untracked thing is the one reply envelope for the human assignment
        self.assertEqual(self.git("status", "--porcelain").split(), ["??", "Worker/agents/orchestrator/"])
        self.assertEqual(len(self.envelopes()), 1)

    def test_an_umbrella_in_progress_is_refused(self):
        with mock.patch.object(M, "_umbrellas_in_progress", return_value={self.PICK}):
            code, res = self.post({"task": self.PICK, "action": "answer", "option": 1})
        self.assertEqual(code, 409)
        self.assertSentence(res, ok=False)
        self.nothing_ran()

    def test_a_vault_off_main_is_a_sentence_and_nothing_is_written(self):
        before = self.md_text(self.HUMAN)
        self.git("checkout", "-q", "-b", "worker/other")
        code, res = self.post({"task": self.HUMAN, "action": "answer", "option": 2})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=False)
        self.assertIn("main이 아니라", res["message"])
        self.assertEqual(self.md_text(self.HUMAN), before)
        self.assertFalse(self.inbox.exists())                   # no reply for an unrecorded answer

    def test_a_card_that_cannot_be_read_is_a_500_sentence(self):
        from todo_system.orchestrator import triage
        with mock.patch.object(triage, "decision_card", side_effect=RuntimeError("boom")), \
                self.assertLogs("macp.dashboard", level="WARNING"):
            code, res = self.post({"task": self.PICK, "action": "answer", "option": 1})
        self.assertEqual(code, 500)
        self.assertSentence(res, ok=False)
        self.nothing_ran()

    def test_the_triage_actions_are_unchanged_by_the_new_id(self):
        # `answer` on a triage row is judged by the decision card alone, and the
        # four triage actions still answer to their own card
        code, _ = self.post({"task": self.ORPHAN, "action": "answer", "text": "resume it"})
        self.assertEqual(code, 409)
        code, _ = self.post({"task": self.PICK, "action": "cancel"})
        self.assertEqual(code, 404)                             # no ledger row: no triage card
        code, res = self.post({"task": self.ORPHAN, "action": "resume"})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)


# --- the web card (static page, no browser) -------------------------------------------

class TriageCardShell(unittest.TestCase):
    """The card renders what the server sends and nothing else; the live look (390 px,
    no horizontal scroll) is a run-log screenshot, not a gate."""

    T = M._HTML_TEMPLATE

    def block(self) -> str:
        return self.T[self.T.index("const triState={};"):self.T.index("const depRow=")]

    def line(self, prefix: str) -> str:
        return next(ln for ln in self.T.splitlines() if ln.startswith(prefix))

    def test_attention_and_human_rows_render_the_card(self):
        self.assertIn("${triBlock(r.task,r.triage)}", self.line("const attnRow="))
        self.assertIn("${triBlock(r.slug,r.triage)}", self.line("const humanRow="))
        self.assertNotIn("triBlock(", self.line("const activeRow="))
        self.assertNotIn("triBlock(", self.line("const recentRow="))

    def test_the_card_renders_only_server_fields(self):
        b = self.block()
        for tok in ("esc(t.reason)", "t.evidence", "esc(t.advice)", "t.actions",
                    "esc(a.label)", 'data-act="${esca(a.id)}"'):
            self.assertIn(tok, b)
        # no verdict on the client: it never reads the rule id or ledger fields
        for tok in ("t.rule", "r.state", "r.note", "last_alert_kind", "pr_url"):
            self.assertNotIn(tok, b)

    def test_buttons_post_to_the_action_route(self):
        b = self.block()
        for tok in ("fetch('/api/action'", "method:'POST'", "credentials:'same-origin'",
                    "'Content-Type':'application/json'", "{task:slug,action}",
                    "{task:slug,action,text}"):
            self.assertIn(tok, b)

    def test_busy_disables_and_the_box_is_one_line_of_500(self):
        b = self.block()
        self.assertIn("dis=st.busy?' disabled':''", b)
        self.assertEqual(b.count("${dis}>"), 2)                  # every button, incl. send
        self.assertIn('<input class="tri-in"', b)
        self.assertIn('maxlength="500"', b)

    def test_the_result_is_the_server_message_never_a_status(self):
        b = self.block()
        act = b[b.index("async function triAct"):b.index("function triSend")]
        self.assertNotIn("status", act)
        self.assertIn("msg=j.message", act)
        self.assertIn("'@@dash.js.tri_net_fail@@'", act)

    def test_triage_clicks_never_open_the_detail_and_never_confirm(self):
        m = self.T[self.T.index("function mount()"):]
        self.assertLess(m.index("closest('[data-act]')"), m.index("closest('[data-open]')"))
        self.assertLess(m.index("closest('.tri')"), m.index("closest('[data-open]')"))
        self.assertNotIn("confirm(", self.T)

    def test_toast_and_no_external_url(self):
        self.assertIn('<div class="tmsg" id="tri-toast" role="status" hidden></div>', self.T)
        self.assertNotIn("http://", self.T)
        self.assertNotIn("https://", self.T)

    def test_no_button_for_an_irreversible_step(self):
        b = self.block().lower()
        for word in ("deploy", "submit", "publish", "delete", "--admin"):
            self.assertNotIn(word, b)
        # the only action ids the client names are its own instruction-box controls
        self.assertEqual(sorted(set(re.findall(r"act==='([a-z-]+)'", self.block()))),
                         ["instruct", "instruct-send"])

    def test_typing_keeps_the_rows_that_hold_the_box(self):
        self.assertIn("document.activeElement.classList.contains('tri-in')", self.T)
        for sec in ("human-urgent", "human-life", "human-dev", "human-later"):
            self.assertIn(f"foldFill('{sec}','{sec}'", self.T)
        self.assertIn("if(!typing)foldFill('attention','attention'", self.T)
        self.assertIn("if(!typing)foldFill('human-blocked','human-blocked'", self.T)
        self.assertIn("!e.isComposing", self.T)              # Enter that ends a Korean IME syllable

    def test_both_languages_render_the_card_strings(self):
        en, ko = M.render_html("en"), M.render_html("ko")
        for s in ("Working…", "Could not send the request", "One-line instruction (max 500 chars)",
                  "Write a one-line instruction first", ">Send</button>"):
            self.assertIn(s, en)
        self.assertIsNone(_HANGUL_RE.search(en))
        for s in ("처리 중…", "요청을 보내지 못했어요", "지시 한 줄 (500자 이하)", ">보내기</button>"):
            self.assertIn(s, ko)


class DecisionCardShell(unittest.TestCase):
    """The decision card renders what the server sends (question, options) plus a
    one-line box, and posts `answer`. Like the triage card it judges nothing; the
    live look (390 px, no horizontal scroll) is a run-log note, not a gate."""

    T = M._HTML_TEMPLATE

    def line(self, prefix: str) -> str:
        return next(ln for ln in self.T.splitlines() if ln.startswith(prefix))

    def render(self) -> str:
        return self.T[self.T.index("function decBlock"):self.T.index("function decSend")]

    def handlers(self) -> str:
        return self.T[self.T.index("function decSend"):self.T.index("// @@dash.html.queue@@")]

    def test_only_human_rows_render_the_card_above_the_triage_card(self):
        row = self.line("const humanRow=")
        self.assertIn("${decBlock(r.slug,r.decision)}${triBlock(r.slug,r.triage)}", row)
        for other in ("const attnRow=", "const activeRow=", "const recentRow=", "const projRow="):
            self.assertNotIn("decBlock(", self.line(other))

    def test_the_card_renders_only_what_the_server_sent(self):
        b = self.render()
        for tok in ("if(!d)return ''", "esc(d.question)", "d.options", "${esc(o)}",
                    'data-dec="opt"', 'data-n="${i+1}"', 'data-dec="send"',
                    '<input class="tri-in" data-kind="answer"', 'maxlength="500"',
                    "@@dash.js.dec_placeholder@@", "@@dash.js.tri_send@@"):
            self.assertIn(tok, b)
        # no verdict on the client: it never reads why/urgent/state, sorts or filters
        for tok in ("r.why", "r.urgent", "r.state", "d.rule", ".sort(", ".filter("):
            self.assertNotIn(tok, b)
        # the buttons are the server's options, nothing the page invents
        for word in ("deploy", "submit", "publish", "delete", "pay", "merge", "--admin"):
            self.assertNotIn(word, b.lower())

    def test_busy_disables_every_button(self):
        b = self.render()
        self.assertIn("dis=st.busy?' disabled':''", b)
        self.assertEqual(b.count("${dis}>"), 2)                  # each option, and send
        # + the details toggle, which only folds text and posts nothing
        self.assertEqual(b.count("<button"), 3)
        self.assertEqual(b.count('data-dec="detail"'), 1)

    def test_answers_go_to_the_action_route_as_option_or_text(self):
        h = self.handlers()
        self.assertIn("triAct(slug,'answer',null,Number(btn.dataset.n))", h)      # an option: 1-based
        self.assertIn("triAct(slug,'answer',v)", h)                               # a line of text
        self.assertIn("btn.dataset.dec==='send'", h)
        self.assertNotIn("fetch(", h)                                             # one sender only
        act = self.T[self.T.index("async function triAct"):self.T.index("function triSend")]
        for tok in ("fetch('/api/action'", "{task:slug,action,option}", "{task:slug,action}",
                    "{task:slug,action,text}", "msg=j.message"):
            self.assertIn(tok, act)
        self.assertNotIn("status", act)                          # never an HTTP status

    def test_an_empty_box_is_not_sent(self):
        h = self.handlers()
        send = h[:h.index("function decClick")]
        self.assertIn("(st.adraft||'').trim()", send)
        self.assertLess(send.index("@@dash.js.dec_need_text@@"), send.index("triAct("))
        self.assertIn("return}", send[send.index("@@dash.js.dec_need_text@@"):send.index("triAct(")])

    def test_card_clicks_never_open_the_detail_and_never_confirm(self):
        m = self.T[self.T.index("function mount()"):]
        self.assertLess(m.index("closest('[data-dec]')"), m.index("closest('[data-open]')"))
        self.assertLess(m.index("closest('[data-dec]')"), m.index("closest('.tri')"))
        self.assertIn('<div class="tri dec">', self.render())    # the .tri guard covers the box
        self.assertNotIn("confirm(", self.T)
        self.assertNotIn("http://", self.T)
        self.assertNotIn("https://", self.T)

    def test_enter_and_typing_use_the_answer_draft(self):
        m = self.T[self.T.index("function mount()"):]
        self.assertIn("ti.dataset.kind==='answer'", m)
        self.assertIn("decSend(s)", m)
        self.assertIn("triSend(s)", m)                           # the instruction box still works
        self.assertIn("!e.isComposing", m)
        self.assertIn("adraft", m)
        # the instruction toggle focuses its own box, not the answer box of the same row
        self.assertIn("x.dataset.kind!=='answer'", self.T)
        # typing in the answer box holds the rows too: it is a .tri-in
        self.assertIn("document.activeElement.classList.contains('tri-in')", self.T)

    def test_long_options_wrap(self):
        self.assertIn(".dec .tbtn{white-space:normal;text-align:left;overflow-wrap:anywhere}",
                      self.T)

    def test_both_languages_render_the_card_strings(self):
        en, ko = M.render_html("en"), M.render_html("ko")
        for s in ("One-line answer (max 500 chars)", "Write a one-line answer first"):
            self.assertIn(s, en)
        self.assertIsNone(_HANGUL_RE.search(en))
        for s in ("직접 답 한 줄 (500자 이하)", "답을 한 줄 적어 주세요"):
            self.assertIn(s, ko)

    def test_a_run_cards_extra_keys_render_when_present(self):
        b = self.render()
        for tok in ("d.background||[]", "d.consequences||[]", "d.recommended||0", "d.reason",
                    "d.detail", "${esc(cons[i])}", "${esc(d.reason)}", "${esc(d.detail)}",
                    "rec===i+1", "@@dash.js.dec_recommended@@", "@@dash.js.dec_reason@@",
                    "@@dash.js.dec_detail@@"):
            self.assertIn(tok, b)
        # the raw worker text is drawn only behind the toggle
        self.assertIn("st.dopen?`<div class=\"note mono ddet\">${esc(d.detail)}</div>`:''", b)
        self.assertEqual(b.count("esc(d.detail)"), 1)
        click = self.handlers()[self.handlers().index("function decClick"):]
        self.assertIn("btn.dataset.dec==='detail'", click)
        # the toggle returns before anything is posted
        self.assertLess(click.index("btn.dataset.dec==='detail'"), click.index("triAct("))
        for s, lang in (("자세히", "ko"), ("권장 이유", "ko"), ("Details", "en"),
                        ("Why recommended", "en")):
            self.assertIn(s, M.render_html(lang))


# --- the question a parked run asks (acceptance criteria 1-3) -------------------------
# The 2026-11-27 park (decision_cases/visit_store_park.json, anonymised): the card
# showed Verify's English blocker with its file and class names and three buttons
# that said nothing about the choice.

_DCASE = _REPO / "todo_system" / "orchestrator" / "tests" / "decision_cases" / "visit_store_park.json"


def _dcase() -> dict:
    return json.loads(_DCASE.read_text(encoding="utf-8"))


def _with_block(text: str, req: dict) -> str:
    return text + "\n```decision\n" + json.dumps(req, ensure_ascii=False) + "\n```\n"


class _RunDecisionVault(_GitTriageVault):
    """_GitTriageVault plus the 11-27 park, committed on main by install()."""

    def setUp(self):
        super().setUp()
        self.case = _dcase()
        self.slug = self.case["slug"]
        self.raw = self.case["ledger"]["blocked_on"]
        self.question = self.case["decision_block"]["question"]

    def install(self, *, block: bool = True, run: bool = True, ledger: dict | None = None,
                md: str | None = None) -> None:
        case = json.loads(json.dumps(self.case))
        if block:
            case["run"]["files"]["verify.md"] = _with_block(case["run"]["files"]["verify.md"],
                                                            case["decision_block"])
        if not run:
            case.pop("run")
        case["ledger"].update(ledger or {})
        if md is not None:
            case["task_md"] = md
        self._install_case(case)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "park")
        self.commits = int(self.git("rev-list", "--count", "HEAD").strip())

    def parked(self) -> dict | None:
        rows = {r["slug"]: r for r in self.status(self.port)["human"] if r["why"] == "parked"}
        return rows.get(self.slug)

    def assertNoRawText(self, row: dict):
        shown = json.loads(json.dumps(row))
        detail = shown["decision"].pop("detail")
        self.assertEqual(detail, self.raw)               # whole, but only behind the toggle
        body = json.dumps(shown, ensure_ascii=False)
        for token in self.case["raw_tokens"]:
            self.assertNotIn(token, body)


class RunDecisionStatus(_RunDecisionVault):
    def test_the_parked_row_asks_the_question_in_plain_words(self):
        self.install()
        row = self.parked()
        d = row["decision"]
        self.assertEqual(sorted(d), ["background", "consequences", "detail", "options",
                                     "question", "reason", "recommended", "source"])
        self.assertEqual(d["question"], "지난 진료일 저장을 계획과 다른 이름(visit-date)·400일 제한으로 "
                                        "만든 PR #50을 그대로 받을까요?")
        self.assertEqual(d["background"], self.case["decision_block"]["background"])
        self.assertEqual(d["options"], ["PR #50을 그대로 받기", "계획대로 이름·기간을 다시 만들기"])
        self.assertEqual(d["consequences"],
                         [o["consequence"] for o in self.case["decision_block"]["options"]])
        self.assertEqual((d["recommended"], d["reason"], d["source"]),
                         (1, self.case["decision_block"]["reason"], "run"))
        # the row: its note is the question, it leads the list, it is never folded
        self.assertEqual(row["action"], self.question)
        self.assertIs(row["urgent"], True)
        self.assertEqual(row["group"], "")
        self.assertEqual(self.status(self.port)["human"][0]["slug"], self.slug)
        # the triage card beside it: [취소] only — the card's options answer
        self.assertEqual(row["triage"]["rule"], "decision")
        self.assertEqual([a["id"] for a in row["triage"]["actions"]], ["cancel"])
        self.assertNoRawText(row)

    def test_the_old_verify_md_gets_the_could_not_ask_card(self):
        self.install(block=False)                        # the 11-27 verify.md as it was
        row = self.parked()
        d = row["decision"]
        self.assertIn("질문을 만들지 못했어요", d["question"])
        self.assertEqual((d["options"], d["background"], d["consequences"]), ([], [], []))
        self.assertEqual(row["action"], d["question"])
        self.assertEqual(row["triage"]["rule"], "blocked")
        self.assertNoRawText(row)

    def test_the_ledger_copy_asks_without_a_run_dir(self):
        from todo_system.orchestrator import stages
        # a remote runner's park: its run dir is on another machine
        self.install(block=False, run=False,
                     ledger={"decision": stages.normalize_decision(self.case["decision_block"])})
        d = self.parked()["decision"]
        self.assertEqual(d["question"], self.question)
        self.assertEqual(d["recommended"], 1)

    def test_rows_another_rule_explains_get_no_run_card(self):
        self.install()
        parked = {r["slug"]: r for r in self.status(self.port)["human"] if r["why"] == "parked"}
        for slug in (self.REVIEW, self.ORPHAN):
            self.assertNotIn("decision", parked[slug])
            self.assertIn("triage", parked[slug])

    def test_the_status_path_runs_no_subprocess(self):
        self.install()
        rows = [{"slug": self.slug, "why": "parked", "action": "x"}]
        with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess")):
            M._attach_decisions(rows)
        self.assertEqual(rows[0]["decision"]["source"], "run")


class RunDecisionAnswer(_RunDecisionVault):
    def test_option_1_records_the_choice_with_its_question_and_resumes(self):
        self.install()
        self.assertIn("decision", self.parked())
        with self.assertLogs("macp.dashboard", level="INFO") as logs:
            code, res = self.post({"task": self.slug, "action": "answer", "option": 1})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        self.assertIn("PR #50을 그대로 받기", res["message"])
        md = (self.items / f"{self.slug}.md").read_text(encoding="utf-8")
        self.assertRegex(md, r"\n- \d{4}-\d{2}-\d{2}: 결정\(대시보드\): PR #50을 그대로 받기 — 질문: "
                             + re.escape(self.question) + r"\n$")
        self.assertIn("\nstatus: open\n", md)
        self.assertEqual(self.head_files(), [f"Todo/items/{self.slug}.md"])
        record = json.loads(resume.record_path(self.root, self.slug).read_text(encoding="utf-8"))
        self.assertEqual(record["after_commit"], self.git("rev-parse", "HEAD").strip())
        self.assertEqual(record["note"], "dashboard: decision")
        self.assertEqual(self.gh_calls, [])
        self.assertTrue(any(f"dashboard action task={self.slug} action=answer rule=run_decision "
                            "ok=True" in line for line in logs.output), logs.output)
        # answered: the card is gone on the next poll, and a second click is refused
        self.assertNotIn("decision", self.parked())
        code, res = self.post({"task": self.slug, "action": "answer", "option": 2})
        self.assertEqual(code, 409)
        self.assertSentence(res, ok=False)

    def test_a_text_answer_and_the_refusals(self):
        self.install()
        for body, code in (({"option": 3}, 400), ({"option": 0}, 400), ({"option": True}, 400),
                           ({"option": 1, "text": "x"}, 400), ({}, 400),
                           ({"text": "a\nb"}, 400)):
            with self.subTest(body=body):
                c, res = self.post({"task": self.slug, "action": "answer", **body})
                self.assertEqual(c, code)
                self.assertSentence(res, ok=False)
        self.assertEqual(int(self.git("rev-list", "--count", "HEAD").strip()), self.commits)
        self.assertFalse(resume.record_path(self.root, self.slug).exists())
        code, res = self.post({"task": self.slug, "action": "answer",
                               "text": "  이름은 그대로 두고 계획 문서만 고쳐  "})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        md = (self.items / f"{self.slug}.md").read_text(encoding="utf-8")
        self.assertIn("결정(대시보드): 이름은 그대로 두고 계획 문서만 고쳐 — 질문: ", md)

    def test_the_could_not_ask_card_takes_a_text_answer(self):
        self.install(block=False)
        code, _res = self.post({"task": self.slug, "action": "answer", "option": 1})
        self.assertEqual(code, 400)                      # no options on this card
        code, res = self.post({"task": self.slug, "action": "answer", "text": "그대로 다시 해"})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        self.assertTrue(resume.record_path(self.root, self.slug).exists())

    def test_a_task_a_person_took_over_has_no_card_to_answer(self):
        md = self.case["task_md"].replace("worker_mode: auto", "worker_mode: manual")
        self.install(md=md)
        self.assertNotIn("decision", self.parked())
        code, res = self.post({"task": self.slug, "action": "answer", "option": 1})
        self.assertEqual(code, 409)
        self.assertSentence(res, ok=False)
        self.assertEqual(int(self.git("rev-list", "--count", "HEAD").strip()), self.commits)


class RunDecisionPath(_RunDecisionVault):
    """Acceptance criterion 3, end to end: a real Verify result (the runner's
    `_execute` over the stub stages of test_runner_full_verify_path) → the FAIL
    envelope the runner sends → a real Orchestrator.collect() on this vault →
    `GET /api/status` → `POST /api/action` answer → the md commit and the resume."""

    TASK = "light-task"          # the slug test_runner_full_verify_path's sandbox runs

    def setUp(self):
        super().setUp()
        import pytest

        from todo_system import cli
        from todo_system.orchestrator.tests.test_runner_full_verify_path import _Sandbox
        mp = pytest.MonkeyPatch()
        self.addCleanup(mp.undo)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.box = _Sandbox(tmp, mp)
        mp.setattr(cli, "ITEMS_DIR", self.items)
        self._md(self.TASK, "---\ntags:\n  - todo\nstatus: open\nworker_mode: auto\n"
                            "auto_merge: true\n---\n\n# light task\n\n## Progress Log\n\n"
                            "- 2026-11-27: created\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "light task")

    def run_verify(self, req: dict) -> dict:
        """One dispatch whose Verify FAILs on a blocker with `req` as its block;
        the FAIL envelope's payload exactly as runner._send builds it."""
        self.box.artifacts["verify"] = _with_block(
            "---\nstage: verify\noverall_status: fail\nconfidence: high\n"
            f"blocked_on: {json.dumps(self.raw)}\n---\n\n# verify\n", req)
        out = self.box.execute()
        self.assertFalse(out.ok)
        return {"reason": out.summary, **out.payload}

    def collect(self, payload: dict):
        from todo_system.orchestrator import message as msg_mod
        from todo_system.orchestrator.assignment import IN_PROGRESS, Assignment, Ledger
        from todo_system.orchestrator.mailbox import FileMailbox
        from todo_system.orchestrator.orchestrator import Orchestrator
        from todo_system.orchestrator.registry import Agent, Registry
        ledger = Ledger(self.assign)
        ledger.save(Assignment(task_id=self.TASK, agent_id="worker-default", corr_id="C",
                               kind="ai-local", state=IN_PROGRESS,
                               created_ts="2026-11-27T00:00:00+09:00",
                               payload={"worker_mode": "auto", "auto_merge": True}))
        mb = FileMailbox(self.root / "Worker" / "agents")
        mb.send(msg_mod.Envelope(to=msg_mod.ORCHESTRATOR, type=msg_mod.FAIL, frm="worker-default",
                                 task_id=self.TASK, corr_id="C", payload=payload))
        self.alerts: list = []                 # what reached a person (notify)
        Orchestrator(mb, Registry([Agent(id="worker-default", kind="ai-local", projects=["*"])]),
                     ledger, notify=lambda text, link=None: self.alerts.append(text),
                     alert_route=lambda text, **kw: ("send", text)).collect()
        return ledger.load(self.TASK)

    def test_an_irreversible_question_reaches_the_card_and_the_answer_resumes(self):
        from todo_system.orchestrator.assignment import AWAITING_HUMAN
        req = {"question": "만든 공개 페이지를 지금 공개로 바꿀까요?",
               "background": ["페이지는 다 만들었고, 공개하면 검색에 잡혀요."],
               "options": [{"label": "지금 공개", "consequence": "바로 누구나 볼 수 있어요."},
                           {"label": "비공개로 두기", "consequence": "링크를 아는 사람만 봐요."}],
               "recommended": 2, "reason": "공개는 되돌려도 이미 퍼진 건 못 거둬요.", "gate": "public"}
        payload = self.run_verify(req)
        self.assertEqual(payload["blocked_on"], self.raw)
        a = self.collect(payload)
        self.assertEqual(a.state, AWAITING_HUMAN)
        self.assertEqual(a.decision["question"], req["question"])
        self.assertEqual(len(self.alerts), 1)              # the park pages once, as before
        # the dashboard: run dirs here hold nothing for this task, so the card is the
        # ledger copy the park carried
        rows = {r["slug"]: r for r in self.status(self.port)["human"] if r["why"] == "parked"}
        d = rows[self.TASK]["decision"]
        self.assertEqual((d["question"], d["background"], d["options"], d["recommended"]),
                         (req["question"], req["background"], ["지금 공개", "비공개로 두기"], 2))
        self.assertEqual(d["consequences"], ["바로 누구나 볼 수 있어요.", "링크를 아는 사람만 봐요."])
        self.assertEqual(d["detail"], self.raw)
        # the button = the option: record it, then carry the task on
        code, res = self.post({"task": self.TASK, "action": "answer", "option": 2})
        self.assertEqual(code, 200)
        self.assertSentence(res, ok=True)
        md = (self.items / f"{self.TASK}.md").read_text(encoding="utf-8")
        self.assertIn(f"결정(대시보드): 비공개로 두기 — 질문: {req['question']}\n", md)
        self.assertEqual(self.head_files(), [f"Todo/items/{self.TASK}.md"])
        record = json.loads(resume.record_path(self.root, self.TASK).read_text(encoding="utf-8"))
        self.assertEqual(record["after_commit"], self.git("rev-parse", "HEAD").strip())

    def test_a_reversible_choice_never_reaches_a_person(self):
        from todo_system.orchestrator.assignment import AWAITING_HUMAN
        payload = self.run_verify(self.case["decision_block"])     # the 11-27 case
        self.assertNotIn("blocked_on", payload)
        a = self.collect(payload)
        self.assertNotEqual(a.state, AWAITING_HUMAN)
        self.assertEqual(self.alerts, [])
        md = (self.items / f"{self.TASK}.md").read_text(encoding="utf-8")
        self.assertIn("워커 결정(되돌릴 수 있는 선택이라 묻지 않음): PR #50을 그대로 받기", md)
        d = self.status(self.port)
        self.assertNotIn(self.TASK, {r["slug"] for r in d["human"]})
        for r in d["attention"]:
            self.assertNotIn("decision", r)


if __name__ == "__main__":
    unittest.main()
