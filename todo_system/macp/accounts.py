"""Claude account layout — one ``CLAUDE_CONFIG_DIR`` per account, auth separate,
everything else shared.

Model
-----
A Claude Code *account* is an isolated ``CLAUDE_CONFIG_DIR``. Only the files
that carry authentication live in the account directory itself
(``.credentials.json``, ``.claude.json``, ``automation-token``) plus the caches
the CLI creates on its own. Every entry in :data:`SHARED_ENTRIES` — transcripts
and the per-project memory links under ``projects/``, settings, commands,
skills, hooks, plugins, keybindings, agents — is an **absolute symlink** into
one shared root, so switching accounts keeps ``--resume``, memory and tooling
continuous. Absolute targets on purpose: a relative link breaks as soon as one
of the two roots moves.

Resolution of the two roots happens **at call time** (never at import):

1. env ``MACP_CLAUDE_SHARED_ROOT`` / ``MACP_CLAUDE_ACCOUNTS_ROOT``
2. ``claude_shared_root`` / ``claude_accounts_root`` in the nearest
   ``.macp/config.toml`` walking up from ``start`` (or the cwd)
3. ``<home>/.claude-shared`` / ``<home>/.claude-accounts``

The account directory is ``<accounts_root>/<name>``. The legacy single config
dir and its sibling settings file are spelled out only by the two ``LEGACY_*``
constants below (tilde strings; expand with :func:`legacy_config_dir` /
:func:`legacy_claude_json`), so every other module imports them from here.

Resolution of the *current* account (:func:`resolve_active` /
:func:`active_account`, also at call time): the project's ``.macp/config.toml``
``account`` pin → the runtime override → the profile's ``active_account`` → the
lowest ``priority`` (ties: first listed) → None, every rule skipping an account
the runtime state marks unavailable. :func:`claude_env_for` turns the chosen
account into a subprocess env — ``CLAUDE_CONFIG_DIR`` unless the dir is the
legacy one, plus the account's ``automation-token`` as ``CLAUDE_CODE_OAUTH_TOKEN``
(or that key removed). Reading the profile for a location stays in ``config``
(``current_account`` and the ``active_*`` wrappers there), so this module stays
a leaf.

Runtime state (leaf ④): a host-local, git-ignored JSON file —
``<vault>/Worker/_state/claude_accounts.json``, overridable through
``MACP_CLAUDE_ACCOUNTS_STATE`` or a ``path=`` argument — records which accounts
are unavailable (``unavailable.<name>``: ``reason`` / ``since`` / ``until``, an
exhausted account until its window resets, an auth or subscription failure with
no expiry = *sticky* until its credentials change or ``macp account use``), the
``active_override`` the runner rotated to, the ``last_switch`` and
``paused_since`` (no account left). :func:`classify_capacity` turns stage output
into the reason, :func:`rotate_active` performs one switch, and
:func:`all_unavailable` / :func:`claim_paused_alert` back the usage guard's
PAUSED hold and the orchestrator's once-only alert. Every writer is
read-merge-write and never raises.

Constraints
-----------
Leaf module: stdlib only, no ``todo_system.macp.config`` import (config imports
this module), no side effects at import. ``.claude.json`` cannot be shared —
auth metadata and settings sit in one file — so :func:`seed_claude_json`
copies the non-auth keys once when an account is created and never syncs
afterwards. ``create_account_layout`` never deletes or replaces anything that is
already there; :func:`layout_drift` reports what deviates.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..paths import _read_config_key, vault_root, vault_search_start

# The pre-layout single config dir and the settings file that sits beside it.
# Tilde strings (not Paths) so the values stay byte-identical to the defaults
# the profile schema always used; expand via the helpers below.
LEGACY_CONFIG_DIR = "~/.claude"
LEGACY_CLAUDE_JSON = "~/.claude.json"

_DEFAULT_SHARED_DIRNAME = ".claude-shared"
_DEFAULT_ACCOUNTS_DIRNAME = ".claude-accounts"

ENV_SHARED_ROOT = "MACP_CLAUDE_SHARED_ROOT"
ENV_ACCOUNTS_ROOT = "MACP_CLAUDE_ACCOUNTS_ROOT"
CFG_SHARED_ROOT = "claude_shared_root"
CFG_ACCOUNTS_ROOT = "claude_accounts_root"

# Upstage (Solar) accounts authenticate with an API key, not a config dir. The
# key is read at CALL time from the env or a key file and is never stored on the
# account, in a profile, or in any output — see the key section at the bottom.
UPSTAGE_KEY_ENV = "UPSTAGE_API_KEY"
UPSTAGE_KEY_FILE_ENV = "MACP_UPSTAGE_KEY_FILE"
CFG_UPSTAGE_KEY_FILE = "upstage_key_file"
DEFAULT_UPSTAGE_KEY_FILE = "~/.config/macp/upstage.env"

# Entries of an account dir that are symlinks into the shared root.
SHARED_ENTRIES = (
    "projects",
    "settings.json",
    "settings.local.json",
    "commands",
    "skills",
    "hooks",
    "plugins",
    "keybindings.json",
    "agents",
)
# The directory-shaped subset: the shared side is created so the link is never
# dangling. File entries get a (possibly dangling) link — the CLI's first write
# then lands on the shared side.
SHARED_DIR_ENTRIES = frozenset(
    {"projects", "commands", "skills", "hooks", "plugins", "agents"})

# Per-account (never shared). The layout does not create these; the CLI / a
# human does.
ACCOUNT_ONLY_ENTRIES = (".credentials.json", ".claude.json", "automation-token")
# Per-account caches the CLI creates on its own; harmless to leave unshared.
ACCOUNT_CACHE_ENTRIES = (
    "statsig", "shell-snapshots", "debug", "todos", "file-history", "session-env")

# The non-auth keys of ``.claude.json`` that seed a new account's copy. A
# whitelist: everything else — ``oauthAccount``, ``userID``, subscription flags,
# anything unknown — is never copied.
CLAUDE_JSON_SEED_KEYS = (
    "projects",
    "theme",
    "editorMode",
    "autoUpdates",
    "claudeInChromeDefaultEnabled",
    "preferredNotifChannel",
    "hasCompletedOnboarding",
    "shiftEnterKeyBindingInstalled",
    "installMethod",
)


# ---- legacy paths -----------------------------------------------------------

def legacy_config_dir() -> Path:
    """:data:`LEGACY_CONFIG_DIR` expanded against the current home."""
    return Path(os.path.expanduser(LEGACY_CONFIG_DIR))


def legacy_claude_json() -> Path:
    """:data:`LEGACY_CLAUDE_JSON` expanded against the current home."""
    return Path(os.path.expanduser(LEGACY_CLAUDE_JSON))


# ---- helpers ----------------------------------------------------------------

def _abs(p: Path | str) -> Path:
    """Expand ``~`` and make absolute **without** following symlinks: a root that
    is itself a symlink is used as spelled, so link targets stay stable."""
    return Path(os.path.abspath(os.path.expanduser(str(p))))


def _check_name(name: str) -> str:
    """An account name is a single path component."""
    bad = (not name or name in (".", "..") or "/" in name or os.sep in name
           or (os.altsep is not None and os.altsep in name))
    if bad:
        raise ValueError(f"invalid account name: {name!r}")
    return name


def _resolve_root(env_key: str, cfg_key: str, default_dirname: str,
                  start: Path | str | None = None) -> Path:
    """env → nearest ``.macp/config.toml`` key → ``<home>/<default_dirname>``.

    Same walk as ``paths.label_prefix``: the first config file found decides —
    when it lacks the key the search stops and the default applies. A relative
    config value is taken relative to the directory holding that config file.
    """
    env = os.environ.get(env_key)
    if env:
        return Path(env).expanduser()
    base = (Path(start) if start is not None else Path.cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = _read_config_key(cfg, cfg_key)
            if key:
                p = Path(key).expanduser()
                return p if p.is_absolute() else d / p
            break
    return Path.home() / default_dirname


def resolve_shared_root(start: Path | str | None = None) -> Path:
    """The shared root every account's :data:`SHARED_ENTRIES` point into."""
    return _resolve_root(ENV_SHARED_ROOT, CFG_SHARED_ROOT, _DEFAULT_SHARED_DIRNAME, start)


