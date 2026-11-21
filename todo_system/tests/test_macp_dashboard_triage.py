"""Dashboard wiring of the attention triage (orchestrator/triage.py).

Phase 3: `/api/status` attaches `triage` to attention rows and human parked
rows — through the real `serve()` server and the real `collect_status()` over a
tmp vault (todo.db with the real schema, ledger rows and run dirs taken from the
real-case fixtures). Only the collectors that would reach outside the vault
(queue, usage, accounts, process list) are stubbed.

The dashboard module is loaded under its own name so patches here never leak
into test_macp_dashboard's instance.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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

    def status(self) -> dict:
        code, body = self.request(self.serve(), "GET", "/api/status",
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


if __name__ == "__main__":
    unittest.main()
