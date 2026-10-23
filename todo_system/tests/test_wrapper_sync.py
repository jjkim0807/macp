"""worker.sync_wrappers(): the maintenance tick keeps the ~/.local/bin PATH
wrappers in step with the engine checkout, so a stage's `Bash(macp-*:*)` rule
never resolves to a missing or stale script.

The motivating failure (2026-10-23): macp-remote-gate shipped in the engine's
scripts/ and was granted to stages, but had never been installed on this host,
so the stage hit `exit 127`. These tests drive explicit tmp engine/bindir seams
— nothing writes into the real ~/.local/bin.
"""

from __future__ import annotations

import stat

from todo_system import worker

WRAPPERS = ("macp-sms", "macp-browser", "macp-remote-gate", "macp-emulator")


def _engine(tmp_path):
    """A tmp engine checkout with a scripts/ dir holding all four fake wrappers."""
    scripts = tmp_path / "engine" / "scripts"
    scripts.mkdir(parents=True)
    for name in WRAPPERS:
        (scripts / name).write_text(f"#!/usr/bin/env bash\n# {name} v1\n", encoding="utf-8")
    return tmp_path / "engine"


def test_all_four_wrapper_names_are_covered():
    assert set(worker._WRAPPER_NAMES) == set(WRAPPERS)


def test_missing_dest_is_installed_0755(tmp_path):
    engine = _engine(tmp_path)
    bindir = tmp_path / "bin"              # does not exist yet
    synced = worker.sync_wrappers(engine=engine, bindir=bindir)
    assert set(synced) == set(WRAPPERS)    # every wrapper absent → every one installed
    for name in WRAPPERS:
        dest = bindir / name
        assert dest.is_file()
        assert dest.read_bytes() == (engine / "scripts" / name).read_bytes()
        assert stat.S_IMODE(dest.stat().st_mode) == 0o755


def test_exit_127_repro_installs_the_one_missing_wrapper(tmp_path):
    """A wrapper present in the engine but absent from bindir gets installed —
    the exact macp-remote-gate gap that produced exit 127."""
    engine = _engine(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("macp-sms", "macp-browser", "macp-emulator"):   # three already present
        dest = bindir / name
        dest.write_bytes((engine / "scripts" / name).read_bytes())
        dest.chmod(0o755)
    synced = worker.sync_wrappers(engine=engine, bindir=bindir)
    assert synced == ["macp-remote-gate"]  # only the missing one
    assert (bindir / "macp-remote-gate").is_file()
    assert stat.S_IMODE((bindir / "macp-remote-gate").stat().st_mode) == 0o755


def test_identical_dest_is_not_rewritten(tmp_path):
    engine = _engine(tmp_path)
    bindir = tmp_path / "bin"
    worker.sync_wrappers(engine=engine, bindir=bindir)           # first install
    mtimes = {n: (bindir / n).stat().st_mtime_ns for n in WRAPPERS}
    synced = worker.sync_wrappers(engine=engine, bindir=bindir)  # steady state
    assert synced == []                     # nothing changed → nothing (re)installed
    for n in WRAPPERS:
        assert (bindir / n).stat().st_mtime_ns == mtimes[n]      # bytes-equal → untouched


def test_changed_dest_is_overwritten_and_only_that_one_returned(tmp_path):
    engine = _engine(tmp_path)
    bindir = tmp_path / "bin"
    worker.sync_wrappers(engine=engine, bindir=bindir)
    # the engine edits exactly one wrapper (a self-improvement PR touched it)
    (engine / "scripts" / "macp-emulator").write_text(
        "#!/usr/bin/env bash\n# macp-emulator v2 — new flag\n", encoding="utf-8")
    synced = worker.sync_wrappers(engine=engine, bindir=bindir)
    assert synced == ["macp-emulator"]      # the return list names exactly the changed one
    assert (bindir / "macp-emulator").read_bytes() == \
        (engine / "scripts" / "macp-emulator").read_bytes()


def test_missing_source_is_skipped_not_raised(tmp_path):
    engine = _engine(tmp_path)
    (engine / "scripts" / "macp-emulator").unlink()   # the engine lacks one wrapper
    bindir = tmp_path / "bin"
    synced = worker.sync_wrappers(engine=engine, bindir=bindir)  # must not raise
    assert set(synced) == {"macp-sms", "macp-browser", "macp-remote-gate"}
    assert not (bindir / "macp-emulator").exists()