def resolve_accounts_root(start: Path | str | None = None) -> Path:
    """The root holding one ``<name>/`` config dir per account."""
    return _resolve_root(ENV_ACCOUNTS_ROOT, CFG_ACCOUNTS_ROOT, _DEFAULT_ACCOUNTS_DIRNAME, start)


def account_dir(name: str, start: Path | str | None = None) -> Path:
    """``<accounts_root>/<name>`` — the account's ``CLAUDE_CONFIG_DIR``."""
    return resolve_accounts_root(start) / _check_name(name)


# ---- layout -----------------------------------------------------------------

def create_account_layout(name: str, *, accounts_root: Path | str | None = None,
                          shared_root: Path | str | None = None) -> Path:
    """Create ``<accounts_root>/<name>/`` with every :data:`SHARED_ENTRIES` item
    symlinked (absolute target) to the same name under the shared root.

    Directory entries get their shared side created; file entries are linked
    even when the shared side does not exist yet. Idempotent: an entry that
    already exists — symlink, regular file or directory — is left untouched,
    never replaced (data preservation first; :func:`layout_drift` reports the
    mismatch). Returns the account directory (absolute).
    """
    _check_name(name)
    shared = _abs(shared_root if shared_root is not None else resolve_shared_root())
    acct = _abs(accounts_root if accounts_root is not None else resolve_accounts_root()) / name
    shared.mkdir(parents=True, exist_ok=True)
    acct.mkdir(parents=True, exist_ok=True)
    for entry in SHARED_ENTRIES:
        target = shared / entry
        is_dir = entry in SHARED_DIR_ENTRIES
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
        link = acct / entry
        # is_symlink() first: a dangling file link reports exists() == False.
        if link.is_symlink() or link.exists():
            continue
        link.symlink_to(target, target_is_directory=is_dir)
    return acct


