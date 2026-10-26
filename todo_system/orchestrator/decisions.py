"""Decision action items — surface decisions whose actions nothing is chasing.

A decision file records what was decided AND what will be done about it. The
second half is the fragile one: `2026-07-29-worldgraph-no-go.md` committed to a
"9월 주말 1회(~12h) 반증 스파이크", that commitment lives only in body prose, and
no task in `Todo/` corresponds to it. Nothing pings it, so it is invisible until
someone reopens the file by hand. This module reads the machine-readable
`action_items` schema (documented in `Decisions/README.md`) and reports the
items whose link to a task is missing or broken.

Read-only by construction (acceptance criterion): every path here is `read_text`
and `Path.exists`. Nothing writes, and nothing creates tasks — deciding whether
an orphaned action deserves a task is a human judgement, and auto-creating one
would collide head-on with the planner policy that bars the worker from
inventing user-decision-bound work.

Two things this module deliberately does NOT do:

- **Parse prose.** The actions in the existing 8 decision files are numbered
  markdown lists, and extracting them would mean a machine deciding what counts
  as an action. When that guess is wrong it silently misrepresents a decision.
  The undeclared list this module emits is a human's migration worklist instead.
- **Reinterpret `affects_tasks`.** That legacy field (3 of 8 files carry it) is
  "tasks this decision affects", not "what this decision committed to do" — no
  `what`, no resolution state. It gets its own function and its own bucket;
  merging the two would fabricate the missing fields.

`MAX_SURFACED = 5` mirrors `stale.py`: noise is bounded by the cap, and the
truncated count is always printed so a capped list cannot read as a complete one.
"""

from __future__ import annotations

from pathlib import Path

MAX_SURFACED = 5

# Frontmatter read outcomes. Three states, not two: `repos._frontmatter()`
# collapses "no frontmatter" and "broken frontmatter" into `{}` because every
# caller there treats a missing field the same way. Here the distinction is the
# product — a decision with no frontmatter is un-migrated (actionable, a human
# writes the block), a decision with broken frontmatter is a bug (actionable,
# someone fixes the YAML), and conflating them hides the second inside the first.
FM_OK = "ok"
FM_NONE = "no_frontmatter"
FM_BROKEN = "broken"

# The vault frontmatter convention writes "no value" as the literal "-"
# (CLAUDE.md), so a bare emptiness test would read "-" as a real slug.
_ABSENT = ("", "-")

# Sentinel meaning "reviewed, and there is nothing to do". Distinct from the key
# being absent, which means "nobody has looked at this yet".
_NONE_SENTINEL = ("none", "없음")

# Item states that are finished. Excluded from the orphan check: chasing a task
# for an action someone already completed or cancelled is pure noise.
_CLOSED_STATES = ("resolved", "dropped")

# Task states that mean the linked task is closed. Wider than `cli.py`'s status
# choices (`open|done|waiting|blocked`) because the acceptance criterion asks
# about `cancelled` and the frontmatter files are free text — the raw string is
# always reported alongside, so this set only decides what gets flagged.
_CLOSED_TASK_STATES = ("done", "cancelled", "canceled")


def _read_frontmatter(path: str | Path) -> tuple[str, dict]:
    """`(kind, mapping)` for a markdown file's YAML frontmatter.

    `kind` is one of `FM_OK` / `FM_NONE` / `FM_BROKEN`; the mapping is empty
    unless `kind` is `FM_OK`. Never raises — a decision file that cannot be read
    must still appear in the report, because a crash here would take out the
    sweep for every other file too.

    `FM_NONE` is a real, expected shape in this vault, not an error: two of the
    eight decision files are ADRs that open with `# ADR: ...` and carry
    `**Status**: Accepted` as markdown body text.
    """
    # Local import mirroring `repos._frontmatter`: PyYAML is a declared
    # dependency but Mac-side only, and the message transport stays stdlib-only.
    import yaml

    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return FM_BROKEN, {}
    if not text.startswith("---"):
        return FM_NONE, {}
    end = text.find("\n---", 3)
    if end == -1:
        # Opens a frontmatter fence and never closes it — malformed, not absent.
        return FM_BROKEN, {}
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return FM_BROKEN, {}
    if data is None:
        # `---\n---` — an empty block is present-but-says-nothing, which is the
        # same actionable state as having written no block at all.
        return FM_NONE, {}
    if not isinstance(data, dict):
        return FM_BROKEN, {}
    return FM_OK, data


