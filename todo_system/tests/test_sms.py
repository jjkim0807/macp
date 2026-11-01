"""Tests for todo_system.sms (phone SMS inbox) and the dashboard's POST /api/sms.

The ingest endpoint is reachable from the internet through the dashboard tunnel,
so its auth is pinned over a real socket: only MACP_SMS_TOKEN may write, the
dashboard token may not, and the SMS token grants no GET access.
"""
from __future__ import annotations

import datetime as dt
import http.client
import importlib.util
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from todo_system import sms

VAULT = Path(__file__).resolve().parents[2]


def _load_dashboard():
    spec = importlib.util.spec_from_file_location("macp_dashboard_sms", VAULT / "scripts" / "macp_dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"MACP_SMS_DIR": self.tmp.name + "/sms"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(sms._ALERT_MEMORY, clear=True)
        p.start()
        self.addCleanup(p.stop)


class StoreTests(_StoreCase):
    def test_append_dedups_by_id_and_is_private(self):
        m = sms.normalize({"id": "a1", "from": "15880000", "body": "hi", "sent_at": 1760500000000})
        self.assertEqual(sms.append([m]), 1)
        self.assertEqual(sms.append([m]), 0)  # phone retry
        self.assertEqual(len(sms.query()), 1)
        self.assertEqual(sms.query()[0]["sent_at"], "2025-10-15T03:46:40+00:00")
        self.assertEqual(stat.S_IMODE(sms.inbox_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(sms.store_dir().stat().st_mode), 0o700)

    def test_missing_id_is_derived_so_retries_still_dedup(self):
        raw = {"from": "1588", "body": "same", "sent_at": 1}
        sms.append([sms.normalize(raw)])
        sms.append([sms.normalize(raw)])
        self.assertEqual(len(sms.query()), 1)

    def test_retention_drops_old_messages(self):
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
        sms.append([sms.normalize({"id": "old", "from": "1", "body": "x"})], now=old)
        sms.append([sms.normalize({"id": "new", "from": "1", "body": "y"})])
        self.assertEqual([r["id"] for r in sms.query()], ["new"])

    def test_normalize_rejects_bad_input(self):
        for bad in ({}, {"from": "1"}, {"body": "x"}, {"from": "1", "body": 3}, "str"):
            with self.assertRaises(ValueError):
                sms.normalize(bad)

    def test_query_filters(self):
        sms.append([sms.normalize({"id": "1", "from": "+82 10-1234-5678", "body": "hello"}),
                    sms.normalize({"id": "2", "from": "SNU", "body": "인증번호 [123456]"})])
        self.assertEqual([r["id"] for r in sms.query(sender="1012345678")], ["1"])
        self.assertEqual([r["id"] for r in sms.query(sender="snu")], ["2"])
        self.assertEqual([r["id"] for r in sms.query(pattern="인증")], ["2"])


class AlertThrottleTests(_StoreCase):
    T0 = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)

    def test_once_per_kind_per_6h(self):
        t0 = self.T0
        self.assertTrue(sms.should_alert("auth", now=t0))
        self.assertFalse(sms.should_alert("auth", now=t0 + dt.timedelta(hours=5, minutes=59)))
        self.assertTrue(sms.should_alert("bad_payload", now=t0 + dt.timedelta(minutes=1)))
        self.assertTrue(sms.should_alert("auth", now=t0 + dt.timedelta(hours=6)))

    def test_state_file_persists_private(self):
        self.assertTrue(sms.should_alert("auth", now=self.T0))
        p = sms.alert_state_path()
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertIn("auth", json.loads(p.read_text()))
        sms._ALERT_MEMORY.clear()  # a fresh process has no memory; the file still holds the window
        self.assertFalse(sms.should_alert("auth", now=self.T0 + dt.timedelta(hours=1)))

    def test_corrupt_state_resets_quietly(self):
        sms.store_dir().mkdir(parents=True)
        sms.alert_state_path().write_text("{not json")
        self.assertTrue(sms.should_alert("auth", now=self.T0))
        self.assertIn("auth", json.loads(sms.alert_state_path().read_text()))

    def test_unreadable_state_falls_back_to_memory(self):
        sms.alert_state_path().mkdir(parents=True)  # a directory: read raises OSError
        self.assertTrue(sms.should_alert("auth", now=self.T0))
        self.assertFalse(sms.should_alert("auth", now=self.T0 + dt.timedelta(minutes=1)))

    def test_compose_has_no_secrets_and_fits(self):
        for kind, code in (("auth", 401), ("auth", 429), ("bad_payload", 400),
                           ("too_large", 413), ("other", 500)):
            msg = sms.compose_ingest_alert(kind, code)
            self.assertLessEqual(len(msg), sms.ALERT_MAX_CHARS)
            self.assertIn(str(code), msg)
            if kind in sms.INGEST_ALERT_HINTS:
                self.assertIn(sms.INGEST_ALERT_HINTS[kind], msg)


class ExtractCodeTests(unittest.TestCase):
    def test_keyword_wins_over_earlier_number(self):
        self.assertEqual(sms.extract_code("[Web발신] 1588-1234 고객센터. 인증번호는 482913 입니다"), "482913")

    def test_bracketed_code(self):
        self.assertEqual(sms.extract_code("[서울대] 인증번호 [0731]을 입력해주세요"), "0731")

    def test_plain_number_fallback_and_none(self):
        self.assertEqual(sms.extract_code("Your G-code 552211"), "552211")
        self.assertEqual(sms.extract_code("Use 9182 to sign in"), "9182")
        self.assertIsNone(sms.extract_code("no digits here 12"))


class CliTests(_StoreCase):
    def _run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = sms.main(list(argv))
        return rc, buf.getvalue()

    def test_list_json(self):
        sms.append([sms.normalize({"id": "1", "from": "1588", "body": "hi"})])
        rc, out = self._run("list", "--since", "5m", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["body"], "hi")

    def test_wait_code_ignores_old_and_times_out(self):
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        sms.append([sms.normalize({"id": "o", "from": "1", "body": "code 111111"})], now=old)
        rc, out = self._run("wait-code", "--timeout", "0", "--interval", "0")
        self.assertEqual((rc, out), (1, ""))
        rc, out = self._run("wait-code", "--timeout", "0", "--since", "10m")
        self.assertEqual((rc, out.strip()), (0, "111111"))

    def test_wait_code_picks_up_message_arriving_later(self):
        def later():
            sms.append([sms.normalize({"id": "n", "from": "SNU", "body": "인증번호 246810"})])
        t = threading.Timer(0.2, later)
        t.start()
        self.addCleanup(t.cancel)
        rc, out = self._run("wait-code", "--from", "snu", "--timeout", "5", "--interval", "0.05")
        self.assertEqual((rc, out.strip()), (0, "246810"))


class IngestEndpointTests(_StoreCase):
    DASH = "dash-token"
    SMS = "sms-token"

    def setUp(self):
        super().setUp()
        self.M = _load_dashboard()
        p = mock.patch.dict(os.environ, {"MACP_SMS_TOKEN": self.SMS})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.M, "collect_status", return_value={"ok": True})
        p.start()
        self.addCleanup(p.stop)
        self.srv = self.M.serve("127.0.0.1", 0, self.DASH, public_exposure=True)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        # Every test records alerts instead of reaching the real notifier.
        self.alerts = []
        p = mock.patch.object(self.M, "SMS_ALERT_NOTIFY", self.alerts.append)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.M.wait_ingest_alerts)  # LIFO: runs before the server stops

    def _settle(self, M=None):
        self.assertTrue((M or self.M).wait_ingest_alerts(5))

    def _req(self, method, path, body=None, token=None, port=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=5)
        headers = {"Content-Type": "application/json", **(headers or {})}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_sms_token_stores_batch(self):
        st, data = self._req("POST", "/api/sms", {"messages": [
            {"id": "x", "from": "1588", "body": "a"}, {"id": "y", "from": "1588", "body": "b"}]},
            token=self.SMS)
        self.assertEqual((st, json.loads(data)), (200, {"received": 2, "stored": 2}))
        self.assertEqual(len(sms.query()), 2)

    def test_single_object_body(self):
        st, _ = self._req("POST", "/api/sms", {"from": "1588", "body": "a"}, token=self.SMS)
        self.assertEqual(st, 200)

    def test_dashboard_token_cannot_write_and_sms_token_cannot_read(self):
        st, _ = self._req("POST", "/api/sms", {"from": "1", "body": "a"}, token=self.DASH)
        self.assertEqual(st, 401)
        st, _ = self._req("POST", "/api/sms", {"from": "1", "body": "a"})
        self.assertEqual(st, 401)
        st, _ = self._req("GET", "/api/status", token=self.SMS)
        self.assertEqual(st, 401)
        self.assertEqual(sms.query(), [])

    def test_bad_payload_and_unknown_path(self):
        st, _ = self._req("POST", "/api/sms", {"from": "1"}, token=self.SMS)
        self.assertEqual(st, 400)
        st, _ = self._req("POST", "/api/other", {"from": "1", "body": "a"}, token=self.SMS)
        self.assertEqual(st, 404)

    def test_setup_page_needs_dashboard_token_and_carries_link(self):
        st, _ = self._req("GET", "/sms-setup", token=self.SMS)
        self.assertEqual(st, 401)
        st, data = self._req("GET", "/sms-setup", token=self.DASH)
        self.assertEqual(st, 200)
        self.assertIn(f"macpsms://setup?url=http%3A%2F%2F127.0.0.1%3A{self.port}%2Fapi%2Fsms&amp;token={self.SMS}",
                      data.decode())

    def test_disabled_without_sms_token(self):
        with mock.patch.dict(os.environ, {"MACP_SMS_TOKEN": ""}):
            st, _ = self._req("POST", "/api/sms", {"from": "1", "body": "a"}, token=self.SMS)
        self.assertEqual(st, 503)

    # -- ingest-failure alert ----------------------------------------------
    SECRET_BODY = {"from": "SECRET-FROM-999", "body": "SECRET-BODY-123"}

    def _valid_post(self, mid):
        st, _ = self._req("POST", "/api/sms", {"id": mid, "from": "1588", "body": "ok"}, token=self.SMS)
        self.assertEqual(st, 200)
        self.assertIn(mid, [r["id"] for r in sms.query()])

    def test_401_alerts_once_without_secrets(self):
        for _ in range(2):
            st, _ = self._req("POST", "/api/sms", self.SECRET_BODY, token="SECRET-TOKEN-XYZ")
            self.assertEqual(st, 401)
        self._settle()
        self.assertEqual(len(self.alerts), 1)
        msg = self.alerts[0]
        self.assertIn("401", msg)
        for secret in ("SECRET-TOKEN-XYZ", "SECRET-FROM-999", "SECRET-BODY-123", self.SMS, self.DASH):
            self.assertNotIn(secret, msg)

    def test_429_shares_auth_window(self):
        codes = [self._req("POST", "/api/sms", self.SECRET_BODY, token="wrong")[0] for _ in range(6)]
        self.assertEqual(codes[-1], 429)
        self._settle()
        self.assertEqual(len(self.alerts), 1)

    # -- refuse while throttled + per-client buckets
    #    (macp-dashboard-auth-limiter-refuse-while-throttled) --------------------

    def _trip_sms(self, headers=None):
        codes = [self._req("POST", "/api/sms", self.SECRET_BODY, token="wrong", headers=headers)[0]
                 for _ in range(self.M.Handler.RL_MAX_FAILS)]
        self.assertEqual(codes[-1], 429, codes)

    def test_correct_sms_token_refused_while_throttled(self):
        self._trip_sms()
        st, _ = self._req("POST", "/api/sms", {"id": "late", "from": "1588", "body": "ok"},
                          token=self.SMS)
        self.assertEqual(st, 429)
        self.assertEqual(sms.query(), [])                  # nothing stored while throttled
        self._settle()
        self.assertEqual(len(self.alerts), 1)              # auth kind: one alert per 6h

    def test_sms_failures_keyed_per_forwarded_client(self):
        attacker = {"X-Forwarded-For": "100.64.0.9"}
        phone = {"X-Forwarded-For": "100.64.0.2"}
        self._trip_sms(attacker)
        st, _ = self._req("POST", "/api/sms", {"id": "phone-1", "from": "1588", "body": "ok"},
                          token=self.SMS, headers=phone)
        self.assertEqual(st, 200)
        self.assertIn("phone-1", [r["id"] for r in sms.query()])
        st, _ = self._req("POST", "/api/sms", {"id": "atk-1", "from": "1588", "body": "ok"},
                          token=self.SMS, headers=attacker)
        self.assertEqual(st, 429)
        self.assertNotIn("atk-1", [r["id"] for r in sms.query()])

    def test_400_and_413_alert_per_kind_200_never(self):
        with mock.patch.object(self.M.Handler, "SMS_MAX_BYTES", 64):
            st, _ = self._req("POST", "/api/sms", {"from": "1", "body": "x" * 100}, token=self.SMS)
        self.assertEqual(st, 413)
        for _ in range(2):
            st, data = self._req("POST", "/api/sms", {"from": "1"}, token=self.SMS)
            self.assertEqual(st, 400)
        parse_error = json.loads(data)["error"]
        self._valid_post("ok1")
        self._settle()
        self.assertEqual(len(self.alerts), 2)
        self.assertEqual(sum("413" in m for m in self.alerts), 1)
        self.assertEqual(sum("400" in m for m in self.alerts), 1)
        for m in self.alerts:
            self.assertNotIn(parse_error, m)

    def test_200_never_alerts(self):
        self._valid_post("only")
        self._settle()
        self.assertEqual(self.alerts, [])

    def test_503_and_404_do_not_alert(self):
        with mock.patch.dict(os.environ, {"MACP_SMS_TOKEN": ""}):
            st, _ = self._req("POST", "/api/sms", {"from": "1", "body": "a"}, token="wrong")
        self.assertEqual(st, 503)
        st, _ = self._req("POST", "/api/other", {"from": "1", "body": "a"}, token="wrong")
        self.assertEqual(st, 404)
        self._settle()
        self.assertEqual(self.alerts, [])

    def test_restart_does_not_reset_throttle(self):
        st, _ = self._req("POST", "/api/sms", self.SECRET_BODY, token="wrong")
        self.assertEqual(st, 401)
        self._settle()
        self.assertEqual(len(self.alerts), 1)
        # Restart: fresh module, fresh server, empty in-process memory, same store.
        M2 = _load_dashboard()
        alerts2 = []
        p = mock.patch.object(M2, "SMS_ALERT_NOTIFY", alerts2.append)
        p.start()
        self.addCleanup(p.stop)
        srv2 = M2.serve("127.0.0.1", 0, self.DASH, public_exposure=True)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        self.addCleanup(srv2.server_close)
        self.addCleanup(srv2.shutdown)
        self.addCleanup(M2.wait_ingest_alerts)
        sms._ALERT_MEMORY.clear()
        st, _ = self._req("POST", "/api/sms", self.SECRET_BODY, token="wrong", port=srv2.server_address[1])
        self.assertEqual(st, 401)
        self._settle(M2)
        self.assertEqual(alerts2, [])

    def test_notifier_failure_is_fail_open(self):
        def boom(_msg):
            raise RuntimeError("SECRET-BODY-123")
        with mock.patch.object(self.M, "SMS_ALERT_NOTIFY", boom), \
                self.assertLogs("macp.dashboard", "WARNING") as logs:
            st, _ = self._req("POST", "/api/sms", self.SECRET_BODY, token="wrong")
            self.assertEqual(st, 401)
            st, _ = self._req("POST", "/api/sms", {"from": "1"}, token=self.SMS)
            self.assertEqual(st, 400)
            self._valid_post("after-boom")
            self._settle()
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("SECRET-BODY-123", text)

    def test_hanging_notifier_does_not_delay_reply(self):
        ev = threading.Event()
        with mock.patch.object(self.M, "SMS_ALERT_NOTIFY", lambda _m: ev.wait(10)):
            self.addCleanup(ev.set)  # runs before the setUp wait-cleanup (LIFO)
            st, _ = self._req("POST", "/api/sms", self.SECRET_BODY, token="wrong")
            self.assertEqual(st, 401)
            self._valid_post("while-hung")
        ev.set()
        self._settle()

    def test_state_error_is_fail_open(self):
        with mock.patch.object(sms, "should_alert", side_effect=OSError("disk")):
            st, _ = self._req("POST", "/api/sms", self.SECRET_BODY, token="wrong")
            self.assertEqual(st, 401)
            st, _ = self._req("POST", "/api/sms", {"from": "1"}, token=self.SMS)
            self.assertEqual(st, 400)
            self._valid_post("after-oserror")
            self._settle()
        self.assertEqual(self.alerts, [])

    def test_default_sender_goes_through_worker_kakao_notify(self):
        from todo_system import worker
        with mock.patch.object(worker, "kakao_notify") as kn:
            self.M._send_ingest_alert("x")
        kn.assert_called_once_with("x")


if __name__ == "__main__":
    unittest.main()
