"""Batch verify — the side-effect-free parts (batch_gate.py) and the ledger
predicates they lean on (assignment.is_batch_return_pending / is_deferred).

Covers who is batched (`eligible`, `repo_gate`, `remote_marked`, `needs_device`,
`device_ok`), the config reader, and the two row transitions (`mark_ready`,
`mark_returned`). The runner wiring is test_runner_batch.py, the orchestrator's
is test_batch_ready_state.py, and md -> payload -> eligible is in test_tasks.py.

Nothing here touches a real vault, a real repo or the network: configs live under
tmp_path, and the repo URLs are neutral placeholders.
"""

from __future__ import annotations

import json
import shlex

import pytest

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import batch_gate, remote_gate, stages
from todo_system.orchestrator.assignment import Assignment

REPO_URL = "https://example.test/acme/batchrepo.git"
PR_URL = "https://example.test/acme/batchrepo/pull/7"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
NOW_ISO = "2026-11-18T10:00:00"
FULL_GATE = [["./gradlew", ":app:testDebugUnitTest"]]
GATE = {"full_gate": FULL_GATE}


def _payload(**extra) -> dict:
    return {"repo": REPO_URL, "worker_mode": "auto", **extra}


def _eligible(payload=None, **overrides) -> tuple[bool, str]:
    kw = dict(tier="standard", gate=GATE, five_stage=True, auto_merge=True)
    kw.update(overrides)
    return batch_gate.eligible(_payload() if payload is None else payload, **kw)


def _write_config(root, data) -> None:
    path = root / batch_gate.CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _row(state=A.DONE, **kw) -> Assignment:
    kw.setdefault("payload", {"repo": REPO_URL, "branch": "worker/t"})
    return Assignment(task_id="t", agent_id="w", corr_id="c", kind="ai-local",
                      state=state, **kw)


# --- eligible ---------------------------------------------------------------

@pytest.mark.parametrize("tier, verify_batch, want", [
    # the tier decides when the task does not say
    ("light", None, True),
    ("standard", None, True),
    ("heavy", None, False),
    # verify_batch: true puts any tier in
    ("light", True, True),
    ("standard", True, True),
    ("heavy", True, True),
    # verify_batch: false keeps any tier out
    ("light", False, False),
    ("standard", False, False),
    ("heavy", False, False),
])
def test_eligible_tier_and_verify_batch(tier, verify_batch, want):
    payload = _payload() if verify_batch is None else _payload(verify_batch=verify_batch)
    ok, why = _eligible(payload, tier=tier)
    assert ok is want
    if verify_batch is None:
        assert why == f"tier {tier}"
    else:
        assert why == f"verify_batch: {str(verify_batch).lower()}"


@pytest.mark.parametrize("raw, want", [("true", True), ("false", False),
                                       ("yes", True), ("no", False)])
def test_eligible_reads_string_verify_batch(raw, want):
    # A payload that came through JSON by hand can carry the YAML spelling.
    assert _eligible(_payload(verify_batch=raw), tier="heavy")[0] is want
    assert _eligible(_payload(verify_batch=raw), tier="light")[0] is want


def test_eligible_ignores_an_unreadable_verify_batch():
    # Neither true nor false: the tier decides, as if the field were absent.
    assert _eligible(_payload(verify_batch="-"), tier="light") == (True, "tier light")
    assert _eligible(_payload(verify_batch="-"), tier="heavy") == (False, "tier heavy")


def test_eligible_needs_a_gate():
    assert _eligible(gate=None) == (False, "repo has no batch gate")


def test_eligible_excludes_a_vault_task():
    assert _eligible({"worker_mode": "auto"}) == (False, "vault task")


def test_eligible_excludes_a_returned_task():
    assert _eligible(_payload(verify_full=True)) == (False, "returned to full verify")
    assert _eligible(_payload(verify_full="true"))[0] is False