def layout_drift(acct_dir: Path | str, shared_root: Path | str | None = None) -> list[str]:
    """Report every :data:`SHARED_ENTRIES` item of ``acct_dir`` that is not the
    expected symlink: replaced by a regular file/directory (the CLI rewrites
    settings files atomically via rename, which turns a file link into a plain
    file), missing, or pointing elsewhere. Empty list = layout intact.

    Each line is ``"<entry>: <problem>"``; consumers print them verbatim.
    """
    acct = _abs(acct_dir)
    shared = _abs(shared_root if shared_root is not None else resolve_shared_root())
    problems: list[str] = []
    for entry in SHARED_ENTRIES:
        want = str(shared / entry)
        p = acct / entry
        if p.is_symlink():                       # before is_dir(): a dir link is_dir() too
            got = os.readlink(p)
            if got != want:
                problems.append(f"{entry}: symlink to {got}, expected {want}")
        elif p.is_dir():
            problems.append(f"{entry}: regular directory (expected symlink to {want})")
        elif p.exists():
            problems.append(f"{entry}: regular file (expected symlink to {want})")
        else:
            problems.append(f"{entry}: missing (expected symlink to {want})")
    return problems


# ---- .claude.json -----------------------------------------------------------

def claude_json_path(config_dir: Path | str) -> Path:
    """Where an account's ``.claude.json`` lives: beside the legacy single
    config dir (the pre-layout location), inside any other config dir."""
    d = _abs(config_dir)
    if d == _abs(legacy_config_dir()):
        return legacy_claude_json()
    return d / ".claude.json"


def seed_claude_json(src: Path | str, dst: Path | str) -> bool:
    """Copy only :data:`CLAUDE_JSON_SEED_KEYS` from ``src`` to a new ``dst``
    (mode 0600). One-shot: returns False without touching anything when ``dst``
    already exists, ``src`` is missing/unparsable/not an object, or none of the
    seed keys is present (no empty file is written). Prints nothing — the
    source holds auth material that must never reach a log.
    """
    src = Path(src)
    dst = Path(dst)
    if dst.exists() or dst.is_symlink():
        return False
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    subset = {k: data[k] for k in CLAUDE_JSON_SEED_KEYS if k in data}
    if not subset:
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(subset, f, indent=2)
        f.write("\n")
    return True


# ---- capacity signatures + runtime state (leaf ④) ----------------------------
#
# When a stage ends on the runner's capacity deferral, its output is classified
# here and the account is rotated through the runtime state file described in
# the module docstring. Stdlib only; the only I/O is that file plus two mtimes.

# Output signatures. Separate tuples on purpose: ``stages._CAPACITY_MARKERS``
# (the deferral gate) and ``model_select._UNAVAILABLE_MARKERS`` (the model
# cache) stay exactly as they are — this is the finer classification layered on
# top of the gate, never a replacement for it.
EXHAUSTED_MARKERS = ("out of usage credits", "usage limit", "weekly limit",
                     "hit your limit", "reached your", "/usage-credits")
AUTH_MARKERS = ("not logged in", "please run /login", "invalid api key",
                "oauth token", "authentication_error", "401")
NO_SUBSCRIPTION_MARKERS = ("no active subscription", "subscription expired",
                           "no active plan", "upgrade your plan", "/upgrade")
TRANSIENT_MARKERS = ("rate limit", "overloaded")

CAPACITY_EXHAUSTED = "exhausted"
CAPACITY_AUTH = "auth"
CAPACITY_NO_SUBSCRIPTION = "no_subscription"
CAPACITY_TRANSIENT = "transient"
# The kinds that make the runner rotate accounts; transient never does.
SWITCH_REASONS = (CAPACITY_EXHAUSTED, CAPACITY_AUTH, CAPACITY_NO_SUBSCRIPTION)
# Marks without an expiry — lifted by a credential change or `account use`.
STICKY_REASONS = (CAPACITY_AUTH, CAPACITY_NO_SUBSCRIPTION)
# An exhausted mark without a known window reset holds this long.
EXHAUSTED_DEFAULT_HOLD_S = 5 * 3600

