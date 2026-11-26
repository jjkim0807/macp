"""Alert routing state never lands in a bare engine checkout.

2026-11-26: the HQ engine checkout grew untracked ``logs/kakao_digest.jsonl`` and
``logs/kakao_route.log``. The path: a caller ran the engine's
``scripts/kakao_send.sh`` with KAKAO_LEVEL / KAKAO_EVENT_KEY but without
MACP_VAULT_ROOT, so the script's data root fell back to its own checkout
(``VAULT_DIR=${KAKAO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}``) and it ran
``kakao_route.py`` with ``KAKAO_VAULT_DIR=<engine>`` — ``kakao_format.route()``
then wrote its digest queue and route log into the engine tree. Those untracked
files made maintenance engine-pull skip every tick.

Two fixes, pinned here:
  * ``kakao_format._vault_root()`` refuses a bare engine tree (ships
    ``todo_system/kakao_format.py``, no ``.macp/config.toml``) — route() degrades
    to its documented fail-open immediate send; a combined layout (marker
    present) is unchanged;
  * the engine's own Python senders hand kakao_send.sh their resolved vault
    (``kakao_format.sender_env``), so the fallback is unreachable from them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from todo_system import kakao_format, worker
from todo_system.orchestrator import human


def _bare_engine(root: Path) -> Path:
    (root / "todo_system").mkdir(parents=True)
    (root / "todo_system" / "kakao_format.py").write_text("# engine\n", encoding="utf-8")
    (root / "scripts").mkdir()
    return root


def test_route_refuses_bare_engine_tree_and_sends(tmp_path, monkeypatch):
    engine = _bare_engine(tmp_path / "engine")
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(engine))

    for level, key in (("digest", None), ("silent", None), (None, "evt:1")):
        verdict, final = kakao_format.route("hello", level=level, event_key=key)
        assert (verdict, final) == ("send", "hello")

    assert not (engine / "logs").exists()


def test_route_keeps_combined_layout(tmp_path, monkeypatch):
    combined = _bare_engine(tmp_path / "combined")
    (combined / ".macp").mkdir()
    (combined / ".macp" / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(combined))

    verdict, final = kakao_format.route("hello", level="digest")

    assert (verdict, final) == ("digest", None)
    assert (combined / "logs" / "kakao_digest.jsonl").is_file()
    assert (combined / "logs" / "kakao_route.log").is_file()


def test_route_with_explicit_paths_skips_root_resolution(tmp_path, monkeypatch):
    engine = _bare_engine(tmp_path / "engine")
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(engine))
    state = tmp_path / "state"

    verdict, _ = kakao_format.route(
        "hello", level="digest",
        digest_path=state / "d.jsonl", dedup_path=state / "dd.json", log_path=state / "r.log")

    assert verdict == "digest"
    assert (state / "d.jsonl").is_file()
    assert not (engine / "logs").exists()


def test_sender_env_fills_vault_only_when_unset(tmp_path):
    assert kakao_format.sender_env(tmp_path, base={})["MACP_VAULT_ROOT"] == str(tmp_path)
    kept = kakao_format.sender_env(tmp_path, base={"MACP_VAULT_ROOT": "/v"})
    assert kept["MACP_VAULT_ROOT"] == "/v"
    seam = kakao_format.sender_env(tmp_path, base={"KAKAO_VAULT_DIR": "/k"})
    assert seam == {"KAKAO_VAULT_DIR": "/k"}
    assert kakao_format.sender_env(None, base={}) == {}


class _Recorder:
    def __init__(self):
        self.kwargs: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.kwargs.append(kwargs)


def test_worker_kakao_notify_passes_vault(tmp_path, monkeypatch):
    script = tmp_path / "kakao_send.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(worker, "KAKAO_SEND_SCRIPT", script)
    monkeypatch.setattr(worker, "_notify_channel_configured", lambda: True)
    monkeypatch.delenv("KAKAO_VAULT_DIR", raising=False)
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(worker.subprocess, "run", rec)

    worker.kakao_notify("hello")

    assert rec.kwargs and rec.kwargs[0]["env"]["MACP_VAULT_ROOT"] == str(worker.ROOT)


def test_human_kakao_notifier_passes_vault(tmp_path, monkeypatch):
    script = tmp_path / "kakao_send.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(human, "KAKAO_SCRIPT", script)
    monkeypatch.delenv("KAKAO_VAULT_DIR", raising=False)
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(human.subprocess, "run", rec)

    human.kakao_notifier("hello")

    assert rec.kwargs[0]["env"]["MACP_VAULT_ROOT"] == str(human._VAULT_DATA_ROOT)


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(),
                    reason="kakao scripts hardcode /usr/bin/python3")
def test_kakao_send_sh_without_vault_env_leaves_no_route_state_in_engine(tmp_path):
    """The incident's exact path, end to end through the real script bytes: a
    digest alert from a caller with no vault env."""
    from todo_system.tests.test_split_engine_root_11d import _engine_files, _Harness

    h = _Harness(tmp_path)
    engine = _engine_files(tmp_path / "engine")

    # Not strict: route() falls open to an immediate send, and this engine copy
    # has no token, so the send itself is skipped (exit 0) — the point is where
    # the routing state went, not delivery.
    r = h.send(engine / "scripts" / "kakao_send.sh", "digest me",
               KAKAO_LEVEL="digest", KAKAO_SEND_STRICT="0")

    assert r.returncode == 0, r.stderr
    assert "route: QUEUED" not in r.stderr
    assert not (engine / "logs" / "kakao_digest.jsonl").exists()
    assert not (engine / "logs" / "kakao_route.log").exists()
    assert not (engine / "logs" / "kakao_dedup.json").exists()
