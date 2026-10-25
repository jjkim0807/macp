"""Transport-neutral tracker contract + the markdown bridge (design §⑨).

The :class:`TrackerAdapter` ABC mirrors the §⑨ draft signatures
(``pull`` / ``push`` / ``map_fields`` / ``map_status``) and is transport-neutral:
Jira/GitHub speak REST, Linear speaks GraphQL, so a concrete adapter owns its own
transport and only ever hands this layer a neutral :class:`TaskRecord`.

This module is **stdlib + :mod:`todo_system.paths` only** — never ``cli``. The
frontmatter helpers (:func:`parse_frontmatter`, :func:`set_scalar`) are deliberately
re-implemented here rather than imported from ``cli``, which pulls a heavy import
graph (calendar sync, worker, graph_memory …); their behaviour mirrors
``cli.parse_frontmatter`` / ``cli.update_frontmatter_value`` line-for-line so a task
md round-trips identically whichever writer last touched it.

Markdown is the source of truth ([[Decisions/2026-10-13-macp-task-store]]); the
tracker is a view. :func:`upsert_task_record` enforces that: on an existing task it
fills only empty md-owned fields, applies the monotonic "done 승격" status rule, and
on a genuine divergence (remote advanced since last sync **and** an md-owned field
differs) it keeps the md and leaves a human-readable note in ``## Progress Log``
rather than overwriting. The two fields the tracker owns are the scalar frontmatter
keys ``external_ref`` (e.g. ``jira:PROJ-123``) and ``external_updated_at`` (the remote
``updated`` observed at the last successful sync).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths


# --------------------------------------------------------------------------- #
# DTO + contract
# --------------------------------------------------------------------------- #


@dataclass
class TaskRecord:
    """Neutral import/export unit — a frontmatter dict + body + external ref.

    Not a new store: it carries exactly what a tracker round-trip needs. ``title``
    becomes the body H1; ``frontmatter`` holds the mapped md-owned scalar/list
    fields (tags/due/priority); ``status`` is the task-level ``open|done`` (never a
    ledger state); ``pr_url`` is set only for ``push``; ``remote_updated_at`` is the
    tracker's own ``updated`` timestamp, persisted as ``external_updated_at`` so the
    next sync can tell whether the remote advanced.
    """

    external_ref: str
    title: str = ""
    body: str = ""
    frontmatter: dict = field(default_factory=dict)
    status: str | None = None
    pr_url: str | None = None
    remote_updated_at: str | None = None


class TrackerAdapter(ABC):
    """External tracker ↔ task md bridge (transport-neutral — §⑨)."""

    @abstractmethod
    def pull(self, since: str | None = None) -> list[TaskRecord]:
        """Tracker → macp (import). ``since`` is an incremental cursor.

        Idempotent at the md layer: the same ``external_ref`` upserts the same
        task (:func:`upsert_task_record` never duplicates)."""

    @abstractmethod
    def push(self, task: TaskRecord) -> str:
        """macp → tracker (export). Returns the ``external_ref``.

        Idempotent: a ``task`` that already carries an ``external_ref`` updates the
        existing issue rather than creating a new one."""

    @abstractmethod
    def map_fields(self, remote: dict) -> dict:
        """Tracker fields → md-owned frontmatter (the §⑨ field table)."""

    @abstractmethod
    def map_status(self, remote_status: str) -> str:
        """Tracker status → task ``status`` (the §⑨ status table)."""


class TrackerError(RuntimeError):
    """A tracker request failed. The message carries only provider / method / path /
    status — never the Authorization header, token, or response body (secret hygiene)."""


# --------------------------------------------------------------------------- #
# Status mapping (§⑨ status table) — shared across adapters
# --------------------------------------------------------------------------- #

# To Do / Open / In Progress / Backlog / Started → open; Done / Closed / Resolved
# and the two "cancelled" spellings → done. Unknown names read as "open" (the
# safe, not-yet-started reading). `status` is 2-valued in the md; reopening only
# happens from the md ("재오픈은 md 에서만"), which upsert enforces separately.
_DONE_STATUSES = frozenset({
    "done", "closed", "complete", "completed", "resolved",
    "won't do", "wont do", "cancelled", "canceled",
})
_CANCELLED_STATUSES = frozenset({"won't do", "wont do", "cancelled", "canceled"})


def map_status_name(remote_status: str) -> str:
    """Normalise a tracker status name to ``open`` | ``done`` (§⑨)."""
    return "done" if (remote_status or "").strip().lower() in _DONE_STATUSES else "open"


def is_cancelled_status(remote_status: str) -> bool:
    """True for the tracker statuses that map to ``done`` **and** carry a
    ``cancelled`` tag (Won't Do / Cancelled) — §⑨ "취소"."""
    return (remote_status or "").strip().lower() in _CANCELLED_STATUSES


# --------------------------------------------------------------------------- #
# ADF (Atlassian Document Format) — stdlib-only, no conversion dependency
# --------------------------------------------------------------------------- #

# Block nodes that end a line of text when walking an ADF document. Best-effort:
# §⑨ records ADF↔md conversion has no vetted library and new deps are banned, so
# pull extracts text nodes and push wraps plain text in a minimal doc.
_ADF_BLOCK_TYPES = frozenset({
    "paragraph", "heading", "blockquote", "codeBlock", "listItem", "panel", "rule",
})


def extract_adf_text(adf: object) -> str:
    """Best-effort plain text from a Jira v3 ADF ``description`` document.

    Walks the node tree, concatenating ``text`` nodes; a ``hardBreak`` and the end
    of each block node (paragraph/heading/listItem/…) become a newline. Collapses
    3+ blank lines to one and strips the ends. Anything that is not a dict (``None``,
    a bare string the API might return) yields ``""``.
    """
    if not isinstance(adf, dict):
        return str(adf) if isinstance(adf, str) else ""
    parts: list[str] = []

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        ntype = node.get("type")
        if ntype == "text":
            parts.append(str(node.get("text", "")))
        elif ntype == "hardBreak":
            parts.append("\n")
        for child in node.get("content") or []:
            walk(child)
        if ntype in _ADF_BLOCK_TYPES:
            parts.append("\n")

    walk(adf)
    text = re.sub(r"\n{3,}", "\n\n", "".join(parts))
    return text.strip()


def adf_doc(text: str) -> dict:
    """Wrap plain ``text`` in a minimal ADF document (``doc → paragraph → text``).

    The stdlib-only path for a ``push`` comment body: Jira v3 requires ADF for
    comments, and this is the smallest valid document that carries one line.
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


# --------------------------------------------------------------------------- #
# Non-secret config resolvers (replicate the paths.repo_slug ladder — NOT in paths.py)
# --------------------------------------------------------------------------- #
#
# The task `touches` does not list paths.py, so these live here and reuse the
# public/helper surface already in paths (`vault_root_or_cwd` as the search anchor,
# `_read_config_key` as the TOML reader) — the same env → nearest .macp/config.toml
# key → neutral default shape as paths.repo_slug / paths.state_backend, without
# editing paths.py. Credentials never resolve through these — only `.secrets/` / env.


def _config_resolver(env_var: str, config_key: str, start: Path | None) -> str | None:
    env = os.environ.get(env_var)
    if env:
        return env
    base = (Path(start) if start is not None else paths.vault_root_or_cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = paths._read_config_key(cfg, config_key)
            if key:
                return key
            break
    return None


def jira_base_url(start: Path | None = None) -> str | None:
    """Resolve the Jira Cloud base URL (``https://<site>.atlassian.net``), else None.

    ``MACP_JIRA_BASE_URL`` env → nearest ``.macp/config.toml`` ``jira_base_url`` key →
    None. None means "not configured" (opt-in no-op). ``start`` is the test seam.
    """
    return _config_resolver("MACP_JIRA_BASE_URL", "jira_base_url", start)


def jira_project(start: Path | None = None) -> str | None:
    """Resolve the Jira project key (e.g. ``PROJ``), else None.

    ``MACP_JIRA_PROJECT`` env → nearest ``.macp/config.toml`` ``jira_project`` key →
    None. ``start`` is the test seam.
    """
    return _config_resolver("MACP_JIRA_PROJECT", "jira_project", start)


def load_jira_credentials(vault_root: Path | None = None) -> tuple[str, str] | None:
    """Return ``(email, api_token)`` for Jira Cloud HTTP Basic auth, else None.

    Reads ``MACP_JIRA_EMAIL`` / ``MACP_JIRA_API_TOKEN`` env first, falling back to
    ``<vault>/.secrets/jira.json`` (``{"email": ..., "api_token": ...}``) for any
    piece the env does not supply. Returns None when either piece is still missing —
    the opt-in no-op case. The token is **returned, never logged**; callers keep it
    out of messages/stdout/md.

    ``vault_root`` overrides the ``.secrets`` anchor (the test seam); runtime callers
    pass the resolved vault root.
    """
    email = os.environ.get("MACP_JIRA_EMAIL", "").strip()
    token = os.environ.get("MACP_JIRA_API_TOKEN", "").strip()
    if not (email and token):
        data = _read_secrets_json("jira.json", vault_root)
        email = email or str(data.get("email", "")).strip()
        token = token or str(data.get("api_token", "")).strip()
    if email and token:
        return email, token
    return None


def github_issues_repo(start: Path | None = None) -> str | None:
    """Resolve the GitHub Issues target repo (``owner/repo``), else None.

    ``MACP_GITHUB_ISSUES_REPO`` env → nearest ``.macp/config.toml``
    ``github_issues_repo`` key → None. None means "not configured" (opt-in no-op) —
    deliberately **not** defaulted from ``paths.repo_slug`` (that is the vault repo,
    and defaulting to it would switch sync on unasked). ``start`` is the test seam.
    """
    return _config_resolver("MACP_GITHUB_ISSUES_REPO", "github_issues_repo", start)


def github_api_url(start: Path | None = None) -> str | None:
    """Resolve the GitHub REST API base URL override, else None.

    ``MACP_GITHUB_API_URL`` env → nearest ``.macp/config.toml`` ``github_api_url`` key
    → None (the adapter then uses ``https://api.github.com``; GitHub Enterprise Server
    is ``https://<host>/api/v3``). ``start`` is the test seam.
    """
    return _config_resolver("MACP_GITHUB_API_URL", "github_api_url", start)


def load_github_token(vault_root: Path | None = None) -> str | None:
    """Return the GitHub Issues token, else None.

    Reads ``MACP_GITHUB_TOKEN`` env first, falling back to
    ``<vault>/.secrets/github.json`` (``{"token": ...}``). Returns None when neither
    supplies one — the opt-in no-op case. The generic ``GITHUB_TOKEN`` env and the
    ``gh`` CLI token are **deliberately not read**: picking up an ambient token would
    switch sync on without anyone configuring it. The token is **returned, never
    logged**; callers keep it out of messages/stdout/md.

    ``vault_root`` overrides the ``.secrets`` anchor (the test seam).
    """
    token = os.environ.get("MACP_GITHUB_TOKEN", "").strip()
    if not token:
        token = str(_read_secrets_json("github.json", vault_root).get("token", "")).strip()
    return token or None


def linear_team(start: Path | None = None) -> str | None:
    """Resolve the Linear team key (e.g. ``ENG``), else None.

    ``MACP_LINEAR_TEAM`` env → nearest ``.macp/config.toml`` ``linear_team`` key →
    None. None means "not configured" (opt-in no-op) — pull is always scoped to one
    team so an unscoped workspace is never imported. ``start`` is the test seam.
    """
    return _config_resolver("MACP_LINEAR_TEAM", "linear_team", start)


def linear_api_url(start: Path | None = None) -> str | None:
    """Resolve the Linear GraphQL endpoint override, else None.

    ``MACP_LINEAR_API_URL`` env → nearest ``.macp/config.toml`` ``linear_api_url`` key
    → None (the adapter then uses ``https://api.linear.app/graphql``). ``start`` is
    the test seam.
    """
    return _config_resolver("MACP_LINEAR_API_URL", "linear_api_url", start)


def load_linear_api_key(vault_root: Path | None = None) -> str | None:
    """Return the Linear personal API key, else None.

    Reads ``MACP_LINEAR_API_KEY`` env first, falling back to
    ``<vault>/.secrets/linear.json`` (``{"api_key": ...}``). Returns None when neither
    supplies one — the opt-in no-op case. The generic ``LINEAR_API_KEY`` env is
    **deliberately not read**: picking up an ambient key would switch sync on without
    anyone configuring it. The key is **returned, never logged**; callers keep it out
    of messages/stdout/md.

    ``vault_root`` overrides the ``.secrets`` anchor (the test seam).
    """
    key = os.environ.get("MACP_LINEAR_API_KEY", "").strip()
    if not key:
        key = str(_read_secrets_json("linear.json", vault_root).get("api_key", "")).strip()
    return key or None


def _read_secrets_json(filename: str, vault_root: Path | None) -> dict:
    """Read ``<vault>/.secrets/<filename>`` as a JSON object, else ``{}``.

    A missing vault root, unreadable file, invalid JSON, or a non-object document
    all read as ``{}`` (→ "not configured"), never an exception.
    """
    try:
        root = Path(vault_root) if vault_root is not None else paths.vault_root_or_cwd()
    except paths.VaultRootError:
        return {}
    try:
        data = json.loads((root / ".secrets" / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# Frontmatter helpers (mirror cli.parse_frontmatter / cli.update_frontmatter_value)
# --------------------------------------------------------------------------- #


def parse_frontmatter(content: str) -> tuple[dict[str, str | list[str]], str]:
    """Parse YAML-ish frontmatter into (dict, body).

    Line-for-line mirror of ``cli.parse_frontmatter`` (kept off cli's import graph):
    scalar ``key: value`` pairs plus simple ``- item`` lists. Quotes are stripped
    from scalars.
    """
    if not content.startswith("---\n"):
        return {}, content

    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, content

    fm_raw, body = parts
    fm_lines = fm_raw.splitlines()[1:]
    result: dict[str, str | list[str]] = {}
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
                    items.append(sub.strip()[2:].strip())
                    idx += 1
                    continue
                if sub.startswith("  - "):
                    items.append(sub.strip()[2:].strip())
                    idx += 1
                    continue
                break
            result[key] = items
            continue
        value = value.strip().strip('"').strip("'")
        result[key] = value
        idx += 1

    return result, body


def set_scalar(content: str, key: str, value: str) -> str:
    """Set/replace a scalar frontmatter ``key`` (mirror ``cli.update_frontmatter_value``).

    Replaces an existing ``key:`` line (dropping any list rows that followed it) or
    appends the key when absent. No-op when ``content`` has no frontmatter.
    """
    if not content.startswith("---\n"):
        return content

    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return content

    fm_raw, body = parts
    fm_lines = fm_raw.splitlines()[1:]
    out_lines: list[str] = []
    idx = 0
    found = False
    while idx < len(fm_lines):
        line = fm_lines[idx]
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            out_lines.append(f"{key}: {value}")
            found = True
            idx += 1
            while idx < len(fm_lines):
                nxt = fm_lines[idx]
                if nxt.startswith("  - ") or nxt.strip().startswith("- "):
                    idx += 1
                    continue
                break
            continue
        out_lines.append(line)
        idx += 1

    if not found:
        out_lines.append(f"{key}: {value}")

    return "---\n" + "\n".join(out_lines) + "\n---\n" + body


def append_progress_log(content: str, note: str, *, today: str | None = None) -> str:
    """Append ``- <today>: <note>`` under the ``## Progress Log`` section.

    Inserts after the section's existing bullet block (before the next heading), or
    creates the section at the end of the file when it is missing. Trailing-newline
    shape is preserved.
    """
    stamp = today or dt.date.today().isoformat()
    entry = f"- {stamp}: {note}"
    had_trailing = content.endswith("\n")
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Progress Log":
            insert_at = i + 1
            k = i + 1
            while k < len(lines):
                s = lines[k]
                if s.startswith("# ") or s.startswith("## "):
                    break
                if s.strip().startswith("- "):
                    insert_at = k + 1
                k += 1
            lines.insert(insert_at, entry)
            out = "\n".join(lines)
            return out + "\n" if had_trailing else out
    tail = "" if content.endswith("\n") else "\n"
    return content + tail + "\n## Progress Log\n\n" + entry + "\n"


# --------------------------------------------------------------------------- #
# Markdown upsert + conflict reconciliation (§⑨ md=SoT)
# --------------------------------------------------------------------------- #


@dataclass
class UpsertResult:
    """What :func:`upsert_task_record` did, for a CLI summary line / test assertions.

    ``action`` ∈ ``{"created", "updated", "unchanged", "conflict"}``; ``conflicts``
    names the md-owned fields that diverged (remote advanced, md kept)."""

    path: Path
    action: str
    conflicts: list[str] = field(default_factory=list)


# md-owned scalar frontmatter fields the tracker reconciles (title is handled
# separately via the body H1). The tracker fills these only when empty in the md
# and never overwrites a human value — a divergence becomes a Progress Log note.
_MD_OWNED_SCALARS = ("due", "priority")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return slug[:60].strip("-")


def _ref_slug(external_ref: str) -> str:
    """A deterministic slug fragment from an external ref (``jira:PROJ-123`` →
    ``jira-proj-123``) — the collision-proof fallback / disambiguator."""
    return _slugify(external_ref.replace(":", "-")) or "task"


def _find_by_external_ref(items_dir: Path, external_ref: str) -> Path | None:
    if not items_dir.is_dir():
        return None
    for md in sorted(items_dir.glob("*.md")):
        try:
            fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if fm.get("external_ref") == external_ref:
            return md
    return None


def _body_title(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _render_tags(tags: object) -> str:
    seq = tags if isinstance(tags, list) else []
    rows = "\n".join(f"  - {t}" for t in seq)
    return "tags:\n" + rows if rows else "tags:\n  - todo"


def _scaffold(record: TaskRecord, stamp: str) -> str:
    tags = ["todo"]
    for t in record.frontmatter.get("tags") or []:
        if t not in tags:
            tags.append(t)
    status = "done" if record.status == "done" else "open"
    title = record.title or record.external_ref
    fm_lines = [
        "---",
        _render_tags(tags),
        f"status: {status}",
        f'due: {record.frontmatter.get("due", "-") or "-"}',
        f"updated: {stamp}",
        f'outcome: "트래커에서 가져옴 — 내용은 {record.external_ref} 참조"',
        'next_action: "트래커 이슈 본문을 확인하고 macp 태스크로 구체화한다"',
        'if_then: "동기화로 생성된 태스크 — 편집은 md 에서 하고 트래커는 뷰로 둔다"',
    ]
    priority = record.frontmatter.get("priority")
    if priority is not None:
        fm_lines.append(f"priority: {priority}")
    fm_lines.append(f"external_ref: {record.external_ref}")
    if record.remote_updated_at:
        fm_lines.append(f"external_updated_at: {record.remote_updated_at}")
    fm_lines.append("---")
    body = record.body.strip()
    body_block = f"\n{body}\n" if body else "\n"
    return (
        "\n".join(fm_lines)
        + f"\n\n# {title}\n{body_block}\n## Progress Log\n\n"
        + f"- {stamp}: 트래커에서 가져옴 ({record.external_ref})\n"
    )


def upsert_task_record(
    record: TaskRecord, items_dir: Path, *, today: str | None = None
) -> UpsertResult:
    """Idempotently upsert a :class:`TaskRecord` into ``items_dir/<slug>.md`` (§⑨).

    * **New** ``external_ref`` → scaffold a task md (title as H1, mapped body +
      fields, ``external_ref`` + ``external_updated_at``).
    * **Existing** ``external_ref`` (matched by scanning frontmatter, never by slug)
      → fill only empty md-owned fields, apply the monotonic "done 승격" status rule,
      refresh ``external_updated_at``; on a genuine divergence (remote advanced since
      the stored ``external_updated_at`` **and** an md-owned field differs) keep the
      md and append a ``## Progress Log`` note.

    The new content is written **only when it differs** from the file on disk, so a
    repeated pull of an unchanged remote is a true no-op (no duplicate, no churn).
    ``items_dir`` is always explicit — never the cli import-time ``ITEMS_DIR`` — so a
    test drives it against ``tmp_path``.
    """
    stamp = today or dt.date.today().isoformat()
    items_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_by_external_ref(items_dir, record.external_ref)

    if existing is None:
        slug = _slugify(record.title) or _ref_slug(record.external_ref)
        dest = items_dir / f"{slug}.md"
        if dest.exists():  # slug collision with a different ref → disambiguate
            dest = items_dir / f"{slug}-{_ref_slug(record.external_ref)}.md"
        content = _scaffold(record, stamp)
        dest.write_text(content, encoding="utf-8")
        return UpsertResult(path=dest, action="created")

    original = existing.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(original)
    content = original
    conflicts: list[str] = []

    prev_remote = fm.get("external_updated_at")
    remote_now = record.remote_updated_at
    remote_advanced = bool(remote_now) and (
        not isinstance(prev_remote, str) or str(remote_now) > prev_remote
    )

    # Title (body H1): the tracker never rewrites a human H1. A divergence while the
    # remote advanced is a conflict note; a missing H1 is left for the human to fill.
    md_title = _body_title(body)
    if record.title and md_title and md_title != record.title and remote_advanced:
        conflicts.append("title")

    # Scalar md-owned fields.
    for key in _MD_OWNED_SCALARS:
        remote_val = record.frontmatter.get(key)
        if remote_val is None or remote_val == "":
            continue
        cur = fm.get(key)
        cur_empty = cur is None or cur == "" or cur == "-"
        if cur_empty:
            content = set_scalar(content, key, str(remote_val))
        elif str(cur) != str(remote_val) and remote_advanced:
            conflicts.append(key)

    # Status: monotonic "done 승격" — done if either side is done; reopen only from md.
    md_status = str(fm.get("status") or "open").strip().lower()
    new_status = "done" if md_status == "done" or record.status == "done" else "open"
    if new_status != md_status:
        content = set_scalar(content, "status", new_status)

    # Record the remote version we just reconciled against.
    if remote_now and str(remote_now) != (prev_remote if isinstance(prev_remote, str) else ""):
        content = set_scalar(content, "external_updated_at", str(remote_now))

    for field_name in conflicts:
        remote_val = record.title if field_name == "title" else record.frontmatter.get(field_name)
        content = append_progress_log(
            content,
            f"트래커 충돌: {field_name} 가 양쪽에서 갈림 — md 유지(트래커값: {remote_val}). "
            f"동기화 소스 {record.external_ref}",
            today=stamp,
        )

    if content == original:
        return UpsertResult(path=existing, action="unchanged")
    existing.write_text(content, encoding="utf-8")
    return UpsertResult(
        path=existing, action="conflict" if conflicts else "updated", conflicts=conflicts
    )
