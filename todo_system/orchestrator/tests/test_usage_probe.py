"""usage_probe.record — the thin best-effort seam scripts/planner_tick.sh and
scripts/brief.sh call after `claude -p` exits. It reuses stages.stage_usage (no
new parser); these tests pin the four transcript outcomes, the append/never-raise
contract, and that its kill switch means the same as the stage loop's. The spy
section at the bottom pins the argv/kwargs the probe hands to the reader."""
from __future__ import annotations

import builtins
import datetime
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import pytest

from todo_system.orchestrator import stages, usage_probe


def _asst(mid, model="m-1", **usage) -> str:
    """One assistant line in claude 2.1.277's transcript shape (the four
    message.usage counters); mirrors test_stages._asst so the fixtures agree."""
    u = dict.fromkeys(stages.USAGE_TOKEN_KEYS, 0)
    u.update(usage)
    return json.dumps({"type": "assistant",
                       "message": {"id": mid, "model": model, "usage": u}})


def _cost(value) -> str:
    return json.dumps({"type": "cost-state", "totalCostUSD": value})


class UsageProbeRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = self.tmp / "cfg"
        self.dest = self.tmp / "state" / "planner_usage.jsonl"
        self.sid = str(uuid.uuid4())
        # stage_usage reads CLAUDE_CONFIG_DIR from os.environ (the probe passes no
        # env), exactly as the scripts run it inheriting the claude call's env.
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _plant(self, *lines, slug="-home-user-projects-vault"):
        """Put a transcript where the CLI would have written it — any slug, the
        session id is globbed (transcript_path)."""
        p = self.cfg / "projects" / slug / f"{self.sid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")

    def _run(self, *extra, sid=None, dest=None, rc="0", source="planner_tick") -> int:
        sid = self.sid if sid is None else sid
        dest = str(self.dest) if dest is None else dest
        return usage_probe.main(["record", "--source", source, "--dest", dest,
                                 "--session-id", sid, "--rc", str(rc), *extra])

    def _rows(self, dest=None) -> list[dict]:
        p = Path(str(self.dest) if dest is None else dest)
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip()]

    # --- the four transcript subtypes -------------------------------------
    def test_success_records_the_nine_usage_keys(self):
        self._plant(_asst("a", model="m-1", input_tokens=11, output_tokens=22,
                          cache_creation_input_tokens=3, cache_read_input_tokens=4),
                    _cost(0.5))
        self.assertEqual(self._run("--model", "m-1", "--duration-ms", "1500"), 0)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "planner_tick")
        self.assertIn("ts", row)
        for k in stages.USAGE_KEYS:
            self.assertIn(k, row, k)
        # Exactly source + ts + the reader's record — nothing extra rides along.
        self.assertEqual(set(row), {"source", "ts", *stages.USAGE_KEYS})
        # ts is ISO-8601 in UTC; checked by shape, never against the real clock.
        ts = datetime.datetime.fromisoformat(row["ts"])
        self.assertEqual(ts.utcoffset(), datetime.timedelta(0))
        self.assertTrue(row["ts"].endswith("+00:00"), row["ts"])
        self.assertEqual(
            {k: row[k] for k in stages.USAGE_KEYS},
            {"input_tokens": 11, "output_tokens": 22,
             "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4,
             "num_turns": 1, "duration_ms": 1500, "model": "m-1",
             "session_id": self.sid, "total_cost_usd": 0.5})

    def test_error_max_turns_records_full_usage_despite_nonzero_rc(self):
        # error_max_turns exits nonzero but its transcript DOES carry usage — a
        # full record, never `unavailable`: rc alone must not demote a real read.
        self._plant(_asst("a", output_tokens=5), _asst("b", output_tokens=6), _cost(0.2))
        self.assertEqual(self._run(rc="1"), 0)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("unavailable", rows[0])
        self.assertEqual(rows[0]["num_turns"], 2)
        self.assertEqual(rows[0]["output_tokens"], 11)
        self.assertEqual(rows[0]["total_cost_usd"], 0.2)

    def test_refused_call_records_unavailable_no_usage(self):
        # A refused call (quota/auth) leaves only the CLI's own synthetic notice —
        # a transcript with no API usage.
        self._plant(_asst("n", model="<synthetic>"), _cost(0))
        self.assertEqual(self._run(rc="1"), 0)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "planner_tick")
        self.assertEqual(rows[0]["unavailable"], "no usage in transcript (rc=1)")
        self.assertEqual(set(rows[0]), {"source", "ts", "unavailable"})

    def test_cli_crash_records_unavailable_transcript_not_found(self):
        # No transcript planted at all + nonzero rc → the CLI died before a session.
        self.assertEqual(self._run(rc="2"), 0)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unavailable"], "cli crash (rc=2): transcript not found")
        self.assertEqual(set(rows[0]), {"source", "ts", "unavailable"})

    # --- append / kill switch / never-raise -------------------------------
    def test_appends_one_line_per_call(self):
        self._plant(_asst("a", output_tokens=1))
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._run(), 0)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        # The second call appended; the first row survived intact.
        for row in rows:
            self.assertEqual(row["source"], "planner_tick")
            self.assertEqual(row["output_tokens"], 1)
            self.assertEqual(row["session_id"], self.sid)

    def test_missing_parent_dirs_are_created(self):
        self._plant(_asst("a", output_tokens=1))
        dest = self.tmp / "a" / "b" / "u.jsonl"
        self.assertFalse(dest.parent.exists())
        self.assertEqual(self._run(dest=str(dest)), 0)
        self.assertEqual(len(self._rows(dest)), 1)

    def _assert_no_record_for_sid(self, sid):
        # The real reader returns None for this sid (not a raise swallowed by
        # main) — the `rec is None` early return, which also precedes the mkdir.
        self.assertIsNone(stages.stage_usage([stages.SESSION_ID_FLAG, sid],
                                             returncode=0, duration_ms=None))
        with mock.patch.object(stages, "stage_usage", wraps=stages.stage_usage) as m:
            self.assertEqual(self._run(sid=sid), 0)
        m.assert_called_once()
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.parent.exists())

    def test_empty_session_id_records_nothing(self):
        self._plant(_asst("a", output_tokens=1))   # transcript is there; sid is ""
        self._assert_no_record_for_sid("")

    def test_non_canonical_session_id_records_nothing(self):
        self._plant(_asst("a", output_tokens=1))
        self._assert_no_record_for_sid("not-a-uuid")

    def test_unwritable_dest_never_raises_and_records_nothing(self):
        self._plant(_asst("a", output_tokens=1))
        blocker = self.tmp / "blocker"           # a FILE where a dir must go, so
        blocker.write_text("x", encoding="utf-8")  # mkdir(parents=True) raises
        dest = blocker / "sub" / "usage.jsonl"
        self.assertEqual(self._run(dest=str(dest)), 0)
        self.assertFalse(Path(dest).exists())

    def test_a_raising_reader_never_escapes(self):
        self._plant(_asst("a", output_tokens=1))
        with mock.patch.object(stages, "stage_usage",
                               side_effect=RuntimeError("boom")) as m:
            self.assertEqual(self._run(), 0)
        m.assert_called_once()
        self.assertFalse(self.dest.exists())

    def test_a_failing_stages_import_records_nothing(self):
        # The bare-python3 risk (plan Risk note): the orchestrator package can't
        # import. _record's lazy `from ... import stages` then raises ImportError,
        # which main() must swallow into a no-record, exit 0.
        self._plant(_asst("a", output_tokens=1))   # a real transcript is there…
        real = builtins.__import__

        def boom(name, *args, **kwargs):
            fromlist = kwargs.get("fromlist", args[2] if len(args) >= 3 else ())
            if name == "todo_system.orchestrator" and fromlist and "stages" in fromlist:
                raise ImportError("no stages in bare python3")
            return real(name, *args, **kwargs)

        with mock.patch.object(stages, "stage_usage", wraps=stages.stage_usage) as m:
            with mock.patch.object(builtins, "__import__", side_effect=boom):
                # RED control: the lazy import really fails before main() swallows it.
                with self.assertRaises(ImportError):
                    usage_probe._record(source="s", dest=str(self.dest),
                                        session_id=self.sid, rc=None,
                                        duration_ms=None, model=None)
                self.assertEqual(self._run(rc="0"), 0)
        m.assert_not_called()                       # the reader was never reached
        self.assertFalse(self.dest.exists())        # …but the import failed first


