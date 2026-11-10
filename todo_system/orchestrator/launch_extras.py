"""Launch-intent extras for the two app-launch paths (screen_check / beta_smoke).

A sign-in-gated app launched with a bare ``monkey -c LAUNCHER`` stops on its
login screen, so every screenshot and every UI expectation sees the same gate
instead of the change under test. This module turns a per-repo declaration into
the ``--es`` / ``--ez`` / ``--ei`` argument tokens of an
``am start -n <pkg>/<activity>`` launch, so a debug build can be handed a test
session token (or an "skip onboarding" flag) and reach its real first screen.

Config shape (vault ``.macp/beta_publish_config.json``, inside the repo entry)::

    "screen_check": {                 # or "smoke" — see `declared`
      "activity": ".MainActivity",
      "launch_extras": [
        {"type": "string", "key": "session_token",
         "value_from_secret": ".secrets/demo-app.json#session_token"},
        {"type": "bool", "key": "skip_onboarding", "value": true}
      ]
    }

Secrets follow :func:`beta_publish.build_prop_args`'s rule exactly: the config
names the **file and the key**, never the value; the value is read at launch time
from a gitignored vault JSON. Three differences from ``build_prop_args``, all
deliberate:

1. **Never raises.** A missing file, unreadable JSON, absent key, bad type or a
   rejected identifier yields ``args=[]`` plus a one-line ``note`` — the caller
   then keeps its existing ``monkey`` command line byte-for-byte, so a
   misconfigured repo degrades to today's behaviour instead of failing a smoke
   gate or a screen check. (``build_prop_args`` raises because a beta APK built
   without its properties would ship broken; a launch without extras merely
   stops at the login screen.)
2. **All-or-nothing.** One bad item skips the whole extras set. A partially
   applied set would launch the app in a state no one declared.
3. **The resolved values come back** in ``Resolved.secrets`` so the caller can
   :func:`redact` them out of anything it persists (a node shell echoing its
   own command line into ``install.log`` is the real leak path).

Pure + stdlib-only at import (``check_engine_isolation.sh`` archives the tree and
imports it), and it imports no sibling orchestrator module — ``beta_smoke`` and
``screen_check`` both import *this*, so the dependency has to point one way.
``vault_root`` is therefore always passed in by the caller.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, NamedTuple

# `key` lands in a node shell AND a device `sh -c`, so it is allow-listed rather
# than escaped: identifier characters and dots only (mirrors
# ``beta_smoke._PKG_RE``'s reasoning about the same two interpolation points).
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# Like ``beta_smoke._PKG_RE`` (no quotes, spaces or `/`) but anchored on an
# identifier character, so a leading dot is refused. ``beta_smoke``'s version
# documents "no leading `.` (no `..` escape)" yet its `[A-Za-z0-9_.]+` accepts
# `..Main`; a package name never starts with a dot, so this one says so.
_PKG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.]*$")
# A relative activity (`.MainActivity`) or a fully qualified one. A leading `.`
# is allowed (that is the `am start -n <pkg>/.Activity` form) but `..`, `/`,
# quotes and spaces are not.
_ACTIVITY_RE = re.compile(r"^\.?[A-Za-z0-9_][A-Za-z0-9_.]*$")
# `am start` extra flags by declared type. An unknown type is a skip, not a
# guess — picking the wrong flag would silently hand the app a string "1".
_FLAGS = {"string": "--es", "bool": "--ez", "int": "--ei"}
# Config blocks searched, in order, for `launch_extras` / `activity`.
_BLOCKS = ("screen_check", "smoke")
# A 1-char secret would turn every occurrence of that character into `***`.
_MIN_REDACT_LEN = 2
# The ONE non-identifier package `am_start_command` accepts: the node-side shell
# variable `beta_smoke._install_and_launch` already assigns (`pkg=$(aapt2 dump
# packagename …)`) when the repo declares no package. Spelled out as a single
# allowed literal rather than loosening _PKG_RE, so nothing else shell-shaped can
# reach the command line through the package position.
PKG_SHELL_VAR = '"$pkg"'


class Resolved(NamedTuple):
    """``args`` — the ``--es/--ez/--ei`` tokens, empty when nothing resolved.
    ``secrets`` — the raw values read from a secret file, for :func:`redact`.
    ``note`` — a one-line, value-free reason when extras were declared but
    skipped (``None`` when nothing was declared or everything resolved)."""

    args: list[str]
    secrets: list[str]
    note: str | None


def _lookup(cfg: Any, key: str) -> Any:
    """``cfg["screen_check"][key]`` → ``cfg["smoke"][key]`` → ``cfg[key]``.

    Three positions because the two callers are handed different shapes: the
    runner passes the whole repo *entry* (whose launch settings may sit under
    either block — the AC allows both, and both launch paths must agree), while
    ``emulator_cli`` passes ``beta_smoke`` a repo scenario JSON whose keys are
    already at the top level. ``None`` when absent or when ``cfg`` is not a dict.
    """
    if not isinstance(cfg, dict):
        return None
    for block in _BLOCKS:
        sub = cfg.get(block)
        if isinstance(sub, dict) and sub.get(key) is not None:
            return sub.get(key)
    return cfg.get(key)


def declared(cfg: Any) -> list:
    """The declared ``launch_extras`` list, or ``[]``.

    ``[]`` covers every "not configured" shape (no cfg, not a dict, key absent,
    value not a list) — which is what keeps an unconfigured repo's launch command
    byte-identical to before."""
    raw = _lookup(cfg, "launch_extras")
    return list(raw) if isinstance(raw, list) else []


def activity(cfg: Any) -> str:
    """The declared launch ``activity`` (``""`` when absent/not a string).

    ``am start -n`` needs an explicit component; the node is NOT asked to resolve
    one (``cmd package resolve-activity``) because a wrong guess would launch
    some other screen and the failure would look like the app's. No activity ->
    :func:`am_start_command` returns ``None`` -> the caller keeps ``monkey``."""
    raw = _lookup(cfg, "activity")
    return raw.strip() if isinstance(raw, str) else ""


def _secret_value(spec: str, vault_root: Path | str) -> tuple[str | None, str]:
    """Read ``<vault-relative json path>#<field>`` -> ``(value, reason)``.

    ``value`` is ``None`` on any problem and ``reason`` then names the FILE and
    the FIELD only — never the value, not even its length. Vault escape is
    refused the same way as ``beta_publish.build_prop_args`` (resolve, then
    ``is_relative_to``), so a config cannot read ``../../.ssh/…``."""
    rel, sep, field = spec.partition("#")
    rel, field = rel.strip(), field.strip()
    if not sep or not rel or not field:
        return None, f"launch_extras value_from_secret must be '<path>#<field>': {rel or spec!r}"
    vault = Path(vault_root).resolve()
    src = (vault / rel).resolve()
    if not src.is_relative_to(vault):
        return None, f"launch_extras secret escapes the vault: {rel!r}"
    if not src.is_file():
        return None, f"launch_extras secret file missing: {rel} (needs field {field!r})"
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, f"launch_extras secret is not readable JSON: {rel}"
    if not isinstance(data, dict):
        return None, f"launch_extras secret must be a JSON object: {rel}"
    val = data.get(field)
    if not isinstance(val, str) or not val.strip():
        return None, f"launch_extras secret field {field!r} is empty or absent in {rel}"
    return val, ""


def _format(kind: str, raw: Any) -> tuple[str | None, str]:
    """Render ``raw`` as the string ``am start`` wants for ``kind``.

    ``bool`` accepts a real bool or the usual frontmatter-ish strings and emits
    ``true``/``false``; ``int`` must parse as an integer (a float or ``"1.5"`` is
    a skip, not a truncation); ``string`` takes any scalar. A rejected value
    returns ``None`` + a reason that names the TYPE, never the value."""
    if kind == "bool":
        if isinstance(raw, bool):
            return ("true" if raw else "false"), ""
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower(), ""
        return None, "a bool launch_extra needs true/false"
    if kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return None, "an int launch_extra needs an integer"
        try:
            return str(int(str(raw).strip())), ""
        except (TypeError, ValueError):
            return None, "an int launch_extra needs an integer"
    if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
        return str(raw), ""
    return None, "a string launch_extra needs a scalar value"


def _one(item: Any, vault_root: Path | str) -> tuple[list[str], list[str], str]:
    """One declared item -> ``(tokens, secrets, reason)``. ``reason`` non-empty
    means skip (and :func:`resolve` then drops the whole set)."""
    if not isinstance(item, dict):
        return [], [], "launch_extras items must be objects"
    kind = str(item.get("type") or "string").strip().lower()
    flag = _FLAGS.get(kind)
    if flag is None:
        return [], [], f"unknown launch_extras type {kind!r} (string|bool|int)"
    key = item.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key.strip()):
        return [], [], "a launch_extras key must match [A-Za-z_][A-Za-z0-9_.]*"
    key = key.strip()
    secrets: list[str] = []
    spec = item.get("value_from_secret")
    if isinstance(spec, str) and spec.strip():
        raw, reason = _secret_value(spec.strip(), vault_root)
        if raw is None:
            return [], [], reason
        secrets.append(raw)
    elif "value" in item:
        raw = item.get("value")
    else:
        return [], [], f"launch_extra {key!r} declares neither value nor value_from_secret"
    rendered, reason = _format(kind, raw)
    if rendered is None:
        return [], [], f"{reason} ({key!r})"
    # TWICE, and that is not a typo. The launch line is parsed by TWO shells: the
    # node shell runs it (gcloud hands `--command` to a remote shell), and then
    # `adb shell` joins its own argv back into one string for the DEVICE's `sh`.
    # One level of quoting is eaten by each, so a value with a space survives only
    # if it carries two. For a token-shaped value both passes are a no-op, so the
    # common case stays readable. `key` is allow-listed above, so it needs none.
    return [flag, key, shlex.quote(shlex.quote(rendered))], secrets, ""


def resolve(cfg: Any, *, vault_root: Path | str) -> Resolved:
    """Turn a repo's declaration into ``am start`` extra tokens. NEVER raises.

    All-or-nothing: the first unusable item drops the whole set (``args=[]``) and
    its value-free reason becomes ``note``, so the caller falls back to today's
    ``monkey`` launch rather than starting the app half-configured. ``secrets``
    carries the raw secret values for :func:`redact`, so they can be scrubbed
    from anything persisted even though they never appear in ``note``."""
    items = declared(cfg)
    if not items:
        return Resolved([], [], None)
    args: list[str] = []
    secrets: list[str] = []
    for item in items:
        try:
            tokens, found, reason = _one(item, vault_root)
        except Exception as e:      # noqa: BLE001 — a launch extra can only ever skip
            return Resolved([], [], f"launch_extras unreadable ({type(e).__name__})")
        if reason:
            return Resolved([], [], reason)
        args.extend(tokens)
        secrets.extend(found)
    return Resolved(args, secrets, None)


def valid_activity(activity_name: str) -> bool:
    """Does ``activity_name`` pass the allow-list? Callers that assemble the
    ``am start`` line somewhere else — ``screen_check`` hands the activity to the
    node script, which splices it next to the package IT resolved with aapt2 —
    validate it here so the one regex stays the single source of truth."""
    return bool(_ACTIVITY_RE.match((activity_name or "").strip()))


def am_start_command(pkg: str, activity_name: str, args: list[str]) -> str | None:
    """``am start -n <pkg>/<activity> <args…>``, or ``None``.

    ``None`` whenever anything is missing or rejected — no extras, no activity,
    an identifier that fails its allow-list — and the caller MUST then keep its
    existing ``monkey -c LAUNCHER`` command line unchanged. That is the whole
    regression contract: a repo that declares nothing launches exactly as it did
    before this module existed. Pure, so the command shape is pinned by unit
    tests without a fake node."""
    if not args:
        return None
    pkg, activity_name = (pkg or "").strip(), (activity_name or "").strip()
    if not valid_activity(activity_name):
        return None
    if pkg != PKG_SHELL_VAR and not _PKG_RE.match(pkg):
        return None
    return f"am start -n {pkg}/{activity_name} " + " ".join(args)


def extras_args(cfg: Any, *, vault_root: Path | str) -> str:
    """``resolve(...).args`` joined into one shell-ready string (``""`` when
    nothing resolved) — the form the node script reads from
    ``APP_TEST_LAUNCH_EXTRAS``."""
    return " ".join(resolve(cfg, vault_root=vault_root).args)


def redact(text: str, secrets: list[str] | None) -> str:
    """Replace every secret value in ``text`` with ``***``.

    Applied to anything persisted from a node round-trip (``install.log``), since
    a remote shell may echo its own command line. Secrets shorter than
    ``_MIN_REDACT_LEN`` are ignored on purpose: redacting a 1-char value would
    pepper the whole log with ``***`` and destroy the diagnostics the log exists
    for."""
    out = text or ""
    for s in secrets or []:
        if isinstance(s, str) and len(s) >= _MIN_REDACT_LEN:
            out = out.replace(s, "***")
    return out
