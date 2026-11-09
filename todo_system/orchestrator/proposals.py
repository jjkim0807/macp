"""Unapplied prompt proposals → dashboard human rows.

Headless workers cannot write ``.claude/**`` (a built-in hard guard), so a stage
that changes a prompt leaves the finished file under
``Worker/proposals/<slug>/commands/<name>.md`` (or ``…/<slug>/.claude/commands/``)
plus a README, and a person applies it with one ``cp``. Nothing announced those
proposals, so one sat unapplied for days. :func:`pending_proposals` finds every
proposal whose command files still differ from (or are missing in)
``<root>/.claude/commands/`` and returns one ``why="todo"`` row per proposal, in
exactly the key set of the dashboard's other human rows (app compatibility).

Polling cost: the dashboard polls every few seconds, so the result is cached
against a stat-only signature (mtime_ns + size of every involved file and dir);
file contents are read only when that signature changes. Stdlib only, fail-soft:
any error yields ``[]`` — it must never break ``/api/status``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PROPOSALS_REL = Path("Worker") / "proposals"
COMMANDS_REL = Path(".claude") / "commands"
_LAYOUTS = ("commands", os.path.join(".claude", "commands"))
ACTION_MAX = 110          # same cut as collect_human's `action`
COND_MAX = 60
PROJECT = "macp"

_CP_RE = re.compile(r"(?:^|[\s`$])cp\s+(\S+)\s+(\S+)")
_COND_RE = re.compile(r"권장일 때만|일 때만|조건부|only if|only when|only after",
                      re.IGNORECASE)

# str(root) → (signature, rows)
_CACHE: dict[str, tuple[tuple, list[dict]]] = {}


def _read(path: Path) -> bytes:
    """The single file-content read (tests patch it to prove the cache holds)."""
    return path.read_bytes()


def _stat(path: Path) -> tuple:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def _slug_dirs(proposals_dir: Path) -> list[Path]:
    return sorted(p for p in proposals_dir.iterdir() if p.is_dir())


def _command_files(slug_dir: Path) -> list[tuple[Path, str]]:
    """(file, name) for every ``*.md`` under either accepted layout."""
    out = []
    for sub in _LAYOUTS:
        d = slug_dir / sub
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix == ".md" and f.is_file():
                out.append((f, f.name))
    return out


def _readme(slug_dir: Path) -> Path | None:
    for f in sorted(slug_dir.iterdir()):
        if f.name.lower() == "readme.md" and f.is_file():
            return f
    return None


def _signature(root: Path) -> tuple | None:
    """Stat-only fingerprint of everything the judgement depends on, or None when
    there is no proposals dir (wheel install / nothing ever proposed)."""
    proposals_dir = root / PROPOSALS_REL
    if not proposals_dir.is_dir():
        return None
    sig = [_stat(proposals_dir), _stat(root / COMMANDS_REL)]
    commands_dir = root / COMMANDS_REL
    if commands_dir.is_dir():
        sig.extend(_stat(f) for f in sorted(commands_dir.iterdir()))
    for slug_dir in _slug_dirs(proposals_dir):
        sig.append(_stat(slug_dir))
        for sub in _LAYOUTS:
            if (slug_dir / sub).is_dir():
                sig.append(_stat(slug_dir / sub))
        readme = _readme(slug_dir)
        if readme is not None:
            sig.append(_stat(readme))
        sig.extend(_stat(f) for f, _ in _command_files(slug_dir))
    return tuple(sig)


def _clean(text: str) -> str:
    text = text.replace("`", "").replace("**", "").replace("*", "")
    return " ".join(text.split())


def _paragraphs(readme_text: str) -> list[str]:
    """Blank-line separated paragraphs outside code fences, lines kept raw."""
    paras, cur, in_fence = [], [], False
    for line in readme_text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if cur:
                paras.append("\n".join(cur))
                cur = []
            continue
        if in_fence:
            continue
        if line.strip():
            cur.append(line)
        elif cur:
            paras.append("\n".join(cur))
            cur = []
    if cur:
        paras.append("\n".join(cur))
    return paras


def _is_cp_line(line: str) -> bool:
    return line.strip().lstrip("`$-> ").startswith("cp ")


def _cp_line(readme_text: str) -> str:
    """The first README line that is a ``cp …`` command (fences included)."""
    for line in readme_text.splitlines():
        if _is_cp_line(line):
            return " ".join(line.strip().lstrip("`$-> ").rstrip("`").split())
    return ""


def _summary(readme_text: str) -> str:
    for para in _paragraphs(readme_text):
        lines = [ln.strip().lstrip("> ").strip() for ln in para.splitlines()]
        if not lines or lines[0].startswith(("#", "|")) or _is_cp_line(lines[0]):
            continue
        text = _clean(" ".join(lines))
        if text:
            return text[:ACTION_MAX]
    return ""


def _condition(readme_text: str) -> str:
    """First paragraph naming a conditional apply ("권장일 때만", "only if" …)."""
    for para in _paragraphs(readme_text):
        text = _clean(" ".join(ln.strip().lstrip("#>- ").strip()
                                for ln in para.splitlines()))
        if _COND_RE.search(text):
            return text[:COND_MAX]
    return ""


def _dest_map(readme_text: str, root: Path) -> dict[str, Path]:
    """basename → destination, from the README's ``cp <src> <dst>`` lines. Only
    destinations inside ``.claude/commands/`` naming a file are taken; anything
    else (globs, a bare directory) falls back to the same-name rule."""
    out: dict[str, Path] = {}
    marker = "/".join(COMMANDS_REL.parts) + "/"
    for line in readme_text.splitlines():
        for src, dst in _CP_RE.findall(line):
            src, dst = src.strip("`"), dst.strip("`")
            name = src.rsplit("/", 1)[-1]
            if "*" in name or not name.endswith(".md"):
                continue
            idx = dst.find(marker)
            if idx < 0 or dst.endswith("/"):
                continue
            out.setdefault(name, root / dst[idx:])
    return out


def _texts():
    try:
        from .. import i18n

        return (lambda slug: i18n.t("dash.proposal.title", slug=slug),
                lambda cond: i18n.t("dash.proposal.conditional", cond=cond))
    except Exception:
        return (lambda slug: f"Unapplied prompt proposal: {slug}",
                lambda cond: f"[conditional: {cond}] ")


def _row(root: Path, slug_dir: Path, readme_text: str, unapplied: Path) -> dict:
    title_of, cond_of = _texts()
    slug = slug_dir.name
    cp = _cp_line(readme_text)
    if not cp:
        rel = unapplied.relative_to(root).as_posix()
        cp = f"cp {rel} {'/'.join(COMMANDS_REL.parts)}/{unapplied.name}"
    summary = _summary(readme_text)
    cond = _condition(readme_text)
    action = (cond_of(cond) if cond else "") + cp + (f" — {summary}" if summary else "")
    # `slug` is the bare proposal slug: tapping the row may open an empty task view
    # when no task shares it — accepted (the row is a reminder to run one cp).
    return {"slug": slug, "title": title_of(slug), "action": action[:ACTION_MAX],
            "why": "todo", "due": "", "not_before": "",
            "urgent": not cond, "later": False,
            "category": "dev", "project": PROJECT}


def _judge(root: Path) -> list[dict]:
    rows = []
    for slug_dir in _slug_dirs(root / PROPOSALS_REL):
        try:
            files = _command_files(slug_dir)
            if not files:
                continue
            readme = _readme(slug_dir)
            readme_text = (_read(readme).decode("utf-8", "replace")
                           if readme is not None else "")
            mapping = _dest_map(readme_text, root)
            unapplied = None
            for f, name in files:
                dest = mapping.get(name, root / COMMANDS_REL / name)
                if not dest.is_file() or _read(dest) != _read(f):
                    unapplied = f
                    break
            if unapplied is not None:
                rows.append(_row(root, slug_dir, readme_text, unapplied))
        except Exception:
            continue
    rows.sort(key=lambda r: r["slug"])
    return rows


def pending_proposals(root: Path | str) -> list[dict]:
    """One human ``todo`` row per unapplied proposal under ``<root>/Worker/proposals``.

    Cached per root against a stat-only signature, so an unchanged tree costs a
    handful of ``stat`` calls and no reads; a ``cp`` (destination mtime/size) or a
    removed proposal dir changes the signature and the row clears. Returns fresh
    dict copies so callers may stamp extra keys without poisoning the cache.
    """
    try:
        root = Path(root)
        sig = _signature(root)
        if sig is None:
            return []
        key = str(root)
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == sig:
            rows = hit[1]
        else:
            rows = _judge(root)
            _CACHE[key] = (sig, rows)
        return [dict(r) for r in rows]
    except Exception:
        return []