ENV_RUNTIME_STATE = "MACP_CLAUDE_ACCOUNTS_STATE"
RUNTIME_STATE_PARTS = ("Worker", "_state", "claude_accounts.json")


def _has_marker(text: str, marker: str) -> bool:
    # A purely numeric marker ("401") must not match inside a longer number
    # (a line number, a byte count) — that would turn an exhausted stage into
    # a sticky auth mark.
    if marker.isdigit():
        return re.search(rf"(?<!\d){re.escape(marker)}(?!\d)", text) is not None
    return marker in text


def classify_capacity(text: str | None) -> str | None:
    """Classify a stage's output: ``"exhausted"`` (quota), ``"auth"`` (lost
    login / bad token), ``"no_subscription"`` (plan gone), ``"transient"``
    (rate limit / overload — never a reason to switch accounts) or None.

    Case-insensitive substring match; when several kinds match, the most
    specific wins: auth → no_subscription → exhausted → transient (a refusal
    that says "not logged in" and mentions ``/usage-credits`` is an auth
    problem). Pure, never raises."""
    t = (text or "").lower()
    if not t:
        return None
    for kind, markers in ((CAPACITY_AUTH, AUTH_MARKERS),
                          (CAPACITY_NO_SUBSCRIPTION, NO_SUBSCRIPTION_MARKERS),
                          (CAPACITY_EXHAUSTED, EXHAUSTED_MARKERS),
                          (CAPACITY_TRANSIENT, TRANSIENT_MARKERS)):
        if any(_has_marker(t, mrk) for mrk in markers):
            return kind
    return None


def iso_from_epoch(epoch: float) -> str:
    """Epoch seconds → UTC ISO-8601 string (the state file's time format)."""
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def epoch_from_iso(value) -> float | None:
    """The state file's ISO string (or an epoch number) → epoch seconds; None
    for None / unparseable. A naive timestamp is read as UTC. Never raises."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _mtime(path) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def runtime_state_path(path=None) -> Path | None:
    """Where the runtime state lives: ``path`` → ``MACP_CLAUDE_ACCOUNTS_STATE``
    → ``<vault root>/Worker/_state/claude_accounts.json`` (the vault resolved at
    call time, like every other root here). None when no vault can be resolved
    (an engine-only tree): then there is no state and nothing is written."""
    if path is not None:
        return Path(os.path.expanduser(str(path)))
    env = os.environ.get(ENV_RUNTIME_STATE)
    if env:
        return Path(os.path.expanduser(env))
    try:
        return vault_root(start=vault_search_start()).joinpath(*RUNTIME_STATE_PARTS)
    except Exception:
        return None


def load_runtime_state(path=None) -> dict:
    """The runtime state dict — ``{}`` for a missing, unreadable or malformed
    file (never raises; a non-object document reads as empty)."""
    p = runtime_state_path(path)
    if p is None:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_runtime_state(state: dict, path=None) -> bool:
    """Write the whole state (temp file + rename). False on any failure — the
    caller already made its decision; persistence is best-effort."""
    p = runtime_state_path(path)
    if p is None:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:
        return False


def _claude_accounts(accounts) -> list:
    return [a for a in (accounts or []) if getattr(a, "agent", "claude") == "claude"]


def _unavailable_map(state) -> dict:
    unav = state.get("unavailable") if isinstance(state, dict) else None
    return unav if isinstance(unav, dict) else {}


def _entry_matches(entry: dict, acct) -> bool:
    """The dir stamp: an entry applies to ``acct`` only when the config dir it
    was written for is the account's current dir, so a mark never follows a
    name to a different registration (and the real host's state file cannot
    leak into a test profile that reuses the name). An unstamped entry — one
    written without ``config_dir`` — applies by name."""
    stamp = entry.get("config_dir")
    if not stamp:
        return True
    return str(_abs(stamp)) == str(config_dir_for(acct))


def _released_by_credentials(entry: dict, acct) -> bool:
    """A sticky mark lifts once the account's auth material changed on disk:
    ``.credentials.json`` (a re-login, or the CLI refreshing its token) or
    ``automation-token`` (a replaced token file), compared with the mtimes
    recorded when the mark was set. A file still missing, or deleted since, is
    not a change — a logout must not release an auth mark. An entry that
    recorded no mtimes (marked without a dir) is released only by
    :func:`clear_unavailable`."""
    d = config_dir_for(acct)
    for key, name in (("cred_mtime", ".credentials.json"), ("token_mtime", "automation-token")):
        if key not in entry:
            continue
        cur = _mtime(d / name)
        if cur is not None and cur != entry.get(key):
            return True
    return False


def unavailable_entry(acct, state=None, now=None) -> dict | None:
    """The state entry that makes ``acct`` unavailable right now, else None:
    the ``unavailable.<name>`` record whose dir stamp matches, whose ``until``
    has not passed (a timed mark) or whose credentials are unchanged (a sticky
    one). ``state`` None reads the runtime state file. Never raises."""
    if acct is None:
        return None
    st = load_runtime_state() if state is None else state
    entry = _unavailable_map(st).get(getattr(acct, "name", ""))
    if not isinstance(entry, dict) or not _entry_matches(entry, acct):
        return None
    now = time.time() if now is None else float(now)
    until = epoch_from_iso(entry.get("until"))
    if until is not None:
        return None if now >= until else entry
    if _released_by_credentials(entry, acct):
        return None
    return entry


def unavailable_reason(acct, state=None, now=None) -> str | None:
    """``unavailable_entry(...)["reason"]`` or None when available."""
    entry = unavailable_entry(acct, state, now)
    return (str(entry.get("reason") or "unavailable")) if entry else None


def until_epoch(entry) -> float | None:
    """An unavailable entry's ``until`` as epoch seconds (None = sticky)."""
    return epoch_from_iso(entry.get("until")) if isinstance(entry, dict) else None


