"""Read-only snapshot of a task's full frontmatter contract.

Why this exists
---------------
A task's contract is spread across three layers, and only the first survives in
``Todo/todo.db``:

* **Layer A** — the 16 frontmatter fields ``cli.parse_task_file`` turns into a
  ``ParsedTask`` (``cli.py:986-1009``) and the DB stores.
* **Layer B** — ``repo`` / ``working_dir`` / ``long_job`` / ``bypass_guard`` / ``model_tier`` /
  ``allowed_tools``, which the orchestrator re-reads straight off the markdown
  (``orchestrator/repos.py:49-80``, called from ``orchestrator/tasks.py:67-92``).
  **No DB column exists for any of them.**
* **Layer C** — fields only a running stage ever reads: ``WorkerTask``'s
  properties (``worker.py:507-624``), ``task_deps``' dependency trio
  (``task_deps.py:83-86``) and ``acceptance_criteria``
  (``task_validate.py:183-187``).

Twelve of those fields have no DB column, so anything that reconstructs a task
from the database alone silently loses them. This module serialises all three
layers together so the loss is visible.

Deliberate design choices
-------------------------
**No fourth parser.** Three frontmatter parsers already exist with different
semantics (``cli.parse_frontmatter`` hand-rolled, ``worker.parse_frontmatter``
hand-rolled and intentionally duplicated, ``repos._frontmatter`` via
``yaml.safe_load``). Adding a fifth reading would make the snapshot disagree
with what consumers actually see, so every value below comes from calling the
real consumer. ``raw_frontmatter`` keeps the unabridged YAML alongside them.

**Read-only.** No function in this module writes anything; only ``main()``
writes, and only when given an explicit ``--out``. The default ``vault_root`` is
the live vault, so a bare ``export_all()`` reads real task files — and still
writes nothing.

**Do not commit the output.** ``Todo/items/`` is git-tracked, so its task
bodies are already in git; a snapshot is a derived, point-in-time duplicate of
that same content and committing it would just add stale copies to track. The
default output convention is therefore ``Todo/Snapshots/``, which
``.gitignore:74`` ignores. (Note that ``Worker/**`` is *not* wholesale ignored
— only specific subpaths are — so ``Worker/exports/*.json`` would be
committable. That is the reason to avoid it, not a reason to use it.)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any

from . import cli as _cli
from . import paths
from . import task_deps as _task_deps
from . import worker as _worker
from .orchestrator import repos as _repos

# Via the shared resolver, exactly like cli.ROOT — the default really is the live
# vault, now redirectable by MACP_VAULT_ROOT. The start= anchor is
# paths.vault_search_start(): the engine root in a checkout, so with the env
# unset it stays byte-identical to the old Path(__file__).resolve().parents[1]
# the exporter used before; None in an installed wheel, where the cwd-upward
# search replaces a site-packages anchor.
DEFAULT_VAULT_ROOT = paths.vault_root(start=paths.vault_search_start())
ITEMS_SUBDIR = "Todo/items"


def _rel_path(md_path: Path, vault_root: Path) -> str:
    """Vault-relative path when the file lives under the vault, else absolute.

    A fixture in ``tmp_path`` is not under the vault; refusing to guess keeps the
    snapshot honest instead of emitting a path that resolves to nothing.
    """
    try:
        return str(md_path.relative_to(vault_root))
    except ValueError:
        return str(md_path)


def _layer_a(md_path: Path, vault_root: Path) -> dict[str, Any]:
    """Layer A via ``cli.parse_task_file`` — the fields that reach the DB.

    ``cli.parse_task_file`` hard-codes ``path.relative_to(ROOT)`` at
    ``cli.py:988`` against the live vault, so it raises ``ValueError`` for any
    file outside it (every test fixture). The fallback re-assembles the same
    dataclass from *the same helpers that function uses* — never a new parser —
    differing only in which root ``path`` is made relative to.
    ``test_layer_a_fallback_matches_primary`` pins the two branches together.
    """
    try:
        parsed = _cli.parse_task_file(md_path)
    except ValueError:
        parsed = _parse_task_file_rooted(md_path, vault_root)
    data = dataclasses.asdict(parsed)
    # asdict keeps (text, done) as tuples; normalise so a JSON round-trip
    # compares equal to the in-memory value.
    data["subtasks"] = [[text, done] for text, done in data.get("subtasks", [])]
    return data


def _parse_task_file_rooted(md_path: Path, vault_root: Path) -> _cli.ParsedTask:
    """``cli.parse_task_file`` (cli.py:949-1009) with an injectable root.

    Mirrors that function line for line and calls into its own helpers
    (``parse_frontmatter``, ``CHECKBOX_RE``, ``_parse_priority``,
    ``_parse_parent_slug``), so a semantic change there shows up here as a test
    failure rather than as a silently divergent snapshot.
    """
    content = md_path.read_text(encoding="utf-8")
    frontmatter, body = _cli.parse_frontmatter(content)

    title = md_path.stem
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    subtasks: list[tuple[str, int]] = []
    for line in body.splitlines():
        match = _cli.CHECKBOX_RE.match(line)
        if not match:
            continue
        subtasks.append((match.group(2).strip(), 1 if match.group(1).lower() == "x" else 0))

    def fm_value(key: str) -> Any:
        value = frontmatter.get(key)
        if isinstance(value, list):
            return ", ".join(value)
        return value

    tags_raw = frontmatter.get("tags")
    if isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if t]
    elif isinstance(tags_raw, str) and tags_raw.strip():
        tags = [tags_raw.strip()]
    else:
        tags = []

    return _cli.ParsedTask(
        slug=md_path.stem,
        path=_rel_path(md_path, vault_root),
        title=title,
        status=fm_value("status") or "open",
        due=fm_value("due"),
        not_before=fm_value("not_before"),
        updated=fm_value("updated"),
        outcome=fm_value("outcome"),
        next_action=fm_value("next_action"),
        if_then=fm_value("if_then"),
        estimate_p50=fm_value("estimate_p50"),
        estimate_p90=fm_value("estimate_p90"),
        review_cycle=fm_value("review_cycle"),
        priority=_cli._parse_priority(fm_value("priority")),
        total_subtasks=len(subtasks),
        done_subtasks=sum(done for _, done in subtasks),
        subtasks=subtasks,
        parent_slug=_cli._parse_parent_slug(fm_value("parent")),
        is_parent=1 if str(fm_value("is_parent") or "").strip().lower() == "true" else 0,
        tags=tags,
        worker_mode=(fm_value("worker_mode") or "").strip().lower() or None,
        auto_merge=0 if str(fm_value("auto_merge")).strip().lower() in ("false", "0", "no") else 1,
    )


def _layer_b(md_path: Path, fm: dict | None = None) -> dict[str, Any]:
    """The six orchestrator-only fields, read through their real accessors.

    Each of these has no DB column, so this layer is the whole point of the
    snapshot. ``repos._clean`` (``repos.py:43-46``) coerces every non-``str`` to
    ``None``, which is why ``_lossy_keys`` cross-checks against the raw YAML.

    ``fm`` is the ``repos._frontmatter`` dict ``export_task`` already parsed; it
    goes to the ``task_*_from_fm`` cores the ``read_task_*`` accessors delegate
    to. Letting each accessor re-parse the file cost five extra ``yaml.safe_load``
    calls per task — 96% of a live-vault ``export_all()``.
    """
    if fm is None:
        fm = _repos._frontmatter(md_path)
    repo, working_dir = _repos.task_repo_from_fm(fm)
    return {
        "repo": repo,
        "working_dir": working_dir,
        "long_job": _repos.task_long_job_from_fm(fm),
        "bypass_guard": _repos.task_bypass_guard_from_fm(fm),
        "allowed_tools": _repos.task_allowed_tools_from_fm(fm),
        "model_tier": _repos.task_model_tier_from_fm(fm),
    }


def _layer_c(md_path: Path, vault_root: Path, deps_index: dict[str, dict] | None = None) -> dict[str, Any]:
    """Stage-runtime fields: ``WorkerTask`` properties + deps trio + criteria.

    ``deps_index`` is ``task_deps.scan_items`` output for the containing
    directory. ``export_all`` computes it once and threads it through; scanning
    per task would re-read every sibling for every task.

    Reading ``allowed_tools_implement`` invokes ``WorkerTask._resolve_allowed_tools``,
    which prints a warning on list-form input (``worker.py:526``). That is the
    real consumer's behaviour, so it is kept — it writes to stdout, never to the
    vault.
    """
    fm, _body = _worker.parse_frontmatter(md_path.read_text(encoding="utf-8"))
    task = _worker.WorkerTask(
        slug=md_path.stem,
        path=md_path,
        rel_path=_rel_path(md_path, vault_root),
        frontmatter=fm,
    )
    if deps_index is None:
        deps_index = _task_deps.scan_items(md_path.parent)
    deps = deps_index.get(md_path.stem, {})
    return {
        "max_turns": task.max_turns,
        "delegate_opt_out": task.delegate_opt_out,
        "worker_branch": task.worker_branch,
        "allowed_tools_implement": task.allowed_tools_implement,
        "depends_on": deps.get("depends_on", []),
        "on_unblock": deps.get("on_unblock"),
        "on_unblock_explicit": deps.get("on_unblock_explicit", False),
        "auto_activate": deps.get("auto_activate"),
        "acceptance_criteria": fm.get("acceptance_criteria"),
    }


# Raw frontmatter key -> (layer, field) whose effective value is None when the
# consumer dropped the value entirely. `repos._clean` (repos.py:43-46) does this
# to every non-str, which is how a YAML-list `allowed_tools` vanishes; a
# non-integer `max_turns` goes the same way at worker.py:513-516.
_DROPPABLE = {
    "repo": ("b", "repo"),
    "working_dir": ("b", "working_dir"),
    "long_job": ("b", "long_job"),
    "allowed_tools": ("b", "allowed_tools"),
    "model_tier": ("b", "model_tier"),
    "max_turns": ("c", "max_turns"),
}

# repos.read_task_bypass_guard (repos.py:69-72) maps anything outside this set to
# False rather than to None, so it needs its own check.
_BYPASS_GUARD_RECOGNISED = ("true", "1", "yes", "false", "0", "no")


def _is_informative(value: Any) -> bool:
    """Whether a raw value carries information a consumer could lose.

    ``-`` is the vault's written-out "deliberately empty" placeholder, so
    dropping it loses nothing and must not be reported as lossy.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in ("", "-")
    return True


