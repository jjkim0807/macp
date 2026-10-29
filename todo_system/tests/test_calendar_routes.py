"""`--calendar-route TAG=CALENDAR_ID`: per-tag target calendars for the todo sync.

Every tag and calendar id here is made up — this is a public repo.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from todo_system import calendar_sync as CS
from todo_system import cli as CLI

DEFAULT = "default@example.com"
A = "a@example.com"
B = "b@example.com"


# --- pure helpers (table tests) ----------------------------------------------

@pytest.mark.parametrize(
    "tags, routes, want",
    [
        (["red"], None, DEFAULT),                                   # no routes
        (["red"], [], DEFAULT),
        (["todo", "red"], [("red", A)], A),                         # one match
        (["todo", "green"], [("red", A)], DEFAULT),                 # no match
        (["red", "blue"], [("red", A), ("blue", B)], A),            # task tag order...
        (["blue", "red"], [("red", A), ("blue", B)], B),            # ...beats flag order
        (["red"], [("red", A), ("red", B)], A),                     # same tag twice: first flag
        ([" red "], [("red", A)], A),                               # whitespace
        (None, [("red", A)], DEFAULT),
        ([], [("red", A)], DEFAULT),
    ],
)
def test_route_calendar(tags, routes, want):
    assert CS.route_calendar(tags, routes, DEFAULT) == want


@pytest.mark.parametrize(
    "raw, want",
    [
        ("todo, red ,blue", ["todo", "red", "blue"]),
        ("red,,blue,", ["red", "blue"]),
        (None, []),
        ("", []),
    ],
)
def test_split_tags(raw, want):
    assert CS._split_tags(raw) == want


@pytest.mark.parametrize(
    "value, want",
    [
        ("red=a@example.com", ("red", A)),
        (" red = a@example.com ", ("red", A)),
        ("red=a=b", ("red", "a=b")),                                # first `=` only
    ],
)
def test_parse_calendar_route(value, want):
    assert CS.parse_calendar_route(value) == want


@pytest.mark.parametrize("value", ["=a@example.com", "red=", "red", " = ", ""])
def test_parse_calendar_route_rejects_malformed(value):
    with pytest.raises(argparse.ArgumentTypeError):
        CS.parse_calendar_route(value)


@pytest.mark.parametrize(
    "routes, want",
    [
        (None, [DEFAULT]),
        ([], [DEFAULT]),
        ([("red", A), ("blue", A), ("green", DEFAULT)], [DEFAULT, A]),
        ([("red", B), ("blue", A)], [DEFAULT, B, A]),
    ],
)
def test_managed_calendars(routes, want):
    assert CS.managed_calendars(DEFAULT, routes) == want


# --- sync against a per-calendar fake service --------------------------------

class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _MultiCalEvents:
    """`service.events()` stand-in that keeps one event store per calendarId."""

    def __init__(self, store: dict[str, list[dict]] | None = None):
        self.store = {cal: list(evs) for cal, evs in (store or {}).items()}
        self.scans: list[str] = []
        self.inserted: list[tuple[str, dict]] = []
        self.deleted: list[tuple[str, str]] = []
        self.patched: list[tuple[str, str]] = []
        self._n = 0

    def list(self, **kw):
        items = self.store.get(kw["calendarId"], [])
        if "privateExtendedProperty" in kw:  # per-slug lookup in _upsert_event
            slug = kw["privateExtendedProperty"].split("=", 1)[1]
            return _Req({"items": [e for e in items if _slug(e) == slug]})
        self.scans.append(kw["calendarId"])  # cleanup scan
        return _Req({"items": list(items)})

    def insert(self, calendarId, body):
        self._n += 1
        self.inserted.append((calendarId, body))
        self.store.setdefault(calendarId, []).append({"id": f"new{self._n}", **body})
        return _Req({})

    def delete(self, calendarId, eventId):
        self.deleted.append((calendarId, eventId))
        self.store[calendarId] = [
            e for e in self.store.get(calendarId, []) if e["id"] != eventId]
        return _Req({})

    def patch(self, calendarId, eventId, body):
        self.patched.append((calendarId, eventId))
        return _Req({})


class _FakeService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


def _slug(ev: dict) -> str | None:
    return ((ev.get("extendedProperties") or {}).get("private") or {}).get("todo_slug")


def _todo_event(eid: str, slug: str) -> dict:
    return {"id": eid, "summary": slug,
            "extendedProperties": {"private": {"todo_slug": slug}}}


def _manual_event(eid: str) -> dict:
    return {"id": eid, "summary": "dentist", "extendedProperties": {}}


def _db_with_tags(rows):
    """rows: (slug, title, due, path, tags, worker_mode)."""
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE tasks (slug TEXT, title TEXT, due TEXT, next_action TEXT, "
        "path TEXT, status TEXT, worker_mode TEXT, tags TEXT)"
    )
    con.executemany(
        "INSERT INTO tasks (slug, title, due, next_action, path, status, worker_mode, tags) "
        "VALUES (?, ?, ?, '', ?, 'open', ?, ?)",
        [(s, t, d, p, wm, tg) for s, t, d, p, tg, wm in rows],
    )
    return con


def _sync(monkeypatch, con, events, routes):
    monkeypatch.setattr(CS, "load_credentials", lambda *a, **k: object())
    monkeypatch.setattr(CS, "build", lambda *a, **k: _FakeService(events))
    return CS.sync_due_tasks_to_calendar(
        con=con,
        calendar_id=DEFAULT,
        client_secret_path=Path("/nope"),
        token_path=Path("/nope"),
        calendar_routes=routes,
    )


def test_moved_task_lands_in_new_calendar_and_leaves_the_old(monkeypatch):
    con = _db_with_tags([("t1", "Task one", "2026-11-02", "x.md", "todo,red", None)])
    events = _MultiCalEvents({DEFAULT: [_todo_event("old", "t1")]})

    created, _ = _sync(monkeypatch, con, events, [("red", A)])

    assert created == 1
    assert [(cal, body["summary"]) for cal, body in events.inserted] == [(A, "Task one")]
    assert events.deleted == [(DEFAULT, "old")]
    assert not [d for d in events.deleted if d[0] == A]


def test_manual_events_untouched_in_every_calendar(monkeypatch):
    con = _db_with_tags([("t1", "Task one", "2026-11-02", "x.md", "red", None)])
    events = _MultiCalEvents({
        DEFAULT: [_manual_event("m-default")],
        A: [_manual_event("m-a")],
    })

    _sync(monkeypatch, con, events, [("red", A)])

    deleted_ids = {eid for _, eid in events.deleted}
    assert "m-default" not in deleted_ids and "m-a" not in deleted_ids
    assert events.scans == [DEFAULT, A]


def test_stale_slug_in_a_route_calendar_is_deleted(monkeypatch):
    con = _db_with_tags([("t1", "Task one", "2026-11-02", "x.md", "red", None)])
    events = _MultiCalEvents({A: [_todo_event("gone", "closed-task")]})

    _sync(monkeypatch, con, events, [("red", A)])

    assert (A, "gone") in events.deleted


def test_correctly_placed_task_is_kept(monkeypatch):
    con = _db_with_tags([("t1", "Task one", "2026-11-02", "x.md", "red", None)])
    events = _MultiCalEvents({A: [_todo_event("keep", "t1")]})

    _sync(monkeypatch, con, events, [("red", A)])

    assert events.deleted == []
    assert events.inserted == []
    assert events.patched == [(A, "keep")]   # legacy event (no baseline) → md wins once


def test_managed_set_is_scanned_once_each_default_first(monkeypatch):
    con = _db_with_tags([("t1", "Task one", "2026-11-02", "x.md", "red", None)])
    events = _MultiCalEvents()

    _sync(monkeypatch, con, events,
          [("red", A), ("blue", A), ("green", DEFAULT)])

    assert events.scans == [DEFAULT, A]


def test_untagged_and_unrouted_tasks_go_to_the_default(monkeypatch):
    con = _db_with_tags([
        ("t1", "No tags", "2026-11-02", "x.md", None, None),
        ("t2", "Unrouted", "2026-11-02", "y.md", "todo,green", None),
        ("t3", "Routed", "2026-11-02", "z.md", "blue", None),
    ])
    events = _MultiCalEvents()

    _sync(monkeypatch, con, events, [("red", A), ("blue", B)])

    assert sorted((cal, body["summary"]) for cal, body in events.inserted) == [
        (B, "Routed"), (DEFAULT, "No tags"), (DEFAULT, "Unrouted")]


def test_worker_owned_task_never_reaches_a_route_calendar(monkeypatch):
    con = _db_with_tags([("bot", "Worker task", "2026-11-02", "x.md", "red", "auto")])
    events = _MultiCalEvents({A: [_todo_event("bot-ev", "bot")]})

    _sync(monkeypatch, con, events, [("red", A)])

    assert events.inserted == []
    assert events.deleted == [(A, "bot-ev")]


def test_empty_routes_behave_like_none_on_a_tagless_schema(monkeypatch):
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE tasks (slug TEXT, title TEXT, due TEXT, next_action TEXT, "
        "path TEXT, status TEXT, worker_mode TEXT)"
    )
    con.execute(
        "INSERT INTO tasks VALUES ('t1', 'Task one', '2026-11-02', '', 'x.md', 'open', NULL)")
    events = _MultiCalEvents({DEFAULT: [_todo_event("stale", "closed-task")]})

    created, _ = _sync(monkeypatch, con, events, [])

    assert created == 1
    assert events.scans == [DEFAULT]
    assert [cal for cal, _ in events.inserted] == [DEFAULT]
    assert events.deleted == [(DEFAULT, "stale")]


# --- CLI flag ------------------------------------------------------------------

SUBCOMMANDS = ["import-md", "daemon", "calendar-sync"]


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_parser_collects_repeated_routes(sub):
    args = CLI.build_parser().parse_args([
        sub, "--calendar-route", "red=a@example.com", "--calendar-route", "blue=b@example.com"])
    assert args.calendar_route == [("red", A), ("blue", B)]


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_parser_without_routes_is_none(sub):
    assert CLI.build_parser().parse_args([sub]).calendar_route is None


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_parser_rejects_a_malformed_route(sub):
    with pytest.raises(SystemExit):
        CLI.build_parser().parse_args([sub, "--calendar-route", "red"])


def test_calendar_sync_command_passes_routes_through(monkeypatch):
    import sqlite3

    seen: dict = {}

    def _fake_sync(**kw):
        seen.update(kw)
        return 0, 0

    monkeypatch.setattr(CLI, "db_connect", lambda *a, **k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(CLI, "init_db", lambda *a, **k: None)
    monkeypatch.setattr(CLI, "import_markdown_to_db", lambda *a, **k: 0)
    monkeypatch.setattr(CLI, "sync_due_tasks_to_calendar", _fake_sync)

    args = CLI.build_parser().parse_args([
        "calendar-sync", "--calendar-id", DEFAULT, "--calendar-route", "red=a@example.com"])
    assert args.func(args) == 0
    assert seen["calendar_id"] == DEFAULT
    assert seen["calendar_routes"] == [("red", A)]

    seen.clear()
    args = CLI.build_parser().parse_args(["calendar-sync", "--calendar-id", DEFAULT])
    assert args.func(args) == 0
    assert seen["calendar_routes"] is None
