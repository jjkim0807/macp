"""devtunnel_watchdog: parse, verdict table, incident sequences, and main's side effects."""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import devtunnel_watchdog as W  # noqa: E402

# Shape of real `devtunnel show macp-dash` output (2026-09-25, values changed).
SHOW = """Tunnel ID             : demo-tunnel.use
Description           :
Access control        : {+Anonymous [connect]}
Host connections      : HOSTS
Client connections    : 0
Ports                 : 1
  8787  auto  https://example-8787.use.devtunnels.ms/  0 client connections
Tunnel Expiration     : 30 days
"""

UP = 3600.0


class Parse(unittest.TestCase):
    def test_host_connections(self):
        self.assertEqual(W.parse_host_connections(SHOW.replace("HOSTS", "0")), 0)
        self.assertEqual(W.parse_host_connections(SHOW.replace("HOSTS", "2")), 2)
        # "Client connections" and the per-port "client connections" must not match.
        self.assertIsNone(W.parse_host_connections("Client connections : 3\n"))
        self.assertIsNone(W.parse_host_connections(""))


class Verdict(unittest.TestCase):
    def v(self, hosts, state=None, active=True, uptime=UP, now=10_000.0):
        return W.verdict(hosts, active, uptime, state or {}, now)

    def test_table(self):
        self.assertEqual(self.v(1), "ok")
        self.assertEqual(self.v(0, active=False), "inactive")
        self.assertEqual(self.v(None, active=False), "inactive")
        self.assertEqual(self.v(0, uptime=30), "starting")
        self.assertEqual(self.v(0), "suspect")
        self.assertEqual(self.v(0, {"zero_since": 9_700.0}), "restart")
        self.assertEqual(self.v(0, {"zero_since": 9_700.0, "restarted_at": 9_700.0}), "stuck")
        self.assertEqual(self.v(0, {"zero_since": 1.0, "restarted_at": 1.0}), "restart")
        self.assertEqual(self.v(None), "unknown")
        self.assertEqual(self.v(None, {"query_fail_since": 9_700.0}), "query_failed")


def run_ticks(hosts_seq, start=0.0, step=300.0):
    """Drive verdict/next_state through a tick sequence; return the verdicts."""
    state, out, now = {}, [], start
    for hosts in hosts_seq:
        v = W.verdict(hosts, True, UP, state, now)
        state = W.next_state(v, state, now)
        out.append(v)
        now += step
    return out, state


class Sequences(unittest.TestCase):
    def test_2026_09_24_incident_restarts_on_second_zero_then_recovers(self):
        out, state = run_ticks([1, 0, 0, 1])
        self.assertEqual(out, ["ok", "suspect", "restart", "ok"])
        self.assertEqual(state, {})

    def test_one_blip_is_not_restarted(self):
        out, _ = run_ticks([1, 0, 1, 0, 1])
        self.assertNotIn("restart", out)

    def test_restart_that_does_not_help_is_stuck_then_retried(self):
        out, _ = run_ticks([0, 0, 0, 0, 0, 0])
        # restart; the next 0 falls within GRACE_S of it → stuck until grace passes.
        self.assertEqual(out[:3], ["suspect", "restart", "stuck"])
        self.assertIn("restart", out[3:])

    def test_stuck_alert_flag_survives_retries_until_healthy(self):
        state = W.next_state("restart", {"stuck_alerted": True, "zero_since": 1.0}, 50.0)
        self.assertTrue(state["stuck_alerted"])
        self.assertEqual(W.next_state("ok", state, 60.0), {})

    def test_query_failure_needs_two_ticks(self):
        out, _ = run_ticks([None, None, None, 1])
        self.assertEqual(out, ["unknown", "query_failed", "query_failed", "ok"])