def available_accounts(accounts, state=None, now=None) -> list:
    """The subset of ``accounts`` (any agent) without a live unavailable entry,
    in the given order. ``state`` None reads the runtime state file."""
    st = load_runtime_state() if state is None else state
    now = time.time() if now is None else float(now)
    return [a for a in accounts if unavailable_entry(a, st, now) is None]


def _mark(state: dict, name: str, reason: str, until, config_dir, now: float) -> dict:
    """Put the unavailable entry for ``name`` into ``state`` (no I/O). Sticky
    reasons never carry an expiry; any other reason is timed — ``until`` (epoch),
    or ``now + EXHAUSTED_DEFAULT_HOLD_S`` when it is missing or already past
    (a stale snapshot must not produce a mark that is dead on arrival)."""
    if reason in STICKY_REASONS:
        until = None
    else:
        until = epoch_from_iso(until)
        if until is None or until <= now:
            until = now + EXHAUSTED_DEFAULT_HOLD_S
    entry = {"reason": reason, "since": iso_from_epoch(now),
             "until": iso_from_epoch(until) if until is not None else None,
             "config_dir": None}
    if config_dir:
        d = _abs(config_dir)
        entry["config_dir"] = str(d)
        entry["cred_mtime"] = _mtime(d / ".credentials.json")
        entry["token_mtime"] = _mtime(d / "automation-token")
    unav = dict(_unavailable_map(state))
    unav[name] = entry
    state["unavailable"] = unav
    return state


def mark_unavailable(name: str, reason: str, until=None, *, config_dir=None,
                     now: float | None = None, path=None) -> dict:
    """Record that account ``name`` cannot be used: ``reason`` is one of the
    :func:`classify_capacity` kinds; ``until`` (epoch) applies to a timed mark
    (``exhausted``: the window reset, default now + 5h), auth / no_subscription
    are always sticky. ``config_dir`` stamps the entry (see
    :func:`_entry_matches`) and records the credential mtimes the sticky
    release compares against. Read-merge-write; returns the new state. Never
    raises."""
    now = time.time() if now is None else float(now)
    state = load_runtime_state(path)
    _mark(state, str(name), str(reason), until, config_dir, now)
    _save_runtime_state(state, path)
    return state


def clear_unavailable(name: str, *, config_dir=None, drop_override: bool = True,
                      path=None) -> bool:
    """Lift ``name``'s mark by hand (``macp account use``): remove its entry
    (when ``config_dir`` is given, only an entry stamped with that dir or
    unstamped), drop the runtime override and the pause markers. Writes only
    when something changed; returns whether it did. Never raises."""
    state = load_runtime_state(path)
    changed = False
    unav = dict(_unavailable_map(state))
    entry = unav.get(name)
    if isinstance(entry, dict):
        stamp = entry.get("config_dir")
        if config_dir is None or not stamp or str(_abs(stamp)) == str(_abs(config_dir)):
            del unav[name]
            state["unavailable"] = unav
            changed = True
    if drop_override and (state.get("active_override") is not None
                          or state.get("active_override_dir") is not None):
        state["active_override"] = None
        state["active_override_dir"] = None
        changed = True
    for key in ("paused_since", "paused_notified_since"):
        if state.get(key) is not None:
            state[key] = None
            changed = True
    if changed:
        _save_runtime_state(state, path)
    return changed


