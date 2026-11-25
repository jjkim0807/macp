"""`recur:` — parsing, the next time, the next round's markdown, and the sweep.

Generic slugs only (`weekly-audit`, `task-a`): this is a public repo. The dates
are fixed and every clock is passed in, so nothing here reads the wall clock.
2026-11-25 is a Wednesday and 2026-11-27 a Friday.
"""

from __future__ import annotations

import datetime as dt

import pytest

from todo_system import recur
from todo_system import task_validate as tv
from todo_system import worker as W

D = dt.datetime


# --- the value ---------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    ("daily", recur.Recur(1)),
    ("weekly", recur.Recur(7)),
    ("every 3d", recur.Recur(3)),
    ("every 1d", recur.Recur(1)),
    ("Every 14D", recur.Recur(14)),
    ("  weekly  ", recur.Recur(7)),
    ('"weekly"', recur.Recur(7)),
    ("weekly fri 09:00", recur.Recur(7, 4, dt.time(9, 0))),
    ("weekly  Fri   9:00", recur.Recur(7, 4, dt.time(9, 0))),
    ("weekly friday 23:59", recur.Recur(7, 4, dt.time(23, 59))),
    ("weekly mon 00:00", recur.Recur(7, 0, dt.time(0, 0))),
    ("weekly sun 18:30", recur.Recur(7, 6, dt.time(18, 30))),
    ("weekly 금 09:00", recur.Recur(7, 4, dt.time(9, 0))),
    ("weekly 금요일 09:00", recur.Recur(7, 4, dt.time(9, 0))),
    ("weekly 월 10:00", recur.Recur(7, 0, dt.time(10, 0))),
])
def test_valid_values_parse(value, expected):
    assert recur.parse_recur(value) == expected
    assert recur.recur_error(value) is None


@pytest.mark.parametrize("value", [None, "", "-", "  ", "off", "OFF", '"off"', False, []])
def test_absent_and_off_mean_no_recurrence_and_no_error(value):
    assert recur.parse_recur(value) is None
    assert recur.recur_error(value) is None


@pytest.mark.parametrize("value", [
    "monthly",
    "every 0d",
    "every 3 days",
    "every 3w",
    "every d",
    "every 12345d",
    "daily 09:00",
    "weekly fri",
    "weekly 09:00",
    "weekly xyz 09:00",
    "weekly fri 24:00",
    "weekly fri 09:60",
    "weekly fri 9am",
    "weekly fri 09:00 extra",
    "on",
    True,
    7,
    ["weekly"],
])
def test_other_values_are_errors_and_never_a_schedule(value):
    assert recur.parse_recur(value) is None
    reason = recur.recur_error(value)
    assert reason is not None
    assert recur.EXPECTED in reason


def test_inline_comment_is_named_in_the_error():
    reason = recur.recur_error("weekly  # every Friday")
    assert "inline comments" in reason
    assert "inline comments" not in recur.recur_error("monthly")


# --- the next time -----------------------------------------------------------


def test_weekly_steps_one_period_from_the_anchor_and_keeps_its_clock_time():
    anchor = D(2026, 11, 27, 10, 0)
    assert recur.next_occurrence(recur.Recur(7), anchor, D(2026, 11, 27, 10, 3)) == D(2026, 12, 4, 10, 0)


def test_missed_periods_are_skipped_to_the_one_time_still_ahead():
    anchor = D(2026, 11, 6, 10, 0)
    # 11-13 and 11-20 went by unnoticed: one round, for 11-27, not three.
    assert recur.next_occurrence(recur.Recur(7), anchor, D(2026, 11, 25, 9, 0)) == D(2026, 11, 27, 10, 0)


def test_a_time_that_is_exactly_now_is_already_past():
    anchor = D(2026, 11, 20, 10, 0)
    assert recur.next_occurrence(recur.Recur(7), anchor, D(2026, 11, 27, 10, 0)) == D(2026, 12, 4, 10, 0)
    assert recur.next_occurrence(recur.Recur(7), anchor, D(2026, 11, 27, 9, 59)) == D(2026, 11, 27, 10, 0)


def test_daily_from_a_date_only_anchor_is_the_next_midnight():
    assert recur.next_occurrence(recur.Recur(1), D(2026, 11, 25), D(2026, 11, 25, 8, 0)) == D(2026, 11, 26)


