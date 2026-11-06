"""Graph Memory indexer (M3-a).

Walks the markdown vault, parses wikilinks + frontmatter relationship fields,
and populates ``Memory/relations.sqlite`` so future briefs can traverse the
accumulated knowledge graph.

Schema is bi-temporal (plan Phase 2.6): ``event_date`` is valid-time (when the
fact held), ``recorded_at`` is transaction-time (when it was written), and
``valid_until`` tombstones a relation that no longer holds. Each edge also
carries nullable ``run_id`` / ``commit_sha`` provenance (memory v2 · G1). Rescans
sync **incrementally** — a vanished relation is tombstoned, not deleted — so
``recorded_at`` and history survive; current-valid queries read
``valid_until IS NULL`` rows only.

Stdlib only. Intentionally duplicates a minimal frontmatter parser so this
module stays decoupled from ``todo_system.cli``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence

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


def _edge_signature(e: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a relation for the incremental tombstone diff.

    Two edges are "the same relation" iff this 6-tuple matches. ``event_date``
    and ``confidence`` are part of it on purpose: a change in either tombstones
    the old row and inserts a new one (rather than silently mutating history),
    while a byte-identical row is left untouched so its ``recorded_at`` is
    preserved. ``recorded_at`` / ``valid_until`` / provenance are deliberately
    excluded — they are the bookkeeping the diff maintains, not identity.
    """
    return (
        e["from_slug"],
        e["to_slug"],
        e["edge_type"],
        e["source_file"],
        e.get("event_date"),
        e.get("confidence"),
    )


def scan_vault_and_index(
    vault_root: Path = ROOT,
    con: sqlite3.Connection | None = None,
    db_path: Path | None = None,
) -> int:
    """Walk vault, parse all edges, sync the edges table incrementally.

    Instead of ``clear_edges`` + full reinsert, this reconciles the live
    (``valid_until IS NULL``) rows against the edges the current vault would
    produce, matched by :func:`_edge_signature` with multiplicity:

    * a relation (or one of its duplicate occurrences) that is no longer present
      has ``valid_until`` stamped — a **tombstone**, not a delete, so history and
      the original ``recorded_at`` survive;
    * a newly-appeared relation (or an extra occurrence) is inserted fresh with
      ``recorded_at`` = now and ``valid_until`` NULL;
    * a byte-identical relation is left exactly as it was (``recorded_at`` kept).

    The net effect on the *live* multiset equals what a full rebuild would have
    produced, so current-valid queries are invariant (design §R1), while the
    tombstoned rows accumulate the bi-temporal history.

    When ``con`` is omitted the connection is opened against ``db_path``, which
    defaults to ``vault_root``'s own ``Memory/relations.sqlite`` — so scanning a
    copied vault can never overwrite this one's. An explicit ``con`` wins and
    ``db_path`` is then ignored.

    Returns the number of live edges after the sync (= what a full rebuild would
    have inserted).
    """
    own_con = False
    if con is None:
        con = init_relations_db(db_path or (vault_root / "Memory" / "relations.sqlite"))
        own_con = True

    recorded_at = dt.datetime.now().isoformat(timespec="seconds")

    # Desired state: every edge the current vault would produce (a full rebuild).
    desired: list[dict[str, Any]] = []
    file_count = 0
    for path in iter_markdown_files(vault_root):
        file_count += 1
        desired.extend(
            edges_from_file(path, vault_root=vault_root, recorded_at=recorded_at)
        )
    desired_counts = Counter(_edge_signature(e) for e in desired)

    # Live state: currently-valid rows grouped by signature, id-ordered so the
    # highest ids are the ones tombstoned when multiplicity drops (deterministic).
    live_ids: dict[tuple[Any, ...], list[int]] = {}
    for row in con.execute(
        """
        SELECT id, from_slug, to_slug, edge_type, source_file, event_date, confidence
        FROM edges
        WHERE valid_until IS NULL
        ORDER BY id
        """
    ):
        sig = (
            row["from_slug"],
            row["to_slug"],
            row["edge_type"],
            row["source_file"],
            row["event_date"],
            row["confidence"],
        )
        live_ids.setdefault(sig, []).append(row["id"])

    # Tombstone the surplus: live occurrences beyond the desired multiplicity
    # (relation gone, or now seen fewer times than before).
    tombstoned = 0
    for sig, ids in live_ids.items():
        want = desired_counts.get(sig, 0)
        for edge_id in ids[want:]:
            con.execute(
                "UPDATE edges SET valid_until = ? WHERE id = ?", (recorded_at, edge_id)
            )
            tombstoned += 1

    # Insert the deficit: desired occurrences the live set does not already cover.
    # Walk ``desired`` in order, taking the shortfall per signature; every
    # occurrence of a signature is identical for insertion, so which we take
    # does not matter.
    deficit: Counter[tuple[Any, ...]] = Counter()
    for sig, want in desired_counts.items():
        shortfall = want - len(live_ids.get(sig, ()))
        if shortfall > 0:
            deficit[sig] = shortfall
    to_insert: list[dict[str, Any]] = []
    for e in desired:
        sig = _edge_signature(e)
        if deficit.get(sig, 0) > 0:
            to_insert.append(e)
            deficit[sig] -= 1
    insert_edges(con, to_insert)

    # insert_edges commits only when it inserts; commit here so a tombstone-only
    # scan (deficit empty) still flushes the UPDATEs above.
    con.commit()

    if own_con:
        con.close()

    live_total = len(desired)
    print(
        f"indexed {live_total} live edges (+{tombstoned} tombstoned) "
        f"from {file_count} files"
    )
    return live_total


