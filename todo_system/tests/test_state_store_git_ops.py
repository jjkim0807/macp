"""Git-op regression suite for the state store — leaf ⑥, design §⑤.

Three vault git operations the design calls out — an old worker-branch
checkout → return (§5.1), a `git reset --hard` (§5.2), and a `git stash push -u`
/ apply (§5.3) — are automated against a throwaway git vault (``tmp_path``), once
per backend:

* **external mode** proves the authoritative state (the host-local SQLite ledger
  and mailbox, kept *outside* the vault tree) is INVARIANT under every vault git
  op — the win this backend buys (acceptance #1).
* **git mode** is a phenomenon-pinning control (acceptance #2): it reproduces the
  file-level resurrection / rollback of the TRACKED ledger + inbox the git
  backend is still exposed to, AND pins that the gitignored reader cursor
  (`Worker/agents/_state/<agent>/cursor.json`, `seen` + ack tombstones —
  mailbox.py:47-55) survives those ops and still suppresses re-delivery. Both
  backends' tests PASS: the "RED control" pins existing behaviour, it is not a
  failing test.

Mirrors the fixtures in ``todo_system/orchestrator/tests/test_state_store.py``
and is stdlib-only (``subprocess`` / ``json`` + pytest), so
``scripts/check_engine_isolation.sh`` stays ``RESULT: PASS``. Every store is built
on a tmp vault; nothing uses the real ``vault_root``, and the SQLite file lives
OUTSIDE the vault working tree (``tmp_path/"state.sqlite"``, a sibling of
``vault/``) so a tree op cannot reach it.
"""

from __future__ import annotations

import subprocess

import pytest

from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import (
    DISPATCHED,
    FAILED,
    IN_PROGRESS,
    Assignment,
)
from todo_system.orchestrator.mailbox import DEFAULT_ROOT
from todo_system.orchestrator.message import Envelope
from todo_system.orchestrator.state_store import GitStateStore
from todo_system.orchestrator.state_store_sqlite import ExternalStateStore


# ---------------------------------------------------------------------------
# Fixtures / helpers (copied, not shared, from test_state_store.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path):
    # tmp_path/"vault" so tmp_path/"state.sqlite" below is a *sibling*, i.e.
    # OUTSIDE the vault working tree — the whole point of the external backend.
    return tmp_path / "vault"


def _git(vault, *args):
    """Run a git subcommand in ``vault``, raising on failure."""
    return subprocess.run(
        ["git", *args], cwd=vault, check=True, capture_output=True, text=True
    )


