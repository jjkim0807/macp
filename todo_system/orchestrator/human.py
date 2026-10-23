"""Human agent adapter — the 'runner' for a human.

A human is a first-class agent that may respond on an unbounded schedule. This
module (Mac-side) does two things:

  deliver: pull the human's inbox and nudge them via KakaoTalk (kakao_send.sh).
           The assignment is NOT considered done — it just got delivered. Humans
           are never timed out to failure; parking/re-nudge is the tracker's job.
  reply  : the human runs this to answer an assignment; it emits a RESULT /
           PROGRESS / NEEDS_INPUT message back to the orchestrator's inbox.

Delivery is idempotent-ish: a message is nudged once then acked. Re-nudge with
backoff (P3 tracker) is separate from first delivery.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from . import message as m
from .mailbox import DEFAULT_ROOT, Mailbox, open_mailbox
from .registry import Agent, load_registry
# Single source of the length budget + the self-limiting composers. Re-exported
# here so orchestrator-side callers keep the one name `human.KAKAO_TEXT_LIMIT`;
# the stdlib-only launchd scripts import the leaf directly (they cannot import
# this module — it pulls PyYAML via .registry).
from ..kakao_format import (  # noqa: F401
    KAKAO_TEXT_LIMIT,
    LEVEL_DIGEST,
    clip_words,
    compose_alert,
)
from .. import kakao_format as _kakao_format
from .. import i18n, paths

# Two roots, split the way beta_publish._ENGINE_ROOT / _VAULT_DATA_ROOT are.
# _ENGINE_ROOT comes from paths.engine_root() (the repo root in a development
# checkout, todo_system/_engine in an installed wheel) and is immune to
# MACP_VAULT_ROOT: it anchors scripts/kakao_send.sh and the subprocess cwd below
# — engine files that ship with the code, never vault data. _VAULT_DATA_ROOT
# resolves through the vault resolver so a redirected vault (public-engine /
# vault split, remote clones) is read from the right place; anchored at
# paths.vault_search_start() — the engine root in a checkout, byte-identical to
# _ENGINE_ROOT when the env is unset (the live config); None in an installed
# wheel, where the cwd-upward search replaces a site-packages anchor. It
# anchors the Worker/assignments ledger in _ledger_corr,
# which the inventory (Notes/macp-public-split-inventory.md §2.1) classifies as
# vault-data — the runtime SoT — and which metrics._ROOT already reads from the
# vault (PR #168). paths is stdlib-only, so this import keeps the module's import
# footprint unchanged (it already pulls PyYAML via .registry).
_ENGINE_ROOT = paths.engine_root()
_VAULT_DATA_ROOT = paths.vault_root(start=paths.vault_search_start())
KAKAO_SCRIPT = _ENGINE_ROOT / "scripts" / "kakao_send.sh"

# notify(text, link) -> None
Notifier = Callable[[str, "str | None"], None]

# i18n keys of each message type's nudge head; any other type gets
# "human.head.other". Resolved per call so the nudge follows ui_language().
_HEAD_KEYS = {
    m.TASK_ASSIGN: "human.head.assign",
    m.QUERY: "human.head.query",
    m.CANCEL: "human.head.cancel",
    m.WAKE: "human.head.wake",
    m.PING: "human.head.ping",
}


def compose_kakao(msg: m.Envelope, agent: Agent) -> str:
    """Compose a self-limiting phone nudge for a human's inbox message.

    The headline (what happened + the ask) always goes out whole; the task id
    identifies WHICH task on line 2; the free-text summary — which may be a long
    English outcome — rides along as a word-clipped tail, never a blind cut.
    Length is <= KAKAO_TEXT_LIMIT by construction (the old `text[:179]` bisected
    the summary mid-token; see macp-kakao-message-silent-truncation)."""
    p = msg.payload or {}
    head = i18n.t(_HEAD_KEYS.get(msg.type, "human.head.other"))
    headline = i18n.t("human.headline", head=head)
    summary = (
        p.get("summary")
        or p.get("outcome")
        or p.get("next_action")
        or p.get("note")
        or ""
    )
    lines = [f"📋 {msg.task_id}"] if msg.task_id else []
    return compose_alert(headline, lines, tail=summary)


def kakao_notifier(text: str, link: str | None = None, *,
                   level: str | None = None, event_key: str | None = None) -> None:
    """Default notifier — fire kakao_send.sh. Non-fatal, like the script itself.

    `level` / `event_key` are the opt-in noise tiers (scripts/KAKAO_ALERTS.md);
    both None — every pre-existing caller — is an immediate send with no routing
    state touched. digest/silent never spawn; routing fails open to a send."""
    if level or event_key:
        try:
            verdict, routed = _kakao_format.route(text, level=level, event_key=event_key)
        except Exception:
            verdict, routed = "send", text
        if verdict != "send":
            return
        text = routed if routed is not None else text
    if not KAKAO_SCRIPT.exists():
        return
    args = [str(KAKAO_SCRIPT), text] + ([link] if link else [])
    try:
        subprocess.run(args, cwd=str(_ENGINE_ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=30)
    except Exception:
        pass


def deliver_pending(
    mailbox: Mailbox,
    agent: Agent,
    notify: Notifier = kakao_notifier,
    dry_run: bool = False,
    route=None,
) -> list[m.Envelope]:
    """Nudge the human about every new inbox message, then ack the delivery.

    TASK_ASSIGN deliveries are digest-tier: the 2026-09-16 audit found every
    "[할당]" ping was an FYI duplicating the review-request sent seconds earlier
    (5/5 in the 09-11..16 log), so they queue for the daily summary instead of
    ringing the phone. Everything else a human gets pushed (QUERY, CANCEL, WAKE,
    PING) stays an immediate send — when in doubt, immediate. The route seam is
    injectable for tests; it falls open to `notify` so a routing failure can
    never swallow a delivery."""
    route = route or _kakao_format.route
    msgs = mailbox.receive(agent.id)
    delivered: list[m.Envelope] = []
    for msg in msgs:
        text = compose_kakao(msg, agent)
        if dry_run:
            print(f"[dry-run kakao -> {agent.id}] {text!r}")
        elif msg.type == m.TASK_ASSIGN:
            try:
                verdict, routed = route(text, level=LEVEL_DIGEST,
                                        event_key=f"assign:{msg.task_id or '-'}")
            except Exception:
                verdict, routed = "send", text
            if verdict == "send":          # routing failed open — deliver, don't lose
                notify(routed if routed is not None else text, None)
        else:
            notify(text, None)
        delivered.append(msg)
    if delivered and not dry_run:
        mailbox.ack(agent.id, delivered)  # delivered != done
    return delivered


_STATUS_MAP = {
    "done": m.RESULT,
    "progress": m.PROGRESS,
    "needs-input": m.NEEDS_INPUT,
    "accept": m.ACCEPT,
    "reject": m.REJECT,
}


def reply(
    mailbox: Mailbox,
    agent_id: str,
    task_id: str,
    status: str,
    note: str = "",
    corr_id: str | None = None,
) -> m.Envelope:
    """Human answers an assignment: send RESULT/PROGRESS/NEEDS_INPUT to orchestrator."""
    env = m.Envelope(
        to=m.ORCHESTRATOR,
        type=_STATUS_MAP[status],
        frm=agent_id,
        task_id=task_id,
        reply_to=agent_id,
        corr_id=corr_id,
        payload={"note": note} if note else {},
    )
    mailbox.send(env)
    return env


def _ledger_corr(task_id: str) -> str | None:
    """The assignment's current corr_id, or None if there is no row to answer.

    `collect()` drops a reply whose corr_id does not match the ledger row, and the
    drop is silent to the sender: the human sees `replied REJECT ...`, the ledger
    never moves, and the task stays parked. Since a human is always answering an
    EXISTING assignment, the row's own corr_id is the right default — passing
    `--corr` by hand was a required step that nothing told you about (2026-09-16:
    two PR-review un-parks were dropped this way).
    """
    try:
        from .assignment import Ledger

        # Worker/assignments = the runtime ledger (SoT) — vault-data, so it resolves
        # through _VAULT_DATA_ROOT and follows MACP_VAULT_ROOT, aligned with
        # metrics._ROOT (PR #168) and the inventory §2.1 classification. NOT the
        # engine root: a redirected vault must read its own ledger, not the install's.
        a = Ledger(_VAULT_DATA_ROOT / "Worker" / "assignments").load(task_id)
        return a.corr_id if a else None
    except Exception:
        return None


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.human")
    p.add_argument("--uri", default=f"git://{DEFAULT_ROOT}")
    p.add_argument("--registry", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("deliver", help="Deliver a human's pending inbox via kakao")
    pd.add_argument("--agent", required=True)
    pd.add_argument("--dry-run", action="store_true")

    pr = sub.add_parser("reply", help="Human answers an assignment")
    pr.add_argument("--agent", required=True)
    pr.add_argument("--task", required=True)
    pr.add_argument("--status", required=True, choices=sorted(_STATUS_MAP))
    pr.add_argument("--note", default="")
    pr.add_argument("--corr", default=None)

    args = p.parse_args(argv)
    mb = open_mailbox(args.uri)
    reg = load_registry(args.registry)

    if args.cmd == "deliver":
        agent = reg.get(args.agent)
        if not agent.is_human:
            print(f"error: {agent.id} is not a human agent (kind={agent.kind})")
            return 2
        delivered = deliver_pending(mb, agent, dry_run=args.dry_run)
        print(f"delivered {len(delivered)} message(s) to {agent.id}"
              + (" (dry-run, not acked)" if args.dry_run and delivered else ""))
        return 0

    if args.cmd == "reply":
        corr = args.corr if args.corr else _ledger_corr(args.task)
        env = reply(mb, args.agent, args.task, args.status, args.note, corr)
        print(f"replied {env.type} for task '{args.task}' -> orchestrator "
              f"({env.msg_id}, corr={env.corr_id})")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