# ---------------------------------------------------------------------------
# Embeddings (memory v2 · G2) — local embedder seam + R5 boundary filter + corpus
# ---------------------------------------------------------------------------
#
# Additive on top of G1. Three pieces, all stdlib and extension-free here (the
# sqlite-vec vec0 table + lazy load live further down):
#
#   1. a deterministic, zero-network *default* embedder (feature-hashing) behind
#      an injectable seam — the real sentence model + quantization is deferred to
#      the G4 §5 bench (design §3.4, decision #6); tests inject a tiny fake;
#   2. the R5 privacy boundary filter — SMS / mail body / health-raw / OTP docs
#      (by path segment or frontmatter tag) are never embedded (design §2 R5,
#      CLAUDE.md §11/§11b: "볼트나 외부로 옮겨 적지 않는다");
#   3. the corpus walk that turns each embeddable doc into a summary text.

# vec0 vector dimension. A module constant, threaded as ``dim=`` through
# ``init_vec0`` / ``embed_vault`` so it stays configurable without touching the
# seam. 256 is a deliberate, cheap default for the stdlib feature-hashing
# embedder; the real model's native dimension is a G4 bench decision.
EMBED_DIM = 256

# R5 boundary denylist (design §2 R5). Defensive: these categories do not exist
# in the current vault corpus, but the filter is fixed by a RED test so a future
# SMS/mail/health-raw doc can never leak into an embedding vector. Matched as a
# whole **path segment** (never a substring) and against frontmatter ``tags``;
# an implementer may widen these sets but must not narrow below SMS/mail-body/
# health-raw. No absolute home paths / real identifiers here (public-id gate).
EMBED_DENY_PATH_SEGMENTS: tuple[str, ...] = (
    "sms",
    "mail",
    "inbox",
    "health-raw",
    "health_raw",
    "otp",
)
EMBED_DENY_TAGS: tuple[str, ...] = (
    "sms",
    "mail",
    "health-raw",
    "health_raw",
    "pii",
    "otp",
    "private-health",
)

# Frontmatter fields whose string value is a human-written summary worth
# embedding (title/outcome/… beats raw body alone). Body is appended after.
_EMBED_SUMMARY_FIELDS: tuple[str, ...] = (
    "title",
    "name",
    "outcome",
    "summary",
    "next_action",
    "if_then",
    "description",
)

# Tokeniser for the feature-hashing embedder: unicode word runs (``\w`` already
# covers Hangul in Python 3), lowercased. Deterministic and stdlib-only.
_EMBED_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _embed_tokens(text: str) -> list[str]:
    return _EMBED_TOKEN_RE.findall(text.lower())


