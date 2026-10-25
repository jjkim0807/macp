"""The shared :class:`TrackerAdapter` contract assertions (design §⑨) — single source.

The seven ``test_contract_*`` checks every tracker adapter owes the md bridge live
here once; Jira, GitHub Issues and Linear each run them against their own local
recording server. Not a test module itself (the ``_`` prefix keeps pytest from
collecting it), so nothing here runs twice.

How a test file uses it:

* load it by path — ``importlib.util.spec_from_file_location`` registered as
  ``sys.modules["macp_tracker_contract"]`` — so it works under any pytest import
  mode (``todo_system/tests`` is not a package);
* define a ``contract`` fixture that returns a :class:`Case` for its adapter;
* re-bind the ``test_contract_*`` functions as module globals
  (``test_contract_x = _contract.test_contract_x``) so pytest collects them there
  and resolves ``contract`` from that file's fixture.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Callable

import pytest

from todo_system.trackers.base import (
    TaskRecord,
    TrackerAdapter,
    TrackerError,
    parse_frontmatter,
    upsert_task_record,
)

PR_URL = "https://github.com/example/repo/pull/5"


@dataclass
class Case:
    name: str
    server: object                         # any recording server exposing ``.requests``
    adapter: TrackerAdapter
    token: str
    seed: Callable[[], None]
    fail: Callable[[int], None]             # make the next pull answer with this HTTP code
    ref_prefix: str
    done_ref: str
    env_vars: tuple[str, ...]               # every env var that could configure the adapter
    from_config: Callable[..., object]
    auth_carries_token: Callable[[str], bool]


def bearer_or_basic(token: str) -> Callable[[str], bool]:
    """Auth predicate for Jira (Basic ``email:token``) and GitHub (``Bearer token``)."""

    def carries(auth: str) -> bool:
        scheme, _, value = auth.partition(" ")
        if scheme == "Basic":
            return base64.b64decode(value).decode("utf-8").endswith(f":{token}")
        return scheme == "Bearer" and value == token

    return carries


def bare_or_bearer(token: str) -> Callable[[str], bool]:
    """Auth predicate for Linear: personal API keys travel unprefixed; accept a Bearer form too."""

    def carries(auth: str) -> bool:
        return auth == token or auth == f"Bearer {token}"

    return carries


def test_contract_is_tracker_adapter(contract):
    assert isinstance(contract.adapter, TrackerAdapter)


def test_contract_pull_upsert_idempotent(contract, tmp_path):
    contract.seed()
    items = tmp_path / "items"
    first = [upsert_task_record(r, items, today="2026-10-25") for r in contract.adapter.pull()]
    second = [upsert_task_record(r, items, today="2026-10-26") for r in contract.adapter.pull()]

    assert [r.action for r in first] == ["created"]
    assert [r.action for r in second] == ["unchanged"]
    assert len(list(items.glob("*.md"))) == 1
    fm, _ = parse_frontmatter(first[0].path.read_text(encoding="utf-8"))
    assert str(fm["external_ref"]).startswith(contract.ref_prefix)


def test_contract_push_open_without_pr_is_noop(contract):
    ref = contract.adapter.push(TaskRecord(external_ref=contract.done_ref, status="open"))
    assert ref == contract.done_ref
    assert contract.server.requests == []


def test_contract_push_done_with_pr_returns_ref_and_sends_pr_url(contract):
    task = TaskRecord(external_ref=contract.done_ref, status="done", pr_url=PR_URL)
    assert contract.adapter.push(task) == contract.done_ref
    assert any(PR_URL in json.dumps(r["json"]) for r in contract.server.requests if r["json"])


def test_contract_from_config_none_without_config(contract, tmp_path, monkeypatch):
    for var in contract.env_vars:
        monkeypatch.delenv(var, raising=False)
    assert contract.from_config(vault_root=tmp_path, start=tmp_path) is None
    assert contract.server.requests == []
    assert not list(tmp_path.glob("**/*.md"))


def test_contract_token_absent_from_md_and_output(contract, tmp_path, capsys):
    contract.seed()
    result = upsert_task_record(contract.adapter.pull()[0], tmp_path / "items", today="2026-10-25")
    md_text = result.path.read_text(encoding="utf-8")
    captured = capsys.readouterr()

    assert contract.token not in md_text
    assert contract.token not in (captured.out + captured.err)
    # The server DID receive the token (auth really sent, not dropped).
    assert contract.auth_carries_token(contract.server.requests[0]["authorization"])


def test_contract_http_error_hides_token(contract):
    contract.fail(401)
    with pytest.raises(TrackerError) as excinfo:
        contract.adapter.pull()
    msg = str(excinfo.value)
    assert "401" in msg
    assert contract.token not in msg
    assert "Basic" not in msg and "Bearer" not in msg
