"""Graph Memory indexer (M3-a).

Walks the markdown vault, parses wikilinks + frontmatter relationship fields,
and populates ``Memory/relations.sqlite`` so future briefs can traverse the
accumulated knowledge graph.

Schema follows plan Phase 2.6 (bi-temporal: ``event_date`` + ``recorded_at``).

Stdlib only. Intentionally duplicates a minimal frontmatter parser so this
module stays decoupled from ``todo_system.cli``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from . import paths

ROOT = paths.vault_root()
MEMORY_DIR = ROOT / "Memory"
ENTITIES_DIR = MEMORY_DIR / "entities"
EPISODES_DIR = MEMORY_DIR / "episodes"
SESSIONS_DIR = MEMORY_DIR / "sessions"
DECISIONS_DIR = ROOT / "Decisions"
TODO_ITEMS_DIR = ROOT / "Todo" / "items"
NOTES_DIR = ROOT / "Notes"
INDEX_PATH = MEMORY_DIR / "index.md"
DB_PATH = MEMORY_DIR / "relations.sqlite"

# Frontmatter fields whose VALUE forms typed edges from the file's slug.
# Each entry: (field_name, edge_type).
LIST_EDGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("depends_on", "depends_on"),
    ("evidence_for", "evidence_for"),
    ("contradicts", "contradicts"),
    ("related_to", "related_to"),
    ("affects_tasks", "affects_tasks"),
    ("related_entities", "related_entities"),
    ("related_tasks", "related_tasks"),
    ("related_decisions", "related_decisions"),
)

# Scalar wikilink fields → typed edges.
SCALAR_EDGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("supersedes", "supersedes"),
    ("parent", "parent"),
)

# Roots that get scanned for .md files (relative to ROOT).
SCAN_ROOTS = (
    "Todo/items",
    "Notes",
    "Memory/entities",
    "Memory/episodes",
    "Decisions",
)

WIKILINK_RE = re.compile(r"\[\[([^|\]]+)(?:\|[^\]]*)?\]\]")
PATH_PREFIX_RE = re.compile(r"^(?:Todo/items/|Notes/|Memory/(?:entities|episodes)/|Decisions/)")


# ---------------------------------------------------------------------------
# Frontmatter parsing (minimal, stdlib-only)
# ---------------------------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content

    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, content

    fm_raw, body = parts
    fm_lines = fm_raw.splitlines()[1:]
    result: dict[str, Any] = {}
    idx = 0
    while idx < len(fm_lines):
        line = fm_lines[idx]
        if not line.strip():
            idx += 1
            continue
        if ":" not in line:
            idx += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            items: list[str] = []
            idx += 1
            while idx < len(fm_lines):
                sub = fm_lines[idx]
                if sub.strip().startswith("- "):
                    items.append(sub.strip()[2:].strip().strip('"').strip("'"))
                    idx += 1
                    continue
                if sub.startswith("  - "):
                    items.append(sub.strip()[2:].strip().strip('"').strip("'"))
                    idx += 1
                    continue
                break
            result[key] = items
            continue
        value = value.strip().strip('"').strip("'")
        result[key] = value
        idx += 1

    return result, body


def normalize_target(raw: str) -> str:
    """Turn ``[[wikilink|alias]]`` target text into a bare slug.

    Strips ``Todo/items/work/`` etc. path prefixes, removes ``.md`` suffix,
    drops trailing alias after ``|`` (caller already stripped it via regex
    group), and strips wikilink brackets if present.
    """
    if not raw:
        return ""
    s = raw.strip().strip("[]").strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    # Strip vault-relative path prefix.
    s = PATH_PREFIX_RE.sub("", s)
    # Strip trailing .md.
    if s.endswith(".md"):
        s = s[:-3]
    return s.strip()


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


# Nullable provenance columns (G1): appended to the end of the edge column list
# so a freshly-created schema and an in-place-migrated one end up identical.
_PROVENANCE_COLUMNS: tuple[str, ...] = ("run_id", "commit_sha")


def init_relations_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_slug TEXT NOT NULL,
            to_slug TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            source_file TEXT NOT NULL,
            event_date TEXT,
            recorded_at TEXT NOT NULL,
            valid_until TEXT,
            confidence REAL,
            run_id TEXT,
            commit_sha TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_slug);
        CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_slug);
        CREATE INDEX IF NOT EXISTS idx_edges_event_date ON edges(event_date);
        CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
        """
    )
    con.commit()

    # Idempotent, lossless migration of a pre-G1 ``edges`` table (created before
    # the provenance columns existed): add each missing column via ALTER TABLE
    # ADD COLUMN so existing rows survive with provenance = NULL. A no-op on a
    # freshly-created schema (the CREATE above already has both columns) and on an
    # already-migrated DB. Column names are hard-coded constants — DDL cannot be
    # parameterised — so the f-string carries no injection surface.
    existing = {row["name"] for row in con.execute("PRAGMA table_info(edges)")}
    for name in _PROVENANCE_COLUMNS:
        if name not in existing:
            con.execute(f"ALTER TABLE edges ADD COLUMN {name} TEXT")
    con.commit()
    return con


