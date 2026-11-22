"""Mailbox transports for the orchestrator.

Layout (under a single shared root, e.g. Worker/agents/):

    <root>/<agent_id>/inbox/<msg_id>.json   # immutable messages TO <agent_id>
    <root>/_state/<agent_id>/cursor.json    # per-reader consumed set + ack
                                            # tombstones (local)

Everyone — including the orchestrator — is just an <agent_id> with an inbox, so
a reply is simply a message addressed `to="orchestrator"`. One code path,
symmetric in both directions.

Transports:
  - FileMailbox: shared filesystem (local, low-latency).
  - GitMailbox : the vault git repo is the transport (durable, offline-tolerant,
    reaches remote hosts and a future cloud orchestrator).

Stdlib-only. `Worker/agents/_state/` is gitignored runtime.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from abc import ABC
from pathlib import Path
from typing import Iterable

from ..vault_git_lock import vault_git_lock
from .atomic_io import atomic_write_json
from .message import Envelope
from .stages import GIT_TIMEOUT_RC, _git_stream

DEFAULT_ROOT = "Worker/agents"

# Every GitMailbox git call gets a wall-clock ceiling. An unbounded pull/push on a
# wedged remote or a stalled credential helper never returns, and the orchestrator
# tick that called receive()/send() wedges with it. Same values as stages.py's
# ceilings; read at call time so tests can shrink them.
GIT_TIMEOUT_LOCAL_S = 180       # add / commit / diff / remote / rebase --abort
GIT_TIMEOUT_NETWORK_S = 300     # pull / push / fetch
_NETWORK_SUBCOMMANDS = frozenset({"pull", "push", "fetch"})

# How long a receive()/send() waits for the vault git lock (vault_git_lock.py)
# before it skips its pull or its commit+push. Shorter than the tick-level
# waits: the runner polls receive() often, and a skipped send leaves its file on
# disk for the next vault sync to commit. Read at call time so tests can shrink it.
GIT_LOCK_TIMEOUT_S = 15.0

# An acked file that ack() deleted is remembered as a tombstone {msg_id: acked_at}.
# The deletion is never committed by the reader, so any git operation that restores
# the tree (a stage agent's `git restore`/`stash`/`reset`, a failed autostash, an
# older branch checked out) brings the identical envelope back — with no claim and
# no `seen` entry it used to look brand new and re-ran a whole pipeline. The window
# lasts until a sync commit lands or as long as an old worker branch is checked
# out; 14 days covers branch lifetimes, the cap bounds the cursor file.
ACK_TOMBSTONE_TTL_S = 14 * 86400
ACK_TOMBSTONE_MAX = 20000


def _git_timeout(args: tuple[str, ...] | list[str]) -> float:
    """Network ceiling if `args` names a remote-talking subcommand, else local.

    Not `stages._default_git_timeout`: that takes the first non-dash token as the
    subcommand, so `-c rebase.autoStash=true pull …` would be graded by
    `rebase.autoStash=true` and get the local ceiling. Here a `-c <val>` pair is
    skipped before looking for the subcommand."""
    it = iter(args)
    for a in it:
        if a == "-c":
            next(it, None)
            continue
        if a.startswith("-"):
            continue
        return GIT_TIMEOUT_NETWORK_S if a in _NETWORK_SUBCOMMANDS else GIT_TIMEOUT_LOCAL_S
    return GIT_TIMEOUT_LOCAL_S


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Mailbox(ABC):
    """A shared message store addressable by agent id."""

    def __init__(self, root: Path):
        self.root = Path(root)

    # -- addressing -------------------------------------------------------
    def _inbox(self, agent_id: str) -> Path:
        return self.root / agent_id / "inbox"

    def _cursor_path(self, agent_id: str) -> Path:
        return self.root / "_state" / agent_id / "cursor.json"

    def init_agent(self, agent_id: str) -> None:
        self._inbox(agent_id).mkdir(parents=True, exist_ok=True)
        (self._inbox(agent_id) / ".gitkeep").touch()

    # -- cursor (per-reader consumed set; local, not shared) --------------
    def _load_cursor(self, agent_id: str) -> tuple[set[str], dict[str, float]]:
        """(`seen`, `acked` tombstones). A cursor written before tombstones
        existed reads as no tombstones; a non-numeric timestamp is dropped."""
        p = self._cursor_path(agent_id)
        if not p.exists():
            return set(), {}
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return set(), {}
        if not isinstance(data, dict):
            return set(), {}
        raw = data.get("acked")
        acked = {str(k): float(v) for k, v in raw.items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)
                 } if isinstance(raw, dict) else {}
        return set(data.get("seen", [])), acked

    def _save_cursor(self, agent_id: str, seen: set[str],
                     acked: dict[str, float]) -> None:
        atomic_write_json(self._cursor_path(agent_id),
                          {"seen": sorted(seen), "acked": dict(sorted(acked.items()))})

    def _load_seen(self, agent_id: str) -> set[str]:
        return self._load_cursor(agent_id)[0]

    def _save_seen(self, agent_id: str, seen: set[str]) -> None:
        # keeps the tombstones: rewriting `seen` must not resurrect anything
        self._save_cursor(agent_id, seen, self._load_cursor(agent_id)[1])

    @staticmethod
    def _prune_tombstones(acked: dict[str, float], now: float) -> dict[str, float]:
        live = {k: v for k, v in acked.items() if now - v <= ACK_TOMBSTONE_TTL_S}
        if len(live) > ACK_TOMBSTONE_MAX:
            newest = sorted(live.items(), key=lambda kv: (kv[1], kv[0]))[-ACK_TOMBSTONE_MAX:]
            live = dict(newest)
        return live

    # -- transport hooks (git overrides these) ----------------------------
    def _before_receive(self, agent_id: str) -> None:  # pull
        pass

    def _after_send(self, msg: Envelope, path: Path) -> None:  # commit/push
        pass

    # -- public API -------------------------------------------------------
    def can_send(self) -> str | None:
        """Why a `send` now would NOT reach its recipient, or None when it would.
        Local transports always deliver."""
        return None

    def send(self, msg: Envelope) -> Path:
        """Deliver `msg` into its recipient's inbox."""
        inbox = self._inbox(msg.to)
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{msg.msg_id}.json"
        path.write_text(msg.to_json(), encoding="utf-8")
        self._after_send(msg, path)
        return path

    def receive(self, agent_id: str) -> list[Envelope]:
        """Return unacked messages for `agent_id`, FIFO by msg_id (ULID time
        order). At-least-once: a message stays returned until `ack`ed."""
        self._before_receive(agent_id)
        inbox = self._inbox(agent_id)
        if not inbox.exists():
            return []
        seen, acked = self._load_cursor(agent_id)
        msgs: list[Envelope] = []
        for f in sorted(inbox.glob("*.json")):
            if f.stem in seen or f.stem in acked:
                continue
            try:
                msgs.append(Envelope.from_json(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue  # skip half-written file; picked up next pass
        return msgs

    def ack(self, agent_id: str, msgs: Envelope | Iterable[Envelope]) -> None:
        """Mark message(s) consumed so they are not redelivered.

        An id whose file is deleted moves from `seen` to a timestamped tombstone
        in the same atomic cursor write, so it is durable before any later git
        restore can bring the file back (see ACK_TOMBSTONE_TTL_S)."""
        if isinstance(msgs, Envelope):
            msgs = [msgs]
        seen, acked = self._load_cursor(agent_id)
        seen.update(m.msg_id for m in msgs)
        # Reclaim consumed files, so the inbox dir does not grow unbounded over the
        # lifetime of a long-running orchestrator. Idempotent: safe to sweep the
        # whole set. Sweeping the tombstones too re-deletes a resurrected file.
        gone = self._after_ack(agent_id, seen | set(acked))
        now = time.time()
        for mid in gone & seen:
            acked.setdefault(mid, now)
        seen -= gone
        # TTL + cap keep the tombstone map bounded, as `seen` was before.
        self._save_cursor(agent_id, seen, self._prune_tombstones(acked, now))

    def _after_ack(self, agent_id: str, acked_ids: set[str]) -> set[str]:
        """Delete the inbox files for consumed ids; return the ids whose file is
        now gone (caller moves them from `seen` to tombstones). FileMailbox
        default."""
        inbox = self._inbox(agent_id)
        gone: set[str] = set()
        for mid in acked_ids:
            f = inbox / f"{mid}.json"
            if not f.exists():
                gone.add(mid)
            else:
                try:
                    f.unlink()
                    gone.add(mid)
                except OSError:
                    pass  # keep in `seen` so it is not redelivered
        return gone


# ---------------------------------------------------------------------------
# FileMailbox — plain shared filesystem
# ---------------------------------------------------------------------------


class FileMailbox(Mailbox):
    pass


# ---------------------------------------------------------------------------
# GitMailbox — the vault git repo is the transport
# ---------------------------------------------------------------------------


class GitMailbox(Mailbox):
    """Message files are synced through the vault git repo.

    receive() pulls first; send() commits+pushes the new message. Remote ops
    are best-effort and no-op when there is no `origin` (so it also works on a
    fresh local repo, e.g. in tests). Every git call is wall-clock bounded; a
    timeout comes back as rc 124 (`GIT_TIMEOUT_RC`), not an exception.

    Both run under the vault git lock (vault_git_lock.py), the one the
    orchestrator's `_pull_main` and the worker's `sync_vault_state` hold too —
    taken in the two hooks only. `_pull` and `_commit_push` stay unlocked on
    purpose: `_commit_push` retries a rejected push through `_pull`, and the
    lock is not re-entrant, so a `_pull` that locked would wait on its own
    caller until the timeout.
    """

    def __init__(self, root: Path, branch: str = "main"):
        super().__init__(root)
        self.branch = branch
        self.repo_dir = self._toplevel(self.root)

    def _after_ack(self, agent_id: str, acked_ids: set[str]) -> set[str]:
        # Files are git-synced; a plain unlink would be restored by the next pull
        # (or churn the tree). Leave them and keep tracking in the cursor — same
        # behaviour as before, so the durable transport is unaffected.
        return set()

    # -- git plumbing -----------------------------------------------------
    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        timeout = _git_timeout(args)
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=self.repo_dir,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            # A return value, not a raise: every caller here judges git by
            # returncode, so a wedged remote takes the ordinary failure path.
            partial = _git_stream(e.stderr)
            if partial and not partial.endswith("\n"):
                partial += "\n"
            r = subprocess.CompletedProcess(
                args=["git", *args], returncode=GIT_TIMEOUT_RC,
                stdout=_git_stream(e.stdout),
                stderr=partial + f"git timeout after {timeout}s: git {' '.join(args)}\n")
        if check:
            r.check_returncode()  # a timeout must not turn check=True into success
        return r

    @staticmethod
    def _toplevel(root: Path) -> Path:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=root if root.exists() else root.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_LOCAL_S,
            )
        except subprocess.TimeoutExpired:
            return root
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
        return root  # not a repo (tests may run before init); best-effort

    _CAN_SEND_TTL_S = 10.0

    def can_send(self) -> str | None:
        """A git mailbox publishes by committing on `self.branch` and pushing it.
        When the checkout holding the mailbox sits on another branch (the live
        vault while a worker run owns it), the commit is refused or lands off
        `self.branch` and the message never reaches the remote (2026-11-17: a
        TASK_ASSIGN to `macbook` stayed staged on a worker branch). Cached briefly:
        the router asks once per candidate task per tick."""
        now = time.monotonic()
        cached = getattr(self, "_can_send_cache", None)
        if cached and now - cached[0] < self._CAN_SEND_TTL_S:
            return cached[1]
        why: str | None = None
        r = self._git("branch", "--show-current")
        if r.returncode == 0:
            head = r.stdout.strip()
            if head != self.branch:
                why = (f"git mailbox checkout {self.repo_dir} is on "
                       f"'{head or 'detached HEAD'}', not '{self.branch}'")
        self._can_send_cache = (now, why)
        return why

    def _has_origin(self) -> bool:
        return "origin" in self._git("remote").stdout.split()

    def _pull(self) -> None:
        if not self._has_origin():
            return
        # -c rebase.autoStash works on older gits (Ubuntu 18.04 / 2.17), unlike
        # the --autostash flag.
        r = self._git(
            "-c", "rebase.autoStash=true", "pull", "--rebase", "origin", self.branch
        )
        # A timed-out pull (rc 124) takes this branch too: abort, use local state,
        # and the next receive() pulls again.
        if r.returncode != 0:
            self._git("rebase", "--abort")  # leave a clean tree, use local state

    def _commit_push(self, path: Path) -> None:
        # Resolve to survive symlinked roots (macOS /var -> /private/var):
        # git rev-parse returns the resolved toplevel, tempfile roots do not.
        rel = path.resolve().relative_to(self.repo_dir)
        self._git("add", str(rel))
        if self._git("diff", "--cached", "--quiet").returncode == 0:
            return  # nothing staged (e.g. already committed)
        self._git("commit", "-m", f"chore(mailbox): {path.stem}")
        if not self._has_origin():
            return
        for _ in range(3):  # push with rebase-retry, like sync_push
            r = self._git("push", "origin", self.branch)
            if r.returncode == 0:
                return
            if r.returncode == GIT_TIMEOUT_RC:
                # A wedged remote must not burn 3x(push+pull) ceilings (~30 min).
                # The commit stays local and goes out with the next push.
                return
            self._pull()

    # -- hooks ------------------------------------------------------------
    # The vault git lock is taken here and only here (see the class docstring):
    # these two hooks are the only way into _pull/_commit_push outside tests.
    # Busy lines go to stderr so the mailbox CLI's stdout stays its own output.
    def _before_receive(self, agent_id: str) -> None:
        with vault_git_lock(self.repo_dir, GIT_LOCK_TIMEOUT_S) as held:
            if not held:
                print("mailbox: vault git lock busy — skipping pull, "
                      "reading local inbox state", file=sys.stderr)
                return
            self._pull()

    def _after_send(self, msg: Envelope, path: Path) -> None:
        with vault_git_lock(self.repo_dir, GIT_LOCK_TIMEOUT_S) as held:
            if not held:
                print(f"mailbox: vault git lock busy — {path.name} left on disk "
                      "for the next vault sync", file=sys.stderr)
                return
            self._commit_push(path)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def open_mailbox(uri: str = f"git://{DEFAULT_ROOT}", base: Path | None = None) -> Mailbox:
    """Open a mailbox from a `transport://path` uri.

    - git://<path>  -> GitMailbox   (default; path is the mailbox root)
    - file://<path> -> FileMailbox

    `path` may be relative (resolved against `base`, default cwd) or absolute.
    """
    if "://" in uri:
        scheme, path = uri.split("://", 1)
    else:
        scheme, path = "git", uri
    root = Path(path)
    if not root.is_absolute():
        root = (base or Path.cwd()) / root
    if scheme == "file":
        return FileMailbox(root)
    if scheme == "git":
        return GitMailbox(root)
    raise ValueError(f"unknown mailbox transport: {scheme!r}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from . import message as m

    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.mailbox")
    p.add_argument("--uri", default=f"git://{DEFAULT_ROOT}", help="transport://root")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-agent", help="Create an agent inbox")
    p_init.add_argument("--id", required=True)

    p_send = sub.add_parser("send", help="Send a message")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--type", required=True, choices=sorted(m.ALL_TYPES))
    p_send.add_argument("--from", dest="frm", default=m.ORCHESTRATOR)
    p_send.add_argument("--task")
    p_send.add_argument("--corr")
    p_send.add_argument("--payload", default="{}", help="JSON object")

    p_recv = sub.add_parser("recv", help="Receive (and optionally ack) messages")
    p_recv.add_argument("--agent", required=True)
    p_recv.add_argument("--ack", action="store_true")

    args = p.parse_args(argv)
    mb = open_mailbox(args.uri)

    if args.cmd == "init-agent":
        mb.init_agent(args.id)
        print(f"initialized inbox for {args.id}")
        return 0

    if args.cmd == "send":
        msg = Envelope(
            to=args.to,
            type=args.type,
            frm=args.frm,
            task_id=args.task,
            corr_id=args.corr,
            payload=json.loads(args.payload),
        )
        path = mb.send(msg)
        print(f"sent {msg.type} to {msg.to}: {msg.msg_id}")
        print(f"  -> {path}")
        return 0

    if args.cmd == "recv":
        msgs = mb.receive(args.agent)
        for msg in msgs:
            print(f"[{msg.type}] {msg.msg_id} from={msg.frm} task={msg.task_id}")
            if msg.payload:
                print(f"    payload={json.dumps(msg.payload, ensure_ascii=False)}")
        if args.ack and msgs:
            mb.ack(args.agent, msgs)
            print(f"acked {len(msgs)} message(s)")
        elif not msgs:
            print("(no new messages)")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
