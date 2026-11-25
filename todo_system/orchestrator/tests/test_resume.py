"""Tests for the conditional deferred resume (resume-when).

Everything here stays inside a `tempfile.mkdtemp()` vault skeleton. Two things
this suite must never touch:

- the **real vault** — `schedule()` writes into `Worker/_state/pending_resumes/`,
  a gitignored runtime path, so a test that used the real root would leave a live
  reservation that the running orchestrator daemon would then consume;
- the **real git** — every git call goes through the `_run` seam and the fakes
  below return `subprocess.CompletedProcess` in the same shape
  `subprocess.run(..., capture_output=True, text=True)` produces, so a fake that
  drifts from production shows up as a type error here rather than as a wrong
  verdict in the tick.

The `_run` seam is passed as an argument rather than patched onto the module's
`subprocess` name, which is the one global module object shared by every
importer (`conftest.py` documents the 2026-09-10 accident where patching it
killed the whole desktop session).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import cli, kakao_format
from todo_system.orchestrator import resume
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator, _parse_ts
from todo_system.orchestrator.registry import Registry
from todo_system.orchestrator.assignment import (
    AWAITING_HUMAN,
    DONE,
    FAILED,
    IN_PROGRESS,
    Assignment,
    Ledger,
)

NOW = dt.datetime(2026, 9, 25, 21, 0, 0, tzinfo=dt.timezone.utc)


def _completed(args, rc: int, out: str = "", err: str = ""):
    """Exactly what `subprocess.run(capture_output=True, text=True)` returns."""
    return subprocess.CompletedProcess(args, rc, out, err)


def _fake_run(*, branch: str = "main", ancestor_rc: int = 0, raises: bool = False):
    """A `_run` stand-in that answers the two git questions resume.py asks."""

    def run(argv, **kwargs):
        if raises:
            raise OSError("git unavailable")
        if argv[1:3] == ["rev-parse", "--abbrev-ref"]:
            return _completed(argv, 0, f"{branch}\n")
        if argv[1:3] == ["merge-base", "--is-ancestor"]:
            return _completed(argv, ancestor_rc)
        raise AssertionError(f"unexpected git call: {argv}")

    return run


class VaultFixture(unittest.TestCase):
    """A minimal vault skeleton: Todo/items + Worker/assignments."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "Todo" / "items").mkdir(parents=True)
        (self.root / "Worker" / "assignments").mkdir(parents=True)
        self.ledger = Ledger(self.root / "Worker" / "assignments")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_task(self, slug: str) -> Path:
        p = self.root / "Todo" / "items" / f"{slug}.md"
        p.write_text(f"---\nstatus: open\n---\n\n# {slug}\n", encoding="utf-8")
        return p

    def write_assignment(self, slug: str, *, state: str = AWAITING_HUMAN,
                         **kwargs) -> Assignment:
        a = Assignment(task_id=slug, agent_id="ai-1", corr_id="c1", state=state,
                       **kwargs)
        self.ledger.save(a)
        return a

    def seed(self, slug: str, *, state: str = AWAITING_HUMAN, **kwargs) -> Assignment:
        self.write_task(slug)
        return self.write_assignment(slug, state=state, **kwargs)


class ScheduleTest(VaultFixture):
    def test_writes_the_record_with_the_documented_schema(self):
        self.seed("lowturnover")
        path = resume.schedule(self.root, "lowturnover", after_commit="a1b2c3d",
                               expire_hours=24, note="long_job 반영 후 재개", now=NOW)

        self.assertEqual(path, resume.record_path(self.root, "lowturnover"))
        self.assertEqual(
            path.parent,
            self.root / "Worker" / "_state" / "pending_resumes",
            "레코드는 gitignored 런타임 경로에만 산다")
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rec["slug"], "lowturnover")
        self.assertEqual(rec["after_commit"], "a1b2c3d")
        self.assertEqual(rec["note"], "long_job 반영 후 재개")
        self.assertEqual(rec["created_ts"], NOW.isoformat())
        self.assertEqual(rec["expires_ts"], (NOW + dt.timedelta(hours=24)).isoformat())

    def test_default_expiry_is_24h(self):
        self.seed("lowturnover")
        path = resume.schedule(self.root, "lowturnover", now=NOW)
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(resume.DEFAULT_EXPIRE_HOURS, 24)
        self.assertEqual(rec["expires_ts"], (NOW + dt.timedelta(hours=24)).isoformat())
        self.assertEqual(rec["after_commit"], "", "sha 없는 예약은 빈 문자열")

    def test_missing_task_md_is_refused(self):
        self.write_assignment("ghost")
        with self.assertRaises(resume.ResumeError) as cm:
            resume.schedule(self.root, "ghost", now=NOW)
        self.assertIn("태스크 파일이 없습니다", str(cm.exception))
        self.assertFalse(resume.resume_dir(self.root).exists(),
                         "거부된 예약은 디렉터리조차 만들지 않는다")

    def test_missing_assignment_is_refused_with_its_own_message(self):
        self.write_task("never-dispatched")
        with self.assertRaises(resume.ResumeError) as cm:
            resume.schedule(self.root, "never-dispatched", now=NOW)
        self.assertIn("assignment가 없습니다", str(cm.exception))

    def test_empty_slug_is_refused(self):
        with self.assertRaises(resume.ResumeError):
            resume.schedule(self.root, "   ", now=NOW)

    def test_non_positive_expiry_is_refused(self):
        self.seed("lowturnover")
        with self.assertRaises(resume.ResumeError):
            resume.schedule(self.root, "lowturnover", expire_hours=0, now=NOW)

    def test_blank_slugs_are_refused_without_side_effects(self):
        for slug in ("", None, "   ", "\t\n"):
            with self.subTest(slug=slug):
                with self.assertRaises(resume.ResumeError) as cm:
                    resume.schedule(self.root, slug, now=NOW)
                self.assertIn("slug가 비어", str(cm.exception))
                self.assertFalse(resume.resume_dir(self.root).exists())

    def test_non_positive_expiry_values_are_refused_without_side_effects(self):
        self.seed("lowturnover")
        for hours in (0, -1, -0.5):
            with self.subTest(expire_hours=hours):
                with self.assertRaises(resume.ResumeError) as cm:
                    resume.schedule(self.root, "lowturnover", expire_hours=hours,
                                    now=NOW)
                self.assertIn("0보다 커야", str(cm.exception))
                self.assertFalse(resume.resume_dir(self.root).exists())

    def test_missing_assignment_leaves_no_directory(self):
        self.write_task("never-dispatched")
        with self.assertRaises(resume.ResumeError):
            resume.schedule(self.root, "never-dispatched", now=NOW)
        self.assertFalse(resume.resume_dir(self.root).exists(),
                         "거부된 예약은 디렉터리조차 만들지 않는다")

    def test_fractional_expiry_hours(self):
        self.seed("lowturnover")
        path = resume.schedule(self.root, "lowturnover", expire_hours=0.5, now=NOW)
        self.assertEqual(
            path,
            self.root / "Worker" / "_state" / "pending_resumes" / "lowturnover.json")
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rec["expires_ts"],
                         (NOW + dt.timedelta(minutes=30)).isoformat())


