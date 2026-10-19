"""Anthropic Admin Usage/Cost CLI — token tables and dollar costs from one line.

Purpose
-------
Answer "how many tokens / how many dollars did this work cost" without the
browser console. The Anthropic **Admin** API exposes two org-level reports:

- ``/v1/organizations/usage_report/messages`` — token counts bucketed by time
  and grouped by model / api key. This CLI's ``usage`` subcommand sums those and
  prices them with a built-in list-price table.
- ``/v1/organizations/cost_report`` — the amounts Anthropic actually billed, in
  **cents** (as decimal strings). The ``cost`` subcommand converts them to USD.

Design notes
------------
- **Stdlib only** (argparse / urllib / json / os / datetime), like
  ``todo_system.sms`` and ``session_log``, so ``python3 -m
  todo_system.anthropic_usage`` works from a bare clone without ``uv``. The
  engine-isolation gate (``scripts/check_engine_isolation.sh``) requires it.
- **Key never printed.** The admin key is read from ``ANTHROPIC_ADMIN_KEY`` or
  ``<vault>/.secrets/anthropic-admin-key`` and sent only in the ``x-api-key``
  header. It is never written to stdout, stderr, logs or exceptions.
- **Needs an organization.** A personal Anthropic account's key gets 401/403 on
  the Admin API; the owner switched the console account to an organization so
  the personal key in ``.secrets`` works. On 401/403 we print a one-line hint
  and exit 2.

Usage
-----
    python3 -m todo_system.anthropic_usage usage \\
        --since 2026-10-16T00:30Z --until 2026-10-16T00:40Z --bucket 1m
    python3 -m todo_system.anthropic_usage cost --day 2026-10-16
    # add --json for machine-readable output

Data lags live traffic by ~5 minutes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from todo_system import paths

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
USAGE_PATH = "/v1/organizations/usage_report/messages"
COST_PATH = "/v1/organizations/cost_report"

# Per-bucket row caps the API documents (also our default limit per request).
BUCKET_LIMITS = {"1m": 1440, "1h": 168, "1d": 31}

# List prices in USD per million tokens (MTok), by model-name prefix.
#
# Only ``claude-sonnet-5``'s input/output are pinned by a test: the 2026-10-16
# sample (uncached in 8,713 / out 5,949) must reprice to the billed total
# $0.0769, which back-solves to input $2.00 / output $10.00 per MTok. The other
# models and the cache tiers are the published list prices and the standard
# prompt-caching multipliers (5m write = 1.25x input, 1h write = 2x input, read
# = 0.1x input). Models with no entry render ``(no price)``.
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {
        "input": 2.00, "output": 10.00,
        "cache_write_5m": 2.50, "cache_write_1h": 4.00, "cache_read": 0.20,
    },
    "claude-opus-5-5": {
        "input": 15.00, "output": 75.00,
        "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50,
    },
    "claude-haiku-5-5": {
        "input": 1.00, "output": 5.00,
        "cache_write_5m": 1.25, "cache_write_1h": 2.00, "cache_read": 0.10,
    },
}

# Token-sum field -> price-table field. Keys match the normalized token dict
# built in _result_tokens(); order drives the usage table columns.
COST_FIELDS = {
    "uncached": "input",
    "cache_write_5m": "cache_write_5m",
    "cache_write_1h": "cache_write_1h",
    "cache_read": "cache_read",
    "output": "output",
}


class AuthError(Exception):
    """Raised on a 401/403 from the Admin API (missing org / wrong key)."""

    def __init__(self, code: int):
        super().__init__(f"admin API returned HTTP {code}")
        self.code = code


def admin_key() -> str | None:
    """The admin key from ``ANTHROPIC_ADMIN_KEY`` or ``.secrets``, else None.

    env wins; otherwise ``<vault>/.secrets/anthropic-admin-key`` (resolved via
    ``paths.vault_root()`` so MACP_VAULT_ROOT / an installed vault both work). A
    missing file or empty value yields None. The value is returned, never logged.
    """
    import os

    env = os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip()
    if env:
        return env
    try:
        key_file = paths.vault_root() / ".secrets" / "anthropic-admin-key"
    except paths.VaultRootError:
        return None
    try:
        value = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def price_for(model: str) -> dict[str, float] | None:
    """List prices for ``model`` by longest matching prefix, else None."""
    best: dict[str, float] | None = None
    best_len = -1
    for prefix, table in PRICES.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = table, len(prefix)
    return best


def _get_json(path: str, params: dict, key: str) -> dict:
    """GET one page of a report as parsed JSON.

    ``params`` values may be lists (``group_by[]``), serialized with
    ``doseq=True`` into repeated keys. On 401/403 prints a one-line hint and
    raises :class:`AuthError`; the key is never placed in the message.
    """
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{API_BASE}{path}?{query}" if query else f"{API_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={"x-api-key": key, "anthropic-version": API_VERSION},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(
                "Anthropic Admin API 인증 실패 (HTTP %d) — 조직 전환·Admin 키 필요 "
                "(개인 계정 키는 403)." % exc.code,
                file=sys.stderr,
            )
            raise AuthError(exc.code) from None
        raise
    return json.loads(body)


def _fetch(path: str, params: dict, key: str) -> list[dict]:
    """All result records across every page, flattened from ``data[].results[]``.

    Follows ``has_more``/``next_page`` by resubmitting with ``page=<next>``.
    """
    records: list[dict] = []
    page: str | None = None
    while True:
        query = dict(params)
        if page:
            query["page"] = page
        data = _get_json(path, query, key)
        for bucket in data.get("data") or []:
            records.extend(bucket.get("results") or [])
        if not data.get("has_more"):
            break
        page = data.get("next_page")
        if not page:
            break
    return records


def _result_tokens(result: dict) -> dict[str, int]:
    """Normalize one usage result's token counts into our fixed column set."""
    cache = result.get("cache_creation") or {}
    return {
        "uncached": int(result.get("uncached_input_tokens") or 0),
        "cache_write_5m": int(cache.get("ephemeral_5m_input_tokens") or 0),
        "cache_write_1h": int(cache.get("ephemeral_1h_input_tokens") or 0),
        "cache_read": int(result.get("cache_read_input_tokens") or 0),
        "output": int(result.get("output_tokens") or 0),
    }


