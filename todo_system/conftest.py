"""Package-wide guards: no test may reach the real kakao channel or its state,
nor this checkout's runtime files, nor the real ~/Worker clones.

Applies to BOTH suites (todo_system/tests and todo_system/orchestrator/tests) —
per-test monkeypatching proved leaky before ([[tests-reach-real-alert-sender]]),
and the noise-tier router adds real state files (logs/kakao_digest.jsonl,
kakao_dedup.json, kakao_route.log) that an incidental alert inside any test
would otherwise write into the live vault.

* ``human.KAKAO_SCRIPT`` / ``worker.KAKAO_SEND_SCRIPT`` point at a path that
  does not exist, so the send helpers return before spawning anything. A test
  that wants to observe a spawn overrides the constant with its own recorder
  script on top of this.
* ``KAKAO_VAULT_DIR`` redirects ``kakao_format.route()``'s default state files
  (read from the env at call time, same seam kakao_send.sh honours) into the
  test's tmp dir.

Runtime files — ``Worker/_state/runner_heartbeat``, ``Worker/queue.lock`` and
``Todo/todo.db`` derive from the vault root (``paths.vault_root()``:
``MACP_VAULT_ROOT`` when set, else this checkout), so a test that reaches them
writes into that vault (changing cwd does not help). The redirect is keyed on
the root each module built them from, so an env vault outside the checkout
redirects the same way. The suite did reach them, and leftover files were the
mild half of it:

* ``Runner.poll_once`` writes the heartbeat and deletes it again. In the live
  vault that overwrites and then deletes the REAL runner's heartbeat, so the
  restart gate (``worker._runner_idle``) reads a runner that is mid-stage as
  idle. ``StagedExecutor`` re-writes it every stage and never clears it.
* ``metrics.render`` opened ``Todo/todo.db`` read-write and left a 0-byte file
  behind, which un-skipped two ``test_macp_dashboard.py`` tests from the second
  run on: the skip count depended on how often the checkout had been tested.
* ``Todo/NOW.md``, ``Todo/FOCUS.md`` and ``Todo/Dashboard.md`` are rewritten by
  the ``sync_*`` writers, which the ``now``/``focus`` commands and ``worker``'s
  parent auto-close call. They write through ``cli``'s import-time globals, so
  a test that forgets to rebind them overwrites the live notes.

The live mailbox — ``Worker/agents/`` (``<agent>/inbox/*.json`` envelopes,
``_state/<agent>/cursor.json`` cursors) is a whole tree, not a file. A test that
opened the default mailbox from the checkout root once left a real TASK_ASSIGN
in an inbox; a squash merge carried it into main and it dispatched no-op runs.
Its root is cwd-relative (``mailbox.DEFAULT_ROOT``), so no module attribute
names it, and envelope names are random ids, so no basename matches them. Two
trees are guarded: this checkout's, which tests running from its root reach,
and the vault's, which differs only when ``MACP_VAULT_ROOT`` names another one.

Two layers, so a missed row cannot pass silently:

* ``_runtime_paths_isolated`` rebinds every ``_RUNTIME_PATHS`` row into a fresh
  tmp dir per test. The production constants themselves are not touched. It
  also wraps ``Mailbox.__init__``, the one place every transport and every way
  of opening a mailbox passes through, so a mailbox rooted at or under the live
  tree (relative or absolute) opens on a mirror in the same tmp dir instead.
* ``_TRIPWIRE``, an audit hook installed when this file is imported (so
  collection, ``setUpClass`` and import-time code are covered too), refuses any
  write, delete, rename, utime or read-write sqlite open of the real files, and
  of anything under the live mailbox tree, with ``RealRuntimePathTouched`` and
  records who tried. Reads stay allowed (``Worker/agents/registry.yaml`` is read
  by every orchestrator entry point). It blocks rather than only
  reporting because in the live vault a report would arrive after the heartbeat
  was already gone. ``_real_runtime_untouched`` fails the session on any record,
  and on a real ``queue.lock`` / ``todo.db`` that appeared or vanished — the one
  trace a spawned process leaves that an in-process hook cannot see.

Limits of the mailbox tree guard: ``mkdir`` is not an audit event, so an empty
directory can still appear under the tree (the redirect is what keeps
``send()`` from making one); a path that reaches the tree only through a
symlink passes the hook's cheap prefix check; a cwd inside a checkout other
than this one or the env vault is that checkout's tree, not guarded. The live inbox is left out of the session
existence check, since the live orchestrator and runners change it by themselves.

The ~/Worker clones — ``StagedExecutor._execute`` calls the run-start disk guard
(``orchestrator/disk_guard.py``) on every run, and below its 3 GiB trigger the
guard runs git probes and ``./gradlew clean`` in the clones under ~/Worker. That
is not hypothetical: the HQ host had 2.2–2.6G free on 2026-09-19, so every
unisolated ``_execute`` test would have fired it. ``_disk_guard_isolated`` sets
the guard's kill switch (``MACP_DISK_GUARD=0``) and points ``WORKER_ROOT``,
``ASSIGNMENTS_DIR`` and ``JOBS_DIR`` into a tmp dir for every test. The guard's own
tests lift the switch and inject a fake for every seam; the tmp paths stay behind
them as a second belt, so a forgotten seam finds an empty worker root, not a clone.
The parked-worktree sweep (``orchestrator/worktree_sweep.py``, reached by every
doctor and runner test) reads those same three seams plus its own
``REGISTRY_DIR``, which the fixture also points into the tmp dir, so it can
never remove a real worktree or gradle-registry dir.

The ``auto`` tick's own side effects — ``orchestrator main auto`` reaches three
things no test may do for real. ``usage_hold()`` calls ``usage_guard.check()``,
whose ``get_snapshot`` sends a GET to the Anthropic OAuth usage endpoint (its
rate limit is shared with every Claude CLI on the account) and writes
``Worker/_state/usage_guard/latest.json`` and ``fetch.lock`` under the vault root
even when the fetch fails. ``_pull_main`` and ``_check_divergence`` run a real
``git fetch origin main`` in the vault, which rewrites ``FETCH_HEAD``, and the
latter writes ``Worker/_state/divergence.json`` there. ``usage_hold`` swallows
every exception, so a test reaching all of this still passed.
``_auto_side_effects_isolated`` closes it for every test:

* ``usage_guard._fetch_usage`` / ``_fetch_usage_for`` raise
  ``RealUsageFetchBlocked``, a plain RuntimeError, so ``get_snapshot`` takes the
  same failure path it takes offline. A test that patches them itself wins.
* ``usage_guard._state_path`` is wrapped: an answer under the real state dir
  becomes a tmp dir (``fetch*.lock`` derive from its parent and follow). It is a
  wrapper and not only ``USAGE_GUARD_STATE_DIR``, which is set as well for
  spawned processes, because ``test_usage_guard.py``'s ``_clean_usage_guard_env``
  deletes every ``USAGE_GUARD_*`` env after this fixture runs. A test that sets
  its own env or patches ``usage_guard.ROOT`` gets the real answer unchanged.
* ``orchestrator._pull_main`` / ``_check_divergence`` do nothing when no ``_run``
  is injected (``_check_divergence`` returns a state dict of the real shape and
  writes no file). With an injected ``_run`` the real function runs, so their own
  tests are untouched.

Opt back in with ``@pytest.mark.real_usage_fetch`` (real fetch functions; the
state redirect stays) or ``@pytest.mark.real_vault_git`` (real git functions),
on a test, class or method. Limits: a spawned process sees only the env
redirect, not the fetch or git block, and a test that reloads either module
(``importlib.reload``) gets the real functions back for the rest of that test.

Bare ``python -m unittest`` never loads this file, so it gets none of these layers.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import sys
import types
from pathlib import Path
from typing import NamedTuple

import pytest

from todo_system import cli, usage_guard, worker
from todo_system.orchestrator import disk_guard, human, mailbox, metrics, worktree_sweep
from todo_system.orchestrator import orchestrator as _orch_mod

# (module, attribute) of every real runtime file a test is known to reach. Read
# at CALL time by _runtime_paths_isolated, so emptying it in memory switches the
# redirect off — which is how the tripwire's RED control is run.
_RUNTIME_PATHS = (
    (worker, "RUNNER_HEARTBEAT_PATH"),
    (worker, "QUEUE_LOCK_PATH"),
    (cli, "DB_PATH"),
    (metrics, "DB"),        # metrics' own copy of Todo/todo.db (_db_counts)
    (cli, "NOW_PATH"),      # the three notes the sync_* writers regenerate
    (cli, "FOCUS_PATH"),
    (cli, "DASHBOARD_PATH"),
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
# The real values, captured at import — before any fixture or test can rebind
# them — so the redirect is keyed on the real file, never on a leftover patch.
_REAL_RUNTIME = types.MappingProxyType(
    {(module, attr): getattr(module, attr) for module, attr in _RUNTIME_PATHS})
# The vault root each module built its rows from, captured with them. Lexical,
# like the rows, so relative_to() never trips over a symlink or a resolve().
# MACP_VAULT_ROOT can put it outside this checkout (a worker stage testing a
# worktree names the live vault); unset, it is this checkout.
_RUNTIME_ANCHORS = types.MappingProxyType(
    {worker: worker.ROOT, cli: cli.ROOT, metrics: metrics._ROOT})
# The live mailbox trees, realpath -> realpath of the root it hangs off: the
# checkout's (the root is cwd-relative, so tests running from the checkout root
# reach it) and the vault's. With no env they are one tree.
_LIVE_MAILBOX_ROOTS = types.MappingProxyType(
    {os.path.realpath(anchor / mailbox.DEFAULT_ROOT): os.path.realpath(anchor)
     for anchor in (_REPO_ROOT, worker.ROOT)})
_REAL_MAILBOX_INIT = mailbox.Mailbox.__init__


def _redirect_target(redirect, real, anchor):
    # Raises if a row was built from a root missing from _RUNTIME_ANCHORS:
    # loud beats a silently mis-mapped redirect.
    return redirect / real.relative_to(anchor)


class RealRuntimePathTouched(PermissionError):
    """Raised by the tripwire in place of the write it refused.

    A PermissionError on purpose: code under test already has a path for an
    unwritable file (runner_heartbeat_start/clear swallow OSError), so the
    tripwire does not invent a failure shape production never sees.
    """

    # Marks the refusal for production's broad fallbacks
    # (Orchestrator._queued_task_ids), which re-raise it instead of reading it
    # as an unreachable mailbox. Checked by attribute, so nothing imports this file.
    never_swallow = True


class _Hit(NamedTuple):
    event: str
    path: str
    test: str
    stack: tuple[str, ...]


class _RuntimePathTripwire:
    """Audit hook that refuses writes to the protected paths and records who tried.

    ``paths`` are single files; ``trees`` are directories protected with
    everything below them. Every audit event in the process lands here, so
    anything outside ``_EVENTS`` returns at once, and a basename check or a
    string prefix check on the absolute path runs before ``realpath``. A path
    that only reaches a tree through a symlink fails that prefix check and is
    not caught. Normalisation runs under try/except: the hook must never raise
    anything but ``RealRuntimePathTouched``, or a bug in it would break
    unrelated I/O.
    """

    error = RealRuntimePathTouched
    _EVENTS = frozenset({
        "open", "os.remove", "os.rename", "os.utime", "os.truncate",
        "os.link", "os.symlink", "shutil.rmtree", "sqlite3.connect",
    })
    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def __init__(self, paths, trees=()):
        self.protected = frozenset(os.path.realpath(p) for p in paths)
        self.trees = frozenset(os.path.realpath(t) for t in trees)
        self.hits: list[_Hit] = []
        self._extra: dict[str, list[_Hit]] = {}
        self._extra_trees: dict[str, list[_Hit]] = {}
        self._reindex()

    def _reindex(self) -> None:
        self._names = frozenset(os.path.basename(p) for p in (*self.protected, *self._extra))
        self._prefixes = tuple(t + os.sep for t in (*self.trees, *self._extra_trees))

    def _in_tree(self, path) -> bool:
        return bool(self._prefixes) and (path + os.sep).startswith(self._prefixes)

    @contextlib.contextmanager
    def guarding(self, path, tree=False):
        """Protect one more path inside the block and yield its hits.

        Those hits stay off ``hits``, so a test can trip the wire on purpose
        without failing the session. ``tree=True`` protects the whole subtree
        under ``path``; inside a session tree it takes that block's hits there
        instead of onto ``hits``.
        """
        real = os.path.realpath(path)
        extra = self._extra_trees if tree else self._extra
        extra[real] = hits = []
        self._reindex()
        try:
            yield hits
        finally:
            del extra[real]
            self._reindex()

    def __call__(self, event, args):
        if event not in self._EVENTS:
            return
        try:
            match = self._match(event, args)
        except Exception:
            return
        if match is None:
            return
        path, sink = match
        sink.append(_Hit(event, path, os.environ.get("PYTEST_CURRENT_TEST", "<collection>"),
                         self._stack()))
        raise RealRuntimePathTouched(f"{event} refused on a real runtime file: {path}")

    def _match(self, event, args):
        # Argument shapes are CPython's; an unset dir_fd arrives as -1.
        if event == "shutil.rmtree":                    # (path, dir_fd)
            top = self._real(args[0], args[1], by_name=False)
            if top is None:
                return None
            prefix = top.rstrip(os.sep) + os.sep        # an ancestor would take the file with it
            for path in (*self.protected, *self._extra, *self.trees, *self._extra_trees):
                if path == top or path.startswith(prefix):
                    return path, self._sink(path)
            if self._in_tree(top):                      # a directory inside a tree
                return top, self._sink(top)
            return None
        if event == "open":                             # (path, mode | None, flags)
            path, mode, flags = args
            if not ((isinstance(mode, str) and any(c in mode for c in "wax+"))
                    or flags & self._WRITE_FLAGS):
                return None
            candidates = ((path, None),)
        elif event == "sqlite3.connect":                # (database,)
            candidates = ((self._sqlite_file(args[0]), None),)
        elif event in ("os.rename", "os.link"):         # (src, dst, src_dir_fd, dst_dir_fd)
            candidates = ((args[0], args[2]), (args[1], args[3]))
        elif event == "os.symlink":                     # (src, dst, dir_fd): the link is the write
            candidates = ((args[1], args[2]),)
        elif event == "os.truncate":                    # (path | fd, length)
            candidates = ((args[0], None),)
        else:                           # os.remove (path, dir_fd), os.utime (path, times, ns, dir_fd)
            candidates = ((args[0], args[-1]),)
        for path, dir_fd in candidates:
            real = self._real(path, dir_fd)
            sink = self._sink(real) if real is not None else None
            if sink is not None:
                return real, sink
        return None

    def _real(self, path, dir_fd, by_name=True):
        if path is None or isinstance(path, int):
            return None                                 # an fd, or nothing to check
        path = os.fsdecode(os.fspath(path))
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(path):
            return None                 # fd-relative: shutil.rmtree internals, caught at its own event
        if (by_name and os.path.basename(path) not in self._names
                and not self._in_tree(os.path.abspath(path))):
            return None
        return os.path.realpath(path)

    def _sink(self, real):
        if real in self.protected:
            return self.hits
        if real in self._extra:
            return self._extra[real]
        # A guarding() tree before the session trees, so a test can trip the
        # wire inside the live mailbox on purpose.
        probe = real + os.sep
        for tree, hits in self._extra_trees.items():
            if probe.startswith(tree + os.sep):
                return hits
        if any(probe.startswith(tree + os.sep) for tree in self.trees):
            return self.hits
        return None

    @staticmethod
    def _sqlite_file(database):
        name = os.fsdecode(os.fspath(database))
        if name in ("", ":memory:"):
            return None
        if name.startswith("file:"):    # a URI: read-only and in-memory opens cannot write
            name, _, query = name[5:].partition("?")
            if {"mode=ro", "mode=memory", "immutable=1"} & set(query.split("&")):
                return None
        return name

    @staticmethod
    def _stack(depth=6):
        # Frame 2 is whoever made the audited call (0 is here, 1 is __call__).
        # No linecache: the hook does no file I/O of its own.
        frames, f = [], sys._getframe(2)
        while f is not None and len(frames) < depth:
            where = f.f_code.co_filename.removeprefix(f"{_REPO_ROOT}{os.sep}")
            frames.append(f"{where}:{f.f_lineno} {f.f_code.co_name}")
            f = f.f_back
        return tuple(frames)


_TRIPWIRE = _RuntimePathTripwire(_REAL_RUNTIME.values(), trees=tuple(_LIVE_MAILBOX_ROOTS))
sys.addaudithook(_TRIPWIRE)

# The auto tick's real seams, captured at import — before any fixture or test
# can rebind them — so the guard wraps the real functions, never a leftover patch.
# The state dir is the one usage_guard built from its ROOT, like _RUNTIME_ANCHORS.
_REAL_FETCH_USAGE = usage_guard._fetch_usage
_REAL_FETCH_USAGE_FOR = usage_guard._fetch_usage_for
_REAL_UG_STATE_PATH = usage_guard._state_path
_REAL_UG_STATE_DIR = os.path.realpath(usage_guard.ROOT / "Worker" / "_state" / "usage_guard")
_REAL_PULL_MAIN = _orch_mod._pull_main
_REAL_CHECK_DIVERGENCE = _orch_mod._check_divergence


class RealUsageFetchBlocked(RuntimeError):
    """Raised by the guard in place of a real usage fetch.

    Deliberately not ``never_swallow``: every caller is fail-open (``get_snapshot``
    records the error, ``usage_hold`` swallows it), and a test reaching the guard
    must see the same no-data path production takes when offline.
    """


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_usage_fetch: lift the conftest block on usage_guard._fetch_usage*")
    config.addinivalue_line(
        "markers", "real_vault_git: lift the conftest no-op on orchestrator._pull_main/"
                   "_check_divergence called without _run")


@pytest.fixture(autouse=True)
def _kakao_isolated(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-kakao_send.sh"
    monkeypatch.setattr(human, "KAKAO_SCRIPT", missing)
    monkeypatch.setattr(worker, "KAKAO_SEND_SCRIPT", missing)
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(tmp_path / "kakao-vault"))


@pytest.fixture(autouse=True)
def _usage_guard_not_disabled(monkeypatch):
    # The README quick start exports USAGE_GUARD_DISABLE=1. A developer who then
    # runs the suite in the same shell must not turn every pacing test green or
    # red by accident; tests that want the switch set it themselves.
    monkeypatch.delenv("USAGE_GUARD_DISABLE", raising=False)


@pytest.fixture(autouse=True)
def _ui_language_ko(monkeypatch):
    # The test vault (vault-template) leaves `language` unset, so the engine
    # default "en" would apply; the existing suites pin the Korean strings (the
    # author's vault sets language = "ko"). Tests of the en path or of the
    # resolver order override this with setenv/delenv.
    monkeypatch.setenv("MACP_LANGUAGE", "ko")


@pytest.fixture(autouse=True)
def _browser_watch_isolated(tmp_path, monkeypatch):
    # The probe script really exists on the HQ: without this, any test that
    # runs cmd_maintenance would spawn node and write the live state file.
    monkeypatch.setenv("MACP_BROWSER_DIR", str(tmp_path / "no-macp-browser"))
    monkeypatch.setenv("MACP_BROWSER_WATCH_DIR", str(tmp_path / "browser-watch"))


@pytest.fixture(autouse=True)
def _runtime_paths_isolated(tmp_path_factory, monkeypatch):
    # mktemp rather than tmp_path, so a test that lists its own tmp_path sees
    # nothing new. The redirect mirrors the vault layout, which is what makes
    # two rows naming one real file (cli.DB_PATH, metrics.DB) share a tmp file.
    redirect = tmp_path_factory.mktemp("macp-runtime")
    for module, attr in _RUNTIME_PATHS:
        target = _redirect_target(redirect, _REAL_RUNTIME[module, attr], _RUNTIME_ANCHORS[module])
        target.parent.mkdir(parents=True, exist_ok=True)   # sqlite3.connect won't
        monkeypatch.setattr(module, attr, target)
    # The mailbox root is cwd-relative, so there is no attribute to rebind:
    # every transport and every way of opening one goes through Mailbox.__init__.

    def _redirected_init(self, root, *args, **kwargs):
        real = os.path.realpath(root)
        for live, anchor in _LIVE_MAILBOX_ROOTS.items():
            if real == live or real.startswith(live + os.sep):
                root = redirect / os.path.relpath(real, anchor)
                root.mkdir(parents=True, exist_ok=True)     # GitMailbox runs git with cwd=root
                break
        _REAL_MAILBOX_INIT(self, root, *args, **kwargs)

    monkeypatch.setattr(mailbox.Mailbox, "__init__", _redirected_init)


@pytest.fixture(autouse=True)
def _disk_guard_isolated(tmp_path_factory, monkeypatch):
    # Switched off AND pointed at a tmp dir (see the module docstring). The dirs
    # are never created: a guard test that forgets a seam then measures /tmp and
    # finds no clones to walk, instead of walking the real ~/Worker.
    base = tmp_path_factory.mktemp("disk-guard")
    monkeypatch.setenv(disk_guard.KILL_ENV, "0")
    monkeypatch.setattr(disk_guard, "WORKER_ROOT", base / "Worker")
    monkeypatch.setattr(disk_guard, "ASSIGNMENTS_DIR", base / "assignments")
    monkeypatch.setattr(disk_guard, "JOBS_DIR", base / "jobs")
    monkeypatch.setattr(worktree_sweep, "REGISTRY_DIR", base / "gradle-registry")


@pytest.fixture(autouse=True)
def _auto_side_effects_isolated(request, tmp_path_factory, monkeypatch):
    # See the module docstring. A test's own monkeypatch/mock.patch runs after
    # this, so a test that patches any of these seams still wins.
    redirect = tmp_path_factory.mktemp("usage-guard")

    def _guarded_state_path():
        path = _REAL_UG_STATE_PATH()
        if os.path.realpath(path.parent) == _REAL_UG_STATE_DIR:
            return redirect / path.name
        return path

    monkeypatch.setattr(usage_guard, "_state_path", _guarded_state_path)
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(redirect))   # for spawned processes

    if request.node.get_closest_marker("real_usage_fetch") is None:
        def _blocked_fetch(*_args, **_kwargs):
            raise RealUsageFetchBlocked(
                "real usage fetch blocked by todo_system/conftest.py "
                "(mark the test real_usage_fetch to lift it)")

        monkeypatch.setattr(usage_guard, "_fetch_usage", _blocked_fetch)
        monkeypatch.setattr(usage_guard, "_fetch_usage_for", _blocked_fetch)

    if request.node.get_closest_marker("real_vault_git") is None:
        def _guarded_pull_main(root, _run=None, **kwargs):
            if _run is None:
                return None
            return _REAL_PULL_MAIN(root, _run, **kwargs)

        def _guarded_check_divergence(root, _run=None, *, threshold=3):
            if _run is None:
                return {
                    "ahead": 0, "behind": 0, "consecutive_behind": 0,
                    "threshold": threshold, "alert": False,
                    "updated": _dt.datetime.now().isoformat(timespec="seconds"),
                    "last_detail": "blocked by todo_system/conftest.py",
                }
            return _REAL_CHECK_DIVERGENCE(root, _run, threshold=threshold)

        monkeypatch.setattr(_orch_mod, "_pull_main", _guarded_pull_main)
        monkeypatch.setattr(_orch_mod, "_check_divergence", _guarded_check_divergence)


@pytest.fixture(scope="session", autouse=True)
def _real_runtime_untouched():
    """Fail the run on any tripwire record, or on a real queue.lock / todo.db
    that appeared or vanished while the suite ran.

    The heartbeat is left out of the existence check: the live runner creates
    and removes it between tasks by itself, so its existence flips on its own.
    The three notes are left out for the same reason: the sync daemon creates
    and regenerates them by itself. In the live vault the other two always
    exist, so their check cannot misfire.
    """
    watched = (_REAL_RUNTIME[worker, "QUEUE_LOCK_PATH"], _REAL_RUNTIME[cli, "DB_PATH"])
    before = [path.exists() for path in watched]
    yield
    flips = [f"{path} exists {was} -> {now}"
             for path, was, now in zip(watched, before, [p.exists() for p in watched])
             if was != now]
    hits = [f"{h.event} {h.path}\n    in {h.test}\n      " + "\n      ".join(h.stack)
            for h in _TRIPWIRE.hits]
    assert not hits and not flips, (
        "tests reached this checkout's real runtime files:\n  " + "\n  ".join(hits + flips))


@pytest.fixture
def real_runtime_paths():
    """The unredirected values, keyed like the ``_RUNTIME_PATHS`` rows."""
    return _REAL_RUNTIME


@pytest.fixture
def real_runtime_anchors():
    """The vault root each ``_RUNTIME_PATHS`` module built its rows from."""
    return _RUNTIME_ANCHORS


@pytest.fixture
def runtime_redirect_target():
    """``(redirect, real, anchor) -> tmp path`` — the mapping the redirect uses."""
    return _redirect_target


@pytest.fixture
def runtime_tripwire():
    return _TRIPWIRE


@pytest.fixture
def real_auto_side_effects():
    """The unguarded auto-tick seams and the real usage_guard state dir."""
    return types.MappingProxyType({
        "fetch_usage": _REAL_FETCH_USAGE,
        "fetch_usage_for": _REAL_FETCH_USAGE_FOR,
        "ug_state_path": _REAL_UG_STATE_PATH,
        "ug_state_dir": _REAL_UG_STATE_DIR,
        "pull_main": _REAL_PULL_MAIN,
        "check_divergence": _REAL_CHECK_DIVERGENCE,
    })
