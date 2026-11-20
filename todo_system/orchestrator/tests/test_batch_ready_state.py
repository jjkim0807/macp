"""Batch verify in the orchestrator — the `batch_ready` ledger state.

A batch-verified run's RESULT (test_runner_batch.py) says one of two things, and
the orchestrator must act on both without a person:

* `batch_ready` → the row waits as BATCH_READY for the repo's integration gate.
  Nobody is alerted, and nothing re-dispatches, reaps or re-nudges it.
* `batch_return` → the row goes back to the queue for an individual full verify
  as a DEFERRAL: no refail, no poison, and `verify_full` on the next payload so
  it is never batched again.

The safety-net sweep (`reconcile_batch_gate`) makes sure a waiting row never
waits forever: a closed task is tidied, a repo that lost its gate and a row older
than a day go back to a full verify.

Everything runs against a tmp vault layout (`<tmp>/Worker/assignments`,
`<tmp>/Worker/agents`, `<tmp>/.macp/batch_gate.json`), so the sweep's
"vault root = two up from the ledger" resolves inside tmp_path. Closing a task
is mocked — the real close writes Todo/items.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from todo_system import kakao_format
from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import batch_gate
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    ALERT_BATCH_GATE_FAILED,
    ALERT_REVIEW_PR,
    FAILURE_ALERT_KINDS,
    Orchestrator,
    Task,
    _parse_ts,
)
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.runner import ExecOutcome, Runner

REPO_URL = "https://example.test/acme/batchrepo.git"
PR_URL = "https://example.test/acme/batchrepo/pull/7"
SLUG = "batch-task"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
GATE = {"full_gate": [["./gradlew", ":app:testDebugUnitTest"]]}
CONFIG = {"enabled": True, "repos": {"batchrepo": GATE}}
EMPTY_SWEEP = {"started": [], "merged": [], "returned": [], "closed": []}
LONG_AGO = "2020-01-01T00:00:00+00:00"


def _base_payload(**extra) -> dict:
    # `heavy: False` pins the routing: no stat of a real checkout decides it.
    return {"summary": "a light change", "repo": REPO_URL, "worker_mode": "auto",
            "branch": f"worker/{SLUG}", "task_path": "", "heavy": False, **extra}


def _ready_result(**overrides) -> dict:
    """The RESULT payload a held batch run sends (runner._execute's `extra`)."""
    result = {
        "summary": "staged pipeline complete (5 stages)",
        "branch": f"worker/{SLUG}",
        "run_dir": "/somewhere/Worker/runs/20261118T100000-batch-task",
        "stages": list(stages.STAGES_AUTO),
        "review_required": True,
        "repo": REPO_URL,
        "pr_url": PR_URL,
        batch_gate.RESULT_READY: True,
        batch_gate.KEY_BATCH: {"head_sha": HEAD_SHA},
    }
    result.update(overrides)
    return result


def _return_result(reason: str = batch_gate.RETURN_NO_DEVICE, **overrides) -> dict:
    result = {
        "summary": "staged pipeline complete (5 stages)",
        "branch": f"worker/{SLUG}",
        "review_required": True,
        "repo": REPO_URL,
        "pr_url": PR_URL,
        batch_gate.RESULT_RETURN: reason,
    }
    result.update(overrides)
    return result


class _World:
    def __init__(self, root):
        self.root = root                                  # the vault stand-in
        self.ledger = Ledger(root / "Worker" / "assignments")
        self.mb = FileMailbox(root / "Worker" / "agents")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["batch-*"],
                                   capabilities=[], repo="~/x")])
        self.notified: list[tuple] = []
        self.routed: list[dict] = []

        def notify(text, link=None):
            self.notified.append((text, link))

        def route(text, *, level=None, event_key=None, cooldown_s=0.0):
            self.routed.append({"text": text, "level": level, "event_key": event_key})
            return "digest", None                         # never reaches `notify`

        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=notify, alert_route=route)

    # -- setup -------------------------------------------------------------
    def write_config(self, config: dict = CONFIG) -> None:
        path = self.root / batch_gate.CONFIG_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config), encoding="utf-8")

    def dispatch(self, slug: str = SLUG, **payload_extra) -> Assignment:
        a = self.orch.dispatch(Task(slug, _base_payload(**payload_extra)))
        assert a is not None and a.state == A.DISPATCHED
        return a

    def reply(self, a: Assignment, type_: str = m.RESULT, **payload) -> None:
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, frm=a.agent_id, type=type_,
                                task_id=a.task_id, corr_id=a.corr_id, payload=payload))

    def collect(self):
        """collect() with the two side doors closed: the task close (it writes
        the real Todo/items) and the in-place retry. Returns both mocks."""
        with mock.patch.object(self.orch, "_close_with_evidence") as close, \
                mock.patch.object(self.orch, "_retry") as retry:
            self.orch.collect()
        return close, retry

    def ready_row(self, slug: str = SLUG, *, ready_ts: str | None = None,
                  **payload_extra) -> Assignment:
        """A BATCH_READY row, saved — built with the same transition collect uses."""
        a = Assignment(task_id=slug, agent_id="w", corr_id=f"c-{slug}", kind="ai-local",
                       state=A.DONE, payload=_base_payload(**payload_extra))
        stamp = ready_ts or m.now_iso()
        assert batch_gate.mark_ready(a, _ready_result(), now_iso=stamp)
        self.ledger.save(a)
        return a

    def row(self, slug: str = SLUG) -> Assignment:
        return self.ledger.load(slug)

    def inbox(self) -> list:
        return [env for env in self.mb.receive("w") if env.type == m.TASK_ASSIGN]


