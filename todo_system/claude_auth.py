"""Auth environment for headless `claude` invocations.

Why this exists: a Keychain OAuth refresh token has an **absolute expiry**
(it is not extended by renewal). Once it expires, the token is cleared to an
empty string and headless `claude -p` dies wholesale — with no symptom, so it
is only discovered days later. This actually happened on 2026-08-15, and the
2026-09-07 re-login only pushed the expiry out 19 days, not 90 (9/15 → 10/4).

Fix: keep a long-lived bearer token issued by `claude setup-token` at
``<config dir>/automation-token`` (600) and inject it via
`CLAUDE_CODE_OAUTH_TOKEN`. It's subscription-based, so no API billing kicks
in. Design rationale: scripts/REMOTE_CONTROL_SETUP.md (this convention is
already used by claude_remote_control.sh; reused here).

The config dir belongs to the **active Claude account**
(`macp.config.current_account`: project `.macp` pin → profile
`active_account` → priority). If there is no account, it falls back to the
previous single dir (`macp.accounts.LEGACY_CONFIG_DIR`; the module constant
``TOKEN_FILE`` is that file, kept for compatibility — the actual resolution
goes through :func:`token_file`). Once an account resolves, :func:`claude_env`
applies both the config-dir layer and the token layer via
`accounts.claude_env_for`, and **removes** any inherited
`CLAUDE_CODE_OAUTH_TOKEN` if that account's dir has no token file (so another
account's token can't leak through).

**When there is no account and no file, nothing happens** — behavior is
unchanged before the token is issued, applies automatically once it is, and
rolls back instantly if the file is ever deleted.
"""

from __future__ import annotations

import os
from pathlib import Path

from .macp import accounts as _accounts

# The legacy single-dir token file, fixed at import time as before (the
# monkeypatch seam). Kept under this name for compatibility; :func:`token_file`
# is the resolver callers go through.
TOKEN_FILE = _accounts.legacy_config_dir() / "automation-token"


class MalformedToken(ValueError):
    """The token file's content doesn't look like a bearer token."""


def _looks_like_token(s: str) -> bool:
    """Minimal shape check for a bearer token.

    On 2026-09-07, running `pbpaste > <token file>` put **the command line
    itself** into the clipboard instead of the setup-token output. If it had
    been injected just because the file wasn't empty, every headless stage
    would have died from auth failure — a wrong token is worse than no token
    (no token falls back to Keychain). So if it doesn't look right, don't
    inject it, and fail loudly.
    """
    if not s or len(s) < 40:
        return False
    if any(c.isspace() for c in s):   # a token has no whitespace/newlines
        return False
    # a shell command, path, or URL got mixed in
    if any(bad in s for bad in ("&&", "|", " ", "/", "~", "$", "<", ">")):
        return False
    return True


def _current_account():
    """The active claude account (``macp.config.current_account`` from the cwd),
    or None. Never raises — a half-installed vault must not break auth."""
    try:
        from .macp.config import current_account
        return current_account()[0]
    except Exception:
        return None


def token_file() -> Path:
    """Where the automation token lives: ``<active account dir>/automation-token``,
    or the module-level :data:`TOKEN_FILE` (the legacy single dir — and the
    monkeypatch seam) when no account is configured."""
    acct = _current_account()
    return TOKEN_FILE if acct is None else _accounts.token_file_for(acct)


def read_automation_token(path: Path | None = None, *, strict: bool = True) -> str | None:
    """The token string, or None if missing or empty. Token content is never logged.

    If ``path`` isn't given, uses :func:`token_file` (the active account dir,
    or ``TOKEN_FILE`` if there's no account). If ``strict``, raises
    :class:`MalformedToken` for content with the wrong shape.
    """
    p = path or token_file()
    try:
        token = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None
    if not _looks_like_token(token):
        if strict:
            raise MalformedToken(
                f"{p} 내용이 bearer 토큰 형태가 아닙니다 (길이 {len(token)}). "
                "`claude setup-token` 출력만 넣어야 합니다 — 파일을 지우면 "
                "Keychain 인증으로 폴백합니다."
            )
        return None
    return token


def claude_env(base: dict | None = None, path: Path | None = None) -> dict:
    """The environment to pass to the `claude` subprocess.

    If ``path`` isn't given, this follows the **active account**
    (:func:`_current_account`): when there is one, ``macp.accounts.claude_env_for``
    applies both the config-dir layer (``CLAUDE_CONFIG_DIR``, only when the
    account dir isn't LEGACY) and the token layer (inject the account dir's
    token, or remove any inherited ``CLAUDE_CODE_OAUTH_TOKEN`` if it has
    none). When there is no account, or ``path`` is given explicitly, this
    behaves as before — inject the token if present, otherwise leave env
    **as is** (no removal: so account-less hosts relying on a global env
    token don't break).

    If ``ANTHROPIC_API_KEY`` is already set, the token layer is left alone —
    in that case the user is deliberately using the API billing path, and
    overwriting it here would silently switch who gets billed.
    """
    env = dict(os.environ if base is None else base)
    if path is None:
        acct = _current_account()
        if acct is not None:
            return _accounts.claude_env_for(acct, env)
    if env.get("ANTHROPIC_API_KEY"):
        return env
    # Don't inject a malformed-shape token — injecting it kills every stage
    # with an auth failure, while not injecting it quietly falls back to
    # Keychain. The latter is safe.
    # (claude_auth_watch separately raises a loud alert about the malformed token.)
    token = read_automation_token(path, strict=False)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def auth_source(env: dict | None = None) -> str:
    """Which credential is active right now — a one-word label for logs/health reports."""
    e = os.environ if env is None else env
    if e.get("ANTHROPIC_API_KEY"):
        return "api-key"
    if e.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "automation-token"
    return "keychain-oauth"
