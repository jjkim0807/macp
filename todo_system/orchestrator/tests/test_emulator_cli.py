"""Unit tests for the stage-facing emulator CLI (``scripts/macp-emulator`` →
:mod:`todo_system.orchestrator.emulator_cli`).

Every external effect flows through an injected fake ``run`` and tmp lock/budget
seams, so nothing shells out to gcloud / gradle / an emulator and nothing touches
the real vault. The contract pinned here:

* exit codes — 0 PASS · 75 node lease held by another owner · 76 over the shared
  monthly budget · 64 unknown subcommand / operator-only ``stop`` inside a stage ·
  1 a real failure (build/scenario/crash/config);
* always-stop — a build failure still stops the node;
* aggregates-only stdout — the printed JSON parses and carries logcat counts,
  per-step ``duration_s`` and screenshot paths, and NEVER the owner-seed source
  path or any dump text;
* the shared node lease (owner = run_id) and the ``--seed owner`` / ``--scenario``
  resolution from the target repo's / vault's ``.macp/``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from todo_system.orchestrator import emulator_cli as ec
from todo_system.orchestrator import lease


# ---------------------------------------------------------------------------
# Fakes — a compact twin of test_beta_smoke.FakeNode (every node effect by argv)
# ---------------------------------------------------------------------------

def _dump(title: str, timer: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<hierarchy rotation="0">\n'
        f'  <node resource-id="com.mulkko:id/task_title" text="{title}"'
        ' bounds="[48,300][1032,420]"/>\n'
        f'  <node resource-id="com.mulkko:id/timer_text" text="{timer}"'
        ' bounds="[300,600][780,900]"/>\n'
        '  <node resource-id="com.mulkko:id/skip_button" text="⏭"'
        ' content-desc="건너뛰기" bounds="[840,2100][1032,2292]"/>\n'
        '</hierarchy>\n'
    )


SMOKE_CFG = {
    "gradle_task": ":app:assembleDebug",
    "avd": "api30",
    "fields": {"title": {"id": "task_title"}, "timer": {"id": "timer_text"}},
    "scenario": [
        {"name": "skip advances to the next task", "tap": {"id": "skip_button"},
         "expect": {"kind": "text_changes", "field": "title", "within_s": 10}},
    ],
    "poll_interval_s": 0,                            # tests never sleep
}
REMOTE_CFG = {"node": "app-test", "zone": "z", "hourly_rate_usd": 0.04, "monthly_cap_usd": 20}


class FakeNode:
    """Records every call; dispatches canned results by argv. ``raise_on`` an ssh
    kind or node token drives the exception paths; ``build_rc=1`` a build failure."""

    def __init__(self, *, dumps=None, logcat="I/App: started\nI/App: ok\n",
                 build_rc=0, install_rc=0,
                 apk="app/build/outputs/apk/debug/app-debug.apk", pkg="com.mulkko"):
        self.calls: list[dict] = []
        self.dumps = list(dumps) if dumps else [_dump("운동하기", "0:30")]
        self._dump_i = 0
        self.logcat = logcat
        self.build_rc = build_rc
        self.install_rc = install_rc
        self.apk = apk
        self.pkg = pkg

    @staticmethod
    def _token(argv) -> str:
        if argv[:1] == ["git"] and "archive" in argv:
            return "git-archive"
        if len(argv) >= 3 and argv[0] == "bash":
            return argv[2]
        return argv[0] if argv else ""

    @staticmethod
    def _ssh_kind(cmd: str) -> str:
        for needle, kind in (("gradlew", "build"), ("install -r", "install"),
                             ("uiautomator dump", "dump"), ("input tap", "tap"),
                             ("logcat -d", "logcat"), ("run-as", "seed"),
                             ("screencap", "screencap")):
            if needle in cmd:
                return kind
        return "ssh-other"

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        argv = list(argv)
        self.calls.append({"argv": argv, "timeout": timeout})
        token = self._token(argv)

        def cp(rc=0, out=""):
            return subprocess.CompletedProcess(argv, rc, out, "")

        if token == "pull":
            try:
                Path(argv[4]).parent.mkdir(parents=True, exist_ok=True)
                Path(argv[4]).write_bytes(b"\x89PNG\r\n")
            except (OSError, IndexError):
                pass
            return cp(0)
        if token in ("start", "git-archive", "push", "boot", "stop"):
            return cp(0)
        if token == "ssh":
            kind = self._ssh_kind(argv[3]) if len(argv) > 3 else "ssh-other"
            if kind == "build":
                return cp(self.build_rc,
                          f"> Task\n{self.apk}" if self.build_rc == 0 else "BUILD FAILED")
            if kind == "install":
                return cp(self.install_rc, self.pkg)
            if kind == "dump":
                d = self.dumps[min(self._dump_i, len(self.dumps) - 1)]
                self._dump_i += 1
                return cp(0, d)
            if kind == "logcat":
                return cp(0, self.logcat)
            return cp(0, "")
        return cp(0, "")

    def tokens(self):
        return [self._token(c["argv"]) for c in self.calls]

    def ssh_kinds(self):
        return [self._ssh_kind(c["argv"][3]) for c in self.calls
                if self._token(c["argv"]) == "ssh" and len(c["argv"]) > 3]


def _advancing():
    return FakeNode(dumps=[_dump("운동하기", "0:30"), _dump("운동하기", "0:30"),
                           _dump("독서하기", "0:30")])


def _scenario_file(tmp_path, cfg=None) -> Path:
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(cfg if cfg is not None else SMOKE_CFG), encoding="utf-8")
    return p


def _run_cli(tmp_path, fake, *, scenario=None, seed="none", owner=None,
             budget_usd=0.0, remote_cfg=REMOTE_CFG, notify=None, now=None,
             lock_path=None):
    budget = tmp_path / "budget.json"
    if budget_usd:
        import datetime as dt
        month = (now or dt.datetime.now().astimezone()).strftime("%Y-%m")
        budget.write_text(
            json.dumps({"months": {month: {"usd": budget_usd, "alerted": False}}}),
            encoding="utf-8")
    argv = ["run", "--repo-dir", str(tmp_path / "clone"), "--sha", "b" * 40,
            "--scenario", str(scenario if scenario is not None else _scenario_file(tmp_path)),
            "--seed", seed]
    if owner is not None:
        argv += ["--owner", owner]
    return ec._main(
        argv, run=fake, node_script=tmp_path / "app-test-node",
        lock_path=lock_path if lock_path is not None else tmp_path / "node.lock",
        result_root=tmp_path / "results", budget_path=budget,
        remote_cfg=remote_cfg, notify=notify, now=now)


# ---------------------------------------------------------------------------
# run — exit-code contract + aggregates-only JSON
# ---------------------------------------------------------------------------

def test_run_pass_returns_0_and_prints_aggregates_only_json(tmp_path, capsys):
    rc = _run_cli(tmp_path, _advancing())
    assert rc == ec.EXIT_OK
    out = capsys.readouterr().out
    res = json.loads(out)                        # stdout is pure JSON
    assert res["ok"] is True
    assert res["kind"] is None
    assert res["logcat"] == {"fatal": 0, "anr": 0, "max_skipped_frames": 0}
    assert res["steps"] and "duration_s" in res["steps"][0]
    assert isinstance(res["screenshots"], list)


def test_run_lease_held_by_other_owner_returns_75_zero_node_calls(tmp_path, capsys):
    squatter = lease.Lease(str(tmp_path / "node.lock"), holder="otherhost:999999", ttl=100_000)
    assert squatter.acquire()
    try:
        fake = FakeNode()
        rc = _run_cli(tmp_path, fake)
        assert rc == ec.EXIT_LEASE_BUSY
        assert fake.calls == []                  # never drove the node
        res = json.loads(capsys.readouterr().out)
        assert res["locked"] is True
        assert res["kind"] == "node"
    finally:
        squatter.release()


def test_run_over_budget_returns_76_zero_node_calls(tmp_path, capsys):
    fake = FakeNode()
    alerts: list[str] = []
    rc = _run_cli(tmp_path, fake, budget_usd=20.0, notify=alerts.append)
    assert rc == ec.EXIT_OVER_BUDGET
    assert fake.calls == []                      # don't spend when over cap
    res = json.loads(capsys.readouterr().out)
    assert res["kind"] == "budget"
    assert len(alerts) == 1                      # one throttled monthly alert


def test_run_build_failure_returns_1_and_stops_the_node(tmp_path, capsys):
    fake = FakeNode(build_rc=1)
    rc = _run_cli(tmp_path, fake)
    assert rc == ec.EXIT_FAILURE
    res = json.loads(capsys.readouterr().out)
    assert res["kind"] == "build"
    assert "stop" in fake.tokens()               # always-stop even on build failure


def test_run_scenario_failure_returns_1(tmp_path, capsys):
    fake = FakeNode(dumps=[_dump("운동하기", "0:30")])     # title never advances
    rc = _run_cli(tmp_path, fake)
    assert rc == ec.EXIT_FAILURE
    assert json.loads(capsys.readouterr().out)["kind"] == "scenario"


def test_run_crash_returns_1_with_logcat_aggregate(tmp_path, capsys):
    fake = FakeNode(dumps=[_dump("운동하기", "0:30"), _dump("운동하기", "0:30"),
                           _dump("독서하기", "0:30")],
                    logcat="E/AndroidRuntime: FATAL EXCEPTION: main\n")
    rc = _run_cli(tmp_path, fake)
    assert rc == ec.EXIT_FAILURE
    res = json.loads(capsys.readouterr().out)
    assert res["kind"] == "crash"
    assert res["logcat"]["fatal"] == 1


# ---------------------------------------------------------------------------
# owner / lease holder (AC3)
# ---------------------------------------------------------------------------

def test_run_owner_flag_becomes_the_lease_holder(tmp_path, monkeypatch):
    seen: list = []
    RealLease = ec.lease.Lease

    def spy(path, holder=None, ttl=120.0):
        obj = RealLease(path, holder=holder, ttl=ttl)
        seen.append(obj)
        return obj

    monkeypatch.setattr(ec.lease, "Lease", spy)
    rc = _run_cli(tmp_path, _advancing(), owner="20261023T234436-macp-worker-emulator-access")
    assert rc == ec.EXIT_OK
    assert seen[-1].holder == "20261023T234436-macp-worker-emulator-access"


def test_run_owner_falls_back_to_env(tmp_path, monkeypatch):
    seen: list = []
    RealLease = ec.lease.Lease

    def spy(path, holder=None, ttl=120.0):
        obj = RealLease(path, holder=holder, ttl=ttl)
        seen.append(obj)
        return obj

    monkeypatch.setattr(ec.lease, "Lease", spy)
    monkeypatch.setenv(ec._OWNER_ENV, "RUNID-from-env")
    rc = _run_cli(tmp_path, _advancing())
    assert rc == ec.EXIT_OK
    assert seen[-1].holder == "RUNID-from-env"


# ---------------------------------------------------------------------------
# --seed owner + --scenario resolution (public-split neutral)
# ---------------------------------------------------------------------------

def test_seed_owner_injects_source_from_config_and_never_leaks_it(tmp_path, capsys):
    secret = "/hq/private/owner-backup/mulkko.db"
    fake = FakeNode(dumps=[_dump("운동하기", "0:30"), _dump("운동하기", "0:30"),
                           _dump("독서하기", "0:30")])
    rc = _run_cli(tmp_path, fake, seed="owner",
                  remote_cfg={**REMOTE_CFG, "owner_seed_source": secret})
    assert rc == ec.EXIT_OK
    assert "seed" in fake.ssh_kinds()            # run-as cp happened
    out = capsys.readouterr().out
    json.loads(out)                              # stdout is still pure JSON
    assert secret not in out                     # the HQ seed path never reaches stdout
    assert "owner_seed_source" not in out        # nor the config key that named it


def test_seed_owner_without_source_is_a_config_error(tmp_path, capsys):
    fake = FakeNode()
    rc = _run_cli(tmp_path, fake, seed="owner", remote_cfg=dict(REMOTE_CFG))
    assert rc == ec.EXIT_FAILURE
    assert fake.calls == []                      # never started the node
    res = json.loads(capsys.readouterr().out)
    assert res["kind"] == "config"
    assert "owner_seed_source" in res["detail"]


def test_scenario_name_resolves_into_target_repo_dot_macp(tmp_path, capsys):
    repo = tmp_path / "clone"
    scen_dir = repo / ".macp" / "emulator"
    scen_dir.mkdir(parents=True)
    (scen_dir / "skip-flow.json").write_text(json.dumps(SMOKE_CFG), encoding="utf-8")
    # pass the NAME (not a path); it must resolve to repo/.macp/emulator/skip-flow.json
    rc = ec._main(
        ["run", "--repo-dir", str(repo), "--sha", "b" * 40, "--scenario", "skip-flow"],
        run=_advancing(), node_script=tmp_path / "n", lock_path=tmp_path / "node.lock",
        result_root=tmp_path / "results", budget_path=tmp_path / "budget.json",
        remote_cfg=REMOTE_CFG)
    assert rc == ec.EXIT_OK
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_missing_scenario_is_a_config_error(tmp_path, capsys):
    fake = FakeNode()
    rc = ec._main(
        ["run", "--repo-dir", str(tmp_path / "clone"), "--sha", "b" * 40,
         "--scenario", "does-not-exist"],
        run=fake, node_script=tmp_path / "n", lock_path=tmp_path / "node.lock",
        result_root=tmp_path / "results", budget_path=tmp_path / "budget.json",
        remote_cfg=REMOTE_CFG)
    assert rc == ec.EXIT_FAILURE
    assert fake.calls == []
    assert json.loads(capsys.readouterr().out)["kind"] == "config"


# ---------------------------------------------------------------------------
# status (read-only)
# ---------------------------------------------------------------------------

def test_status_free_prints_json(tmp_path, capsys):
    rc = ec._main(["status"], lock_path=tmp_path / "node.lock",
                  budget_path=tmp_path / "budget.json", remote_cfg=REMOTE_CFG)
    assert rc == ec.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["node_lease"]["state"] == "free"
    assert data["over_budget"] is False
    assert data["monthly_cap_usd"] == 20.0


def test_status_held_reflects_the_lease(tmp_path, capsys):
    held = lease.Lease(str(tmp_path / "node.lock"), holder="otherhost:1", ttl=100_000)
    assert held.acquire()
    try:
        rc = ec._main(["status"], lock_path=tmp_path / "node.lock",
                      budget_path=tmp_path / "budget.json", remote_cfg=REMOTE_CFG)
        assert rc == ec.EXIT_OK
        data = json.loads(capsys.readouterr().out)
        assert data["node_lease"]["state"] == "held"
        assert data["node_lease"]["holder"] == "otherhost:1"
    finally:
        held.release()


# ---------------------------------------------------------------------------
# stop (operator-only) + unknown subcommand
# ---------------------------------------------------------------------------

def test_stop_inside_a_stage_is_refused_64_without_a_node_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ec._OWNER_ENV, "20261023T234436-macp-worker-emulator-access")
    fake = FakeNode()
    rc = ec._main(["stop"], run=fake, node_script=tmp_path / "n", remote_cfg=REMOTE_CFG)
    assert rc == ec.EXIT_USAGE
    assert fake.calls == []                      # a stage must not stop the node


def test_stop_as_human_stops_the_node(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(ec._OWNER_ENV, raising=False)
    fake = FakeNode()
    rc = ec._main(["stop"], run=fake, node_script=tmp_path / "n", remote_cfg=REMOTE_CFG)
    assert rc == ec.EXIT_OK
    assert "stop" in fake.tokens()


def test_unknown_subcommand_returns_64(tmp_path):
    assert ec._main(["bogus"]) == ec.EXIT_USAGE
    assert ec._main([]) == ec.EXIT_USAGE
