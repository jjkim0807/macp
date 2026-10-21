"""Characterization tests for ``todo_system.session_log``.

Covers the journal-append contract (CLAUDE.md §9), the git-state capture, and
the one real ``git commit`` path in either of the two modules under test —
``_commit_and_maybe_push``. That commit path is exercised against a genuine
throwaway git repository rather than a mock, because the failure mode worth
catching is "the vault silently stops recording manual work", which only a real
repo reproduces.

Safety: ``isolated_vault`` re-binds ``session_log.ROOT`` and ``SESSIONS_DIR`` into
``tmp_path``. Every test that can write or commit asserts that redirection in its
first lines — an unpatched run would ``git add`` the developer's real vault.
"""

from __future__ import annotations

import ast
import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import session_log

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# git helpers (real repos in tmp_path)
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)], capture_output=True, check=True
    )
    _run_git(path, "config", "user.email", "test@example.com")
    _run_git(path, "config", "user.name", "Test User")
    _run_git(path, "config", "commit.gpgsign", "false")
    # Neutralise any hooks the developer configured globally.
    _run_git(path, "config", "core.hooksPath", str(path / ".no-hooks"))
    return path


def _commit(repo: Path, filename: str, subject: str) -> str:
    (repo / filename).write_text(filename, encoding="utf-8")
    _run_git(repo, "add", filename)
    _run_git(repo, "commit", "-q", "-m", subject)
    return _run_git(repo, "rev-parse", "HEAD")


def _inside_git_repo(path: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _today() -> str:
    return session_log._now_local().date().isoformat()


# Journal filenames carry the host id, so every test runs as one fixed host
# rather than whatever machine pytest happens to be on.
HOST = "host-a"


@pytest.fixture(autouse=True)
def _pin_host(monkeypatch):
    monkeypatch.setenv("MACP_HOST_ID", HOST)


def _name(session: str | None = "s1", project: str = "demo", host: str = HOST) -> str:
    """Today's journal filename; ``session=None`` is the default (dated) session."""
    stem = f"{_today()}-{project}" if session is None else f"{_today()}-{session}-{project}"
    return f"{stem}.{host}.md"


def _journal(vault, name: str | None = None) -> tuple[dict, str]:
    if name is None:
        name = _name()
    return session_log.parse_frontmatter((vault.sessions / name).read_text(encoding="utf-8"))


def _subjects(commits: list[str]) -> list[str]:
    return [c.split(" ", 1)[1] for c in commits]


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("agent-commander", "agent-commander"),
        ("Agent Commander", "agent-commander"),
        ("agent_commander", "agent-commander"),
        ("  Mixed CASE  ", "mixed-case"),
        ("a--b", "a-b"),
        ("!!!", "session"),  # nothing survives → fallback
        ("한글", "session"),  # non-ASCII is dropped entirely
    ],
)
def test_slugify(raw, expected):
    assert session_log._slugify(raw) == expected


# ---------------------------------------------------------------------------
# Journal append contract (AC#5)
# ---------------------------------------------------------------------------


def test_same_day_same_project_appends_to_a_single_journal(isolated_vault):
    # Probe strings must not occur in the boilerplate intro, which contains the
    # word "second-brain".
    assert session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "alpha-note"]) == 0
    assert session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "beta-note"]) == 0

    assert [p.name for p in isolated_vault.sessions.glob("*.md")] == [_name()]
    fm, body = _journal(isolated_vault)
    assert fm["checkpoints"] == "2"
    assert body.count("## Checkpoints") == 1
    assert body.index("alpha-note") < body.index("beta-note")
    assert body.count("### ") == 2  # one timestamped heading per checkpoint


def test_default_session_id_is_todays_date(isolated_vault):
    assert session_log.main(["log", "--project", "demo", "--note", "n"]) == 0
    files = list(isolated_vault.sessions.glob("*.md"))
    assert len(files) == 1
    stem = files[0].name.removesuffix(f"-demo.{HOST}.md")
    assert dt.date.fromisoformat(stem) == dt.datetime.now(dt.timezone.utc).astimezone().date()


def test_explicit_session_ids_split_the_journal(isolated_vault):
    session_log.main(["log", "--project", "demo", "--session", "morning", "--note", "a"])
    session_log.main(["log", "--project", "demo", "--session", "evening", "--note", "b"])
    assert sorted(p.name for p in isolated_vault.sessions.glob("*.md")) == [
        _name("evening"),
        _name("morning"),
    ]


def test_explicit_session_id_preserves_today_date_in_filename_and_event_date(isolated_vault):
    """Regression: ``--session compact-controls`` used to produce a filename and
    ``event_date`` seeded from the session id itself, losing the real date."""
    today = _today()
    assert session_log.main(
        ["log", "--project", "demo", "--session", "compact-controls", "--note", "n"]
    ) == 0

    expected = isolated_vault.sessions / _name("compact-controls")
    assert expected.exists()
    fm, _ = session_log.parse_frontmatter(expected.read_text(encoding="utf-8"))
    assert fm["event_date"] == today
    assert dt.date.fromisoformat(fm["event_date"]) == dt.date.fromisoformat(today)


def test_appending_explicit_session_across_midnight_reuses_existing_file(isolated_vault):
    """A session that started yesterday keeps appending to yesterday's journal
    instead of splitting into a new file (and event_date is not recomputed)."""
    yesterday = (dt.date.fromisoformat(_today()) - dt.timedelta(days=1)).isoformat()
    existing = isolated_vault.sessions / f"{yesterday}-compact-controls-demo.{HOST}.md"
    existing.write_text(
        session_log._serialize_frontmatter(
            {
                "tags": ["memory", "session"],
                "project": "demo",
                "session": "compact-controls",
                "event_date": yesterday,
                "recorded_date": yesterday,
                "updated": f"{yesterday}T23:55:00+09:00",
                "status": "active",
                "checkpoints": 1,
            }
        )
        + "\n# Manual session — demo (compact-controls)\n\n## Checkpoints\n\n### 23:55:00\n\nfirst note\n",
        encoding="utf-8",
    )

    assert session_log.main(
        ["log", "--project", "demo", "--session", "compact-controls", "--note", "second note"]
    ) == 0

    assert sorted(p.name for p in isolated_vault.sessions.glob("*.md")) == [existing.name]
    fm, body = session_log.parse_frontmatter(existing.read_text(encoding="utf-8"))
    assert fm["event_date"] == yesterday  # not recomputed to today
    assert fm["checkpoints"] == "2"
    assert "second note" in body


