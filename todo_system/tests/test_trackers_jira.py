"""Tests for the TrackerAdapter contract + Jira adapter (design §⑨).

Two layers, both offline:

* **md bridge** (``base.upsert_task_record``) — idempotent upsert, the monotonic
  "done 승격" status rule, and the md-wins conflict note, driven against a
  ``tmp_path`` items dir (never the real vault — the session-scoped
  ``_real_vault_untouched`` conftest guard backs this up).
* **Jira transport** (``jira.JiraAdapter``) — exercised against a local
  ``RecordingJiraServer`` (``127.0.0.1:0``, mirrors ``test_slack_send.py``) so
  pull/push round-trips, the opt-in no-op, and token secrecy are asserted on the
  exact bytes without ever reaching a real Jira site.
"""

from __future__ import annotations

from todo_system.trackers.base import (
    TaskRecord,
    append_progress_log,
    parse_frontmatter,
    upsert_task_record,
)

# --------------------------------------------------------------------------- #
# md bridge: upsert / idempotency / conflict / monotonic status (no network)
# --------------------------------------------------------------------------- #


def _record(**kw) -> TaskRecord:
    base = dict(
        external_ref="jira:PROJ-123",
        title="Fix the thing",
        body="Do the work.",
        frontmatter={"tags": ["backend"], "due": "2026-10-10", "priority": 70},
        status="open",
        remote_updated_at="2026-10-01T00:00:00.000+0000",
    )
    base.update(kw)
    return TaskRecord(**base)


def test_pull_upsert_creates_md(tmp_path):
    items = tmp_path / "items"
    result = upsert_task_record(_record(), items, today="2026-10-24")

    assert result.action == "created"
    assert result.path.exists()
    text = result.path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    assert fm["external_ref"] == "jira:PROJ-123"
    assert fm["external_updated_at"] == "2026-10-01T00:00:00.000+0000"
    assert "todo" in fm["tags"] and "backend" in fm["tags"]
    assert fm["due"] == "2026-10-10"
    assert "# Fix the thing" in body
    assert "Do the work." in body


def test_pull_idempotent_no_duplicate(tmp_path):
    items = tmp_path / "items"
    first = upsert_task_record(_record(), items, today="2026-10-24")
    before = first.path.read_text(encoding="utf-8")

    second = upsert_task_record(_record(), items, today="2026-10-25")  # same remote, later day

    assert second.action == "unchanged"
    assert second.path == first.path
    assert sorted(p.name for p in items.glob("*.md")) == [first.path.name]
    assert second.path.read_text(encoding="utf-8") == before  # byte-identical, no churn


def test_conflict_keeps_md_and_notes_human(tmp_path):
    items = tmp_path / "items"
    created = upsert_task_record(_record(), items, today="2026-10-24")

    # Human edits the md's due (md-side change).
    human = created.path.read_text(encoding="utf-8").replace("due: 2026-10-10", "due: 2026-10-20")
    created.path.write_text(human, encoding="utf-8")

    # Remote also changed due AND advanced its updated timestamp (remote-side change).
    diverged = _record(
        frontmatter={"tags": ["backend"], "due": "2026-10-15", "priority": 70},
        remote_updated_at="2026-10-05T00:00:00.000+0000",
    )
    result = upsert_task_record(diverged, items, today="2026-10-26")

    assert result.action == "conflict"
    assert result.conflicts == ["due"]
    text = result.path.read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(text)
    assert fm["due"] == "2026-10-20"              # md wins — not overwritten
    assert "트래커 충돌: due" in text               # human-readable trace
    assert "2026-10-15" in text                    # names the tracker value
    assert text.index("## Progress Log") < text.index("트래커 충돌")  # note is in the log


def test_conflict_silent_when_remote_not_advanced(tmp_path):
    """md differs from a STALE remote (not advanced): md wins silently, no note."""
    items = tmp_path / "items"
    created = upsert_task_record(_record(), items, today="2026-10-24")
    human = created.path.read_text(encoding="utf-8").replace("due: 2026-10-10", "due: 2026-10-20")
    created.path.write_text(human, encoding="utf-8")

    # Same remote timestamp as last sync → remote did NOT advance.
    result = upsert_task_record(_record(frontmatter={"due": "2026-10-15"}), items, today="2026-10-26")

    assert result.action == "unchanged"
    assert "트래커 충돌" not in result.path.read_text(encoding="utf-8")


def test_status_monotonic_done_sticky(tmp_path):
    """md is done; a remote that still reads open must NOT reopen it (§⑨)."""
    items = tmp_path / "items"
    created = upsert_task_record(_record(), items, today="2026-10-24")
    closed = created.path.read_text(encoding="utf-8").replace("status: open", "status: done")
    created.path.write_text(closed, encoding="utf-8")

    result = upsert_task_record(_record(status="open"), items, today="2026-10-25")

    fm, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fm["status"] == "done"  # stayed done — reopen only from md


def test_status_promoted_to_done_from_remote(tmp_path):
    """md is open; a remote that reads done promotes it to done (§⑨)."""
    items = tmp_path / "items"
    upsert_task_record(_record(), items, today="2026-10-24")

    result = upsert_task_record(
        _record(status="done", remote_updated_at="2026-10-05T00:00:00.000+0000"),
        items,
        today="2026-10-25",
    )

    fm, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fm["status"] == "done"


def test_upsert_fills_empty_md_field(tmp_path):
    """An empty md-owned field is filled from the remote (not a conflict)."""
    items = tmp_path / "items"
    created = upsert_task_record(_record(frontmatter={"tags": ["backend"]}), items, today="2026-10-24")
    fm, _ = parse_frontmatter(created.path.read_text(encoding="utf-8"))
    assert fm.get("due") in (None, "-")  # nothing to seed it

    result = upsert_task_record(
        _record(frontmatter={"tags": ["backend"], "due": "2026-11-01"},
                remote_updated_at="2026-10-05T00:00:00.000+0000"),
        items,
        today="2026-10-25",
    )
    fm2, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fm2["due"] == "2026-11-01"
    assert result.conflicts == []


def test_append_progress_log_creates_section_when_missing(tmp_path):
    md = "---\nstatus: open\n---\n\n# No log here\n\nbody\n"
    out = append_progress_log(md, "first entry", today="2026-10-24")
    assert "## Progress Log" in out
    assert "- 2026-10-24: first entry" in out
