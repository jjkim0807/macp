"""Console-script entry point for ``macp`` (the todo CLI + lite mode).

``todo_system.cli`` resolves the vault root at import time (``ROOT =
paths.vault_root()``) and many modules read that constant, so the resolution
cannot move into ``main()`` without changing every caller. Without a vault the
import raised ``VaultRootError`` straight through the console-script shim and
a first-time user saw a traceback instead of the fix. This wrapper catches that
one error, prints the message plus a short quick start, and exits 2; with a
vault it is a transparent hand-off to ``cli.main``.

Lite mode (``docs/design/lite-mode.md``) adds two things in front of that:

* The six lite verbs (``init add up status dashboard memory`` — names the todo
  CLI's argparse rejects) go to ``todo_system.lite`` before ``cli`` is imported,
  because ``macp init`` must run when no vault exists yet. Any other argv never
  imports ``todo_system.lite``.
* When nothing resolves a vault — exactly the case that exits 2 — and the lite
  default vault (:func:`default_vault`) exists, that vault is used. With no
  default vault the quick start now says ``macp init``.

``macp -h`` / ``macp --help`` prints the lite verbs first (:func:`verb_summary`),
then the todo CLI's own help when a vault resolves, and exits 0 either way.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

LITE_VERBS = ("init", "add", "up", "status", "dashboard", "memory")

QUICK_START = (
    "Quick start, inside a git repository you own:\n"
    "  macp init      # creates the vault ~/.macp/vault and registers the repository\n"
    "Or point MACP_VAULT_ROOT at an existing vault (a copy of vault-template).\n"
)


def default_vault() -> Path:
    """The lite-mode vault: ``vault`` under ``MACP_HOME`` (default ``~/.macp``,
    the same rule as ``todo_system.macp.config.HOME``)."""
    return Path(os.environ.get("MACP_HOME") or (Path.home() / ".macp")) / "vault"


def verb_summary() -> str:
    """The lite verbs with one line each — the commands the README teaches."""
    from todo_system.i18n import t
    lines = [t("lite.help.usage")]
    lines += [f"  {verb:<10} {t('lite.help.' + verb)}" for verb in LITE_VERBS]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in LITE_VERBS:
        from todo_system import lite
        return lite.main(args)

    from todo_system import paths

    if not os.environ.get("MACP_VAULT_ROOT"):
        try:
            paths.vault_root()
        except paths.VaultRootError:
            fallback = default_vault()
            if (fallback / "Todo" / "items").is_dir():
                os.environ["MACP_VAULT_ROOT"] = str(fallback)
    wants_help = bool(args) and args[0] in ("-h", "--help")
    if wants_help:
        # `macp --help` is the first thing a new user types: show the README's
        # verbs and exit 0 whether or not a vault exists yet.
        sys.stdout.write(verb_summary())
    try:
        from todo_system import cli
    except paths.VaultRootError as exc:
        if wants_help:
            from todo_system.i18n import t
            sys.stdout.write(f"\n{t('lite.help.no_vault')}\n")
            return 0
        sys.stderr.write(f"macp: {exc}\n\n{QUICK_START}")
        return 2
    if wants_help:
        from todo_system.i18n import t
        parser = cli.build_parser()
        parser.prog = "macp"
        sys.stdout.write(f"\n{t('lite.help.task_cli')}\n\n{parser.format_help()}")
        return 0
    return cli.main()


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