class EvaluateTest(unittest.TestCase):
    """`evaluate` is pure — no vault, no git, no ledger."""

    def record(self, *, after_commit="a1b2c3d", hours_left=1.0):
        return {"slug": "s", "after_commit": after_commit,
                "created_ts": NOW.isoformat(),
                "expires_ts": (NOW + dt.timedelta(hours=hours_left)).isoformat()}

    def verdict(self, *, branch="main", head_contains=True, state=AWAITING_HUMAN,
                **rec):
        return resume.evaluate(self.record(**rec), branch=branch,
                               head_contains=head_contains, state=state, now=NOW)

    def test_all_conditions_met_resumes(self):
        self.assertEqual(self.verdict(), resume.RESUME)

    def test_failed_state_also_resumes(self):
        self.assertEqual(self.verdict(state=FAILED), resume.RESUME)

    def test_branch_not_main_waits(self):
        self.assertEqual(self.verdict(branch="worker/other-task"), resume.WAIT)

    def test_unknown_branch_waits(self):
        """git 실패(`None`)는 "아니다"가 아니라 "아직 모른다"다."""
        self.assertEqual(self.verdict(branch=None), resume.WAIT)

    def test_sha_not_ancestor_waits(self):
        self.assertEqual(self.verdict(head_contains=False), resume.WAIT)

    def test_undecidable_ancestry_waits(self):
        """없는 sha를 "조상이 아님"으로 읽으면 fail-safe가 깨진다."""
        self.assertEqual(self.verdict(head_contains=None), resume.WAIT)

    def test_no_after_commit_skips_the_ancestry_gate(self):
        self.assertEqual(self.verdict(after_commit="", head_contains=None),
                         resume.RESUME)

    def test_state_moved_on_is_stale(self):
        self.assertEqual(self.verdict(state=DONE), resume.STALE)
        self.assertEqual(self.verdict(state=IN_PROGRESS), resume.STALE)
        self.assertEqual(self.verdict(state=None), resume.STALE)

    def test_past_expiry_is_expired(self):
        self.assertEqual(self.verdict(hours_left=-1), resume.EXPIRED)

    def test_expired_wins_over_ready(self):
        """만료는 **최우선** 분기다 — 조건이 다 맞아도 실행하지 않는다."""
        self.assertEqual(
            resume.evaluate(self.record(hours_left=-0.01), branch="main",
                            head_contains=True, state=AWAITING_HUMAN, now=NOW),
            resume.EXPIRED)

    def test_expired_wins_over_wait(self):
        """만료 판정이 브랜치 게이트 뒤에 있으면 예약이 영원히 안 지워진다."""
        self.assertEqual(self.verdict(hours_left=-1, branch="worker/x"),
                         resume.EXPIRED)

    def test_broken_expiry_is_expired_not_waiting(self):
        for value in ("not-a-date", "", None):
            with self.subTest(value=value):
                rec = {"slug": "s", "after_commit": "", "expires_ts": value}
                self.assertEqual(
                    resume.evaluate(rec, branch="main", head_contains=True,
                                    state=AWAITING_HUMAN, now=NOW),
                    resume.EXPIRED)

    def test_missing_expiry_key_is_expired(self):
        self.assertTrue(resume.is_expired({"slug": "s"}, NOW))

    def test_naive_expiry_is_compared_not_crashed(self):
        """사람이 손으로 쓴 offset 없는 시각 하나가 스윕을 터뜨리지 않는다."""
        rec = {"slug": "s", "after_commit": "",
               "expires_ts": (NOW + dt.timedelta(hours=1)).replace(
                   tzinfo=None).isoformat()}
        self.assertEqual(
            resume.evaluate(rec, branch="main", head_contains=True,
                            state=AWAITING_HUMAN, now=NOW),
            resume.RESUME)

    def test_gate_order_precedence(self):
        """판정 순서 1(만료) > 2(브랜치) > 3(조상) > 4(STALE) > 5(RESUME)를 한 표로 고정.

        개별 게이트 테스트는 각 분기가 존재한다는 것만 보인다. 여기서는 앞 게이트가
        뒤 게이트보다 **먼저** 걸린다는 것을 본다 — 예컨대 브랜치 게이트가 state 검사
        뒤로 밀리면 워커 브랜치 위에서 done 행이 STALE로 폐기된다.
        """
        rows = [
            ("1: expired+ready(failed)", dict(hours_left=-0.01, state=FAILED),
             resume.EXPIRED),
            ("1: expired+ready(awaiting)", dict(hours_left=-0.01), resume.EXPIRED),
        ]
        for branch in ("worker/x", None):
            for state in (DONE, IN_PROGRESS, None):
                rows.append((f"2>4: branch={branch} state={state}",
                             dict(branch=branch, state=state), resume.WAIT))
        for head in (False, None):
            for state in (DONE, IN_PROGRESS):
                rows.append((f"3>4: head_contains={head} state={state}",
                             dict(head_contains=head, state=state), resume.WAIT))
        for state in (DONE, IN_PROGRESS):
            rows.append((f"4: no sha state={state}",
                         dict(after_commit="", head_contains=None, state=state),
                         resume.STALE))
        rows += [
            ("5: awaiting_human", dict(state=AWAITING_HUMAN), resume.RESUME),
            ("5: failed", dict(state=FAILED), resume.RESUME),
        ]
        for label, kwargs, expected in rows:
            with self.subTest(label):
                self.assertEqual(self.verdict(**kwargs), expected, label)