def test_derive_updated_parses_event_date_after_fix(isolated_vault):
    """Regression: before the fix, ``event_date`` held the session id
    (e.g. "compact-controls"), which ``dt.datetime.fromisoformat`` cannot parse,
    so ``_derive_updated`` always returned None for explicit-session journals."""
    today = _today()
    assert session_log.main(
        ["log", "--project", "demo", "--session", "compact-controls", "--note", "n"]
    ) == 0
    fm, _ = session_log.parse_frontmatter(
        (isolated_vault.sessions / _name("compact-controls")).read_text(encoding="utf-8")
    )
    derived = session_log._derive_updated(fm["event_date"], "23:59:59", fm["updated"])
    assert derived is not None
    assert derived.startswith(today)


def test_status_defaults_to_active_and_done_closes_the_session(isolated_vault):
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "a"])
    assert _journal(isolated_vault)[0]["status"] == "active"

    session_log.main(
        ["log", "--project", "demo", "--session", "s1", "--note", "b", "--status", "done"]
    )
    fm, _ = _journal(isolated_vault)
    assert fm["status"] == "done"
    assert fm["checkpoints"] == "2"  # closing the session is itself a checkpoint


def test_status_is_sticky_once_set(isolated_vault):
    """A later call without --status keeps `done` — the session does not reopen."""
    session_log.main(
        ["log", "--project", "demo", "--session", "s1", "--note", "a", "--status", "done"]
    )
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "b"])
    assert _journal(isolated_vault)[0]["status"] == "done"


def _set_updated(vault, name: str, when: dt.datetime) -> None:
    """Rewrite a journal's ``updated`` field, going through parse/serialize
    rather than raw string replacement so the frontmatter stays well-formed."""
    path = vault.sessions / name
    fm, body = session_log.parse_frontmatter(path.read_text(encoding="utf-8"))
    fm["updated"] = when.isoformat(timespec="seconds")
    path.write_text(session_log._serialize_frontmatter(fm) + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Stale-session sweep
# ---------------------------------------------------------------------------


def test_sweep_leaves_journal_active_exactly_at_threshold(isolated_vault):
    # Pins a fixed `now` and passes it explicitly to sweep_stale_journals rather
    # than going through the CLI (which calls _now_local() itself) — otherwise
    # the real time elapsed between setting `updated` and the sweep running
    # would drift past the boundary this test means to pin exactly.
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "a"])
    # `updated` round-trips through isoformat(timespec="seconds"), so `now` must
    # be truncated to whole seconds too — otherwise its microseconds would make
    # the stored `updated` land slightly earlier than intended, pushing elapsed
    # just past the boundary this test means to pin exactly.
    now = session_log._now_local().replace(microsecond=0)
    _set_updated(isolated_vault, _name(), now - dt.timedelta(days=session_log.STALE_AFTER_DAYS))

    changed = session_log.sweep_stale_journals(now, apply=True)
    assert changed == []
    assert _journal(isolated_vault)[0]["status"] == "active"


def test_sweep_marks_journal_stale_just_past_threshold(isolated_vault):
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "a"])
    now = session_log._now_local().replace(microsecond=0)
    _set_updated(
        isolated_vault,
        _name(),
        now - dt.timedelta(days=session_log.STALE_AFTER_DAYS, seconds=1),
    )
    before_body = _journal(isolated_vault)[1]

    changed = session_log.sweep_stale_journals(now, apply=True)
    assert changed == [isolated_vault.sessions / _name()]
    fm, body = _journal(isolated_vault)
    assert fm["status"] == "stale"
    assert body == before_body  # body byte-identical, only frontmatter changed


def test_sweep_does_not_touch_a_just_checkpointed_journal(isolated_vault):
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "x"])

    assert session_log.main(["sweep", "--apply"]) == 0
    assert _journal(isolated_vault)[0]["status"] == "active"


def test_sweep_never_reopens_or_overwrites_a_done_session(isolated_vault):
    session_log.main(
        ["log", "--project", "demo", "--session", "s1", "--note", "a", "--status", "done"]
    )
    now = session_log._now_local()
    _set_updated(isolated_vault, _name(), now - dt.timedelta(days=70))

    assert session_log.main(["sweep", "--apply"]) == 0
    assert _journal(isolated_vault)[0]["status"] == "done"


def test_sweep_dry_run_reports_but_does_not_write(isolated_vault):
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "a"])
    now = session_log._now_local()
    _set_updated(isolated_vault, _name(), now - dt.timedelta(days=70))

    assert session_log.main(["sweep"]) == 0
    assert _journal(isolated_vault)[0]["status"] == "active"


def test_project_name_is_slugified_into_the_filename(isolated_vault):
    session_log.main(["log", "--project", "Agent Commander", "--session", "s1", "--note", "n"])
    assert (isolated_vault.sessions / _name(project="agent-commander")).exists()


def test_new_journal_gets_title_intro_and_canonical_frontmatter(isolated_vault):
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "n"])
    raw = (isolated_vault.sessions / _name()).read_text(encoding="utf-8")
    fm, body = session_log.parse_frontmatter(raw)

    assert raw.startswith("---\ntags:\n  - memory\n  - session\n")
    assert fm["project"] == "demo"
    assert fm["session"] == "s1"
    assert fm["event_date"] == _today()  # event_date is today's date at creation, not the session id
    assert fm["checkpoints"] == "1"
    assert "# Manual session — demo (s1)" in body
    assert "## Checkpoints" in body


def test_event_date_is_pinned_at_creation_and_recorded_date_tracks_now(isolated_vault):
    session_log.main(["log", "--project", "demo", "--session", "2020-01-01", "--note", "a"])
    session_log.main(["log", "--project", "demo", "--session", "2020-01-01", "--note", "b"])
    today = _today()
    fm, _ = _journal(isolated_vault, _name("2020-01-01"))
    # the session id looks like a date, but it is not the event_date source
    assert fm["event_date"] == today
    assert fm["recorded_date"] == today


def test_task_links_are_wrapped_deduped_and_survive_a_round_trip(isolated_vault):
    session_log.main(
        [
            "log", "--project", "demo", "--session", "s1", "--note", "a",
            "--task", "foo", "--task", "[[bar]]",
        ]
    )
    assert _journal(isolated_vault)[0]["related_tasks"] == ["[[Todo/items/foo]]", "[[bar]]"]

    # A repeat of the same task on a later checkpoint does not duplicate the link.
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "b", "--task", "foo"])
    assert _journal(isolated_vault)[0]["related_tasks"] == ["[[Todo/items/foo]]", "[[bar]]"]


