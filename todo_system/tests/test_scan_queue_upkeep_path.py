"""The todo-sync pass (`worker.scan_queue`) after the v1 queue worker was removed.

`scan_queue()` is what the todo-sync daemon calls each time it sees a change.
The v1 worker's queue self-heal used to run inside it and did three things.
Two are still needed and now sit in `scan_queue` itself:

* a `pending` row whose source task has closed is marked `skipped` — nothing
  else closes a row that has no ledger assignment while its task still names
  a dispatchable mode — and only on a source that is explicitly closed;
* the two filesystem-only worktree sweeps run once per pass, and a sweep that
  raises cannot stop the pass.

The third job, resetting `in_progress` rows and retrying `failed` ones, went
with the worker that produced those states: such rows are left as they are.

Both sweeps are replaced in every test. Unpatched they shell out against the
module's real vault root and remove worktrees.
"""

from __future__ import annotations

import json
import types

import pytest

from todo_system import worker

_TASK = """---
tags:
  - todo
status: {status}
worker_mode: auto
outcome: "fixture"
---

# {slug}
"""


def _row(slug: str, status: str, **extra) -> dict:
    return {"slug": slug, "run_id": f"20200101T000000-{slug}",
            "task_path": f"Todo/items/{slug}.md", "mode": "auto", "status": status,
            "created_at": "2020-01-01T00:00:00", **extra}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    items = tmp_path / "Todo" / "items"
    items.mkdir(parents=True)
    queue_path = tmp_path / "Worker" / "queue.json"
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setattr(worker, "ITEMS_DIR", items)
    monkeypatch.setattr(worker, "QUEUE_PATH", queue_path)
    calls = {"wt": 0, "parked": 0}

    def reclaim():
        calls["wt"] += 1
        return ["WT-LINE"]

    def parked():
        calls["parked"] += 1
        return ["PARKED-LINE"]

    monkeypatch.setattr(worker, "reclaim_stale_worktrees", reclaim)
    monkeypatch.setattr(worker._worktree_sweep, "reclaim_parked_worktrees", parked)

    def task(slug: str, status: str = "open") -> None:
        (items / f"{slug}.md").write_text(_TASK.format(slug=slug, status=status),
                                          encoding="utf-8")

    def queue(*rows: dict) -> None:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(json.dumps({"runs": list(rows)}), encoding="utf-8")

    def runs() -> list[dict]:
        return json.loads(queue_path.read_text(encoding="utf-8"))["runs"]

    return types.SimpleNamespace(task=task, queue=queue, runs=runs, calls=calls)


def test_closed_source_closes_its_pending_rows_and_nothing_else(vault):
    vault.task("done-task", "done")
    vault.task("dropped-task", "canceled")
    vault.task("live-task")
    vault.queue(_row("done-task", "pending"), _row("done-task", "pending"),
                _row("dropped-task", "pending"), _row("live-task", "pending"),
                _row("no-such-task", "pending"))

    assert worker.scan_queue() == 0

    assert [(r["slug"], r["status"]) for r in vault.runs()] == [
        ("done-task", "skipped"), ("done-task", "skipped"),
        ("dropped-task", "skipped"),
        ("live-task", "pending"),
        # not found is not closed: Todo/items can be absent on a fresh clone
        ("no-such-task", "pending"),
    ]


def test_rows_the_removed_worker_left_are_not_touched(vault):
    vault.task("failed-task")
    vault.task("stuck-task")
    vault.queue(_row("failed-task", "failed"),
                _row("stuck-task", "in_progress", started_at="2020-01-01T00:00:00"))

    assert worker.scan_queue() == 0

    failed, stuck = vault.runs()
    assert failed["status"] == "failed" and "auto_retry_count" not in failed
    assert stuck["status"] == "in_progress"
    assert stuck["started_at"] == "2020-01-01T00:00:00"


def test_sweeps_run_once_per_pass_and_are_logged(vault, capsys):
    vault.task("gone-task", "done")
    vault.queue(_row("gone-task", "pending"))

    assert worker.scan_queue() == 0

    assert vault.calls == {"wt": 1, "parked": 1}
    out = capsys.readouterr().out.splitlines()
    assert "scan-queue: skip stale pending: gone-task (source done)" in out
    assert "worktree-sweep: WT-LINE" in out
    assert "worktree-sweep: PARKED-LINE" in out
    # sweep lines never change the queue
    assert [r["status"] for r in vault.runs()] == ["skipped"]


def test_a_raising_parked_sweep_does_not_stop_registration(vault, monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(worker._worktree_sweep, "reclaim_parked_worktrees", boom)
    vault.task("new-task")

    assert worker.scan_queue() == 0

    [row] = vault.runs()
    assert (row["slug"], row["status"], row["mode"], row["host"]) == (
        "new-task", "pending", "auto", "default")
    assert row["task_path"] == "Todo/items/new-task.md"