def all_unavailable(accounts, *, state=None, now=None, path=None,
                    reconcile: bool = False) -> bool:
    """True when ``accounts`` holds at least one claude account and none of them
    is available — the PAUSED condition the usage guard's ``check()`` holds on.
    With ``reconcile=True`` (and the state read from the file, not passed in)
    the file's ``paused_since`` is kept in step: stamped when the pause begins,
    cleared when an account is usable again — written only on change, so the
    orchestrator's once-only alert (:func:`claim_paused_alert`) can tell a new
    pause from the one it already announced. Never raises."""
    cands = _claude_accounts(accounts)
    if not cands:
        return False
    now = time.time() if now is None else float(now)
    loaded = state is None
    st = load_runtime_state(path) if loaded else state
    paused = not available_accounts(cands, st, now)
    if reconcile and loaded:
        if paused and not st.get("paused_since"):
            st["paused_since"] = iso_from_epoch(now)
            _save_runtime_state(st, path)
        elif not paused and st.get("paused_since"):
            st["paused_since"] = None
            _save_runtime_state(st, path)
    return paused


def rotate_active(current: str, reason: str, *, until=None, profile, project=None,
                  now: float | None = None, path=None) -> dict | None:
    """One account switch, as the runner does it when a stage hit a capacity
    wall on ``current``: mark it unavailable (``reason`` / ``until`` as in
    :func:`mark_unavailable`), pick the next account with the normal rules among
    the ones still available and record it as ``active_override`` (an override
    outlives the mark that caused it — the old account becomes a *candidate*
    again when its window resets, but the override keeps winning until it is
    itself unavailable or ``macp account use`` resets it), or — nothing left —
    clear the override and stamp ``paused_since``. ``last_switch`` is updated
    either way.

    Returns ``{"from", "to" (None = paused), "reason"}`` for a switch that
    happened, None when nothing was done: ``current`` is not one of the
    profile's claude accounts, or it is already marked (so two runners failing
    on the same account produce one switch and one alert). Never raises."""
    now = time.time() if now is None else float(now)
    cands = _claude_accounts(getattr(profile, "accounts", None))
    cur = next((a for a in cands if getattr(a, "name", "") == current), None)
    if cur is None:
        return None
    state = load_runtime_state(path)
    if unavailable_entry(cur, state, now) is not None:
        return None
    _mark(state, current, str(reason), until, config_dir_for(cur), now)
    avail = available_accounts(cands, state, now)
    nxt = None
    if avail:
        cand, _source = resolve_active(profile, project, state, now)
        if cand is not None and any(cand is a for a in avail):
            nxt = cand
    if nxt is not None:
        state["active_override"] = getattr(nxt, "name", "")
        state["active_override_dir"] = str(config_dir_for(nxt))
        state["paused_since"] = None
    else:
        state["active_override"] = None
        state["active_override_dir"] = None
        state["paused_since"] = state.get("paused_since") or iso_from_epoch(now)
    to = getattr(nxt, "name", None) if nxt is not None else None
    state["last_switch"] = {"from": current, "to": to, "reason": str(reason),
                            "ts": iso_from_epoch(now)}
    _save_runtime_state(state, path)
    return {"from": current, "to": to, "reason": str(reason)}


def claim_paused_alert(path=None) -> bool:
    """True exactly once per pause: when ``paused_since`` is set and differs
    from the ``paused_notified_since`` recorded by the previous claim. The
    claim is persisted, so a second caller (or the next tick) gets False until
    the pause lifts and a new one begins. Never raises."""
    state = load_runtime_state(path)
    since = state.get("paused_since")
    if not since or state.get("paused_notified_since") == since:
        return False
    state["paused_notified_since"] = since
    _save_runtime_state(state, path)
    return True


# ---- account selection ------------------------------------------------------

def _priority(acct) -> int:
    try:
        return int(getattr(acct, "priority", 100))
    except (TypeError, ValueError):
        return 100


def pick_account(accounts, active_name: str = "", agent: str | None = "claude"):
    """Choose one account: the one named ``active_name`` if it is a candidate,
    else the lowest ``priority`` (ties: first listed), else None.

    Duck-typed over ``name`` / ``agent`` / ``priority`` attributes so the config
    module's ``Account`` works without this module importing it. ``agent=None``
    considers every account; the default keeps claude accounts only.
    """
    cands = [a for a in accounts
             if agent is None or getattr(a, "agent", "claude") == agent]
    if not cands:
        return None
    if active_name:
        for a in cands:
            if getattr(a, "name", "") == active_name:
                return a
    best = min(enumerate(cands), key=lambda t: (_priority(t[1]), t[0]))
    return best[1]