# ---------------------------------------------------------------------------
# The frontmatter round-trip is a fixed point
#
# These five tests used to encode the opposite: the journal destroyed its own
# frontmatter on the third checkpoint, because parse_frontmatter consumes the
# "\n" after the closing delimiter while _serialize_frontmatter did not emit one.
# Each round-trip ate a leading body newline until "---" fused with the title,
# after which every call prepended a fresh block (the "------" seam) and restarted
# the counter at 1. They are rewritten here against the fixed behaviour; the
# `# was:` comments carry the old names so the diff and a grep both find them.
# ---------------------------------------------------------------------------


# was: test_third_checkpoint_collides_the_closing_delimiter_and_kills_the_frontmatter
def test_frontmatter_survives_repeated_checkpoints_as_a_single_block(isolated_vault):
    r"""Five checkpoints leave exactly one parseable frontmatter block.

    The third checkpoint is where the old bug fused the closing ``---`` into the
    title, so the loop deliberately runs past it.
    """
    for i in range(5):
        session_log.main(["log", "--project", "demo", "--session", "s1", "--note", f"note-{i}"])

    raw = (isolated_vault.sessions / _name()).read_text(encoding="utf-8")
    assert "---# Manual session — demo (s1)" not in raw  # no fusion
    assert raw.count("\n---\n") == 1  # exactly one closing delimiter
    assert "\n---\n\n# Manual session — demo (s1)" in raw  # blank line intact

    fm, body = session_log.parse_frontmatter(raw)
    assert fm != {}  # readable back, unlike before
    assert fm["checkpoints"] == "5"
    assert body.count("### ") == 5
    for i in range(5):
        assert f"note-{i}" in body


# was: test_after_corruption_every_call_stacks_another_frontmatter_block
def test_repeated_checkpoints_never_stack_a_second_frontmatter_block(isolated_vault):
    """The counter climbs 1→5 in place instead of restarting in a new block."""
    seen = []
    for i in range(5):
        session_log.main(["log", "--project", "demo", "--session", "s1", "--note", f"note-{i}"])
        seen.append(session_log.parse_frontmatter(
            (isolated_vault.sessions / _name()).read_text(encoding="utf-8")
        )[0]["checkpoints"])

    assert seen == ["1", "2", "3", "4", "5"]

    raw = (isolated_vault.sessions / _name()).read_text(encoding="utf-8")
    assert "\n------\n" not in raw  # the stacking seam never appears
    assert raw.count("\ncheckpoints: ") == 1  # one and only one counter on disk
    assert raw.count("# Manual session — demo (s1)") == 1
    assert raw.count("## Checkpoints") == 1


# was: test_status_done_stops_sticking_once_the_frontmatter_is_corrupt
def test_status_done_sticks_on_a_late_checkpoint_and_list_reads_it_back(isolated_vault, capsys):
    """AC#3: ``--status done`` closes the session on *any* checkpoint, including
    ones past the third — and ``list`` renders it instead of ``?``."""
    for i in range(3):
        session_log.main(["log", "--project", "demo", "--session", "s1", "--note", f"note-{i}"])
    session_log.main(
        ["log", "--project", "demo", "--session", "s1", "--note", "closing", "--status", "done"]
    )
    capsys.readouterr()

    raw = (isolated_vault.sessions / _name()).read_text(encoding="utf-8")
    fm, _ = session_log.parse_frontmatter(raw)
    assert fm["status"] == "done"
    assert fm["checkpoints"] == "4"

    assert session_log.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "status=done" in listed
    assert "checkpoints=4" in listed
    assert "=?" not in listed


# was: test_git_delta_regresses_to_a_first_checkpoint_after_corruption
def test_git_delta_stays_incremental_across_many_checkpoints(isolated_vault, tmp_path):
    """``last_head`` survives every round-trip, so the module's headline feature —
    "only the commits since last time" — keeps working past the third checkpoint
    instead of reverting to dumping recent history."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "one")
    for _ in range(3):
        session_log.main(["log", "--project", "demo", "--session", "s1", "--repo-dir", str(repo)])

    head2 = _commit(repo, "b.txt", "two")
    session_log.main(["log", "--project", "demo", "--session", "s1", "--repo-dir", str(repo)])

    raw = (isolated_vault.sessions / _name()).read_text(encoding="utf-8")
    assert raw.count("session start; recent commits") == 1  # only checkpoint 1 is a "first"
    assert "1 new commit(s) since last checkpoint:" in raw
    assert raw.count("no new commits since last checkpoint.") == 2  # checkpoints 2 and 3

    fm, _ = session_log.parse_frontmatter(raw)
    assert fm["last_head"] == head2
    assert fm["checkpoints"] == "4"


# was: test_related_tasks_are_lost_from_the_readable_frontmatter_after_corruption
def test_related_tasks_stay_in_the_parsed_frontmatter_across_checkpoints(isolated_vault):
    """The task backlinks stay visible to every consumer, not just on disk."""
    for i in range(3):
        session_log.main(
            ["log", "--project", "demo", "--session", "s1", "--note", f"note-{i}", "--task", "foo"]
        )
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "later"])

    raw = (isolated_vault.sessions / _name()).read_text(encoding="utf-8")
    assert "[[Todo/items/foo]]" in raw
    fm, _ = session_log.parse_frontmatter(raw)
    assert fm["related_tasks"] == ["[[Todo/items/foo]]"]  # carried, not duplicated


# ---------------------------------------------------------------------------
# Refusing to append to an already-damaged journal
# ---------------------------------------------------------------------------

# The shape older versions left on disk: newest block first, "------" seams
# between blocks, and a final delimiter fused into the title line.
STACKED_JOURNAL = """\
---
tags:
  - memory
  - session
project: demo
session: s1
checkpoints: 1
------
tags:
  - memory
  - session
project: demo
session: s1
last_head: 1111111111111111111111111111111111111111
checkpoints: 3
---# Manual session — demo (s1)

## Checkpoints

### 09:00:00 KST