def clear_edges(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM edges")
    con.commit()


def insert_edges(con: sqlite3.Connection, edges: Iterable[dict[str, Any]]) -> int:
    rows = [
        (
            e["from_slug"],
            e["to_slug"],
            e["edge_type"],
            e["source_file"],
            e.get("event_date"),
            e["recorded_at"],
            e.get("valid_until"),
            e.get("confidence"),
            e.get("run_id"),
            e.get("commit_sha"),
        )
        for e in edges
    ]
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO edges (
            from_slug, to_slug, edge_type, source_file,
            event_date, recorded_at, valid_until, confidence,
            run_id, commit_sha
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def iter_markdown_files(vault_root: Path = ROOT) -> Iterable[Path]:
    for rel in SCAN_ROOTS:
        base = vault_root / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            yield path


def _coerce_confidence(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def edges_from_file(path: Path, vault_root: Path = ROOT, recorded_at: str | None = None) -> list[dict[str, Any]]:
    """Parse a single .md file and yield typed edges."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

    fm, body = parse_frontmatter(content)
    from_slug = path.stem
    rel_source = str(path.relative_to(vault_root))
    event_date = fm.get("event_date") if isinstance(fm.get("event_date"), str) else None
    recorded_at = recorded_at or dt.datetime.now().isoformat(timespec="seconds")
    confidence = _coerce_confidence(fm.get("confidence"))

    edges: list[dict[str, Any]] = []

    def add(to_slug: str, edge_type: str) -> None:
        to_norm = normalize_target(to_slug)
        if not to_norm or to_norm == from_slug:
            return
        edges.append(
            {
                "from_slug": from_slug,
                "to_slug": to_norm,
                "edge_type": edge_type,
                "source_file": rel_source,
                "event_date": event_date,
                "recorded_at": recorded_at,
                "valid_until": None,
                "confidence": confidence,
            }
        )

    # 1) Typed list edges from frontmatter.
    for field, edge_type in LIST_EDGE_FIELDS:
        raw = fm.get(field)
        if not raw:
            continue
        if isinstance(raw, list):
            for item in raw:
                add(item, edge_type)
        elif isinstance(raw, str):
            add(raw, edge_type)

    # 2) Scalar wikilink fields.
    for field, edge_type in SCALAR_EDGE_FIELDS:
        raw = fm.get(field)
        if isinstance(raw, str) and raw.strip() and raw.strip() != "-":
            add(raw, edge_type)

    # 3) Wikilinks in body → generic 'wikilink' edges.
    for match in WIKILINK_RE.finditer(body):
        add(match.group(1), "wikilink")

    return edges


def scan_vault_and_index(
    vault_root: Path = ROOT,
    con: sqlite3.Connection | None = None,
    db_path: Path | None = None,
) -> int:
    """Walk vault, parse all edges, replace contents of edges table.

    When ``con`` is omitted the connection is opened against ``db_path``, which
    defaults to ``vault_root``'s own ``Memory/relations.sqlite`` — so scanning a
    copied vault can never overwrite this one's. An explicit ``con`` wins and
    ``db_path`` is then ignored.

    Returns total number of edges inserted.
    """
    own_con = False
    if con is None:
        con = init_relations_db(db_path or (vault_root / "Memory" / "relations.sqlite"))
        own_con = True

    clear_edges(con)

    recorded_at = dt.datetime.now().isoformat(timespec="seconds")
    total = 0
    file_count = 0
    for path in iter_markdown_files(vault_root):
        file_count += 1
        batch = edges_from_file(path, vault_root=vault_root, recorded_at=recorded_at)
        if batch:
            insert_edges(con, batch)
            total += len(batch)

    if own_con:
        con.close()

    print(f"indexed {total} edges from {file_count} files")
    return total


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_related(con: sqlite3.Connection, slug: str, depth: int = 1) -> list[dict[str, Any]]:
    """Return edges within ``depth`` hops of ``slug`` (both directions)."""
    visited: set[str] = {slug}
    frontier: set[str] = {slug}
    results: list[dict[str, Any]] = []

    for _ in range(max(1, depth)):
        if not frontier:
            break
        placeholders = ",".join(["?"] * len(frontier))
        rows = con.execute(
            f"""
            SELECT from_slug, to_slug, edge_type, source_file, event_date, recorded_at
            FROM edges
            WHERE from_slug IN ({placeholders}) OR to_slug IN ({placeholders})
            """,
            tuple(frontier) * 2,
        ).fetchall()
        next_frontier: set[str] = set()
        for row in rows:
            r = dict(row)
            results.append(r)
            for k in ("from_slug", "to_slug"):
                if r[k] not in visited:
                    next_frontier.add(r[k])
                    visited.add(r[k])
        frontier = next_frontier

    return results


def most_referenced_slugs(con: sqlite3.Connection, limit: int = 10) -> list[tuple[str, int]]:
    rows = con.execute(
        """
        SELECT to_slug, COUNT(*) AS n
        FROM edges
        GROUP BY to_slug
        ORDER BY n DESC, to_slug ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [(row["to_slug"], int(row["n"])) for row in rows]


def recent_episodes(vault_root: Path | None = None, days: int = 14) -> list[Path]:
    if vault_root is None:
        vault_root = ROOT
    episodes_dir = vault_root / "Memory" / "episodes"
    if not episodes_dir.exists():
        return []
    cutoff = dt.date.today() - dt.timedelta(days=days)
    out: list[Path] = []
    for path in sorted(episodes_dir.glob("*.md")):
        # Episode filenames begin with YYYY-MM-DD by convention.
        try:
            d = dt.date.fromisoformat(path.stem[:10])
        except ValueError:
            # Fall back to mtime if filename does not parse.
            d = dt.date.fromtimestamp(path.stat().st_mtime)
        if d >= cutoff:
            out.append(path)
    return out


def recent_sessions(vault_root: Path | None = None, days: int = 14) -> list[Path]:
    """Manual work-session journals (Memory/sessions/, written by
    todo_system.session_log). Filenames begin with YYYY-MM-DD like episodes."""
    if vault_root is None:
        vault_root = ROOT
    sessions_dir = vault_root / "Memory" / "sessions"
    if not sessions_dir.exists():
        return []
    cutoff = dt.date.today() - dt.timedelta(days=days)
    out: list[Path] = []
    for path in sorted(sessions_dir.glob("*.md")):
        try:
            d = dt.date.fromisoformat(path.stem[:10])
        except ValueError:
            d = dt.date.fromtimestamp(path.stat().st_mtime)
        if d >= cutoff:
            out.append(path)
    return out


def _aliases_for_entity(path: Path) -> list[str]:
    """Return [entity_slug, name, *aliases] from an entity file's frontmatter."""
    try:
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return [path.stem]
    out: list[str] = [path.stem]
    name = fm.get("name")
    if isinstance(name, str) and name.strip():
        out.append(name.strip())
    aliases = fm.get("aliases")
    if isinstance(aliases, list):
        out.extend(a.strip() for a in aliases if isinstance(a, str) and a.strip())
    elif isinstance(aliases, str) and aliases.strip():
        out.append(aliases.strip())
    return out


def scan_unlinked_mentions(vault_root: Path | None = None) -> list[dict[str, Any]]:
    """Find places in vault body text where an entity is mentioned but NOT
    already wrapped as a wikilink. Returns a list of proposal dicts.

    Conservative: only matches entities whose name/alias is >= 3 chars and
    appears at word boundary. Skips matches inside existing ``[[...]]`` and
    inside markdown code fences.
    """
    if vault_root is None:
        vault_root = ROOT
    entities_dir = vault_root / "Memory" / "entities"
    if not entities_dir.exists():
        return []

    # Build (alias_lower, canonical_slug) pairs, sorted by length desc so
    # longer aliases match before shorter ones.
    aliases: list[tuple[str, str]] = []
    for entity_path in sorted(entities_dir.glob("*.md")):
        slug = entity_path.stem
        for name in _aliases_for_entity(entity_path):
            if len(name) < 3:
                continue
            aliases.append((name, slug))
    aliases.sort(key=lambda x: -len(x[0]))

    proposals: list[dict[str, Any]] = []

    for path in iter_markdown_files(vault_root):
        # Don't propose self-references for entity files.
        if path.parent == entities_dir:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _fm, body = parse_frontmatter(content)
        # Mask existing [[wikilinks]] and code blocks to avoid double-matching.
        masked = WIKILINK_RE.sub(lambda m: " " * len(m.group(0)), body)
        # Naive code-fence masking (triple backtick spans).
        masked = re.sub(r"```.*?```", lambda m: " " * len(m.group(0)), masked, flags=re.DOTALL)

        rel = str(path.relative_to(vault_root))
        for alias, slug in aliases:
            # Skip if file is itself this slug (no self-link).
            if path.stem == slug:
                continue
            # Word-boundary search (Korean has no \b — use lookaround for non-letter).
            pattern = re.compile(
                r"(?<![\w가-힣])" + re.escape(alias) + r"(?![\w가-힣])"
            )
            for m in pattern.finditer(masked):
                # Snippet around the match for human review.
                start = max(0, m.start() - 40)
                end = min(len(masked), m.end() + 40)
                snippet = body[start:end].replace("\n", " ⏎ ").strip()
                proposals.append(
                    {
                        "source_file": rel,
                        "alias": alias,
                        "suggested_slug": slug,
                        "offset": m.start(),
                        "snippet": snippet,
                    }
                )

    return proposals


def render_cross_link_queue(proposals: list[dict[str, Any]]) -> str:
    now = dt.datetime.now().isoformat(timespec="seconds")
    today = dt.date.today().isoformat()

    by_source: dict[str, list[dict[str, Any]]] = {}
    for p in proposals:
        by_source.setdefault(p["source_file"], []).append(p)

    out: list[str] = []
    out.append(
        "---\n"
        "tags:\n"
        "  - todo\n"
        "  - inbox\n"
        "  - cross-link\n"
        f"updated: {today}\n"
        "---\n\n"
    )
    out.append("# 🔗 Cross-link 제안 (review queue)\n\n")
    out.append("> 자동 생성 — 사용자가 검토 후 vault 본문에 wikilink로 직접 적용.\n")
    out.append(f"> last_synced: {now}\n")
    out.append(f"> 총 {len(proposals)}건 제안 ({len(by_source)} 파일)\n\n")

    if not proposals:
        out.append("- 제안 없음.\n")
        return "".join(out)

    for source, items in sorted(by_source.items()):
        out.append(f"## [[{Path(source).stem}]]\n\n")
        out.append(f"`{source}`\n\n")
        # Dedupe per source by (alias, snippet hash) to avoid noise.
        seen: set[tuple[str, str]] = set()
        for it in items:
            key = (it["alias"], it["snippet"][:60])
            if key in seen:
                continue
            seen.add(key)
            out.append(
                f"- **{it['alias']}** → `[[{it['suggested_slug']}]]`\n"
                f"  > {it['snippet']}\n"
            )
        out.append("\n")

    out.append(
        "---\n\n"
        "## 적용 방법\n\n"
        "각 항목을 vault 본문에서 찾아 `[[suggested_slug]]` 또는 `[[suggested_slug|표시 텍스트]]` 형식으로 직접 바꿔주세요. "
        "AI는 사용자가 직접 쓴 task 본문을 절대 수정하지 않습니다 (Memory/, Decisions/, Briefings/만 직접 write).\n"
    )
    return "".join(out)


def active_decisions(vault_root: Path | None = None) -> list[tuple[Path, dict[str, Any]]]:
    if vault_root is None:
        vault_root = ROOT
    decisions_dir = vault_root / "Decisions"
    if not decisions_dir.exists():
        return []
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(decisions_dir.glob("*.md")):
        try:
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if str(fm.get("decision_status", "")).lower() == "accepted":
            out.append((path, fm))
    return out


# ---------------------------------------------------------------------------
# Index rendering
# ---------------------------------------------------------------------------


def render_index_markdown(con: sqlite3.Connection, vault_root: Path | None = None) -> str:
    if vault_root is None:
        vault_root = ROOT
    today = dt.date.today().isoformat()
    now = dt.datetime.now().isoformat(timespec="seconds")

    edges_total = con.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"]
    type_counts = Counter(
        row["edge_type"] for row in con.execute("SELECT edge_type FROM edges")
    )

    top_refs = most_referenced_slugs(con, limit=10)
    episodes = recent_episodes(vault_root, days=14)
    sessions = recent_sessions(vault_root, days=14)
    decisions = active_decisions(vault_root)

    out: list[str] = []
    out.append(
        "---\n"
        "tags:\n"
        "  - memory\n"
        "  - index\n"
        f"updated: {today}\n"
        "---\n\n"
    )
    out.append("# 🧠 Memory Index\n\n")
    out.append(
        "> AI 에이전트가 매 brief 시작 시 가장 먼저 읽는 그래프 메모리 진입점. 자동 생성 (직접 수정 금지).\n"
    )
    out.append(f"> last_synced: {now}\n")
    out.append(f"> total edges: {edges_total}\n\n")

    out.append("## 📊 Edge type 분포\n\n")
    if not type_counts:
        out.append("- (edges 없음)\n\n")
    else:
        for edge_type, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            out.append(f"- `{edge_type}`: {count}\n")
        out.append("\n")

    out.append("## 🏷 핵심 entity (참조 빈도 top 10)\n\n")
    if not top_refs:
        out.append("- (참조된 entity 없음)\n\n")
    else:
        for slug, count in top_refs:
            out.append(f"- [[{slug}]] — {count}회 참조\n")
        out.append("\n")

    out.append("## ✅ 활성 의사결정 (`decision_status: accepted`)\n\n")
    if not decisions:
        out.append("- (활성 결정 없음)\n\n")
    else:
        for path, fm in decisions:
            slug = path.stem
            date = fm.get("decision_date") or fm.get("event_date") or "-"
            out.append(f"- [[{slug}]] (날짜: `{date}`)\n")
        out.append("\n")

    out.append("## 📅 최근 14일 episodes\n\n")
    if not episodes:
        out.append("- (최근 episode 없음)\n\n")
    else:
        for path in episodes[-20:]:
            out.append(f"- [[{path.stem}]]\n")
        out.append("\n")

    out.append("## 🛠 최근 14일 수동 작업 세션 (worker 밖)\n\n")
    if not sessions:
        out.append("- (최근 수동 세션 없음)\n\n")
    else:
        for path in sessions[-20:]:
            try:
                fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                fm = {}
            status = fm.get("status", "?")
            ckpts = fm.get("checkpoints", "?")
            out.append(f"- [[{path.stem}]] (`{status}`, {ckpts} checkpoints)\n")
        out.append("\n")

    out.append("## 📜 운영 원칙\n\n")
    out.append(
        "- 매일 아침 brief가 `Memory/episodes/<YYYY-MM-DD>-<slug>.md`를 자동 추가 (M3-b)\n"
        "- worker 밖 **수동 작업**은 `Memory/sessions/`에 기록 — `python3 -m todo_system.session_log log ...` (외부 에이전트가 주기적 호출)\n"
        "- 큰 결정은 `Decisions/<date>-<slug>.md` (ADR 패턴), task가 아닌 결정 자체로 남김\n"
        "- entity 페이지(`Memory/entities/<name>.md`)는 사람·기관·개념·시스템 카드 — 사실 누적, 모순 보존\n"
        "- 모든 변경은 git commit 단위로 (사용자 revert 가능)\n"
        "- 사용자가 vault 본문에 직접 쓴 wikilink는 read-only — AI는 Memory/, Decisions/, Briefings/에만 직접 write\n"
    )

    return "".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    """Resolve ``(vault_root, db_path, index_path)`` for one CLI invocation.

    Without ``--vault-root`` the module globals are handed back *verbatim* rather
    than re-derived from ``ROOT``, so the no-argument path stays byte-identical to
    the one these commands have always opened and ``monkeypatch.setattr(gm,
    "DB_PATH", ...)`` keeps its meaning.

    The supplied root is deliberately **not** ``.resolve()``d: on macOS that
    rewrites ``/tmp`` to ``/private/tmp`` and would break callers comparing the
    written paths against their own tmp directory.
    """
    root = getattr(args, "vault_root", None)
    if root is None:
        return ROOT, DB_PATH, INDEX_PATH
    return root, root / "Memory" / "relations.sqlite", root / "Memory" / "index.md"


def cmd_scan(args: argparse.Namespace) -> int:
    root, db_path, _index_path = _cli_paths(args)
    con = init_relations_db(db_path)
    try:
        scan_vault_and_index(vault_root=root, con=con)
    finally:
        con.close()
    return 0


def cmd_render_index(args: argparse.Namespace) -> int:
    root, db_path, index_path = _cli_paths(args)
    con = init_relations_db(db_path)
    try:
        # Ensure edges are fresh before rendering.
        if con.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"] == 0:
            scan_vault_and_index(vault_root=root, con=con)
        md = render_index_markdown(con, vault_root=root)
    finally:
        con.close()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(md, encoding="utf-8")
    print(f"wrote {index_path.relative_to(root)}")
    return 0


def cmd_cross_links(args: argparse.Namespace) -> int:
    root, _db_path, _index_path = _cli_paths(args)
    proposals = scan_unlinked_mentions(vault_root=root)
    out_path = root / "Todo" / "Inbox" / "cross-link-suggestions.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_cross_link_queue(proposals), encoding="utf-8")
    print(f"wrote {out_path.relative_to(root)} with {len(proposals)} proposals")
    return 0


def cmd_show_related(args: argparse.Namespace) -> int:
    root, db_path, _index_path = _cli_paths(args)
    con = init_relations_db(db_path)
    try:
        if con.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"] == 0:
            scan_vault_and_index(vault_root=root, con=con)
        rows = get_related(con, args.slug, depth=args.depth)
    finally:
        con.close()
    if not rows:
        print(f"no edges related to slug={args.slug}")
        return 0
    for r in rows:
        print(
            f"- {r['from_slug']} --[{r['edge_type']}]--> {r['to_slug']} "
            f"(source={r['source_file']}, event_date={r['event_date'] or '-'})"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="second-brain graph memory indexer (M3-a)")
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared by every subcommand. It hangs off the *subparsers* rather than the
    # top level because the flag is documented to follow the subcommand:
    # ``main(["scan", "--vault-root", str(tmp)])``. ``add_help=False`` is
    # mandatory for a ``parents=`` parser — otherwise ``-h`` is defined twice.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="vault root to operate on (default: this repo)",
    )

    p_scan = sub.add_parser(
        "scan",
        help="Scan vault, rebuild Memory/relations.sqlite",
        parents=[common],
    )
    p_scan.set_defaults(func=cmd_scan)

    p_render = sub.add_parser(
        "render-index",
        help="Write Memory/index.md from current edges",
        parents=[common],
    )
    p_render.set_defaults(func=cmd_render_index)

    p_rel = sub.add_parser("related", help="Show edges related to a slug", parents=[common])
    p_rel.add_argument("slug")
    p_rel.add_argument("--depth", type=int, default=1)
    p_rel.set_defaults(func=cmd_show_related)

    p_xl = sub.add_parser(
        "cross-links",
        help="Scan unlinked entity mentions, write Todo/Inbox/cross-link-suggestions.md",
        parents=[common],
    )
    p_xl.set_defaults(func=cmd_cross_links)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
