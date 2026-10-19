"""Console-script entry point for ``macp`` (the todo CLI).

``todo_system.cli`` resolves the vault root at import time (``ROOT =
paths.vault_root()``) and many modules read that constant, so the resolution
cannot move into ``main()`` without changing every caller. Without a vault the
import raised ``VaultRootError`` straight through the console-script shim and
a first-time user saw a traceback instead of the fix. This wrapper catches that
one error, prints the message plus a two-line quick start, and exits 2; with a
vault it is a transparent hand-off to ``cli.main``.
"""

from __future__ import annotations

import sys

QUICK_START = (
    "Quick start:\n"
    "  cp -r vault-template ~/my-vault      # from a macp checkout\n"
    "  export MACP_VAULT_ROOT=~/my-vault\n"
)


def main() -> int:
    from todo_system import paths

    try:
        from todo_system import cli
    except paths.VaultRootError as exc:
        sys.stderr.write(f"macp: {exc}\n\n{QUICK_START}")
        return 2
    return cli.main()


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
