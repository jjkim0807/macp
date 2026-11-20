"""Durable pickup for report-stage follow-ups.

The report stage names follow-ups in free prose — `## 다음 단계` (Next steps) and
`## 사람이 검토해야 할 부분` (Needs human review) in report.md — and, until now,
nothing carried them forward. The planner only ever saw them if they happened to ride along in a
`run_completed` trigger's context (PR #79/#80 were luck, not design), and its
own execution steps (worker-planner.md 1–12) never read Worker/runs at all. So a
follow-up that missed that one scan window evaporated: a 2026-08-26 audit found 3
in a single week (the 08-19 blocked-dispatch report sat 7 days before a human
re-filed it by hand, the 16b Google-signin round-trip the same, the 08-25
walkforward re-measure).

This module closes that path by making the follow-ups DURABLE. The runner appends
them to `Worker/_state/followups.jsonl` the instant the report is read
(append_followups), the planner tick injects the still-open ones into every
planner invocation (render_context / `emit-context`), and whatever the planner
DID with one — created a task stamped `from_followup: <id>`, or recorded a reason
in `followups_rejected.jsonl` — is reconciled back so it stops re-appearing
(reconcile). An unhandled follow-up simply stays open across ticks, which is the
whole point: durable-until-resolved beats silently-gone.

A rejection that needed a person used to vanish the same way (by 2026-09-19, 75
of 170 rejection lines mentioned a human or a decision and nobody was told), so
from 2026-09-19 a line carrying `"route": "human"` parked the row behind a kakao
digest and a human-decision CLI. The 2026-11-20 decision (ask a person only about
irreversible actions) turned that park into a record: the line now closes the row
like any rejection and leaves one "확인 못 함" line in the source task's Progress
Log (_record_unverified) — no alert, no waiting.

Because the rejections file is append-only and read last-wins, each row keeps a
`rejections_seen` watermark and reconcile reads only the lines past it, so a line
already consumed (including one that parked a row under the old human path) is
never applied twice. Rows written before the field existed count as 0.

Stdlib-only and best-effort throughout — recording a follow-up must never break a
pipeline that otherwise finished, so every write swallows OSError and every read
of a corrupt line skips it rather than raising.

Paths are module globals (not import-time-frozen defaults) so tests can
monkeypatch `FOLLOWUPS_PATH` / `ITEMS_DIR` / `REJECTIONS_PATH` onto a tempdir —
the same idiom `stages.RUNS_DIR` uses, and necessary because the real paths are
gitignored live state that a test must never touch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

from .. import paths

# Live runtime state, under Worker/_state/ (gitignored) and Todo/items/ (git-tracked but host-local).
# Reassigned wholesale by tests; every function that defaults to one reads the
# CURRENT module global at call time (path=None → resolve inside), never a value
# captured in a default argument.
# _ROOT resolves through paths.vault_root so these follow MACP_VAULT_ROOT (the
# metrics._ROOT shape), instead of the env-immune engine-root alias that stages
# still exposes; byte-identical to the old value when the env is unset. start=
# is paths.vault_search_start(): in a checkout the engine root (the former
# parents[2]), so a cwd-upward .macp never captures it; None in an installed
# wheel, where the cwd-upward search replaces a site-packages anchor.
_ROOT = paths.vault_root(start=paths.vault_search_start())
FOLLOWUPS_PATH = _ROOT / "Worker" / "_state" / "followups.jsonl"
REJECTIONS_PATH = _ROOT / "Worker" / "_state" / "followups_rejected.jsonl"
ITEMS_DIR = _ROOT / "Todo" / "items"
CLI = "python -m todo_system.orchestrator.followups"

# The report.md sections whose bullets are follow-ups. Both are captured on
# purpose (Research Open-Q): "다음 단계" is the obvious one, but a
# "사람이 검토해야 할 부분" note is exactly the kind of thing that goes unowned.
# Each tuple is one section's accepted headings: [0] is the canonical (Korean)
# name and is what gets recorded; the rest are English aliases, matched
# case-insensitively, so an English report yields the same records and ids.
NEXT_STEPS_HEADINGS = ("다음 단계", "Next steps")
HUMAN_REVIEW_HEADINGS = ("사람이 검토해야 할 부분", "Needs human review")
FOLLOWUP_SECTIONS = (NEXT_STEPS_HEADINGS[0], HUMAN_REVIEW_HEADINGS[0])

# A bullet whose text is (or opens with) one of these sentinels is the section's
# "nothing here" marker, not a follow-up — the report template writes
# `- bullet (if absent, "없음")`. `none` must stand as a whole word, so a real bullet
# like "nonexistent path …" is kept.
EMPTY_MARKERS = ("없음", "none")
_EMPTY_MARKER = EMPTY_MARKERS[0]


def section_body_any(text: str, headings: tuple[str, ...]) -> str | None:
    """The block under the first of `headings` present in `text` (see _section_body).

    Tried in tuple order, so the canonical heading wins when several appear.
    Aliases after [0] ignore case; [0] keeps the exact case-sensitive match."""
    for i, heading in enumerate(headings):
        pat = re.escape(heading) if i == 0 else f"(?i:{re.escape(heading)})"
        m = re.search(
            rf"^##[ \t]+{pat}[ \t]*$(.*?)(?=^#{{1,2}}[ \t]|\Z)",
            text or "", re.M | re.S)
        if m:
            return m.group(1)
    return None


def is_empty_marker(item: str) -> bool:
    """True when a bullet's text is a "nothing here" sentinel (EMPTY_MARKERS)."""
    return (item.startswith(EMPTY_MARKERS[0])
            or re.match(rf"{EMPTY_MARKERS[1]}\b", item, re.I) is not None)