class IsExpiredTest(unittest.TestCase):
    """`is_expired` 직접 호출 — 항상 `NOW` 기반 시각을 넘긴다(실제 시계 금지)."""

    def rec(self, expires_ts):
        return {"slug": "s", "expires_ts": expires_ts}

    def test_future_is_not_expired(self):
        self.assertFalse(resume.is_expired(
            self.rec((NOW + dt.timedelta(hours=1)).isoformat()), NOW))

    def test_past_is_expired(self):
        self.assertTrue(resume.is_expired(
            self.rec((NOW - dt.timedelta(seconds=1)).isoformat()), NOW))

    def test_exact_expiry_instant_is_not_yet_expired(self):
        """비교는 `now > expires`(엄격) — 만료 시각 그 순간은 아직 유효하다."""
        self.assertFalse(resume.is_expired(self.rec(NOW.isoformat()), NOW))

    def test_one_second_after_expiry_is_expired(self):
        self.assertTrue(resume.is_expired(
            self.rec(NOW.isoformat()), NOW + dt.timedelta(seconds=1)))

    def test_missing_or_broken_expiry_counts_as_expired(self):
        cases = [("missing key", {"slug": "s"})]
        for value in (None, "", "   ", "not-a-date", 123):
            cases.append((repr(value), self.rec(value)))
        for label, record in cases:
            with self.subTest(label):
                self.assertTrue(resume.is_expired(record, NOW), label)


class LoadRecordsTest(VaultFixture):
    def test_absent_directory_is_empty_not_an_error(self):
        good, bad = resume.load_records(self.root)
        self.assertEqual((good, bad), ([], []))

    def test_corrupt_record_is_reported_and_kept(self):
        self.seed("ok-one")
        resume.schedule(self.root, "ok-one", now=NOW)
        broken = resume.resume_dir(self.root) / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")

        good, bad = resume.load_records(self.root)

        self.assertEqual([p.name for p, _ in good], ["ok-one.json"])
        self.assertEqual([p.name for p, _ in bad], ["broken.json"])
        self.assertTrue(broken.exists(),
                        "복구할 수 없는 의도를 조용히 삭제하지 않는다")

    def test_json_that_is_not_an_object_counts_as_unreadable(self):
        resume.resume_dir(self.root).mkdir(parents=True)
        (resume.resume_dir(self.root) / "list.json").write_text("[1, 2]",
                                                               encoding="utf-8")
        good, bad = resume.load_records(self.root)
        self.assertEqual(good, [])
        self.assertEqual(len(bad), 1)


class GitSeamTest(VaultFixture):
    def test_current_branch_reads_head(self):
        self.assertEqual(
            resume.current_branch(self.root, _fake_run(branch="main")), "main")

    def test_current_branch_is_none_when_git_fails(self):
        def run(argv, **kwargs):
            return _completed(argv, 128, "", "fatal: not a git repository")

        self.assertIsNone(resume.current_branch(self.root, run))

    def test_current_branch_is_none_when_git_raises(self):
        self.assertIsNone(resume.current_branch(self.root, _fake_run(raises=True)))

    def test_head_contains_true_on_rc_zero(self):
        self.assertIs(resume.head_contains(self.root, "abc",
                                           _fake_run(ancestor_rc=0)), True)

    def test_head_contains_false_only_on_rc_one(self):
        self.assertIs(resume.head_contains(self.root, "abc",
                                           _fake_run(ancestor_rc=1)), False)

    def test_head_contains_is_none_on_any_other_rc(self):
        """rc 128 = 없는 ref. "조상이 아님"이 아니라 "판정 불가"다 — 그래야
        태스크 md가 아직 안 온 sha를 영구 거절이 아니라 대기로 다룬다."""
        self.assertIsNone(resume.head_contains(self.root, "deadbeef",
                                               _fake_run(ancestor_rc=128)))

    def test_head_contains_is_none_when_git_raises(self):
        self.assertIsNone(resume.head_contains(self.root, "abc",
                                               _fake_run(raises=True)))


