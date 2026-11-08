"""Every frontmatter field a consumer reads must survive the snapshot.

Why the fixtures are synthetic
------------------------------
``Todo/items/`` is gitignored (``.gitignore:78``), so in a worker checkout it is
not merely empty — **the directory does not exist**. A test that enumerated the
live vault would therefore count zero tasks and pass vacuously, asserting
nothing. Every behavioural test below builds its own vault under ``tmp_path``
and injects the root explicitly.

For the same reason these tests deliberately do **not** use the ``isolated_vault``
fixture: it redirects module globals, which would make
``test_default_root_is_the_live_vault`` assert against a value it had itself
planted. (``conftest.py`` makes the same argument for graph_memory's CLI tests.)

Every expected value below differs from the value the field would take if it were
*absent*. That is what makes the assertions load-bearing: an exporter that
dropped a field entirely would fall back to the default and a same-as-default
expectation would still pass.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path

import pytest

from todo_system import cli as _cli
from todo_system import paths
from todo_system import task_export as tx

FULL_CONTRACT = """---
status: blocked
due: 2026-09-01
not_before: 2026-08-25
updated: 2026-08-24
outcome: "every contract field survives the snapshot"
next_action: "run the exporter"
if_then: "if it is monday then export the contract"
estimate_p50: 30m
estimate_p90: 60m
review_cycle: weekly
priority: 72.5
parent: "[[Todo/items/parent-task]]"
is_parent: true
worker_mode: auto
auto_merge: false
repo: "https://github.com/example-org/example.git"
working_dir: "/tmp/example"
long_job: "python collect.py --all"
bypass_guard: true
allowed_tools: "Read, Bash(git:*)"
model_tier: light
max_turns: 40
delegate: false
worker_branch: custom/branch-name
depends_on: [upstream-a, upstream-b]
on_unblock: quick
auto_activate: false
tags:
  - todo
  - work
acceptance_criteria:
  - first criterion
  - second criterion
---

# Full Contract Task

- [x] a finished step
- [ ] an unfinished step
"""

MINIMAL = """---
status: open
---

# Minimal Task