def _default_embedder(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic, offline, stdlib-only feature-hashing embedder.

    Each token is hashed (``hashlib`` — **not** the salted builtin ``hash``, so
    the vector is byte-identical across processes and runs) into one of ``dim``
    buckets with a sign bit, counts are accumulated, and the vector is
    L2-normalised. No ML dependency, no network. This is the "경량 결정적 로컬
    기본값" the requirement asks for; the real model is a G4 decision. It is the
    default behind the ``embed_vault`` seam, which tests override with a fake.
    """
    vec = [0.0] * dim
    for tok in _embed_tokens(text):
        digest = hashlib.sha1(tok.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0.0:
        vec = [v / norm for v in vec]
    return vec


def _content_hash(text: str) -> str:
    """sha256 of the embed-input text — the change-detection key for incremental
    re-embedding (a byte-identical summary is never re-embedded)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path_segments(rel_path: str | Path) -> set[str]:
    """Lower-cased path segments of a vault-relative path, plus the filename
    stem as its own segment (so ``Notes/sms.md`` is caught while
    ``Notes/smsidebar.md`` is not — segment match, never substring)."""
    p = Path(rel_path)
    segments = {part.lower() for part in p.parts}
    segments.add(p.stem.lower())
    return segments


def is_embeddable(rel_path: str | Path, fm: dict[str, Any]) -> bool:
    """R5 boundary filter: False iff the doc is on the privacy denylist.

    Denied when any path segment is in :data:`EMBED_DENY_PATH_SEGMENTS` **or** any
    frontmatter ``tags`` entry is in :data:`EMBED_DENY_TAGS`. Everything else is
    embeddable. Pinned by a RED test (``test_graph_memory_embeddings.py``).
    """
    if _path_segments(rel_path) & set(EMBED_DENY_PATH_SEGMENTS):
        return False

    raw_tags = fm.get("tags")
    if isinstance(raw_tags, list):
        tags = {str(t).strip().lower() for t in raw_tags}
    elif isinstance(raw_tags, str):
        tags = {raw_tags.strip().lower()}
    else:
        tags = set()
    if tags & set(EMBED_DENY_TAGS):
        return False

    return True


def _embed_input_text(path: Path, fm: dict[str, Any], body: str, *, max_chars: int = 4000) -> str:
    """Derive the summary text to embed from a doc: filename stem + human-written
    summary frontmatter fields + body, truncated. Never the raw sensitive fields
    (those docs are filtered out upstream by :func:`is_embeddable`)."""
    parts: list[str] = [path.stem]
    for key in _EMBED_SUMMARY_FIELDS:
        val = fm.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    body_text = body.strip()
    if body_text:
        parts.append(body_text)
    return "\n".join(parts)[:max_chars]


class CorpusDoc(NamedTuple):
    """One embeddable document: its vault-relative path, the embed-input text,
    and its mtime (a cheap incremental pre-filter; the content hash is authoritative)."""

    source_file: str
    text: str
    mtime: float


def iter_embed_corpus(vault_root: Path = ROOT) -> Iterable[CorpusDoc]:
    """Walk SCAN_ROOTS and yield a :class:`CorpusDoc` per **embeddable** doc.

    Reuses :func:`iter_markdown_files` (so ``Memory/sessions`` is already out of
    scope), applies the R5 filter, and derives a non-empty summary text for each
    survivor. Denied or unreadable or empty-summary docs are skipped.
    """
    for path in iter_markdown_files(vault_root):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(content)
        rel = str(path.relative_to(vault_root))
        if not is_embeddable(rel, fm):
            continue
        text = _embed_input_text(path, fm, body)
        if not text.strip():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        yield CorpusDoc(source_file=rel, text=text, mtime=mtime)


# --- incremental plan (plain companion table — no extension needed) ---------
#
# Vectors are a *derived* cache (design §4.3 point 3): the first embed is a full
# batch, later runs re-embed only changed/new docs and drop gone/newly-denied
# ones. The change-detection state lives in a plain ``vec_sources`` table whose
# ``rowid`` is the link to the matching ``vec_embeddings`` (vec0) row — a plain
# table so the whole plan / dry-run path is testable with sqlite-vec absent.


class PlanItem(NamedTuple):
    """A doc the next embed must (re)embed: its path, text, content hash, mtime."""

    source_file: str
    text: str
    content_hash: str
    mtime: float


@dataclass
class EmbedResult:
    """Outcome of an :func:`embed_vault` call (counts only — no vectors/text)."""

    total_corpus: int
    to_embed: int
    to_remove: int
    embedded: int
    removed: int
    unchanged: int
    dim: int
    dry_run: bool
    skipped_reason: str | None


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def init_vec_sources(con: sqlite3.Connection) -> None:
    """Create the plain ``vec_sources`` companion table (idempotent). Its
    ``rowid`` doubles as the ``vec_embeddings`` (vec0) rowid for each source."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS vec_sources (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT UNIQUE NOT NULL,
            content_hash TEXT NOT NULL,
            mtime REAL,
            dim INTEGER NOT NULL,
            embedded_at TEXT NOT NULL
        )
        """
    )
    con.commit()


