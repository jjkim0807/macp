"""ops_alerts: idempotent open/resolve of a vault human task, fail-open, isolated.

Every test drives the call-time ``VAULT_ROOT`` seam at a tmp dir (the ``vault``
fixture overrides the suite-wide autouse isolation with a path the test can read
back). Keys are neutral fakes (``hq-disk`` / ``acct-a``) — no real account,
address or identifier, because the module ships in the public engine repo.
"""

from __future__ import annotations

import pytest

from todo_system import cli
from todo_system import ops_alerts as oa


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    monkeypatch.setattr(oa, "VAULT_ROOT", root)
    return root


def _fixed_day(monkeypatch, day):
    monkeypatch.setattr(oa, "_today", lambda: day)


def _fm(path):
    fm, _body = cli.parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm


# -- slug / key building -----------------------------------------------------

def test_slug_normalizes_and_defaults_to_legacy():
    assert oa._slug("hq-disk") == "hq-disk"
    assert oa._slug("Acct A") == "acct-a"
    assert oa._slug("  --Weird__Name!!  ") == "weird-name"
    assert oa._slug("") == "legacy"
    assert oa._slug(None) == "legacy"


def test_account_key_is_stable_and_handles_missing_name():
    assert oa.account_key("acct-a") == "claude-auth-acct-a"
    assert oa.account_key(None) == "claude-auth-legacy"
    assert oa.account_key("") == "claude-auth-legacy"


# -- open: creation / exact frontmatter --------------------------------------

def test_open_creates_task_with_the_documented_frontmatter(vault, monkeypatch):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "본부 루트 디스크 여유 부족",
                  ["df -h /", "du -sh ~/.gradle ~/.cache ~/Worker"], level="alert")
    path = vault / "Todo" / "items" / "ops-hq-disk.md"
    assert path.is_file()
    fm = _fm(path)
    assert fm["status"] == "open"
    assert fm["worker_mode"] == "manual"
    assert fm["due"] == "2049-01-10"
    assert fm["updated"] == "2049-01-10"
    assert "todo" in fm["tags"] and "ops" in fm["tags"] and "hq-disk" in fm["tags"]
    body = path.read_text(encoding="utf-8")
    assert "# 본부 루트 디스크 여유 부족" in body
    assert "- [ ] df -h /" in body
    assert "## Progress Log" in body and "경보 발생" in body


def test_level_is_inert_for_routing(vault, monkeypatch):
    # level → a free-form severity string only: never priority, never a tag.
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "t", ["s"], level="critical")
    fm = _fm(vault / "Todo" / "items" / "ops-hq-disk.md")
    assert fm["severity"] == "critical"
    assert "priority" not in fm
    assert "critical" not in fm["tags"]


def test_open_with_no_level_omits_severity(vault, monkeypatch):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "t", ["s"])
    fm = _fm(vault / "Todo" / "items" / "ops-hq-disk.md")
    assert "severity" not in fm


# -- open: once-a-day re-occurrence throttle + revive ------------------------

def test_second_open_same_day_is_a_silent_throttle(vault, monkeypatch, capsys):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "t", ["s"], level="alert")
    path = vault / "Todo" / "items" / "ops-hq-disk.md"
    before = path.read_text(encoding="utf-8")
    capsys.readouterr()
    oa.open_alert("hq-disk", "t", ["s"], level="alert")
    after = path.read_text(encoding="utf-8")
    assert after == before                      # no write at all
    assert "재발생" not in after
    assert capsys.readouterr().out == ""


def test_open_next_day_adds_one_reoccurrence_line_and_bumps_updated(vault, monkeypatch):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "t", ["s"], level="alert")
    _fixed_day(monkeypatch, "2049-01-11")
    oa.open_alert("hq-disk", "t", ["s"], level="alert")
    path = vault / "Todo" / "items" / "ops-hq-disk.md"
    body = path.read_text(encoding="utf-8")
    assert body.count("재발생") == 1
    assert "- 2049-01-11: 재발생" in body
    assert _fm(path)["updated"] == "2049-01-11"
    assert _fm(path)["status"] == "open"        # still open, not revived


