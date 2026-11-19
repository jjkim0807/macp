"""Unit tests for the remote-gate executor (todo_system/orchestrator/remote_gate).

Every external effect — gcloud up/create, ssh/scp, the remote gradle, gcloud
stop/delete — is driven through a fake ``run`` (the beta_publish / screen_check
precedent), so nothing shells out and no gcloud / VM / gradle is ever touched.
All config, run dirs, job dir and budget ledger live under ``tmp_path``.

This file sits inside the public-export scan tree, so NO literal from the
identifier gate's PATTERN (personal names, school domains, absolute home paths)
may appear here — fixtures use neutral placeholders only.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

from todo_system.orchestrator import remote_gate as rg

# A neutral task slug and the real-shaped (but non-personal) heavy command.
_SLUG = "demo-remote-gate"
_CMD = "./gradlew :shared:testDebugUnitTest"

# A fixed, tz-aware clock so the budget ledger's month key is deterministic.
_NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.timezone.utc)

# Two JUnit testsuites (2 + 3 = 5 tests, all green) and a failing/erroring/skipped
# one — neutral content, no personal identifiers.
_XML_A = '<testsuite name="A" tests="2" failures="0" errors="0" skipped="0"></testsuite>'
_XML_B = '<testsuite name="B" tests="3" failures="0" errors="0" skipped="0"></testsuite>'
_XML_MIXED = '<testsuite name="C" tests="4" failures="1" errors="1" skipped="1"></testsuite>'


# gcloud's real wording for a missing instance (neutral resource path).
_NOT_FOUND_ERR = ("ERROR: (gcloud.compute.instances.describe) Could not fetch resource:\n"
                  " - The resource 'projects/p/zones/z/instances/n' was not found\n")


def _cfg(**over):
    base = {"enabled": True, "node_mode": "start-stop", "zone": "z", "node": "n",
            "machine_type": "mt", "image": None, "monthly_cap_usd": 20,
            "per_run_vm_ttl_min": 120, "hourly_rate_usd": 0.04}
    base.update(over)
    return base


class FakeGcloud:
    """Records every ``run`` call and dispatches canned results by the gcloud
    sub-operation — the beta_publish / screen_check fake-``run`` pattern. ``run``
    never shells out: no gcloud, no VM, no gradle is ever touched.

    ``raise_on`` is an operation token ("up"|"down"|"prepare"|"exec"|"harvest")
    that raises ``raise_exc`` instead of returning, to drive the fallback path.
    ``harvest_xml`` is a list of JUnit XML strings the harvest ssh streams back."""

    def __init__(self, *, up_rc=0, exec_rc=0, harvest_xml=None,
                 raise_on=None, raise_exc=None, status=None, describe_rc=None,
                 down_rc=0, down_err=""):
        self.calls: list[dict] = []
        self.up_rc = up_rc
        self.exec_rc = exec_rc
        self.harvest_xml = harvest_xml
        self.raise_on = raise_on
        self.raise_exc = raise_exc
        # create-delete describes first: ``status=None`` = the instance is absent
        # (gcloud's not-found stderr), else that status string on stdout.
        # ``describe_rc`` forces a non-not-found describe failure.
        self.status = status
        self.describe_rc = describe_rc
        self.down_rc = down_rc
        self.down_err = down_err

    @staticmethod
    def _classify(argv: list[str]) -> str:
        a = list(argv)
        if "instances" in a:
            if "describe" in a:
                return "describe"
            if "create" in a or "start" in a:
                return "up"
            if "delete" in a or "stop" in a:
                return "down"
        if "ssh" in a:
            cmd = a[a.index("--command") + 1] if "--command" in a else ""
            if rg._JUNIT_SEP in cmd:
                return "harvest"
            if "git clone" in cmd:
                return "prepare"
            return "exec"
        return "other"

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        argv = list(argv)
        self.calls.append({"argv": argv, "timeout": timeout})
        kind = self._classify(argv)
        if self.raise_on == kind:
            assert self.raise_exc is not None
            raise self.raise_exc

        def cp(rc=0, out="", err=""):
            return subprocess.CompletedProcess(argv, rc, out, err)

        if kind == "describe":
            if self.describe_rc is not None:
                return cp(self.describe_rc, "", "ERROR: permission denied")
            if self.status is None:
                return cp(1, "", _NOT_FOUND_ERR)
            return cp(0, self.status + "\n")
        if kind == "up":
            return cp(self.up_rc, "")
        if kind == "down":
            return cp(self.down_rc, "", self.down_err)
        if kind == "exec":
            return cp(self.exec_rc,
                      "BUILD SUCCESSFUL" if self.exec_rc == 0 else "BUILD FAILED")
        if kind == "harvest":
            if self.harvest_xml is None:
                return cp(0, "")
            stream = rg._JUNIT_SEP + "\n" + f"\n{rg._JUNIT_SEP}\n".join(self.harvest_xml)
            return cp(0, stream)
        return cp(0, "")          # down / prepare / other

    def kinds(self) -> list[str]:
        return [self._classify(c["argv"]) for c in self.calls]


def _run_gate(tmp_path, fake, *, cfg=None, slug="demo", notify=None, now=_NOW,
              cmd=_CMD, **kw):
    return rg.run_remote_gate(
        slug, "https://example.test/repo.git", "deadbeef", cmd,
        cfg=cfg if cfg is not None else _cfg(), run=fake,
        jobs_dir=tmp_path / "jobs", budget_path=tmp_path / "budget.json",
        now=now, notify=notify or (lambda _m: None), **kw)

# A verify.md with mulkko-118's actual on-disk wording: the command runs GREEN in
# row #1 (Exit 0) and TIMES OUT in row #4 (Exit 143 / "Timed out at 60-min cap").
# The row-#1/row-#4 pair is the whole point — the signal reader must flag #4 and
# NOT #1, even though both name the same command.
_VERIFY_MD_TIMED_OUT = """\
---
stage: verify
overall_status: skip
---