def plan_embedding(
    con: sqlite3.Connection,
    corpus: Iterable[CorpusDoc],
    *,
    rebuild: bool = False,
) -> tuple[list[PlanItem], list[str], list[str]]:
    """Diff the current corpus against ``vec_sources`` — **read-only, no writes**.

    Returns ``(to_embed, unchanged, to_remove)``:

    * **to_embed** — new docs, docs whose embed-input text changed (content hash
      differs), and (when ``rebuild``) every doc;
    * **unchanged** — source_files whose content hash matches the stored one;
    * **to_remove** — source_files in ``vec_sources`` no longer in the corpus
      (deleted on disk, or newly denied by the R5 filter).

    A missing ``vec_sources`` table reads as "nothing embedded yet" → first run
    is a full batch. Never writes, so the dry-run path is side-effect-free.
    """
    existing: dict[str, str] = {}
    if _table_exists(con, "vec_sources"):
        for row in con.execute("SELECT source_file, content_hash FROM vec_sources"):
            existing[row["source_file"]] = row["content_hash"]

    to_embed: list[PlanItem] = []
    unchanged: list[str] = []
    seen: set[str] = set()
    for doc in corpus:
        seen.add(doc.source_file)
        content_hash = _content_hash(doc.text)
        prior = existing.get(doc.source_file)
        if rebuild or prior is None or prior != content_hash:
            to_embed.append(
                PlanItem(doc.source_file, doc.text, content_hash, doc.mtime)
            )
        else:
            unchanged.append(doc.source_file)

    to_remove = sorted(sf for sf in existing if sf not in seen)
    return to_embed, unchanged, to_remove


# --- sqlite-vec lazy load + vec0 build/upsert (the optional extension) -------


class EmbeddingsUnavailable(RuntimeError):
    """The sqlite-vec extension could not be loaded (not installed, or this
    Python/SQLite was built without loadable-extension support). Callers turn
    this into a clear "skipping embed" message and keep the rest of
    graph_memory working — it is never fatal."""


def _load_sqlite_vec(con: sqlite3.Connection):
    """Lazily import sqlite-vec and load the extension into ``con``.

    The import is **inside this function only** — never at module top — so
    ``import todo_system.graph_memory`` (and the whole scan/index/query path,
    and the installed-wheel smoke) stay importable with sqlite-vec absent. It
    goes through :func:`importlib.import_module` rather than a literal ``import
    sqlite_vec`` statement on purpose: ``graph_memory`` is on the bare-clone
    stdlib-only contract (CLAUDE.md §9, ``test_session_log`` AST gate), and a
    static ``import sqlite_vec`` anywhere in the file — even guarded — would
    break it. The dependency stays genuinely optional: ``ImportError`` here is
    turned into a graceful skip, so a dependency-free clone never crashes.
    Raises :class:`EmbeddingsUnavailable` for both "not installed" and "this
    python has no loadable-extension support". Returns the ``sqlite_vec`` module
    (for ``serialize_float32``).
    """
    try:
        sqlite_vec = importlib.import_module("sqlite_vec")
    except ImportError as exc:  # not installed (the shipped wheel's state)
        raise EmbeddingsUnavailable("sqlite-vec is not installed") from exc
    try:
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError) as exc:
        raise EmbeddingsUnavailable(
            f"sqlite-vec could not be loaded into this SQLite build: {exc}"
        ) from exc
    return sqlite_vec


def init_vec0(con: sqlite3.Connection, dim: int) -> None:
    """Create the ``vec_embeddings`` vec0 virtual table (idempotent). Requires
    the extension already loaded (:func:`_load_sqlite_vec`). ``dim`` is int-coerced
    into the DDL — it cannot be parameterised, so the coercion is the injection
    guard. A pre-existing table with a different ``dim`` is left as-is by
    ``IF NOT EXISTS`` (a dim change would need a rebuild — out of G2 scope)."""
    con.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(embedding float[{int(dim)}])"
    )
    con.commit()