@pytest.fixture
def world(tmp_path) -> _World:
    return _World(tmp_path)


# --- RESULT → batch_ready ------------------------------------------------------

def test_batch_ready_result_parks_the_row_and_alerts_nobody(world, capsys):
    a = world.dispatch()
    world.reply(a, **_ready_result())
    close, retry = world.collect()

    row = world.row()
    assert row.state == A.BATCH_READY
    assert row.payload[batch_gate.KEY_BATCH] == {
        "pr_url": PR_URL, "head_sha": HEAD_SHA, "branch": f"worker/{SLUG}",
        "ready_ts": row.updated_ts, "run": "20261118T100000-batch-task"}
    assert [h["type"] for h in row.history[-2:]] == [m.RESULT, batch_gate.HISTORY_READY]
    assert row.timeout_streak == 0
    assert row.last_alert_kind == ""
    # nobody is paged, the task is not closed, nothing is retried
    assert world.routed == []
    assert world.notified == []
    close.assert_not_called()
    retry.assert_not_called()
    assert f"{SLUG}: batch_ready" in capsys.readouterr().out
    # the reply was consumed
    assert world.mb.receive(m.ORCHESTRATOR) == []


def test_batch_ready_row_keeps_no_host_path(world):
    a = world.dispatch()
    world.reply(a, **_ready_result())
    world.collect()
    assert "/somewhere" not in world.row().to_json()


@pytest.mark.parametrize("drop", ["pr_url", "head_sha"])
def test_incomplete_batch_ready_falls_back_to_the_review_park(world, drop):
    # The runner also sets review_required, so a RESULT the orchestrator cannot
    # honour parks the PR for a human — it never closes the task unmerged.
    a = world.dispatch()
    result = _ready_result()
    if drop == "head_sha":
        result[batch_gate.KEY_BATCH] = {}
    else:
        del result[drop]
    world.reply(a, **result)
    close, _retry = world.collect()

    row = world.row()
    assert row.state == A.AWAITING_HUMAN
    assert batch_gate.KEY_BATCH not in row.payload
    assert batch_gate.HISTORY_READY not in [h["type"] for h in row.history]
    assert len(world.routed) == 1                       # the ordinary review alert
    close.assert_not_called()


def test_review_required_result_without_batch_keys_is_unchanged(world):
    a = world.dispatch()
    world.reply(a, review_required=True, pr_url=PR_URL, repo=REPO_URL)
    close, _retry = world.collect()

    row = world.row()
    assert row.state == A.AWAITING_HUMAN
    assert row.last_alert_kind == ALERT_REVIEW_PR
    assert [r["event_key"] for r in world.routed] == [f"review:{ALERT_REVIEW_PR}:{SLUG}"]
    assert batch_gate.KEY_BATCH not in row.payload
    close.assert_not_called()