def _normalize_slug(value) -> str:
    """A task slug from any of the three link forms the vault actually uses.

    Bare (`ai-stock-trading-5-strategy-ai`, 7 refs across 2 files), wikilink
    (`[[withdrawal-process]]`, 8 refs in the 2026-05-19 decision), and aliased
    wikilink (`[[slug|display title]]`, the form `Todo/Todo.md` writes) all collapse
    to the bare slug. A path prefix (`[[Todo/items/slug]]`) and a `.md` suffix
    are stripped too, so a link copied out of the index still resolves.

    Returns "" for anything absent — None, blank, or the vault's "-" placeholder.
    """
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if v.startswith("[[") and v.endswith("]]"):
        v = v[2:-2].strip()
    v = v.split("|", 1)[0].strip()      # aliased wikilink: keep the target
    v = v.rsplit("/", 1)[-1].strip()    # path-qualified link: keep the basename
    if v.endswith(".md"):
        v = v[:-3].strip()
    return "" if v in _ABSENT else v


def _task_status(items_dir: str | Path, slug: str) -> str | None:
    """The `status` a task's own frontmatter declares, or None if no such task.

    Reported as the raw string, not mapped onto a fixed enum. `cli.py` accepts
    `open|done|waiting|blocked` while the acceptance criterion asks about
    `cancelled`, and the frontmatter files hold whatever was typed — normalising
    here would invent a state the source never claimed.

    Note `Todo/items/` is gitignored, so this returns None for everything in a
    fresh clone. That is why both directories are injected by the caller and the
    tests run entirely on temp fixtures.
    """
    path = Path(items_dir) / f"{slug}.md"
    if not path.exists():
        return None
    kind, fm = _read_frontmatter(path)
    if kind != FM_OK:
        return ""
    return str(fm.get("status") or "").strip()


def _classify_item(item, items_dir: str | Path) -> tuple[str, dict]:
    """`(kind, fields)` for one entry of an `action_items` list."""
    if not isinstance(item, dict):
        # A bare string list (`- run the spike`) is the likeliest near-miss
        # migration. Carrying the raw text keeps the report actionable.
        return "unparseable", {"note": f"항목이 매핑이 아님: {item!r}"}

    what = str(item.get("what") or "").strip()
    item_status = str(item.get("status") or "").strip()
    slug = _normalize_slug(item.get("task"))
    fields = {"what": what, "item_status": item_status, "task": slug}

    if item_status.lower() in _CLOSED_STATES:
        # Resolved or dropped: the link no longer has to point anywhere.
        return "ok", fields
    if slug.lower() in _NONE_SENTINEL:
        return "ok", fields
    if not slug:
        return "empty_task", {**fields, "note": "`task` 가 비어 있음"}

    status = _task_status(items_dir, slug)
    if status is None:
        return "orphan", {**fields, "note": f"`Todo/items/{slug}.md` 없음"}
    fields["task_status"] = status
    if status.lower() in _CLOSED_TASK_STATES:
        # The item still says open while its task is finished. Not a broken
        # link — a stale one, and the only place the task's status is
        # load-bearing rather than decorative.
        return "closed_task", {**fields, "note": f"태스크는 `{status}` 인데 항목은 미해결"}
    return "ok", fields


def _scan_decision(path: Path, items_dir: str | Path) -> list[dict]:
    """Every row a single decision file contributes (`ok` rows included)."""
    base = {"decision": path.stem, "path": str(path), "what": "", "task": "",
            "item_status": "", "task_status": None, "note": ""}

    def row(kind, **kw):
        return {**base, "kind": kind, **kw}

    kind, fm = _read_frontmatter(path)
    if kind == FM_BROKEN:
        return [row("unparseable", note="프론트매터 YAML 파싱 실패")]
    if kind == FM_NONE:
        return [row("undeclared", note="프론트매터 없음 (ADR 형식)")]
    if "action_items" not in fm:
        return [row("undeclared", note="`action_items` 키 없음")]

    value = fm["action_items"]
    if value is None:
        # A bare `action_items:` line. The key was typed, so this counts as
        # declared-and-empty rather than un-migrated.
        return [row("ok", note="`action_items` 가 비어 있음 (선언됨)")]
    if isinstance(value, str):
        if value.strip().lower() in _NONE_SENTINEL:
            return [row("ok", note="행동 항목 없음 (명시)")]
        return [row("unparseable", note=f"`action_items` 가 목록도 `none` 도 아님: {value!r}")]
    if not isinstance(value, list):
        return [row("unparseable", note=f"`action_items` 가 목록이 아님: {type(value).__name__}")]
    if not value:
        return [row("ok", note="행동 항목 없음 (빈 목록)")]

    return [row(k, **f) for k, f in (_classify_item(it, items_dir) for it in value)]