def _print_embed_plan(
    to_embed: list[PlanItem], unchanged: list[str], to_remove: list[str], *, dim: int
) -> None:
    print(f"embed plan (dry-run) — dim={dim}, corpus sees {len(to_embed) + len(unchanged)} docs")
    print(f"  to embed ({len(to_embed)}):")
    for item in to_embed:
        print(f"    + {item.source_file}")
    print(f"  unchanged: {len(unchanged)}")
    print(f"  to remove ({len(to_remove)}):")
    for source_file in to_remove:
        print(f"    - {source_file}")


def embed_vault(
    con: sqlite3.Connection,
    *,
    vault_root: Path = ROOT,
    embedder: Callable[[str], Sequence[float]] = _default_embedder,
    dim: int = EMBED_DIM,
    dry_run: bool = False,
    rebuild: bool = False,
) -> EmbedResult:
    """Embed the vault corpus into the ``vec_embeddings`` vec0 table, incrementally.

    Walks :func:`iter_embed_corpus` (R5-filtered), diffs it against ``vec_sources``
    with :func:`plan_embedding`, and:

    * ``dry_run=True`` — prints the plan and returns, touching **nothing** (no
      vec0 / vec_sources write, no sqlite-vec needed);
    * sqlite-vec absent — prints a clear skip message and returns with
      ``skipped_reason`` set (the rest of graph_memory is unaffected);
    * otherwise — builds the tables, applies the plan (insert new vectors,
      update changed ones in place by rowid, delete gone/denied ones), and upserts
      ``vec_sources``.

    ``embedder`` is the injectable seam (default: the stdlib feature-hashing
    :func:`_default_embedder`); tests pass a fake. ``dim`` must match what the
    embedder produces — a mismatch raises, so a wrong fake fails loudly.
    """
    corpus = list(iter_embed_corpus(vault_root))
    to_embed, unchanged, to_remove = plan_embedding(con, corpus, rebuild=rebuild)

    def _result(embedded: int, removed: int, skipped_reason: str | None, dry: bool) -> EmbedResult:
        return EmbedResult(
            total_corpus=len(corpus),
            to_embed=len(to_embed),
            to_remove=len(to_remove),
            embedded=embedded,
            removed=removed,
            unchanged=len(unchanged),
            dim=dim,
            dry_run=dry,
            skipped_reason=skipped_reason,
        )

    if dry_run:
        _print_embed_plan(to_embed, unchanged, to_remove, dim=dim)
        return _result(0, 0, None, True)

    try:
        vec_ext = _load_sqlite_vec(con)
    except EmbeddingsUnavailable as exc:
        print(f"sqlite-vec not installed — skipping embed ({exc})")
        return _result(0, 0, str(exc), False)

    init_vec_sources(con)
    init_vec0(con, dim)

    existing_rowids = {
        row["source_file"]: row["rowid"]
        for row in con.execute("SELECT rowid, source_file FROM vec_sources")
    }
    now = dt.datetime.now().isoformat(timespec="seconds")
    serialize = vec_ext.serialize_float32

    embedded = 0
    for item in to_embed:
        vector = list(embedder(item.text))
        if len(vector) != dim:
            raise ValueError(
                f"embedder returned {len(vector)} dims, expected {dim} "
                f"(for {item.source_file}) — embedder/dim mismatch"
            )
        blob = serialize([float(x) for x in vector])
        rowid = existing_rowids.get(item.source_file)
        if rowid is None:
            cur = con.execute(
                "INSERT INTO vec_sources (source_file, content_hash, mtime, dim, embedded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item.source_file, item.content_hash, item.mtime, dim, now),
            )
            con.execute(
                "INSERT INTO vec_embeddings (rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, blob),
            )
        else:
            con.execute(
                "UPDATE vec_sources SET content_hash = ?, mtime = ?, dim = ?, embedded_at = ? "
                "WHERE source_file = ?",
                (item.content_hash, item.mtime, dim, now, item.source_file),
            )
            con.execute(
                "UPDATE vec_embeddings SET embedding = ? WHERE rowid = ?", (blob, rowid)
            )
        embedded += 1

    removed = 0
    for source_file in to_remove:
        rowid = existing_rowids.get(source_file)
        if rowid is not None:
            con.execute("DELETE FROM vec_embeddings WHERE rowid = ?", (rowid,))
        con.execute("DELETE FROM vec_sources WHERE source_file = ?", (source_file,))
        removed += 1

    con.commit()
    print(
        f"embedded {embedded} docs ({len(unchanged)} unchanged, {removed} removed) "
        f"into vec0[{dim}] from {len(corpus)} corpus docs"
    )
    return _result(embedded, removed, None, False)


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
            WHERE (from_slug IN ({placeholders}) OR to_slug IN ({placeholders}))
              AND valid_until IS NULL
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


