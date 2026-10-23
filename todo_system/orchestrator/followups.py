"""Durable pickup for report-stage follow-ups.

The report stage names follow-ups in free prose — `## 다음 단계` and
`## 사람이 검토해야 할 부분` in report.md — and, until now, nothing carried them
forward. The planner only ever saw them if they happened to ride along in a
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

A rejection, though, was the end of the line, and that reopened the same leak one
step later. By 2026-09-19, 75 of 170 rejection lines mentioned a human or a
decision, and nobody was told: the three improvement proposals from the
2026-09-16 stage cost-benefit audit were rejected as "a human decision", and the
person found out three days later by asking. So there is a third path: a
rejection line carrying `"route": "human"` parks the follow-up as
`awaiting_human` instead of closing it. It drops out of the planner injection
(nothing to re-judge every tick), queues ONE kakao digest line (the daily-summary
tier, never an immediate ping), and waits for a person at
`python -m todo_system.orchestrator.followups human`.
`approve <id>` hands it back to the planner as open + approved, in its own
"사람 승인됨" block; `dismiss <id> --reason …` closes it. Rejections written
before this existed are never backfilled — `reroute <id>` revives one at a time.

Because the rejections file is append-only and read last-wins, each row keeps a
`rejections_seen` watermark and reconcile reads only the lines past it. Without
it, the very line that parked a row would re-park or close it on the first
reconcile after the human approved it. Rows written before the field existed
count as 0, so old state reconciles exactly as it always did.

Stdlib-only and best-effort throughout — recording a follow-up must never break a
pipeline that otherwise finished, so every write swallows OSError and every read
of a corrupt line skips it rather than raising. That includes the digest notice:
a failed queue is retried on the next reconcile, never sent immediately instead.

Paths are module globals (not import-time-frozen defaults) so tests can
monkeypatch `FOLLOWUPS_PATH` / `ITEMS_DIR` / `REJECTIONS_PATH` / `DIGEST_PATH` /
`ROUTE_LOG_PATH` onto a tempdir — the same idiom `stages.RUNS_DIR` uses, and
necessary because the real paths are gitignored live state that a test must
never touch.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

from .. import i18n, kakao_format, paths

# Live runtime state, all under gitignored trees (Worker/_state/, Todo/items/).
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
# Where the one-line "decision needed" notice is queued. None (production) lets
# kakao_format.route() resolve KAKAO_VAULT_DIR or the vault's logs/ — the SAME
# queue scripts/kakao_digest_send.py drains — and keeps conftest's KAKAO_VAULT_DIR
# safety net in force. Tests pin both onto a tempdir.
DIGEST_PATH: Path | None = None
ROUTE_LOG_PATH: Path | None = None

# A follow-up the planner parked for a person (`"route": "human"` on its
# rejection line) instead of closing it. Only a created task, `approve` or
# `dismiss` moves it on; rejection lines never do.
STATUS_HUMAN = "awaiting_human"
# First line of the digest notice. The fixed `…:` prefix is what compose_digest
# groups on, so several parked items fold into one "×N" line, and the CLI hint
# lives inside that key so it survives even the folded form. The notice itself
# uses human_digest_head() (the active language, resolved per call); this
# constant stays the ko value for importers.
HUMAN_DIGEST_HEAD = i18n.MESSAGES["ko"]["followup.digest_head"]
CLI = "python -m todo_system.orchestrator.followups"


def human_digest_head() -> str:
    """HUMAN_DIGEST_HEAD in the active UI language."""
    return i18n.t("followup.digest_head")

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
# `- bullet (없으면 "없음")`. `none` must stand as a whole word, so a real bullet
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


def awaiting_followups(path: Path | None = None) -> list[dict]:
    """The follow-ups parked for a human decision (`route: human`)."""
    return [r for r in _load_raw(path) if r.get("status") == STATUS_HUMAN]


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
    so this text is the planner's ONLY source for it, `route: human` included.

    Parked (awaiting_human) and done rows are dropped even if a caller passes
    them: a parked item waits on a person, and re-showing it would have the
    planner re-judge it every tick. Approved rows (a person ran `approve`) get
    their own block saying the decision is already made."""
    recs = [r for r in (open_recs or [])
            if r.get("status") not in (STATUS_HUMAN, "done")]
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
        "단, 사람 판단이 필요한 항목 — 사용자 결정, 또는 콘솔·배포·sudo·실기기처럼 사람만",
        "할 수 있는 조치 — 이면서 사람에게 알리는 다른 채널이 아직 없을 때만 그 줄에",
        '`"route":"human"`을 붙여라: `{"id":"<id>","reason":"...","route":"human"}`.',
        "그러면 done이 아니라 사람 결정 대기로 남아 하루 1회 카톡 요약과 `followups human`",
        "목록으로 사람에게 가고, 이 주입에서는 빠진다. route 금지: auto_merge:false PR",
        "리뷰처럼 [검토요청]이 이미 가는 것, 정보성, 이미 처리됐거나 다른 태스크가 커버하는",
        "것, 중복 — 이런 건 지금처럼 route 없이 기각하라.",
        "",
    ]
    regular = [r for r in recs if not r.get("approved")]
    approved = [r for r in recs if r.get("approved")]
    lines.extend(_context_line(r) for r in regular)
    if approved:
        if regular:
            lines.append("")
        lines += [
            "### 사람 승인됨 — 태스크화 대상",
            "",
            "사람이 `followups approve`로 이미 결정한 항목이다. user_decision_bound로 다시",
            "기각하지 말고 `from_followup: <id>` 태스크로 만들어라. 승인이 푸는 것은 사용자",
            "결정 게이트뿐이다 — external_action·costs_money·destructive_irreversible 등 다른",
            "blocked_patterns는 그대로이니, 그 사유라면 route 없이 기각하라.",
            "",
        ]
        lines.extend(_context_line(r) for r in approved)
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

    The file is append-only and one id can collect several lines over its life
    (parked, approved, rejected again), so reconcile needs the whole per-id
    history to tell a line it already consumed from a fresh one (see
    `rejections_seen`). `human` is True only for `"route": "human"`, ignoring
    case and surrounding whitespace; any other value, or none, is a plain
    rejection exactly as before."""
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


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _digest_message(rec: dict) -> str:
    """The digest notice for one parked follow-up, ≤ KAKAO_TEXT_LIMIT.

    The id comes right after the fixed prefix, before the slug, so the 60-char
    clip compose_digest applies to a singleton line keeps it. The slug is capped
    at 60 and the reason is the word-clipped tail, so the memo fits by
    construction."""
    task = kakao_format.clip_words(str(rec.get("source_task") or "-"), 60)
    return kakao_format.compose_alert(
        f"{human_digest_head()}: [{rec.get('id')}] {task}",
        [f"→ {CLI} human"],
        tail=i18n.t("alert.reason", reason=rec.get("human_reason") or "-"))


def _notify_awaiting(recs: list[dict]) -> bool:
    """Queue ONE digest line per parked follow-up not yet notified; True if any.

    route()'s digest tier does no dedup of its own, so the marker lives on the
    record (`human_notified`) and is permanent: one line per id, ever — a
    re-park after an approve shows only in `followups human`. Only a "digest"
    verdict marks the row. Anything else — route() failing open to "send", or
    raising — leaves it unmarked for the next reconcile to retry. It is never
    sent immediately, and it never breaks the caller."""
    marked = False
    for rec in recs:
        if rec.get("status") != STATUS_HUMAN or rec.get("human_notified"):
            continue
        try:
            verdict, _ = kakao_format.route(
                _digest_message(rec), level=kakao_format.LEVEL_DIGEST,
                event_key=f"followup-human:{rec.get('id')}",
                digest_path=DIGEST_PATH, log_path=ROUTE_LOG_PATH)
        except Exception:
            continue
        if verdict == "digest":
            rec["human_notified"] = _now()
            marked = True
    return marked


def reconcile(followups_path: Path | None = None, items_dir: Path | None = None,
              rejections_path: Path | None = None) -> list[str]:
    """Apply what the planner did to the live follow-ups; return the ids moved.

    Live means open or awaiting_human. Done rows are never reopened here — only
    `reroute` revives one, which is the no-backfill guard. For each live row:

    * a Todo item stamped `from_followup: <id>` closes it as `created: <slug>`.
      This beats everything, a parked or approved row included;
    * an awaiting_human row is otherwise left alone — rejection lines never
      move it, only `approve` / `dismiss` do;
    * an open row looks only at its FRESH rejection lines (past its
      `rejections_seen` watermark), and the last one wins: `"route": "human"`
      parks it as awaiting_human, anything else closes it as
      `rejected: <reason>` exactly as before.

    The watermark is what stops an approve from bouncing. The file is
    append-only and last-wins, so without it the very line that parked a row
    would re-park (or close) it on the first reconcile after the human approved
    it. A row without the field has seen 0 lines, so old state behaves as it
    always did.

    Newly parked rows then get their one digest line (_notify_awaiting), only
    after the transition is on disk, so a failed write never notifies. A row
    whose queueing failed on an earlier pass is retried here even when nothing
    else changed. Everything it cannot match stays as it is — that is the
    durable default, not a failure."""
    fpath = Path(followups_path) if followups_path else FOLLOWUPS_PATH
    recs = _load_raw(fpath)
    live = [r for r in recs if r.get("status") in ("open", STATUS_HUMAN)]
    if not live:
        return []
    created = _tasks_from_followup(Path(items_dir) if items_dir else ITEMS_DIR)
    rejections = _rejection_entries(rejections_path)
    changed: list[str] = []
    for rec in live:
        fid = str(rec["id"])
        if fid in created:
            rec["status"] = "done"
            rec["resolution"] = f"created: {created[fid]}"
            changed.append(rec["id"])
            continue
        if rec.get("status") != "open":
            continue
        entries = rejections.get(fid, [])
        fresh = entries[_seen(rec):]
        if not fresh:
            continue
        rec["rejections_seen"] = len(entries)
        last = fresh[-1]
        if last["human"]:
            rec["status"] = STATUS_HUMAN
            rec["human_reason"] = last["reason"]
        else:
            rec["status"] = "done"
            rec["resolution"] = (f"rejected: {last['reason']}" if last["reason"]
                                 else "rejected")
        changed.append(rec["id"])
    if changed and not _write_all(recs, fpath):
        return []
    if _notify_awaiting(recs):
        _write_all(recs, fpath)
    return changed


def _transition(fid: str, check, apply, path: Path | None = None,
                after=None) -> tuple[int, str]:
    """One human-driven state change: load, find `fid`, validate, mutate, rewrite.

    `check(rec)` returns an error message for a row in the wrong state (or None);
    `apply(rec)` mutates it. Returns (1, msg) for an unknown id, a refused
    transition or an unwritable file, else (0, ""). `after(recs)` runs once the
    change is on disk; if it reports that it touched recs again, they are
    rewritten once more (reroute's digest marker)."""
    p = Path(path) if path else FOLLOWUPS_PATH
    recs = _load_raw(p)
    hits = [r for r in recs if str(r.get("id")) == fid]
    if not hits:
        return 1, f"unknown follow-up id: {fid}"
    for rec in hits:
        err = check(rec)
        if err:
            return 1, f"{fid}: {err}"
    for rec in hits:
        apply(rec)
    if not _write_all(recs, p):
        return 1, f"{fid}: could not write {p}"
    if after is not None and after(recs):
        _write_all(recs, p)
    return 0, ""


def _state_of(rec: dict) -> str:
    return f"status={rec.get('status')!r}, resolution={rec.get('resolution')!r}"


def approve(fid: str, path: Path | None = None,
            rejections_path: Path | None = None) -> tuple[int, str]:
    """awaiting_human → open + approved: a person made the call the planner could
    not, and the next planner run gets the row under '사람 승인됨 — 태스크화 대상'.

    Every rejection line already on file for the id counts as seen, so the line
    that parked it can never re-park or close it; only a line written after the
    approve can."""
    seen = len(_rejection_entries(rejections_path).get(fid, []))

    def check(rec):
        if rec.get("status") != STATUS_HUMAN:
            return f"{_state_of(rec)} — only an {STATUS_HUMAN} follow-up can be approved"
        return None

    def apply(rec):
        rec.update(status="open", approved=True, approved_ts=_now(),
                   rejections_seen=seen)

    rc, msg = _transition(fid, check, apply, path)
    if rc:
        return rc, msg
    return 0, i18n.t("followup.approved_msg", fid=fid)


def dismiss(fid: str, reason: str,
            path: Path | None = None) -> tuple[int, str]:
    """awaiting_human → done, resolution `dismissed: <reason>` — a person decided
    against it. Kept distinct from `rejected:` (the planner's word) on purpose."""

    def check(rec):
        if rec.get("status") != STATUS_HUMAN:
            return f"{_state_of(rec)} — only an {STATUS_HUMAN} follow-up can be dismissed"
        return None

    def apply(rec):
        rec.update(status="done", resolution=f"dismissed: {reason}",
                   dismissed_ts=_now())

    rc, msg = _transition(fid, check, apply, path)
    return (rc, msg) if rc else (0, i18n.t("followup.dismissed_msg", fid=fid, reason=reason))


def reroute(fid: str, path: Path | None = None,
            rejections_path: Path | None = None) -> tuple[int, str]:
    """done `rejected…` → awaiting_human: re-file ONE old rejection as a human
    decision. History is never backfilled automatically (that would page for
    every past rejection at once); this is the manual, one-id-at-a-time path.

    A row closed as `created:` or `dismissed:`, or one still open or parked, is
    refused. The revived row gets its digest line like any fresh park — unless
    it was already notified once before (the marker is permanent)."""
    seen = len(_rejection_entries(rejections_path).get(fid, []))

    def check(rec):
        if (rec.get("status") != "done"
                or not str(rec.get("resolution") or "").startswith("rejected")):
            return f"{_state_of(rec)} — only a rejected follow-up can be rerouted"
        return None

    def apply(rec):
        reason = (str(rec.get("resolution") or "").removeprefix("rejected")
                  .strip().removeprefix(":").strip())
        rec.update(status=STATUS_HUMAN, human_reason=reason, resolution=None,
                   rejections_seen=seen, rerouted_ts=_now())

    rc, msg = _transition(fid, check, apply, path, after=_notify_awaiting)
    return (rc, msg) if rc else (0, i18n.t("followup.rerouted_msg", fid=fid, status=STATUS_HUMAN))


def render_human(recs: list[dict]) -> str:
    """The `followups human` listing: what waits on a person, plus what a person
    approved that the planner has not turned into a task yet. Read-only."""
    waiting = [r for r in recs if r.get("status") == STATUS_HUMAN]
    approved = [r for r in recs if r.get("status") == "open" and r.get("approved")]
    if not waiting and not approved:
        return i18n.t("followup.none") + "\n"
    out = [i18n.t("followup.waiting", n=len(waiting)) if waiting else i18n.t("followup.none")]
    for r in waiting:
        text = kakao_format.clip_words(str(r.get("text") or ""), 100)
        reason = kakao_format.clip_words(str(r.get("human_reason") or "-"), 300)
        out += [
            "",
            f"- [{r.get('id')}] from {r.get('source_task')} ({_section_label(r.get('section'))})",
            "  " + i18n.t("followup.body", text=text),
            "  " + i18n.t("alert.reason", reason=reason),
        ]
    if approved:
        out += ["", i18n.t("followup.approved_head", n=len(approved))]
        out += [f"- [{r.get('id')}] from {r.get('source_task')} "
                + i18n.t("followup.approved_at", ts=r.get("approved_ts") or "-")
                for r in approved]
    out += [
        "",
        i18n.t("followup.hint_approve", cli=CLI),
        i18n.t("followup.hint_dismiss", cli=CLI),
        i18n.t("followup.hint_reroute", cli=CLI),
    ]
    return "\n".join(out) + "\n"


# Display names of the recorded (canonical, Korean) section names. The record
# keeps the canonical name; only the `followups human` listing shows it in the
# active language. An unknown section is shown as recorded.
_SECTION_LABEL_KEYS = {
    NEXT_STEPS_HEADINGS[0]: "followup.section_next_steps",
    HUMAN_REVIEW_HEADINGS[0]: "followup.section_human_review",
}


def _section_label(section) -> str:
    key = _SECTION_LABEL_KEYS.get(section)
    return i18n.t(key) if key else str(section)


def _parse_dismiss(rest: list[str]) -> tuple[str, str] | None:
    """(id, reason) from `<id> --reason <text…>` or `<id> --reason=<text…>`."""
    if len(rest) < 2 or rest[0].startswith("--"):
        return None
    fid, flag, tail = rest[0], rest[1], rest[2:]
    if flag == "--reason":
        reason = " ".join(tail)
    elif flag.startswith("--reason="):
        reason = " ".join([flag.removeprefix("--reason="), *tail])
    else:
        return None
    reason = reason.strip()
    return (fid, reason) if reason else None


def _clean_id(raw: str) -> str:
    """Accept an id pasted with the brackets `human` prints around it."""
    return raw.strip().strip("[]")


def _usage() -> int:
    sys.stderr.write(
        f"usage: {CLI} {{emit-context|reconcile|human|approve <id>|"
        "dismiss <id> --reason <text>|reroute <id>}\n")
    return 2


def _reply(rc: int, msg: str) -> int:
    (sys.stderr if rc else sys.stdout).write(msg + "\n")
    return rc


def main(argv: list[str] | None = None) -> int:
    """The planner-tick subcommands plus the human-decision CLI.

    `emit-context` and `reconcile` are called from scripts/planner_tick.sh.
    emit-context prints the render block (empty when there are none, so
    `CTX=$(...)` comes back empty and no injection happens); reconcile applies
    what the planner just did.

    The rest are for a person: `human` lists what waits on them (read-only, it
    never reconciles); `approve <id>` / `dismiss <id> --reason <text>` decide a
    parked follow-up; `reroute <id>` re-files one old `rejected` follow-up as
    awaiting a human. Exit codes: 0 done, 1 unknown id / refused transition /
    write failure, 2 bad usage."""
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    rest = args[1:]
    if cmd == "emit-context":
        sys.stdout.write(render_context(open_followups()))
        return 0
    if cmd == "reconcile":
        done = reconcile()
        if done:
            sys.stderr.write(
                f"reconciled {len(done)} follow-up(s): {' '.join(done)}\n")
        return 0
    if cmd == "human":
        sys.stdout.write(render_human(load_followups()))
        return 0
    if cmd in ("approve", "reroute"):
        if len(rest) != 1 or not _clean_id(rest[0]):
            return _usage()
        fn = approve if cmd == "approve" else reroute
        return _reply(*fn(_clean_id(rest[0])))
    if cmd == "dismiss":
        parsed = _parse_dismiss(rest)
        if not parsed or not _clean_id(parsed[0]):
            return _usage()
        return _reply(*dismiss(_clean_id(parsed[0]), parsed[1]))
    return _usage()


if __name__ == "__main__":
    raise SystemExit(main())