class ConsumeTest(VaultFixture):
    """The whole sweep: git seam + apply / drop / expire, and never raising."""

    BACKDATE = 1860.0   # what the tick passes: fail-cooldown cap + 60s

    def setUp(self):
        super().setUp()
        self.logs: list[str] = []
        self.alerts: list[tuple[str, str]] = []

    def notify(self, msg, *, event_key):
        self.alerts.append((msg, event_key))

    def consume(self, **kwargs):
        kwargs.setdefault("_run", _fake_run())
        return resume.consume(self.ledger, self.root, backdate_s=self.BACKDATE,
                              now=NOW, notify=self.notify,
                              log=self.logs.append, **kwargs)

    def reserve(self, slug, *, state=AWAITING_HUMAN, after_commit="a1b2c3d",
                hours=24.0, **fields):
        """Seed task + assignment and write a reservation via the real `schedule()`.

        `schedule()` refuses a non-positive `--expire-hours`, so an ALREADY-expired
        record cannot be produced through it — that shape only arises from the
        passage of time. Write the real record, then back-date `expires_ts` in
        place, so these tests still exercise the production writer.
        """
        a = self.seed(slug, state=state, **fields)
        path = resume.schedule(self.root, slug, after_commit=after_commit,
                               expire_hours=max(hours, 1.0), now=NOW)
        if hours <= 0:
            rec = json.loads(path.read_text(encoding="utf-8"))
            rec["expires_ts"] = (NOW + dt.timedelta(hours=hours)).isoformat()
            path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return a

    # --- the happy path ---------------------------------------------------

    def test_resume_applies_when_all_conditions_met(self):
        self.reserve("lowturnover", refail_count=3, blocked_on="upstream PR",
                     timeout_streak=2, timed_out=True, capacity_retry_count=5,
                     job_running=True, checkout_busy=True, updated_ts=NOW.isoformat())

        resumed = self.consume()

        self.assertEqual(resumed, ["lowturnover"])
        a = self.ledger.load("lowturnover")
        # exactly the four fields the acceptance criteria name
        self.assertEqual(a.refail_count, 0)
        self.assertEqual(a.state, FAILED)
        self.assertEqual(a.blocked_on, "")
        self.assertEqual(
            dt.datetime.fromisoformat(a.updated_ts),
            NOW - dt.timedelta(seconds=self.BACKDATE),
            "updated_ts는 dispatch의 fail 쿨다운이 이미 지난 과거여야 한다")
        # everything else is out of scope and must be untouched
        self.assertEqual(a.timeout_streak, 2)
        self.assertTrue(a.timed_out)
        self.assertEqual(a.capacity_retry_count, 5)
        self.assertTrue(a.job_running)
        self.assertTrue(a.checkout_busy)
        # the record is consumed, and the reason is recorded
        self.assertFalse(resume.record_path(self.root, "lowturnover").exists())
        self.assertEqual([h["type"] for h in a.history], ["resumed-scheduled"])
        self.assertIn("a1b2c3d", a.last_note)
        self.assertEqual([key for _, key in self.alerts], ["resume:lowturnover"])

    def test_backdated_updated_ts_is_older_than_the_cooldown_the_tick_will_apply(self):
        """dispatch가 `updated_ts`에 앵커하는 쿨다운(orchestrator.py:357)이 이미
        지나 있어야 같은 틱에서 재디스패치로 이어진다."""
        self.reserve("lowturnover")
        self.consume()
        a = self.ledger.load("lowturnover")
        age = (NOW - dt.datetime.fromisoformat(a.updated_ts)).total_seconds()
        self.assertGreaterEqual(age, self.BACKDATE)

    def test_resume_without_after_commit_does_not_call_git_ancestry(self):
        self.reserve("lowturnover", after_commit="")

        def run(argv, **kwargs):
            if argv[1:3] == ["merge-base", "--is-ancestor"]:
                raise AssertionError("sha 없는 예약이 조상 판정을 불렀다")
            return _completed(argv, 0, "main\n")

        self.assertEqual(self.consume(_run=run), ["lowturnover"])

    def test_resume_saves_once_and_the_saved_row_is_already_consistent(self):
        """순서 계약: 중간 save 가 끼면 반쯤 바뀐 행(refail은 0인데 state는 그대로
        등)이 디스크에 먼저 닿는다. save 는 한 번뿐이고, 그 순간 네 필드가 이미
        최종값이어야 한다."""
        self.reserve("lowturnover", refail_count=3, blocked_on="upstream PR",
                     updated_ts=NOW.isoformat())

        class SpyLedger:
            def __init__(self, inner):
                self.inner = inner
                self.root = inner.root
                self.snapshots = []

            def load(self, slug):
                return self.inner.load(slug)

            def save(self, a):
                # 값을 복사해 둔다 — 객체 참조만 잡으면 save 뒤의 변이가 섞여
                # 부분 save 를 가린다.
                self.snapshots.append(
                    (a.task_id, a.state, a.refail_count, a.blocked_on, a.updated_ts))
                self.inner.save(a)

        spy = SpyLedger(self.ledger)
        resumed = resume.consume(spy, self.root, backdate_s=self.BACKDATE, now=NOW,
                                 notify=self.notify, log=self.logs.append,
                                 _run=_fake_run())

        self.assertEqual(resumed, ["lowturnover"])
        self.assertEqual(len(spy.snapshots), 1, "재개 1건당 save 는 정확히 1회")
        slug, state, refail, blocked, ts = spy.snapshots[0]
        self.assertEqual(slug, "lowturnover")
        self.assertEqual(state, FAILED)
        self.assertEqual(refail, 0)
        self.assertEqual(blocked, "")
        self.assertEqual(dt.datetime.fromisoformat(ts),
                         NOW - dt.timedelta(seconds=self.BACKDATE))

    # --- each condition, one test ----------------------------------------

    def test_branch_not_main_waits_and_keeps_the_record(self):
        a0 = self.reserve("lowturnover", refail_count=2)

        self.assertEqual(self.consume(_run=_fake_run(branch="worker/other")), [])

        self.assertTrue(resume.record_path(self.root, "lowturnover").exists(),
                        "조건 미충족은 예약을 지우지 않는다")
        a = self.ledger.load("lowturnover")
        self.assertEqual((a.state, a.refail_count), (a0.state, 2))

    def test_sha_not_ancestor_waits_and_keeps_the_record(self):
        self.reserve("lowturnover")

        self.assertEqual(self.consume(_run=_fake_run(ancestor_rc=1)), [])

        self.assertTrue(resume.record_path(self.root, "lowturnover").exists())
        self.assertEqual(self.ledger.load("lowturnover").state, AWAITING_HUMAN)

    def test_state_changed_drops_record_without_touching_assignment(self):
        self.reserve("lowturnover", state=DONE, refail_count=4,
                     blocked_on="still here", updated_ts=NOW.isoformat())

        self.assertEqual(self.consume(), [])

        a = self.ledger.load("lowturnover")
        self.assertEqual(a.state, DONE)
        self.assertEqual(a.refail_count, 4)
        self.assertEqual(a.blocked_on, "still here")
        self.assertEqual(a.history, [])
        self.assertFalse(resume.record_path(self.root, "lowturnover").exists(),
                         "해석할 수 없어진 예약은 조용히 남지 않는다")
        self.assertEqual(self.alerts, [], "이미 처리된 건은 사람을 부르지 않는다")

    def test_reservation_whose_ledger_row_vanished_is_dropped(self):
        """행이 사라진 예약(누가 assignment를 지웠다)은 재개 대상이 없으므로 폐기."""
        self.reserve("gone")
        (self.root / "Worker" / "assignments" / "gone.json").unlink()

        self.assertEqual(self.consume(), [])
        self.assertFalse(resume.record_path(self.root, "gone").exists())

    def test_an_unparseable_ledger_row_does_not_resume_and_does_not_raise(self):
        """`Ledger.load`는 깨진 행에서 `None`뿐 아니라 `TypeError`도 낼 수 있다
        (`Assignment(**{})` → 필수 인자 누락). 어느 쪽이든 재개는 일어나지 않는다."""
        self.reserve("corrupt-row")
        (self.root / "Worker" / "assignments" / "corrupt-row.json").write_text(
            "{}", encoding="utf-8")

        self.assertEqual(self.consume(), [])

    def test_expired_record_is_deleted_and_alerts_once(self):
        self.reserve("lowturnover", hours=-1, refail_count=7,
                     blocked_on="untouched", updated_ts=NOW.isoformat())

        self.assertEqual(self.consume(), [])

        a = self.ledger.load("lowturnover")
        self.assertEqual((a.state, a.refail_count, a.blocked_on),
                         (AWAITING_HUMAN, 7, "untouched"))
        self.assertFalse(resume.record_path(self.root, "lowturnover").exists())
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][1], "resume-expired:lowturnover")

    def test_expiry_beats_otherwise_ready_conditions(self):
        """만료된 예약은 조건이 다 맞아도 **실행되지 않는다** — 재개 0건, 경보 1건."""
        self.reserve("lowturnover", hours=-1)

        self.assertEqual(self.consume(), [])

        self.assertEqual(self.ledger.load("lowturnover").state, AWAITING_HUMAN)
        self.assertEqual([key for _, key in self.alerts],
                         ["resume-expired:lowturnover"])

    # --- fail-safe --------------------------------------------------------

    def test_git_failure_is_fail_safe(self):
        self.reserve("lowturnover")

        resumed = self.consume(_run=_fake_run(raises=True))   # must not raise

        self.assertEqual(resumed, [])
        self.assertEqual(self.ledger.load("lowturnover").state, AWAITING_HUMAN)
        self.assertTrue(resume.record_path(self.root, "lowturnover").exists(),
                        "판정 불가는 '아직 아님' — 예약을 남긴다")

    def test_corrupt_json_record_does_not_kill_the_sweep(self):
        self.reserve("good-one")
        broken = resume.resume_dir(self.root) / "aaa-broken.json"
        broken.write_text("{ not json", encoding="utf-8")

        resumed = self.consume()

        self.assertEqual(resumed, ["good-one"],
                         "먼저 정렬된 깨진 레코드가 뒤의 정상 레코드를 굶기지 않는다")
        self.assertTrue(broken.exists(), "복구 불가한 예약은 지우지 않는다")
        self.assertTrue(any("aaa-broken.json" in line for line in self.logs))

    def test_a_ledger_that_raises_does_not_kill_the_sweep(self):
        self.reserve("boom")
        self.reserve("fine")

        class AngryLedger:
            def __init__(self, inner):
                self.inner = inner
                self.root = inner.root

            def load(self, slug):
                if slug == "boom":
                    raise RuntimeError("ledger exploded")
                return self.inner.load(slug)

            def save(self, a):
                self.inner.save(a)

        resumed = resume.consume(AngryLedger(self.ledger), self.root,
                                 backdate_s=self.BACKDATE, now=NOW,
                                 notify=self.notify, log=self.logs.append,
                                 _run=_fake_run())

        self.assertEqual(resumed, ["fine"])
        self.assertTrue(any("ledger exploded" in line for line in self.logs))

    def test_a_failed_save_keeps_the_reservation_and_spares_siblings(self):
        """save 가 실패했는데 예약을 먼저 지웠다면 사람이 건 재개 의도가 조용히
        사라진다. 예약은 save 성공 뒤에만 소비된다. 실패 레코드가 먼저 정렬돼도
        뒤의 형제는 굶지 않는다."""
        self.reserve("aaa-boom")
        self.reserve("zzz-fine")

        class SaveFailsLedger:
            def __init__(self, inner):
                self.inner = inner
                self.root = inner.root

            def load(self, slug):
                return self.inner.load(slug)

            def save(self, a):
                if a.task_id == "aaa-boom":
                    raise OSError("disk full on save")
                self.inner.save(a)

        resumed = resume.consume(SaveFailsLedger(self.ledger), self.root,
                                 backdate_s=self.BACKDATE, now=NOW,
                                 notify=self.notify, log=self.logs.append,
                                 _run=_fake_run())

        self.assertEqual(resumed, ["zzz-fine"])
        self.assertTrue(resume.record_path(self.root, "aaa-boom").exists(),
                        "save 실패한 예약은 디스크에 남아 다음 틱에 재시도된다")
        self.assertTrue(any("disk full on save" in line for line in self.logs))
        self.assertEqual(self.ledger.load("aaa-boom").state, AWAITING_HUMAN)
        self.assertFalse(resume.record_path(self.root, "zzz-fine").exists())
        self.assertEqual(self.ledger.load("zzz-fine").state, FAILED)
        self.assertEqual([key for _, key in self.alerts], ["resume:zzz-fine"])

    def test_a_broken_notifier_never_loses_the_resume(self):
        self.reserve("lowturnover")

        def angry(msg, *, event_key):
            raise RuntimeError("kakao down")

        resumed = resume.consume(self.ledger, self.root, backdate_s=self.BACKDATE,
                                 now=NOW, notify=angry, log=self.logs.append,
                                 _run=_fake_run())

        self.assertEqual(resumed, ["lowturnover"])
        self.assertEqual(self.ledger.load("lowturnover").state, FAILED)
        self.assertTrue(any("알림 실패" in line for line in self.logs))

    def test_no_records_is_a_no_op(self):
        self.assertEqual(self.consume(), [])
        self.assertEqual(self.logs, [])

    def test_repeated_skip_causes_are_capped(self):
        directory = resume.resume_dir(self.root)
        directory.mkdir(parents=True)
        for i in range(6):
            (directory / f"bad{i}.json").write_text("{ nope", encoding="utf-8")

        self.consume()

        caps = [line for line in self.logs if "더 건너뜀" in line]
        self.assertEqual(len(caps), 1, "같은 원인 6건이 로그를 도배하지 않는다")

    def test_wait_logs_why_it_is_still_waiting(self):
        self.reserve("lowturnover")
        self.consume(_run=_fake_run(ancestor_rc=1))
        self.assertTrue(any("아직 아님" in line for line in self.logs))