def test_every_n_days_stays_on_the_anchors_grid():
    anchor = D(2026, 11, 1)
    assert recur.next_occurrence(recur.Recur(3), anchor, D(2026, 11, 1, 0, 5)) == D(2026, 11, 4)
    # 24 days later, at noon: the grid is 11-25, 11-28, … and 11-25 00:00 is past.
    assert recur.next_occurrence(recur.Recur(3), anchor, D(2026, 11, 25, 12, 0)) == D(2026, 11, 28)


def test_an_anchor_still_ahead_gets_the_period_after_it():
    assert recur.next_occurrence(recur.Recur(7), D(2026, 12, 4), D(2026, 11, 25, 9, 0)) == D(2026, 12, 11)


def test_weekday_form_is_the_same_weekday_one_week_on():
    spec = recur.parse_recur("weekly fri 09:00")
    assert recur.next_occurrence(spec, D(2026, 11, 27, 9, 0), D(2026, 11, 27, 9, 5)) == D(2026, 12, 4, 9, 0)


def test_weekday_form_never_lands_on_the_anchors_own_date():
    # The round stands on Friday 11-27 already; a later clock time that same
    # day (the user moved the series to 14:00) is next week's, not today's.
    spec = recur.parse_recur("weekly fri 14:00")
    assert recur.next_occurrence(spec, D(2026, 11, 27, 9, 0), D(2026, 11, 27, 9, 5)) == D(2026, 12, 4, 14, 0)
    assert recur.next_occurrence(spec, D(2026, 11, 27), D(2026, 11, 25, 9, 0)) == D(2026, 12, 4, 14, 0)


def test_weekday_form_from_an_off_schedule_anchor_finds_the_first_such_day():
    spec = recur.parse_recur("weekly 금 09:00")
    assert recur.next_occurrence(spec, D(2026, 11, 25), D(2026, 11, 25, 8, 0)) == D(2026, 11, 27, 9, 0)


def test_weekday_form_skips_missed_weeks():
    spec = recur.parse_recur("weekly fri 09:00")
    assert recur.next_occurrence(spec, D(2026, 11, 6, 9, 0), D(2026, 11, 25, 12, 0)) == D(2026, 11, 27, 9, 0)
    assert recur.next_occurrence(spec, D(2026, 11, 6, 9, 0), D(2026, 11, 27, 9, 0)) == D(2026, 12, 4, 9, 0)


# --- the slug ----------------------------------------------------------------


@pytest.mark.parametrize("slug, expected", [
    ("weekly-audit-20261127", ("weekly-audit", dt.date(2026, 11, 27))),
    ("weekly-audit", ("weekly-audit", None)),
    ("task-a-20261127-20261204", ("task-a-20261127", dt.date(2026, 12, 4))),
    ("task-a-20261340", ("task-a-20261340", None)),      # not a date
    ("task-a-2026112", ("task-a-2026112", None)),        # seven digits
    ("task-a-202611270", ("task-a-202611270", None)),    # nine digits
    ("20261127", ("20261127", None)),                    # no series in front
])
def test_split_slug(slug, expected):
    assert recur.split_slug(slug) == expected


# --- the next round's markdown -------------------------------------------------

_ROUND = '''---
tags:
  - todo
  - work
status: done
due: 2026-11-29
not_before: "2026-11-27 10:00"
updated: 2026-11-20
scheduled: "2026-11-27 10:00~11:00"
completed: "2026-11-27 10:40"
outcome: "The report for the 2026-11-27 round is committed"
recur: weekly
worker_mode: delegate
worker_branch: "worker/weekly-audit-20261127"
acceptance_criteria:
  - "status: the report exists"
  - "- [x] is only text here"
---

# Weekly audit

- [x] step one
  - [X] nested step
- [ ] step two
-[x] not a checkbox

## Progress Log

- 2026-11-20: created by hand
- 2026-11-27: ran

## Notes

- [x] a box below the log is reset too
kept as written: 2026-11-27, weekly-audit-20261127
'''