def _section_body(text: str, section: str) -> str | None:
    """The raw block under `## <section>`, up to the next `#`/`##` header or EOF.

    None when the header is absent (distinct from present-but-empty)."""
    return section_body_any(text, (section,))


def parse_report_followups(text: str) -> list[dict]:
    """The follow-up bullets in a report.md, as `[{"section", "text"}, ...]`.

    Only TOP-LEVEL bullets (no leading whitespace) count — a nested sub-bullet is
    elaboration on its parent, not an item of its own, and `re.match` on `[-*]`
    naturally drops any line that starts with a space. Blank and `없음`/`none`
    bullets are skipped. English headings (`## Next steps`, `## Needs human
    review`) are read too and recorded under the canonical Korean section name.
    Order is section order, then document order within a section."""
    out: list[dict] = []
    for headings in (NEXT_STEPS_HEADINGS, HUMAN_REVIEW_HEADINGS):
        body = section_body_any(text or "", headings)
        if not body:
            continue
        for line in body.splitlines():
            bm = re.match(r"[-*][ \t]+(.+?)[ \t]*$", line)
            if not bm:
                continue
            item = bm.group(1).strip()
            if not item or is_empty_marker(item):
                continue
            out.append({"section": headings[0], "text": item})
    return out


def _fid(task: str, section: str, text: str) -> str:
    """Stable 12-hex id for a follow-up, keyed on (task, section, normalized text).

    Deliberately NOT keyed on run_id: a retried or resumed run re-parses the same
    report.md and must produce the SAME id so append_followups dedupes it instead
    of stacking a duplicate every attempt. Whitespace is collapsed and the text
    lowercased so cosmetic reflow of an identical bullet does not mint a new id."""
    norm = re.sub(r"\s+", " ", (text or "").strip()).lower()
    payload = f"{task}\x00{section}\x00{norm}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def _load_raw(path: Path | None = None) -> list[dict]:
    """Every record in the jsonl, open and done. Unparseable lines are skipped."""
    p = Path(path) if path else FOLLOWUPS_PATH
    out: list[dict] = []
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def load_followups(path: Path | None = None) -> list[dict]:
    """All recorded follow-ups (open + done), in file order."""
    return _load_raw(path)


