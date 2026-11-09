"""The runner writes `<run_dir>/related.md` before any stage runs (AC#1).

The research stage's tools are `Read Write` — it cannot run a query, and
`TASK_TOOLS_STAGES` is implement-only so a task cannot widen that. So the engine
has to do the query and leave the answer as a file. These pin the runner half:

* the file really lands in the run dir the run just created, with the metadata;
* a query that EXPLODES changes nothing — no file, the same `ExecOutcome`, no
  exception out of `_execute`. That is the whole point of AC#1's "실패면 파일
  없이 진행(파이프라인을 막지 않음)": the pipeline must not die over a hint;
* zero related records writes no file at all (the `record_orphaned_commits`
  rule: nothing to say ⇒ no artifact to mislead a reader);
* the query gets the task's TITLE. The dispatch payload's title key is `summary`,
  not `title`, and `query_related` reads `title` — handing it the payload raw
  would silently drop the title from the query text, costing quality with no
  symptom. A test is the only thing that can notice that.

Harness is `test_runner.py`'s `OrphanedCommitEscalationTest` sandbox: every
vault-touching call is mocked and `RUNS_DIR` points into a tmp dir, so nothing
here writes to the real `Worker/runs/`.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import graph_memory as gm
from todo_system.orchestrator import message as m
from todo_system.orchestrator import related_context as rc
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

SLUG = "sample-task-alpha"
SUMMARY = "샘플 태스크 A의 제목"
RELATED_SLUG = "episode-00"
RELATED_TITLE = "에피소드 00"
RELATED_SOURCE = f"Memory/episodes/{RELATED_SLUG}.md"


def _agent(**kw) -> Agent:
    base = dict(id="armchair-commander", kind="ai-local",
                capabilities=["git"], repo="~/Worker/armchair-commander")
    base.update(kw)
    return Agent(**base)


def _item(slug: str = RELATED_SLUG, *, vector: float = 0.0) -> gm.RelatedItem:
    return gm.RelatedItem(
        slug=slug,
        source_file=RELATED_SOURCE,
        run_id=None,
        commit_sha=None,
        score=gm.RelatedScore(vector=vector, graph=0.5, combined=0.5 + vector / 2),
        edge_type="depends_on",
        hops=1,
    )


def _prepared(rc_code: int = 0) -> subprocess.CompletedProcess:
    cp = subprocess.CompletedProcess(args=["git", "checkout", "-B"], returncode=rc_code,
                                     stdout="", stderr="")
    cp.orphaned_commits = None
    return cp


class RunnerRelatedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "clone"
        self.repo.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        # A throwaway vault so the title lookup is deterministic and read-only.
        self.vault = self.tmp / "vault"
        doc = self.vault / "Memory" / "episodes" / f"{RELATED_SLUG}.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(f"---\ntitle: {RELATED_TITLE}\n---\n\nbody\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @contextlib.contextmanager
    def _sandbox(self):
        with mock.patch.object(repos, "ensure_worktree", return_value=self.repo), \
             mock.patch.object(stages, "restore_main"), \
             mock.patch.object(stages, "install_push_guard"), \
             mock.patch.object(stages, "default_branch", return_value="main"), \
             mock.patch.object(stages, "prepare_external_branch", return_value=_prepared()), \
             mock.patch.object(stages, "remove_worktree"), \
             mock.patch.object(stages, "RUNS_DIR", self.runs), \
             mock.patch.object(rc, "VAULT_ROOT", self.vault), \
             mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")), \
             mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None), \
             mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}):
            yield

    def _run(self, **payload):
        """One full `execute` with an empty stage pipeline: the run reaches run_dir
        creation (where related.md is written) and stops. auto_merge:False keeps
        `merge_landed`'s live `gh` call out of the path."""
        base = {"repo": "git@github.com:x/y.git", "auto_merge": False, "summary": SUMMARY}
        base.update(payload)
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id=SLUG, payload=base)
        lines: list[str] = []
        with self._sandbox(), mock.patch.object(StagedExecutor, "_pipeline_for",
                                                return_value=()):
            out = StagedExecutor().execute(msg, _agent(), progress=lines.append)
        return out, lines

    def _run_dir(self) -> Path:
        dirs = list(self.runs.iterdir())
        self.assertEqual(len(dirs), 1, f"expected exactly one run dir, got {dirs}")
        return dirs[0]

    def test_related_md_lands_in_the_run_dir_with_slug_title_and_source(self):
        with mock.patch.object(rc, "related_items", return_value=[_item()]):
            out, lines = self._run()

        self.assertTrue(out.ok, out.summary)
        text = (self._run_dir() / rc.RELATED_FILENAME).read_text(encoding="utf-8")
        self.assertIn(f"`{RELATED_SLUG}`", text)
        self.assertIn(RELATED_TITLE, text)
        self.assertIn(RELATED_SOURCE, text)
        self.assertIn(f"slug: {SLUG}", text)
        self.assertIn("mode: bfs-only", text)
        self.assertIn("related: 1 record(s) → related.md (bfs-only)", lines)

    def test_the_query_receives_the_payloads_summary_as_the_title(self):
        """`summary`, not `title` — see the module docstring."""
        seen: list[dict] = []

        def _capture(task, **_kw):
            seen.append(dict(task))
            return [_item()]

        with mock.patch.object(rc, "related_items", _capture):
            self._run(touches=["todo_system/x.py"], outcome="무엇이 끝나면 닫히는가")

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["slug"], SLUG)
        self.assertEqual(seen[0]["title"], SUMMARY)
        self.assertEqual(seen[0]["touches"], ["todo_system/x.py"])
        self.assertEqual(seen[0]["outcome"], "무엇이 끝나면 닫히는가")

    def test_a_raising_query_costs_the_file_and_nothing_else(self):
        """Fail-open: identical outcome to the patched-nothing run, no file, no raise."""
        with mock.patch.object(rc, "related_items", return_value=[_item()]):
            baseline, _ = self._run()

        def _boom(*_a, **_kw):
            raise RuntimeError("query exploded")

        shutil.rmtree(self.runs)
        self.runs.mkdir()
        with mock.patch.object(rc, "related_items", _boom):
            out, _lines = self._run()

        self.assertEqual(out, baseline)
        self.assertFalse((self._run_dir() / rc.RELATED_FILENAME).exists())

    def test_a_raising_writer_also_costs_only_the_file(self):
        """Second belt: the failure is in write_related itself, past the query."""
        def _boom(*_a, **_kw):
            raise OSError("disk went away")

        with mock.patch.object(rc, "write_related", _boom):
            out, lines = self._run()

        self.assertTrue(out.ok, out.summary)
        self.assertFalse((self._run_dir() / rc.RELATED_FILENAME).exists())
        self.assertFalse([ln for ln in lines if ln.startswith("related:")])

    def test_zero_related_records_write_no_file(self):
        with mock.patch.object(rc, "related_items", return_value=[]):
            out, lines = self._run()

        self.assertTrue(out.ok, out.summary)
        self.assertFalse((self._run_dir() / rc.RELATED_FILENAME).exists())
        self.assertFalse([ln for ln in lines if ln.startswith("related:")])

    def test_the_kill_switch_leaves_no_file(self):
        with mock.patch.dict("os.environ", {rc.KILL_ENV: "0"}):
            out, _lines = self._run()

        self.assertTrue(out.ok, out.summary)
        self.assertFalse((self._run_dir() / rc.RELATED_FILENAME).exists())


class RelatedIsAnInputNotAnArtifactTest(unittest.TestCase):
    """related.md must stay out of every required/inherited artifact list: the
    research stage's required-artifact check would then fail whenever the engine
    legitimately wrote no file, which is exactly what AC#1 forbids."""

    def test_related_md_is_not_a_stage_artifact(self):
        self.assertNotIn(rc.RELATED_FILENAME, set(stages.STAGE_ARTIFACT.values()))
        self.assertNotIn(rc.RELATED_FILENAME, stages.APPENDED_ARTIFACTS)
        self.assertEqual(stages.STAGE_ARTIFACT["research"], "requirements.md")

    def test_record_related_returns_none_rather_than_raising(self):
        with mock.patch.object(rc, "write_related", side_effect=RuntimeError("boom")):
            self.assertIsNone(stages.record_related(Path("/nonexistent"), {"slug": SLUG}))


if __name__ == "__main__":
    unittest.main()