- [ ] the only step
"""

# frontmatter key -> (snapshot location, effective value the real consumer sees).
# Layer A goes to the DB; layers B and C have no DB column at all.
EXPECTED: dict[str, tuple[str, object]] = {
    # -- Layer A: cli.parse_task_file -> ParsedTask -> todo.db (cli.py:986-1009)
    "status": ("layer_a.status", "blocked"),
    "due": ("layer_a.due", "2026-09-01"),
    "not_before": ("layer_a.not_before", "2026-08-25"),
    "updated": ("layer_a.updated", "2026-08-24"),
    "outcome": ("layer_a.outcome", "every contract field survives the snapshot"),
    "next_action": ("layer_a.next_action", "run the exporter"),
    "if_then": ("layer_a.if_then", "if it is monday then export the contract"),
    "estimate_p50": ("layer_a.estimate_p50", "30m"),
    "estimate_p90": ("layer_a.estimate_p90", "60m"),
    "review_cycle": ("layer_a.review_cycle", "weekly"),
    "priority": ("layer_a.priority", 72.5),
    "parent": ("layer_a.parent_slug", "parent-task"),
    "is_parent": ("layer_a.is_parent", 1),
    "tags": ("layer_a.tags", ["todo", "work"]),
    "worker_mode": ("layer_a.worker_mode", "auto"),
    "auto_merge": ("layer_a.auto_merge", 0),
    # -- Layer B: orchestrator re-reads the markdown (repos.py:49-80)
    "repo": ("layer_b.repo", "https://github.com/example-org/example.git"),
    "working_dir": ("layer_b.working_dir", "/tmp/example"),
    "long_job": ("layer_b.long_job", "python collect.py --all"),
    "bypass_guard": ("layer_b.bypass_guard", True),
    "allowed_tools": ("layer_b.allowed_tools", "Read, Bash(git:*)"),
    "model_tier": ("layer_b.model_tier", "light"),
    # -- Layer C: only a running stage ever reads these
    "max_turns": ("layer_c.max_turns", 40),
    "delegate": ("layer_c.delegate_opt_out", True),
    "worker_branch": ("layer_c.worker_branch", "custom/branch-name"),
    "depends_on": ("layer_c.depends_on", ["upstream-a", "upstream-b"]),
    "on_unblock": ("layer_c.on_unblock", "quick"),
    "auto_activate": ("layer_c.auto_activate", False),
    "acceptance_criteria": (
        "layer_c.acceptance_criteria",
        ["first criterion", "second criterion"],
    ),
}

# The fields with no column in todo.db — anything rebuilding a task from the
# database alone loses exactly these (AC3).
DB_COLUMNLESS = (
    "repo",
    "working_dir",
    "long_job",
    "bypass_guard",
    "allowed_tools",
    "model_tier",
    "max_turns",
    "delegate",
    "worker_branch",
    "depends_on",
    "on_unblock",
    "auto_activate",
    "acceptance_criteria",
)


def _vault(tmp_path, **extra: str):
    """A throwaway vault holding the full-contract task plus a minimal one."""
    items = tmp_path / "Todo" / "items"
    items.mkdir(parents=True)
    (items / "full-contract.md").write_text(FULL_CONTRACT, encoding="utf-8")
    (items / "minimal.md").write_text(MINIMAL, encoding="utf-8")
    for name, content in extra.items():
        (items / f"{name}.md").write_text(content, encoding="utf-8")
    return tmp_path


def _dig(snapshot: dict, dotted: str):
    layer, field = dotted.split(".")
    return snapshot[layer][field]


def _snapshot_of(tmp_path, slug: str = "full-contract") -> dict:
    return next(s for s in tx.export_all(tmp_path) if s["slug"] == slug)


def _manifest(root) -> dict[str, tuple[int, str]]:
    """{relative path: (size, sha256)} for every file under ``root``."""
    return {
        str(p.relative_to(root)): (
            p.stat().st_size,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_contract_roster_covers_every_layer():
    """Guards the roster itself: 29 fields, 16 of them backed by a DB column."""
    assert len(EXPECTED) == 29
    assert len(DB_COLUMNLESS) == 13
    assert sum(1 for loc, _ in EXPECTED.values() if loc.startswith("layer_a")) == 16
    # Every columnless field must be a field we actually assert on.
    assert set(DB_COLUMNLESS) <= set(EXPECTED)


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_every_contract_field_survives(tmp_path, key):
    snapshot = _snapshot_of(_vault(tmp_path))
    location, expected = EXPECTED[key]

    # The raw YAML is preserved verbatim...
    assert key in snapshot["raw_frontmatter"], f"{key} missing from raw_frontmatter"
    # ...and the layer the real consumer reads carries the effective value.
    assert _dig(snapshot, location) == expected, f"{key} lost at {location}"


def test_db_columnless_fields_present(tmp_path):
    """AC3: the twelve fields with no DB column are the whole point."""
    snapshot = _snapshot_of(_vault(tmp_path))
    for key in DB_COLUMNLESS:
        location, expected = EXPECTED[key]
        assert _dig(snapshot, location) == expected, f"columnless field {key} lost"


def test_minimal_task_does_not_crash(tmp_path):
    """A task carrying almost no contract still exports, with defaults."""
    snapshot = _snapshot_of(_vault(tmp_path), slug="minimal")
    assert snapshot["layer_a"]["status"] == "open"
    assert snapshot["layer_b"]["repo"] is None
    assert snapshot["layer_c"]["worker_branch"] == "worker/minimal"
    assert snapshot["lossy"] == []


def test_snapshot_survives_a_json_round_trip(tmp_path):
    """The snapshot is the deliverable, so it has to serialise losslessly."""
    snapshots = tx.export_all(_vault(tmp_path))
    reloaded = json.loads(json.dumps(snapshots, default=str))
    # Compared without the raw layer: yaml.safe_load yields a datetime.date for
    # `due:`, which only `default=str` can encode and which therefore cannot
    # round-trip identically. The effective layers must survive exactly.
    for before, after in zip(snapshots, reloaded):
        for layer in ("layer_a", "layer_b", "layer_c", "lossy"):
            assert before[layer] == after[layer]


def test_list_form_allowed_tools_is_flagged_not_hidden(tmp_path):
    """The silent-loss case: repos._clean discards a YAML list (repos.py:43-46).

    Recording only the effective value would make this indistinguishable from a
    task that never declared any tools — the snapshot would conceal the bug.
    """
    list_form = FULL_CONTRACT.replace(
        'allowed_tools: "Read, Bash(git:*)"',
        "allowed_tools:\n  - Read\n  - Bash(gh:*)",
    )
    assert "allowed_tools:\n  - Read" in list_form, "fixture edit did not apply"
    vault = _vault(tmp_path, listform=list_form)
    snapshot = _snapshot_of(vault, slug="listform")

    assert snapshot["raw_frontmatter"]["allowed_tools"] == ["Read", "Bash(gh:*)"]
    assert snapshot["layer_b"]["allowed_tools"] is None
    assert "allowed_tools" in snapshot["lossy"]


def test_placeholder_dash_is_not_reported_as_lossy(tmp_path):
    """`-` is the vault's written-out 'deliberately empty'; dropping it loses
    nothing, so flagging it would drown the real losses in noise."""
    dashed = FULL_CONTRACT.replace(
        'repo: "https://github.com/example-org/example.git"', "repo: -"
    )
    snapshot = _snapshot_of(_vault(tmp_path, dashed=dashed), slug="dashed")
    assert snapshot["layer_b"]["repo"] is None
    assert "repo" not in snapshot["lossy"]


def test_layer_a_fallback_matches_primary(tmp_path, monkeypatch):
    """The tmp-path fallback must agree with cli.parse_task_file itself.

    cli.parse_task_file raises ValueError outside the live vault (relative_to at
    cli.py:988), so fixtures always take the fallback. Repointing cli.ROOT at the
    fixture vault forces the *primary* branch, pinning the two together — without
    writing anything to the real vault.
    """
    vault = _vault(tmp_path)
    md = vault / "Todo" / "items" / "full-contract.md"

    monkeypatch.setattr(_cli, "ROOT", vault)
    primary = tx._layer_a(md, vault)

    fallback = dataclasses.asdict(tx._parse_task_file_rooted(md, vault))
    fallback["subtasks"] = [[t, d] for t, d in fallback["subtasks"]]

    assert primary == fallback
    # Guard against both branches being trivially empty.
    assert primary["path"] == "Todo/items/full-contract.md"
    assert primary["title"] == "Full Contract Task"


# Since the engine split the vault carries data, not engine code, so "the default
# root contains todo_system/task_export.py" can no longer hold anywhere and was
# dropped; the default root is still pinned to cli.ROOT and to a real Todo/ tree.
# Byte-identity to parents[1] holds when the engine runs from a combined layout (a
# .macp/config.toml marker at the checkout root), which no real checkout carries any
# more, so it is retargeted at the ``combined_layout`` fixture (todo_system/conftest.py).


def test_default_root_is_the_live_vault():
    """AC5: the default really is the live vault, not a redirected stand-in.

    Without this, 'the default call writes nothing' could pass simply because the
    default pointed somewhere harmless.
    """
    assert tx.DEFAULT_VAULT_ROOT == _cli.ROOT
    # Since the engine split the vault carries data, not engine code: its Todo/ tree
    # is what the default export_all() reads (test_default_call_writes_nothing below
    # pins that it is non-empty).
    assert (tx.DEFAULT_VAULT_ROOT / "Todo").is_dir()


# DEFAULT_VAULT_ROOT now flows through the shared paths.vault_root resolver
# (public-split follow-up). test_default_root_is_the_live_vault above already pins
# it to cli.ROOT (which is likewise resolver-derived); these two add the explicit
# migration properties: (b) env-unset it is byte-identical to the old hard-coded
# Path(__file__).resolve().parents[1], and (a) the exact expression the module
# uses is redirectable via MACP_VAULT_ROOT. The constant is import-time, so we
# assert against the resolver called with the module's own start= anchor rather
# than reloading. (b) is split in two: inside a monkeypatch context the
# combined_layout stands in for the combined checkout and the resolver returns it
# unchanged; with the patch undone, on the real tree the anchor is parents[1].


def test_default_vault_root_is_byte_identical_repo_root_when_env_unset(
        monkeypatch, combined_layout, install_engine_at):
    env = os.environ.get("MACP_VAULT_ROOT")    # what the module saw at import
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        install_engine_at(m, combined_layout)
        # The exact expression task_export.py:66 evaluates: in a combined layout the
        # .macp marker at the install root makes the resolver return that root
        # unchanged — the value the old hard-coded parents[1] produced.
        assert paths.vault_search_start() == combined_layout
        assert paths.vault_root(start=paths.vault_search_start()) == combined_layout
    # real tree: the anchor is this module's own parents[1], a real engine root
    install = Path(tx.__file__).resolve().parents[1]
    assert paths.vault_search_start() == install
    assert (install / "todo_system" / "task_export.py").is_file()
    # The constant holds the resolver's import-time value (the env vault when a
    # worker stage set MACP_VAULT_ROOT before import).
    expected = (Path(env).expanduser() if env
                else paths.vault_root(start=paths.vault_search_start()))
    assert tx.DEFAULT_VAULT_ROOT == expected


def test_default_vault_root_is_redirectable_via_macp_vault_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path))
    install = Path(tx.__file__).resolve().parents[1]
    # Same expression task_export.py:66 evaluates — env wins over the anchor.
    assert paths.vault_root(start=install) == tmp_path


def test_export_all_writes_nothing_to_the_vault_it_reads(tmp_path):
    """Read-only, proven where the exporter is doing real work.

    A byte-level manifest is safe here because nothing else writes to tmp_path;
    the live-vault test below cannot make the same assumption.
    """
    vault = _vault(tmp_path)
    before = _manifest(vault)

    snapshots = tx.export_all(vault)

    assert len(snapshots) == 2, "fixture vault produced no work — assertion vacuous"
    assert _manifest(vault) == before


def test_default_call_writes_nothing(tmp_path):
    """AC5: a bare export_all() creates nothing in the live vault.

    Compares the *set of paths* rather than sizes or mtimes: the host runs
    todo-sync on a timer, which legitimately rewrites Todo/NOW.md, FOCUS.md and
    todo.db while pytest is running. conftest.py's real-vault guard is written
    the same way and for the same reason. File *creation* is the failure mode
    that matters for a read-only exporter, and this catches it.
    """
    todo_dir = tx.DEFAULT_VAULT_ROOT / "Todo"
    before = {str(p) for p in todo_dir.rglob("*")}
    assert before, "live Todo/ tree is empty — nothing is being watched"

    tx.export_all()

    assert {str(p) for p in todo_dir.rglob("*")} == before


def test_main_writes_only_when_asked(tmp_path, capsys):
    """The library never writes; main() writes only with an explicit --out."""
    vault = _vault(tmp_path)
    out = tmp_path / "snapshots" / "contract.json"

    assert tx.main(["--vault-root", str(vault)]) == 0
    assert not out.exists()
    stdout_payload = json.loads(capsys.readouterr().out)
    assert len(stdout_payload) == 2

    assert tx.main(["--vault-root", str(vault), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == stdout_payload
