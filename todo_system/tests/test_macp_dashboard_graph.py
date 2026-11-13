"""Tests for the dashboard's graph-memory view: ``/graph`` and ``/api/graph``.

The collector reads graph_memory's bi-temporal ``edges`` table read-only; these
pin the current-vs-``as_of`` semantics, the fail-soft shape, the node cap, and
that the page keeps the dashboard's offline / token-gate contract.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from todo_system import graph_memory, i18n

_SRC = Path(__file__).resolve().parents[2] / "scripts" / "macp_dashboard.py"


def _load():
    spec = importlib.util.spec_from_file_location("macp_dashboard_graph_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


def _edge(con, frm, to, etype, source, recorded, valid_until=None):
    con.execute(
        "INSERT INTO edges (from_slug, to_slug, edge_type, source_file, recorded_at, valid_until) "
        "VALUES (?,?,?,?,?,?)", (frm, to, etype, source, recorded, valid_until))


class GraphCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "relations.sqlite"
        con = graph_memory.init_relations_db(self.db)   # the real schema, not a copy
        _edge(con, "write-paper", "run-experiment", "depends_on", "Todo/items/write-paper.md", "2026-11-01T09:00:00")
        _edge(con, "write-paper", "use-tdgl", "related_decisions", "Todo/items/write-paper.md", "2026-11-01T09:00:00")
        _edge(con, "use-tdgl", "hzo", "wikilink", "Decisions/use-tdgl.md", "2026-11-02T09:00:00")
        # tombstoned on 11-05: live on 11-03, gone now
        _edge(con, "write-paper", "old-idea", "related_to", "Todo/items/write-paper.md",
              "2026-11-01T09:00:00", valid_until="2026-11-05T00:00:00")
        con.commit()
        con.close()
        for name, value in (("RELATIONS_DB", self.db),
                            ("_title_map", lambda: {"write-paper": "Write the paper"}),
                            ("_todo_status_map", lambda: {"run-experiment": "done"})):
            p = mock.patch.object(M, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _ids(self, g):
        return {n["id"] for n in g["nodes"]}

    def test_current_view_excludes_tombstoned_edges(self):
        g = M.collect_graph()
        self.assertTrue(g["available"])
        self.assertEqual(self._ids(g), {"write-paper", "run-experiment", "use-tdgl", "hzo"})
        self.assertEqual(len(g["edges"]), 3)
        self.assertEqual(g["first_recorded"], "2026-11-01T09:00:00")

    def test_as_of_rewinds_to_edges_live_then(self):
        g = M.collect_graph(M._graph_as_of("2026-11-01"))
        self.assertIn("old-idea", self._ids(g))           # not tombstoned yet
        self.assertNotIn("hzo", self._ids(g))             # recorded the next day
        g = M.collect_graph(M._graph_as_of("2026-10-31"))
        self.assertEqual(g["nodes"], [])

    def test_node_fields_kind_title_status_degree(self):
        nodes = {n["id"]: n for n in M.collect_graph()["nodes"]}
        self.assertEqual(nodes["write-paper"]["label"], "Write the paper")
        self.assertEqual(nodes["write-paper"]["kind"], "task")
        self.assertEqual(nodes["write-paper"]["degree"], 2)
        self.assertEqual(nodes["use-tdgl"]["kind"], "decision")
        self.assertEqual(nodes["run-experiment"]["status"], "done")
        self.assertEqual(nodes["hzo"]["kind"], "ref")     # only ever a target
        self.assertEqual(nodes["hzo"]["label"], "hzo")    # no title -> slug

    def test_target_only_slug_known_to_todo_db_is_a_task(self):
        with mock.patch.object(M, "_title_map", lambda: {"run-experiment": "Run it"}):
            nodes = {n["id"]: n for n in M.collect_graph()["nodes"]}
        self.assertEqual(nodes["run-experiment"]["kind"], "task")

    def test_windows_separators_in_source_file_still_classify(self):
        self.assertEqual(M._graph_kind("Todo\\items\\x.md"), "task")

    def test_cap_keeps_most_connected_and_reports_the_rest(self):
        g = M.collect_graph(max_nodes=2)
        self.assertEqual(self._ids(g), {"write-paper", "use-tdgl"})
        self.assertEqual(g["truncated"], 2)
        for e in g["edges"]:
            self.assertIn(e["from"], self._ids(g))
            self.assertIn(e["to"], self._ids(g))

    def test_missing_index_is_unavailable_not_an_error(self):
        with mock.patch.object(M, "RELATIONS_DB", Path(self.tmp.name) / "nope.sqlite"):
            g = M.collect_graph()
        self.assertFalse(g["available"])
        self.assertEqual((g["nodes"], g["edges"]), ([], []))

    def test_unreadable_index_is_unavailable(self):
        bad = Path(self.tmp.name) / "bad.sqlite"
        bad.write_bytes(b"not a database")
        with mock.patch.object(M, "RELATIONS_DB", bad):
            self.assertFalse(M.collect_graph()["available"])

    def test_collector_opens_the_index_read_only(self):
        with mock.patch.object(M.sqlite3, "connect", wraps=sqlite3.connect) as spy:
            M.collect_graph()
        self.assertTrue(any("mode=ro" in str(c.args[0]) for c in spy.call_args_list))


class AsOfParsingTests(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(M._graph_as_of("2026-11-03"), "2026-11-03T23:59:59")
        self.assertEqual(M._graph_as_of("2026-11-03T08:30"), "2026-11-03T08:30:00")
        self.assertEqual(M._graph_as_of("2026-11-03T08:30:15"), "2026-11-03T08:30:15")

    def test_garbage_means_now(self):
        for raw in (None, "", "yesterday", "2026-11-03'; DROP TABLE edges;--", "2026/11/03"):
            self.assertIsNone(M._graph_as_of(raw), raw)


class GraphPageTests(unittest.TestCase):
    PAGES = {lang: M.render_graph_html(lang) for lang in ("ko", "en")}

    def test_every_marker_is_filled(self):
        for lang, page in self.PAGES.items():
            self.assertIsNone(re.search(r"@@[a-z0-9_.]+@@", page), lang)

    def test_no_external_urls(self):
        for page in self.PAGES.values():
            self.assertNotIn("https://", page)
            self.assertNotIn("http://", page)
            self.assertNotIn("<script src", page)

    def test_doctype_and_html_lead_the_document(self):
        for page in self.PAGES.values():
            self.assertIn(b"<html", page.encode("utf-8")[:100].lower())

    def test_main_page_links_to_graph(self):
        self.assertIn('href="graph"', M.HTML)

    def test_graph_keys_exist_in_both_languages(self):
        keys = {k for k in i18n.DASHBOARD["ko"] if k.startswith("dash.graph.")}
        self.assertTrue(keys)
        self.assertEqual(keys, {k for k in i18n.DASHBOARD["en"] if k.startswith("dash.graph.")})


class GraphRoutesOverSocketTests(unittest.TestCase):
    TOKEN = "s3cret-token"

    def setUp(self):
        p = mock.patch.object(M, "collect_graph", return_value={"available": True, "nodes": [], "edges": []})
        self.collect = p.start()
        self.addCleanup(p.stop)
        self.srv = M.serve("127.0.0.1", 0, self.TOKEN)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def _get(self, path, token=True):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.request("GET", path, headers={"Authorization": f"Bearer {self.TOKEN}"} if token else {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader("Content-Type"), body

    def test_graph_routes_sit_behind_the_token_gate(self):
        for path in ("/graph", "/api/graph"):
            self.assertEqual(self._get(path, token=False)[0], 401, path)

    def test_api_graph_passes_normalised_as_of(self):
        status, ctype, body = self._get("/api/graph?as_of=2026-11-03")
        self.assertEqual((status, ctype), (200, "application/json"))
        self.assertEqual(json.loads(body)["available"], True)
        self.collect.assert_called_with("2026-11-03T23:59:59")
        self._get("/api/graph?as_of=bogus")
        self.collect.assert_called_with(None)

    def test_graph_page_is_html(self):
        status, ctype, body = self._get("/graph")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"))
        self.assertIn(b'id="svg"', body)


if __name__ == "__main__":
    unittest.main()