_NEXT = '''---
tags:
  - todo
  - work
status: open
due: 2026-12-06
not_before: "2026-12-04 10:00"
updated: 2026-11-27
scheduled: "2026-12-04 10:00~11:00"
outcome: "The report for the 2026-11-27 round is committed"
recur: weekly
worker_mode: delegate
worker_branch: "worker/weekly-audit-20261204"
acceptance_criteria:
  - "status: the report exists"
  - "- [x] is only text here"
---

# Weekly audit

- [ ] step one
  - [ ] nested step
- [ ] step two
-[x] not a checkbox

## Progress Log

- 2026-11-27: 생성 — weekly-audit-20261127에서 recur

## Notes

- [ ] a box below the log is reset too
kept as written: 2026-11-27, weekly-audit-20261127
'''


def _build(text=_ROUND, *, anchor=D(2026, 11, 27, 10, 0), when=D(2026, 12, 4, 10, 0),
           today=dt.date(2026, 11, 27)):
    return recur.build_next_round(
        text, prev_slug="weekly-audit-20261127", new_slug="weekly-audit-20261204",
        anchor=anchor, when=when, today=today)


def test_next_round_differs_in_the_moved_fields_and_nothing_else():
    assert _build() == _NEXT


def test_next_round_log_line_follows_the_ui_language(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert "- 2026-11-27: created — recur from weekly-audit-20261127\n" in _build()


def test_midnight_not_before_is_written_as_a_bare_date():
    out = _build(anchor=D(2026, 11, 27), when=D(2026, 12, 4))
    assert "\nnot_before: 2026-12-04\n" in out


def test_seconds_survive_in_not_before():
    out = _build(anchor=D(2026, 11, 27, 10, 0, 30), when=D(2026, 12, 4, 10, 0, 30))
    assert '\nnot_before: "2026-12-04 10:00:30"\n' in out


def test_dates_move_by_whole_days_and_the_block_keeps_its_clock_times():
    # Wednesday 14:00 -> Friday 09:00 is two dates on, whatever the hours say.
    out = _build(anchor=D(2026, 11, 25, 14, 0), when=D(2026, 11, 27, 9, 0))
    assert "\ndue: 2026-12-01\n" in out
    assert '\nscheduled: "2026-11-29 10:00~11:00"\n' in out
    assert '\nnot_before: "2026-11-27 09:00"\n' in out


def test_fields_the_round_does_not_have_are_not_invented_except_not_before():
    text = "---\nstatus: open\nrecur: daily\n---\n\n# Daily\n\n- [x] done\n"
    out = recur.build_next_round(
        text, prev_slug="daily-check", new_slug="daily-check-20261126",
        anchor=D(2026, 11, 25), when=D(2026, 11, 26), today=dt.date(2026, 11, 25))
    assert out == (
        "---\nstatus: open\nrecur: daily\nnot_before: 2026-11-26\nupdated: 2026-11-25\n---\n"
        "\n# Daily\n\n- [ ] done\n\n## Progress Log\n\n"
        "- 2026-11-25: 생성 — daily-check에서 recur\n")


@pytest.mark.parametrize("line", [
    'due: "-"', "due: -", 'scheduled: "10:00~11:00"', "scheduled: -", "due: someday",
])
def test_values_without_a_date_are_left_alone(line):
    text = f"---\nstatus: open\n{line}\nrecur: weekly\n---\nbody\n"
    assert f"\n{line}\n" in _build(text)


def test_a_date_only_scheduled_moves_too():
    text = "---\nstatus: open\nscheduled: 2026-11-27\nrecur: weekly\n---\nbody\n"
    assert "\nscheduled: 2026-12-04\n" in _build(text)


def test_what_one_round_owns_alone_does_not_travel():
    # A second file naming the same tracker issue would be updated, and reported
    # done, in place of the first.
    text = ("---\nstatus: done\nexternal_ref: jira:PROJ-123\n"
            "external_updated_at: 2026-11-20T10:00:00Z\n"
            'completed: "2026-11-27 10:40"\nrecur: weekly\n---\nbody\n')
    out = _build(text)
    for key in ("external_ref", "external_updated_at", "completed"):
        assert f"\n{key}:" not in out
    assert out.startswith("---\nstatus: open\nrecur: weekly\nnot_before: ")


def test_a_worker_branch_of_another_name_is_kept():
    text = '---\nstatus: open\nworker_branch: "worker/custom"\nrecur: weekly\n---\nbody\n'
    assert '\nworker_branch: "worker/custom"\n' in _build(text)


def test_only_top_level_keys_are_rewritten():
    text = ("---\nstatus: done\ndecision_options:\n  - label: go\n    status: pending\n"
            "    due: 2026-11-29\nrecur: weekly\n---\nbody\n")
    out = _build(text)
    assert "\nstatus: open\n" in out
    assert "\n    status: pending\n    due: 2026-11-29\n" in out


def test_a_key_written_twice_ends_up_defined_once():
    # The engine's line parsers take the LAST definition: a copy that rewrote only
    # the first `status:` would be born `done`.
    text = ("---\nstatus: open\nrecur: weekly\nstatus: done\n"
            "due: 2026-11-29\ndue: 2026-11-30\n---\nbody\n")
    out = _build(text)
    assert out.count("\nstatus:") == 1
    assert W.parse_frontmatter(out)[0]["status"] == "open"
    assert W.parse_frontmatter(out)[0]["due"] == "2026-12-07"
    assert "\ndue: 2026-12-06\ndue: 2026-12-07\n" in out


def test_a_date_that_is_not_one_is_left_as_written():
    text = "---\nstatus: open\ndue: 2026-13-45\nrecur: weekly\n---\nbody\n"
    assert "\ndue: 2026-13-45\n" in _build(text)


def test_a_file_without_frontmatter_is_refused():
    with pytest.raises(ValueError):
        _build("# no frontmatter\n")


@pytest.mark.parametrize("text", [
    "---\nrecur: weekly fri 09:00\n---\n",
    '---\nstatus: open\nrecur: "every 3d"\n---\n',
    "---\nrecur: off\nstatus: open\n---\nbody\n",
    "---\nrecur:   daily   \n---\n",
    "---\nrecur: daily\nstatus: open\nrecur: weekly\n---\n",      # written twice: the last one
])
def test_the_sweep_reads_recur_exactly_as_validate_does(text):
    lines, _body = recur._frontmatter(text)
    assert recur._value(lines, "recur") == W.parse_frontmatter(text)[0]["recur"]


# --- the sweep ---------------------------------------------------------------

NOW = D(2026, 11, 27, 10, 3)          # a Friday, just past 10:00


def _write(items, slug, *, status="open", recur_value="weekly", not_before='"2026-11-27 10:00"',
           extra="", body="- [x] step\n"):
    lines = ["---", "tags:", "  - todo", f"status: {status}"]
    if not_before is not None:
        lines.append(f"not_before: {not_before}")
    if recur_value is not None:
        lines.append(f"recur: {recur_value}")
    if extra:
        lines += extra.strip("\n").split("\n")
    lines += ["---", "", f"# {slug}", "", body]
    path = items / f"{slug}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _snapshot(items):
    return {p.name: p.read_bytes() for p in sorted(items.iterdir())}


def test_a_round_that_reached_its_time_gets_its_next_round(tmp_path):
    _write(tmp_path, "weekly-audit-20261127", extra="due: 2026-11-29")
    made = recur.generate_due(tmp_path, now=NOW)
    assert made == [recur.Created("weekly-audit-20261204", "weekly-audit-20261127",
                                  "2026-12-04T10:00")]
    text = (tmp_path / "weekly-audit-20261204.md").read_text(encoding="utf-8")
    assert '\nnot_before: "2026-12-04 10:00"\n' in text
    assert "\ndue: 2026-12-06\n" in text
    assert "\nstatus: open\n" in text
    assert "\nupdated: 2026-11-27\n" in text
    assert "\nrecur: weekly\n" in text
    assert "- [ ] step\n" in text
    assert "- 2026-11-27: 생성 — weekly-audit-20261127에서 recur\n" in text


def test_the_sweep_is_idempotent(tmp_path):
    _write(tmp_path, "weekly-audit-20261127")
    assert len(recur.generate_due(tmp_path, now=NOW)) == 1
    after_first = _snapshot(tmp_path)
    assert recur.generate_due(tmp_path, now=NOW) == []
    assert recur.generate_due(tmp_path, now=NOW + dt.timedelta(days=3)) == []
    assert _snapshot(tmp_path) == after_first
    assert sorted(after_first) == ["weekly-audit-20261127.md", "weekly-audit-20261204.md"]


def test_the_chain_continues_when_the_new_round_reaches_its_own_time(tmp_path):
    _write(tmp_path, "weekly-audit-20261127", status="done")
    recur.generate_due(tmp_path, now=NOW)
    later = D(2026, 12, 4, 10, 0)
    assert [c.slug for c in recur.generate_due(tmp_path, now=later)] == ["weekly-audit-20261211"]


def test_a_round_that_is_still_deferred_creates_nothing(tmp_path):
    _write(tmp_path, "weekly-audit-20261127")
    before = _snapshot(tmp_path)
    assert recur.generate_due(tmp_path, now=D(2026, 11, 27, 9, 59)) == []
    assert _snapshot(tmp_path) == before


def test_tasks_without_recur_are_read_and_never_written(tmp_path):
    _write(tmp_path, "task-a", recur_value=None, not_before=None)
    _write(tmp_path, "task-b-20261127", recur_value=None)
    _write(tmp_path, "task-c", recur_value=None, body="recur: weekly in the body is prose\n")
    before = _snapshot(tmp_path)
    assert recur.generate_due(tmp_path, now=NOW) == []
    assert _snapshot(tmp_path) == before


def test_another_open_round_of_the_series_holds_the_next_one_back(tmp_path):
    _write(tmp_path, "weekly-audit-20261120", not_before='"2026-11-20 10:00"')
    _write(tmp_path, "weekly-audit-20261127")
    before = _snapshot(tmp_path)
    assert recur.generate_due(tmp_path, now=NOW) == []
    assert _snapshot(tmp_path) == before


def test_the_series_resumes_once_the_open_round_closes(tmp_path):
    stuck = _write(tmp_path, "weekly-audit-20261120", not_before='"2026-11-20 10:00"')
    _write(tmp_path, "weekly-audit-20261127", status="done")
    assert recur.generate_due(tmp_path, now=D(2026, 12, 9, 12, 0)) == []
    stuck.write_text(stuck.read_text(encoding="utf-8").replace("status: open", "status: cancelled"),
                     encoding="utf-8")
    # 12-04 went by while the old round was stuck: one round, for 12-11.
    made = recur.generate_due(tmp_path, now=D(2026, 12, 9, 12, 0))
    assert [c.slug for c in made] == ["weekly-audit-20261211"]


def test_an_open_dated_round_holds_back_even_without_recur_of_its_own(tmp_path):
    # e.g. a round written by hand before the series got `recur`
    _write(tmp_path, "weekly-audit-20261120", recur_value=None, not_before='"2026-11-20 10:00"')
    _write(tmp_path, "weekly-audit-20261127")
    assert recur.generate_due(tmp_path, now=NOW) == []


def test_an_open_first_round_without_a_date_holds_the_series_back(tmp_path):
    first = _write(tmp_path, "weekly-audit", not_before='"2026-11-20 10:00"')
    _write(tmp_path, "weekly-audit-20261127")
    assert recur.generate_due(tmp_path, now=NOW) == []
    first.write_text(first.read_text(encoding="utf-8").replace("status: open", "status: done"),
                     encoding="utf-8")
    assert [c.slug for c in recur.generate_due(tmp_path, now=NOW)] == ["weekly-audit-20261204"]


def test_an_umbrella_task_named_like_the_series_does_not_hold_it_back(tmp_path):
    # An open task that only shares the series' name (the rounds' `parent`, say)
    # carries no `recur`, so it is not a round and must not stall the series.
    _write(tmp_path, "weekly-audit", recur_value=None, not_before=None, extra="is_parent: true")
    _write(tmp_path, "weekly-audit-20261127", extra='parent: "[[weekly-audit]]"')
    assert [c.slug for c in recur.generate_due(tmp_path, now=NOW)] == ["weekly-audit-20261204"]


@pytest.mark.parametrize("status", ["done", "cancelled", "canceled"])
def test_closing_the_newest_round_early_does_not_end_the_series(tmp_path, status):
    _write(tmp_path, "weekly-audit-20261127", status=status)
    assert recur.generate_due(tmp_path, now=D(2026, 11, 26, 9, 0)) == []     # not its time yet
    assert [c.slug for c in recur.generate_due(tmp_path, now=NOW)] == ["weekly-audit-20261204"]


def test_recur_off_on_the_newest_round_stops_the_series(tmp_path):
    _write(tmp_path, "weekly-audit-20261120", status="done", not_before='"2026-11-20 10:00"')
    _write(tmp_path, "weekly-audit-20261127", recur_value="off")
    before = _snapshot(tmp_path)
    assert recur.generate_due(tmp_path, now=NOW) == []
    assert _snapshot(tmp_path) == before


def test_removing_recur_from_the_newest_round_stops_the_series(tmp_path):
    _write(tmp_path, "weekly-audit-20261120", status="done", not_before='"2026-11-20 10:00"')
    _write(tmp_path, "weekly-audit-20261127", status="done", recur_value=None)
    assert recur.generate_due(tmp_path, now=NOW) == []


def test_an_invalid_value_creates_nothing(tmp_path):
    _write(tmp_path, "weekly-audit-20261127", recur_value="every friday")
    before = _snapshot(tmp_path)
    assert recur.generate_due(tmp_path, now=NOW) == []
    assert _snapshot(tmp_path) == before


def test_a_first_round_without_dates_counts_from_today(tmp_path):
    _write(tmp_path, "daily-check", recur_value="daily", not_before=None)
    made = recur.generate_due(tmp_path, now=D(2026, 11, 25, 8, 0))
    assert made == [recur.Created("daily-check-20261126", "daily-check", "2026-11-26")]
    assert "\nnot_before: 2026-11-26\n" in (
        tmp_path / "daily-check-20261126.md").read_text(encoding="utf-8")


def test_a_round_without_not_before_counts_from_its_scheduled_date(tmp_path):
    _write(tmp_path, "weekly-review", not_before=None,
           extra='due: 2026-11-27\nscheduled: "2026-11-27 10:00~11:00"')
    made = recur.generate_due(tmp_path, now=D(2026, 11, 25, 8, 0))
    assert [c.slug for c in made] == ["weekly-review-20261204"]
    text = (tmp_path / "weekly-review-20261204.md").read_text(encoding="utf-8")
    assert "\nnot_before: 2026-12-04\n" in text
    assert '\nscheduled: "2026-12-04 10:00~11:00"\n' in text
    assert "\ndue: 2026-12-04\n" in text


def test_a_dated_round_without_not_before_counts_from_its_slug_date(tmp_path):
    _write(tmp_path, "weekly-audit-20261127", not_before=None)
    made = recur.generate_due(tmp_path, now=D(2026, 11, 25, 8, 0))
    assert [c.slug for c in made] == ["weekly-audit-20261204"]


def test_weekday_form_through_the_sweep(tmp_path):
    _write(tmp_path, "weekly-review", recur_value="weekly fri 09:00", not_before="2026-11-25")
    made = recur.generate_due(tmp_path, now=D(2026, 11, 25, 8, 0))
    assert made == [recur.Created("weekly-review-20261127", "weekly-review", "2026-11-27T09:00")]
    assert '\nnot_before: "2026-11-27 09:00"\n' in (
        tmp_path / "weekly-review-20261127.md").read_text(encoding="utf-8")


def test_a_taken_slot_is_never_overwritten(tmp_path):
    # The newest round's not_before was pulled a week early by hand, so the
    # "next" slot is its own file: the sweep takes the first free one after it.
    _write(tmp_path, "weekly-audit-20261204", not_before='"2026-11-27 10:00"')
    before = (tmp_path / "weekly-audit-20261204.md").read_bytes()
    made = recur.generate_due(tmp_path, now=NOW)
    assert [c.slug for c in made] == ["weekly-audit-20261211"]
    assert (tmp_path / "weekly-audit-20261204.md").read_bytes() == before
    text = (tmp_path / "weekly-audit-20261211.md").read_text(encoding="utf-8")
    assert '\nnot_before: "2026-12-11 10:00"\n' in text


def test_one_broken_series_does_not_stop_the_others(tmp_path):
    (tmp_path / "broken-series.md").mkdir()               # reading it raises
    _write(tmp_path, "weekly-audit-20261127")
    warned = []
    made = recur.generate_due(tmp_path, now=NOW, warn=warned.append)
    assert [c.slug for c in made] == ["weekly-audit-20261204"]
    assert len(warned) == 1 and warned[0].startswith("broken-series: ")


def test_a_failed_write_leaves_no_half_written_round(tmp_path, monkeypatch):
    _write(tmp_path, "weekly-audit-20261127")
    dest = tmp_path / "weekly-audit-20261204.md"
    real_open = open

    class FullDisk:
        """The real file, whose write fails after a few bytes reached the disk."""

        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

        def write(self, _text):
            self._fh.write("---\nstatus: op")
            raise OSError(28, "No space left on device")

    warned = []
    with monkeypatch.context() as m:
        m.setattr(recur, "open", lambda *a, **k: FullDisk(real_open(*a, **k)), raising=False)
        assert recur.generate_due(tmp_path, now=NOW, warn=warned.append) == []
    assert len(warned) == 1 and "No space left on device" in warned[0]
    assert not dest.exists()
    # the slot is free again, so the next tick writes the same round whole
    assert [c.slug for c in recur.generate_due(tmp_path, now=NOW)] == ["weekly-audit-20261204"]
    assert dest.read_text(encoding="utf-8").endswith("에서 recur\n")


def test_a_missing_directory_is_an_empty_sweep(tmp_path):
    assert recur.generate_due(tmp_path / "no-such-items", now=NOW) == []


def test_the_default_clock_is_not_before_s(tmp_path, monkeypatch):
    from todo_system import not_before as nb
    _write(tmp_path, "weekly-audit-20261127")
    monkeypatch.setattr(nb, "_now", lambda: D(2026, 11, 27, 9, 0))
    assert recur.generate_due(tmp_path) == []
    monkeypatch.setattr(nb, "_now", lambda: NOW)
    assert [c.slug for c in recur.generate_due(tmp_path)] == ["weekly-audit-20261204"]


# --- validate ------------------------------------------------------------------


def _levels(issues, field):
    return [i.level for i in issues if i.field == field]


@pytest.mark.parametrize("value", ["daily", "weekly", "every 3d", "weekly fri 09:00", "off"])
def test_validate_accepts_every_documented_value(value):
    assert tv.validate_frontmatter({"recur": value}) == []


@pytest.mark.parametrize("value", ["monthly", "every 0d", "weekly fri", "weekly  # note", ["weekly"]])
def test_validate_flags_a_bad_value_as_an_error(value):
    issues = tv.validate_frontmatter({"recur": value})
    assert _levels(issues, "recur") == [tv.ERROR]
    assert recur.EXPECTED in issues[0].message


def test_validate_says_nothing_about_a_task_without_recur():
    assert tv.validate_frontmatter({"status": "open", "tags": ["todo"]}) == []


def test_validate_file_reads_the_value_the_sweep_reads(tmp_path):
    """One file, both readers: what validate flags is exactly what the tick skips."""
    bad = _write(tmp_path, "weekly-audit-20261127", recur_value="every friday")
    assert _levels(tv.validate_file(bad), "recur") == [tv.ERROR]
    assert recur.generate_due(tmp_path, now=NOW) == []

    good = _write(tmp_path, "weekly-review-20261127", recur_value='"weekly fri 10:00"')
    assert _levels(tv.validate_file(good), "recur") == []
    assert [c.slug for c in recur.generate_due(tmp_path, now=NOW)] == ["weekly-review-20261204"]


def test_cli_validate_exits_nonzero_on_a_bad_recur(tmp_path, monkeypatch, capsys):
    """The real `cli validate <slug>` path: argv -> cmd_validate -> validate_file."""
    from todo_system import cli

    _write(tmp_path, "weekly-audit-20261127", recur_value="every friday")
    _write(tmp_path, "weekly-review", recur_value="weekly fri 09:00")
    monkeypatch.setattr(cli, "ITEMS_DIR", tmp_path)

    assert cli.cmd_validate(cli.build_parser().parse_args(["validate", "weekly-audit-20261127"])) == 1
    out = capsys.readouterr().out
    assert "[error] weekly-audit-20261127: recur — " in out
    assert "1 error(s)" in out

    assert cli.cmd_validate(cli.build_parser().parse_args(["validate", "weekly-review"])) == 0
    assert "worker contract OK" in capsys.readouterr().out
