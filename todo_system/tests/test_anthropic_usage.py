"""Tests for todo_system.anthropic_usage.

Network is stubbed: a fake ``urllib.request.urlopen`` returns queued JSON bodies
and captures every Request, so the tests pin token summation + list-price cost,
the cents->dollars conversion, pagination, and the key-absence / 403 paths
without touching the Admin API or the real key.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from todo_system import anthropic_usage


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Transport:
    """Fake urlopen: pops queued responses, records Request objects."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        nxt = self.responses.pop(0)
        if isinstance(nxt, urllib.error.HTTPError):
            raise nxt
        return _FakeResp(json.dumps(nxt).encode())

    def query(self, index: int) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.requests[index].full_url).query)


def _install(monkeypatch, responses) -> _Transport:
    transport = _Transport(responses)
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "test")
    return transport


def _usage_bucket(uncached, output, model="claude-sonnet-5", api_key_id="apikey_x"):
    return {"results": [{
        "model": model,
        "api_key_id": api_key_id,
        "uncached_input_tokens": uncached,
        "output_tokens": output,
    }]}


def test_usage_sums_buckets_and_prices_at_list_rate(monkeypatch, capsys):
    # 2026-10-16 sample: three 1m buckets sum to in 8,713 / out 5,949 for
    # claude-sonnet-5, billed $0.0769.
    transport = _install(monkeypatch, [{
        "data": [
            _usage_bucket(696, 1428),
            _usage_bucket(5951, 2213),
            _usage_bucket(2066, 2308),
        ],
        "has_more": False,
    }])
    rc = anthropic_usage.main([
        "usage", "--since", "2026-10-16T00:30Z", "--until", "2026-10-16T00:40Z",
        "--bucket", "1m", "--json",
    ])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    row = rows[0]
    assert row["tokens"]["uncached"] == 8713
    assert row["tokens"]["output"] == 5949
    assert round(row["cost_usd"], 4) == 0.0769

    # version header + group_by[] repeated-key serialization on the request.
    req = transport.requests[0]
    assert req.get_header("Anthropic-version") == "2023-06-01"
    assert transport.query(0)["group_by[]"] == ["model", "api_key_id"]


def test_unknown_model_renders_no_price(monkeypatch, capsys):
    _install(monkeypatch, [{
        "data": [_usage_bucket(100, 100, model="claude-future-9")],
        "has_more": False,
    }])
    rc = anthropic_usage.main(["usage", "--day", "2026-10-16", "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["cost_usd"] is None


def test_cost_converts_cents_string_to_dollars(monkeypatch, capsys):
    _install(monkeypatch, [{
        "data": [{"results": [
            {"description": "output tokens", "model": "claude-sonnet-5",
             "token_type": "output", "amount": "5.949"},
            {"description": "input tokens", "model": "claude-sonnet-5",
             "token_type": "input", "amount": "1.7426"},
        ]}],
        "has_more": False,
    }])
    rc = anthropic_usage.main(["cost", "--day", "2026-10-16", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    by_type = {r["token_type"]: r["amount_usd"] for r in out["rows"]}
    assert by_type["output"] == pytest.approx(0.05949)
    assert by_type["input"] == pytest.approx(0.017426)
    assert out["total_usd"] == pytest.approx(0.076916)


def test_pagination_follows_next_page(monkeypatch, capsys):
    transport = _install(monkeypatch, [
        {"data": [_usage_bucket(100, 200)], "has_more": True, "next_page": "p2"},
        {"data": [_usage_bucket(300, 400)], "has_more": False},
    ])
    rc = anthropic_usage.main(["usage", "--day", "2026-10-16", "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["tokens"]["uncached"] == 400  # 100 + 300 aggregated
    assert rows[0]["tokens"]["output"] == 600
    assert len(transport.requests) == 2
    assert transport.query(1)["page"] == ["p2"]


def test_missing_key_exits_2_without_network(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path))  # no .secrets/ here

    def _boom(*a, **k):
        raise AssertionError("must not hit the network when the key is absent")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    rc = anthropic_usage.main(["usage", "--day", "2026-10-16"])
    assert rc == 2
    assert "test" not in (capsys.readouterr().out)


def test_403_exits_2_and_never_prints_key(monkeypatch, capsys):
    _install(monkeypatch, [
        urllib.error.HTTPError("https://api.anthropic.com", 403, "Forbidden", {}, None),
    ])
    rc = anthropic_usage.main(["usage", "--day", "2026-10-16"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "test" not in (captured.out + captured.err)