def test_verify_full_beats_verify_batch_true():
    # Order pin: a task sent back once never batches again, even if its
    # frontmatter asks for the batch.
    ok, why = _eligible(_payload(verify_full=True, verify_batch=True), tier="light")
    assert (ok, why) == (False, "returned to full verify")


@pytest.mark.parametrize("worker_mode", ["quick", "delegate", "discover", "review", ""])
def test_eligible_excludes_other_worker_modes(worker_mode):
    payload = _payload(worker_mode=worker_mode, verify_batch=True)
    assert _eligible(payload) == (False, "not the 5-stage auto pipeline")


def test_eligible_excludes_a_run_that_is_not_five_stage():
    # e.g. a `trivial` tag turns an auto task into the one-stage quick pipeline
    assert _eligible(five_stage=False) == (False, "not the 5-stage auto pipeline")


def test_eligible_excludes_auto_merge_off():
    assert _eligible(_payload(verify_batch=True), auto_merge=False) == (
        False, "auto_merge off")


def test_eligible_excludes_a_remote_marked_gate():
    assert _eligible(_payload(verify_batch=True), remote_marked=True) == (
        False, "full gate is remote-marked")


@pytest.mark.parametrize("payload", [None, "not a dict", ["repo"]])
def test_eligible_tolerates_a_non_dict_payload(payload):
    # Reads as an empty payload: no repo, so a vault task.
    assert batch_gate.eligible(payload, tier="light", gate=GATE, five_stage=True,
                               auto_merge=True) == (False, "vault task")


# --- needs_device / device_ok -----------------------------------------------

@pytest.mark.parametrize("payload, want", [
    ({}, False),
    ({"heavy_signals": ["emulator"]}, True),
    ({"heavy_signals": ["screen_check", "emulator"]}, True),
    ({"heavy_signals": ["screen_check"]}, False),
    ({"heavy_signals": "emulator"}, False),          # not a list: not a signal
    ({"emulator": True}, True),
    ({"emulator": "true"}, True),
    ({"emulator": False}, False),
    ({"emulator": "-"}, False),
    (None, False),
])
def test_needs_device(payload, want):
    assert batch_gate.needs_device(payload) is want


@pytest.mark.parametrize("needs, device_pass, want", [
    (False, False, True),     # nothing asked for: may wait for the gate
    (False, True, True),
    (True, True, True),       # asked for and seen
    (True, False, False),     # asked for, no evidence this run
])
def test_device_ok(needs, device_pass, want):
    payload = {"heavy_signals": ["emulator"]} if needs else {}
    assert batch_gate.device_ok(payload, device_pass=device_pass) is want


# --- load_config --------------------------------------------------------------

def test_load_config_missing_file_is_off(tmp_path):
    assert batch_gate.load_config(tmp_path) == {}


def test_load_config_disabled_is_off(tmp_path):
    _write_config(tmp_path, {"enabled": False, "repos": {"batchrepo": GATE}})
    assert batch_gate.load_config(tmp_path) == {}


def test_load_config_without_enabled_is_off(tmp_path):
    _write_config(tmp_path, {"repos": {"batchrepo": GATE}})
    assert batch_gate.load_config(tmp_path) == {}


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"enabled"', ""])
def test_load_config_broken_or_non_object_is_off(tmp_path, text):
    _write_config(tmp_path, text)
    assert batch_gate.load_config(tmp_path) == {}


def test_load_config_never_raises_on_a_bad_root():
    assert batch_gate.load_config(None) == {}


def test_load_config_reads_an_enabled_config(tmp_path):
    # Also shows the suite guard lets a tmp root through to the real reader.
    cfg = {"enabled": True, "repos": {"batchrepo": GATE}}
    _write_config(tmp_path, cfg)
    assert batch_gate.load_config(tmp_path) == cfg
    assert batch_gate.load_config(str(tmp_path)) == cfg


def test_load_config_accepts_a_string_enabled(tmp_path):
    _write_config(tmp_path, {"enabled": "true", "repos": {}})
    assert batch_gate.load_config(tmp_path)["enabled"] == "true"