old note
"""


def test_a_damaged_journal_is_refused_rather_than_stacked_onto(isolated_vault, capsys):
    """Fixing the round-trip does not heal existing files: ``parse_frontmatter``
    still returns ``({}, whole file)`` for them, so an unguarded ``cmd_log``
    would pile another block on top. It must bail without writing."""
    journal = isolated_vault.sessions / _name()
    journal.write_text(STACKED_JOURNAL, encoding="utf-8")

    assert session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "new"]) == 2

    out = capsys.readouterr().out
    assert "has unreadable frontmatter" in out
    assert "session_log repair --apply" in out
    assert journal.read_text(encoding="utf-8") == STACKED_JOURNAL  # byte-identical
    assert "new" not in journal.read_text(encoding="utf-8").split("## Checkpoints")[1]


def test_the_guard_does_not_fire_on_a_journal_without_frontmatter(isolated_vault):
    """A hand-written note that never had frontmatter is not damage — it opens
    with prose, not a delimiter, so the journal is appended to as usual."""
    journal = isolated_vault.sessions / _name()
    journal.write_text("# hand-written\n\nsome prose\n", encoding="utf-8")

    assert session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "new"]) == 0

    fm, body = _journal(isolated_vault)
    assert fm["checkpoints"] == "1"
    assert "some prose" in body  # the original content is kept
    assert "new" in body

    # …and the frontmatter it just grew round-trips like any other journal.
    assert session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "again"]) == 0
    assert _journal(isolated_vault)[0]["checkpoints"] == "2"


# ---------------------------------------------------------------------------
# Argument contract
# ---------------------------------------------------------------------------


def test_log_requires_a_note_or_a_repo_dir(isolated_vault, capsys):
    assert session_log.main(["log", "--project", "demo", "--session", "s1"]) == 2
    assert "nothing to record" in capsys.readouterr().out
    assert list(isolated_vault.sessions.glob("*.md")) == []


def test_push_without_commit_is_rejected_before_anything_is_written(isolated_vault, capsys):
    assert (
        session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "n", "--push"])
        == 2
    )
    assert "--push requires --commit" in capsys.readouterr().out
    assert list(isolated_vault.sessions.glob("*.md")) == []


def test_argparse_rejects_a_missing_subcommand_or_project(isolated_vault):
    with pytest.raises(SystemExit):
        session_log.main([])
    with pytest.raises(SystemExit):
        session_log.main(["log", "--note", "n"])
    assert list(isolated_vault.sessions.glob("*.md")) == []


# ---------------------------------------------------------------------------
# capture_git
# ---------------------------------------------------------------------------


def test_capture_git_first_checkpoint_shows_recent_history(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    heads = [_commit(repo, f"f{i}.txt", f"commit {i}") for i in range(3)]

    got = session_log.capture_git(repo, "HEAD", None)

    assert got["first"] is True
    assert got["moved"] is True  # `None != head`, so the first checkpoint reads as moved
    assert got["branch"] == "main"
    assert got["dirty"] is False
    assert got["head"] == heads[-1]
    assert got["head"].startswith(got["short"])
    assert _subjects(got["commits"]) == ["commit 2", "commit 1", "commit 0"]  # newest first


def test_capture_git_reports_only_commits_since_the_last_checkpoint(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _commit(repo, "a.txt", "base")
    _commit(repo, "b.txt", "second")
    _commit(repo, "c.txt", "third")

    got = session_log.capture_git(repo, "HEAD", base)

    assert got["first"] is False
    assert got["moved"] is True
    assert _subjects(got["commits"]) == ["third", "second"]
    assert "base" not in _subjects(got["commits"])


def test_capture_git_reports_no_movement(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    head = _commit(repo, "a.txt", "base")

    got = session_log.capture_git(repo, "HEAD", head)

    assert got["moved"] is False
    assert got["first"] is False
    assert got["commits"] == []


def test_capture_git_flags_a_dirty_worktree(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    head = _commit(repo, "a.txt", "base")
    assert session_log.capture_git(repo, "HEAD", head)["dirty"] is False

    (repo / "untracked.txt").write_text("wip", encoding="utf-8")
    assert session_log.capture_git(repo, "HEAD", head)["dirty"] is True


def test_capture_git_labels_an_explicit_ref_rather_than_the_checked_out_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "base")
    _run_git(repo, "branch", "release")

    got = session_log.capture_git(repo, "release", None)

    assert got["branch"] == "release"
    assert _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # never left main


def test_capture_git_returns_none_for_a_repo_with_no_commits(tmp_path):
    repo = _init_repo(tmp_path / "empty")  # initialised, never committed
    assert session_log.capture_git(repo, "HEAD", None) is None


def test_capture_git_returns_none_outside_a_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    if _inside_git_repo(plain):
        pytest.skip("tmp_path is itself inside a git repo; the not-a-repo path cannot be reached")
    assert session_log.capture_git(plain, "HEAD", None) is None


# ---------------------------------------------------------------------------
# cmd_log + git capture
# ---------------------------------------------------------------------------


def test_repo_dir_capture_is_recorded_and_advances_between_checkpoints(isolated_vault, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    head1 = _commit(repo, "a.txt", "first subject")

    assert (
        session_log.main(
            ["log", "--project", "demo", "--session", "s1", "--repo-dir", str(repo)]
        )
        == 0
    )
    fm, body = _journal(isolated_vault)
    assert fm["last_head"] == head1
    assert fm["repo"] == str(repo.resolve())
    assert "ref" not in fm  # default HEAD is not written out
    assert "session start; recent commits (1):" in body
    assert "first subject" in body
    assert "_(working tree: clean)_" in body

    head2 = _commit(repo, "b.txt", "second subject")
    session_log.main(["log", "--project", "demo", "--session", "s1", "--repo-dir", str(repo)])
    fm, body = _journal(isolated_vault)
    assert fm["last_head"] == head2
    assert "1 new commit(s) since last checkpoint:" in body

    # Third checkpoint, nothing new on the repo. This is where the frontmatter
    # used to stop parsing, so it is asserted through parse_frontmatter now.
    session_log.main(["log", "--project", "demo", "--session", "s1", "--repo-dir", str(repo)])
    fm, body = _journal(isolated_vault)
    assert "no new commits since last checkpoint." in body
    assert fm["checkpoints"] == "3"
    assert fm["last_head"] == head2  # unchanged, and still readable


def test_explicit_ref_is_recorded_in_frontmatter(isolated_vault, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "base")
    _run_git(repo, "branch", "release")

    session_log.main(
        ["log", "--project", "demo", "--session", "s1", "--repo-dir", str(repo), "--ref", "release"]
    )
    assert _journal(isolated_vault)[0]["ref"] == "release"


def test_non_git_repo_dir_warns_and_records_the_note_only(isolated_vault, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    if _inside_git_repo(plain):
        pytest.skip("tmp_path is itself inside a git repo; the not-a-repo path cannot be reached")

    assert (
        session_log.main(
            ["log", "--project", "demo", "--session", "s1", "--repo-dir", str(plain), "--note", "manual"]
        )
        == 0
    )
    assert "is not a git repo; recording note only" in capsys.readouterr().out

    fm, body = _journal(isolated_vault)
    assert "last_head" not in fm
    assert fm["repo"] == str(plain.resolve())  # the repo field is written even so
    assert "manual" in body
    assert "**Git**" not in body


# ---------------------------------------------------------------------------
# The real commit path (AC#3, retargeted from graph_memory by the plan)
# ---------------------------------------------------------------------------


def test_commit_stages_only_the_session_journal(isolated_vault, tmp_path, capsys):
    # Guard before anything can run `git add`: we must be pointed at the tmp vault.
    assert session_log.ROOT == isolated_vault.root
    assert session_log.ROOT.is_relative_to(tmp_path)

    _init_repo(isolated_vault.root)
    decoy = isolated_vault.write("Notes/someone-elses-work.md", "do not commit me\n")

    assert (
        session_log.main(
            ["log", "--project", "demo", "--session", "s1", "--note", "n", "--commit"]
        )
        == 0
    )

    assert (
        _run_git(isolated_vault.root, "log", "-1", "--format=%s")
        == "chore(session): demo manual-work checkpoint"
    )
    assert _run_git(isolated_vault.root, "show", "--name-only", "--format=").split() == [
        f"Memory/sessions/{_name()}"
    ]
    # The unrelated file was in the same tree and stayed out of the commit.
    assert "?? Notes/someone-elses-work.md" in _run_git(
        isolated_vault.root, "status", "--porcelain", "-uall"
    )
    assert decoy.exists()
    assert "session-log: committed (chore(session): demo manual-work checkpoint)" in capsys.readouterr().out


def test_commit_message_carries_the_project_name(isolated_vault):
    assert session_log.ROOT == isolated_vault.root
    _init_repo(isolated_vault.root)

    session_log.main(
        ["log", "--project", "Agent Commander", "--session", "s1", "--note", "n", "--commit"]
    )
    # Note the message uses the raw --project value, not the slug used for the filename.
    assert (
        _run_git(isolated_vault.root, "log", "-1", "--format=%s")
        == "chore(session): Agent Commander manual-work checkpoint"
    )
    assert (isolated_vault.sessions / _name(project="agent-commander")).exists()


def test_committing_with_nothing_staged_bails_without_a_new_commit(isolated_vault, tmp_path, capsys):
    assert session_log.ROOT == isolated_vault.root
    assert session_log.ROOT.is_relative_to(tmp_path)
    _init_repo(isolated_vault.root)

    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "n", "--commit"])
    first = _run_git(isolated_vault.root, "rev-parse", "HEAD")
    capsys.readouterr()

    # Re-run the commit helper without rewriting the journal: nothing to stage.
    session_log._commit_and_maybe_push(isolated_vault.sessions / _name(), "demo", push=False)

    assert "session-log: nothing staged to commit" in capsys.readouterr().out
    assert _run_git(isolated_vault.root, "rev-parse", "HEAD") == first


def test_commit_failure_is_reported_but_does_not_fail_the_command(isolated_vault, tmp_path, capsys):
    """The journal is written even when the vault is not a repo — the commit is
    best-effort and ``cmd_log`` still returns 0. Frozen as-is: a caller checking
    only the exit code cannot tell that nothing was committed."""
    assert session_log.ROOT == isolated_vault.root
    if _inside_git_repo(isolated_vault.root):
        pytest.skip("tmp_path is itself inside a git repo; the not-a-repo path cannot be reached")

    assert (
        session_log.main(
            ["log", "--project", "demo", "--session", "s1", "--note", "n", "--commit"]
        )
        == 0
    )
    assert "session-log: git add failed (is the vault a git repo?)" in capsys.readouterr().out
    assert (isolated_vault.sessions / _name()).exists()


def test_push_failure_leaves_the_commit_in_place(isolated_vault, tmp_path, capsys):
    assert session_log.ROOT == isolated_vault.root
    assert session_log.ROOT.is_relative_to(tmp_path)
    # No remote is configured, so `git push` fails locally without touching a network.
    _init_repo(isolated_vault.root)

    assert (
        session_log.main(
            ["log", "--project", "demo", "--session", "s1", "--note", "n", "--commit", "--push"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "session-log: committed" in out
    assert "session-log: git push failed (check remote/credentials)" in out
    assert (
        _run_git(isolated_vault.root, "log", "-1", "--format=%s")
        == "chore(session): demo manual-work checkpoint"
    )


def test_commit_is_opt_in(isolated_vault):
    """Without --commit the recorder performs no git writes at all."""
    assert session_log.ROOT == isolated_vault.root
    _init_repo(isolated_vault.root)

    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "n"])

    assert _run_git(isolated_vault.root, "status", "--porcelain") != ""
    assert (
        subprocess.run(
            ["git", "-C", str(isolated_vault.root), "rev-parse", "HEAD"],
            capture_output=True,
        ).returncode
        != 0
    )  # still no commits


# ---------------------------------------------------------------------------
# list / show
# ---------------------------------------------------------------------------


def test_list_shows_newest_first_and_show_prints_the_latest_journal(isolated_vault, capsys):
    session_log.main(["log", "--project", "demo", "--session", "2026-01-01", "--note", "older"])
    session_log.main(["log", "--project", "demo", "--session", "2026-02-01", "--note", "newer"])
    capsys.readouterr()

    assert session_log.main(["list"]) == 0
    out = capsys.readouterr().out
    assert out.index(_name("2026-02-01")) < out.index(_name("2026-01-01"))
    assert "status=active" in out
    assert "checkpoints=1" in out

    assert session_log.main(["show", "demo"]) == 0
    shown = capsys.readouterr().out
    assert "newer" in shown and "older" not in shown

    assert session_log.main(["show", "nope"]) == 1
    assert "no session journal for project 'nope'" in capsys.readouterr().out


def test_list_and_show_handle_an_absent_sessions_directory(isolated_vault, capsys):
    isolated_vault.sessions.rmdir()

    assert session_log.main(["list"]) == 0
    assert "(no sessions recorded yet)" in capsys.readouterr().out
    assert session_log.main(["show", "demo"]) == 1
    assert "(no sessions recorded yet)" in capsys.readouterr().out


def test_list_handles_an_empty_sessions_directory(isolated_vault, capsys):
    assert session_log.main(["list"]) == 0
    assert "(no sessions recorded yet)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# `repair`: unstacking journals the old round-trip already damaged
#
# The fixture is not synthetic. It is the real
# ``Memory/sessions/2026-07-20-agent-commander.md`` as it stood before a human
# hand-repaired it in 9112e752, copied in byte-for-byte:
#   git show 9112e752^:Memory/sessions/2026-07-20-agent-commander.md
# 18 stacked blocks, 17 "------" seams, a closing delimiter fused into the title
# on line 272, and 20 checkpoint headings (17 blocks reading `checkpoints: 1`
# plus a final `checkpoints: 3` — the last good count before the damage began).
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORRUPT_JOURNAL = FIXTURES / "corrupt-session-journal.md"


def _install_fixture(vault, name: str = "2026-07-20-agent-commander.md") -> Path:
    """Copy the damaged journal into the tmp vault. Asserts the redirection
    first — an unpatched run would repair the developer's real vault."""
    assert vault.sessions.is_relative_to(vault.root)
    assert session_log.SESSIONS_DIR == vault.sessions
    path = vault.sessions / name
    path.write_text(CORRUPT_JOURNAL.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_the_corrupt_fixture_still_has_the_damage_it_is_supposed_to_have():
    """Negative control. If someone re-exports the fixture from the *repaired*
    file, every test below would pass vacuously against healthy input."""
    raw = CORRUPT_JOURNAL.read_text(encoding="utf-8")
    lines = raw.split("\n")
    assert lines[0] == "---"
    assert sum(1 for ln in lines if ln == "-" * 6) == 17  # stacking seams
    assert sum(1 for ln in lines if ln.startswith("checkpoints:")) == 18
    assert lines[271].startswith("---# Manual session — agent-commander")
    assert len(session_log._CHECKPOINT_HEADING.findall(raw)) == 20
    assert session_log.parse_frontmatter(raw)[0] == {}  # unreadable, as shipped


def test_repair_unstacks_the_real_18_block_journal(isolated_vault):
    """AC#5: the whole point — a damaged journal becomes readable again without
    losing a single checkpoint."""
    path = _install_fixture(isolated_vault)
    before = path.read_text(encoding="utf-8")

    assert session_log.main(["repair", str(path), "--apply"]) == 0

    raw = path.read_text(encoding="utf-8")
    assert "\n------\n" not in raw  # every seam is gone
    assert "---# Manual session" not in raw  # the fusion is undone
    assert raw.count("\n---\n") == 1  # exactly one frontmatter block

    fm, body = session_log.parse_frontmatter(raw)
    assert fm != {}
    assert fm["checkpoints"] == "20"  # 17 restarted counters + the last good 3
    assert fm["last_head"] == "8df9064a85bc12097fdd31655ed22ba4fc6e81ce"
    assert fm["project"] == "agent-commander"
    assert fm["event_date"] == "2026-07-20"  # from the oldest block
    assert fm["updated"] == "2026-07-20T18:14:46+09:00"  # from the newest
    assert fm["related_tasks"] == ["[[Todo/items/agent-commander-game-mode]]"]

    # Not one checkpoint lost, and the prose is carried over untouched.
    assert len(session_log._CHECKPOINT_HEADING.findall(body)) == 20
    assert "\n".join(before.split("\n")[272:]) in raw


def test_repair_is_idempotent(isolated_vault):
    """A repaired journal is a healthy journal, so a second pass is a no-op —
    it must not strip a newline or re-merge anything."""
    path = _install_fixture(isolated_vault)
    session_log.main(["repair", str(path), "--apply"])
    once = path.read_text(encoding="utf-8")

    assert session_log.main(["repair", str(path), "--apply"]) == 0
    assert path.read_text(encoding="utf-8") == once
    assert session_log.repair_journal(once) is None


def test_a_repaired_journal_accepts_checkpoints_again(isolated_vault, tmp_path):
    """End to end: repair clears the cmd_log guard, the counter resumes from 20
    instead of restarting, and the recovered last_head makes the next capture a
    real delta rather than another "session start". Installed under this host's
    name: unsuffixed (legacy) journals are read-only and never appended to."""
    name = f"2026-07-20-agent-commander.{HOST}.md"
    path = _install_fixture(isolated_vault, name)
    session_log.main(["repair", str(path), "--apply"])

    assert session_log.main(
        ["log", "--project", "agent-commander", "--session", "2026-07-20", "--note", "resumed"]
    ) == 0

    fm, body = _journal(isolated_vault, name)
    assert fm["checkpoints"] == "21"
    assert fm["last_head"] == "8df9064a85bc12097fdd31655ed22ba4fc6e81ce"  # carried
    assert body.count("## Checkpoints") == 1
    assert "resumed" in body
    assert path.read_text(encoding="utf-8").count("\n---\n") == 1


def test_repair_defaults_to_dry_run_and_writes_nothing(isolated_vault, capsys):
    """AC#6: no write without --apply."""
    path = _install_fixture(isolated_vault)
    before = path.read_text(encoding="utf-8")

    assert session_log.main(["repair", str(path)]) == 0

    assert path.read_text(encoding="utf-8") == before  # byte-identical
    out = capsys.readouterr().out
    assert "would repair" in out
    assert "18 blocks → 1" in out
    assert "checkpoints 20" in out
    assert "re-run with --apply" in out
    assert "repaired:" not in out


def test_explicit_dry_run_flag_is_accepted_and_conflicts_with_apply(isolated_vault, capsys):
    path = _install_fixture(isolated_vault)
    before = path.read_text(encoding="utf-8")

    assert session_log.main(["repair", str(path), "--dry-run"]) == 0
    assert path.read_text(encoding="utf-8") == before
    assert "would repair" in capsys.readouterr().out

    # Contradictory flags must not silently write.
    assert session_log.main(["repair", str(path), "--dry-run", "--apply"]) == 2
    assert path.read_text(encoding="utf-8") == before
    assert "mutually exclusive" in capsys.readouterr().out


def test_repair_leaves_healthy_journals_alone(isolated_vault, capsys):
    """The state of the real vault today: already repaired. "nothing to repair"
    is the correct output, not a failure."""
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "one"])
    session_log.main(["log", "--project", "demo", "--session", "s1", "--note", "two"])
    healthy = (isolated_vault.sessions / _name()).read_text(encoding="utf-8")
    capsys.readouterr()

    assert session_log.main(["repair", "--apply"]) == 0  # no paths → scans the dir

    assert (isolated_vault.sessions / _name()).read_text(encoding="utf-8") == healthy
    assert "nothing to repair" in capsys.readouterr().out