def resolve_active(profile, project=None, state=None, now=None) -> tuple[object | None, str]:
    """The account the current work runs as, plus *which rule* chose it:
    ``(account, source)`` with source ∈ ``pin`` | ``override`` | ``profile`` |
    ``priority`` | ``none``.

    Candidates are the profile's claude accounts (``agent == "claude"``, as in
    :func:`pick_account` and ``account env``). Order:

    1. ``pin`` — the project's ``.macp/config.toml`` ``account`` names a candidate
    2. ``override`` — the runtime state's ``active_override`` names a candidate
       (and its ``active_override_dir``, when stamped, is that account's dir)
    3. ``profile`` — the profile's ``active_account`` names a candidate
    4. ``priority`` — the lowest ``priority`` (ties: first listed)
    5. ``none`` — no candidate at all (``profile`` may be None)

    A pin / override / active_account that names no candidate (unknown, or a
    non-claude account) falls through to the next rule. ``state`` is the runtime
    state dict (:func:`load_runtime_state`): with one, every rule considers only
    the candidates that are available (:func:`unavailable_entry`, at ``now``) —
    and when *none* is, the filter is dropped and the rules run over all of
    them, so the stage env stays stable while :func:`all_unavailable` signals
    the pause separately. Without a state (None, the default) there is no
    runtime filter at all: pure, no filesystem access, never raises —
    ``config.current_account`` is the wrapper that reads the profile and the
    state for a location.
    """
    cands = [a for a in (getattr(profile, "accounts", None) or [])
             if getattr(a, "agent", "claude") == "claude"]
    if not cands:
        return None, "none"
    pool = cands
    if state is not None:
        avail = available_accounts(cands, state, now)
        if avail:
            pool = avail
    pin = getattr(project, "account", None) if project is not None else None
    if pin:
        for a in pool:
            if getattr(a, "name", "") == pin:
                return a, "pin"
    if state is not None:
        override = state.get("active_override") or ""
        if override:
            override_dir = state.get("active_override_dir") or ""
            for a in pool:
                if getattr(a, "name", "") == override and (
                        not override_dir or str(_abs(override_dir)) == str(config_dir_for(a))):
                    return a, "override"
    active = getattr(profile, "active_account", "") or ""
    if active:
        for a in pool:
            if getattr(a, "name", "") == active:
                return a, "profile"
    acct = pick_account(pool)
    if acct is not None:
        return acct, "priority"
    return None, "none"


def active_account(profile, project=None, state=None, now=None):
    """:func:`resolve_active` without the source — the account, or None."""
    return resolve_active(profile, project, state, now)[0]


def config_dir_for(acct) -> Path:
    """An account's ``CLAUDE_CONFIG_DIR`` as an absolute path (tilde expanded,
    symlinks NOT resolved — the :func:`_abs` spelling rule). ``None``, or an
    account without ``claude_config_dir``, means the legacy single dir."""
    d = getattr(acct, "claude_config_dir", "") if acct is not None else ""
    return _abs(d or LEGACY_CONFIG_DIR)


def is_legacy_config_dir(config_dir: Path | str) -> bool:
    """True when ``config_dir`` spells the legacy single dir
    (:data:`LEGACY_CONFIG_DIR`). Compared as spelled, without following
    symlinks: after migration the legacy path is a symlink to an account dir,
    and that account dir must still count as non-legacy (the CLI then gets an
    explicit ``CLAUDE_CONFIG_DIR``)."""
    return _abs(config_dir) == _abs(LEGACY_CONFIG_DIR)


def token_file_for(acct) -> Path:
    """``<config dir>/automation-token`` of ``acct`` (legacy dir for None)."""
    return config_dir_for(acct) / "automation-token"


def credentials_file_for(acct) -> Path:
    """``<config dir>/.credentials.json`` of ``acct`` (legacy dir for None)."""
    return config_dir_for(acct) / ".credentials.json"


