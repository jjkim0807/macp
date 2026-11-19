"""Server deploy watch — a merged server change not in production → one alert.

Unit level: ``run_once`` against a fake ``run`` that plays git + the tag command
(``FakeRepo``). Tick level and real-git integration live further down.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from todo_system.orchestrator import server_deploy_watch as sdw

NOW = dt.datetime(2026, 11, 20, 12, 0, 0)
TAG_CMD = ["prod-tag", "describe", "<service>"]
DEPLOY = "~/.config/app/deploy.sh <sha>"
SECRET = "ERROR: token=s3cr3t-do-not-log"


def _sha(n: int) -> str:
    return f"{n:x}".rjust(7, "a") + "b" * 33


class FakeRepo:
    """A linear history on ``main`` played through the ``run`` seam.

    ``commits``: list of (sha, ct, subject, touches_server), oldest first.
    ``local``: shas the clone already has (``fetch`` adds every commit).
    ``tag``: what the tag command prints (or ``tag_rc``/``tag_exc`` to fail).
    """

    def __init__(self, commits, *, tag=None, local=None):
        self.commits = list(commits)
        self.local = set(local if local is not None else (c[0] for c in self.commits))
        self.tag = tag
        self.tag_rc = 0
        self.tag_stderr = ""
        self.tag_exc: BaseException | None = None
        self.calls: list[list[str]] = []
        self.raise_on_git: BaseException | None = None

    def _idx(self, sha):
        for i, c in enumerate(self.commits):
            if c[0] == sha:
                return i
        return None

    @staticmethod
    def _cp(argv, rc=0, out="", err=""):
        return subprocess.CompletedProcess(argv, rc, out, err)

    def __call__(self, argv, *, cwd=None, timeout=None):
        self.calls.append(list(argv))
        if argv == TAG_CMD:
            if self.tag_exc is not None:
                raise self.tag_exc
            return self._cp(argv, self.tag_rc, (self.tag or "") + "\n", self.tag_stderr)
        assert argv[0] == "git" and argv[1] == "-C", argv
        if self.raise_on_git is not None:
            raise self.raise_on_git
        sub, args = argv[3], argv[4:]
        tip = self.commits[-1][0]
        if sub == "symbolic-ref":
            return self._cp(argv, 0, "refs/remotes/origin/main\n")
        if sub == "ls-remote":
            return self._cp(argv, 0, f"{tip}\trefs/heads/main\n")
        if sub == "cat-file":
            sha = args[1].split("^", 1)[0]
            return self._cp(argv, 0 if sha in self.local else 1)
        if sub == "fetch":
            self.local |= {c[0] for c in self.commits}
            return self._cp(argv, 0)
        if sub == "rev-parse":
            ref = args[-1].split("^", 1)[0]
            hits = [c[0] for c in self.commits if c[0].startswith(ref) and c[0] in self.local]
            return self._cp(argv, 0, hits[0] + "\n") if len(hits) == 1 else self._cp(argv, 1)
        if sub == "merge-base":
            a, b = self._idx(args[1]), self._idx(args[2])
            return self._cp(argv, 0 if a <= b else 1)
        if sub == "log":
            assert "--" in args and all(p.startswith(":(glob)") for p in args[args.index("--") + 1:])
            rng = args[args.index("--") - 1]
            if args[0] == "-1":
                upto = self._idx(rng)
                srv = [c for c in self.commits[:upto + 1] if c[3]]
                return self._cp(argv, 0, (srv[-1][0] + "\n") if srv else "")
            lo, hi = rng.split("..")
            lo_i, hi_i = self._idx(lo), self._idx(hi)
            rows = [c for c in self.commits[lo_i + 1:hi_i + 1] if c[3]]
            return self._cp(argv, 0, "".join(f"{c[0]}\t{c[1]}\t{c[2]}\n" for c in reversed(rows)))
        raise AssertionError(f"unexpected git subcommand {sub}")


def _ct(minutes_ago: float) -> int:
    return int((NOW - dt.timedelta(minutes=minutes_ago)).timestamp())


def _vault(tmp_path: Path, repos: dict | None = None) -> Path:
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    if repos is None:
        repos = {"app": {"server_paths": ["server/**"], "tag_cmd": TAG_CMD, "deploy_cmd": DEPLOY}}
    (vault / ".macp" / "server_deploy_watch.json").write_text(
        json.dumps({"_readme": "x", "repos": repos}), encoding="utf-8")
    return vault


def _worker_root(tmp_path: Path, *names: str) -> Path:
    wr = tmp_path / "worker"
    for n in names or ("app",):
        (wr / n / ".git").mkdir(parents=True)
    return wr


class Recorder:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, msg, *, event_key=None):
        self.sent.append((msg, event_key))


def _go(vault, wr, fake, notify, now, logs=None):
    return sdw.run_once(vault, notify=notify, now=now, run=fake, worker_root=wr,
                        progress=(logs.append if logs is not None else (lambda s: None)))


C1, C2, C3, C4 = _sha(1), _sha(2), _sha(3), _sha(4)


def _behind_repo(c3_age=120):
    # deployed = C1; C2 app-only; C3 server change not yet deployed
    return FakeRepo([(C1, _ct(600), "server: base", True),
                     (C2, _ct(300), "app: ui", False),
                     (C3, _ct(c3_age), "server: fix crash", True)],
                    tag=f"<registry>/app/server:{C1[:7]}")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_no_config_file_is_silent_and_writes_no_state(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    fake = _behind_repo()
    rec = Recorder()
    assert _go(vault, _worker_root(tmp_path), fake, rec, NOW) == []
    assert fake.calls == [] and rec.sent == []
    assert not sdw.state_path(vault).exists()


def test_corrupt_or_empty_config_is_silent(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    sdw.config_path(vault).write_text("{not json", encoding="utf-8")
    fake = _behind_repo()
    assert _go(vault, _worker_root(tmp_path), fake, Recorder(), NOW) == []
    assert fake.calls == [] and not sdw.state_path(vault).exists()


def test_repo_without_entry_is_ignored(tmp_path):
    vault = _vault(tmp_path)
    wr = _worker_root(tmp_path, "app", "other")
    fake = _behind_repo()
    _go(vault, wr, fake, Recorder(), NOW)
    assert all(str(wr / "other") not in a for a in fake.calls)


@pytest.mark.parametrize("bad", [
    {"server_paths": ["server/**"], "tag_cmd": "gcloud run services describe x", "deploy_cmd": DEPLOY},
    {"server_paths": [], "tag_cmd": TAG_CMD, "deploy_cmd": DEPLOY},
    {"server_paths": ["server/**"], "tag_cmd": TAG_CMD},
    {"server_paths": ["server/**"], "tag_cmd": TAG_CMD, "deploy_cmd": DEPLOY, "grace_minutes": "30"},
    "not-a-dict",
])
def test_invalid_config_entry_is_skipped_without_alert(tmp_path, bad):
    vault = _vault(tmp_path, {"app": bad})
    fake = _behind_repo()
    rec = Recorder()
    logs: list[str] = []
    assert _go(vault, _worker_root(tmp_path), fake, rec, NOW, logs) == []
    assert fake.calls == [] and rec.sent == []
    assert any("invalid config entry 'app'" in s for s in logs)


def test_validate_entry_rejects_path_like_repo_names():
    ok = {"server_paths": ["server/**"], "tag_cmd": TAG_CMD, "deploy_cmd": DEPLOY}
    assert sdw.validate_entry("app", ok)["grace_s"] == 30 * 60
    assert sdw.validate_entry("../etc", ok) is None
    assert sdw.validate_entry("..", ok) is None


# ---------------------------------------------------------------------------
# behind / grace / throttle / clear
# ---------------------------------------------------------------------------

def test_behind_within_grace_does_not_alert(tmp_path):
    vault = _vault(tmp_path)
    rec = Recorder()
    assert _go(vault, _worker_root(tmp_path), _behind_repo(c3_age=10), rec, NOW) == []
    assert rec.sent == []


def test_behind_past_grace_sends_one_alert_naming_repo_commits_and_deploy_cmd(tmp_path):
    vault = _vault(tmp_path)
    wr = _worker_root(tmp_path)
    rec = Recorder()
    assert _go(vault, wr, _behind_repo(), rec, NOW) == ["app"]
    assert len(rec.sent) == 1
    msg, key = rec.sent[0]
    assert "app" in msg and "서버 미배포" in msg
    assert f"{C3[:7]} server: fix crash" in msg
    assert C2[:7] not in msg                      # app-only commit is not a server change
    assert f"~/.config/app/deploy.sh {C3[:7]}" in msg
    assert f"운영 태그: {C1[:7]}" in msg
    assert key == f"server-undeployed:app:{C3[:12]}"
    state = json.loads(sdw.state_path(vault).read_text())
    assert state["repos"]["app"]["alerted_sha"] == C3


def test_same_undeployed_commit_does_not_repeat(tmp_path):
    vault = _vault(tmp_path)
    wr = _worker_root(tmp_path)
    rec = Recorder()
    fake = _behind_repo()
    _go(vault, wr, fake, rec, NOW)
    for k in range(1, 4):  # every later tick past the throttle
        assert _go(vault, wr, fake, rec, NOW + dt.timedelta(minutes=11 * k)) == []
    assert len(rec.sent) == 1


def test_min_poll_throttle_skips_run_inside_600s(tmp_path):
    vault = _vault(tmp_path)
    wr = _worker_root(tmp_path)
    fake = _behind_repo(c3_age=5)
    _go(vault, wr, fake, Recorder(), NOW)
    n = len(fake.calls)
    _go(vault, wr, fake, Recorder(), NOW + dt.timedelta(seconds=599))
    assert len(fake.calls) == n


def test_newer_undeployed_commit_alerts_again_only_after_its_own_grace(tmp_path):
    vault = _vault(tmp_path)
    wr = _worker_root(tmp_path)
    rec = Recorder()
    fake = _behind_repo()
    _go(vault, wr, fake, rec, NOW)
    assert len(rec.sent) == 1
    t1 = NOW + dt.timedelta(minutes=20)
    fake.commits.append((C4, int(t1.timestamp()), "server: second fix", True))
    fake.local.add(C4)
    assert _go(vault, wr, fake, rec, t1 + dt.timedelta(minutes=10)) == []    # C4 only 10 min old
    assert _go(vault, wr, fake, rec, t1 + dt.timedelta(minutes=31)) == ["app"]
    assert len(rec.sent) == 2
    assert f"{C4[:7]} server: second fix" in rec.sent[1][0]
    assert f"{C3[:7]} server: fix crash" in rec.sent[1][0]


@pytest.mark.parametrize("deployed", ["C3", "C4"])
def test_production_at_or_past_newest_clears_state_silently(tmp_path, deployed):
    vault = _vault(tmp_path)
    wr = _worker_root(tmp_path)
    rec = Recorder()
    fake = _behind_repo()
    fake.commits.append((C4, _ct(60), "docs: readme", False))   # non-server descendant
    _go(vault, wr, fake, rec, NOW)
    assert len(rec.sent) == 1
    fake.tag = {"C3": C3, "C4": C4}[deployed][:7]
    logs: list[str] = []
    assert _go(vault, wr, fake, rec, NOW + dt.timedelta(minutes=11), logs) == []
    assert len(rec.sent) == 1
    assert "alerted_sha" not in json.loads(sdw.state_path(vault).read_text())["repos"]["app"]
    assert any("caught up" in s for s in logs)


def test_current_from_the_start_sends_nothing(tmp_path):
    vault = _vault(tmp_path)
    fake = _behind_repo()
    fake.tag = C3[:7]
    rec = Recorder()
    assert _go(vault, _worker_root(tmp_path), fake, rec, NOW) == []
    assert rec.sent == []


def test_missing_tip_commit_is_fetched_once(tmp_path):
    vault = _vault(tmp_path)
    fake = _behind_repo()
    fake.local = {C1}
    rec = Recorder()
    assert _go(vault, _worker_root(tmp_path), fake, rec, NOW) == ["app"]
    assert sum(1 for a in fake.calls if a[3:4] == ["fetch"]) == 1


def test_no_server_commit_ever_skips_tag_command(tmp_path):
    vault = _vault(tmp_path)
    fake = FakeRepo([(C1, _ct(600), "app only", False)], tag=C1[:7])
    _go(vault, _worker_root(tmp_path), fake, Recorder(), NOW)
    assert TAG_CMD not in fake.calls


def test_unresolvable_tag_is_incomparable_no_alert_state_unchanged(tmp_path):
    vault = _vault(tmp_path)
    fake = _behind_repo()
    fake.tag = "deadbee"
    rec = Recorder()
    logs: list[str] = []
    assert _go(vault, _worker_root(tmp_path), fake, rec, NOW, logs) == []
    assert rec.sent == []
    assert json.loads(sdw.state_path(vault).read_text())["repos"]["app"] == {}
    assert any("incomparable" in s for s in logs)


def test_no_checkout_skips_without_running_anything(tmp_path):
    vault = _vault(tmp_path)
    wr = tmp_path / "worker"
    wr.mkdir()
    fake = _behind_repo()
    assert _go(vault, wr, fake, Recorder(), NOW) == []
    assert fake.calls == []


# ---------------------------------------------------------------------------
# tag command failures
# ---------------------------------------------------------------------------

def test_tag_failure_alerts_once_per_day_with_rc_only(tmp_path, capsys):
    vault = _vault(tmp_path)
    wr = _worker_root(tmp_path)
    fake = _behind_repo()
    fake.tag, fake.tag_rc, fake.tag_stderr = SECRET, 1, SECRET
    rec = Recorder()
    sdw.run_once(vault, notify=rec, now=NOW, run=fake, worker_root=wr)
    sdw.run_once(vault, notify=rec, now=NOW + dt.timedelta(hours=2), run=fake, worker_root=wr)
    assert len(rec.sent) == 1
    msg, key = rec.sent[0]
    assert "rc=1" in msg and "운영 태그를 읽을 수 없음" in msg
    assert "s3cr3t" not in msg
    assert key == "server-tag-unreadable:app:2026-11-20"
    sdw.run_once(vault, notify=rec, now=NOW + dt.timedelta(days=1), run=fake, worker_root=wr)
    assert len(rec.sent) == 2
    assert "s3cr3t" not in capsys.readouterr().out


def test_tag_failure_leaves_behind_alert_state_alone(tmp_path):
    vault = _vault(tmp_path)
    wr = _worker_root(tmp_path)
    fake = _behind_repo()
    rec = Recorder()
    _go(vault, wr, fake, rec, NOW)
    fake.tag_rc = 2
    _go(vault, wr, fake, rec, NOW + dt.timedelta(minutes=11))
    assert json.loads(sdw.state_path(vault).read_text())["repos"]["app"]["alerted_sha"] == C3


def test_tag_timeout_reports_rc_timeout(tmp_path):
    vault = _vault(tmp_path)
    fake = _behind_repo()
    fake.tag_exc = subprocess.TimeoutExpired(TAG_CMD, 60)
    rec = Recorder()
    _go(vault, _worker_root(tmp_path), fake, rec, NOW)
    assert len(rec.sent) == 1 and "rc=timeout" in rec.sent[0][0]


def test_tag_unparseable_output_reports_rc0_unparseable(tmp_path):
    vault = _vault(tmp_path)
    fake = _behind_repo()
    fake.tag = SECRET
    rec = Recorder()
    _go(vault, _worker_root(tmp_path), fake, rec, NOW)
    assert len(rec.sent) == 1 and "rc=0(unparseable)" in rec.sent[0][0]
    assert "s3cr3t" not in rec.sent[0][0]


@pytest.mark.parametrize("out,want", [
    ("abc1234", "abc1234"),
    ("<registry>/proj/app/server:abc1234\n", "abc1234"),
    ("reg.example:5000/app/server:ABC1234@sha256:" + "f" * 64, "abc1234"),
    ("warning line\nabc1234def\n", "abc1234def"),
    ("<registry>/app/server", None),
    ("<registry>/app/server:latest", None),
    ("", None),
    (None, None),
])
def test_parse_tag(out, want):
    assert sdw.parse_tag(out) == want


# ---------------------------------------------------------------------------
# never deploys
# ---------------------------------------------------------------------------

def test_never_deploys_only_allowed_git_or_tag_cmd_run(tmp_path):
    vault = _vault(tmp_path, {"app": {"server_paths": ["server/**"], "tag_cmd": TAG_CMD,
                                       "deploy_cmd": "deploy-app.sh <sha>"}})
    wr = _worker_root(tmp_path)
    fake = _behind_repo()
    fake.local = {C1}
    rec = Recorder()
    _go(vault, wr, fake, rec, NOW)                                 # fetch + alert
    fake.tag = C3[:7]
    _go(vault, wr, fake, rec, NOW + dt.timedelta(minutes=11))      # cleared
    fake.tag_rc = 1
    _go(vault, wr, fake, rec, NOW + dt.timedelta(minutes=22))      # tag failure
    assert len(rec.sent) == 2
    for argv in fake.calls:
        assert argv == TAG_CMD or (argv[0] == "git" and argv[3] in sdw._ALLOWED_GIT), argv
        assert "deploy-app.sh" not in " ".join(argv)


def test_git_helper_refuses_mutating_subcommands(tmp_path):
    for sub in ("checkout", "reset", "clean", "push", "worktree"):
        with pytest.raises(ValueError):
            sdw._git(tmp_path, [sub], lambda *a, **k: None)


def test_one_repo_failure_does_not_stop_others(tmp_path):
    entry = {"server_paths": ["server/**"], "tag_cmd": TAG_CMD, "deploy_cmd": DEPLOY}
    vault = _vault(tmp_path, {"bad": entry, "app": entry})
    wr = _worker_root(tmp_path, "bad", "app")
    good = _behind_repo()

    def run(argv, **kw):
        if argv[:3] == ["git", "-C", str(wr / "bad")]:
            raise RuntimeError("boom")
        return good(argv, **kw)

    rec = Recorder()
    assert _go(vault, wr, run, rec, NOW) == ["app"]


# ---------------------------------------------------------------------------
# tick level — the real Orchestrator.tick() path
# ---------------------------------------------------------------------------

def _orch(tmp_path):
    from todo_system.orchestrator.assignment import Ledger
    from todo_system.orchestrator.mailbox import FileMailbox
    from todo_system.orchestrator.orchestrator import Orchestrator
    from todo_system.orchestrator.registry import Registry
    ledger = Ledger(tmp_path / "Worker" / "assignments")   # vault_root == tmp_path
    return Orchestrator(FileMailbox(tmp_path / "agents"), Registry([]), ledger,
                        notify=lambda *a, **k: None)


def _tick_vault(tmp_path, monkeypatch, fake):
    (tmp_path / ".macp").mkdir()
    sdw.config_path(tmp_path).write_text(json.dumps({"repos": {"app": {
        "server_paths": ["server/**"], "tag_cmd": TAG_CMD, "deploy_cmd": DEPLOY}}}),
        encoding="utf-8")
    wr = tmp_path / "clones"
    (wr / "app" / ".git").mkdir(parents=True)
    monkeypatch.setattr(sdw, "_default_worker_root", lambda: wr)
    monkeypatch.setattr(sdw, "_run", fake)


def _real_now_behind_repo():
    now = dt.datetime.now()

    def ct(minutes):
        return int((now - dt.timedelta(minutes=minutes)).timestamp())

    return FakeRepo([(C1, ct(600), "server: base", True),
                     (C2, ct(300), "app: ui", False),
                     (C3, ct(120), "server: fix crash", True)],
                    tag=f"<registry>/app/server:{C1[:7]}")


def _undeployed_calls(routed):
    return [c for c in routed.call_args_list
            if str(c.kwargs.get("event_key", "")).startswith("server-undeployed:")]


def test_tick_sends_one_alert_through_notify_routed(tmp_path, monkeypatch):
    from unittest import mock
    from todo_system.orchestrator.tests.test_orchestrator import _hermetic_tick
    fake = _real_now_behind_repo()
    _tick_vault(tmp_path, monkeypatch, fake)
    orch = _orch(tmp_path)
    with mock.patch.object(orch, "_notify_routed") as routed:
        out = _hermetic_tick(orch, [])
        assert isinstance(out, dict)
        calls = _undeployed_calls(routed)
        assert len(calls) == 1
        msg = calls[0].args[0]
        assert "app" in msg and f"{C3[:7]} server: fix crash" in msg
        assert f"~/.config/app/deploy.sh {C3[:7]}" in msg
        # past the min-poll throttle: same undeployed commit → no second alert
        sp = sdw.state_path(tmp_path)
        st = json.loads(sp.read_text())
        st["last_check_ts"] = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat()
        sp.write_text(json.dumps(st))
        n_calls = len(fake.calls)
        _hermetic_tick(orch, [])
        assert len(fake.calls) > n_calls                     # it did check again
        assert len(_undeployed_calls(routed)) == 1


def test_tick_is_fail_open_when_the_watch_raises(tmp_path, monkeypatch):
    from unittest import mock
    from todo_system.orchestrator.tests.test_orchestrator import _hermetic_tick
    fake = _real_now_behind_repo()
    fake.raise_on_git = RuntimeError("boom")
    _tick_vault(tmp_path, monkeypatch, fake)
    orch = _orch(tmp_path)

    def explode(*a, **k):
        raise RuntimeError("load failed")

    with mock.patch.object(orch, "_notify_routed") as routed:
        assert isinstance(_hermetic_tick(orch, []), dict)     # per-repo isolation
        monkeypatch.setattr(sdw, "load_state", explode)
        assert orch._server_deploy_watch_tick() == []          # wrapper isolation
        assert isinstance(_hermetic_tick(orch, []), dict)
        assert not [c for c in routed.call_args_list
                    if str(c.kwargs.get("event_key", "")).startswith("server-")]


def test_tick_without_config_is_silent(tmp_path, monkeypatch):
    from todo_system.orchestrator.tests.test_orchestrator import _hermetic_tick
    fake = _real_now_behind_repo()
    monkeypatch.setattr(sdw, "_run", fake)
    orch = _orch(tmp_path)
    assert orch._server_deploy_watch_tick() == []
    _hermetic_tick(orch, [])
    assert fake.calls == []
    assert not sdw.state_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# real git — catches argv shapes (pathspec magic, ^{commit}) a fake would accept
# ---------------------------------------------------------------------------

def _git(cwd, *args, when=None):
    import os
    env = dict(os.environ)
    if when is not None:
        stamp = f"{int(when.timestamp())} +0000"
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
                        *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _commit(seed, rel, msg, when):
    f = seed / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(msg, encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", msg, when=when)
    return _git(seed, "rev-parse", "HEAD")


def test_real_git_behind_then_cleared(tmp_path):
    import shutil
    import sys
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    now = dt.datetime.now().replace(microsecond=0)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    c1 = _commit(seed, "server/a.py", "server: base", now - dt.timedelta(hours=10))
    _git(seed, "push", "-q", "origin", "main")
    wr = tmp_path / "worker"
    wr.mkdir()
    _git(wr, "clone", "-q", str(origin), "app")              # runner checkout: only c1
    c2 = _commit(seed, "app/x.kt", "app: ui", now - dt.timedelta(hours=3))
    c3 = _commit(seed, "server/sub/b.py", "server: fix crash", now - dt.timedelta(hours=2))
    _git(seed, "push", "-q", "origin", "main")

    tagfile = tmp_path / "tag.txt"
    tagfile.write_text(f"<registry>/app/server:{c1[:7]}\n", encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    tag_cmd = [sys.executable, "-c", "import sys;print(open(sys.argv[1]).read())", str(tagfile)]
    sdw.config_path(vault).write_text(json.dumps({"repos": {"app": {
        "server_paths": ["server/**"], "tag_cmd": tag_cmd, "deploy_cmd": DEPLOY}}}),
        encoding="utf-8")
    head_before = _git(wr / "app", "rev-parse", "HEAD")

    rec = Recorder()
    assert sdw.run_once(vault, notify=rec, now=now, worker_root=wr,
                        progress=lambda s: None) == ["app"]
    assert len(rec.sent) == 1
    msg = rec.sent[0][0]
    assert f"{c3[:7]} server: fix crash" in msg and c2[:7] not in msg
    assert f"deploy.sh {c3[:7]}" in msg
    assert _git(wr / "app", "rev-parse", "HEAD") == head_before   # checkout untouched

    c4 = _commit(seed, "docs/readme.md", "docs: readme", now - dt.timedelta(minutes=5))
    _git(seed, "push", "-q", "origin", "main")
    tagfile.write_text(f"<registry>/app/server:{c4[:7]}\n", encoding="utf-8")
    assert sdw.run_once(vault, notify=rec, now=now + dt.timedelta(minutes=11), worker_root=wr,
                        progress=lambda s: None) == []
    assert len(rec.sent) == 1
    assert "alerted_sha" not in json.loads(sdw.state_path(vault).read_text())["repos"]["app"]
