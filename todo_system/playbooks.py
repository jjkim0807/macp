"""Repo playbooks — the short list of pitfalls one repo's runs keep hitting.

Why: every task of a repo starts cold. A retry of the SAME task gets its own
failure back (``stages.previous_failure_context``), and recall (``macp-recall``)
answers the questions a stage thinks to ask. Neither helps the next task of the
repo, which walks into the 60-minute command cap, the test double a signature
change breaks, the missing import only Verify can see — again. A playbook is the
fixed answer to "what does this repo usually trip on": a few lines, always in
the prompt, never searched for.

Where it lives and who reads it
-------------------------------
``<vault>/Memory/playbooks/<repo>.md``, where ``<repo>`` is the slug-safe repo
name (``usage_value.project_name``: ``…/Acme-App.git`` → ``acme-app``). The body
of the file IS the prompt section: ``stages.build_stage_argv`` appends it,
capped at MAX_BYTES of UTF-8, to the run metadata of the implement and verify
stages of every task whose ``repo`` is that repo (:func:`prompt_section`).
Tasks of other repos, vault tasks and the other stages get nothing.

What feeds it (local files only, read-only)
-------------------------------------------
1. **Verify FAIL → later PASS of the same task** (``Worker/runs/<ts>-<slug>/``):
   the last ``fail``/``skip`` verify before a ``pass``. The cause comes out of
   the failing verify.md by a deterministic rule (:func:`cause_of`).
2. **The repair commits**: the PASS run's own commits as its run.log records
   them (:func:`fix_of`). A PASS with no commit falls back to its Conclusion.
3. **Optional summary**: the report stage already wrote an LLM summary of the
   PASS run — its episode (``Memory/episodes/…-worker-<slug>.md``). The lesson
   bullet sharing the most identifiers with the failure replaces the mechanical
   "cause → fix" text (:func:`episode_summary`); no new model call is made.
   ``summarize=None`` keeps the deterministic text alone.
4. **Interactive memory** (``Memory/claude/vault/*.md``) about the repo: a file
   whose stem, ``name`` or ``description`` names the repo or one of its aliases
   (:func:`repo_aliases`). Its ``description`` is the line.

Rules (each pinned by ``tests/test_playbooks.py``)
--------------------------------------------------
* category — :func:`classify`, the first CATEGORIES rule that matches;
* dedupe — two run lessons of one category sharing at least MERGE_MIN_SHARED
  identifiers (MERGE_MIN_OVERLAP of the smaller set) are one line, ``×N``,
  carrying the newest text, source and date;
* expiry — a run lesson older than EXPIRE_DAYS is dropped before merging, so
  ``×N`` counts recent repeats. Memory lines do not age: the memory file is
  their lifetime;
* order — run lessons by count then date, memory lines by date, the two lists
  interleaved so neither crowds the other out;
* cap — whole lines only, while the section stays within MAX_BYTES;
* privacy — the R5 document boundary of ``related_context`` (the same
  ``read_document`` / ``r5_allows`` as the recall card) on every source: the
  task file of a run, the episode, the memory file. :func:`scrub` then takes
  e-mail addresses, phone numbers, verification codes, API keys, URL queries
  and home directories out of every line.

Refresh
-------
:func:`refresh` rebuilds one repo's file and writes it only when the bytes
change — the file holds no timestamp, so an unchanged playbook is an unchanged
file. The runner calls it at the start of every external-repo run
(``stages.record_playbook``, beside ``record_related``), so each run's stages
read the playbook as of that run's start. Only the HQ host writes
(``hq_host.check``): ``Worker/runs`` is host-local, and two hosts rebuilding one
tracked file from different run sets would fight over it in git. Every other
host reads the file git delivered. A repo with nothing left to say loses it.

Measurement
-----------
:func:`recurrence` counts, per week, the verify ``fail``/``skip`` results and
how many repeat a cause category that ANOTHER task of the same repo already
failed on within EXPIRE_DAYS — the number a playbook exists to bring down (a
task's own retries already get their failure back). The weekly usage section of
the brief carries it as one line (:func:`brief_line`, appended by
``usage_value.brief_section``); ``recurrence`` on the CLI prints the table.

CLI (only ``refresh`` writes):

    python -m todo_system.playbooks show --repo URL [--vault-root DIR] [--today YYYY-MM-DD]
    python -m todo_system.playbooks refresh (--repo URL | --all) [--vault-root DIR]
    python -m todo_system.playbooks recurrence [--weeks N] [--vault-root DIR] [--today YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Sequence

from . import graph_memory as gm
from .orchestrator import hq_host
from .orchestrator import related_context as rc
from .orchestrator.atomic_io import atomic_write_text
from .usage_value import project_name

PLAYBOOK_DIR = ("Memory", "playbooks")
MEMORY_DIR = ("Memory", "claude", "vault")
EPISODES_DIR = ("Memory", "episodes")
ITEMS_DIR = ("Todo", "items")
RUNS_DIR = ("Worker", "runs")
MEMORY_INDEX = "MEMORY.md"

MAX_BYTES = 2048            # the whole prompt section, UTF-8
EXPIRE_DAYS = 60
CAUSE_MAX_BYTES = 130
FIX_MAX_BYTES = 70
FOCUS_LEAD_CHARS = 16       # context kept before the words that decided the category
SUMMARY_MAX_BYTES = 180
MEMORY_MAX_BYTES = 160
SOURCE_MAX_CHARS = 30
MERGE_MIN_SHARED = 2
MERGE_MIN_OVERLAP = 0.5
SUMMARY_MIN_KEYS = 2
# Causes that are a condition of the repo or the pipeline rather than of one
# piece of code: two of them in one category are the same pitfall whatever
# identifiers their texts happen to name (the full suite over the command cap,
# a branch cut from a stale base, a decision only a person can make).
CATEGORY_MERGED = frozenset({"budget", "env", "rebase", "human", "task-md"})
TASK_HEAD = 16384           # characters of a task file read for its frontmatter + H1
VERIFY_HEAD = 4096          # enough for a verify.md's frontmatter verdict
ALIAS_MIN_LEN = 3
SLUG_ALIAS_MIN_TASKS, SLUG_ALIAS_MIN_SHARE = 3, 0.8
TITLE_ALIAS_MIN_TASKS, TITLE_ALIAS_MIN_SHARE = 20, 0.9
RECURRENCE_WEEKS = 4
NOT_PASS = ("fail", "skip")

HEADING = "## Repo playbook — {key}"
INTRO = ("Pitfalls this repo's earlier runs hit and what fixed them, most repeated first. "
         "Each line ends with its source (a `Worker/runs/` run or a `Memory/claude/vault/` "
         "memory) and date. A checklist: the task and this prompt win any disagreement.")

# Seam read at CALL time (never frozen into a default argument), so a test can
# point it at a tmp vault: None → graph_memory.ROOT, read as an attribute.
VAULT_ROOT: Path | None = None

Summarizer = Callable[[Path, str, str, str], "str | None"]


class Lesson(NamedTuple):
    """One playbook line before rendering."""

    kind: str            # "run" | "memory"
    category: str
    text: str            # scrubbed and clipped
    source: str          # run id, or the memory file's vault-relative path
    date: str            # YYYY-MM-DD
    count: int = 1
    keys: frozenset = frozenset()   # identifiers, for merging run lessons


class TaskRow(NamedTuple):
    key: str             # repo key, "" for a vault task
    title: str


class Playbook(NamedTuple):
    text: str            # the whole file
    shown: int           # lines inside the cap
    total: int           # lessons before the cap


# -- paths ---------------------------------------------------------------------

def vault_root() -> Path:
    return Path(VAULT_ROOT) if VAULT_ROOT is not None else Path(gm.ROOT)


def repo_key(repo: str | None) -> str:
    """The playbook name of a repo URL (``usage_value.project_name``); "" for none."""
    repo = (repo or "").strip()
    return project_name(repo) if repo and repo != "-" else ""


def playbook_path(key: str, root: Path | None = None) -> Path:
    base = Path(root) if root is not None else vault_root()
    return base.joinpath(*PLAYBOOK_DIR) / f"{key}.md"


def _read(path: Path, limit: int | None = None) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return ""


# -- text helpers ----------------------------------------------------------------

_WS = re.compile(r"\s+")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_EMPHASIS = re.compile(r"\*\*|__")


def flat(text: str) -> str:
    """One line: wikilinks to their target, bold markers and the YAML escape of
    a quote (a frontmatter ``description: "… \\"x\\" …"``) dropped, whitespace
    collapsed."""
    text = _EMPHASIS.sub("", _LINK.sub(r"\1", str(text))).replace('\\"', '"')
    return _WS.sub(" ", text).strip()


def clip(text: str, max_bytes: int) -> str:
    """``text`` on one line, cut on a character boundary to at most ``max_bytes``
    UTF-8 bytes, ending in "…" when cut."""
    one = flat(text)
    raw = one.encode("utf-8")
    if len(raw) <= max_bytes:
        return one
    return raw[:max(max_bytes - 3, 0)].decode("utf-8", "ignore").rstrip() + "…"


_SCRUB = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"(?<![\w-])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
                r"|AIza[0-9A-Za-z_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,})"), "[secret]"),
    (re.compile(r"(https?://[^\s?#`'\")]+)\?[^\s`'\")]*"), r"\1"),
    (re.compile(r"(?<![\d.])(?:\+?82[- ]?|0)1[016789][- ]?\d{3,4}[- ]?\d{4}(?![\d.])"), "[phone]"),
    (re.compile(r"(?i)((?:인증\s*번호|인증|otp|verification code|코드)\D{0,6})\d{4,8}(?!\d)"),
     r"\1[code]"),
    (re.compile(r"/(?:home|Users)/[^/\s`'\"]+/"), "~/"),
)


def scrub(text: str) -> str:
    """The line-level half of the privacy boundary (the document half is R5)."""
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text)
    return text


_KEY_WORD = re.compile(r"[A-Za-z_][\w.:/-]*")
_CODE_LIKE = re.compile(r"[a-z][A-Z]|\w[._:/]\w")


def keys_of(text: str) -> frozenset:
    """The identifiers in ``text`` — camelCase words and dotted / colon / slash
    paths (``BackupImportTest.kt:60:9`` → ``backupimporttest.kt``) — lowercased.
    Plain words are left out: two failures are "the same" when they name the
    same code, not when they share English."""
    out = set()
    for raw in _KEY_WORD.findall(text or ""):
        word = raw.rstrip(".:/-")
        if not _CODE_LIKE.search(word):
            continue
        tok = re.sub(r"(?::\d+)+$", "", word.lower())
        if len(tok) >= 4:
            out.add(tok)
    return frozenset(out)


# Cause categories, first match wins. The order puts the cause above its
# symptom: a gate that "did not finish" is a budget problem even when the
# unfinished suite would also have been red.
CATEGORIES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pattern, re.I)) for name, pattern in (
        ("rebase", r"stale base|rebase|merge-tree|merge conflict|content conflict"
                   r"|conflicts? with|behind origin|충돌"),
        ("human", r"user decision|human decision|needs? (?:a )?(?:human|person|user)"
                  r"|사용자 (?:결정|선택)|사람이 (?:결정|판단)|결정 필요"),
        ("budget", r"timed? ?out|time ?cap|\d+[- ]?min(?:ute)?s? (?:command )?(?:cap|limit)"
                   r"|\d+\s?분 (?:명령 )?한도|한도에 걸|did not finish|didn't finish"
                   r"|never (?:completed|finished)|could not finish|끝내지 못|끝나지 않"
                   r"|exit 143|rc=143|max[ _-]?turns|turn (?:limit|cap)"
                   r"|ran out of (?:time|turns)|시간이 모자|SIGTERM"),
        ("device", r"emulator|에뮬레이터|on-device|real device|실기|기기에서|device-only"
                   r"|rotation|회전|accessibility|접근성|logcat|scenario|시나리오"),
        ("env", r"not available|not installed|not on (?:the )?path|whitelist"
                r"|allowed[_ -]?tools|허용 목록|gated|permission denied|toolchain"
                r"|gcloud|command not found"),
        ("compile", r"does not compile|doesn't compile|fails? to compile|failed to compile"
                    r"|compil(?:e|ation) error|unresolved reference|overrides nothing"
                    r"|type mismatch|cannot find symbol|BUILD FAILED"
                    r"|컴파일(?:되지| 오류|에 실패| 단계에서 멈|하지 못)"),
        ("resource", r"out ?of ?memory|\bOOM\b|java heap|heap space|memory\.high|freez"
                     r"|프리즈|\bhangs?\b|idle에 (?:도달|닿)|never idle|공회전|힙"),
        ("wiring", r"not wired|unwired|wiring|배선|call ?site|호출부|호출 위치|unreachable"),
        ("incomplete", r"not implemented|구현 미완|미구현|never ran|empty diff|diff 0"
                       r"|0 commits|no commits|손도 못|absent from the diff"),
        ("migration", r"migration|마이그레이션|schema version|스키마"),
        ("test", r"tests? fail|failing test|assert|\bred\b|실패|regression"),
        ("task-md", r"frontmatter|yaml|misrout"),
    ))
OTHER = "other"


def category_match(text: str) -> tuple[str, int | None]:
    """``(category, where its rule matched)``; ``(OTHER, None)`` for no match."""
    for name, pattern in CATEGORIES:
        m = pattern.search(text or "")
        if m:
            return name, m.start()
    return OTHER, None


def classify(text: str) -> str:
    return category_match(text)[0]


def clip_focused(text: str, max_bytes: int) -> str:
    """:func:`clip`, but when the words that decided the category sit past the
    first half of the budget, the cut starts a little before them ("…" first):
    a long cause keeps the part that says what failed, not its preamble."""
    one = flat(text)
    if len(one.encode("utf-8")) <= max_bytes:
        return one
    _name, at = category_match(one)
    if at is None or len(one[:at].encode("utf-8")) < max_bytes // 2:
        return clip(one, max_bytes)
    return clip("…" + one[max(at - FOCUS_LEAD_CHARS, 0):].lstrip(), max_bytes)


# -- verify.md / run.log --------------------------------------------------------

_CONCLUSION = ("Conclusion", "결론", "Verdict")
_VERDICT_LEAD = re.compile(r"^\s*(?:overall[:\s]*)?(?:FAIL|SKIP|PASS)\b[^.\n:—]*[.:—]?\s*", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+")
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
# "원인은" / "root cause" are deliberately absent: a Conclusion uses them for the
# bug the task fixes ("원인은 … 후보가 뜬 것이다"), not for why verify failed.
_DECIDING = re.compile(r"결정 요인|deciding (?:factor|check)|decisive", re.I)
_DECIDING_LEAD = re.compile(
    r"^.*?(?:결정 요인은|결정 요인:|deciding (?:factor|check)(?: is|:)|decisive \w+(?: is|:))\s*"
    r"(?:(?:하나|두 가지|세 가지)(?:이다|다|입니다)?\s*[—:.-]\s*)?", re.I)
_FAILING = re.compile(
    r"\bfail|does not|did not|doesn't|didn't|\bnever\b|cannot|can't|could not"
    r"|not (?:wired|run|implemented|compile)|timed? ?out|\bred\b|한도|못(?:했|하|한|해)"
    r"|않(?:았|는|아)|없(?:다|고|어|음)|실패|멈", re.I)
_AC_ROW = re.compile(r"^\|\s*([^|]*)\|\s*([^|]*)\|\s*[^|]*\|\s*\**\s*(?:FAIL|UNVERIFIED)\b[^|]*\|\s*([^|]*)\|",
                     re.M | re.I)


def section(body: str, names: Sequence[str] = _CONCLUSION) -> str:
    """The text under the first ``## <name>`` heading, up to the next ``## ``."""
    for name in names:
        m = re.search(rf"^##\s+{re.escape(name)}\b[^\n]*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S | re.I)
        if m:
            return m.group(1).strip()
    return ""


def _logical_lines(text: str) -> list[str]:
    """Markdown lines with soft-wrapped paragraphs joined: a new logical line
    starts after a blank line and at every bullet; any other line continues
    the one before it (a wrapped Conclusion must not end a sentence mid-way)."""
    out: list[str] = []
    joinable = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            joinable = False
            continue
        if joinable and not _BULLET.match(line) and out:
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
        joinable = True
    return out


def sentences(text: str) -> list[str]:
    """``text`` as sentences: per logical line (bullet markers dropped), then
    on . ! ? followed by whitespace."""
    out = []
    for line in _logical_lines(_EMPHASIS.sub("", text)):
        for part in _SENTENCE_SPLIT.split(line):
            part = _BULLET.sub("", part).strip()
            if part:
                out.append(part)
    return out


def _deciding(conclusion: str) -> str:
    """The Conclusion's deciding-factor sentence, else its first failure
    sentence, else its first sentence — the verdict word itself removed."""
    parts = sentences(conclusion)
    if parts:
        parts[0] = _VERDICT_LEAD.sub("", parts[0], count=1).strip()
    parts = [p for p in parts if p]
    for i, s in enumerate(parts):
        if _DECIDING.search(s):
            rest = _DECIDING_LEAD.sub("", s, count=1).strip() or s
            if len(rest) < 25 and i + 1 < len(parts):
                rest = f"{rest} {parts[i + 1]}"
            return rest
    for s in parts:
        if _FAILING.search(s):
            return s
    return parts[0] if parts else ""


def cause_of(verify_text: str) -> str:
    """One line on why a verify failed, by a fixed rule, first hit wins:
    ``blocked_on``; the Conclusion's deciding sentence; its first FAIL /
    UNVERIFIED acceptance row. "" when the file says none of these."""
    fm, body = gm.parse_frontmatter(verify_text or "")
    blocked = fm.get("blocked_on")
    if isinstance(blocked, str) and blocked.strip() not in ("", "-", '""', "none"):
        return flat(blocked)
    conclusion = section(body)
    if conclusion:
        found = _deciding(conclusion)
        if found:
            return flat(found)
    row = _AC_ROW.search(body)
    if row:
        return flat(f"AC {row.group(1).strip()} {row.group(2).strip()}: {row.group(3).strip()}")
    return ""


def pass_note(verify_text: str) -> str:
    """The first sentence of a PASS verify's Conclusion, verdict word removed."""
    _fm, body = gm.parse_frontmatter(verify_text or "")
    parts = sentences(section(body))
    if not parts:
        return ""
    return flat(_VERDICT_LEAD.sub("", parts[0], count=1)) or (flat(parts[1]) if len(parts) > 1 else "")


_COMMIT_LINE = re.compile(r"^\s*[-*]\s*commit:\s*(.+)$", re.M | re.I)
_SHA = r"[0-9a-f]{7,40}"
_QUOTES = "`\"'"
_TRAILER = re.compile(r"\s+(?:→|->)\s+|\s+\(\d+ files?\b|\s+—\s+커밋 뒤")
_NO_COMMIT = re.compile(r"^(?:없음|none|n/a|no commits?|0 commits?)\b", re.I)


def _is_label(head: str, slug: str) -> bool:
    """Whether the part before a subject's first " — " is a label (slug, phase,
    ellipsis) rather than the subject itself."""
    head = head.strip()
    return bool(re.search(r"\bphase\s*\d", head, re.I) or "…" in head or "..." in head
                or (slug and slug[:12] in head) or re.fullmatch(r"[\w-]+", head))


def commit_subject(raw: str, slug: str = "") -> str:
    """The subject of one run.log ``- commit:`` value, without the sha, quotes,
    conventional prefix, slug and ``phase N —`` label; "" for "no commit"."""
    s = raw.strip()
    if s and s[0] in _QUOTES:
        end = s.find(s[0], 1)
        inner = s[1:end] if end > 0 else s[1:]
        s = s[end + 1:] if re.fullmatch(_SHA, inner.strip()) and end > 0 else inner
    s = re.sub(rf"^{_SHA}\b\s*", "", s.strip())
    s = re.sub(rf"^\((?:full sha\s*)?{_SHA}\)\s*", "", s)
    s = s.lstrip("—–-:· ").strip()
    if s and s[0] in _QUOTES:
        end = s.find(s[0], 1)
        s = s[1:end] if end > 0 else s[1:]
    else:
        s = _TRAILER.split(s)[0]
    s = re.sub(r"^[a-z]+(?:\([^)]*\))?!?:\s*", "", s.strip())
    head, sep, tail = s.partition(" — ")
    if sep and _is_label(head, slug):
        s = tail
    s = flat(s).strip("`\"' ")
    return "" if not s or _NO_COMMIT.match(s) else s


def fix_of(run_log: str, slug: str = "") -> str:
    """The repair: the first two distinct commit subjects a run.log records."""
    subjects: list[str] = []
    for raw in _COMMIT_LINE.findall(run_log or ""):
        subject = commit_subject(raw, slug)
        if subject and subject not in subjects:
            subjects.append(subject)
        if len(subjects) == 2:
            break
    return "; ".join(subjects)


# -- the optional summary: the PASS run's own episode ------------------------------

_LESSONS_HEADING = re.compile(r"^##\s+(?:학습|lessons?|learnings?)\b[^\n]*$", re.M | re.I)


def episode_lessons(root: Path, slug: str, run_id: str) -> list[str]:
    """The lesson bullets the report stage wrote for ``run_id`` in the task's
    episode file (inside R5), in their order; [] when there are none."""
    edir = Path(root).joinpath(*EPISODES_DIR)
    for path in sorted(edir.glob(f"*-worker-{glob.escape(slug)}.md"), reverse=True):
        doc = rc.read_document(Path(root), path)
        if doc is None:
            continue
        body = doc[2]
        at = body.find(run_id)
        if at < 0:
            continue
        part = body[at:]
        nxt = re.search(r"^# ", part, re.M)
        part = part[:nxt.start()] if nxt else part
        head = _LESSONS_HEADING.search(part)
        if head is None:
            return []
        block = part[head.end():]
        stop = re.search(r"^##\s", block, re.M)
        block = block[:stop.start()] if stop else block
        bullets = re.findall(r"^\s*[-*]\s+(.+(?:\n(?![ \t]*[-*]\s|[ \t]*$).+)*)", block, re.M)
        return [flat(b) for b in bullets if b.strip()]
    return []


def episode_summary(root: Path, slug: str, pass_run: str, context: str) -> str | None:
    """The PASS run's first lesson — the report stage leads with the one that
    mattered (14 of 18 real FAIL→PASS pairs, 2026-11) — unless a later lesson
    names at least SUMMARY_MIN_KEYS more of the failure's identifiers
    (``context``: cause, fix, Conclusion) than the first does. None when the
    run wrote no lesson, and the deterministic text stands."""
    lessons = episode_lessons(root, slug, pass_run)
    if not lessons:
        return None
    want = keys_of(context)
    scores = [len(keys_of(text) & want) for text in lessons]
    best = max(range(len(lessons)), key=lambda i: (scores[i], -i))
    if scores[best] >= SUMMARY_MIN_KEYS and scores[best] > scores[0]:
        return lessons[best]
    return lessons[0]


# -- sources ---------------------------------------------------------------------

_RUN_NAME = re.compile(r"^(\d{4})(\d{2})(\d{2})T\d{6}-(.+)$")


def run_date(run_id: str) -> str:
    m = _RUN_NAME.match(run_id)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def verdict_of(run_dir: Path) -> str | None:
    """``pass`` / ``fail`` / ``skip`` from a run's verify.md, or None."""
    from .orchestrator import stages     # resolves a vault root at import: not at module top
    text = _read(Path(run_dir) / "verify.md", VERIFY_HEAD)
    verdict = stages.parse_verify_verdict(text) if text else None
    return verdict.lower() if verdict else None


def _h1(body: str) -> str:
    for line in body.splitlines()[:rc.HEADING_SCAN_LINES]:
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def task_index(root: Path) -> dict[str, TaskRow]:
    """``{slug: TaskRow}`` for every task file inside R5. A task outside R5 is
    absent, so nothing of its runs is ever read."""
    items = Path(root).joinpath(*ITEMS_DIR)
    out: dict[str, TaskRow] = {}
    try:
        paths = sorted(items.glob("*.md"))
    except OSError:
        return out
    for path in paths:
        doc = rc.read_document(Path(root), path, head=TASK_HEAD)
        if doc is None:
            continue
        _rel, fm, body = doc
        repo = fm.get("repo")
        out[path.stem] = TaskRow(repo_key(repo) if isinstance(repo, str) else "", _h1(body))
    return out


def run_dirs(root: Path, slugs: Iterable[str] | None = None) -> dict[str, list[Path]]:
    """``{slug: [run dir, oldest first]}``; only ``slugs`` when given."""
    wanted = set(slugs) if slugs is not None else None
    found: dict[str, list[Path]] = defaultdict(list)
    try:
        entries = list(os.scandir(Path(root).joinpath(*RUNS_DIR)))
    except OSError:
        return {}
    for entry in entries:
        m = _RUN_NAME.match(entry.name)
        if m is None or (wanted is not None and m.group(4) not in wanted):
            continue
        try:
            if entry.is_dir():
                found[m.group(4)].append(Path(entry.path))
        except OSError:
            continue
    return {slug: sorted(dirs, key=lambda p: p.name) for slug, dirs in found.items()}


def run_pairs(root: Path, slugs: Iterable[str]) -> list[tuple[Path, Path, str]]:
    """``(fail run, pass run, slug)``: per task, the last ``fail``/``skip``
    verify before each ``pass`` that follows one."""
    pairs = []
    for slug, dirs in sorted(run_dirs(root, slugs).items()):
        last_bad: Path | None = None
        for d in dirs:
            verdict = verdict_of(d)
            if verdict in NOT_PASS:
                last_bad = d
            elif verdict == "pass" and last_bad is not None:
                pairs.append((last_bad, d, slug))
                last_bad = None
    return pairs


def lesson_from_pair(root: Path, fail_dir: Path, pass_dir: Path, slug: str, *,
                     summarize: Summarizer | None = episode_summary) -> Lesson | None:
    """The playbook lesson of one FAIL→PASS pair, or None when the failing
    verify names no cause. The category and the merge identifiers always come
    from the deterministic cause and fix; ``summarize`` only replaces the text."""
    fail_text = _read(Path(fail_dir) / "verify.md")
    cause = cause_of(fail_text)
    if not cause:
        return None
    fix = (fix_of(_read(Path(pass_dir) / "run.log"), slug)
           or pass_note(_read(Path(pass_dir) / "verify.md")))
    conclusion = section(gm.parse_frontmatter(fail_text)[1])
    category = classify(cause)
    if category == OTHER:
        category = classify(conclusion)
    text = None
    if summarize is not None:
        try:
            text = summarize(Path(root), slug, Path(pass_dir).name, f"{cause}\n{fix}\n{conclusion}")
        except Exception:       # noqa: BLE001 - a summary is optional by definition
            text = None
    if text and text.strip():
        text = clip(scrub(text), SUMMARY_MAX_BYTES)
    else:
        text = clip_focused(scrub(cause), CAUSE_MAX_BYTES)
        if fix:
            text = f"{text} → {clip(scrub(fix), FIX_MAX_BYTES)}"
    return Lesson("run", category, text, Path(pass_dir).name, run_date(Path(pass_dir).name), 1,
                  keys_of(f"{cause} {fix}"))


def _first_word(title: str) -> str:
    words = title.split()
    return re.sub(r"[^\w-]", "", words[0].lower()) if words else ""


def repo_aliases(key: str, index: dict[str, TaskRow]) -> set[str]:
    """The names the vault uses for repo ``key``: the key itself, plus the first
    slug word and the first title word that the repo's tasks use and other
    tasks (almost) never do — e.g. a project nickname all its slugs start with."""
    aliases = {key} if key else set()
    tables = ((lambda slug, row: slug.split("-", 1)[0].lower(),
               SLUG_ALIAS_MIN_TASKS, SLUG_ALIAS_MIN_SHARE),
              (lambda slug, row: _first_word(row.title),
               TITLE_ALIAS_MIN_TASKS, TITLE_ALIAS_MIN_SHARE))
    for word_of, min_tasks, min_share in tables:
        counts: dict[str, Counter] = defaultdict(Counter)
        for slug, row in index.items():
            word = word_of(slug, row)
            if word:
                counts[word][row.key] += 1
        for word, by_repo in counts.items():
            mine = by_repo.get(key, 0)
            if (len(word) >= ALIAS_MIN_LEN and mine >= min_tasks
                    and mine / sum(by_repo.values()) >= min_share):
                aliases.add(word)
    return aliases


def mentions(text: str, alias: str) -> bool:
    """``alias`` as a whole word of ``text`` (ASCII), or anywhere in it (a Hangul
    alias takes particles: "<name>에서")."""
    if not alias:
        return False
    if alias.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text, re.I) is not None
    return alias in text