def claude_env_for(acct, base: dict | None = None) -> dict:
    """The env a ``claude`` subprocess gets to run as ``acct`` — a new dict;
    ``base`` (default ``os.environ``) is never mutated.

    Two layers:

    * config dir — ``CLAUDE_CONFIG_DIR=<acct dir>``, **unless** the dir is the
      legacy single dir, which is left unset (forcing it makes the CLI ignore its
      normal auth store, e.g. the macOS Keychain — the rule
      ``config.build_invocation`` always applied).
    * token — when ``ANTHROPIC_API_KEY`` is set the token layer is not touched
      (the caller chose the API-billing path; the ``claude_auth.claude_env``
      rule). Otherwise ``<acct dir>/automation-token`` is read with
      ``claude_auth.read_automation_token(strict=False)``: a well-formed token is
      injected as ``CLAUDE_CODE_OAUTH_TOKEN``; a missing or malformed file
      REMOVES any ``CLAUDE_CODE_OAUTH_TOKEN`` inherited from ``base`` — a
      process-wide token (an env file) belongs to *some* account and must never
      leak into another account's session.
    """
    env = dict(os.environ if base is None else base)
    d = config_dir_for(acct)
    if not is_legacy_config_dir(d):
        env["CLAUDE_CONFIG_DIR"] = str(d)
    if env.get("ANTHROPIC_API_KEY"):
        return env
    # claude_auth owns the token-shape rule and imports THIS module at import
    # time, so the reverse edge is taken at call time via importlib: no Import
    # node (the leaf-module guard parses this file's AST) and nothing beyond
    # the stdlib is imported when this module loads.
    read_token = importlib.import_module("..claude_auth", __package__).read_automation_token
    token = read_token(d / "automation-token", strict=False)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    else:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


# ---- Upstage (Solar) API key -------------------------------------------------
#
# An Upstage account holds only *where* its key lives (a variable name and a
# file path) — never the value. The value is resolved at call time, env first,
# so it never reaches a profile TOML (``config.write_profile`` serializes
# ``Account.env`` verbatim), a log line, a commit or a PR. Display code asks
# :func:`key_source` for the ORIGIN only, the same "existence, never contents"
# rule the claude ``creds=yes/no`` column follows.

def upstage_key_file(account=None, start=None) -> Path:
    """Where the ``UPSTAGE_API_KEY`` key file lives, as an absolute path.

    ``MACP_UPSTAGE_KEY_FILE`` → the account's ``key_file`` → the nearest
    ``.macp/config.toml`` ``upstage_key_file`` → :data:`DEFAULT_UPSTAGE_KEY_FILE`.
    Same walk as :func:`_resolve_root`: the first config file found decides, and
    a relative value is taken against the directory holding that config. The
    path need not exist — this only spells it (:func:`_abs`, symlinks not
    followed). ``account`` is duck-typed on ``key_file`` so this works for any
    object, including one from a profile written before the field existed.
    """
    env = os.environ.get(UPSTAGE_KEY_FILE_ENV)
    if env:
        return _abs(env)
    on_account = str(getattr(account, "key_file", "") or "")
    if on_account:
        return _abs(on_account)
    base = (Path(start) if start is not None else Path.cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = _read_config_key(cfg, CFG_UPSTAGE_KEY_FILE)
            if key:
                p = Path(key).expanduser()
                return p if p.is_absolute() else _abs(d / p)
            break
    return _abs(DEFAULT_UPSTAGE_KEY_FILE)


def _warn_loose_key_file(path: Path) -> None:
    """One stderr line when the key file is readable beyond its owner. The PATH
    is named, never the contents; reading continues either way."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        print(f"warning: {path} is group/other-readable (expected 0600)",
              file=sys.stderr)


def read_key_file(path: Path | str, var: str = UPSTAGE_KEY_ENV) -> str | None:
    """``var``'s value from a ``KEY=VALUE`` env file, else None.

    Blank lines and ``#`` comments are skipped, a leading ``export`` is allowed,
    and one layer of surrounding quotes is stripped. A missing file, an
    unreadable one, or a file without ``var`` all read as None — like
    ``paths._read_config_key`` this never raises, because every caller's
    fallback is "no key".
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    _warn_loose_key_file(p)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != var:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value.strip() or None
    return None


def read_api_key(account=None, env=None, start=None) -> str | None:
    """The Upstage API key for a call, or None — env ``UPSTAGE_API_KEY`` first,
    then :func:`read_key_file` of :func:`upstage_key_file`. The return value is
    the secret: callers put it in a request header and nowhere else."""
    src = os.environ if env is None else env
    value = (src.get(UPSTAGE_KEY_ENV) or "").strip()
    if value:
        return value
    return read_key_file(upstage_key_file(account, start))


def key_source(account=None, env=None, start=None) -> str:
    """Where :func:`read_api_key` would get the key: ``env`` | ``file`` |
    ``missing``. The display-safe view — it returns the origin, never a value,
    so ``account list`` and ``account probe`` can report key availability
    without ever holding the secret."""
    src = os.environ if env is None else env
    if (src.get(UPSTAGE_KEY_ENV) or "").strip():
        return "env"
    if read_key_file(upstage_key_file(account, start)):
        return "file"
    return "missing"
