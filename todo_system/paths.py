"""Single source of truth for the MaestroACP vault root and daemon-label prefix.

Historically each engine module recomputed the vault root as
``Path(__file__).resolve().parents[1]``. This module collapses that into one
``vault_root()`` resolver so the root can be redirected (public-engine / vault
split, remote clones) without editing every call site.

Resolution order:

1. ``MACP_VAULT_ROOT`` environment variable, if set and non-empty.
2. The ``vault_root`` key of the nearest ``.macp/config.toml`` found by walking
   up from ``start`` (or the current working directory).
3. The directory *containing* that ``.macp/config.toml`` when the file exists
   but carries no ``vault_root`` key — i.e. the vault marker alone locates it.
4. Legacy fallback, now marker-gated: the repo root two levels up from this file
   (``Path(__file__).resolve().parents[1]``). When that directory holds a
   ``.macp/config.toml`` marker it is returned unchanged — **byte-identical** to
   the value the four engine modules used before this resolver existed, so an
   installed vault (this repo included) keeps resolving exactly the same root it
   does today. When the marker is absent the resolver raises ``VaultRootError``
   rather than guessing, naming the two fixes: set ``MACP_VAULT_ROOT`` or create
   ``.macp/config.toml`` at the vault root.

Why the cwd-upward marker is ``.macp/config.toml`` (a vault-specific file) and
not ``.git``: a worker stage can run with its cwd inside an *external* repo, and
that repo has no ``.macp/`` — so the upward search fails there and resolution
falls through to step 4. In a real deployment the install location carries the
``.macp`` marker, so that fallback still returns the vault root; only an install
with no marker at all — the original silent "wrong root" case — now fails loudly
instead of returning a bogus parent directory.

``label_prefix()`` resolves the launchd/systemd **label prefix** the same way
(env → ``.macp/config.toml`` key → a neutral ``com.macp.`` default), so the org
segment on every daemon label can be redirected in one place instead of being
hard-coded across ``daemonctl``, ``session_end``, ``worker`` and the install scripts.
An installed vault pins its real prefix via the config key, so neutralizing the
default leaves that vault's live labels unchanged.

``engine_root()`` resolves the *other* root — the directory holding the engine's
non-Python files (``scripts/``, ``.claude/commands/``). In a development checkout
(this repo, a ``git archive`` of the engine tree, the public export tree) that is
the checkout root beside ``todo_system/``, byte-identical to the per-module
``parents[N]`` constants it replaced; in an installed wheel it is
``todo_system/_engine/``, where ``pyproject.toml`` force-includes both trees in
repo layout. The wheel is recognised by that package-internal marker
(``is_installed()``), not by what happens to sit beside the package in
site-packages. It never consults ``MACP_VAULT_ROOT``: engine files ship with the
code, vault data does not.

``worker_root()`` and ``timezone()`` lift the last two **host assumptions** out of
the code — "external repos live under ``~/Worker``" and "the calendar runs on
Asia/Seoul" — into the same env → config-key → default ladder. Both anchor their
config search at ``vault_root_or_cwd()`` instead of the cwd, because the
orchestrator and runner have to read them while running from outside the vault.
``vault_root_or_cwd()`` is ``vault_root()`` with a cwd fallback, used to resolve
*relative CLI arguments* (``--ledger Worker/assignments``) against the vault
rather than against wherever the process happened to start.

``vault_search_start()`` is the ``start=`` anchor the engine modules hand to
``vault_root()`` for their import-time constants: the engine root in a checkout
(so the combined vault keeps finding itself with no env and no cwd involved), and
``None`` in a wheel, where anchoring on site-packages could only ever fail and
the documented env → cwd-upward order is the right one.

Stdlib-only on purpose. ``session_log`` runs from a bare ``git clone`` under
plain ``python3`` (no ``uv`` / project dependencies) and imports this module, so
nothing here may pull in a third-party or project package. TOML parsing uses the
stdlib ``tomllib`` (Python 3.11+); on older interpreters (macOS launchd's bare
``/usr/bin/python3`` is 3.9) a minimal line reader picks up top-level
``key = "string"`` / ``key = 'string'`` pairs, so a launchd daemon still resolves
the vault's pinned label prefix instead of silently falling to the neutral default.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


class VaultRootError(RuntimeError):
    """Raised when the vault root cannot be resolved and must not be guessed.

    Means :func:`vault_root` exhausted every branch: no ``MACP_VAULT_ROOT`` env
    var, no ``.macp/config.toml`` on the cwd-upward search path, and no
    ``.macp/config.toml`` marker beside the installed package either. Raising
    beats blindly returning ``Path(__file__).parents[1]``, which used to hand
    back a bogus directory whenever the code ran from outside an installed vault.
    """


def _read_config_key(cfg: Path, key: str) -> str | None:
    """Return ``key``'s string value from a ``.macp/config.toml``, else None.

    Returns None (never raises) when the file cannot be read/parsed, or the key
    is missing or not a non-empty string. Callers treat None as "no key" and fall
    back to their own default. Without ``tomllib`` (Python < 3.11) the
    :func:`_read_config_key_minimal` line reader answers instead.
    """
    try:
        import tomllib
    except ImportError:
        return _read_config_key_minimal(cfg, key)
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _read_config_key_minimal(cfg: Path, key: str) -> str | None:
    """tomllib-less fallback: a top-level single-line string ``key``, else None.

    Reads only lines before the first ``[table]`` header of the form
    ``key = "value"`` (basic string without escapes) or ``key = 'value'``
    (literal string), optionally followed by a ``#`` comment. Anything richer —
    numbers, arrays, escapes — reads as "no key", matching the tomllib path's
    non-string rule.
    """
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    line_re = re.compile(
        r"""^\s*""" + re.escape(key)
        + r"""\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?$""")
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            break
        m = line_re.match(line)
        if m:
            value = m.group(1) if m.group(1) is not None else m.group(2)
            return value or None
    return None


def vault_root(start: Path | None = None) -> Path:
    """Resolve the vault root; see the module docstring for the resolution order.

    ``start`` is a test seam for the cwd-upward search (drive it against a
    throwaway ``tmp_path``). Runtime callers invoke ``vault_root()`` with no
    arguments, so the search begins at the current working directory.
    """
    env = os.environ.get("MACP_VAULT_ROOT")
    if env:
        return Path(env).expanduser()

    base = (start or Path.cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = _read_config_key(cfg, "vault_root")
            return Path(key).expanduser() if key else d

    repo_root = Path(__file__).resolve().parents[1]
    if (repo_root / ".macp" / "config.toml").is_file():
        return repo_root
    raise VaultRootError(
        "cannot resolve the MaestroACP vault root: MACP_VAULT_ROOT is unset, no "
        ".macp/config.toml was found on the search path, and the install "
        f"location {repo_root} has no .macp/config.toml marker either. Fix it by "
        "setting MACP_VAULT_ROOT to the vault root, or by creating "
        ".macp/config.toml at the vault root."
    )


def _package_dir(package_dir: Path | None) -> Path:
    """The ``todo_system/`` directory to resolve layouts from (resolved)."""
    return package_dir.resolve() if package_dir is not None else Path(__file__).resolve().parent


def is_installed(package_dir: Path | None = None) -> bool:
    """True when this package runs from an installed wheel, False in a checkout.

    The test is the package-internal marker ``todo_system/_engine/`` — the
    directory ``pyproject.toml`` force-includes ``scripts/`` and
    ``.claude/commands/`` into when the wheel is built. A development checkout,
    a ``git archive`` of the engine tree and the public export tree have no such
    directory (``.gitignore`` keeps a stray copy from ever being tracked), and
    nothing *outside* the package is consulted, so a third-party ``scripts/``
    directory at the top of site-packages cannot flip the answer.

    ``package_dir`` is the same test seam as :func:`engine_root`.
    """
    return (_package_dir(package_dir) / "_engine").is_dir()


def engine_root(package_dir: Path | None = None) -> Path:
    """Resolve the directory holding the engine's non-Python files
    (``scripts/``, ``.claude/commands/``).

    * Installed wheel — ``todo_system/_engine/`` exists inside the package
      (:func:`is_installed`): ``pyproject.toml`` force-includes both trees under
      it in repo layout, so that directory is the root. Checked first, so a
      ``scripts/`` directory that some other distribution dropped at the top of
      site-packages cannot turn an installed wheel into a "checkout".
    * Development checkout — this repo, a ``git archive`` of the engine tree,
      the public export tree: no marker, and ``todo_system/`` sits beside
      ``scripts/``, so the answer is the checkout root,
      ``Path(__file__).resolve().parents[1]`` — byte-identical to the per-module
      ``_ENGINE_ROOT`` / ``_INSTALL_DIR`` constants this replaces.
    * Neither layout recognisable: still the ``_engine`` slot — never the bare
      ``site-packages`` directory.

    Immune to ``MACP_VAULT_ROOT`` by design: engine files ship with the code.
    ``package_dir`` is a test seam (the ``todo_system/`` directory to resolve
    from); runtime callers pass nothing. Stdlib-only and 3.9-safe like the rest
    of this module (bare ``python3`` daemons import it).
    """
    pkg = _package_dir(package_dir)
    engine_dir = pkg / "_engine"
    if engine_dir.is_dir():
        return engine_dir
    checkout = pkg.parent
    if (checkout / "scripts").is_dir():
        return checkout
    return engine_dir


def vault_search_start(package_dir: Path | None = None) -> Path | None:
    """The ``start=`` anchor for the engine modules' import-time ``vault_root()``.

    * Development checkout: :func:`engine_root` — byte-identical to the
      ``Path(__file__).resolve().parents[N]`` anchor every module spelled out
      before, so the combined vault (engine checkout == vault) resolves itself
      with no env set and no cwd involved, and a bogus ``.macp/config.toml`` in
      the cwd cannot capture it.
    * Installed wheel: ``None``, which makes :func:`vault_root` follow its
      documented order — ``MACP_VAULT_ROOT``, then the cwd-upward marker search
      (a process started inside the vault finds it with no env at all). The
      site-packages anchor it replaces could never hold a vault marker, so the
      only thing it ever did there was ignore the cwd and raise. Without env and
      outside a vault the resolver still raises :class:`VaultRootError`, by
      design — never a guess.

    ``package_dir`` is the same test seam as :func:`engine_root`.
    """
    if is_installed(package_dir):
        return None
    return engine_root(package_dir)


_DEFAULT_LABEL_PREFIX = "com.macp."


def label_prefix(start: Path | None = None) -> str:
    """Resolve the launchd/systemd label prefix — the org segment shared by every
    daemon label (default ``com.macp.``). The returned value **includes its
    trailing separator**, so callers concatenate it straight onto the product
    segment: ``f"{label_prefix()}secondbrain.orchestrator"``.

    Resolution order mirrors :func:`vault_root`:

    1. ``MACP_LABEL_PREFIX`` environment variable, if set and non-empty.
    2. The ``label_prefix`` key of the nearest ``.macp/config.toml`` found by
       walking up from ``start`` (or the current working directory).
    3. ``_DEFAULT_LABEL_PREFIX`` (``com.macp.``) — a **neutral** default for a fresh
       engine clone that has neither env nor a config key. An installed vault pins
       its own prefix via the config key (step 2), so its live labels are unaffected
       by neutralizing this default.

    Unlike :func:`vault_root`, a bare ``.macp`` marker carries no usable prefix, so
    a config file without the key falls through to the default rather than deriving
    anything from the marker directory. ``start`` is the same test seam.
    """
    env = os.environ.get("MACP_LABEL_PREFIX")
    if env:
        return env

    base = (start or Path.cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = _read_config_key(cfg, "label_prefix")
            if key:
                return key
            break

    return _DEFAULT_LABEL_PREFIX


_DEFAULT_PRIMARY_CALENDAR_ID = ""


def primary_calendar_id(start: Path | None = None) -> str:
    """Resolve the primary Google Calendar id the todo-sync daemon writes to.

    Resolution order mirrors :func:`label_prefix`:

    1. ``MACP_CALENDAR_ID`` environment variable, if set and non-empty.
    2. The ``calendar_id`` key of the nearest ``.macp/config.toml`` found by
       walking up from ``start`` (or the current working directory).
    3. ``_DEFAULT_PRIMARY_CALENDAR_ID`` — a neutral **empty string**, so a fresh
       engine clone with no config carries no personal calendar. Callers that
       drive a real sync (the install scripts / unit generator) omit the
       ``--calendar-id`` argument when this is empty and let the CLI's own
       default apply, rather than syncing to a blank target.

    ``start`` is the same test seam as :func:`vault_root`.
    """
    env = os.environ.get("MACP_CALENDAR_ID")
    if env:
        return env

    base = (start or Path.cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = _read_config_key(cfg, "calendar_id")
            if key:
                return key
            break

    return _DEFAULT_PRIMARY_CALENDAR_ID


_DEFAULT_REPO_SLUG = "example-org/MaestroACP"


def repo_slug(start: Path | None = None) -> str:
    """Resolve the ``owner/repo`` GitHub slug the monitor/health tools query.

    Resolution order mirrors :func:`label_prefix`:

    1. ``MACP_REPO_SLUG`` environment variable, if set and non-empty.
    2. The ``repo_slug`` key of the nearest ``.macp/config.toml`` found by
       walking up from ``start`` (or the current working directory).
    3. ``_DEFAULT_REPO_SLUG`` (``example-org/MaestroACP``) — a neutral
       placeholder so a fresh engine clone carries no personal repo owner; the
       ``gh`` calls that consume it already fail soft when the slug does not
       resolve to a real repository.

    ``start`` is the same test seam as :func:`vault_root`.
    """
    env = os.environ.get("MACP_REPO_SLUG")
    if env:
        return env

    base = (start or Path.cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = _read_config_key(cfg, "repo_slug")
            if key:
                return key
            break

    return _DEFAULT_REPO_SLUG


def vault_root_or_cwd(start: Path | None = None) -> Path:
    """:func:`vault_root`, but falling back to the cwd instead of raising.

    The base for resolving **relative CLI arguments** (``--uri file://Worker/agents``,
    ``--ledger Worker/assignments``, ``--db Todo/todo.db``). Those used to resolve
    against ``Path.cwd()`` implicitly, which forced every orchestrator/runner
    invocation to run with its cwd *at* the vault; anchoring them here lets the
    same relative defaults work from any directory.

    Falls back to ``Path.cwd()`` on :class:`VaultRootError` so the worst case is
    **byte-identical to the pre-resolver behaviour**: an install with neither
    ``MACP_VAULT_ROOT`` nor a ``.macp`` marker keeps interpreting the arguments
    exactly as it does today instead of failing at argument-assembly time. Other
    exceptions pass through — only the "cannot resolve" case is a fallback case.

    ``start`` is :func:`vault_root`'s test seam, forwarded unchanged.
    """
    try:
        return vault_root(start=start)
    except VaultRootError:
        return Path.cwd()


_DEFAULT_WORKER_ROOT = "~/Worker"


def worker_root(start: Path | None = None) -> Path:
    """Resolve the directory external repositories are cloned under.

    Resolution order mirrors :func:`label_prefix`:

    1. ``MACP_WORKER_ROOT`` environment variable, if set and non-empty.
    2. The ``worker_root`` key of the nearest ``.macp/config.toml``.
    3. ``~/Worker`` — the historical hard-coded default, so a host that
       configures nothing keeps the clone layout it has today.

    Both configured forms go through ``expanduser()``, so ``~/w`` is accepted.

    Unlike :func:`label_prefix`, the config search starts at
    :func:`vault_root_or_cwd` rather than the cwd: the orchestrator and runner
    must resolve this knob while running from *outside* the vault (their cwd can
    be an external repo checkout, or ``/tmp``), where a cwd-upward walk would
    find no ``.macp`` marker at all. ``start``, when given, overrides that
    anchor and is the test seam.

    Resolved **per call**, never frozen into a module constant or a default
    argument — an import-time snapshot is read before any test or caller can set
    the env var, which is precisely the trap this resolver replaces.
    """
    env = os.environ.get("MACP_WORKER_ROOT")
    if env:
        return Path(env).expanduser()

    base = (start or vault_root_or_cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = _read_config_key(cfg, "worker_root")
            if key:
                return Path(key).expanduser()
            break

    return Path(_DEFAULT_WORKER_ROOT).expanduser()


_LOCALTIME_PATH = Path("/etc/localtime")
_ZONEINFO_DIR = Path("/usr/share/zoneinfo")


def _host_iana_timezone() -> str | None:
    """The host's local timezone as an IANA name (``Asia/Seoul``), or None.

    The stdlib has no "local IANA zone" API — ``time.tzname`` yields only
    abbreviations (``KST``), which Google Calendar rejects. Two sources that do
    carry the real name:

    1. ``TZ``, when it looks like an IANA identifier (contains ``/``, or is
       ``UTC``). A ``TZ`` holding a POSIX rule string (``KST-9``) is not a name
       and is skipped.
    2. ``/etc/localtime`` when it is a symlink into the zoneinfo database: its
       target relative to ``/usr/share/zoneinfo`` *is* the IANA name.

    Returns None when neither answers (a copied, non-symlink ``/etc/localtime``;
    macOS variants; a container with no tzdata). Callers treat None as "say
    nothing about the zone" rather than guessing a region.

    Never raises: ``paths`` is imported by bare-``python3`` daemons, so a
    missing or unreadable ``/etc`` must not take a daemon down. The two paths
    are module constants so tests can point them at a ``tmp_path``.
    """
    env = os.environ.get("TZ")
    if env and ("/" in env or env == "UTC"):
        return env
    try:
        if _LOCALTIME_PATH.is_symlink():
            target = Path(os.readlink(_LOCALTIME_PATH))
            if not target.is_absolute():
                target = (_LOCALTIME_PATH.parent / target).resolve()
            name = target.relative_to(_ZONEINFO_DIR).as_posix()
            return name or None
    except (OSError, ValueError):
        return None
    return None


def timezone(start: Path | None = None) -> str | None:
    """Resolve the IANA timezone calendar events are written in, or None.

    Resolution order:

    1. ``MACP_TIMEZONE`` environment variable, if set and non-empty.
    2. The ``timezone`` key of the nearest ``.macp/config.toml``.
    3. :func:`_host_iana_timezone` — the host's own zone, when it is
       discoverable as an IANA name.
    4. ``None``, meaning **"do not state a timezone"**. Calendar callers then
       omit the ``timeZone`` key from the event body and Google applies the
       target calendar's own zone, which is the correct reading of "host local"
       for a vault that has pinned nothing.

    The replacement for four hard-coded ``"Asia/Seoul"`` literals: a vault that
    wants that zone pins ``timezone = "Asia/Seoul"`` in ``.macp/config.toml``.

    Anchors its config search at :func:`vault_root_or_cwd`, not the cwd, for the
    same reason :func:`worker_root` does. ``start`` overrides that anchor and is
    the test seam. Resolved per call — the sync daemon is long-lived and should
    pick up a config edit without a restart.
    """
    env = os.environ.get("MACP_TIMEZONE")
    if env:
        return env

    base = (start or vault_root_or_cwd()).resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            key = _read_config_key(cfg, "timezone")
            if key:
                return key
            break

    return _host_iana_timezone()