def _memory_date(fm: dict, path: Path) -> str:
    raw = str(fm.get("modified") or "")
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    try:
        return dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return ""


def about_repo(stem: str, name: str, desc: str, aliases: Iterable[str]) -> bool:
    """Whether a memory is about the repo: its file stem or ``name`` names an
    alias, or its description LEADS with one ("<repo>: …", "<repo>/<lib> …",
    "<nickname> 프리셋 …"). A description that only mentions the repo further
    in is about something else that touches it (another app's deploy recipe
    that shares its database) and does not count."""
    lead = re.split(r"[\s:/(,—–]", desc.strip(), maxsplit=1)[0] if desc.strip() else ""
    return any(mentions(text, alias) for alias in aliases if alias
               for text in (stem, name, lead) if text)


def memory_lessons(root: Path, aliases: Iterable[str]) -> list[Lesson]:
    """One lesson per interactive memory (inside R5) that is :func:`about_repo`.
    ``type: user`` memories are about the person, not the code, and are left
    out; so is the index file."""
    names = [a for a in aliases if a]
    mdir = Path(root).joinpath(*MEMORY_DIR)
    try:
        paths = sorted(mdir.glob("*.md"))
    except OSError:
        return []
    out = []
    for path in paths:
        if path.name == MEMORY_INDEX:
            continue
        doc = rc.read_document(Path(root), path)
        if doc is None:
            continue
        rel, fm, _body = doc
        desc = fm.get("description")
        if not (isinstance(desc, str) and desc.strip()):
            continue
        if str(fm.get("type") or "").strip().lower() == "user":
            continue
        if not about_repo(path.stem, str(fm.get("name") or ""), desc, names):
            continue
        out.append(Lesson("memory", classify(desc), clip(scrub(desc), MEMORY_MAX_BYTES), rel,
                          _memory_date(fm, path)))
    return out