def _cost_usd(tokens: dict[str, int], price: dict[str, float] | None) -> float | None:
    """Dollar cost of a token row given a price table, or None if unpriced."""
    if price is None:
        return None
    total = 0.0
    for col, price_key in COST_FIELDS.items():
        total += tokens[col] * price.get(price_key, 0.0) / 1_000_000
    return total


# ---- datetime helpers (RFC3339) --------------------------------------------

def _parse_dt(text: str) -> dt.datetime:
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _rfc3339(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(args: argparse.Namespace) -> tuple[str, str]:
    """(starting_at, ending_at) from --day or --since/--until, as RFC3339 Z."""
    if getattr(args, "day", None):
        start = dt.datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        return _rfc3339(start), _rfc3339(start + dt.timedelta(days=1))
    if not args.since or not args.until:
        raise SystemExit("need --day or both --since and --until")
    return _rfc3339(_parse_dt(args.since)), _rfc3339(_parse_dt(args.until))


# ---- table formatting -------------------------------------------------------

def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


# ---- usage subcommand -------------------------------------------------------

USAGE_COLUMNS = ["uncached", "cache_write_5m", "cache_write_1h", "cache_read", "output"]


def cmd_usage(args: argparse.Namespace) -> int:
    key = admin_key()
    if not key:
        print(
            "admin 키가 없다 — ANTHROPIC_ADMIN_KEY 또는 <vault>/.secrets/anthropic-admin-key 필요.",
            file=sys.stderr,
        )
        return 2
    start, end = _window(args)
    group = [g.strip() for g in args.group.split(",") if g.strip()]
    limit = args.limit or BUCKET_LIMITS.get(args.bucket, 1440)
    params: dict = {
        "starting_at": start,
        "ending_at": end,
        "bucket_width": args.bucket,
        "group_by[]": group,
        "limit": limit,
    }
    try:
        results = _fetch(USAGE_PATH, params, key)
    except AuthError:
        return 2

    agg: dict[tuple, dict] = {}
    for result in results:
        gkey = tuple(result.get(field) for field in group)
        entry = agg.get(gkey)
        if entry is None:
            entry = {
                "group": {field: result.get(field) for field in group},
                "model": result.get("model"),
                "api_key_id": result.get("api_key_id"),
                "tokens": {col: 0 for col in USAGE_COLUMNS},
            }
            agg[gkey] = entry
        tokens = _result_tokens(result)
        for col in USAGE_COLUMNS:
            entry["tokens"][col] += tokens[col]

    out_rows = []
    for entry in agg.values():
        model = entry["model"] or ""
        cost = _cost_usd(entry["tokens"], price_for(model) if model else None)
        out_rows.append({
            "group": entry["group"],
            "model": model or None,
            "api_key_id": entry["api_key_id"],
            "tokens": entry["tokens"],
            "cost_usd": cost,
        })
    out_rows.sort(key=lambda r: (r["model"] or "", r["api_key_id"] or ""))

    if args.json:
        print(json.dumps(out_rows, ensure_ascii=False))
        return 0

    headers = ["model", "api_key_id", *USAGE_COLUMNS, "cost"]
    table_rows = []
    for row in out_rows:
        cost = row["cost_usd"]
        cost_cell = f"${cost:.4f}" if cost is not None else "(no price)"
        table_rows.append([
            row["model"] or "-",
            row["api_key_id"] or "-",
            *(f"{row['tokens'][col]:,}" for col in USAGE_COLUMNS),
            cost_cell,
        ])
    if table_rows:
        _print_table(headers, table_rows)
    else:
        print("(no usage in window)")
    return 0


# ---- cost subcommand --------------------------------------------------------

def cmd_cost(args: argparse.Namespace) -> int:
    key = admin_key()
    if not key:
        print(
            "admin 키가 없다 — ANTHROPIC_ADMIN_KEY 또는 <vault>/.secrets/anthropic-admin-key 필요.",
            file=sys.stderr,
        )
        return 2
    start, end = _window(args)
    params: dict = {
        "starting_at": start,
        "ending_at": end,
        "bucket_width": "1d",
        "group_by[]": ["description"],
    }
    try:
        results = _fetch(COST_PATH, params, key)
    except AuthError:
        return 2

    rows = []
    total = 0.0
    for result in results:
        amount_usd = float(result.get("amount") or 0) / 100
        total += amount_usd
        rows.append({
            "description": result.get("description"),
            "model": result.get("model"),
            "token_type": result.get("token_type"),
            "amount_usd": amount_usd,
        })
    rows.sort(key=lambda r: r["amount_usd"], reverse=True)

    if args.json:
        print(json.dumps({"rows": rows, "total_usd": total}, ensure_ascii=False))
        return 0

    headers = ["description", "model", "token_type", "amount_usd"]
    table_rows = [
        [
            r["description"] or "-",
            r["model"] or "-",
            r["token_type"] or "-",
            f"${r['amount_usd']:.5f}",
        ]
        for r in rows
    ]
    if table_rows:
        _print_table(headers, table_rows)
    print(f"total: ${total:.5f}")
    return 0


# ---- argparse ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m todo_system.anthropic_usage",
        description="Query Anthropic Admin usage (tokens + list-price cost) and cost (billed USD).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    usage = sub.add_parser("usage", help="token table + list-price cost (usage_report/messages)")
    usage.add_argument("--since", help="RFC3339 start, e.g. 2026-10-16T00:30Z")
    usage.add_argument("--until", help="RFC3339 end")
    usage.add_argument("--day", help="YYYY-MM-DD (expands to that UTC day)")
    usage.add_argument("--bucket", choices=["1m", "1h", "1d"], default="1d")
    usage.add_argument("--group", default="model,api_key_id",
                       help="comma list of group_by fields (default model,api_key_id)")
    usage.add_argument("--limit", type=int, default=0,
                       help="rows per request (default: bucket cap)")
    usage.add_argument("--json", action="store_true")
    usage.set_defaults(func=cmd_usage)

    cost = sub.add_parser("cost", help="billed amounts in USD (cost_report, cents->dollars)")
    cost.add_argument("--since", help="RFC3339 start")
    cost.add_argument("--until", help="RFC3339 end")
    cost.add_argument("--day", help="YYYY-MM-DD (expands to that UTC day)")
    cost.add_argument("--json", action="store_true")
    cost.set_defaults(func=cmd_cost)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