def test_repair_without_paths_scans_the_sessions_directory(isolated_vault, capsys):
    _install_fixture(isolated_vault, "2026-07-20-agent-commander.md")
    _install_fixture(isolated_vault, "2026-07-21-other-project.md")

    assert session_log.main(["repair"]) == 0

    out = capsys.readouterr().out
    assert out.count("would repair:") == 2
    assert "2 file(s) would change" in out


def test_repair_reports_a_missing_path_instead_of_scanning(isolated_vault, capsys):
    assert session_log.main(["repair", str(isolated_vault.sessions / "nope.md")]) == 2
    assert "does not exist" in capsys.readouterr().out


def test_repair_recounts_checkpoints_from_the_headings_and_warns_on_mismatch(
    isolated_vault, capsys
):
    """The stacked counters are not trustworthy, so the headings win. When the
    two disagree the discrepancy is surfaced rather than silently resolved."""
    path = isolated_vault.sessions / "s1-demo.md"
    path.write_text(
        STACKED_JOURNAL.replace("checkpoints: 3", "checkpoints: 9"), encoding="utf-8"
    )

    assert session_log.main(["repair", str(path), "--apply"]) == 0

    out = capsys.readouterr().out
    assert "block counters sum to 10" in out  # 1 + 9 claimed…
    assert "1 checkpoint headings are on disk" in out  # …but only one is real
    fm, _ = session_log.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["checkpoints"] == "1"