# -- merge, expire, order, render --------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def same_lesson(a: Lesson, b: Lesson) -> bool:
    """Two run lessons are one pitfall when they share a category and either
    the category is CATEGORY_MERGED, the texts are equal, or their identifiers
    overlap (MERGE_MIN_SHARED, MERGE_MIN_OVERLAP of the smaller set)."""
    if a.kind != "run" or b.kind != "run" or a.category != b.category:
        return False
    if a.category in CATEGORY_MERGED or _norm(a.text) == _norm(b.text):
        return True
    shared = a.keys & b.keys
    small = min(len(a.keys), len(b.keys))
    return len(shared) >= MERGE_MIN_SHARED and small > 0 and len(shared) / small >= MERGE_MIN_OVERLAP


def merge(lessons: Iterable[Lesson]) -> list[Lesson]:
    """Duplicates (:func:`same_lesson`) folded into one line: counts added,
    identifiers joined, the newest text / source / date kept."""
    out: list[Lesson] = []
    for les in sorted(lessons, key=lambda x: (x.date, x.source)):
        for i, have in enumerate(out):
            if same_lesson(have, les):
                out[i] = les._replace(count=have.count + les.count, keys=have.keys | les.keys)
                break
        else:
            out.append(les)
    return out


def expire(lessons: Iterable[Lesson], today: dt.date, days: int = EXPIRE_DAYS) -> list[Lesson]:
    """Run lessons dated before ``today - days`` dropped; memory lines kept."""
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    return [x for x in lessons if x.kind != "run" or x.date >= cutoff]