def open_followups(path: Path | None = None) -> list[dict]:
    """Only the follow-ups still awaiting a planner decision."""
    return [r for r in _load_raw(path) if r.get("status") == "open"]


def append_followups(text: str, *, source_task: str, source_run: str,
                     path: Path | None = None, ts: str | None = None) -> list[str]:
    """Append this report's NEW follow-ups to the jsonl; return the ids added.

    Idempotent by id: an id already present (open OR done) is skipped, so a
    re-run of the report stage, or a done follow-up whose report is re-parsed,
    never re-opens or duplicates it. Best-effort — an unwritable state dir loses
    the record but must not fail the caller (the runner's report seam)."""
    p = Path(path) if path else FOLLOWUPS_PATH
    items = parse_report_followups(text)
    if not items:
        return []
    seen = {r["id"] for r in _load_raw(p)}
    stamp = ts or dt.datetime.now().isoformat(timespec="seconds")
    fresh: list[str] = []
    lines: list[str] = []
    for it in items:
        fid = _fid(source_task, it["section"], it["text"])
        if fid in seen:
            continue
        seen.add(fid)          # guard against duplicate bullets within one report
        rec = {"id": fid, "source_task": source_task, "source_run": source_run,
               "section": it["section"], "text": it["text"], "ts": stamp,
               "status": "open", "resolution": None}
        lines.append(json.dumps(rec, ensure_ascii=False))
        fresh.append(fid)
    if not lines:
        return []
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return []
    return fresh


def _write_all(recs: list[dict], path: Path) -> bool:
    """Rewrite the whole jsonl from `recs` (jsonl has no in-place update).

    False on an unwritable file — callers treat that as "nothing changed"."""
    try:
        with Path(path).open("w", encoding="utf-8") as f:
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def _resolve(mapping: dict, path: Path | None = None) -> list[str]:
    """Mark each id in `mapping` done with its mapped resolution; one rewrite.

    Rewrites the whole file (jsonl has no in-place update). Returns the ids that
    actually flipped open→done — an id absent or already done contributes
    nothing, so a no-op reconcile touches no bytes and returns []."""
    p = Path(path) if path else FOLLOWUPS_PATH
    if not mapping:
        return []
    recs = _load_raw(p)
    changed: list[str] = []
    for rec in recs:
        rid = rec.get("id")
        if rid in mapping and rec.get("status") != "done":
            rec["status"] = "done"
            rec["resolution"] = mapping[rid]
            changed.append(rid)
    if not changed:
        return []
    if not _write_all(recs, p):
        return []
    return changed


def mark_done(ids, resolution: str | None = None,
              path: Path | None = None) -> list[str]:
    """Close the given follow-up ids with one shared `resolution`."""
    return _resolve({i: resolution for i in (ids or [])}, path)


def _context_line(r: dict) -> str:
    return (f"- [{r.get('id')}] ({r.get('section')} · from {r.get('source_task')}): "
            f"{r.get('text')}")