def test_plain_result_still_closes_the_task(world):
    a = world.dispatch()
    world.reply(a, review_required=False, repo=REPO_URL)
    close, _retry = world.collect()

    assert world.row().state == A.DONE
    close.assert_called_once()
    assert world.routed == []


# --- RESULT → back to an individual full verify -----------------------------------

def test_batch_return_result_goes_back_as_a_deferral(world, capsys):
    a = world.dispatch()
    world.reply(a, **_return_result(batch_gate.RETURN_NO_DEVICE))
    close, retry = world.collect()

    row = world.row()
    assert row.state == A.FAILED
    assert row.history[-1]["type"] == A.BATCH_RETURNED
    assert row.history[-1]["reason"] == "no-device-evidence"
    assert row.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert row.payload[batch_gate.KEY_RETURN_REASON] == "no-device-evidence"
    assert batch_gate.KEY_BATCH not in row.payload
    assert row.refail_count == 0
    assert A.is_batch_return_pending(row)
    assert row.display_state == "deferred"               # every view: waiting, not failed
    # not a failure: no alert, no in-place retry, no close
    assert world.routed == []
    assert world.notified == []
    retry.assert_not_called()
    close.assert_not_called()
    assert f"{SLUG}: batch return (no-device-evidence)" in capsys.readouterr().out


def test_batch_keys_on_a_fail_reply_are_ignored(world):
    # A report that declared the run failed still carries the keys the runner
    # set, but a FAIL is a FAIL: the ordinary failure handling runs.
    a = world.dispatch()
    world.reply(a, m.FAIL, reason="report declared the run failed", **_ready_result())
    _close, retry = world.collect()

    row = world.row()
    assert row.state == A.FAILED
    assert batch_gate.KEY_BATCH not in row.payload
    assert not A.is_batch_return_pending(row)
    assert not row.is_deferred
    retry.assert_called_once()                           # the bounded retry, as always


def test_batch_return_on_a_fail_reply_is_ignored(world):
    a = world.dispatch()
    world.reply(a, m.FAIL, reason="verify FAIL — not merged", **_return_result())
    _close, retry = world.collect()

    row = world.row()
    assert row.state == A.FAILED
    assert batch_gate.KEY_VERIFY_FULL not in row.payload
    assert not row.is_deferred
    retry.assert_called_once()