class OrchestratorHookTest(VaultFixture):
    """`reconcile_resumes()` + its placement in `tick()`.

    The vault skeleton is laid out so `Path(ledger.root).parent.parent` — how the
    wrapper derives the vault root — lands on `self.root`. If that ever drifts the
    sweep would read the REAL vault, so it is asserted rather than assumed.
    """

    def setUp(self):
        super().setUp()
        (self.root / "Worker" / "_state").mkdir(exist_ok=True)
        self.sent: list[tuple[str, str | None]] = []
        self.routed: list[tuple[str, str | None, str | None]] = []

        def route(text, *, level=None, event_key=None, cooldown_s=0.0):
            self.routed.append((text, level, event_key))
            return "send", text

        self.orch = Orchestrator(
            FileMailbox(self.root / "Worker" / "mb" / "orchestrator"),
            Registry([]), self.ledger,
            notify=lambda text, link=None: self.sent.append((text, link)),
            alert_route=route,
        )

    def reserve(self, slug, **fields):
        self.seed(slug, **fields)
        return resume.schedule(self.root, slug, after_commit="a1b2c3d", now=None)

    def test_vault_root_is_the_ledger_grandparent(self):
        self.assertEqual(Path(self.ledger.root).parent.parent, self.root)

    def test_reconcile_resumes_applies_the_reservation(self):
        self.reserve("lowturnover", refail_count=3, blocked_on="upstream")
        with mock.patch.object(resume, "current_branch", return_value="main"), \
             mock.patch.object(resume, "head_contains", return_value=True):
            resumed = self.orch.reconcile_resumes()

        self.assertEqual(resumed, ["lowturnover"])
        a = self.ledger.load("lowturnover")
        self.assertEqual((a.state, a.refail_count, a.blocked_on), (FAILED, 0, ""))

    def test_backdate_clears_the_cooldown_dispatch_will_apply(self):
        """백데이트 폭이 dispatch의 fail 쿨다운보다 커야 같은 틱에서 재디스패치된다."""
        self.reserve("lowturnover")
        with mock.patch.object(resume, "current_branch", return_value="main"), \
             mock.patch.object(resume, "head_contains", return_value=True):
            self.orch.reconcile_resumes()

        a = self.ledger.load("lowturnover")
        age = dt.datetime.now().astimezone().timestamp() - _parse_ts(a.updated_ts)
        self.assertGreater(age, self.orch._fail_cooldown(a.refail_count))
        self.assertGreater(age, self.orch.fail_cooldown_s,
                           "티어가 재조정돼도 안전하도록 상한을 넘겨야 한다")

    def test_resume_alert_is_digest_not_immediate(self):
        """재개는 사람의 즉시 판단을 요구하지 않는다 — 폰을 울리지 않는다."""
        self.reserve("lowturnover")
        with mock.patch.object(resume, "current_branch", return_value="main"), \
             mock.patch.object(resume, "head_contains", return_value=True):
            self.orch.reconcile_resumes()

        self.assertEqual([(lvl, key) for _, lvl, key in self.routed],
                         [(kakao_format.LEVEL_DIGEST, "resume:lowturnover")])

    def test_reconcile_resumes_never_raises(self):
        self.reserve("lowturnover")
        with mock.patch.object(resume, "consume",
                              side_effect=RuntimeError("sweep exploded")):
            self.assertEqual(self.orch.reconcile_resumes(), [])

    def test_tick_reports_resumed_and_runs_it_before_dispatch(self):
        """훅은 dispatch **앞**에 있어야 백데이트된 행이 같은 틱에 나간다."""
        self.reserve("lowturnover")
        order: list[str] = []

        def fake_dispatch(runnable):
            order.append("dispatch")
            return []

        real = self.orch.reconcile_resumes

        def traced():
            order.append("resumes")
            return real()

        stubs = ("collect", "reconcile_merged", "reconcile_closed", "reconcile_queue",
                 "reconcile_parents", "reap_stale")
        with contextlib.ExitStack() as st:
            for name in stubs:
                st.enter_context(mock.patch.object(self.orch, name, return_value=[]))
            for name, value in (("paused", False), ("budget_exceeded", False),
                                ("usage_hold", None)):
                st.enter_context(mock.patch.object(self.orch, name,
                                                  return_value=value))
            st.enter_context(mock.patch.object(self.orch, "reconcile_resumes",
                                              side_effect=traced))
            st.enter_context(mock.patch.object(self.orch,
                                              "_dispatch_serial_per_checkout",
                                              side_effect=fake_dispatch))
            st.enter_context(mock.patch.object(resume, "current_branch",
                                              return_value="main"))
            st.enter_context(mock.patch.object(resume, "head_contains",
                                              return_value=True))
            out = self.orch.tick([])

        self.assertEqual(out["resumed"], ["lowturnover"])
        self.assertEqual(order, ["resumes", "dispatch"])
        self.assertEqual(self.ledger.load("lowturnover").state, FAILED)