# Verification

| # | Command | Tree (sha) | Exit | Decisive output |
|---|---------|------------|------|-----------------|
| 1 | `./gradlew :shared:testDebugUnitTest --rerun --tests <14 named classes>` | abc1234 | 0 | BUILD SUCCESSFUL 2m8s; 140 tests, 0 failures, 0 errors |
| 4 | `./gradlew :shared:testDebugUnitTest --rerun` (FULL suite) | abc1234 | 143 | **Timed out at 60-min cap.** SIGTERM left no per-class XML |
"""

# A clean all-green verify.md: the command appears only in an Exit-0 row.
_VERIFY_MD_CLEAN_PASS = """\
---
stage: verify
overall_status: pass
---

# Verification

| # | Command | Tree (sha) | Exit | Decisive output |
|---|---------|------------|------|-----------------|
| 1 | `./gradlew :shared:testDebugUnitTest --rerun` | abc1234 | 0 | BUILD SUCCESSFUL 2m8s; 140 tests, 0 failures |
"""


def _mk_run(runs_dir, name, *, verify_md=None, stages_json=None):
    rd = runs_dir / name
    rd.mkdir(parents=True)
    if verify_md is not None:
        (rd / "verify.md").write_text(verify_md, encoding="utf-8")
    if stages_json is not None:
        (rd / "stages.json").write_text(json.dumps(stages_json), encoding="utf-8")
    return rd


# ---------------------------------------------------------------------------
# Prior-run timeout/memory signal reader (verify.md primary, stages.json 2nd)
# ---------------------------------------------------------------------------

def test_prior_run_timed_out_reads_the_verify_md_row(tmp_path):
    runs = tmp_path / "runs"
    _mk_run(runs, f"20261018T120000-{_SLUG}", verify_md=_VERIFY_MD_TIMED_OUT)
    assert rg.prior_run_timed_out(_SLUG, _CMD, runs_dir=runs) is True


def test_clean_pass_verify_md_is_not_a_timeout(tmp_path):
    """The over-match guard: the command runs GREEN (Exit 0) in the only row, so
    the same-row timeout-token requirement must keep this False."""
    runs = tmp_path / "runs"
    _mk_run(runs, f"20261018T120000-{_SLUG}", verify_md=_VERIFY_MD_CLEAN_PASS)
    assert rg.prior_run_timed_out(_SLUG, _CMD, runs_dir=runs) is False


def test_a_different_command_is_not_flagged(tmp_path):
    runs = tmp_path / "runs"
    _mk_run(runs, f"20261018T120000-{_SLUG}", verify_md=_VERIFY_MD_TIMED_OUT)
    assert rg.prior_run_timed_out(
        _SLUG, "./gradlew :app:assembleRelease", runs_dir=runs) is False


def test_stages_json_timed_out_stage_is_the_secondary_signal(tmp_path):
    runs = tmp_path / "runs"
    _mk_run(runs, f"20261018T120000-{_SLUG}",
            stages_json={"timed_out_stage": "verify", "completed": []})
    assert rg.prior_run_timed_out(_SLUG, _CMD, runs_dir=runs) is True


def test_no_prior_run_is_false(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    assert rg.prior_run_timed_out(_SLUG, _CMD, runs_dir=runs) is False


def test_picks_newest_non_excluded_prior_run(tmp_path):
    runs = tmp_path / "runs"
    _mk_run(runs, f"20261018T120000-{_SLUG}", verify_md=_VERIFY_MD_TIMED_OUT)   # older, timed out
    _mk_run(runs, f"20261018T130000-{_SLUG}", verify_md=_VERIFY_MD_CLEAN_PASS)  # newest (current)
    # newest run is a clean pass → not a timeout
    assert rg.prior_run_timed_out(_SLUG, _CMD, runs_dir=runs) is False
    # exclude the newest (the current run) → the older timed-out run is the signal
    assert rg.prior_run_timed_out(
        _SLUG, _CMD, runs_dir=runs, exclude_run_id=f"20261018T130000-{_SLUG}") is True


# ---------------------------------------------------------------------------
# decide() — (a) repo gate, (b) prior timeout, (c) local; budget override
# ---------------------------------------------------------------------------

def test_decide_gate_remote_true_is_remote():
    gates = {"commands": [{"cmd": _CMD, "remote": True}]}
    assert rg.decide(_CMD, gates=gates) == "remote"


def test_decide_gate_heavy_true_is_remote():
    gates = {"commands": [{"cmd": _CMD, "heavy": True}]}
    assert rg.decide(_CMD, gates=gates) == "remote"


def test_decide_no_gate_prior_timeout_is_remote():
    assert rg.decide(_CMD, gates={}, prior_timed_out=True) == "remote"


def test_decide_no_gate_no_prior_is_local():
    assert rg.decide(_CMD, gates={}, prior_timed_out=False) == "local"


def test_decide_unlisted_command_is_local_even_with_gates_present():
    gates = {"commands": [{"cmd": "./gradlew :app:test", "remote": True}]}
    assert rg.decide(_CMD, gates=gates) == "local"


def test_decide_budget_exhausted_forces_local_over_everything():
    gates = {"commands": [{"cmd": _CMD, "remote": True}]}
    assert rg.decide(_CMD, gates=gates, prior_timed_out=True,
                     budget_exhausted=True) == "local"


def test_decide_loads_gates_from_the_repo_checkout(tmp_path):
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "gates.json").write_text(
        json.dumps({"commands": [{"cmd": _CMD, "remote": True}]}), encoding="utf-8")
    assert rg.decide(_CMD, tmp_path) == "remote"
    assert rg.decide("./gradlew :app:test", tmp_path) == "local"


# ---------------------------------------------------------------------------
# aggregate_junit_xml + estimate_cost_usd (pure)
# ---------------------------------------------------------------------------

def test_aggregate_junit_xml_sums_two_files():
    agg = rg.aggregate_junit_xml([_XML_A, _XML_MIXED])
    assert agg["tests"] == 6
    assert agg["failures"] == 1
    assert agg["errors"] == 1
    assert agg["skipped"] == 1
    assert agg["suites"] == 2
    assert agg["passed"] == 3                      # 6 - 1 - 1 - 1


def test_aggregate_junit_xml_handles_testsuites_wrapper_and_junk():
    doc = '<testsuites><testsuite tests="2" failures="1"/><testsuite tests="3"/></testsuites>'
    agg = rg.aggregate_junit_xml([doc, "not xml at all", "<broken"])
    assert agg["tests"] == 5 and agg["failures"] == 1 and agg["suites"] == 2


def test_aggregate_junit_dir_globs_xml(tmp_path):
    d = tmp_path / "build" / "test-results" / "testDebugUnitTest"
    d.mkdir(parents=True)
    (d / "TEST-A.xml").write_text(_XML_A, encoding="utf-8")
    (d / "TEST-B.xml").write_text(_XML_B, encoding="utf-8")
    assert rg.aggregate_junit_dir(tmp_path)["tests"] == 5


def test_estimate_cost_usd_is_local_and_linear():
    assert rg.estimate_cost_usd(3600, 0.04) == 0.04
    assert rg.estimate_cost_usd(1800, 0.04) == 0.02
    assert rg.estimate_cost_usd(0, 0.04) == 0.0
    assert rg.estimate_cost_usd(-5, 0.04) == 0.0    # clamped


# ---------------------------------------------------------------------------
# run_remote_gate — always-stop, fallback, caps, Worker/jobs output
# ---------------------------------------------------------------------------

def test_success_stops_node_writes_files_and_aggregates_junit(tmp_path):
    fake = FakeGcloud(harvest_xml=[_XML_A, _XML_B])
    res = _run_gate(tmp_path, fake)

    assert res["rc"] == 0 and res["fell_back_local"] is False
    assert res["junit"]["tests"] == 5 and res["junit"]["failures"] == 0
    assert "up" in fake.kinds() and "down" in fake.kinds()   # started AND stopped
    jobs = tmp_path / "jobs"
    assert (jobs / "demo-remote-1.json").is_file()
    assert (jobs / "demo-remote-1.rc").read_text(encoding="utf-8").strip() == "0"
    assert (jobs / "demo-remote-1.log").is_file()
    saved = json.loads((jobs / "demo-remote-1.json").read_text(encoding="utf-8"))
    assert saved["handle"] == "demo-remote-1" and saved["cmd"] == _CMD


def test_second_run_gets_the_next_index(tmp_path):
    _run_gate(tmp_path, FakeGcloud(harvest_xml=[_XML_A]))
    res2 = _run_gate(tmp_path, FakeGcloud(harvest_xml=[_XML_A]))
    assert res2["handle"] == "demo-remote-2"
    assert (tmp_path / "jobs" / "demo-remote-2.json").is_file()


def test_build_failure_is_a_verdict_not_a_fallback_and_still_stops(tmp_path):
    fake = FakeGcloud(exec_rc=1, harvest_xml=[_XML_MIXED])
    res = _run_gate(tmp_path, fake)
    assert res["rc"] == 1                       # the command's rc is the gate verdict
    assert res["fell_back_local"] is False      # a failed gate is NOT a remote failure
    assert "down" in fake.kinds()               # node still stopped


def test_node_up_failure_falls_back_local_and_still_tears_down(tmp_path):
    fake = FakeGcloud(up_rc=1)                   # instances start returns non-zero
    res = _run_gate(tmp_path, fake)
    assert res["fell_back_local"] is True and res["rc"] is None
    assert "down" in fake.kinds()               # teardown runs even when up failed


def test_arbitrary_exception_is_absorbed_and_tears_down(tmp_path):
    fake = FakeGcloud(raise_on="prepare", raise_exc=RuntimeError("ssh exploded"))
    res = _run_gate(tmp_path, fake)             # must NOT raise
    assert res["fell_back_local"] is True
    assert res.get("error") == "RuntimeError"
    assert "down" in fake.kinds()


def test_command_timeout_falls_back_and_tears_down(tmp_path):
    fake = FakeGcloud(raise_on="exec",
                      raise_exc=subprocess.TimeoutExpired(cmd=["ssh"], timeout=1))
    res = _run_gate(tmp_path, fake)
    assert res["fell_back_local"] is True
    assert "down" in fake.kinds()


def test_create_delete_backend_passes_ttl_and_deletes(tmp_path):
    cfg = _cfg(node_mode="create-delete", per_run_vm_ttl_min=90, image="toolchain-img")
    fake = FakeGcloud(harvest_xml=[_XML_A])
    res = _run_gate(tmp_path, fake, cfg=cfg)

    up = next(c for c in fake.calls if "create" in c["argv"])
    assert "--max-run-duration=5400s" in up["argv"]            # 90 min
    assert "--instance-termination-action=DELETE" in up["argv"]
    assert "--image" in up["argv"] and "toolchain-img" in up["argv"]
    assert "delete" in {t for c in fake.calls for t in c["argv"]}   # teardown deletes
    assert res["node_mode"] == "create-delete"


# ---------------------------------------------------------------------------
# create-delete: create spec, idempotent up, always-delete on every exit path
# ---------------------------------------------------------------------------

_CD_SPEC = {"node_mode": "create-delete", "image_family": "toolchain-fam",
            "image_project": "img-proj", "spot": True, "nested_virtualization": True,
            "boot_disk_gb": 40, "boot_disk_type": "pd-balanced",
            "no_service_account": True, "labels": "purpose=test"}


def test_node_create_argv_carries_the_configured_spec():
    argv = rg.node_create_argv(rg.node_params(_cfg(**_CD_SPEC, image="toolchain-img")), 600)
    assert argv[2:5] == ["instances", "create", "n"]
    assert "--max-run-duration=600s" in argv
    assert "--instance-termination-action=DELETE" in argv
    assert argv[argv.index("--image-family") + 1] == "toolchain-fam"
    assert "--image" not in argv                       # family wins over a plain image
    assert argv[argv.index("--image-project") + 1] == "img-proj"
    assert "--provisioning-model=SPOT" in argv
    assert "--enable-nested-virtualization" in argv
    assert "--boot-disk-size=40GB" in argv
    assert "--boot-disk-type=pd-balanced" in argv
    assert "--no-service-account" in argv and "--no-scopes" in argv
    assert "--labels=purpose=test" in argv


def test_node_create_argv_omits_every_optional_flag_when_absent():
    argv = rg.node_create_argv(rg.node_params(_cfg(node_mode="create-delete")), 600)
    joined = " ".join(argv)
    for flag in ("--image", "--provisioning-model", "--enable-nested-virtualization",
                 "--boot-disk", "--network", "--subnet", "--tags", "--service-account",
                 "--scopes", "--no-service-account", "--no-address", "--labels", "--project"):
        assert flag not in joined, flag
    # the orphan backstop is unconditional
    assert "--max-run-duration=600s" in argv
    assert "--instance-termination-action=DELETE" in argv


def test_node_params_tolerates_bad_spec_values():
    p = rg.node_params(_cfg(spot="false", boot_disk_gb="x", boot_disk_type="  "))
    assert p["spot"] is False and p["boot_disk_gb"] is None and p["boot_disk_type"] is None


def _up(fake, cfg, *, progress=None):
    return rg._node_up(fake, rg.node_mode(cfg), rg.node_params(cfg), 600,
                       timeout=5, progress=progress)


def test_create_delete_up_reuses_a_running_node_without_create():
    fake = FakeGcloud(status="RUNNING")
    assert _up(fake, _cfg(**_CD_SPEC)) == "reused"
    assert fake.kinds() == ["describe"]


def test_create_delete_up_creates_when_absent():
    fake = FakeGcloud(status=None)
    assert _up(fake, _cfg(**_CD_SPEC)) == "created"
    assert fake.kinds() == ["describe", "up"]
    assert "create" in fake.calls[1]["argv"]
    assert "--instance-termination-action=DELETE" in fake.calls[1]["argv"]


def test_create_delete_up_starts_an_existing_stopped_node():
    fake = FakeGcloud(status="TERMINATED")
    assert _up(fake, _cfg(**_CD_SPEC)) == "started"
    assert "start" in fake.calls[1]["argv"] and "create" not in fake.calls[1]["argv"]


def test_create_delete_up_raises_on_a_non_not_found_describe_failure():
    fake = FakeGcloud(describe_rc=1)
    try:
        _up(fake, _cfg(**_CD_SPEC))
    except rg.RemoteGateError:
        pass
    else:
        raise AssertionError("expected RemoteGateError")
    assert fake.kinds() == ["describe"]                # never guesses "absent" → no create


def test_start_stop_up_is_unchanged_no_describe():
    fake = FakeGcloud()
    assert _up(fake, _cfg()) == "started"
    assert fake.kinds() == ["up"] and "start" in fake.calls[0]["argv"]


def _deleted(fake) -> bool:
    return any("delete" in c["argv"] for c in fake.calls)


def test_create_delete_success_deletes(tmp_path):
    fake = FakeGcloud(harvest_xml=[_XML_A])
    res = _run_gate(tmp_path, fake, cfg=_cfg(**_CD_SPEC))
    assert res["rc"] == 0 and res["fell_back_local"] is False
    assert _deleted(fake) and fake.kinds()[-1] == "down"


def test_create_delete_build_failure_still_deletes(tmp_path):
    fake = FakeGcloud(exec_rc=1, harvest_xml=[_XML_MIXED])
    res = _run_gate(tmp_path, fake, cfg=_cfg(**_CD_SPEC))
    assert res["rc"] == 1 and _deleted(fake)


def test_create_delete_exception_still_deletes(tmp_path):
    fake = FakeGcloud(raise_on="prepare", raise_exc=RuntimeError("ssh exploded"))
    res = _run_gate(tmp_path, fake, cfg=_cfg(**_CD_SPEC))
    assert res["fell_back_local"] is True and _deleted(fake)


def test_create_delete_timeout_still_deletes(tmp_path):
    fake = FakeGcloud(raise_on="exec",
                      raise_exc=subprocess.TimeoutExpired(cmd=["ssh"], timeout=1))
    res = _run_gate(tmp_path, fake, cfg=_cfg(**_CD_SPEC))
    assert res["fell_back_local"] is True and _deleted(fake)


def test_create_delete_describe_failure_falls_back_and_still_deletes(tmp_path):
    fake = FakeGcloud(describe_rc=1)
    res = _run_gate(tmp_path, fake, cfg=_cfg(**_CD_SPEC))
    assert res["fell_back_local"] is True and _deleted(fake)


def test_node_down_already_absent_is_ok_and_never_raises():
    lines: list[str] = []
    fake = FakeGcloud(down_rc=1, down_err=_NOT_FOUND_ERR)
    rc = rg._node_down(fake, "create-delete", rg.node_params(_cfg()), lines.append)
    assert rc == 0 and "already absent" in lines[-1]


def test_node_down_real_failure_returns_rc_and_raise_is_absorbed():
    fake = FakeGcloud(down_rc=1, down_err="ERROR: quota")
    assert rg._node_down(fake, "create-delete", rg.node_params(_cfg()), lambda _m: None) == 1
    boom = FakeGcloud(raise_on="down", raise_exc=subprocess.TimeoutExpired(["g"], 1))
    assert rg._node_down(boom, "create-delete", rg.node_params(_cfg()), lambda _m: None) is None


# ---------------------------------------------------------------------------
# node-up / node-down CLI (scripts/app-test-node start|stop) + node_script_env
# ---------------------------------------------------------------------------

def test_node_cli_up_applies_env_overrides_and_creates(capsys):
    fake = FakeGcloud(status=None)
    rc = rg._node_cli("node-up", run=fake, cfg=_cfg(**_CD_SPEC),
                      environ={"APP_TEST_NODE": "n-cd", "APP_TEST_ZONE": "z2"})
    assert rc == 0
    for call in fake.calls:
        a = call["argv"]
        assert "n-cd" in a and a[a.index("--zone") + 1] == "z2"
    assert fake.kinds() == ["describe", "up"]
    assert "app-test-node:" in capsys.readouterr().err


def test_node_cli_down_deletes_and_absent_is_rc0():
    fake = FakeGcloud()
    assert rg._node_cli("node-down", run=fake, cfg=_cfg(**_CD_SPEC), environ={}) == 0
    assert "delete" in fake.calls[0]["argv"] and "n" in fake.calls[0]["argv"]
    gone = FakeGcloud(down_rc=1, down_err=_NOT_FOUND_ERR)
    assert rg._node_cli("node-down", run=gone, cfg=_cfg(**_CD_SPEC), environ={}) == 0
    failed = FakeGcloud(down_rc=1, down_err="ERROR: quota")
    assert rg._node_cli("node-down", run=failed, cfg=_cfg(**_CD_SPEC), environ={}) == 1


def test_node_cli_start_stop_mode_uses_start_and_stop():
    up = FakeGcloud()
    assert rg._node_cli("node-up", run=up, cfg=_cfg(), environ={}) == 0
    assert up.kinds() == ["up"] and "start" in up.calls[0]["argv"]
    down = FakeGcloud()
    assert rg._node_cli("node-down", run=down, cfg=_cfg(), environ={}) == 0
    assert "stop" in down.calls[0]["argv"]


def test_node_cli_up_failure_is_rc1():
    fake = FakeGcloud(status=None, up_rc=1)
    assert rg._node_cli("node-up", run=fake, cfg=_cfg(**_CD_SPEC), environ={}) == 1


def test_node_cli_without_config_is_unavailable_and_touches_nothing():
    fake = FakeGcloud()
    assert rg._node_cli("node-up", run=fake, cfg={}, environ={}) == rg.NODE_CLI_UNAVAILABLE
    assert rg._node_cli("node-down", run=fake, cfg={}, environ={}) == rg.NODE_CLI_UNAVAILABLE
    assert fake.calls == []


def test_main_dispatches_node_subcommands(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(rg, "_node_cli", lambda action, **_kw: seen.append(action) or 0)
    assert rg._main(["node-up"]) == 0 and rg._main(["node-down"]) == 0
    assert seen == ["node-up", "node-down"]


def test_node_script_env_maps_config_and_pins_python():
    env = rg.node_script_env({"node": "n", "zone": "z"}, {"KEEP": "1"})
    assert env["APP_TEST_NODE"] == "n" and env["APP_TEST_ZONE"] == "z"
    assert env["KEEP"] == "1" and env["APP_TEST_PYTHON"]
    bare = rg.node_script_env({}, {})
    assert "APP_TEST_NODE" not in bare and "APP_TEST_ZONE" not in bare


def test_per_run_timeout_default_is_the_vm_ttl(tmp_path):
    cfg = _cfg(per_run_vm_ttl_min=30)
    fake = FakeGcloud(harvest_xml=[_XML_A])
    _run_gate(tmp_path, fake, cfg=cfg)
    # the exec ssh call is given the per-run cap (30 min) as its timeout
    exec_call = next(c for c, k in zip(fake.calls, fake.kinds()) if k == "exec")
    assert exec_call["timeout"] == 30 * 60


def test_monthly_cap_vetoes_remote_without_touching_a_node_and_alerts_once(tmp_path):
    budget = tmp_path / "budget.json"
    cfg = _cfg(monthly_cap_usd=1.0)
    rg.record_cost(2.0, _NOW, path=budget)       # already over the $1 cap this month
    assert rg.budget_exhausted(cfg, _NOW, path=budget) is True

    alerts: list[str] = []
    fake1 = FakeGcloud(harvest_xml=[_XML_A])
    res1 = rg.run_remote_gate(
        "demo", "https://example.test/repo.git", "deadbeef", _CMD, cfg=cfg,
        run=fake1, jobs_dir=tmp_path / "jobs", budget_path=budget, now=_NOW,
        notify=alerts.append)
    assert res1["fell_back_local"] is True
    assert fake1.calls == []                     # NO node started when over cap
    assert len(alerts) == 1                       # exactly one throttled alert
    assert (tmp_path / "jobs" / "demo-remote-1.json").is_file()

    # A second over-cap run in the same month must NOT alert again (throttle).
    fake2 = FakeGcloud(harvest_xml=[_XML_A])
    res2 = rg.run_remote_gate(
        "demo", "https://example.test/repo.git", "deadbeef", _CMD, cfg=cfg,
        run=fake2, jobs_dir=tmp_path / "jobs", budget_path=budget, now=_NOW,
        notify=alerts.append)
    assert res2["fell_back_local"] is True
    assert fake2.calls == []
    assert len(alerts) == 1                       # still exactly one


def test_record_cost_accumulates_within_a_month(tmp_path):
    budget = tmp_path / "budget.json"
    assert rg.record_cost(0.5, _NOW, path=budget) == 0.5
    assert rg.record_cost(0.25, _NOW, path=budget) == 0.75
    assert rg.month_spent_usd(_NOW, path=budget) == 0.75


# ---------------------------------------------------------------------------
# AC#5 — mulkko-118 reproduction: prior 60-min timeout → remote → rc=0 → PASS
# ---------------------------------------------------------------------------

def test_mulkko_118_prior_timeout_routes_remote_and_rc0_is_a_passed_gate(tmp_path):
    runs = tmp_path / "runs"
    # A prior run recorded the full suite hitting the 60-min per-command cap
    # (mulkko-118's actual verify.md wording).
    _mk_run(runs, f"20261018T120803-{_SLUG}", verify_md=_VERIFY_MD_TIMED_OUT)

    # (b) the retry's Verify sees the prior timeout for the SAME command → remote.
    prior = rg.prior_run_timed_out(_SLUG, _CMD, runs_dir=runs)
    assert prior is True
    assert rg.decide(_CMD, gates={}, prior_timed_out=prior) == "remote"

    # The remote node runs it to rc=0 with a clean JUnit aggregate → a passed gate.
    fake = FakeGcloud(harvest_xml=[_XML_A, _XML_B])
    result = _run_gate(tmp_path, fake, slug=_SLUG)
    assert result["rc"] == 0 and result["fell_back_local"] is False
    assert result["junit"]["failures"] == 0 and result["junit"]["errors"] == 0
    assert rg.gate_passed(result) is True


def test_gate_passed_rejects_fallback_failure_and_dirty_junit():
    assert rg.gate_passed({"rc": 0, "fell_back_local": False,
                           "junit": {"failures": 0, "errors": 0}}) is True
    assert rg.gate_passed({"rc": 0, "fell_back_local": True, "junit": {}}) is False
    assert rg.gate_passed({"rc": 1, "fell_back_local": False, "junit": {}}) is False
    assert rg.gate_passed({"rc": None, "fell_back_local": True, "junit": {}}) is False
    assert rg.gate_passed({"rc": 0, "fell_back_local": False,
                           "junit": {"failures": 2, "errors": 0}}) is False