# --- hybrid query (memory v2 · G3): semantic KNN + structural BFS -----------
#
# ``query_related`` blends two orthogonal, separately-testable signals and returns a
# ranked list whose items each carry provenance + a split score:
#
#   * semantic — an embedding-KNN over the G2 ``vec_embeddings`` vec0 table
#     (``_knn_seeds``), behind the same lazy ``_load_sqlite_vec`` seam and the same
#     graceful-degradation contract as ``embed_vault``;
#   * structural — a ``get_related``-style BFS from the task's own slug over the G1
#     bi-temporal ``edges`` table (``_traverse_from_slug``), honouring an ``as_of``
#     time axis.
#
# ``get_related`` is left byte-for-byte unchanged (parallel change / 병존); the BFS
# here is a *separate* function so its SELECT can add provenance columns and the
# time predicate without disturbing the current-valid reader everything else uses.


@dataclass(frozen=True)
class RelatedScore:
    """Split score for one hybrid-query hit. Both components are always returned
    separately — the blend weight is a G4-tunable knob (design §5.2)."""

    vector: float  # semantic KNN component; 0.0 if no hit / embeddings unavailable
    graph: float  # structural BFS component; 0.0 if not reached from the task slug
    combined: float  # vector_weight*vector + graph_weight*graph


@dataclass(frozen=True)
class RelatedItem:
    """One ranked hybrid-query result with its provenance and split score."""

    slug: str  # the related node (file stem / edge endpoint)
    source_file: str | None  # connecting edge's source, else the KNN doc's path
    run_id: str | None  # from the connecting edge; None for a pure-semantic hit
    commit_sha: str | None  # from the connecting edge; None for a pure-semantic hit
    score: RelatedScore
    edge_type: str | None  # discovering edge's type; None for a pure-semantic hit
    hops: int | None  # BFS hop distance from the task slug; None if only semantic


def _task_query_text(task: Mapping[str, Any]) -> str:
    """Compose the KNN query text from a task identity — ``slug`` + ``title`` +
    ``touches`` (+ ``outcome`` / ``next_action`` when present) — mirroring
    :func:`_embed_input_text`'s "stem + summary fields" shape so a task queries the
    corpus in the same vocabulary it was embedded in. ``task`` is read with
    ``.get()`` so missing keys are tolerated (R4: "slug·제목·touches 로 질의")."""
    parts: list[str] = []
    for key in ("slug", "title"):
        val = task.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    touches = task.get("touches")
    if isinstance(touches, (list, tuple)):
        parts.extend(str(t).strip() for t in touches if str(t).strip())
    elif isinstance(touches, str) and touches.strip():
        parts.append(touches.strip())
    for key in ("outcome", "next_action"):
        val = task.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return "\n".join(parts)


def _traverse_from_slug(
    con: sqlite3.Connection,
    slug: str,
    depth: int,
    as_of: str | None,
) -> dict[str, dict[str, Any]]:
    """BFS from ``slug`` over ``edges`` (both directions), returning — per reached
    slug, the start excluded — the ``{hop, source_file, run_id, commit_sha,
    edge_type}`` of the edge that *first* discovered it (``ORDER BY id`` + a
    ``visited`` set, so the lowest-id discovering edge wins and the result is
    deterministic).

    Separate from :func:`get_related` on purpose — its SELECT adds the provenance
    columns and a time predicate. When ``as_of is None`` the window is the
    current-valid one (``valid_until IS NULL``, identical to ``get_related``);
    otherwise it is the set of edges valid at that instant:
    ``recorded_at <= as_of AND (valid_until IS NULL OR valid_until > as_of)``.
    """
    if not slug:
        return {}
    if as_of is None:
        time_clause = "AND valid_until IS NULL"
        time_params: tuple[str, ...] = ()
    else:
        time_clause = "AND recorded_at <= ? AND (valid_until IS NULL OR valid_until > ?)"
        time_params = (as_of, as_of)

    visited: set[str] = {slug}
    frontier: set[str] = {slug}
    found: dict[str, dict[str, Any]] = {}

    for hop in range(1, max(1, depth) + 1):
        if not frontier:
            break
        placeholders = ",".join(["?"] * len(frontier))
        rows = con.execute(
            f"""
            SELECT from_slug, to_slug, edge_type, source_file, run_id, commit_sha
            FROM edges
            WHERE (from_slug IN ({placeholders}) OR to_slug IN ({placeholders}))
              {time_clause}
            ORDER BY id
            """,
            tuple(frontier) * 2 + time_params,
        ).fetchall()
        next_frontier: set[str] = set()
        for row in rows:
            r = dict(row)
            for key in ("from_slug", "to_slug"):
                other = r[key]
                if other in visited:
                    continue
                visited.add(other)
                next_frontier.add(other)
                found[other] = {
                    "hop": hop,
                    "source_file": r["source_file"],
                    "run_id": r["run_id"],
                    "commit_sha": r["commit_sha"],
                    "edge_type": r["edge_type"],
                }
        frontier = next_frontier

    return found


