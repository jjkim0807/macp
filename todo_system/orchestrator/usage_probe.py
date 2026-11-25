"""Best-effort token-usage probe for the two shell-driven `claude -p` callers.

`scripts/planner_tick.sh` and `scripts/brief.sh` invoke `claude -p` directly —
outside the Python stage loop that already records usage via
`stages.record_stage_usage`. This module is the seam that gives those two the same
Design-B instrumentation *without a second parser*: the caller adds a fresh
`--session-id <sid>` per invocation, and after the CLI exits calls

    <py> -m todo_system.orchestrator.usage_probe record \\
        --source <s> --dest <path.jsonl> --session-id <sid> --rc <int> \\
        [--duration-ms <int>] [--model <m>]

`record` hands a synthetic `[--session-id <sid>(, --model <m>)]` argv to
`stages.stage_usage`, which extracts the sid, reads that session's transcript back
out of the CLI's own JSONL, and returns either the nine `stages.USAGE_KEYS` or a
`{"unavailable": "<why>"}` marker. That record is appended as one JSON line to an
untracked state file (`Worker/_state/*.jsonl`, git-ignored — so it never stalls
the vault-sync queue).

Best-effort by contract: the record body never raises and the process always
exits 0, so a token count can never fail the script that produced it. When the
argv carries no session id (`stage_usage` returns None), nothing is written.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path


def _record(*, source: str, dest: str, session_id: str, rc: int | None,
            duration_ms: int | None, model: str | None) -> None:
    # Lazy import: the kill-switch decision is made in pure shell, so the hot
    # path (switch off) never pays to load the orchestrator package — and if that
    # package cannot import at all (a bare python3 without the project env), the
    # enclosing try in main() turns it into "record nothing", not a crash.
    from todo_system.orchestrator import stages

    argv = [stages.SESSION_ID_FLAG, session_id]
    if model:
        # Only steers transcript_usage's model tie-break (model_hint); never the
        # session lookup, which keys off --session-id alone.
        argv += ["--model", model]
    rec = stages.stage_usage(argv, returncode=rc, duration_ms=duration_ms)
    if rec is None:
        # No session id survived argv_session_id (kill switch off, or an empty /
        # non-canonical sid): there is nothing to record. Mirrors the stage loop.
        return
    row = {"source": source,
           "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           **rec}
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="usage_probe")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser(
        "record", help="append one claude -p usage record to a JSONL state file")
    rec.add_argument("--source", required=True)
    rec.add_argument("--dest", required=True)
    rec.add_argument("--session-id", dest="session_id", required=True)
    rec.add_argument("--rc", type=int, default=None)
    rec.add_argument("--duration-ms", dest="duration_ms", type=int, default=None)
    rec.add_argument("--model", default=None)
    args = parser.parse_args(argv)
    if args.cmd == "record":
        try:
            _record(source=args.source, dest=args.dest,
                    session_id=args.session_id, rc=args.rc,
                    duration_ms=args.duration_ms, model=args.model)
        except Exception:
            # Best-effort: a token count is never worth failing the caller. Any
            # reader/import/disk error becomes a silent no-record.
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