def scan(decisions_dir: str | Path, items_dir: str | Path) -> list[dict]:
    """Every action-item row across every decision file, healthy ones included.

    Both directories are arguments with no defaults on purpose: a module-level
    default path would make an argument-free call quietly sweep the real vault,
    which is exactly how a "pure" helper ends up depending on live state that
    tests cannot control.

    `README.md` is skipped — it is the schema documentation living in the same
    folder, and without this it reports itself as an undeclared decision.
    """
    out: list[dict] = []
    for path in sorted(Path(decisions_dir).glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        out.extend(_scan_decision(path, items_dir))
    return out


def find_action_item_issues(decisions_dir: str | Path,
                            items_dir: str | Path) -> list[dict]:
    """Action items whose link to a task is missing, broken, or stale.

    Read-only. Creates nothing. Kinds, most actionable first:

    - `orphan` — the item names a task that does not exist in `Todo/items/`
    - `empty_task` — the item declares no task at all (blank or the "-" sentinel)
    - `closed_task` — the task exists but is already done/cancelled while the
      item still reads open
    - `unparseable` — the frontmatter or the `action_items` value is malformed
    - `undeclared` — no `action_items` key: nobody has written the block yet

    Ordered so `orphan` can never be pushed out of a capped view by the
    `undeclared` bulk. That ordering is load-bearing right now: the schema is
    new, so every existing decision is undeclared, and a naive file-order sort
    would bury the one genuinely broken link under eight migration TODOs.
    """
    rank = {k: i for i, k in enumerate(
        ("orphan", "empty_task", "closed_task", "unparseable", "undeclared"))}
    rows = [r for r in scan(decisions_dir, items_dir) if r["kind"] != "ok"]
    rows.sort(key=lambda r: (rank.get(r["kind"], 99), r["decision"], r["what"]))
    return rows


def find_affects_tasks_orphans(decisions_dir: str | Path,
                               items_dir: str | Path) -> list[dict]:
    """Broken links in the legacy `affects_tasks` field — a SEPARATE bucket.

    Never merged into `find_action_item_issues`. `affects_tasks` says which
    tasks a decision touches, not what the decision committed to doing: it
    carries no `what` and no resolution state, so promoting its entries to
    action items would fabricate both. Three of the eight decision files use it
    (15 references, in bare-slug and wikilink form), and it stays a
    link-integrity check over those.
    """
    out: list[dict] = []
    for path in sorted(Path(decisions_dir).glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        kind, fm = _read_frontmatter(path)
        if kind != FM_OK:
            continue
        refs = fm.get("affects_tasks")
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list):
            continue
        for ref in refs:
            slug = _normalize_slug(ref)
            if not slug or slug.lower() in _NONE_SENTINEL:
                continue
            if _task_status(items_dir, slug) is None:
                out.append({"decision": path.stem, "path": str(path),
                            "task": slug, "kind": "orphan",
                            "note": f"`Todo/items/{slug}.md` 없음"})
    out.sort(key=lambda r: (r["decision"], r["task"]))
    return out


def render_section(rows: list[dict], limit: int = MAX_SURFACED) -> list[str]:
    """Markdown bullets for the dashboard section.

    Always states the total when the list is capped, for the same reason
    `stale.render_section` does: a truncated view that looks complete is worse
    than no view, because the reader stops looking.
    """
    if not rows:
        return ["- 없음"]
    shown = rows[:limit]
    lines = []
    for r in shown:
        head = f"- `{r['decision']}` [{r['kind']}]"
        what = r.get("what") or ""
        task = r.get("task") or ""
        detail = " — ".join(x for x in (what, r.get("note") or "") if x)
        tail = f" (task: `{task}`)" if task else ""
        lines.append(f"{head} {detail}{tail}" if detail else f"{head}{tail}")
    hidden = len(rows) - len(shown)
    if hidden:
        lines.append(f"- … 외 {hidden}건 더 (총 {len(rows)}건)")
    return lines


def _main(argv: list[str]) -> int:
    """`python -m todo_system.orchestrator.decisions <decisions_dir> <items_dir>`."""
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0])
        print("usage: python -m todo_system.orchestrator.decisions "
              "<decisions_dir> <items_dir>")
        return 2
    decisions_dir, items_dir = argv
    issues = find_action_item_issues(decisions_dir, items_dir)
    print(f"# 행동 항목 이슈 {len(issues)}건")
    for r in issues:
        print(f"{r['kind']:12} {r['decision']:48} {r.get('what') or r.get('note') or ''}")
    affects = find_affects_tasks_orphans(decisions_dir, items_dir)
    print(f"\n# affects_tasks 끊긴 링크 {len(affects)}건 (별도 버킷)")
    for r in affects:
        print(f"{'orphan':12} {r['decision']:48} {r['task']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    raise SystemExit(_main(sys.argv[1:]))