class CliResumeTest(VaultFixture):
    """`cmd_resume` — the one-liner that replaces hand-editing assignment json.

    `cli.ROOT` is a module global resolved at call time, so pointing it at the
    tmp vault (the seam `session_end` uses) keeps these away from the real one.
    """

    def run_cmd(self, slug, **kwargs):
        args = argparse.Namespace(slug=slug, after_commit=kwargs.get("after_commit"),
                                  expire_hours=kwargs.get("expire_hours", 24),
                                  note=kwargs.get("note"))
        buf = io.StringIO()
        with mock.patch.object(cli, "ROOT", self.root), \
             contextlib.redirect_stdout(buf):
            rc = cli.cmd_resume(args)
        return rc, buf.getvalue()

    def test_writes_the_reservation_and_returns_zero(self):
        self.seed("lowturnover")
        rc, out = self.run_cmd("lowturnover", after_commit="a1b2c3d")

        self.assertEqual(rc, 0)
        self.assertTrue(resume.record_path(self.root, "lowturnover").exists())
        self.assertIn("a1b2c3d", out)

    def test_rejects_unknown_slug_with_nonzero_exit(self):
        rc, out = self.run_cmd("__no_such_slug__")
        self.assertNotEqual(rc, 0)
        self.assertIn("태스크 파일이 없습니다", out)

    def test_rejects_slug_without_an_assignment_with_nonzero_exit(self):
        self.write_task("never-dispatched")
        rc, out = self.run_cmd("never-dispatched")
        self.assertNotEqual(rc, 0)
        self.assertIn("assignment가 없습니다", out)

    def test_parser_exposes_the_documented_flags(self):
        parsed = cli.build_parser().parse_args(
            ["resume", "lowturnover", "--after-commit", "a1b2c3d",
             "--expire-hours", "6", "--note", "why"])
        self.assertIs(parsed.func, cli.cmd_resume)
        self.assertEqual(parsed.slug, "lowturnover")
        self.assertEqual(parsed.after_commit, "a1b2c3d")
        self.assertEqual(parsed.expire_hours, 6)
        self.assertEqual(parsed.note, "why")

    def test_parser_default_expiry_matches_the_module_constant(self):
        """`--expire-hours` default는 지연 import 때문에 리터럴로 적혀 있다 —
        모듈 상수와 갈라지면 CLI와 스윕이 서로 다른 만료를 말하게 된다."""
        parsed = cli.build_parser().parse_args(["resume", "s"])
        self.assertEqual(parsed.expire_hours, resume.DEFAULT_EXPIRE_HOURS)
        self.assertIsNone(parsed.after_commit)