def render_context(open_recs: list[dict]) -> str:
    """The planner-context block for a set of open follow-ups, or "" when empty.

    Empty in → empty out is load-bearing: planner_tick only appends this after
    `/worker-planner` when it is non-empty, so on the common (no-follow-up) path
    the planner invocation stays byte-identical to before. The block also carries
    the resolve protocol inline — worker-planner.md says nothing about follow-ups,
    so this text is the planner's ONLY source for it, the `route: human` marker
    included.

    Only open rows are listed, even if a caller passes others: a done row is
    settled, and a row the old human path parked (`awaiting_human`) is not
    re-judged."""
    recs = [r for r in (open_recs or []) if r.get("status") == "open"]
    if not recs:
        return ""
    lines = [
        "## 미처리 report 후속 (durable — 이전 run report에서 이월)",
        "",
        "아래는 이전 run report가 남긴 뒤 아직 소진되지 않은 후속이다. 각 항목을",
        "판단해: 새 태스크로 만들 가치가 있으면 그 태스크 frontmatter에",
        "`from_followup: <id>`를 넣고(그러면 reconcile이 이 목록에서 제거한다),",
        "부적합하면 `Worker/_state/followups_rejected.jsonl`에",
        '`{"id":"<id>","reason":"..."}`를 append 하라. 아무것도 안 하면 다음',
        "tick에도 그대로 다시 보인다(의도된 durable 동작).",
        "",
        "판단 기준: 되돌릴 수 없는 일(실계좌·결제/유료 리소스, 외부 제출·발송, 공개 전환,",
        "되살릴 수 없는 삭제, 사용자 명의 행위)만 사람 몫이다 — 그런 항목은 `worker_mode: manual`",
        "태스크(`from_followup: <id>`)로 만든다. 사람 확인이 필요하지만 되돌릴 수 있는 항목은 그 줄에",
        '`"route":"human"`을 붙여 기각하라: `{"id":"<id>","reason":"...","route":"human"}` — 원',
        "태스크 Progress Log에 「확인 못 함」 한 줄로 남고 알림·대기는 없다. 그 밖의 부적합 항목은 route 없이 기각하라.",
        "",
    ]
    lines.extend(_context_line(r) for r in recs)
    return "\n".join(lines)


_FROM_FOLLOWUP_RE = re.compile(r"^from_followup:[ \t]*(.+)$", re.M)


def _frontmatter(text: str) -> str:
    """The YAML frontmatter block (between the first two `---`), or ""."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[:end] if end != -1 else ""


def _tasks_from_followup(items_dir: Path) -> dict:
    """{followup_id: task_slug} for every Todo item stamped `from_followup: <id>`.

    Scans frontmatter only, so a `from_followup` mentioned in prose body can't
    spuriously close a follow-up."""
    out: dict = {}
    try:
        paths = sorted(Path(items_dir).glob("*.md"))
    except OSError:
        return out
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _FROM_FOLLOWUP_RE.search(_frontmatter(text))
        if not m:
            continue
        fid = m.group(1).strip().strip('"\'')
        if fid and fid.lower() not in ("null", "none", "-", ""):
            out.setdefault(fid, p.stem)
    return out


def _rejection_entries(path: Path | None = None) -> dict:
    """{followup_id: [{"reason", "human"}, ...]} — every rejection line, in order.

    The file is append-only and one id can collect several lines over its life,
    so reconcile needs the whole per-id history to tell a line it already
    consumed from a fresh one (see `rejections_seen`). `human` is True only for
    `"route": "human"`, ignoring case and surrounding whitespace — the marker
    that also records the rejection on the source task (_record_unverified); any
    other value, or none, is a plain rejection."""
    p = Path(path) if path else REJECTIONS_PATH
    out: dict = {}
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.setdefault(str(rec["id"]), []).append({
                "reason": str(rec.get("reason", "")).strip(),
                "human": str(rec.get("route") or "").strip().lower() == "human",
            })
    return out


def _seen(rec: dict) -> int:
    """How many of this id's rejection lines are already consumed (0 if unset)."""
    try:
        return max(0, int(rec.get("rejections_seen") or 0))
    except (TypeError, ValueError):
        return 0


