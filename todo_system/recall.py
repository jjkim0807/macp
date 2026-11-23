"""Free-text search of the vault's memory index, for an agent that is stuck mid-run.

Until this module a worker run got its memory once: the engine queried the index
before the first stage and left the answer in ``<run_dir>/related.md``
(``orchestrator/related_context.py``). A problem the run only met later — a build
daemon that died, a migration trap — could not be looked up, even when a past
run had already written the answer down. ``macp-recall "<question>"`` is that
lookup: the stage asks in its own words and gets back pointers to open.

What it does, and nothing more:

* runs ``graph_memory.query_related`` with the question as the query text. No
  task slug is passed, so the answer is the semantic layer alone — the nearest
  embedded documents under the vault's configured embedder (``MACP_EMBEDDER``,
  else the ``embedder`` key of the vault's ``.macp/config.toml``, else the
  default). The config search starts at the VAULT, not the cwd: a stage's shell
  may be standing in another repository's checkout, and the index belongs to
  the vault;
* turns each hit into a card through ``related_context.doc_card`` — title, path
  and the document's own one-line summary field. That call also re-applies the
  privacy filter to the file as it is now, so a document outside the boundary
  never comes out, whatever the index still holds. No body text is printed;
* prints at most ``MAX_OUTPUT_BYTES`` (4 KB) — whole results are dropped from
  the end, and a last cut on a character boundary backs that up;
* appends one JSON line per call (the question and the result paths, never a
  title or a gist) to the file named by ``MACP_RECALL_LOG``. The runner sets it
  to ``<run_dir>/recall.log`` for the stages that hold the rule; unset — an
  interactive shell — nothing is logged.

The index is opened read-only and nothing in the vault is written.

:func:`usage` reads a run's log back: how often recall was called, and how many
of the documents it returned the run went on to name in its own artifacts. The
report stage is handed both numbers (``stages.build_stage_argv``) and writes
them into ``report.md``. They exist to decide whether this tool is kept.

Environment (read at call time):

  MACP_RECALL_LOG    file to append the call record to (set per stage by the runner)
  MACP_RECALL_TASK   slug of the task the stage is running; that task's own
                     document is left out of the answer

Exit status: 0 when the question was answered (no results included), 1 when the
index could not be read (the reason is one line on stderr), 2 on a usage error.

    macp-recall "gradle daemon disappeared during the beta build"
    macp-recall "room migration test seam" --k 8

Standard library only at import time. ``graph_memory`` and ``related_context``
resolve the vault when they are imported, so both are imported inside the
functions that need them and ``main`` can report a missing vault in one line.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Sequence

LOG_ENV = "MACP_RECALL_LOG"
TASK_ENV = "MACP_RECALL_TASK"
LOG_FILENAME = "recall.log"

DEFAULT_K = 5
MAX_K = 20
MAX_OUTPUT_BYTES = 4096
# Ask the index for more than `k`: hits dropped by the boundary filter, a
# missing file or the task's own document must not shrink the answer.
OVERFETCH = 3

TITLE_MAX_CHARS = 120
QUERY_ECHO_CHARS = 80     # the question, echoed in the first output line
QUERY_LOG_CHARS = 500     # the question, as stored in the log
NOTE_MAX_CHARS = 200

# The run artifacts an agent writes. A document recall returned counts as cited
# when one of these names it. Not recall.log itself, nor related.md (the engine
# wrote that before the run), nor report.md (written after the count is taken).
CITATION_SOURCES = ("implementation_plan.md", "run.log", "verify.md", "quick.md",
                    "delegate.md")


class RecallUnavailable(RuntimeError):
    """The index could not be queried. The message is the one-line reason."""


class Answer(NamedTuple):
    cards: list          # related_context.DocCard, best first
    embedder: str        # the backend the question was embedded with
    note: str            # one diagnostic line from the query layer, "" when clean


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:max(limit - 1, 0)].rstrip() + "…"


def _clip_bytes(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` UTF-8 bytes, never mid-character."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore")


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def clamp_k(k: Any) -> int:
    try:
        return max(1, min(int(k), MAX_K))
    except (TypeError, ValueError):
        return DEFAULT_K


# -- the query -----------------------------------------------------------------

def search(query: str, *, k: int = DEFAULT_K, root: Path | None = None,
           exclude: Iterable[str] = ()) -> Answer:
    """Up to ``k`` cards for a free-text question, best first.

    ``root`` is the vault to ask (its index, its documents, its embedder
    config); ``None`` means the resolved one. ``exclude`` are slugs to leave out.
    Raises :class:`RecallUnavailable` when there is no index to read or the
    query itself failed — an empty answer always means "asked, nothing found".
    """
    from . import graph_memory as gm
    from .orchestrator import related_context as rc

    base = Path(root) if root is not None else rc.vault_root()
    con = rc.open_index(root)
    if con is None:
        raise RecallUnavailable(f"no memory index at {rc.db_path(root)}")
    want = clamp_k(k)
    skip = {s for s in exclude if s}
    # The query layer reports a dimension mismatch with print(); keep it out of
    # the answer's byte budget and hand its first line back as the note.
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            name = gm.embedder_name(start=base)
            embed, dim, name = gm.resolve_embedder(name=name)
            items = gm.query_related(con, {"title": query},
                                     k=max(want * OVERFETCH, rc.MIN_KNN),
                                     embedder=embed, dim=dim)
    except gm.EmbedderUnavailable as exc:
        raise RecallUnavailable(f"embedder unavailable ({exc})") from exc
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise RecallUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        con.close()

    cards = []
    for item in items:
        if item.slug in skip:
            continue
        card = rc.doc_card(base, item.slug, item.source_file)
        if card is None:
            continue
        cards.append(card)
        if len(cards) >= want:
            break
    lines = [ln.strip() for ln in said.getvalue().splitlines() if ln.strip()]
    return Answer(cards, name, _clip(lines[0], NOTE_MAX_CHARS) if lines else "")


# -- rendering -----------------------------------------------------------------

class Page(NamedTuple):
    text: str            # what is printed, at most the byte limit
    paths: list          # the paths of the cards that made it into `text`


def _head(count: int, echo: str, embedder: str) -> str:
    return f'recall: {count} result(s) for "{echo}" (embedder: {embedder})'


def _dropped(count: int, limit: int) -> str:
    return f"({count} more not shown: the answer is capped at {limit} bytes)"


def render(query: str, answer: Answer, *, root: Path,
           limit: int = MAX_OUTPUT_BYTES) -> Page:
    """The page printed for one answer: a header, one three-line block per card
    (title / path / gist), and a footer naming the vault root the paths hang
    off. Never more than ``limit`` bytes: the fixed lines are sized first and
    blocks are added while they fit, so a block is shown whole or not at all,
    and the drop is said in one line. A last cut on a character boundary is
    kept behind that, for a limit too small to hold even the fixed lines.
    """
    cards = list(answer.cards)
    echo = _clip(query, QUERY_ECHO_CHARS)
    foot = [f"note: {answer.note}"] if answer.note else []
    if not cards:
        lines = [f'recall: no results for "{echo}" (embedder: {answer.embedder})']
        if not answer.note:
            lines.append("(nothing in the index matched, or this vault has not "
                         "been embedded yet)")
        return Page(_clip_bytes("\n".join([*lines, *foot]) + "\n", limit), [])

    foot.append(f"paths are relative to the vault root {root}")
    foot.append("Read the ones that look useful; when a result helps, name its "
                "path in your run log.")
    # Every line ends in one newline. Size the lines that are always there (the
    # header and drop notice at their widest), and the blocks get the rest.
    fixed = [_head(len(cards), echo, answer.embedder), _dropped(len(cards), limit), *foot]
    budget = limit - sum(_size(line) + 1 for line in fixed)
    shown: list[str] = []
    for number, card in enumerate(cards, 1):
        block = (f"{number}. {_clip(card.title, TITLE_MAX_CHARS)}\n"
                 f"   path: {card.path}\n"
                 f"   gist: {card.gist or '-'}")
        cost = _size(block) + 1
        if cost > budget:
            break
        shown.append(block)
        budget -= cost
    lines = [_head(len(shown), echo, answer.embedder), *shown]
    if len(shown) < len(cards):
        lines.append(_dropped(len(cards) - len(shown), limit))
    text = _clip_bytes("\n".join([*lines, *foot]) + "\n", limit)
    return Page(text, [card.path for card in cards[:len(shown)]])


# -- the call log --------------------------------------------------------------

def log_path() -> Path | None:
    """Where this call is recorded: ``$MACP_RECALL_LOG``, or ``None`` when unset."""
    raw = os.environ.get(LOG_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def append_log(path: Path, *, query: str, k: int, results: Sequence[str],
               error: str = "") -> bool:
    """Append one call to ``path`` as a JSON line: when, the question, ``k`` and
    the result paths (plus the reason when the call failed). Paths only — a
    title or a gist is never written here. Best-effort: a log that cannot be
    written is one stderr line, never a failed lookup."""
    entry: dict[str, Any] = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "query": _clip(query, QUERY_LOG_CHARS),
        "k": k,
        "results": [str(p) for p in results],
    }
    if error:
        entry["error"] = _clip(error, NOTE_MAX_CHARS)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        print(f"macp-recall: call not logged ({type(exc).__name__})", file=sys.stderr)
        return False


def read_log(path: Path) -> list[dict]:
    """The calls recorded in ``path``, oldest first. A missing file is no calls;
    a line that is not a JSON object is skipped."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    calls = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "query" in entry:
            calls.append(entry)
    return calls