# --- surviving a sync rebase (real git) -------------------------------------
#
# The vault sync daemon's `pull --rebase origin main` rewrites a local commit to
# a new sha (2026-11-17: the reservation waited on the old sha until it expired).
# These tests run REAL git in a tmp repo, through the `_run` seam — the module's
# `subprocess` is never patched — with global/system config switched off so the
# host's git config (hooks, signing, identity) cannot leak in.

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _real_git_run(argv, **kwargs):
    """The production `subprocess.run`, isolated from the host's git config."""
    return subprocess.run(argv, env=_GIT_ENV, **kwargs)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class RealGitRepoFixture(VaultFixture):
    """`VaultFixture` whose root is also a git repo on `main` with one base commit."""

    SLUG = "lowturnover"

    def setUp(self):
        super().setUp()
        self.logs: list[str] = []
        self.alerts: list[tuple[str, str]] = []
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.name", "macp-test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", os.devnull)
        self.commit("README.md", "base\n", "base")

    def git(self, *args) -> str:
        p = _real_git_run(["git", *args], cwd=self.root, capture_output=True,
                          text=True, timeout=60)
        if p.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {p.stderr}")
        return p.stdout.strip()

    def commit(self, rel: str, text: str, msg: str) -> str:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.git("add", "--", rel)
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    @property
    def md_rel(self) -> str:
        return f"Todo/items/{self.SLUG}.md"

    def task_commit(self) -> tuple[str, str]:
        """Seed task + assignment; commit the md (base), then edit it (X). Returns (base, X)."""
        self.seed(self.SLUG)
        text = (self.root / self.md_rel).read_text(encoding="utf-8")
        base = self.commit(self.md_rel, text, "add task")
        x = self.commit(self.md_rel, text + "\nlong_job: collect\n", "attach long_job")
        return base, x

    def rebase_onto_upstream(self, base: str) -> str:
        """What `pull --rebase` does: an upstream commit lands under the local X.
        Returns the rewritten HEAD sha (X')."""
        self.git("checkout", "-q", "-b", "upstream", base)
        self.commit("Notes/other.md", "from the other host\n", "upstream change")
        self.git("checkout", "-q", "main")
        self.git("rebase", "-q", "upstream")
        return self.git("rev-parse", "HEAD")

    def reserve(self, sha: str) -> Path:
        return resume.schedule(self.root, self.SLUG, after_commit=sha, now=NOW,
                               _run=_real_git_run)

    def record(self) -> dict:
        return json.loads(resume.record_path(self.root, self.SLUG)
                          .read_text(encoding="utf-8"))

    def notify(self, msg, *, event_key):
        self.alerts.append((msg, event_key))

    def consume(self, **kwargs):
        kwargs.setdefault("_run", _real_git_run)
        return resume.consume(self.ledger, self.root, backdate_s=1860.0, now=NOW,
                              notify=self.notify, log=self.logs.append, **kwargs)


class ScheduleSignatureTest(RealGitRepoFixture):
    def test_schedule_records_patch_id_in_real_repo(self):
        _, x = self.task_commit()
        self.reserve(x[:9])
        rec = self.record()
        self.assertEqual(rec["after_commit"], x[:9], "사람이 넘긴 sha는 그대로 둔다")
        self.assertRegex(rec["patch_id"], r"^[0-9a-f]{40}$")
        self.assertTrue(dt.datetime.fromisoformat(rec["commit_ts"]).tzinfo)
        self.assertEqual(rec["paths"], [self.md_rel])

    def test_merge_commit_gets_no_patch_id(self):
        base, _ = self.task_commit()
        self.git("checkout", "-q", "-b", "side", base)
        self.commit("Notes/side.md", "side\n", "side")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.reserve(self.git("rev-parse", "HEAD"))
        self.assertNotIn("patch_id", self.record(), "머지 커밋은 옛 동작(엄격한 조상 검사)")


class ScheduleSignatureFailSafeTest(VaultFixture):
    def test_schedule_outside_repo_omits_patch_id(self):
        self.seed("lowturnover")
        path = resume.schedule(self.root, "lowturnover", after_commit="a1b2c3d",
                               now=NOW, _run=_real_git_run)
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rec["after_commit"], "a1b2c3d")
        self.assertNotIn("patch_id", rec, "git 저장소가 아니면 서명 없이 예약만 저장")

    def test_schedule_survives_git_raising(self):
        self.seed("lowturnover")
        path = resume.schedule(self.root, "lowturnover", after_commit="a1b2c3d",
                               now=NOW, _run=_fake_run(raises=True))
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rec["after_commit"], "a1b2c3d")
        self.assertNotIn("patch_id", rec)

    def test_schedule_without_after_commit_calls_no_git(self):
        self.seed("lowturnover")

        def run(argv, **kwargs):
            raise AssertionError(f"unexpected git call: {argv}")

        path = resume.schedule(self.root, "lowturnover", now=NOW, _run=run)
        self.assertNotIn("patch_id", json.loads(path.read_text(encoding="utf-8")))


