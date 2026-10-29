"""`--calendar-route TAG=CALENDAR_ID`: per-tag target calendars for the todo sync.

Every tag and calendar id here is made up — this is a public repo.
"""
from __future__ import annotations

import argparse

import pytest

from todo_system import calendar_sync as CS

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