def _lossy_keys(raw: dict, layer_b: dict, layer_c: dict) -> list[str]:
    """Keys the raw frontmatter sets but the consumer silently discards.

    Without this the snapshot would *conceal* the very bug it exists to expose:
    recording only effective values makes a YAML-list ``allowed_tools`` look
    identical to one that was never written.
    """
    lossy = []
    for key, (layer, field) in _DROPPABLE.items():
        if not _is_informative(raw.get(key)):
            continue
        if (layer_b if layer == "b" else layer_c).get(field) is None:
            lossy.append(key)

    guard = raw.get("bypass_guard")
    if isinstance(guard, str) and guard.strip().lower() not in _BYPASS_GUARD_RECOGNISED:
        lossy.append("bypass_guard")

    return sorted(lossy)


def export_task(
    md_path: str | Path,
    vault_root: str | Path = DEFAULT_VAULT_ROOT,
    deps_index: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """One task's full contract across all three layers. Writes nothing."""
    md_path = Path(md_path)
    vault_root = Path(vault_root)
    raw = _repos._frontmatter(md_path)
    layer_b = _layer_b(md_path, raw)
    layer_c = _layer_c(md_path, vault_root, deps_index)
    return {
        "slug": md_path.stem,
        "path": _rel_path(md_path, vault_root),
        "raw_frontmatter": raw,
        "layer_a": _layer_a(md_path, vault_root),
        "layer_b": layer_b,
        "layer_c": layer_c,
        "lossy": _lossy_keys(raw, layer_b, layer_c),
    }


def export_all(vault_root: str | Path = DEFAULT_VAULT_ROOT) -> list[dict[str, Any]]:
    """Every task under ``<vault_root>/Todo/items``, sorted by filename.

    Returns ``[]`` when the directory is absent — the normal state for a
    worker checkout of an external project repo, which has no ``Todo/`` at
    all. Writes nothing.
    """
    vault_root = Path(vault_root)
    items_dir = vault_root / ITEMS_SUBDIR
    if not items_dir.is_dir():
        return []
    # Computed once and threaded through: scan_items reads the whole directory.
    deps_index = _task_deps.scan_items(items_dir)
    return [export_task(p, vault_root, deps_index) for p in sorted(items_dir.glob("*.md"))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m todo_system.task_export",
        description="Serialise every task's frontmatter contract (all three layers).",
        epilog=(
            "Do NOT commit the output: it contains task bodies from Todo/items/, "
            "which .gitignore:78 deliberately keeps untracked. Write it under "
            "Todo/Snapshots/ (already ignored at .gitignore:74)."
        ),
    )
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--out", default=None, help="write JSON here (default: stdout)")
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)

    snapshots = export_all(args.vault_root)
    # default=str: yaml.safe_load turns an unquoted `due: 2026-08-24` into a
    # datetime.date, which json cannot serialise on its own.
    payload = json.dumps(snapshots, ensure_ascii=False, indent=args.indent, default=str)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {len(snapshots)} task contract(s) to {out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