def _names(text: str, path: str) -> bool:
    """Whether ``text`` names the document at ``path``: the path itself, or its
    slug as a whole word (so ``foo`` is not found inside ``foo-bar``)."""
    if path in text:
        return True
    slug = Path(path).stem
    return bool(slug) and re.search(
        r"(?<![\w-])" + re.escape(slug) + r"(?![\w-])", text) is not None


def usage(run_dir: Path, task_id: str | None = None) -> dict[str, int]:
    """What one run did with recall, from its ``recall.log`` and artifacts.

    ``calls``    how many times recall was called (failed calls included);
    ``results``  how many distinct documents those calls returned;
    ``cited``    how many of those the run named in ``CITATION_SOURCES`` — by
                 path or by slug. The task's own document is not counted: its
                 slug is in every artifact whether recall was used or not.

    All zero for a run with no log. Never raises.
    """
    base = Path(run_dir)
    calls = read_log(base / LOG_FILENAME)
    paths: list[str] = []
    for entry in calls:
        found = entry.get("results")
        for p in found if isinstance(found, list) else ():
            if isinstance(p, str) and p and p not in paths:
                paths.append(p)
    cited = 0
    if paths:
        parts = []
        for name in CITATION_SOURCES:
            try:
                parts.append((base / name).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        text = "\n".join(parts)
        cited = sum(1 for p in paths
                    if Path(p).stem != (task_id or "") and _names(text, p))
    return {"calls": len(calls), "results": len(paths), "cited": cited}


# -- CLI -----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macp-recall",
        description="Search the vault's memory (past tasks, run episodes, notes, "
                    "decisions) with a free-text question. Prints title, path "
                    "and a one-line gist per result; Read a path for the rest.")
    parser.add_argument("query", nargs="+",
                        help="the question or the symptom, in any language")
    parser.add_argument("--k", "-k", type=int, default=DEFAULT_K,
                        help=f"how many results (default {DEFAULT_K}, at most {MAX_K})")
    parser.add_argument("--vault-root", type=Path, default=None,
                        help="ask this vault instead of the resolved one")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    query = " ".join(args.query).strip()
    if not query:
        parser.error("the question is empty")
    k = clamp_k(args.k)
    named = (Path(args.vault_root).expanduser().resolve()
             if args.vault_root is not None else None)
    if named is not None:
        # graph_memory resolves a vault when it is imported; with none configured
        # the named one has to be visible by then, or the import below raises.
        os.environ.setdefault("MACP_VAULT_ROOT", str(named))

    from . import paths
    try:
        from .orchestrator import related_context as rc
    except paths.VaultRootError as exc:
        print(f"macp-recall: {exc}", file=sys.stderr)
        return 1

    log = log_path()
    try:
        answer = search(query, k=k, root=named,
                        exclude=(os.environ.get(TASK_ENV, "").strip(),))
    except RecallUnavailable as exc:
        if log is not None:
            append_log(log, query=query, k=k, results=(), error=str(exc))
        print(f"macp-recall: {exc}", file=sys.stderr)
        return 1
    page = render(query, answer, root=named if named is not None else rc.vault_root())
    sys.stdout.write(page.text)
    if log is not None:
        # What the agent was shown, not what the index returned: a result cut
        # for the byte cap was never seen and cannot be cited.
        append_log(log, query=query, k=k, results=page.paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
