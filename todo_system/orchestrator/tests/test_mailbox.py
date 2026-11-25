"""Tests for the orchestrator mailbox (P0). Stdlib unittest, no network."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import atomic_io, message as m
from todo_system.orchestrator import mailbox as mb_mod
from todo_system.orchestrator.mailbox import FileMailbox, GitMailbox, open_mailbox


class EnvelopeTest(unittest.TestCase):
    def test_roundtrip_and_from_alias(self):
        e = m.Envelope(to="a", type=m.TASK_ASSIGN, frm="orchestrator", task_id="t1")
        d = e.to_dict()
        self.assertEqual(d["from"], "orchestrator")  # frm serialized as "from"
        self.assertNotIn("frm", d)
        back = m.Envelope.from_json(e.to_json())
        self.assertEqual(back.msg_id, e.msg_id)
        self.assertEqual(back.frm, "orchestrator")
        self.assertEqual(back.task_id, "t1")

    def test_reply_preserves_conversation(self):
        assign = m.Envelope(to="agentX", type=m.TASK_ASSIGN, task_id="t1")
        rep = assign.reply(m.ACCEPT)
        self.assertEqual(rep.to, "orchestrator")   # back to sender
        self.assertEqual(rep.frm, "agentX")
        self.assertEqual(rep.corr_id, assign.corr_id)  # same conversation
        self.assertEqual(rep.task_id, "t1")

    def test_ulid_monotonic(self):
        ids = [m.new_id() for _ in range(2000)]
        self.assertEqual(ids, sorted(ids))          # time-sortable
        self.assertEqual(len(set(ids)), len(ids))   # unique

    def test_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            m.Envelope(to="a", type="NONSENSE")


class FileMailboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_send_receive_ack(self):
        self.mb.send(m.Envelope(to="agentX", type=m.TASK_ASSIGN, task_id="t1"))
        got = self.mb.receive("agentX")
        self.assertEqual([e.task_id for e in got], ["t1"])
        # at-least-once: still redelivered until acked
        self.assertEqual(len(self.mb.receive("agentX")), 1)
        self.mb.ack("agentX", got)
        self.assertEqual(self.mb.receive("agentX"), [])

    def test_ack_reclaims_files_and_bounds_cursor(self):
        # A long-running orchestrator must not accumulate inbox files or an
        # ever-growing cursor set: ack deletes the consumed file and forgets it.
        inbox = self.mb._inbox("agentX")
        self.mb.send(m.Envelope(to="agentX", type=m.PROGRESS, task_id="t1"))
        got = self.mb.receive("agentX")
        self.assertEqual(len(list(inbox.glob("*.json"))), 1)
        self.mb.ack("agentX", got)
        self.assertEqual(list(inbox.glob("*.json")), [])   # file reclaimed
        self.assertEqual(self.mb._load_seen("agentX"), set())  # cursor stays bounded
        self.assertEqual(self.mb.receive("agentX"), [])        # not redelivered

    def test_acked_envelope_resurrected_is_not_redelivered(self):
        # ack unlinks the file but nothing commits that deletion, so a later
        # `git restore`/`checkout -- .` in the vault brings the very same bytes
        # back. The cursor must still know the id was consumed.
        inbox = self.mb._inbox("agentX")
        self.mb.send(m.Envelope(to="agentX", type=m.TASK_ASSIGN, task_id="t1"))
        [got] = self.mb.receive("agentX")
        path = inbox / f"{got.msg_id}.json"
        raw = path.read_bytes()
        self.mb.ack("agentX", got)
        self.assertFalse(path.exists())

        path.write_bytes(raw)                       # = git restore of the deletion

        self.assertEqual(self.mb.receive("agentX"), [])
        # the next ack sweep reclaims the resurrected file again
        self.mb.send(m.Envelope(to="agentX", type=m.PROGRESS, task_id="t2"))
        self.mb.ack("agentX", self.mb.receive("agentX"))
        self.assertEqual(list(inbox.glob("*.json")), [])

    def test_ack_tombstones_are_bounded(self):
        ids = []
        with mock.patch.object(mb_mod, "ACK_TOMBSTONE_MAX", 3), \
                mock.patch.object(mb_mod.time, "time") as clock:
            for i in range(5):
                clock.return_value = 1000.0 + i
                e = m.Envelope(to="agentX", type=m.PROGRESS, task_id=f"t{i}")
                self.mb.send(e)
                self.mb.ack("agentX", e)
                ids.append(e.msg_id)
            _, acked = self.mb._load_cursor("agentX")
            self.assertEqual(set(acked), set(ids[-3:]))      # cap keeps the newest

            clock.return_value = 1004.0 + mb_mod.ACK_TOMBSTONE_TTL_S + 1
            e = m.Envelope(to="agentX", type=m.PROGRESS, task_id="late")
            self.mb.send(e)
            self.mb.ack("agentX", e)
            _, acked = self.mb._load_cursor("agentX")
            self.assertEqual(set(acked), {e.msg_id})          # TTL expired the rest

    def test_cursor_without_tombstones_still_loads(self):
        # a cursor written by the pre-tombstone build has only `seen`
        self.mb._cursor_path("agentX").parent.mkdir(parents=True)
        self.mb._cursor_path("agentX").write_text('{"seen": ["a"]}', encoding="utf-8")
        self.assertEqual(self.mb._load_cursor("agentX"), ({"a"}, {}))
        self.mb._save_seen("agentX", {"a", "b"})
        self.assertEqual(self.mb._load_seen("agentX"), {"a", "b"})

    def test_crash_during_cursor_write_preserves_prior_cursor(self):
        # A crash mid-write to cursor.json must not leave a torn file that
        # _load_seen would silently read back as an empty set (which would
        # redeliver every already-acked message).
        self.mb._save_seen("agentX", {"a", "b"})
        with mock.patch.object(atomic_io.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.mb._save_seen("agentX", {"a", "b", "c"})
        self.assertEqual(self.mb._load_seen("agentX"), {"a", "b"})

    def test_fifo_order(self):
        for i in range(5):
            self.mb.send(m.Envelope(to="agentX", type=m.PROGRESS, task_id=f"t{i}"))
        got = self.mb.receive("agentX")
        self.assertEqual([e.task_id for e in got], [f"t{i}" for i in range(5)])

    def test_symmetric_reply_to_orchestrator(self):
        assign = m.Envelope(to="agentX", type=m.TASK_ASSIGN, task_id="t1")
        self.mb.send(assign)
        [inbox_msg] = self.mb.receive("agentX")
        self.mb.send(inbox_msg.reply(m.RESULT, payload={"pr": "#42"}))
        [reply] = self.mb.receive("orchestrator")
        self.assertEqual(reply.type, m.RESULT)
        self.assertEqual(reply.payload["pr"], "#42")
        self.assertEqual(reply.corr_id, assign.corr_id)

    def test_factory_file(self):
        mb = open_mailbox(f"file://{self.tmp / 'agents'}")
        self.assertIsInstance(mb, FileMailbox)


class GitMailboxTest(unittest.TestCase):
    """Two clones of a bare origin exchange a message purely through git —
    the cross-host transport that makes remote runners work."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bare = self.tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.bare)],
                       check=True, capture_output=True)
        self.hostA = self._clone("A")
        self.hostB = self._clone("B")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clone(self, name: str) -> Path:
        d = self.tmp / name
        subprocess.run(["git", "clone", str(self.bare), str(d)],
                       check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(d), "config", k, v],
                           check=True, capture_output=True)
        (d / "Worker" / "agents").mkdir(parents=True, exist_ok=True)
        (d / "Worker" / "agents" / ".gitkeep").touch()
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, capture_output=True)
        r = subprocess.run(["git", "-C", str(d), "commit", "-m", "init"],
                           capture_output=True, text=True)
        if r.returncode == 0:  # first clone seeds main; second already has it
            subprocess.run(["git", "-C", str(d), "push", "-u", "origin", "main"],
                           check=True, capture_output=True)
        return d

    def test_cross_host_exchange(self):
        mbA = GitMailbox(self.hostA / "Worker" / "agents")
        mbB = GitMailbox(self.hostB / "Worker" / "agents")

        # Orchestrator on host A dispatches to an agent whose runner is host B.
        assign = m.Envelope(to="sc1-homeland", type=m.TASK_ASSIGN, task_id="research-1")
        mbA.send(assign)  # commits + pushes

        got = mbB.receive("sc1-homeland")  # pulls, then reads inbox
        self.assertEqual([e.task_id for e in got], ["research-1"])
        mbB.ack("sc1-homeland", got)

        # Agent on host B replies; orchestrator on host A receives it.
        mbB.send(got[0].reply(m.RESULT, payload={"branch": "worker/sc1-homeland-1"}))
        back = mbA.receive("orchestrator")
        self.assertEqual([e.type for e in back], [m.RESULT])
        self.assertEqual(back[0].payload["branch"], "worker/sc1-homeland-1")
        self.assertEqual(back[0].corr_id, assign.corr_id)

    # -- cache clone self-heal (MACP_MAILBOX_CACHE_CLONE) ------------------
    def _git(self, d: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True)

    def _stuck_cache_clone(self) -> GitMailbox:
        """Host B ends with the 2026-11-19 shape: a tracked file it still has
        locally modified was deleted upstream, so the autostash pop after the
        rebase conflicts and leaves unmerged entries behind."""
        led = Path("Worker") / "assignments" / "t.json"
        (self.hostA / led).parent.mkdir(parents=True, exist_ok=True)
        (self.hostA / ".gitignore").write_text("Worker/agents/_state/\n", encoding="utf-8")
        (self.hostA / led).write_text("{}", encoding="utf-8")
        self._git(self.hostA, "add", "-A")
        self._git(self.hostA, "commit", "-qm", "ledger row")
        self._git(self.hostA, "push", "-q", "origin", "main")
        self._git(self.hostB, "pull", "-q", "--rebase", "origin", "main")
        (self.hostB / led).write_text('{"local": 1}', encoding="utf-8")       # dirty on B
        self._git(self.hostA, "rm", "-q", str(led))                          # deleted upstream
        self._git(self.hostA, "commit", "-qm", "drop row")
        self._git(self.hostA, "push", "-q", "origin", "main")
        cursor = self.hostB / "Worker" / "agents" / "_state" / "b" / "cursor.json"
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text('{"seen": ["m1"]}', encoding="utf-8")              # ignored
        return GitMailbox(self.hostB / "Worker" / "agents")

    def test_cache_clone_resets_a_stuck_pull_to_origin(self):
        mb = self._stuck_cache_clone()
        with mock.patch.dict(os.environ, {"MACP_MAILBOX_CACHE_CLONE": "1"}):
            mb._pull()
        self.assertFalse(mb._unmerged())
        head = self._git(self.hostB, "rev-parse", "HEAD").stdout.strip()
        origin = self._git(self.hostB, "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(head, origin)
        self.assertEqual(self._git(self.hostB, "status", "--porcelain").stdout.strip(), "")
        # the ignored mailbox cursor survives, so nothing is delivered twice
        cursor = self.hostB / "Worker" / "agents" / "_state" / "b" / "cursor.json"
        self.assertEqual(cursor.read_text(encoding="utf-8"), '{"seen": ["m1"]}')
        # and the next pull works again
        self.assertEqual(self._git(self.hostB, "pull", "-q", "--rebase", "origin", "main").returncode, 0)

    def test_without_cache_clone_a_stuck_pull_is_left_alone(self):
        mb = self._stuck_cache_clone()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MACP_MAILBOX_CACHE_CLONE", None)
            mb._pull()
        # unchanged behaviour off the remote relay clones (HQ's live vault)
        self.assertTrue(mb._unmerged())


class GitMailboxHelperTest(unittest.TestCase):
    """Mock-based coverage of GitMailbox's git plumbing — no real remote."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo_dir = self.tmp / "repo"
        self.repo_dir.mkdir()
        with mock.patch.object(GitMailbox, "_toplevel", return_value=self.repo_dir):
            self.gm = GitMailbox(self.repo_dir / "agents", branch="main")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cp(self, returncode=0, stdout=""):
        return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")

    # -- _toplevel ----------------------------------------------------
    def test_toplevel_returns_parsed_path_on_success(self):
        with mock.patch.object(subprocess, "run", return_value=self._cp(0, "/repo\n")):
            self.assertEqual(GitMailbox._toplevel(Path("/some/root")), Path("/repo"))

    def test_toplevel_falls_back_to_root_when_not_a_repo(self):
        with mock.patch.object(subprocess, "run", return_value=self._cp(1, "")):
            root = Path("/some/root")
            self.assertEqual(GitMailbox._toplevel(root), root)

    # -- _has_origin ----------------------------------------------------
    def test_has_origin_true(self):
        with mock.patch.object(self.gm, "_git", return_value=self._cp(0, "origin\n")) as git:
            self.assertTrue(self.gm._has_origin())
            self.assertIn("remote", git.call_args.args)

    def test_has_origin_false(self):
        with mock.patch.object(self.gm, "_git", return_value=self._cp(0, "")):
            self.assertFalse(self.gm._has_origin())

    # -- _pull ----------------------------------------------------
    def test_pull_noop_without_origin(self):
        with mock.patch.object(self.gm, "_has_origin", return_value=False):
            with mock.patch.object(self.gm, "_git") as git:
                self.gm._pull()
                git.assert_not_called()

    def test_pull_with_origin_success_does_not_abort(self):
        with mock.patch.object(self.gm, "_has_origin", return_value=True):
            with mock.patch.object(self.gm, "_git", return_value=self._cp(0)) as git:
                self.gm._pull()
                git.assert_called_once_with(
                    "-c", "rebase.autoStash=true", "pull", "--rebase", "origin", "main"
                )

    def test_pull_with_origin_failure_aborts_rebase(self):
        with mock.patch.object(self.gm, "_has_origin", return_value=True):
            with mock.patch.object(self.gm, "_git", return_value=self._cp(1)) as git:
                self.gm._pull()
                self.assertEqual(git.call_count, 2)
                git.assert_called_with("rebase", "--abort")

    # -- _commit_push ----------------------------------------------------
    def test_commit_push_noop_when_nothing_staged(self):
        f = self.repo_dir / "msg.json"
        f.write_text("{}")
        calls = {"add": self._cp(0), "diff": self._cp(0)}

        def fake_git(*args, **kwargs):
            if args[0] == "add":
                return calls["add"]
            if args[0] == "diff":
                return calls["diff"]
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(self.gm, "_git", side_effect=fake_git) as git:
            self.gm._commit_push(f)
            called_cmds = [c.args[0] for c in git.call_args_list]
            self.assertEqual(called_cmds, ["add", "diff"])

    def test_commit_push_commits_but_skips_push_without_origin(self):
        f = self.repo_dir / "msg.json"
        f.write_text("{}")

        def fake_git(*args, **kwargs):
            if args[0] == "add":
                return self._cp(0)
            if args[0] == "diff":
                return self._cp(1)  # staged changes present
            if args[0] == "commit":
                return self._cp(0)
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(self.gm, "_has_origin", return_value=False):
            with mock.patch.object(self.gm, "_git", side_effect=fake_git) as git:
                self.gm._commit_push(f)
                called_cmds = [c.args[0] for c in git.call_args_list]
                self.assertEqual(called_cmds, ["add", "diff", "commit"])

    def test_commit_push_succeeds_on_first_push(self):
        f = self.repo_dir / "msg.json"
        f.write_text("{}")

        def fake_git(*args, **kwargs):
            if args[0] == "add":
                return self._cp(0)
            if args[0] == "diff":
                return self._cp(1)
            if args[0] == "commit":
                return self._cp(0)
            if args[0] == "push":
                return self._cp(0)
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(self.gm, "_has_origin", return_value=True):
            with mock.patch.object(self.gm, "_pull") as pull:
                with mock.patch.object(self.gm, "_git", side_effect=fake_git) as git:
                    self.gm._commit_push(f)
                    push_calls = [c for c in git.call_args_list if c.args[0] == "push"]
                    self.assertEqual(len(push_calls), 1)
                    pull.assert_not_called()

    def test_commit_push_retries_via_pull_then_gives_up_after_three_pushes(self):
        f = self.repo_dir / "msg.json"
        f.write_text("{}")

        def fake_git(*args, **kwargs):
            if args[0] == "add":
                return self._cp(0)
            if args[0] == "diff":
                return self._cp(1)
            if args[0] == "commit":
                return self._cp(0)
            if args[0] == "push":
                return self._cp(1)  # always fails
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(self.gm, "_has_origin", return_value=True):
            with mock.patch.object(self.gm, "_pull") as pull:
                with mock.patch.object(self.gm, "_git", side_effect=fake_git) as git:
                    self.gm._commit_push(f)
                    push_calls = [c for c in git.call_args_list if c.args[0] == "push"]
                    self.assertEqual(len(push_calls), 3)
                    self.assertEqual(pull.call_count, 3)


class MailboxMainTest(unittest.TestCase):
    """CLI entrypoint `_main` — exercised over the `file://` transport."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.uri = f"file://{self.tmp / 'agents'}"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = mb_mod._main(argv)
        return rc, out.getvalue()

    def test_init_agent(self):
        rc, out = self._run(["--uri", self.uri, "init-agent", "--id", "agentX"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / "agents" / "agentX" / "inbox" / ".gitkeep").exists())
        self.assertIn("initialized inbox for agentX", out)

    def test_send_and_recv_roundtrip(self):
        rc, out = self._run(
            [
                "--uri", self.uri, "send",
                "--to", "agentX", "--type", m.TASK_ASSIGN,
                "--task", "t1", "--payload", '{"k": 1}',
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn(f"sent {m.TASK_ASSIGN} to agentX", out)

        rc, out = self._run(["--uri", self.uri, "recv", "--agent", "agentX"])
        self.assertEqual(rc, 0)
        self.assertIn("task=t1", out)
        self.assertIn('payload={"k": 1}', out)

    def test_recv_empty_prints_no_new_messages(self):
        rc, out = self._run(["--uri", self.uri, "recv", "--agent", "agentX"])
        self.assertEqual(rc, 0)
        self.assertIn("(no new messages)", out)

    def test_recv_with_ack(self):
        self._run(
            [
                "--uri", self.uri, "send",
                "--to", "agentX", "--type", m.TASK_ASSIGN, "--task", "t1",
            ]
        )
        rc, out = self._run(["--uri", self.uri, "recv", "--agent", "agentX", "--ack"])
        self.assertEqual(rc, 0)
        self.assertIn("acked 1 message(s)", out)

        rc, out = self._run(["--uri", self.uri, "recv", "--agent", "agentX"])
        self.assertEqual(rc, 0)
        self.assertIn("(no new messages)", out)

    def test_send_invalid_json_payload_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            self._run(
                [
                    "--uri", self.uri, "send",
                    "--to", "agentX", "--type", m.TASK_ASSIGN, "--payload", "not-json",
                ]
            )

    def test_send_unknown_type_rejected_by_argparse(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                mb_mod._main(
                    ["--uri", self.uri, "send", "--to", "agentX", "--type", "NONSENSE"]
                )


if __name__ == "__main__":
    unittest.main()