# --- repo_gate ----------------------------------------------------------------

def _cfg(entry) -> dict:
    return {"enabled": True, "repos": {"batchrepo": entry}}


def test_repo_gate_finds_the_repo_by_its_checkout_name():
    gate = batch_gate.repo_gate(_cfg({"full_gate": FULL_GATE, "quick_hint": "h"}), REPO_URL)
    assert gate == {"full_gate": FULL_GATE, "quick_hint": "h"}
    # the same repo spelled without `.git` or with a trailing slash
    assert batch_gate.repo_gate(_cfg(GATE), "https://example.test/acme/batchrepo/") == GATE


def test_repo_gate_unregistered_repo_is_none():
    assert batch_gate.repo_gate(_cfg(GATE), "https://example.test/acme/other.git") is None


def test_repo_gate_wraps_a_single_argv():
    gate = batch_gate.repo_gate(_cfg({"full_gate": ["./gradlew", "test"]}), REPO_URL)
    assert gate["full_gate"] == [["./gradlew", "test"]]


def test_repo_gate_keeps_several_commands_in_order():
    full = [["npm", "run", "lint"], ["npm", "test"]]
    assert batch_gate.repo_gate(_cfg({"full_gate": full}), REPO_URL)["full_gate"] == full


def test_repo_gate_returns_a_copy():
    entry = {"full_gate": ["./gradlew", "test"]}
    cfg = _cfg(entry)
    batch_gate.repo_gate(cfg, REPO_URL)["full_gate"].append(["extra"])
    assert entry == {"full_gate": ["./gradlew", "test"]}     # config untouched


@pytest.mark.parametrize("full_gate", [
    None, [], [[]], "./gradlew test", [["./gradlew", ""]], [["./gradlew", 3]],
    ["./gradlew", ""], [["./gradlew"], "test"], [["./gradlew", "  "]], {"cmd": "x"},
])
def test_repo_gate_malformed_full_gate_is_none(full_gate):
    assert batch_gate.repo_gate(_cfg({"full_gate": full_gate}), REPO_URL) is None


@pytest.mark.parametrize("cfg", [{}, None, {"enabled": True}, {"repos": []},
                                 {"repos": {"batchrepo": "gate"}}])
def test_repo_gate_empty_or_malformed_config_is_none(cfg):
    assert batch_gate.repo_gate(cfg, REPO_URL) is None


def test_repo_gate_without_a_repo_is_none():
    assert batch_gate.repo_gate(_cfg(GATE), None) is None
    assert batch_gate.repo_gate(_cfg(GATE), "") is None


# --- remote_marked ------------------------------------------------------------

def _write_gates(checkout, commands) -> None:
    (checkout / ".macp").mkdir(parents=True, exist_ok=True)
    (checkout / ".macp" / "gates.json").write_text(
        json.dumps({"commands": commands}), encoding="utf-8")


def test_remote_marked_when_the_repo_marks_the_command(tmp_path):
    _write_gates(tmp_path, [{"cmd": shlex.join(FULL_GATE[0]), "remote": True}])
    assert batch_gate.remote_marked(GATE, tmp_path) is True


def test_remote_marked_when_the_repo_marks_it_heavy(tmp_path):
    _write_gates(tmp_path, [{"cmd": shlex.join(FULL_GATE[0]), "heavy": True}])
    assert batch_gate.remote_marked(GATE, tmp_path) is True


def test_remote_marked_any_command_of_several(tmp_path):
    gate = {"full_gate": [["npm", "run", "lint"], ["npm", "test"]]}
    _write_gates(tmp_path, [{"cmd": "npm test", "remote": True}])
    assert batch_gate.remote_marked(gate, tmp_path) is True


def test_not_remote_marked_without_a_gates_file(tmp_path):
    assert batch_gate.remote_marked(GATE, tmp_path) is False