def test_open_after_resolve_revives_the_same_file(vault, monkeypatch):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "t", ["s"], level="alert")
    oa.resolve_alert("hq-disk", "회복")
    path = vault / "Todo" / "items" / "ops-hq-disk.md"
    assert _fm(path)["status"] == "done"
    _fixed_day(monkeypatch, "2049-01-12")
    oa.open_alert("hq-disk", "t", ["s"], level="alert")
    # one path per key: revived in place, back to open
    assert list((vault / "Todo" / "items").glob("ops-hq-disk*.md")) == [path]
    assert _fm(path)["status"] == "open"
    assert _fm(path)["updated"] == "2049-01-12"


# -- resolve -----------------------------------------------------------------

def test_resolve_flips_open_to_done_and_logs(vault, monkeypatch):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "t", ["s"], level="alert")
    _fixed_day(monkeypatch, "2049-01-11")
    oa.resolve_alert("hq-disk", "여유 회복")
    path = vault / "Todo" / "items" / "ops-hq-disk.md"
    fm = _fm(path)
    assert fm["status"] == "done"
    assert fm["updated"] == "2049-01-11"
    assert "- 2049-01-11: 해소 — 여유 회복" in path.read_text(encoding="utf-8")


def test_resolve_with_no_task_is_a_silent_noop(vault, capsys):
    oa.resolve_alert("hq-disk", "회복")
    assert not (vault / "Todo" / "items").exists()     # nothing created
    assert capsys.readouterr().out == ""               # nothing printed


def test_resolve_already_done_does_not_write_again(vault, monkeypatch):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "t", ["s"], level="alert")
    oa.resolve_alert("hq-disk", "회복")
    path = vault / "Todo" / "items" / "ops-hq-disk.md"
    after_first = path.read_text(encoding="utf-8")
    oa.resolve_alert("hq-disk", "다시")
    assert path.read_text(encoding="utf-8") == after_first
    assert after_first.count(": 해소") == 1          # one Progress Log line, not re-logged


# -- fail-open ---------------------------------------------------------------

def test_open_is_fail_open_on_an_unwritable_vault_root(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(oa, "VAULT_ROOT", blocker / "under-a-file")   # mkdir will fail
    oa.open_alert("hq-disk", "t", ["s"])                              # must not raise
    assert "ops-alert: open" in capsys.readouterr().out


def test_both_apis_are_fail_open_when_the_write_raises(vault, monkeypatch, capsys):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-disk", "t", ["s"], level="alert")              # a real open task first
    capsys.readouterr()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(oa, "_atomic_write", boom)
    oa.open_alert("acct-a", "t", ["s"])                              # create path → write boom
    _fixed_day(monkeypatch, "2049-01-11")
    oa.resolve_alert("hq-disk", "회복")                              # resolve path → write boom
    out = capsys.readouterr().out
    assert out.count("ops-alert:") == 2
    assert "open" in out and "resolve" in out


# -- the generated file ingests through the real frontmatter parser ----------

def test_generated_task_round_trips_through_cli_parse_frontmatter(vault, monkeypatch):
    _fixed_day(monkeypatch, "2049-01-10")
    oa.open_alert("hq-browser", "본부 브라우저 로그인 풀림",
                  ["본부에서: browser.sh start", "맥에서 노VNC 터널로 직접 로그인"],
                  level="alert")
    fm, body = cli.parse_frontmatter(
        (vault / "Todo" / "items" / "ops-hq-browser.md").read_text(encoding="utf-8"))
    assert fm["status"] == "open" and fm["worker_mode"] == "manual"
    assert isinstance(fm["tags"], list) and "ops" in fm["tags"]
    assert body.lstrip().startswith("# 본부 브라우저 로그인 풀림")