def test_a_fault_in_the_transition_falls_back_to_the_review_park(world, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("ledger row is odd")

    monkeypatch.setattr(batch_gate, "mark_ready", boom)
    a = world.dispatch()
    world.reply(a, **_ready_result())
    world.collect()

    assert world.row().state == A.AWAITING_HUMAN        # parked, not lost and not closed
    assert len(world.routed) == 1
    assert "batch result not applied (RuntimeError)" in capsys.readouterr().out


# --- the returned row is re-dispatched without a penalty ---------------------------

def _returned(world, reason: str = batch_gate.RETURN_NO_DEVICE) -> Assignment:
    a = world.dispatch()
    world.reply(a, **_return_result(reason))
    world.collect()
    return world.row()


def test_returned_row_waits_the_poll_cooldown(world):
    row = _returned(world)
    returned_at = _parse_ts(row.updated_ts)
    assert world.orch.dispatch(Task(SLUG, _base_payload()), now=returned_at + 1) is None
    assert world.orch.dispatch(
        Task(SLUG, _base_payload()),
        now=returned_at + world.orch.job_poll_cooldown_s - 1) is None
    assert world.row().state == A.FAILED                # untouched while it waits
    assert len(world.inbox()) == 1                       # only the first dispatch
    gate = world.orch._ledger_gate(world.row(), returned_at + 1)
    assert (gate.kind, gate.deferred) == ("poll", True)


def test_returned_row_redispatches_with_verify_full_and_no_refail(world):
    row = _returned(world)
    # Sitting AT the poison threshold: an ordinary failed row would be
    # quarantined on the next dispatch (see the control test below).
    row.refail_count = world.orch.poison_threshold
    world.ledger.save(row)
    task = Task(SLUG, _base_payload())
    payload_before = dict(task.payload)
    due = _parse_ts(row.updated_ts) + world.orch.job_poll_cooldown_s + 1

    new = world.orch.dispatch(task, now=due)

    assert new is not None
    assert new.state == A.DISPATCHED                     # out again, not quarantined
    assert new.refail_count == world.orch.poison_threshold      # carried, NOT +1
    assert new.corr_id != row.corr_id
    # the marks reach the new ledger row...
    assert new.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert new.payload[batch_gate.KEY_RETURN_REASON] == "no-device-evidence"
    saved = world.row()
    assert saved.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert not saved.is_deferred                         # a live run now, not a wait
    # ...and the envelope the runner will execute
    envelope = world.inbox()[-1]
    assert envelope.corr_id == new.corr_id
    assert envelope.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert envelope.payload[batch_gate.KEY_RETURN_REASON] == "no-device-evidence"
    # the caller's Task object is left exactly as it was
    assert task.payload == payload_before
    assert batch_gate.KEY_VERIFY_FULL not in task.payload
    # nobody was paged about a "repeatedly failing" task
    assert world.routed == []
    # and that payload is never batched again
    assert batch_gate.eligible(new.payload, tier="standard", gate=GATE,
                               five_stage=True, auto_merge=True) == (
        False, "returned to full verify")
    assert batch_gate.eligible(saved.payload, tier="light", gate=GATE,
                               five_stage=True, auto_merge=True)[0] is False


def test_control_an_ordinary_failed_row_at_the_threshold_is_quarantined(world):
    # The same row WITHOUT the batch-returned mark: the test above is only
    # evidence if this one really is refused.
    a = Assignment(task_id=SLUG, agent_id="w", corr_id="c", kind="ai-local",
                   state=A.FAILED, refail_count=world.orch.poison_threshold,
                   updated_ts=LONG_AGO, payload=_base_payload())
    world.ledger.save(a)
    assert world.orch.dispatch(Task(SLUG, _base_payload())) is None
    assert world.row().state == A.AWAITING_HUMAN
    assert world.inbox() == []
    assert len(world.routed) == 1                        # the quarantine alert


def test_verify_full_survives_a_later_failure_and_redispatch(world):
    # Once returned, the task stays out of the batch until it is done — through
    # an ordinary failure of its full-verify run too.
    row = _returned(world)
    due = _parse_ts(row.updated_ts) + world.orch.job_poll_cooldown_s + 1
    second = world.orch.dispatch(Task(SLUG, _base_payload()), now=due)
    world.reply(second, m.FAIL, reason="verify FAIL — not merged")
    world.collect()                                      # _retry is mocked: stays FAILED
    failed = world.row()
    assert failed.state == A.FAILED and not failed.is_deferred
    assert failed.payload[batch_gate.KEY_VERIFY_FULL] is True

    third = world.orch.dispatch(Task(SLUG, _base_payload()),
                                now=_parse_ts(failed.updated_ts) + 10 * 3600)
    assert third is not None
    assert third.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert third.payload[batch_gate.KEY_RETURN_REASON] == "no-device-evidence"
    assert third.refail_count == failed.refail_count + 1          # a real failure counts


def test_dispatch_without_a_previous_row_carries_nothing(world):
    a = world.dispatch()
    for key in batch_gate.CARRY_KEYS:
        assert key not in a.payload
        assert key not in world.inbox()[-1].payload


def test_batch_carry_reads_only_the_carry_keys():
    row = Assignment(task_id=SLUG, agent_id="w", corr_id="c",
                     payload={"verify_full": True, "batch_return_reason": "expired",
                              "batch": {"pr_url": PR_URL}, "summary": "s"})
    assert Orchestrator._batch_carry(row) == {"verify_full": True,
                                              "batch_return_reason": "expired"}
    assert Orchestrator._batch_carry(None) == {}
    assert Orchestrator._batch_carry(Assignment(task_id=SLUG, agent_id="w",
                                                corr_id="c")) == {}
    odd = Assignment(task_id=SLUG, agent_id="w", corr_id="c")
    odd.payload = None
    assert Orchestrator._batch_carry(odd) == {}


# --- a batch_ready row is left alone ----------------------------------------------

def test_batch_ready_row_is_not_dispatched_listed_reaped_or_renudged(world):
    world.ready_row(ready_ts=LONG_AGO)
    row = world.row()
    row.created_ts = LONG_AGO
    world.ledger.save(row)
    task = Task(SLUG, _base_payload())

    assert world.orch._ledger_gate(world.row()).kind == "busy"
    assert world.orch.dispatch(task) is None
    assert world.inbox() == []
    assert world.orch.queue_snapshot([task]) == []
    assert world.orch.reap_stale() == []                 # quiet for years, not in flight
    assert world.orch.renudge_due() == []                # nobody owes an answer
    assert world.orch._checkout_owners() == {}           # holds no checkout
    assert world.orch._inflight_count("w") == 0
    assert world.row().state == A.BATCH_READY
    assert world.routed == []


# --- the safety-net sweep ----------------------------------------------------------

def test_sweep_with_no_waiting_row_reads_no_config_and_prints_nothing(
        world, monkeypatch, capsys):
    for slug, state in (("batch-done", A.DONE), ("batch-failed", A.FAILED),
                        ("batch-parked", A.AWAITING_HUMAN), ("batch-run", A.IN_PROGRESS)):
        world.ledger.save(Assignment(task_id=slug, agent_id="w", corr_id="c",
                                     kind="ai-local", state=state,
                                     payload=_base_payload()))
    load_config = mock.Mock(return_value=CONFIG)
    monkeypatch.setattr(batch_gate, "load_config", load_config)
    capsys.readouterr()

    assert world.orch.reconcile_batch_gate() == EMPTY_SWEEP
    load_config.assert_not_called()
    assert capsys.readouterr().out == ""
    assert world.routed == []


def test_sweep_on_an_empty_ledger_is_silent(world, capsys):
    assert world.orch.reconcile_batch_gate() == EMPTY_SWEEP
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("config", [
    None,                                                          # no config file
    {**CONFIG, "enabled": False},                                  # switched off
    {"enabled": True, "repos": {"otherrepo": GATE}},               # repo removed
    {"enabled": True, "repos": {"batchrepo": {"full_gate": []}}},  # gate emptied
])
def test_sweep_returns_a_row_whose_repo_has_no_gate(world, config, capsys):
    if config is not None:
        world.write_config(config)
    world.ready_row()

    result = world.orch.reconcile_batch_gate()

    assert result == {**EMPTY_SWEEP, "returned": [SLUG]}
    row = world.row()
    assert row.state == A.FAILED
    assert row.payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_NOT_ENABLED
    assert row.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert batch_gate.KEY_BATCH not in row.payload
    assert row.display_state == "deferred"
    assert world.routed == []                            # switching it off pages nobody
    assert f"batch gate: {SLUG} returned" in capsys.readouterr().out
    # idempotent: the row is no longer batch_ready
    assert world.orch.reconcile_batch_gate() == EMPTY_SWEEP


def test_sweep_keeps_a_single_row_until_its_wait_is_over(world, capsys):
    # One task alone waits MAX_WAIT_S for company. Until then the sweep leaves
    # no trace of it anywhere: no write, no output, no job state.
    world.write_config()
    a = world.ready_row()
    ready_at = _parse_ts(a.payload[batch_gate.KEY_BATCH]["ready_ts"])

    assert world.orch.reconcile_batch_gate(now=ready_at + 60) == EMPTY_SWEEP
    assert world.orch.reconcile_batch_gate(
        now=ready_at + batch_gate.MAX_WAIT_S - 1) == EMPTY_SWEEP
    row = world.row()
    assert row.state == A.BATCH_READY
    assert row.to_json() == a.to_json()                  # not rewritten while it waits
    assert world.routed == []
    assert capsys.readouterr().out == ""
    assert not batch_gate.state_dir(world.root).exists()  # no job record, no lock


def test_sweep_tries_the_gate_once_a_single_row_has_waited(world, monkeypatch, tmp_path):
    # Past MAX_WAIT_S the integration gate takes over (test_batch_gate_job.py).
    # This host has no shared clone of the repo, so all the gate can do is send
    # the task back — and it finds that out before asking gh about any PR.
    world.write_config()
    a = world.ready_row()
    ready_at = _parse_ts(a.payload[batch_gate.KEY_BATCH]["ready_ts"])
    monkeypatch.setattr(repos, "local_path",
                        lambda url, worker_root=None: tmp_path / "no-such-clone")
    monkeypatch.setattr(stages, "gh_pr_state",
                        lambda *_a, **_k: pytest.fail("gh was asked about a PR"))

    result = world.orch.reconcile_batch_gate(now=ready_at + batch_gate.MAX_WAIT_S + 1)

    assert result == {**EMPTY_SWEEP, "returned": [SLUG]}
    row = world.row()
    assert row.payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_NO_CHECKOUT
    assert row.display_state == "deferred"
    assert world.routed == []                            # nobody is paged for it
    assert batch_gate.load_job(world.root, "batchrepo") is None


def test_sweep_expires_an_old_row_with_one_digest_notice(world):
    world.write_config()
    a = world.ready_row()
    ready_at = _parse_ts(a.payload[batch_gate.KEY_BATCH]["ready_ts"])
    late = ready_at + batch_gate.EXPIRE_S + 1

    result = world.orch.reconcile_batch_gate(now=late)

    assert result == {**EMPTY_SWEEP, "returned": [SLUG]}
    row = world.row()
    assert row.state == A.FAILED
    assert row.payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_EXPIRED
    assert row.display_state == "deferred"
    [notice] = world.routed
    assert notice["event_key"] == f"batch-expired:{SLUG}"
    assert notice["level"] == kakao_format.LEVEL_DIGEST
    assert "24" in notice["text"]                        # EXPIRE_S, in hours
    assert world.notified == []                          # digest: no immediate send
    # a second pass finds nothing left to expire and sends nothing more
    assert world.orch.reconcile_batch_gate(now=late + 600) == EMPTY_SWEEP
    assert len(world.routed) == 1


@pytest.mark.parametrize("record", [None, {}, {"ready_ts": "not a time"}, "odd"])
def test_sweep_expires_a_row_whose_ready_time_cannot_be_read(world, record):
    # A row the sweep cannot age must not sit there forever.
    world.write_config()
    a = world.ready_row()
    if record is None:
        del a.payload[batch_gate.KEY_BATCH]
    else:
        a.payload[batch_gate.KEY_BATCH] = record
    world.ledger.save(a)

    assert world.orch.reconcile_batch_gate()["returned"] == [SLUG]
    assert world.row().payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_EXPIRED


def test_sweep_tidies_a_row_whose_task_is_already_closed(world):
    # No config on purpose: a closed task is tidied, not sent back for a verify
    # nobody wants — the closed check comes first.
    md = world.root / "Todo" / "items" / f"{SLUG}.md"
    md.parent.mkdir(parents=True)
    md.write_text("---\nstatus: done\nworker_mode: auto\n---\n# t\n", encoding="utf-8")
    world.ready_row(task_path=f"Todo/items/{SLUG}.md")

    with mock.patch.object(world.orch, "_close_with_evidence") as close:
        result = world.orch.reconcile_batch_gate()

    assert result == {**EMPTY_SWEEP, "closed": [SLUG]}
    close.assert_called_once()
    row = world.row()
    assert row.state == A.DONE
    assert row.history[-1]["type"] == "reconciled-ledger-tidy"
    assert batch_gate.KEY_VERIFY_FULL not in row.payload
    assert world.routed == []


def test_sweep_leaves_a_row_whose_task_is_open(world):
    md = world.root / "Todo" / "items" / f"{SLUG}.md"
    md.parent.mkdir(parents=True)
    md.write_text("---\nstatus: open\nworker_mode: auto\n---\n# t\n", encoding="utf-8")
    world.write_config()
    world.ready_row(task_path=f"Todo/items/{SLUG}.md")

    with mock.patch.object(world.orch, "_close_with_evidence") as close:
        assert world.orch.reconcile_batch_gate() == EMPTY_SWEEP
    close.assert_not_called()
    assert world.row().state == A.BATCH_READY


def test_sweep_isolates_a_row_that_raises(world, capsys):
    world.ready_row("batch-bad")
    world.ready_row("batch-good")
    real = world.orch._task_md_closed

    def flaky(task):
        if task.slug == "batch-bad":
            raise RuntimeError("md unreadable")
        return real(task)

    with mock.patch.object(world.orch, "_task_md_closed", side_effect=flaky):
        result = world.orch.reconcile_batch_gate()       # no config → not-enabled

    assert result["returned"] == ["batch-good"]
    assert world.row("batch-bad").state == A.BATCH_READY          # skipped, kept
    assert world.row("batch-good").state == A.FAILED
    assert "reconcile_batch_gate: batch-bad skipped: md unreadable" in capsys.readouterr().out


def test_sweep_never_raises_when_the_config_read_breaks(world, monkeypatch, capsys):
    world.ready_row()

    def boom(*_a, **_k):
        raise RuntimeError("config store is down")

    monkeypatch.setattr(batch_gate, "load_config", boom)
    assert world.orch.reconcile_batch_gate() == EMPTY_SWEEP
    assert world.row().state == A.BATCH_READY           # kept for the next tick
    assert "batch gate sweep skipped (fail-open)" in capsys.readouterr().out


def test_sweep_reads_the_config_two_up_from_the_ledger(world, monkeypatch):
    world.ready_row()
    seen = []
    monkeypatch.setattr(batch_gate, "load_config",
                        lambda root: seen.append(root) or CONFIG)
    world.orch.reconcile_batch_gate()
    assert seen == [world.root]


# --- tick() ------------------------------------------------------------------------

_TICK_STUBS = (
    "reroute_remote", "reap_stale", "reconcile_unparked", "reconcile_resumes",
    "reconcile_rescope", "_dispatch_serial_per_checkout", "collect",
    "reconcile_split_parents", "reconcile_merged",
    "reconcile_closed", "reconcile_unblocked", "reconcile_parents",
    "reconcile_human_ready", "reconcile_queue", "_advote_bridge_tick",
    "_beta_sweep_tick", "_project_stall_tick", "renudge_due",
)


def _tick(orch, *, now=None, live: tuple = ()):
    """`orch.tick([])` with every sweep stubbed except reconcile_batch_gate (and
    whatever `live` names), and the three holds answered — so the result says
    something about the batch sweep's place in the tick and nothing about this
    host (the test_orchestrator._hermetic_tick approach, widened)."""
    patches = [mock.patch.object(orch, name, return_value=[])
               for name in _TICK_STUBS if name not in live]
    patches += [mock.patch.object(orch, "usage_hold", return_value=None),
                mock.patch.object(orch, "paused", return_value=False),
                mock.patch.object(orch, "budget_exceeded", return_value=False)]
    for patch in patches:
        patch.start()
    try:
        return orch.tick([], now)
    finally:
        for patch in reversed(patches):
            patch.stop()


def test_tick_reports_the_sweep_under_batch_gate(world):
    assert _tick(world.orch)["batch_gate"] == EMPTY_SWEEP


def test_tick_runs_the_sweep_with_its_clock(world):
    world.write_config()
    a = world.ready_row()
    ready_at = _parse_ts(a.payload[batch_gate.KEY_BATCH]["ready_ts"])

    assert _tick(world.orch, now=ready_at + 60)["batch_gate"] == EMPTY_SWEEP
    assert world.row().state == A.BATCH_READY

    result = _tick(world.orch, now=ready_at + batch_gate.EXPIRE_S + 1)
    assert result["batch_gate"] == {**EMPTY_SWEEP, "returned": [SLUG]}
    assert world.row().payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_EXPIRED


def test_tick_sweeps_after_collect(world):
    # No config: the RESULT collect() just turned into batch_ready is seen by
    # the sweep in the SAME tick and sent straight back.
    a = world.dispatch()
    world.reply(a, **_ready_result())
    with mock.patch.object(world.orch, "_close_with_evidence"):
        result = _tick(world.orch, live=("collect",))

    assert result["batch_gate"]["returned"] == [SLUG]
    row = world.row()
    assert row.payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_NOT_ENABLED
    assert [h["type"] for h in row.history[-3:]] == [
        m.RESULT, batch_gate.HISTORY_READY, A.BATCH_RETURNED]


def test_tick_survives_a_broken_sweep(world, monkeypatch):
    world.ready_row()

    def boom(*_a, **_k):
        raise RuntimeError("config store is down")

    monkeypatch.setattr(batch_gate, "load_config", boom)
    result = _tick(world.orch)
    assert result["batch_gate"] == EMPTY_SWEEP
    assert "dispatched" in result and "renudged" in result       # the tick finished


# --- runner → mailbox → collect -----------------------------------------------------

class _BatchExecutor:
    """Stands in for StagedExecutor: returns the outcome a held batch run does."""

    mode = "fake"

    def __init__(self, outcome: ExecOutcome):
        self.outcome = outcome

    def execute(self, msg, agent, progress, **_):
        return self.outcome


def test_a_held_run_reaches_the_ledger_through_the_real_mailbox(world):
    # The runner splats its outcome payload into the RESULT envelope; this pins
    # that the keys survive that trip and collect() reads them off it.
    a = world.dispatch(task_path=f"Todo/items/{SLUG}.md")
    payload = _ready_result()
    summary = payload.pop("summary")
    runner = Runner(world.mb, world.reg.get("w"),
                    _BatchExecutor(ExecOutcome(True, summary, payload)),
                    log=lambda *_: None, ledger_root=world.ledger.root)

    assert runner.poll_once() == 1
    world.collect()

    row = world.row()
    assert row.state == A.BATCH_READY
    assert row.corr_id == a.corr_id
    assert row.payload[batch_gate.KEY_BATCH]["head_sha"] == HEAD_SHA
    assert row.payload[batch_gate.KEY_BATCH]["pr_url"] == PR_URL
    assert world.routed == []


def test_a_returned_run_reaches_the_ledger_through_the_real_mailbox(world):
    world.dispatch(task_path=f"Todo/items/{SLUG}.md")
    payload = _return_result(batch_gate.RETURN_QUICK_NOT_PASS)
    summary = payload.pop("summary")
    runner = Runner(world.mb, world.reg.get("w"),
                    _BatchExecutor(ExecOutcome(True, summary, payload)),
                    log=lambda *_: None, ledger_root=world.ledger.root)

    assert runner.poll_once() == 1
    world.collect()

    row = world.row()
    assert row.display_state == "deferred"
    assert row.payload[batch_gate.KEY_RETURN_REASON] == "quick-not-pass"


# --- wire strings ------------------------------------------------------------------

def test_wire_strings_are_the_decided_ones():
    # These land in git-tracked assignment JSON and in envelopes; a rename here
    # strands every row written under the old spelling.
    assert A.BATCH_READY == "batch_ready"
    assert A.BATCH_RETURNED == "batch-returned"
    assert batch_gate.HISTORY_READY == "batch-ready"
    assert batch_gate.HISTORY_MERGED == "batch-merged"
    assert (batch_gate.RESULT_READY, batch_gate.RESULT_RETURN, batch_gate.KEY_BATCH) == (
        "batch_ready", "batch_return", "batch")
    assert (batch_gate.KEY_VERIFY_BATCH, batch_gate.KEY_VERIFY_FULL,
            batch_gate.KEY_RETURN_REASON) == (
        "verify_batch", "verify_full", "batch_return_reason")
    assert ALERT_BATCH_GATE_FAILED == "batch-gate-failed"
    assert ALERT_BATCH_GATE_FAILED not in FAILURE_ALERT_KINDS
    reasons = {v for k, v in vars(batch_gate).items() if k.startswith("RETURN_")}
    assert reasons == {
        "gate-failed", "gate-timeout", "conflict", "branch-moved", "branch-missing",
        "no-checkout", "expired", "restarts", "merge-failed", "not-enabled",
        "no-device-evidence", "no-pr", "no-head-sha", "quick-not-pass"}
