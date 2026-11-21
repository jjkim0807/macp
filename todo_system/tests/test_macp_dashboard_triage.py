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


if __name__ == "__main__":
    unittest.main()