def _git_vault(vault):
    """A throwaway git vault that mirrors the real vault's gitignore.

    ``symbolic-ref`` pins the initial branch to ``main`` independently of the
    host git version / ``init.defaultBranch`` (so ``checkout main`` below is
    reliable). The user identity is what GitMailbox's auto-commits need. The
    committed ``.gitignore`` makes ``Worker/agents/_state/`` an IGNORED path —
    exactly as in the real vault — so the reader cursor is never committed and
    survives checkout / reset / ``stash -u`` intact; without it those ops would
    stash or rewind the cursor and the suppression assertions would be hollow.
    """
    vault.mkdir(parents=True, exist_ok=True)
    _git(vault, "init", "-q")
    _git(vault, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(vault, "config", "user.email", "test@example.invalid")
    _git(vault, "config", "user.name", "state-store-git-ops-test")
    (vault / DEFAULT_ROOT).mkdir(parents=True)
    (vault / ".gitignore").write_text("Worker/agents/_state/\n", encoding="utf-8")
    _git(vault, "add", ".gitignore")
    _git(vault, "commit", "-q", "-m", "init vault")


def _commit(vault, msg):
    """Stage everything tracked (the gitignored cursor is skipped) and commit."""
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", msg)


def _stash_ref(vault, tag):
    """Resolve the stash created with ``-m <tag>`` to its ``stash@{N}`` ref.

    The test applies a stash by this ref, never a bare ``pop``/``apply``, so a
    shared stash stack (the real vault) is left untouched (design §5.3)."""
    out = _git(vault, "stash", "list").stdout
    for line in out.splitlines():
        ref, _, subject = line.partition(":")
        if tag in subject:
            return ref.strip()
    raise AssertionError(f"no stash entry tagged {tag!r}:\n{out}")


def _git_store(vault):
    _git_vault(vault)
    # Explicit planner path so nothing writes worker.PLANNER_STATE_PATH (unused
    # here, but it keeps the store off the real vault by construction).
    return GitStateStore(vault, planner_state_path=vault / "planner_state.json")


def _external_store(vault, tmp_path):
    # A git vault is still needed: the git OPS run on the tree. The SQLite lives
    # OUTSIDE it (sibling of vault/), so a tree op cannot reach it.
    _git_vault(vault)
    return ExternalStateStore(sqlite_path=tmp_path / "state.sqlite", vault_root=vault)


def _inbox_path(vault, agent, msg):
    return vault / "Worker" / "agents" / agent / "inbox" / f"{msg.msg_id}.json"


def _assignment(state):
    return Assignment(task_id="A", agent_id="worker", corr_id="c", state=state)


# ---------------------------------------------------------------------------
# §5.1 — old worker branch checkout → return
# ---------------------------------------------------------------------------


def test_checkout_old_branch_git_resurrects_inbox_but_cursor_suppresses(vault):
    """git control: checking out an old worker branch resurrects a tracked inbox
    file on disk (the file-level vuln), but the gitignored cursor survived the
    branch switch and still suppresses the acked message."""
    store = _git_store(vault)
    mb, led = store.mailbox(), store.ledger()

    msg_m = Envelope(to="orchestrator", type=m.TASK_ASSIGN, task_id="A")
    msg_n = Envelope(to="orchestrator", type=m.PING)
    mb.send(msg_m)  # GitMailbox.send auto-commits each message
    mb.send(msg_n)
    led.save(_assignment(IN_PROGRESS))
    _commit(vault, "seed ledger A")

    # The old worker branch captures the tree WITH msg_m committed.
    _git(vault, "branch", "worker/old")

    path_m = _inbox_path(vault, "orchestrator", msg_m)
    assert path_m.is_file()

    # ack msg_m: cursor records seen={msg_m}; GitMailbox._after_ack returns set(),
    # so the file is NOT deleted (mailbox.py:240-244) — suppression is via `seen`.
    mb.ack("orchestrator", msg_m)
    assert path_m.is_file()
    assert [e.msg_id for e in mb.receive("orchestrator")] == [msg_n.msg_id]

    # On main, drop msg_m so main no longer carries it.
    _git(vault, "rm", "-q", str(path_m.relative_to(vault)))
    _commit(vault, "drop M from main")
    assert not path_m.exists()

    # Checking out the old branch RESURRECTS the file on disk...
    _git(vault, "checkout", "-q", "worker/old")
    assert path_m.is_file()  # the file-level vulnerability
    # ...but the gitignored cursor survived the switch, so receive suppresses it.
    assert [e.msg_id for e in mb.receive("orchestrator")] == [msg_n.msg_id]

    _git(vault, "checkout", "-q", "main")
    assert not path_m.exists()  # gone again on main


def test_checkout_old_branch_external_leaves_sqlite_invariant(vault, tmp_path):
    """external: the SQLite ledger/mailbox live outside the tree, so an old
    worker-branch checkout → return cannot touch them. msg_m stays permanently
    consumed (row deleted) — no tombstone needed."""
    store = _external_store(vault, tmp_path)
    mb, led = store.mailbox(), store.ledger()

    msg_m = Envelope(to="orchestrator", type=m.TASK_ASSIGN, task_id="A")
    msg_n = Envelope(to="orchestrator", type=m.PING)
    mb.send(msg_m)  # SQLite rows; nothing in the tree
    mb.send(msg_n)
    led.save(_assignment(IN_PROGRESS))
    mb.ack("orchestrator", msg_m)  # SqliteMailbox DELETEs the msg_m row

    before_led = led.load("A").to_json()
    before_recv = [e.msg_id for e in mb.receive("orchestrator")]
    assert before_recv == [msg_n.msg_id]

    # A real tree swap: a dummy tracked file that differs between the branches.
    (vault / "dummy.txt").write_text("v1\n", encoding="utf-8")
    _commit(vault, "dummy v1")
    _git(vault, "branch", "worker/old")
    (vault / "dummy.txt").write_text("v2\n", encoding="utf-8")
    _commit(vault, "dummy v2")
    _git(vault, "checkout", "-q", "worker/old")
    assert (vault / "dummy.txt").read_text() == "v1\n"  # the tree really moved
    _git(vault, "checkout", "-q", "main")
    assert (vault / "dummy.txt").read_text() == "v2\n"

    assert led.load("A").to_json() == before_led
    assert led.load("A").state == IN_PROGRESS
    assert [e.msg_id for e in mb.receive("orchestrator")] == before_recv


# ---------------------------------------------------------------------------
# §5.2 — git reset --hard
# ---------------------------------------------------------------------------


def test_reset_hard_git_rolls_back_ledger_but_cursor_suppresses(vault):
    """git control: `git reset --hard HEAD~1` rewinds the TRACKED ledger file to
    its previous committed state (the known git-mode limitation, design §5.2),
    while the gitignored cursor is untouched and keeps suppressing msg_m."""
    store = _git_store(vault)
    mb, led = store.mailbox(), store.ledger()

    msg_m = Envelope(to="orchestrator", type=m.TASK_ASSIGN, task_id="A")
    mb.send(msg_m)  # committed by GitMailbox.send
    mb.ack("orchestrator", msg_m)  # cursor seen={msg_m}; file kept
    assert mb.receive("orchestrator") == []

    led.save(_assignment(DISPATCHED))
    _commit(vault, "ledger A dispatched")  # becomes HEAD~1
    led.save(_assignment(IN_PROGRESS))
    _commit(vault, "ledger A in_progress")  # HEAD
    assert led.load("A").state == IN_PROGRESS

    _git(vault, "reset", "--hard", "HEAD~1")
    # Ledger.load re-reads the file each call, so it sees the rewound content.
    assert led.load("A").state == DISPATCHED  # rolled back with the tree
    # The cursor is gitignored → untouched by reset --hard → still suppresses.
    assert _inbox_path(vault, "orchestrator", msg_m).is_file()
    assert mb.receive("orchestrator") == []


def test_reset_hard_external_leaves_sqlite_invariant(vault, tmp_path):
    """external: reset only rewinds the tree, which holds no ledger in external
    mode → SQLite ledger/mailbox byte-identical, A's progress state preserved."""
    store = _external_store(vault, tmp_path)
    mb, led = store.mailbox(), store.ledger()

    msg_m = Envelope(to="orchestrator", type=m.TASK_ASSIGN, task_id="A")
    mb.send(msg_m)
    mb.ack("orchestrator", msg_m)  # SqliteMailbox deletes the row
    led.save(_assignment(DISPATCHED))
    led.save(_assignment(IN_PROGRESS))
    before_led = led.load("A").to_json()
    assert led.load("A").state == IN_PROGRESS
    assert mb.receive("orchestrator") == []

    # Two dummy tracked commits so `reset --hard HEAD~1` actually rewinds a tree.
    (vault / "dummy.txt").write_text("v1\n", encoding="utf-8")
    _commit(vault, "dummy v1")
    (vault / "dummy.txt").write_text("v2\n", encoding="utf-8")
    _commit(vault, "dummy v2")
    _git(vault, "reset", "--hard", "HEAD~1")
    assert (vault / "dummy.txt").read_text() == "v1\n"  # the tree really rewound

    assert led.load("A").to_json() == before_led
    assert led.load("A").state == IN_PROGRESS  # SQLite untouched
    assert mb.receive("orchestrator") == []


# ---------------------------------------------------------------------------
# §5.3 — git stash / autostash
# ---------------------------------------------------------------------------


def test_stash_git_removes_dirty_ledger_but_cursor_suppresses(vault):
    """git control: `git stash push -u` clears a dirty (uncommitted) ledger edit
    from the tree and `apply` restores it; the gitignored cursor is never stashed
    by -u, so it keeps suppressing msg_m throughout."""
    store = _git_store(vault)
    mb, led = store.mailbox(), store.ledger()

    msg_m = Envelope(to="orchestrator", type=m.TASK_ASSIGN, task_id="A")
    mb.send(msg_m)
    mb.ack("orchestrator", msg_m)  # cursor seen={msg_m}
    led.save(_assignment(DISPATCHED))
    _commit(vault, "ledger A dispatched")
    assert mb.receive("orchestrator") == []

    # A dirty TRACKED edit (a stage mutating the ledger mid-run) + an untracked
    # file in the tracked area.
    led.save(_assignment(FAILED))
    (vault / "Worker" / "assignments" / "scratch.tmp").write_text("x\n", encoding="utf-8")
    assert led.load("A").state == FAILED

    _git(vault, "stash", "push", "-u", "-m", "macp-test-5-3")
    assert led.load("A").state == DISPATCHED  # dirty edit stashed away
    assert not (vault / "Worker" / "assignments" / "scratch.tmp").exists()
    # The cursor lives under the ignored Worker/agents/_state/, so -u did NOT
    # stash it — suppression still holds.
    assert mb.receive("orchestrator") == []

    _git(vault, "stash", "apply", _stash_ref(vault, "macp-test-5-3"))
    assert led.load("A").state == FAILED  # dirty edit restored
    assert (vault / "Worker" / "assignments" / "scratch.tmp").exists()
    assert mb.receive("orchestrator") == []


def test_stash_external_leaves_sqlite_invariant(vault, tmp_path):
    """external: stash only moves tree changes → SQLite ledger/mailbox invariant
    across `stash push -u` and `apply`."""
    store = _external_store(vault, tmp_path)
    mb, led = store.mailbox(), store.ledger()

    msg_m = Envelope(to="orchestrator", type=m.TASK_ASSIGN, task_id="A")
    mb.send(msg_m)
    mb.ack("orchestrator", msg_m)
    led.save(_assignment(IN_PROGRESS))
    before_led = led.load("A").to_json()
    assert mb.receive("orchestrator") == []

    (vault / "dummy.txt").write_text("v1\n", encoding="utf-8")
    _commit(vault, "dummy v1")
    (vault / "dummy.txt").write_text("dirty\n", encoding="utf-8")  # tracked, dirty
    (vault / "untracked.tmp").write_text("x\n", encoding="utf-8")  # untracked
    _git(vault, "stash", "push", "-u", "-m", "macp-test-5-3-ext")
    assert (vault / "dummy.txt").read_text() == "v1\n"  # the tree really cleaned
    assert not (vault / "untracked.tmp").exists()

    assert led.load("A").to_json() == before_led
    assert led.load("A").state == IN_PROGRESS  # SQLite outside the tree → invariant
    assert mb.receive("orchestrator") == []

    _git(vault, "stash", "apply", _stash_ref(vault, "macp-test-5-3-ext"))
    assert led.load("A").to_json() == before_led  # still invariant
    assert mb.receive("orchestrator") == []