def test_repair_does_not_resurrect_keys_the_newest_block_dropped(isolated_vault, capsys):
    """``ref`` is per-invocation: the fixture's oldest block has
    ``ref: origin/main`` but the newest 17 do not, and the checkpoint prose
    confirms the captures really did move to plain ``main``. Carrying the stale
    value forward would misdescribe the journal, so it is dropped and reported.
    """
    path = _install_fixture(isolated_vault)
    assert "ref: origin/main" in path.read_text(encoding="utf-8")

    session_log.main(["repair", str(path), "--apply"])

    fm, _ = session_log.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert "ref" not in fm
    assert "dropped ref" in capsys.readouterr().out


def test_repair_merges_a_done_status_stickily(isolated_vault):
    """A session closed in *any* block stays closed — losing `done` would quietly
    reopen a finished session."""
    path = isolated_vault.sessions / "s1-demo.md"
    path.write_text(
        STACKED_JOURNAL.replace("checkpoints: 3", "status: done\ncheckpoints: 3"),
        encoding="utf-8",
    )
    assert session_log.main(["repair", str(path), "--apply"]) == 0
    fm, _ = session_log.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["status"] == "done"


def test_repair_ignores_horizontal_rules_in_the_body(isolated_vault):
    """The block scanner must stop at the end of the head area. A "---" rule in
    the prose is content, not a delimiter."""
    path = isolated_vault.sessions / "s1-demo.md"
    path.write_text(STACKED_JOURNAL + "\nmid-body rule:\n\n---\n\nafter the rule\n", encoding="utf-8")

    assert session_log.main(["repair", str(path), "--apply"]) == 0

    raw = path.read_text(encoding="utf-8")
    _, body = session_log.parse_frontmatter(raw)
    assert "after the rule" in body
    assert "\n---\n" in body  # the rule itself survives, still in the body
    assert raw.count("\n------\n") == 0