def ordered(lessons: Iterable[Lesson]) -> list[Lesson]:
    """Run lessons by count then date; memory lines with a pitfall category
    before the uncategorised ones (a design note is context, a pitfall is the
    point), each by date; the two lists interleaved, run first."""
    lessons = list(lessons)
    runs = sorted((x for x in lessons if x.kind == "run"),
                  key=lambda x: (x.count, x.date, x.source), reverse=True)
    mems = sorted((x for x in lessons if x.kind != "run"),
                  key=lambda x: (x.category != OTHER, x.date, x.source), reverse=True)
    out = []
    for i in range(max(len(runs), len(mems))):
        out += runs[i:i + 1] + mems[i:i + 1]
    return out


def _short(text: str, limit: int = SOURCE_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def item_line(les: Lesson) -> str:
    times = f"×{les.count} · " if les.count > 1 else ""
    source = les.source if les.kind == "run" else f"mem {Path(les.source).stem}"
    date = f" · {les.date}" if les.date else ""
    return f"- [{les.category}] {les.text} ({times}{_short(source)}{date})"


def _size(lines: Sequence[str]) -> int:
    return len("\n".join(lines).encode("utf-8"))


def render_section(key: str, lessons: Iterable[Lesson], *,
                   max_bytes: int = MAX_BYTES) -> tuple[str, int]:
    """``(section, lines shown)``: heading, intro, then whole item lines in
    :func:`ordered` order while the section stays within ``max_bytes``. A line
    that does not fit is skipped and a shorter later one may still go in."""
    lines = [HEADING.format(key=key), "", INTRO, ""]
    shown = 0
    for les in ordered(lessons):
        line = item_line(les)
        if _size(lines + [line]) <= max_bytes:
            lines.append(line)
            shown += 1
    return "\n".join(lines), shown


def render(key: str, lessons: Sequence[Lesson]) -> Playbook:
    body, shown = render_section(key, lessons)
    head = ["---", "kind: playbook", f"repo: {key}", f"items: {shown}", "---", ""]
    return Playbook("\n".join(head) + body + "\n", shown, len(lessons))


def collect(repo: str, *, root: Path, today: dt.date,
            summarize: Summarizer | None = episode_summary) -> list[Lesson]:
    """Every lesson of ``repo`` as of ``today``: merged, unexpired run lessons
    plus the memory lines."""
    key = repo_key(repo)
    if not key:
        return []
    index = task_index(root)
    slugs = [slug for slug, row in index.items() if row.key == key]
    lessons = [les for les in (lesson_from_pair(root, f, p, s, summarize=summarize)
                               for f, p, s in run_pairs(root, slugs)) if les is not None]
    return merge(expire(lessons, today)) + memory_lessons(root, repo_aliases(key, index))


def build(repo: str, *, root: Path | None = None, today: dt.date | None = None,
          summarize: Summarizer | None = episode_summary) -> Playbook | None:
    """The playbook file of ``repo``, or None when there is nothing to say."""
    base = Path(root) if root is not None else vault_root()
    lessons = collect(repo, root=base, today=today or dt.date.today(), summarize=summarize)
    if not lessons:
        return None
    book = render(repo_key(repo), lessons)
    return book if book.shown else None


# -- the two entry points the pipeline uses ----------------------------------------

def refresh(repo: str | None, *, root: Path | None = None, today: dt.date | None = None,
            local_id: str | None = None,
            summarize: Summarizer | None = episode_summary) -> str | None:
    """Rebuild ``Memory/playbooks/<repo>.md``; one progress line when the file
    changed, None otherwise. Never raises.

    Writes only on the HQ host (``hq_host.check`` — fail-open like every caller
    of it: no marker means a single-host vault), only when the bytes differ,
    and removes the file when the repo has nothing left to say."""
    if not repo:
        return None
    try:
        base = Path(root) if root is not None else vault_root()
        if not hq_host.check(base, local_id)[0]:
            return None
        key = repo_key(repo)
        if not key:
            return None
        path = playbook_path(key, base)
        book = build(repo, root=base, today=today, summarize=summarize)
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        rel = "/".join((*PLAYBOOK_DIR, path.name))
        if book is None:
            if old is None:
                return None
            path.unlink()
            return f"playbook: {key} — nothing left to say, removed {rel}"
        if book.text == old:
            return None
        atomic_write_text(path, book.text)
        return f"playbook: {key} — {book.shown} line(s) → {rel}"
    except Exception:       # noqa: BLE001 - a hint must never break a run
        return None


def prompt_section(repo: str | None, *, root: Path | None = None,
                   max_bytes: int = MAX_BYTES) -> str:
    """The playbook of ``repo`` as a prompt section (≤ ``max_bytes`` UTF-8), or
    "" — no repo, no file, or no item line. Never raises.

    Re-capped at read time, line by line, so a hand-edited file cannot push a
    stage prompt past the limit."""
    if not repo:
        return ""
    try:
        key = repo_key(repo)
        if not key:
            return ""
        text = playbook_path(key, Path(root) if root is not None else None).read_text(encoding="utf-8")
        body = gm.parse_frontmatter(text)[1].strip("\n")
        kept: list[str] = []
        for line in body.splitlines():
            if _size(kept + [line]) > max_bytes:
                break
            kept.append(line)
        if not any(line.startswith("- ") for line in kept):
            return ""
        return "\n".join(kept).rstrip()
    except Exception:       # noqa: BLE001 - the prompt builder must not fail
        return ""


# -- measurement -------------------------------------------------------------------

class FailEvent(NamedTuple):
    run_id: str
    slug: str
    repo: str            # repo key, "vault" for a vault task
    category: str
    day: dt.date


def fail_events(root: Path, index: dict[str, TaskRow] | None = None) -> list[FailEvent]:
    """Every ``fail``/``skip`` verify of a task inside R5, oldest first."""
    index = task_index(root) if index is None else index
    events = []
    for slug, dirs in run_dirs(root, index).items():
        for d in dirs:
            if verdict_of(d) not in NOT_PASS:
                continue
            m = _RUN_NAME.match(d.name)
            day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            events.append(FailEvent(d.name, slug, index[slug].key or "vault",
                                    classify(cause_of(_read(d / "verify.md"))), day))
    return sorted(events, key=lambda e: e.run_id)


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def recurrence(root: Path | None = None, *, weeks: int = RECURRENCE_WEEKS,
               today: dt.date | None = None, events: Sequence[FailEvent] | None = None) -> list[dict]:
    """One row per week (Monday first, oldest first, the current week last):
    ``fails`` = fail/skip verifies that week; ``repeats`` = those whose cause
    category another task of the same repo already failed on in the
    EXPIRE_DAYS before; ``categories`` = the repeats by category."""
    base = Path(root) if root is not None else vault_root()
    today = today or dt.date.today()
    events = fail_events(base) if events is None else list(events)
    first = week_start(today) - dt.timedelta(weeks=max(weeks, 1) - 1)
    rows = {first + dt.timedelta(weeks=i): {"week": first + dt.timedelta(weeks=i), "fails": 0,
                                            "repeats": 0, "categories": Counter()}
            for i in range(max(weeks, 1))}
    seen: dict[tuple[str, str], list[FailEvent]] = defaultdict(list)
    horizon = dt.timedelta(days=EXPIRE_DAYS)
    for ev in events:
        prior = seen[(ev.repo, ev.category)]
        repeat = ev.category != OTHER and any(
            p.slug != ev.slug and ev.day - p.day <= horizon for p in prior)
        row = rows.get(week_start(ev.day))
        if row is not None and ev.day <= today:
            row["fails"] += 1
            if repeat:
                row["repeats"] += 1
                row["categories"][ev.category] += 1
        prior.append(ev)
    return [rows[k] for k in sorted(rows)]


def _categories_text(counter: Counter) -> str:
    return ", ".join(f"{name}×{n}" for name, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def recurrence_table(rows: Sequence[dict]) -> str:
    lines = ["| week of | fail/skip | same-cause repeats | repeated categories |",
             "|---|---:|---:|---|"]
    for row in rows:
        lines.append(f"| {row['week'].isoformat()} | {row['fails']} | {row['repeats']} | "
                     f"{_categories_text(row['categories']) or '—'} |")
    return "\n".join(lines)


def brief_line(root: Path | None = None, *, today: dt.date | None = None,
               weeks: int = RECURRENCE_WEEKS) -> str:
    """The one line the weekly usage section of the brief ends with:
    repeats/all per week. "" on any failure."""
    from . import i18n
    try:
        rows = recurrence(root, weeks=weeks, today=today)
    except Exception:       # noqa: BLE001 - the brief must not lose a section over it
        return ""
    cells = " · ".join(f"{r['week'].month}/{r['week'].day} {r['repeats']}/{r['fails']}"
                       for r in rows)
    return i18n.t("playbooks.brief_line", weeks=len(rows), cells=cells)


# -- CLI -----------------------------------------------------------------------------

def _all_repos(root: Path) -> list[str]:
    """Every repo key a task names (a bare key is its own repo_key)."""
    return sorted({row.key for row in task_index(root).values() if row.key})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m todo_system.playbooks",
                                description="Per-repo playbooks of pitfalls from past runs.")
    sub = p.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="print the playbook a refresh would write (writes nothing)")
    show.add_argument("--repo", required=True)
    ref = sub.add_parser("refresh", help="rebuild Memory/playbooks/<repo>.md (HQ host only)")
    group = ref.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo")
    group.add_argument("--all", action="store_true", help="every repo a task names")
    rec = sub.add_parser("recurrence", help="weekly same-cause FAIL repeats (the keep/delete measure)")
    rec.add_argument("--weeks", type=int, default=RECURRENCE_WEEKS)
    for sp in (show, ref, rec):
        sp.add_argument("--vault-root", type=Path, default=None)
        sp.add_argument("--today", type=dt.date.fromisoformat, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.vault_root if args.vault_root is not None else vault_root()
    if args.cmd == "show":
        book = build(args.repo, root=root, today=args.today)
        if book is None:
            print(f"(no playbook for {repo_key(args.repo) or args.repo})", file=sys.stderr)
            return 1
        sys.stdout.write(book.text)
        return 0
    if args.cmd == "refresh":
        repos = _all_repos(root) if args.all else [args.repo]
        for repo in repos:
            line = refresh(repo, root=root, today=args.today)
            if line:
                print(line)
        return 0
    rows = recurrence(root, weeks=args.weeks, today=args.today)
    print(recurrence_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