def test_not_remote_marked_for_another_command(tmp_path):
    # remote_gate matches the command string exactly: a sibling task is not it.
    _write_gates(tmp_path, [{"cmd": "./gradlew :app:testDebug", "remote": True}])
    assert batch_gate.remote_marked(GATE, tmp_path) is False


def test_remote_marked_is_conservative_when_it_cannot_tell(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("gates unreadable")

    monkeypatch.setattr(remote_gate, "decide", boom)
    assert batch_gate.remote_marked(GATE, tmp_path) is True


# --- mark_ready ---------------------------------------------------------------

def _ready_result(**overrides) -> dict:
    result = {
        "review_required": True,
        batch_gate.RESULT_READY: True,
        batch_gate.KEY_BATCH: {"head_sha": HEAD_SHA},
        "pr_url": PR_URL,
        "repo": REPO_URL,
        "branch": "worker/t",
        "run_dir": "/somewhere/Worker/runs/20261118T100000-t",
    }
    result.update(overrides)
    return result


def test_mark_ready_moves_the_row():
    a = _row(history=[{"type": "RESULT"}])
    assert batch_gate.mark_ready(a, _ready_result(), now_iso=NOW_ISO) is True
    assert a.state == A.BATCH_READY == "batch_ready"
    assert a.payload[batch_gate.KEY_BATCH] == {
        "pr_url": PR_URL, "head_sha": HEAD_SHA, "branch": "worker/t",
        "ready_ts": NOW_ISO, "run": "20261118T100000-t"}
    assert a.updated_ts == NOW_ISO
    assert a.last_note
    assert a.history[-1] == {"ts": NOW_ISO, "type": batch_gate.HISTORY_READY,
                             "from": "orchestrator"}
    assert a.history[0] == {"type": "RESULT"}            # earlier history kept
    # waiting, not finished and not failed
    assert not a.is_terminal
    assert not a.is_deferred
    assert a.display_state == "batch_ready"


def test_mark_ready_stores_only_the_run_basename():
    a = _row()
    batch_gate.mark_ready(a, _ready_result(), now_iso=NOW_ISO)
    assert "/" not in a.payload[batch_gate.KEY_BATCH]["run"]
    assert "/somewhere" not in a.to_json()               # no host path in the ledger


def test_mark_ready_falls_back_to_the_row_for_repo_and_branch():
    a = _row(payload={"repo": REPO_URL, "branch": "worker/custom"})
    result = _ready_result()
    del result["repo"], result["branch"], result["run_dir"]
    assert batch_gate.mark_ready(a, result, now_iso=NOW_ISO) is True
    assert a.payload[batch_gate.KEY_BATCH]["branch"] == "worker/custom"
    assert a.payload[batch_gate.KEY_BATCH]["run"] == ""


def test_mark_ready_defaults_the_branch_to_the_slug():
    a = _row(payload={"repo": REPO_URL})
    result = _ready_result()
    del result["branch"]
    batch_gate.mark_ready(a, result, now_iso=NOW_ISO)
    assert a.payload[batch_gate.KEY_BATCH]["branch"] == "worker/t"


@pytest.mark.parametrize("drop", ["pr_url", "head_sha", "repo"])
def test_mark_ready_refuses_an_incomplete_result_and_leaves_the_row(drop):
    a = _row(payload={"branch": "worker/t"} if drop == "repo"
             else {"repo": REPO_URL, "branch": "worker/t"})
    before = a.to_json()
    result = _ready_result()
    if drop == "head_sha":
        result[batch_gate.KEY_BATCH] = {}
    else:
        del result[drop]
    assert batch_gate.mark_ready(a, result, now_iso=NOW_ISO) is False
    assert a.to_json() == before                         # nothing changed at all


@pytest.mark.parametrize("batch", [None, "sha", ["sha"], {"head_sha": "  "}])
def test_mark_ready_refuses_a_malformed_batch_record(batch):
    a = _row()
    before = a.to_json()
    assert batch_gate.mark_ready(a, _ready_result(**{batch_gate.KEY_BATCH: batch}),
                                 now_iso=NOW_ISO) is False
    assert a.to_json() == before


# --- mark_returned ------------------------------------------------------------

def test_mark_returned_sends_the_row_back_without_a_penalty():
    a = _row(state=A.BATCH_READY, refail_count=2, timeout_streak=1,
             capacity_limited=True, job_running=True, checkout_busy=True,
             timed_out=True, blocked_on="something",
             payload={"repo": REPO_URL, "branch": "worker/t",
                      batch_gate.KEY_BATCH: {"pr_url": PR_URL, "head_sha": HEAD_SHA}})
    batch_gate.mark_returned(a, batch_gate.RETURN_EXPIRED, now_iso=NOW_ISO)

    assert a.state == A.FAILED
    assert batch_gate.KEY_BATCH not in a.payload
    assert a.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert a.payload[batch_gate.KEY_RETURN_REASON] == "expired"
    assert a.payload["repo"] == REPO_URL                 # the rest of the payload stays
    assert a.updated_ts == NOW_ISO
    assert (a.capacity_limited, a.job_running, a.checkout_busy, a.timed_out) == (
        False, False, False, False)
    assert a.blocked_on == ""
    assert a.history[-1] == {"ts": NOW_ISO, "type": A.BATCH_RETURNED,
                             "reason": "expired", "from": "orchestrator"}
    assert A.BATCH_RETURNED == "batch-returned"
    # nothing failed: the counters that drive poison and the timeout park stay
    assert a.refail_count == 2
    assert a.timeout_streak == 1
    # ...and every view reads it as a wait
    assert A.is_batch_return_pending(a)
    assert a.is_deferred
    assert a.display_state == A.DEFERRED == "deferred"


def test_mark_returned_row_reads_the_same_from_json():
    a = _row()
    batch_gate.mark_returned(a, batch_gate.RETURN_NOT_ENABLED, now_iso=NOW_ISO)
    raw = json.loads(a.to_json())
    assert A.is_batch_return_pending(raw)
    assert A.is_deferred(raw)
    assert A.display_state(raw) == "deferred"
    again = Assignment.from_json(a.to_json())
    assert again.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert again.is_deferred


def test_returned_payload_is_never_batched_again():
    a = _row(payload=_payload(verify_batch=True))
    batch_gate.mark_returned(a, batch_gate.RETURN_NO_DEVICE, now_iso=NOW_ISO)
    assert _eligible(a.payload, tier="light") == (False, "returned to full verify")


def test_carry_keys_are_what_mark_returned_writes():
    a = _row()
    batch_gate.mark_returned(a, batch_gate.RETURN_EXPIRED, now_iso=NOW_ISO)
    assert set(batch_gate.CARRY_KEYS) <= set(a.payload)
    assert batch_gate.CARRY_KEYS == ("verify_full", "batch_return_reason")


# --- rows that existed before: unchanged ---------------------------------------

def test_plain_failed_rows_are_not_batch_returns():
    assert not A.is_batch_return_pending(_row(state=A.FAILED))                # no history
    assert not A.is_batch_return_pending(_row(state=A.FAILED, history=[{"type": "FAIL"}]))
    resumed = _row(state=A.FAILED, history=[{"type": "FAIL"},
                                            {"type": A.RESUMED_SCHEDULED}])
    assert not A.is_batch_return_pending(resumed)
    assert A.is_resume_pending(resumed) and resumed.is_deferred               # as before
    plain = _row(state=A.FAILED, last_note="tests red", history=[{"type": "FAIL"}])
    assert not plain.is_deferred
    assert plain.display_state == A.FAILED


def test_batch_return_is_read_from_the_last_history_entry_only():
    # Once a later event lands (the next dispatch replaces the row; a reply
    # appends), the row stops reading as a pending return.
    a = _row(state=A.FAILED, history=[{"type": A.BATCH_RETURNED}, {"type": "FAIL"}])
    assert not A.is_batch_return_pending(a)
    assert not a.is_deferred


def test_only_a_failed_row_can_be_a_pending_return():
    for state in (A.REJECTED, A.DONE, A.AWAITING_HUMAN, A.BATCH_READY, A.IN_PROGRESS):
        row = _row(state=state, history=[{"type": A.BATCH_RETURNED}])
        assert not A.is_batch_return_pending(row), state
        assert not row.is_deferred, state
        assert row.display_state == state


def test_malformed_history_is_not_a_pending_return():
    assert not A.is_batch_return_pending({"state": A.FAILED, "history": "batch-returned"})
    assert not A.is_batch_return_pending({"state": A.FAILED, "history": ["batch-returned"]})
    assert not A.is_batch_return_pending({"state": A.FAILED})
    assert not A.is_batch_return_pending({})


def test_the_older_deferrals_still_read_as_deferred():
    assert _row(state=A.FAILED, job_running=True).is_deferred
    assert _row(state=A.FAILED, checkout_busy=True).is_deferred
    assert not _row(state=A.FAILED, capacity_limited=True).is_deferred


def test_batch_ready_is_in_no_group_the_orchestrator_branches_on():
    from todo_system.orchestrator import orchestrator as orch_mod

    assert A.BATCH_READY not in A.TERMINAL
    assert A.BATCH_READY not in orch_mod._IN_FLIGHT
    assert A.BATCH_READY != A.AWAITING_HUMAN
    assert A.TERMINAL == frozenset({A.DONE, A.FAILED, A.REJECTED})     # untouched


# --- verify_hint --------------------------------------------------------------

def test_verify_hint_names_the_deferred_gate_and_the_diff_command():
    hint = batch_gate.verify_hint({"full_gate": FULL_GATE, "quick_hint": "Use :app only."},
                                  "main")
    assert "`./gradlew :app:testDebugUnitTest`" in hint
    assert "`git diff --name-only origin/main...HEAD`" in hint
    assert hint.endswith("Use :app only.")


def test_verify_hint_lists_every_command_and_quotes_arguments():
    hint = batch_gate.verify_hint({"full_gate": [["npm", "run", "lint"],
                                                 ["sh", "-c", "a b"]]})
    assert "`npm run lint`, `sh -c 'a b'`" in hint
    assert "origin/<default>...HEAD" in hint             # no default branch known


def test_verify_hint_skips_a_non_string_quick_hint():
    hint = batch_gate.verify_hint({"full_gate": FULL_GATE, "quick_hint": ["x"]}, "main")
    assert hint.endswith("origin/main...HEAD`.")
    assert batch_gate.verify_hint(None).startswith("Changed files:")


# --- the suite guard (todo_system/conftest.py) ---------------------------------

def test_suite_guard_wraps_the_reader(real_batch_gate_config):
    assert batch_gate.load_config is not real_batch_gate_config["load_config"]


def test_suite_guard_answers_empty_for_every_real_root(
        monkeypatch, tmp_path, real_batch_gate_config):
    # The real reader builds its path through batch_gate.Path, so a spy there
    # shows whether it ran at all — "{} because no file exists" would prove
    # nothing about the guard.
    built: list[tuple] = []
    real_path = batch_gate.Path

    def spy(*parts):
        built.append(parts)
        return real_path(*parts)

    monkeypatch.setattr(batch_gate, "Path", spy)
    assert real_batch_gate_config["roots"]
    for root in real_batch_gate_config["roots"]:
        assert batch_gate.load_config(root) == {}
    assert batch_gate.load_config(stages._VAULT_DATA_ROOT) == {}
    assert built == []                    # the real reader never ran for a real root

    _write_config(tmp_path, {"enabled": True, "repos": {}})
    assert batch_gate.load_config(tmp_path) == {"enabled": True, "repos": {}}
    assert built                          # ...and did run for a test's own root