def _record_unverified(rec: dict, reason: str, items_dir: Path) -> bool:
    """Leave one "확인 못 함" line in the source task's Progress Log; True if written.

    The 2026-11-20 decision (C2): a follow-up that needs a person's check but is
    reversible is recorded, not parked — no alert, no waiting. Same shape as
    task_deps.apply_unblocks' progress note (newest first under the
    `## Progress Log` header, a new section at the end when there is none), kept
    here rather than imported because task_deps pulls in the cli and this module
    stays stdlib-only. Best-effort: an empty or path-like slug, a missing md or an
    OSError returns False and leaves the follow-up closed."""
    slug = str(rec.get("source_task") or "").strip()
    if not slug or "/" in slug or slug.startswith("."):
        return False
    why = " ".join(str(reason or "").split())
    tail = f"followup {rec.get('id')}: {why}" if why else f"followup {rec.get('id')}"
    line = (f"- {dt.date.today().isoformat()}: 확인 못 함 — "
            f"{' '.join(str(rec.get('text') or '').split())} ({tail})")
    path = Path(items_dir) / f"{slug}.md"
    try:
        text = path.read_text(encoding="utf-8")
        head = "## Progress Log\n"
        i = text.find(head)
        if i == -1:
            text = text.rstrip() + f"\n\n{head}\n{line}\n"
        else:
            j = i + len(head) + (1 if text.startswith("\n", i + len(head)) else 0)
            text = text[:j] + line + "\n" + text[j:]
        path.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def reconcile(followups_path: Path | None = None, items_dir: Path | None = None,
              rejections_path: Path | None = None) -> list[str]:
    """Apply what the planner did to the open follow-ups; return the ids closed.

    Only open rows are live: done rows are never reopened, and rows the old human
    path parked (`awaiting_human`) are left as they are. For each open row:

    * a Todo item stamped `from_followup: <id>` closes it as `created: <slug>`,
      which beats any rejection line in the same pass;
    * otherwise only its FRESH rejection lines count (past its `rejections_seen`
      watermark), and the last one closes it as `rejected: <reason>`. When that
      line carries `"route": "human"`, the source task's Progress Log also gets
      one "확인 못 함" line (_record_unverified) — only after the close is on
      disk, so a failed write records nothing, and once, since a done row is
      never live again.

    Everything it cannot match stays open — that is the durable default, not a
    failure."""
    fpath = Path(followups_path) if followups_path else FOLLOWUPS_PATH
    items = Path(items_dir) if items_dir else ITEMS_DIR
    recs = _load_raw(fpath)
    live = [r for r in recs if r.get("status") == "open"]
    if not live:
        return []
    created = _tasks_from_followup(items)
    rejections = _rejection_entries(rejections_path)
    changed: list[str] = []
    to_record: list[tuple[dict, str]] = []
    for rec in live:
        fid = str(rec["id"])
        if fid in created:
            rec["status"] = "done"
            rec["resolution"] = f"created: {created[fid]}"
            changed.append(rec["id"])
            continue
        entries = rejections.get(fid, [])
        fresh = entries[_seen(rec):]
        if not fresh:
            continue
        rec["rejections_seen"] = len(entries)
        last = fresh[-1]
        rec["status"] = "done"
        rec["resolution"] = (f"rejected: {last['reason']}" if last["reason"]
                             else "rejected")
        if last["human"]:
            to_record.append((rec, last["reason"]))
        changed.append(rec["id"])
    if changed and not _write_all(recs, fpath):
        return []
    for rec, reason in to_record:
        _record_unverified(rec, reason, items)
    return changed


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_usage(sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    """The subcommand grammar: emit-context and reconcile, no arguments."""
    parser = argparse.ArgumentParser(prog=CLI)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("emit-context")
    sub.add_parser("reconcile")
    return parser


def main(argv: list[str] | None = None) -> int:
    """The two planner-tick subcommands, both called from scripts/planner_tick.sh.

    emit-context prints the render block (empty when there are none, so
    `CTX=$(...)` comes back empty and no injection happens); reconcile applies
    what the planner just did. Exit codes: 0 done, 2 bad usage (including
    `--help`, which argparse answers with exit 0 instead)."""
    args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        ns = parser.parse_args(args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if ns.cmd == "emit-context":
        sys.stdout.write(render_context(open_followups()))
        return 0
    if ns.cmd == "reconcile":
        done = reconcile()
        if done:
            sys.stderr.write(
                f"reconciled {len(done)} follow-up(s): {' '.join(done)}\n")
        return 0
    return _usage(parser)


if __name__ == "__main__":
    raise SystemExit(main())