def test_repair_journal_returns_none_for_input_it_cannot_understand():
    assert session_log.repair_journal("no frontmatter here\n") is None
    assert session_log.repair_journal("---\ntags:\n  - memory\n") is None  # unterminated
    assert session_log.repair_journal("---\nproject: x\n---\n\n# T\n") is None  # healthy


# ---------------------------------------------------------------------------
# `repair`: single-block journals whose counter stalled *before* the stacking
#
# A second, older failure mode. These files never stacked — one frontmatter
# block, a healthy bare "---" close, so ``parse_frontmatter`` reads them fine —
# but the counter froze at 3 while the appends kept adding headings. Shape and
# numbers are the real ``Memory/sessions/2026-07-20-parent-match-app.md``:
# 5 headings on disk under ``checkpoints: 3``, last checkpoint at 08:49:36
# while ``updated`` still says 08:03:40.
# ---------------------------------------------------------------------------

STALE_COUNTER_JOURNAL = """\
---
tags:
  - memory
  - session
project: demo
session: 2026-07-20
event_date: 2026-07-20
recorded_date: 2026-07-20
updated: 2026-07-20T08:03:40+09:00
status: active
repo: /home/user/Worker/Demo
last_head: fe805254c9816622d0c2f10cfe4462ea9430c24a
checkpoints: 3
related_tasks:
  - "[[Todo/items/demo]]"
---

# Manual session — demo (2026-07-20)

## Checkpoints

### 07:31:05 KST

one

### 07:52:18 KST

two

### 08:03:40 KST

three

### 08:26:11 KST

four — the counter stopped moving here

### 08:49:36 KST

five
"""


def test_the_stale_counter_fixture_is_the_shape_the_bug_needs():
    """Negative control for the diagnosis: one block, *not* fused, so the
    stacked-block repair path has nothing to merge — and yet the file is wrong.
    If this ever reports 2+ blocks the tests below stop testing the reported bug.
    """
    blocks, _, fused = session_log._split_stacked_frontmatter(STALE_COUNTER_JOURNAL)
    assert (len(blocks), fused) == (1, False)
    fm, body = session_log.parse_frontmatter(STALE_COUNTER_JOURNAL)
    assert fm["checkpoints"] == "3"  # …what the frontmatter claims
    assert len(session_log._CHECKPOINT_HEADING.findall(body)) == 5  # …what is real