class RebasedCommitTest(RealGitRepoFixture):
    def test_rebased_commit_satisfies_reservation(self):
        base, x = self.task_commit()
        self.reserve(x)
        x2 = self.rebase_onto_upstream(base)
        self.assertNotEqual(x, x2, "rebase가 sha를 바꿨다 — 사건 재현")
        self.assertIs(resume.head_contains(self.root, x, _real_git_run), False)
        self.assertEqual(resume.find_rewritten(self.root, self.record(), _real_git_run),
                         (True, x2))

        self.assertEqual(self.consume(), [self.SLUG])

        a = self.ledger.load(self.SLUG)
        self.assertEqual((a.state, a.refail_count, a.blocked_on), (FAILED, 0, ""))
        self.assertEqual(a.history[-1]["after_commit"], x)
        self.assertEqual(a.history[-1]["rebased_to"], x2)
        self.assertIn(x, a.last_note)
        self.assertFalse(resume.record_path(self.root, self.SLUG).exists())
        self.assertTrue(any("rebased:" in line and x in line and x2 in line
                            for line in self.logs), self.logs)

    def test_abbreviated_sha_is_rebased_too(self):
        base, x = self.task_commit()
        self.reserve(x[:9])
        x2 = self.rebase_onto_upstream(base)
        self.assertEqual(self.consume(), [self.SLUG])
        self.assertEqual(self.ledger.load(self.SLUG).history[-1]["rebased_to"], x2)

    def test_stale_sha_given_after_rebase_is_found(self):
        """사람이 rebase 뒤에 옛 sha로 예약해도 원 커밋 시각부터 찾으므로 충족된다."""
        base, x = self.task_commit()
        x2 = self.rebase_onto_upstream(base)
        self.reserve(x)
        self.assertIn("patch_id", self.record())
        self.assertEqual(self.consume(), [self.SLUG])
        self.assertEqual(self.ledger.load(self.SLUG).history[-1]["rebased_to"], x2)

    def test_plain_ancestor_still_resumes_without_rebased_note(self):
        _, x = self.task_commit()
        self.reserve(x)
        self.assertEqual(self.consume(), [self.SLUG])
        self.assertNotIn("rebased_to", self.ledger.load(self.SLUG).history[-1])
        self.assertFalse(any("rebased:" in line for line in self.logs))

    def test_different_patch_is_not_satisfied(self):
        base, x = self.task_commit()
        self.reserve(x)
        # main moves on WITHOUT X: an upstream commit, then a different edit to the same md
        self.git("checkout", "-q", "-B", "main", base)
        self.commit("Notes/other.md", "from the other host\n", "upstream change")
        text = (self.root / self.md_rel).read_text(encoding="utf-8")
        self.commit(self.md_rel, text + "\nlong_job: something-else\n", "other edit")

        self.assertIs(resume.head_contains(self.root, x, _real_git_run), False)
        self.assertEqual(resume.find_rewritten(self.root, self.record(), _real_git_run),
                         (False, ""))
        self.assertEqual(self.consume(), [])
        self.assertTrue(resume.record_path(self.root, self.SLUG).exists())
        self.assertEqual(self.ledger.load(self.SLUG).state, AWAITING_HUMAN)
        self.assertTrue(any("아직 아님" in line for line in self.logs))
        self.assertFalse(any("rebased:" in line for line in self.logs))

    def test_old_record_without_patch_id_keeps_waiting(self):
        base, x = self.task_commit()
        path = self.reserve(x)
        rec = self.record()
        for key in ("patch_id", "commit_ts", "paths"):
            rec.pop(key, None)
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        self.rebase_onto_upstream(base)

        self.assertEqual(resume.find_rewritten(self.root, rec, _real_git_run),
                         (None, ""))
        self.assertEqual(self.consume(), [], "patch_id 없는 옛 레코드는 옛 동작 그대로")
        self.assertTrue(path.exists())
        self.assertTrue(any("아직 아님" in line for line in self.logs))

    def test_expired_rebased_reservation_still_expires(self):
        base, x = self.task_commit()
        path = self.reserve(x)
        self.rebase_onto_upstream(base)
        rec = self.record()
        rec["expires_ts"] = (NOW - dt.timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

        self.assertEqual(self.consume(), [])
        self.assertFalse(path.exists(), "만료가 rebase 폴백보다 먼저다")
        self.assertEqual(self.ledger.load(self.SLUG).state, AWAITING_HUMAN)
        self.assertEqual([key for _, key in self.alerts], [f"resume-expired:{self.SLUG}"])
        self.assertFalse(any("rebased:" in line for line in self.logs))

    def test_rebased_on_a_worker_branch_still_waits(self):
        base, x = self.task_commit()
        self.reserve(x)
        self.rebase_onto_upstream(base)
        self.git("checkout", "-q", "-b", "worker/other")
        self.assertEqual(self.consume(), [])
        self.assertFalse(any("rebased:" in line for line in self.logs),
                         "main이 아니면 폴백 검색도 하지 않는다")


class FindRewrittenFailSafeTest(VaultFixture):
    RECORD = {"slug": "lowturnover", "after_commit": "a1b2c3d",
              "patch_id": "f" * 40, "commit_ts": NOW.isoformat(),
              "created_ts": NOW.isoformat(), "paths": ["Todo/items/lowturnover.md"]}

    def test_git_raising_is_none(self):
        self.assertEqual(resume.find_rewritten(self.root, dict(self.RECORD),
                                               _fake_run(raises=True)), (None, ""))

    def test_log_rc_128_is_none(self):
        def run(argv, **kwargs):
            return _completed(argv, 128, "", "fatal: bad revision")

        self.assertEqual(resume.find_rewritten(self.root, dict(self.RECORD), run),
                         (None, ""))

    def test_unparseable_dates_are_none(self):
        rec = dict(self.RECORD, commit_ts="nope", created_ts="")

        def run(argv, **kwargs):
            raise AssertionError(f"unexpected git call: {argv}")

        self.assertEqual(resume.find_rewritten(self.root, rec, run), (None, ""))

    def test_git_error_during_fallback_waits_without_skip(self):
        self.seed("lowturnover")
        path = resume.schedule(self.root, "lowturnover", after_commit="a1b2c3d", now=NOW)
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec.update(patch_id="f" * 40, commit_ts=NOW.isoformat())
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        calls: list[list[str]] = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1:3] == ["rev-parse", "--abbrev-ref"]:
                return _completed(argv, 0, "main\n")
            if argv[1:3] == ["merge-base", "--is-ancestor"]:
                return _completed(argv, 1)
            raise OSError("git log blew up")

        logs: list[str] = []
        resumed = resume.consume(self.ledger, self.root, backdate_s=1860.0, now=NOW,
                                 log=logs.append, _run=run)

        self.assertEqual(resumed, [])
        self.assertTrue(any(argv[1] == "log" for argv in calls), "폴백이 실제로 시도됐다")
        self.assertTrue(path.exists())
        self.assertTrue(any("아직 아님" in line for line in logs))
        self.assertFalse(any("건너뜀" in line for line in logs), logs)
        self.assertEqual(self.ledger.load("lowturnover").state, AWAITING_HUMAN)


if __name__ == "__main__":
    unittest.main()