def _knn_seeds(
    con: sqlite3.Connection,
    query_text: str,
    *,
    k: int,
    embedder: Callable[[str], Sequence[float]],
    dim: int,
) -> dict[str, dict[str, Any]]:
    """Semantic layer: the ``k`` nearest embedded corpus docs to ``query_text`` as
    ``{slug: {"vector": score, "source_file": path}}``, slug = the doc's path stem.

    Graceful degradation, identical to :func:`embed_vault`: yields ``{}`` (never
    raises) when ``k <= 0``, when the vault was never embedded (``vec_embeddings`` /
    ``vec_sources`` absent), or when sqlite-vec cannot be loaded at query time
    (:class:`EmbeddingsUnavailable`). sqlite-vec stays behind the lazy
    :func:`_load_sqlite_vec` seam — no top-level ``import sqlite_vec`` (stdlib-only
    contract). An embedder whose output length ``!= dim`` is a loud ``ValueError``,
    matching ``embed_vault`` (a wrong fake fails fast, it is not a silent skip).

    The vector score is ``1.0 / (1.0 + distance)`` over sqlite-vec's L2 ``distance``
    (selectable alongside ``rowid`` in a KNN ``MATCH`` query). Rows come back
    distance-ascending, so the first occurrence of a slug is its nearest doc; a
    later rowid sharing the same path stem is ignored. The ``LIMIT ?`` (= ``k``) is
    mandatory for a vec0 KNN query.
    """
    if k <= 0:
        return {}
    if not _table_exists(con, "vec_embeddings") or not _table_exists(con, "vec_sources"):
        return {}
    try:
        vec_ext = _load_sqlite_vec(con)
    except EmbeddingsUnavailable:
        return {}

    vector = list(embedder(query_text))
    if len(vector) != dim:
        raise ValueError(
            f"embedder returned {len(vector)} dims, expected {dim} — embedder/dim mismatch"
        )
    blob = vec_ext.serialize_float32([float(x) for x in vector])

    seeds: dict[str, dict[str, Any]] = {}
    rows = con.execute(
        "SELECT rowid, distance FROM vec_embeddings WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT ?",
        (blob, k),
    ).fetchall()
    for row in rows:
        src = con.execute(
            "SELECT source_file FROM vec_sources WHERE rowid = ?", (row["rowid"],)
        ).fetchone()
        if src is None:
            continue
        source_file = src["source_file"]
        slug = Path(source_file).stem
        if slug in seeds:
            continue  # distance-ordered → the first row per slug is its nearest doc
        seeds[slug] = {
            "vector": 1.0 / (1.0 + row["distance"]),
            "source_file": source_file,
        }
    return seeds