def test_repair_recounts_a_single_block_journal_whose_counter_stalled():
    """The reported bug. A journal with one healthy block still under-reports
    when its counter stalled, and repair must recount it from the headings —
    touching those two frontmatter lines and nothing else."""
    result = session_log.repair_journal(STALE_COUNTER_JOURNAL)
    assert result is not None  # today: None, i.e. "nothing to repair"

    new_text, stats = result
    fm, body = session_log.parse_frontmatter(new_text)
    assert fm["checkpoints"] == "5"
    # `updated` is derived from the last heading + event_date, reusing the
    # offset already in the file rather than the host's local timezone.
    assert fm["updated"] == "2026-07-20T08:49:36+09:00"
    # Not recoverable from the body (it only ever holds short shas), so left be.
    assert fm["last_head"] == "fe805254c9816622d0c2f10cfe4462ea9430c24a"
    assert stats["blocks"] == 1
    assert stats["claimed"] == 3
    assert stats["checkpoints"] == 5

    # The prose is carried over byte for byte: only the two counter lines move.
    before, after = STALE_COUNTER_JOURNAL.split("\n"), new_text.split("\n")
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert [b.split(":")[0] for _, b in changed] == ["updated", "checkpoints"]
    assert body == session_log.parse_frontmatter(STALE_COUNTER_JOURNAL)[1]

    # …and the output is itself healthy, so a second pass is a no-op.
    assert session_log.repair_journal(new_text) is None


def test_repair_leaves_a_single_block_journal_whose_count_matches():
    """Idempotence: recounting must only *report* a change when there is one,
    or every healthy journal in the vault would rewrite on each pass."""
    healthy = STALE_COUNTER_JOURNAL.replace("checkpoints: 3", "checkpoints: 5")
    assert session_log.repair_journal(healthy) is None


def test_repair_leaves_updated_alone_when_no_offset_can_be_read():
    """No `updated` on disk means no timezone to reuse. The count is still
    wrong and gets fixed; the timestamp is *not* guessed from the host clock."""
    no_updated = STALE_COUNTER_JOURNAL.replace(
        "updated: 2026-07-20T08:03:40+09:00\n", ""
    )
    new_text, _ = session_log.repair_journal(no_updated)
    fm, _ = session_log.parse_frontmatter(new_text)
    assert fm["checkpoints"] == "5"
    assert "updated" not in fm


def test_repair_never_rewinds_updated_to_an_earlier_time():
    """A session that ran past midnight would derive `event_date` + last heading
    into a time *before* the recorded one. Rewinding a timestamp is worse than
    leaving it, so the derived value is only taken when it moves forward."""
    ahead = STALE_COUNTER_JOURNAL.replace(
        "updated: 2026-07-20T08:03:40+09:00", "updated: 2026-07-21T02:15:00+09:00"
    )
    new_text, _ = session_log.repair_journal(ahead)
    fm, _ = session_log.parse_frontmatter(new_text)
    assert fm["checkpoints"] == "5"  # still recounted…
    assert fm["updated"] == "2026-07-21T02:15:00+09:00"  # …but not rewound


def test_repair_recounts_a_stalled_journal_end_to_end(isolated_vault, capsys):
    """Through the CLI: dry-run writes nothing, --apply fixes the two lines, and
    the single-block path does not print the misleading "1 blocks → 1"."""
    path = isolated_vault.sessions / "2026-07-20-demo.md"
    path.write_text(STALE_COUNTER_JOURNAL, encoding="utf-8")

    assert session_log.main(["repair", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == STALE_COUNTER_JOURNAL  # dry-run
    out = capsys.readouterr().out
    assert "would repair" in out
    assert "checkpoints 3 → 5" in out
    assert "blocks → 1" not in out  # there was only ever one block

    assert session_log.main(["repair", str(path), "--apply"]) == 0
    fm, _ = session_log.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["checkpoints"] == "5"
    assert "block counters sum to 3" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Frontmatter round-trip
# ---------------------------------------------------------------------------


def test_parse_frontmatter_returns_content_untouched_when_absent():
    assert session_log.parse_frontmatter("just body\n") == ({}, "just body\n")


def test_frontmatter_serializes_in_canonical_key_order_with_extras_last():
    fm = {"custom": "x", "checkpoints": 2, "tags": ["memory"], "project": "demo"}
    out = session_log._serialize_frontmatter(fm)
    assert out.splitlines() == [
        "---",
        "tags:",
        "  - memory",
        "project: demo",
        "checkpoints: 2",
        "custom: x",
        "---",
    ]


def test_frontmatter_quotes_wikilinks_and_colon_bearing_list_items():
    out = session_log._serialize_frontmatter({"related_tasks": ["[[a]]", "has: colon", "plain"]})
    assert '  - "[[a]]"' in out
    assert '  - "has: colon"' in out
    assert "  - plain" in out


# ---------------------------------------------------------------------------
# Bare-clone / stdlib-only contract (AC#4)
# ---------------------------------------------------------------------------

# `python3 -m todo_system.session_log` imports the package before the module, so
# __init__.py is part of the contract too.
STDLIB_ONLY_SOURCES = ("__init__.py", "session_log.py", "graph_memory.py")


def _import_roots(source: str) -> set[str]:
    """Root package of every import anywhere in the source.

    ``ast.walk`` rather than top-level nodes only: a function-local ``import
    yaml`` would still blow up at call time on a dependency-free clone.
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("filename", STDLIB_ONLY_SOURCES)
def test_bare_clone_contract_allows_only_stdlib_imports(filename):
    """CLAUDE.md §9: these must run under plain ``python3`` from a bare clone —
    no uv, no PyYAML, no project dependencies installed. This is the gate; a new
    third-party import anywhere in these files fails here.
    """
    source = (PACKAGE_DIR / filename).read_text(encoding="utf-8")
    third_party = _import_roots(source) - set(sys.stdlib_module_names) - {"todo_system"}
    assert third_party == set(), f"{filename} imports non-stdlib: {sorted(third_party)}"


def test_the_stdlib_check_is_not_vacuous():
    """Negative control: prove the discriminator above can actually reject."""
    assert "yaml" not in sys.stdlib_module_names
    assert "__future__" in sys.stdlib_module_names
    roots = _import_roots("import yaml\nfrom pathlib import Path\nfrom . import sibling\n")
    assert roots == {"yaml", "pathlib"}  # relative imports are correctly excluded
    assert roots - set(sys.stdlib_module_names) == {"yaml"}


def test_session_log_is_runnable_as_a_module():
    """Smoke leg only. CI installs pyyaml, so a green result here does NOT prove
    stdlib-only — the ast test above is the real gate. This catches the package
    becoming non-executable via ``-m`` (e.g. a syntax error or a bad __init__).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "todo_system.session_log", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "todo_system.session_log" in proc.stdout