class MainSideEffects(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, val in (("STATE_PATH", Path(tmp.name) / "s.json"),):
            p = mock.patch.object(W, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.calls = []
        self.notes = []

        def fake_run(args, timeout=30):
            self.calls.append(args)
            return mock.Mock(returncode=0, stdout="")
        for name, val in (("_run", fake_run), ("unit_active", lambda: True),
                          ("unit_uptime_s", lambda: UP),
                          ("notify", lambda *a, **k: self.notes.append(a))):
            p = mock.patch.object(W, name, val)
            p.start()
            self.addCleanup(p.stop)

    def tick(self, hosts, *argv):
        with mock.patch.object(W, "query_hosts", return_value=hosts), \
             mock.patch.object(sys, "argv", ["w", *argv]), \
             mock.patch("builtins.print"):
            W.main()

    def restarts(self):
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", "restart"]]

    def test_second_zero_restarts_the_unit(self):
        self.tick(0)
        self.assertEqual(self.restarts(), [])
        self.tick(0)
        self.assertEqual(self.restarts(), [["systemctl", "--user", "restart", W.UNIT]])
        self.assertEqual(self.notes, [])

    def test_dry_run_mutates_nothing(self):
        self.tick(0)
        before = W.STATE_PATH.read_text()
        self.tick(0, "--dry-run")
        self.assertEqual(self.restarts(), [])
        self.assertEqual(W.STATE_PATH.read_text(), before)

    def test_query_failure_alerts_once(self):
        for _ in range(4):
            self.tick(None)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.restarts(), [])


# ---- public-URL probe ---------------------------------------------------------

PROBE_T = 0.3   # probe timeout used against the local fake server


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        srv = self.server
        srv.requests.append((self.path, dict(self.headers)))
        if srv.block:
            srv.release.wait(5)
            try:
                self._send(200)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._send(srv.status)

    def _send(self, code):
        body = b'{"ok": false}'
        self.send_response(code)
        if 300 <= code < 400:
            self.send_header("Location", "/elsewhere")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def fake_status_server(test: unittest.TestCase, status: int = 401, block: bool = False):
    """A real HTTP server on 127.0.0.1:<ephemeral> answering `status` (or hanging)."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    srv.status, srv.block, srv.requests = status, block, []
    srv.release = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    test.addCleanup(srv.release.set)        # runs first (LIFO): unblock hung handlers
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}/"
    return srv


def closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/"


class ParsePublicUrl(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(W.parse_public_url(SHOW), "https://example-8787.use.devtunnels.ms/")
        self.assertIsNone(W.parse_public_url(""))
        self.assertIsNone(W.parse_public_url("Host connections : 1\nPorts : 0\n"))


class PublicUrlSource(unittest.TestCase):
    def test_env_wins_without_running_devtunnel(self):
        run = mock.Mock()
        with mock.patch.dict(os.environ, {W.PUBLIC_URL_ENV: " http://127.0.0.1:1/ "}), \
             mock.patch.object(W, "_run", run):
            self.assertEqual(W.public_url(), "http://127.0.0.1:1/")
        run.assert_not_called()

    def test_falls_back_to_devtunnel_show(self):
        with mock.patch.dict(os.environ), \
             mock.patch.object(W, "_run",
                               return_value=mock.Mock(returncode=0, stdout=SHOW)):
            os.environ.pop(W.PUBLIC_URL_ENV, None)
            self.assertEqual(W.public_url(), "https://example-8787.use.devtunnels.ms/")

    def test_none_when_show_fails(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(W.PUBLIC_URL_ENV, None)
            with mock.patch.object(W, "_run", return_value=None):
                self.assertIsNone(W.public_url())
            with mock.patch.object(W, "_run",
                                   return_value=mock.Mock(returncode=1, stdout=SHOW)):
                self.assertIsNone(W.public_url())


class Probe(unittest.TestCase):
    def test_401_is_healthy_and_sends_no_token(self):
        srv = fake_status_server(self, 401)
        label = W.probe_public(srv.url, PROBE_T)
        self.assertEqual(label, "401")
        self.assertTrue(W.probe_healthy(label))
        path, headers = srv.requests[0]
        self.assertEqual(path, "/api/status")
        self.assertNotIn("authorization", {k.lower() for k in headers})

    def test_200_is_healthy(self):
        srv = fake_status_server(self, 200)
        self.assertEqual(W.probe_public(srv.url, PROBE_T), "200")
        self.assertTrue(W.probe_healthy("200"))

    def test_5xx_is_unhealthy(self):
        srv = fake_status_server(self, 503)
        label = W.probe_public(srv.url, PROBE_T)
        self.assertEqual(label, "http_503")
        self.assertFalse(W.probe_healthy(label))

    def test_redirect_is_not_followed_and_unhealthy(self):
        srv = fake_status_server(self, 302)
        label = W.probe_public(srv.url, PROBE_T)
        self.assertEqual(label, "http_302")
        self.assertFalse(W.probe_healthy(label))
        self.assertEqual(len(srv.requests), 1)

    def test_timeout_is_unhealthy_and_capped(self):
        srv = fake_status_server(self, block=True)
        t0 = time.monotonic()
        label = W.probe_public(srv.url, PROBE_T)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(label, "timeout")
        self.assertFalse(W.probe_healthy(label))

    def test_connection_refused_is_unhealthy(self):
        label = W.probe_public(closed_port_url(), PROBE_T)
        self.assertEqual(label, "conn_error")
        self.assertFalse(W.probe_healthy(label))

    def test_default_cap_is_ten_seconds(self):
        self.assertEqual(W.PROBE_TIMEOUT_S, 10)


class ProbeVerdict(unittest.TestCase):
    def v(self, hosts, probe_ok, state=None, uptime=UP, now=10_000.0):
        return W.verdict(hosts, True, uptime, state or {}, now, probe_ok)

    def test_probe_table(self):
        self.assertEqual(self.v(1, None), "ok")
        self.assertEqual(self.v(1, True), "ok")
        self.assertEqual(self.v(1, False), "probe_suspect")
        self.assertEqual(self.v(1, False, {"probe_fail_since": 9_700.0}), "restart")
        self.assertEqual(self.v(1, False, {"probe_fail_since": 9_700.0,
                                           "restarted_at": 9_700.0}), "stuck")
        self.assertEqual(self.v(1, False, uptime=30), "starting")
        # host 0 still wins over a healthy probe
        self.assertEqual(self.v(0, True), "suspect")

    def test_probe_sequence_2026_11_17(self):
        state, out, now = {}, [], 0.0
        for probe_ok in (True, False, False, True):
            v = W.verdict(1, True, UP, state, now, probe_ok)
            state = W.next_state(v, state, now)
            out.append(v)
            now += 300.0
        self.assertEqual(out, ["ok", "probe_suspect", "restart", "ok"])
        self.assertEqual(state, {})


STATE_KEYS = {"zero_since", "restarted_at", "query_fail_since", "probe_fail_since",
              "stuck_alerted", "query_alerted"}


class MainProbe(MainSideEffects):
    """main()'s real decision path with the real probe against a fake server."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(W, "PROBE_TIMEOUT_S", PROBE_T),):
            p.start()
            self.addCleanup(p.stop)

    def point_at(self, srv_or_url):
        url = srv_or_url if isinstance(srv_or_url, str) else srv_or_url.url
        p = mock.patch.dict(os.environ, {W.PUBLIC_URL_ENV: url})
        p.start()
        self.addCleanup(p.stop)

    def state(self):
        return json.loads(W.STATE_PATH.read_text())

    def test_host1_probe_401_never_restarts(self):  # ①
        srv = fake_status_server(self, 401)
        self.point_at(srv)
        for _ in range(3):
            self.tick(1)
        self.assertEqual(self.restarts(), [])
        self.assertEqual(self.notes, [])
        self.assertEqual(self.state(), {})
        self.assertGreaterEqual(len(srv.requests), 3)   # the probe really ran

    def test_host1_probe_timeout_two_ticks_restarts(self):  # ②
        srv = fake_status_server(self, block=True)
        self.point_at(srv)
        self.tick(1)
        self.assertEqual(self.restarts(), [])
        self.tick(1)
        self.assertEqual(self.restarts(), [["systemctl", "--user", "restart", W.UNIT]])
        self.assertEqual(len(srv.requests), 2)

    def test_host1_probe_timeout_one_tick_no_restart(self):  # ③
        srv = fake_status_server(self, block=True)
        self.point_at(srv)
        self.tick(1)
        self.assertEqual(self.restarts(), [])
        self.assertIn("probe_fail_since", self.state())
        srv.block, srv.status = False, 401
        self.tick(1)
        self.assertEqual(self.restarts(), [])
        self.assertEqual(self.state(), {})

    def test_host0_two_ticks_restarts_without_probe(self):  # ④
        srv = fake_status_server(self, 401)
        self.point_at(srv)
        self.tick(0)
        self.tick(0)
        self.assertEqual(self.restarts(), [["systemctl", "--user", "restart", W.UNIT]])
        self.assertEqual(srv.requests, [])

    def test_refused_connection_two_ticks_restarts(self):
        self.point_at(closed_port_url())
        self.tick(1)
        self.tick(1)
        self.assertEqual(len(self.restarts()), 1)

    def test_dead_url_after_restart_alerts_once(self):
        srv = fake_status_server(self, block=True)
        self.point_at(srv)
        for _ in range(4):
            self.tick(1)
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(len(self.notes), 1)

    def test_state_file_never_holds_secrets(self):
        srv = fake_status_server(self, block=True)
        self.point_at(srv)
        self.tick(1)
        self.tick(1)
        raw = W.STATE_PATH.read_text()
        self.assertLessEqual(set(json.loads(raw)), STATE_KEYS)
        self.assertNotIn("127.0.0.1", raw)

    def test_log_line_has_label_not_url(self):
        srv = fake_status_server(self, 401)
        self.point_at(srv)
        with mock.patch.object(W, "query_hosts", return_value=1), \
             mock.patch.object(sys, "argv", ["w"]), \
             mock.patch("builtins.print") as pr:
            W.main()
        line = " ".join(str(c.args[0]) for c in pr.call_args_list)
        self.assertIn("probe=401", line)
        self.assertNotIn(srv.url, line)

    def test_dry_run_still_probes(self):
        srv = fake_status_server(self, 401)
        self.point_at(srv)
        self.tick(1, "--dry-run")
        self.assertEqual(len(srv.requests), 1)
        self.assertFalse(W.STATE_PATH.exists())