def query_related(
    con: sqlite3.Connection,
    task: Mapping[str, Any],
    *,
    k: int = 10,
    depth: int = 1,
    as_of: str | None = None,
    embedder: Callable[[str], Sequence[float]] = _default_embedder,
    dim: int = EMBED_DIM,
    vector_weight: float = 0.5,
    graph_weight: float = 0.5,
) -> list[RelatedItem]:
    """Hybrid retrieval for a task identity: blend a semantic embedding-KNN with a
    structural bi-temporal BFS and return a provenance-carrying, ranked list.

    Given ``task`` (a mapping read with ``.get()`` — ``slug`` + ``title`` +
    ``touches`` + ``outcome`` / ``next_action``), the query (1) **semantically**
    finds the ``k`` nearest embedded corpus docs (:func:`_knn_seeds`, over the G2
    vec0 table) and (2) **structurally** expands the task's own slug through the G1
    ``edges`` graph (:func:`_traverse_from_slug`, ``depth`` hops, ``as_of``-filtered),
    then unions the two candidate sets into :class:`RelatedItem`\\ s.

    Each item keeps both score components separately (``vector`` / ``graph``) plus
    their weighted ``combined``; the list is ranked by ``combined`` **desc**,
    tie-broken by ``slug`` **asc**, with the task's own slug excluded. Provenance
    precedence: a structurally-reached slug carries its connecting edge's
    ``source_file`` / ``run_id`` / ``commit_sha`` / ``edge_type`` / ``hops``; a
    pure-semantic hit carries the KNN doc's ``source_file`` with
    ``run_id = commit_sha = edge_type = hops = None``.

    **Graceful fallback** (never raises): when sqlite-vec is unavailable or the vault
    was never embedded, the semantic layer yields nothing and the result is BFS-only
    — the same contract :func:`embed_vault` honours.

    **Time semantics**: ``as_of`` narrows only the *structural* traversal to the
    edges valid at that instant. The vector KNN is a latest-snapshot layer (vectors
    are a derived cache with no per-vector validity window), so ``as_of`` does not
    rewind it — a documented G3 limitation, not solved here. The graph layer is
    seeded from the task's **own slug only**; seeding BFS additionally from the
    semantic hits is a G4 blend-tuning option, out of G3 scope (keeps the two signals
    orthogonal and separately testable).
    """
    task_slug = task.get("slug")
    graph_hits = (
        _traverse_from_slug(con, task_slug, depth, as_of) if task_slug else {}
    )

    query_text = _task_query_text(task)
    vector_hits = _knn_seeds(con, query_text, k=k, embedder=embedder, dim=dim)

    items: list[RelatedItem] = []
    for slug in set(graph_hits) | set(vector_hits):
        if slug == task_slug:
            continue
        g = graph_hits.get(slug)
        v = vector_hits.get(slug)
        vector_score = v["vector"] if v else 0.0
        graph_score = 1.0 / (1.0 + g["hop"]) if g else 0.0
        combined = vector_weight * vector_score + graph_weight * graph_score
        if g is not None:
            source_file = g["source_file"]
            run_id = g["run_id"]
            commit_sha = g["commit_sha"]
            edge_type = g["edge_type"]
            hops = g["hop"]
        else:
            source_file = v["source_file"]
            run_id = None
            commit_sha = None
            edge_type = None
            hops = None
        items.append(
            RelatedItem(
                slug=slug,
                source_file=source_file,
                run_id=run_id,
                commit_sha=commit_sha,
                score=RelatedScore(
                    vector=vector_score, graph=graph_score, combined=combined
                ),
                edge_type=edge_type,
                hops=hops,
            )
        )

    items.sort(key=lambda it: (-it.score.combined, it.slug))
    return items


def most_referenced_slugs(con: sqlite3.Connection, limit: int = 10) -> list[tuple[str, int]]:
    rows = con.execute(
        """
        SELECT to_slug, COUNT(*) AS n
        FROM edges
        WHERE valid_until IS NULL
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

    # Current-valid only: tombstoned (valid_until IS NOT NULL) history rows must
    # not inflate the totals or the type distribution the brief/planner read.
    edges_total = con.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE valid_until IS NULL"
    ).fetchone()["n"]
    type_counts = Counter(
        row["edge_type"]
        for row in con.execute(
            "SELECT edge_type FROM edges WHERE valid_until IS NULL"
        )
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


def cmd_embed(args: argparse.Namespace) -> int:
    """Embed the vault corpus into the vec0 table (or just show the plan with
    ``--dry-run``). Always returns 0 — a missing sqlite-vec is a graceful skip,
    not an error."""
    root, db_path, _index_path = _cli_paths(args)
    con = init_relations_db(db_path)
    try:
        embed_vault(con, vault_root=root, dry_run=args.dry_run, rebuild=args.rebuild)
    finally:
        con.close()
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

    p_embed = sub.add_parser(
        "embed",
        help="Embed the vault corpus into the sqlite-vec vec0 table (incremental)",
        parents=[common],
    )
    p_embed.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be embedded/removed without writing (needs no sqlite-vec)",
    )
    p_embed.add_argument(
        "--rebuild",
        action="store_true",
        help="re-embed every corpus doc, ignoring the incremental content-hash check",
    )
    p_embed.set_defaults(func=cmd_embed)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