# --- reader seam: argv/kwargs handed to stages.stage_usage ----------------
class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return None


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    monkeypatch.setattr(stages, "stage_usage", s)
    return s


def _spy_run(dest, *extra):
    return usage_probe.main(["record", "--source", "planner", "--dest", str(dest),
                             "--session-id", "sid-1", *extra])


def test_argv_with_model(spy, tmp_path):
    assert _spy_run(tmp_path / "u.jsonl", "--model", "m") == 0
    assert spy.calls[0][0] == [stages.SESSION_ID_FLAG, "sid-1", "--model", "m"]


def test_argv_without_model(spy, tmp_path):
    assert _spy_run(tmp_path / "u.jsonl") == 0
    assert spy.calls[0][0] == [stages.SESSION_ID_FLAG, "sid-1"]


def test_rc_and_duration_forwarded_as_kwargs(spy, tmp_path):
    dest = tmp_path / "u.jsonl"
    assert _spy_run(dest, "--rc", "3", "--duration-ms", "120") == 0
    kwargs = spy.calls[0][1]
    assert kwargs["returncode"] == 3
    assert kwargs["duration_ms"] == 120

    assert _spy_run(dest) == 0
    kwargs = spy.calls[1][1]
    assert kwargs["returncode"] is None
    assert kwargs["duration_ms"] is None


if __name__ == "__main__":
    unittest.main()
