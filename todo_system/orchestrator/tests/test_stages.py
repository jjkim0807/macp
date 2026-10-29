"""Tests for the staged pipeline port (convergence)."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from todo_system import paths
from todo_system.orchestrator import disk_guard
from todo_system.orchestrator import model_select as ms
from todo_system.orchestrator import stages
from todo_system.orchestrator import worktree_sweep
from todo_system.orchestrator.runner import StagedExecutor, make_executor


def _wait(pred, timeout=15.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def _pid_gone(pid: int) -> bool:
    """True when `pid` is no longer a live process.

    `os.kill(pid, 0)` on its own is not enough: under a subreaper (containers,
    some init setups) a killed grandchild can linger as a zombie whose pid still
    answers signal 0 — the trap `jobs.py` records for its own liveness probe. A
    `/proc` state of `Z` counts as gone; where there is no `/proc` (macOS) the
    ProcessLookupError arm is the whole answer."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False            # alive but not ours to signal
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    fields = stat.rsplit(")", 1)[-1].split()    # comm may contain spaces/parens
    return bool(fields) and fields[0] == "Z"


class ConstantsTest(unittest.TestCase):
    def test_pipeline_shape(self):
        self.assertEqual(stages.STAGES_AUTO,
                         ("research", "plan", "implement", "verify", "report"))
        # every stage has tools, a turn budget, and an artifact
        for s in stages.STAGES_AUTO:
            self.assertIn(s, stages.STAGE_TOOLS)
            self.assertIn(s, stages.STAGE_MAX_TURNS)
            self.assertIn(s, stages.STAGE_ARTIFACT)

    def test_no_forbidden_tools(self):
        for s in stages.STAGES_AUTO:
            stages._safe_tools(s)  # raises if a forbidden tool sneaks in

    def test_forbidden_list_matches_the_worker_copy(self):
        """The list is duplicated and the copies had drifted: stages/ was missing
        `Bash(* --no-verify` while claiming in a comment to mirror worker. A
        security list that exists twice needs something holding it together."""
        from todo_system import worker
        self.assertEqual(set(stages.FORBIDDEN_TOOL_SUBSTRINGS),
                         set(worker.FORBIDDEN_TOOL_SUBSTRINGS))

    def test_forbidden_patterns_catch_the_colon_form_actually_written(self):
        """Until 2026-08-05 curl/ssh carried a trailing space, so they matched
        `Bash(curl -X …)` — which nobody writes — and missed `Bash(curl:*)`,
        which eleven tasks granted and which validated clean."""
        for probe in ("Bash(curl:*)", "Bash(ssh:*)", "Bash(rm:*)",
                      "Bash(sudo:*)", "Bash(scp:*)"):
            self.assertTrue(
                any(bad in probe for bad in stages.FORBIDDEN_TOOL_SUBSTRINGS),
                f"{probe} slips through the guard")

    def test_forbidden_patterns_do_not_catch_ordinary_grants(self):
        for ok in ("Bash(git:*)", "Bash(gh:*)", "Bash(uv:*)", "Read", "Write",
                   "Bash(./gradlew:*)", "Bash(pytest:*)", "Bash(flutter:*)",
                   "Bash(dart:*)"):
            self.assertFalse(
                any(bad in ok for bad in stages.FORBIDDEN_TOOL_SUBSTRINGS),
                f"{ok} wrongly blocked")

    def test_verify_and_quick_carry_the_fallback_runners(self):
        """Step 5b tells verify to reproduce the repo's own CI commands when CI
        cannot run, so a missing runner silently closes that path: moksori-2
        (2026-08-23) parked at FAIL(unverified) because bare `flutter analyze`
        answered "This command requires approval" twice, and a human running the
        same three gates on the same sha found them all green.

        verify and quick are asserted together on purpose — the runner set used
        to be two hand-maintained copies, so an addition could land in one and
        be forgotten in the other."""
        for stage in ("verify", "quick"):
            granted = stages.STAGE_TOOLS[stage].split()
            for tool in ("Bash(flutter:*)", "Bash(dart:*)", "Bash(gradlew:*)",
                         "Bash(./gradlew:*)", "Bash(pnpm:*)", "Bash(npx:*)",
                         "Bash(uv:*)", "Bash(pytest:*)"):
                self.assertIn(tool, granted, f"{stage} cannot run {tool}")

    def test_verify_stays_read_only(self):
        """verify being test-only is what lets the pipeline trust its verdict.
        Asserted for verify ALONE: implement/quick/report/delegate hold Edit
        legitimately, so a sweep over every stage would be red on arrival."""
        self.assertNotIn("Edit", stages.STAGE_TOOLS["verify"].split())

    def test_legacy_verify_carries_every_fallback_runner(self):
        """The legacy worker's verify whitelist is not overridable — worker.py
        reads DEFAULT_VERIFY_TOOLS directly (no frontmatter path), so a runner
        missing there means the Step 5b local fallback simply cannot run on that
        path. Every runner added since 2026-08-18 (pnpm/node/npx) and 2026-08-24
        (flutter/dart) landed in stages.py alone, leaving the legacy verify ten
        runners short. Asserted against FALLBACK_RUNNER_TOOLS rather than a
        literal list so a future addition is covered the moment it is written."""
        from todo_system import worker
        granted = worker.DEFAULT_VERIFY_TOOLS.split()
        for tool in stages.FALLBACK_RUNNER_TOOLS.split():
            self.assertIn(tool, granted, f"legacy verify cannot run {tool}")

    def test_legacy_stage_tools_match_the_stage_source(self):
        """stages.STAGE_TOOLS is the single source; worker.DEFAULT_*_TOOLS alias
        it. Pinned so re-pasting a literal into worker.py is red on arrival —
        that copy-paste is exactly how the two lists drifted apart.

        'quick' is excluded deliberately: the legacy pipeline has no such stage
        (worker.STAGES_BY_MODE covers auto + delegate only), so requiring a
        DEFAULT_QUICK_TOOLS would invent a constant nothing consumes."""
        from todo_system import worker
        for stage in ("research", "plan", "implement",
                      "verify", "report", "delegate"):
            self.assertEqual(
                getattr(worker, f"DEFAULT_{stage.upper()}_TOOLS"),
                stages.STAGE_TOOLS[stage],
                f"legacy {stage} whitelist drifted from stages.STAGE_TOOLS")

    def test_legacy_verify_stays_read_only(self):
        """The read-only invariant has to hold on the legacy path too, since
        that is the one whose whitelist a task cannot override."""
        from todo_system import worker
        self.assertNotIn("Edit", worker.DEFAULT_VERIFY_TOOLS.split())


class RunnerVaultDataRootSplitTest(unittest.TestCase):
    """runner.py's 11 consumers of the stages.REPO_ROOT compat alias were moved to
    the explicit stages._VAULT_DATA_ROOT constant (macp-vault-root-runner-module) —
    every one is a vault-checkout git op or a vault-data read. Two invariants pin
    that migration.

    Value comparisons only: no git, no importlib.reload, so nothing here touches
    the live vault or perturbs this process's RUNS_DIR. The runner's git behaviour
    against the resolved root is exercised on tmp repos by
    LiveSiblingCheckoutGuardTest (test_runner) and the Vault* classes below; this
    pins the CONSTANT the 11 lines read, which is what makes the "byte-identical
    when unset" claim testable at all.
    """

    @unittest.skipUnless(
        (Path(paths.__file__).resolve().parents[1] / ".macp" / "config.toml").is_file(),
        "vault-only: byte-identity to the engine root holds only in the combined vault "
        "layout (.macp/config.toml at the repo root); the engine-only export tree has "
        "no marker and vault_root() raises by design (public-split-10 AC2 / AC4).",
    )
    def test_byte_identical_to_engine_root_when_env_unset(self):
        """AC#2: with MACP_VAULT_ROOT unset (the installed config) every replaced
        line resolves to the exact path the REPO_ROOT alias always returned."""
        anchor = Path(stages.__file__).resolve().parents[2]
        env = os.environ.get("MACP_VAULT_ROOT")    # what stages saw at import
        # The resolver, env cleared, returns the engine anchor — the value
        # _VAULT_DATA_ROOT is computed from at import in the shipped config.
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("MACP_VAULT_ROOT", None)
            self.assertEqual(paths.vault_root(start=anchor), anchor)
        # …and the imported constants agree, so the 11 runner lines are unchanged.
        # A worker stage sets the env before import; the constant then holds it.
        self.assertEqual(stages._VAULT_DATA_ROOT, Path(env).expanduser() if env else anchor)
        self.assertEqual(stages._ENGINE_ROOT, anchor)

    def test_the_split_is_not_a_rename_redirect_moves_only_vault_data(self):
        """The migration would be pointless if the two roots could never differ:
        _VAULT_DATA_ROOT follows MACP_VAULT_ROOT, _ENGINE_ROOT stays pinned. Drive
        the resolver directly (value only) rather than reloading stages, so the
        live constants / RUNS_DIR are untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"MACP_VAULT_ROOT": tmp}):
                redirected = paths.vault_root(start=stages._ENGINE_ROOT)
            self.assertEqual(redirected, Path(tmp))
            self.assertNotEqual(redirected, stages._ENGINE_ROOT)
        # The engine root never moved — the env only steers the vault-data root.
        self.assertEqual(stages._ENGINE_ROOT,
                         Path(stages.__file__).resolve().parents[2])


class StageTimeoutForTest(unittest.TestCase):
    """stage_timeout_for derives the per-stage subprocess cap from the task tier,
    with an explicit `stage_timeout_min` frontmatter override on top."""

    def test_each_tier_maps_to_its_timeout(self):
        # 2026-10-18 ladder (user decision "finish, even if slow"): 3h / 2h / 1h.
        self.assertEqual(stages.stage_timeout_for({"model_tier": "heavy"}), 10800)
        self.assertEqual(stages.stage_timeout_for({"model_tier": "standard"}), 7200)
        self.assertEqual(stages.stage_timeout_for({"model_tier": "light"}), 3600)

    def test_untiered_payload_falls_back_to_default_tier(self):
        # classify_tier's DEFAULT_TIER is `standard`, so an empty payload sizes to
        # the standard cap rather than the (larger) heavy one.
        self.assertEqual(stages.stage_timeout_for({}),
                         stages.STAGE_TIMEOUTS[ms.DEFAULT_TIER])

    def test_stage_timeout_min_overrides_the_tier(self):
        self.assertEqual(stages.stage_timeout_for(
            {"model_tier": "light", "stage_timeout_min": 120}), 7200)

    def test_bad_or_nonpositive_stage_timeout_min_is_ignored(self):
        for bad in ("soon", 0, -5, None):
            self.assertEqual(
                stages.stage_timeout_for({"model_tier": "standard",
                                          "stage_timeout_min": bad}), 7200)

    def test_max_stage_timeout_is_the_ceiling_of_the_ladder(self):
        self.assertEqual(stages.MAX_STAGE_TIMEOUT, max(stages.STAGE_TIMEOUTS.values()))
        self.assertEqual(stages.MAX_STAGE_TIMEOUT, 10800)


class BuildArgvTest(unittest.TestCase):
    def _argv(self, stage, **kw):
        return stages.build_stage_argv(
            stage, task_id="worldgraph-4", run_id="20260720T120000-worldgraph-4",
            run_dir=Path("/vault/Worker/runs/x"), branch="agent/worldgraph-4", **kw)

    def test_implement_stage(self):
        argv = self._argv("implement")
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[1], "-p")
        prompt = argv[2]
        self.assertIn("- slug: `worldgraph-4`", prompt)          # metadata injected
        self.assertIn("- stage: `implement`", prompt)
        self.assertIn("--max-turns", argv)
        self.assertIn("200", argv)                               # implement budget
        self.assertIn("Edit", argv)                              # implement can edit
        self.assertIn("Bash(git:*)", argv)

    def test_model_and_effort(self):
        from todo_system.orchestrator import model_select
        argv = self._argv("implement")
        self.assertIn("--model", argv)
        self.assertIn(stages.stage_model(), argv)
        # whatever resolve_model() picked, it must come from the declared chain —
        # derived, not hard-coded, so extending FALLBACKS doesn't break this
        self.assertIn(stages.stage_model(),
                      (model_select.PREFERRED, *model_select.FALLBACKS))
        self.assertIn("--effort", argv)
        self.assertIn("max", argv)                   # "ultra" coding

    def test_model_is_resolved_per_call_not_frozen_at_import(self):
        """Regression (2026-07-31): STAGE_MODEL was an import-time constant, so a
        long-lived `runner --daemon` kept using whatever model was resolved at
        process start — it held an out-of-credits model for hours after the
        fallback had been fixed, and only a manual restart picked the change up.
        """
        from unittest import mock
        from todo_system.orchestrator import model_select
        with mock.patch.object(model_select, "resolve_model", return_value="model-a"):
            self.assertIn("model-a", self._argv("implement"))
        # same process, no re-import: a changed resolution must be picked up
        with mock.patch.object(model_select, "resolve_model", return_value="model-b"):
            self.assertIn("model-b", self._argv("implement"))

    def test_tier_picks_the_ladder_and_the_effort(self):
        """A light task must not spend a Fable pipeline on a one-liner: the
        argv carries the light ladder's model and the light effort."""
        from unittest import mock
        from todo_system.orchestrator import model_select
        seen: list[tuple] = []

        def _resolve(*, use_cache=True, tier="heavy"):
            seen.append(tier)
            return f"model-for-{tier}"

        with mock.patch.object(model_select, "resolve_model", side_effect=_resolve):
            argv = self._argv("implement", tier="light")
            self.assertIn("model-for-light", argv)
            self.assertIn(model_select.TIER_EFFORT["light"], argv)
            self.assertNotIn("max", argv)
            argv = self._argv("implement", tier="heavy")
            self.assertIn("model-for-heavy", argv)
            self.assertIn("max", argv)
            argv = self._argv("implement")            # no tier = pre-tier behaviour
            self.assertIn("model-for-heavy", argv)
        self.assertEqual(seen, ["light", "heavy", "heavy"])

    def test_report_stage_runs_one_notch_below_the_task_tier(self):
        """The stage-type dimension: report is push + gh pr + report.md, so it
        drops a rung; the stages that produce or judge the work do not."""
        self.assertEqual(stages.effective_tier("report", "heavy"), "standard")
        self.assertEqual(stages.effective_tier("report", "standard"), "light")
        self.assertEqual(stages.effective_tier("report", "light"), "light")
        for st in ("research", "plan", "implement", "verify", "quick", "delegate"):
            self.assertEqual(stages.effective_tier(st, "heavy"), "heavy", st)
            self.assertEqual(stages.effective_tier(st, "light"), "light", st)
        from unittest import mock
        from todo_system.orchestrator import model_select
        with mock.patch.object(model_select, "resolve_model",
                               side_effect=lambda *, use_cache=True, tier="heavy": f"m-{tier}"):
            self.assertIn("m-standard", self._argv("report", tier="heavy"))
            self.assertIn("m-heavy", self._argv("verify", tier="heavy"))

    def test_record_model_tier_lands_in_stages_json_without_clobbering(self):
        import json
        import tempfile
        run_dir = Path(tempfile.mkdtemp())
        stages.record_stage(run_dir, "research")
        stages.record_model_tier(run_dir, "light", "worker_mode: quick")
        stages.record_stage(run_dir, "plan")
        data = json.loads((run_dir / stages.STAGE_PROGRESS).read_text())
        self.assertEqual(data["model_tier"], "light")
        self.assertEqual(data["model_tier_reason"], "worker_mode: quick")
        self.assertEqual(data["completed"], ["research", "plan"])

    def test_effort_falls_back_when_tier_module_breaks(self):
        from unittest import mock
        with mock.patch.object(stages, "effective_tier", side_effect=RuntimeError("boom")):
            self.assertEqual(stages.stage_effort("implement", "light"), stages.STAGE_EFFORT)
            self.assertEqual(stages.stage_model("implement", "light"), stages.STAGE_FALLBACK_MODEL)

    def test_model_falls_back_when_resolution_raises(self):
        from unittest import mock
        from todo_system.orchestrator import model_select
        with mock.patch.object(model_select, "resolve_model",
                               side_effect=RuntimeError("boom")):
            self.assertIn(stages.STAGE_FALLBACK_MODEL, self._argv("implement"))

    def test_verify_stage_tools(self):
        argv = self._argv("verify")
        self.assertIn("Bash(gh:*)", argv)                        # CI checks via gh
        self.assertNotIn("Edit", argv)                           # verify must not edit
        self.assertIn("50", argv)

    def test_external_repo_metadata(self):
        prompt = self._argv("implement", repo="https://h/x/foo.git",
                            working_dir="sub")[2]
        self.assertIn("- repo: `https://h/x/foo.git`", prompt)
        self.assertIn("External repo task", prompt)

    def test_external_repo_metadata_says_how_to_reach_the_wrapper(self):
        """Implement ran zero gradle commands on edge-to-edge-insets-pairent
        (2026-09-25): cwd is the vault root, `./gradlew` is the whitelisted
        form, and nothing told it a standalone `cd` gets it there."""
        prompt = self._argv("implement", repo="https://h/x/foo.git",
                            working_dir="/w/foo")[2]
        self.assertIn("`cd /w/foo` as its own Bash call", prompt)
        self.assertIn("`./gradlew", prompt)
        without_wd = self._argv("implement", repo="https://h/x/foo.git")[2]
        self.assertNotIn("as its own Bash call", without_wd)

    def test_prompt_states_the_real_tool_list(self):
        """The prose runner list inside worker-verify.md drifts from the code —
        it named neither pnpm (granted 2026-08-18) nor flutter (2026-08-24). An
        agent that believes the prose stops probing instead of running the gate,
        so the decided whitelist is injected verbatim after the prompt."""
        prompt = self._argv("verify")[2]
        self.assertIn("- allowed tools (authoritative", prompt)
        for tool in ("Bash(flutter:*)", "Bash(dart:*)", "Bash(pnpm:*)"):
            self.assertIn(tool, prompt)

    def test_injected_tool_list_reflects_a_task_override(self):
        """implement honours the task's own list, so the injected line has to
        show that list — not the stage default it replaced."""
        prompt = self._argv("implement", allowed_tools="Read,Bash(gcloud:*)")[2]
        self.assertIn("Bash(gcloud:*)", prompt)
        self.assertNotIn("Bash(pytest:*)", prompt)


class VerifyVerdictTest(unittest.TestCase):
    def test_pass_fail_skip(self):
        self.assertEqual(stages.parse_verify_verdict("---\noverall_status: pass\n---"), "PASS")
        self.assertEqual(stages.parse_verify_verdict("overall_status: fail"), "FAIL")
        self.assertEqual(stages.parse_verify_verdict("overall_status: skip"), "SKIP")

    def test_none_when_absent(self):
        self.assertIsNone(stages.parse_verify_verdict("## CI Result\nno verdict here"))

    def test_parse_report_merged(self):
        self.assertIs(stages.parse_report_merged("merged: yes"), True)
        self.assertIs(stages.parse_report_merged("merged: no"), False)
        self.assertIsNone(stages.parse_report_merged("merged: n/a"))
        self.assertIsNone(stages.parse_report_merged("no merged field here"))

    def test_merge_landed_reads_last_stage_artifact(self):
        d = Path(tempfile.mkdtemp())
        try:
            # a QUICK task records the merge in quick.md, not report.md — reading
            # report.md would wrongly return None and falsely surface for review.
            (d / stages.STAGE_ARTIFACT["quick"]).write_text("merged: yes\n", encoding="utf-8")
            self.assertIs(stages.merge_landed(d, "quick"), True)
            # a 5-stage task records it in report.md
            (d / stages.STAGE_ARTIFACT["report"]).write_text("merged: no\n", encoding="utf-8")
            self.assertIs(stages.merge_landed(d, "report"), False)
            # missing artifact → unknown (surface for review)
            self.assertIsNone(stages.merge_landed(d, "delegate"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ResumeTest(unittest.TestCase):
    """A run that dies late must not re-buy the stages that already succeeded."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_runs = stages.RUNS_DIR
        stages.RUNS_DIR = self.tmp

    def tearDown(self):
        stages.RUNS_DIR = self._orig_runs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, run_id, done=(), verify=None):
        d = self.tmp / run_id
        d.mkdir(parents=True)
        for s in done:
            (d / stages.STAGE_ARTIFACT[s]).write_text(f"{s} output\n", encoding="utf-8")
            stages.record_stage(d, s)
        if verify is not None:
            (d / stages.STAGE_ARTIFACT["verify"]).write_text(
                f"overall_status: {verify}\n", encoding="utf-8")
        return d

    def test_record_and_read_back(self):
        d = self._run("20260101T000000-t", done=("research", "plan"))
        self.assertEqual(stages.completed_stages(d), ["research", "plan"])

    def test_inherits_creative_prefix_from_crashed_run(self):
        self._run("20260101T000000-t", done=("research", "plan", "implement"))
        got = stages.resumable_run("t", "20260102T000000-t")
        self.assertIsNotNone(got)
        _, resumable = got
        self.assertEqual(resumable, ["research", "plan", "implement"])

    def test_verify_and_report_never_inherited(self):
        # they read live state (CI conclusion, mergeability) — a stale artifact lies
        self._run("20260101T000000-t", done=("research", "verify", "report"))
        _, resumable = stages.resumable_run("t", "20260102T000000-t")
        self.assertEqual(resumable, ["research"])

    def test_real_verify_failure_starts_over(self):
        # FAIL = the code is broken; reusing that implement re-verifies the same red
        self._run("20260101T000000-t", done=("research", "plan", "implement"),
                  verify="fail")
        self.assertIsNone(stages.resumable_run("t", "20260102T000000-t"))

    def test_verify_pass_still_resumable(self):
        # e.g. report crashed — no reason to re-implement
        self._run("20260101T000000-t", done=("research", "plan", "implement"),
                  verify="pass")
        _, resumable = stages.resumable_run("t", "20260102T000000-t")
        self.assertEqual(resumable, ["research", "plan", "implement"])

    def test_no_prior_run_and_unrecorded_run(self):
        self.assertIsNone(stages.resumable_run("t", "20260102T000000-t"))
        (self.tmp / "20260101T000000-t").mkdir()      # ran, recorded nothing
        self.assertIsNone(stages.resumable_run("t", "20260102T000000-t"))

    def test_other_tasks_runs_are_not_borrowed(self):
        self._run("20260101T000000-other", done=("research", "plan"))
        self.assertIsNone(stages.resumable_run("t", "20260102T000000-t"))

    def test_suffix_collision_run_is_not_borrowed(self):
        # 'other' above shares no substring, so it never caught the real failure:
        # a slug that merely ENDS with this one is still a DIFFERENT task, and
        # inheriting its plan sends implement off building the wrong thing.
        self._run("20260101T000000-api-poc", done=("research", "plan", "implement"))
        self.assertIsNone(stages.resumable_run("poc", "20260103T000000-poc"))
        # positive control — matching NOTHING would satisfy the assertion above
        self._run("20260102T000000-poc", done=("research",))
        rd, resumable = stages.resumable_run("poc", "20260103T000000-poc")
        self.assertEqual(rd.name, "20260102T000000-poc")
        self.assertEqual(resumable, ["research"])

    def test_inherit_copies_artifacts_and_skips_empty(self):
        prior = self._run("20260101T000000-t", done=("research", "plan"))
        (prior / stages.STAGE_ARTIFACT["plan"]).write_text("", encoding="utf-8")
        new = self.tmp / "20260102T000000-t"
        new.mkdir()
        got = stages.inherit_artifacts(prior, new, ["research", "plan"])
        self.assertEqual(got, ["research"])           # empty plan is not inherited
        self.assertTrue((new / stages.STAGE_ARTIFACT["research"]).exists())


class ProvenanceStampTest(unittest.TestCase):
    """An inherited artifact must SAY it was inherited, without changing what the
    next stage reads. A run dir stopped meaning 'one execution' when
    inherit_artifacts() started copying a prior run's bytes into it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    FRONTMATTER = ("---\nstage: research\nrun_id: 20260101T000000-t\n---\n"
                   "\n# Requirements\n본문 한 줄\n")

    def test_stamp_frontmatter_preserves_body(self):
        p = self.tmp / "requirements.md"
        p.write_text(self.FRONTMATTER, encoding="utf-8")
        before = stages.strip_frontmatter(self.FRONTMATTER)

        self.assertTrue(stages.stamp_inherited(p, "20251231T235959-t"))

        after = p.read_text(encoding="utf-8")
        self.assertIn("inherited_from: 20251231T235959-t", after)
        # the producing run's own id is a DIFFERENT fact and must survive
        self.assertIn("run_id: 20260101T000000-t", after)
        # the money assertion: the next stage's prompt sees byte-identical body
        self.assertEqual(stages.strip_frontmatter(after), before)
        self.assertEqual(stages.read_provenance(p), "20251231T235959-t")

    def test_stamp_plain_file_adds_no_frontmatter_block(self):
        # run.log has no frontmatter; prepending a `---` block would leak past
        # strip_frontmatter() (it strips only the first) into the next prompt.
        p = self.tmp / "run.log"
        p.write_text("\n=== research (rc=0) ===\nout\n", encoding="utf-8")
        self.assertTrue(stages.stamp_inherited(p, "20251231T235959-t"))
        text = p.read_text(encoding="utf-8")
        self.assertFalse(text.startswith("---"))
        self.assertIn("=== inherited from 20251231T235959-t (above this line) ===", text)
        self.assertEqual(stages.strip_frontmatter(text), text)
        self.assertEqual(stages.read_provenance(p), "20251231T235959-t")

    def test_run_log_marker_stays_true_after_append(self):
        # the runner opens run.log in append mode AFTER inheritance, so the stamp
        # has to stay honest about a file it no longer wholly describes
        p = self.tmp / "run.log"
        p.write_text("\n=== implement (rc=0) ===\ninherited output\n", encoding="utf-8")
        stages.stamp_inherited(p, "20251231T235959-t")
        with p.open("a", encoding="utf-8") as f:
            f.write("\n=== verify (rc=0) ===\nthis run's own output\n")

        text = p.read_text(encoding="utf-8")
        self.assertEqual(stages.read_provenance(p), "20251231T235959-t")
        marker = text.index("=== inherited from 20251231T235959-t (above this line) ===")
        self.assertLess(text.index("inherited output"), marker)   # above = inherited
        self.assertGreater(text.index("this run's own output"), marker)  # below = fresh

    def test_appended_artifact_with_frontmatter_still_gets_boundary(self):
        # run.log usually has no frontmatter, but a stage that wrote it with Write
        # instead of appending leaves one — and the runner appends to it either
        # way, so a whole-file `inherited_from:` header would go stale on the very
        # next stage. Observed on a real run dir (20260817T201522-ai-stock-*).
        p = self.tmp / "run.log"
        p.write_text("---\nstage: implement\nrun_id: 20260101T000000-t\n---\n"
                     "\n# Run Log\ninherited body\n", encoding="utf-8")
        self.assertTrue(stages.stamp_inherited(p, "20251231T235959-t", appended=True))
        text = p.read_text(encoding="utf-8")
        self.assertNotIn("inherited_from:", text)        # not a whole-file claim
        self.assertIn("=== inherited from 20251231T235959-t (above this line) ===", text)
        self.assertEqual(stages.read_provenance(p), "20251231T235959-t")

    def test_run_log_is_the_appended_artifact(self):
        # pins the wiring: whatever the runner appends to must be stamped that way
        self.assertEqual(stages.APPENDED_ARTIFACTS, {"run.log"})
        self.assertEqual(stages.STAGE_ARTIFACT["implement"], "run.log")

    def test_restamp_replaces_not_duplicates(self):
        # 3rd-generation inheritance: only the immediate hop is meaningful, and a
        # second `inherited_from:` key would make the frontmatter ambiguous
        p = self.tmp / "requirements.md"
        p.write_text(self.FRONTMATTER, encoding="utf-8")
        stages.stamp_inherited(p, "20251231T235959-t")
        stages.stamp_inherited(p, "20260102T000000-t")
        text = p.read_text(encoding="utf-8")
        self.assertEqual(text.count("inherited_from:"), 1)
        self.assertEqual(stages.read_provenance(p), "20260102T000000-t")
        self.assertEqual(stages.strip_frontmatter(text),
                         stages.strip_frontmatter(self.FRONTMATTER))

    def test_unstamped_artifact_reads_as_unknown_not_inherited(self):
        p = self.tmp / "requirements.md"
        p.write_text(self.FRONTMATTER, encoding="utf-8")
        self.assertIsNone(stages.read_provenance(p))
        self.assertIsNone(stages.read_provenance(self.tmp / "missing.md"))

    def test_stamp_failure_keeps_inheritance(self):
        # a stamp is an observability nicety; dropping the stage over one would
        # re-buy an expensive stage to gain a comment
        prior, new = self.tmp / "prior", self.tmp / "new"
        prior.mkdir()
        new.mkdir()
        raw = b"\xff\xfe not utf-8 at all\n"
        (prior / stages.STAGE_ARTIFACT["research"]).write_bytes(raw)

        got = stages.inherit_artifacts(prior, new, ["research"])

        self.assertEqual(got, ["research"])           # still inherited
        dst = new / stages.STAGE_ARTIFACT["research"]
        self.assertEqual(dst.read_bytes(), raw)       # bytes untouched
        self.assertIsNone(stages.read_provenance(dst))  # unknown, not a crash

    def test_inherit_artifacts_stamps_every_copy(self):
        prior, new = self.tmp / "20260101T000000-t", self.tmp / "20260102T000000-t"
        prior.mkdir()
        new.mkdir()
        (prior / stages.STAGE_ARTIFACT["research"]).write_text(
            self.FRONTMATTER, encoding="utf-8")
        # deliberately frontmatter'd, the shape that would tempt a header stamp
        (prior / stages.STAGE_ARTIFACT["implement"]).write_text(
            "---\nstage: implement\nrun_id: 20260101T000000-t\n---\n\nout\n",
            encoding="utf-8")

        got = stages.inherit_artifacts(prior, new, ["research", "implement"])

        self.assertEqual(got, ["research", "implement"])
        for s in got:
            self.assertEqual(
                stages.read_provenance(new / stages.STAGE_ARTIFACT[s]),
                "20260101T000000-t", f"{s} artifact lost its provenance")
        # …and run.log got the append-safe marker, requirements.md the header key
        self.assertIn("(above this line)",
                      (new / stages.STAGE_ARTIFACT["implement"]).read_text(encoding="utf-8"))
        self.assertIn("inherited_from:",
                      (new / stages.STAGE_ARTIFACT["research"]).read_text(encoding="utf-8"))


class ResumeInExecuteTest(unittest.TestCase):
    """End-to-end on the runner: the inherited stages must not spawn a process.

    The unit tests above only prove the bookkeeping; this proves the money part —
    that `claude -p` is never invoked for a stage we already paid for.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_runs = stages.RUNS_DIR
        stages.RUNS_DIR = self.tmp

    def tearDown(self):
        stages.RUNS_DIR = self._orig_runs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _execute(self, prior_done=(), remote_branch=True):
        from todo_system.orchestrator import message as m
        from todo_system.orchestrator import repos
        if prior_done:
            d = self.tmp / "20260101T000000-t"
            d.mkdir(parents=True)
            for s in prior_done:
                (d / stages.STAGE_ARTIFACT[s]).write_text(f"{s}\n", encoding="utf-8")
                stages.record_stage(d, s)
        spawned = []
        real_argv = stages.build_stage_argv

        def spy_argv(stage, **kw):
            """The stage name is only knowable here — the prompt is inlined into
            argv, so sniffing the command line for it is guesswork."""
            spawned.append(stage)
            spy_argv.pending = (kw["run_dir"], stage)
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            """Stand in for `claude -p`: write the artifact the stage owes."""
            rd, stage = spy_argv.pending
            (Path(rd) / stages.STAGE_ARTIFACT[stage]).write_text(
                "overall_status: pass\nmerged: yes\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload={"repo": "https://h/o/r.git", "worker_mode": "auto"})
        ex = StagedExecutor()
        with mock.patch.object(repos, "ensure_worktree", return_value=Path("/tmp/r")), \
             mock.patch.object(stages, "restore_main") as restore, \
             mock.patch.object(stages, "install_push_guard"), \
             mock.patch.object(stages, "prepare_external_branch",
                               return_value=subprocess.CompletedProcess([], 0, "", "")), \
             mock.patch.object(stages, "remote_branch_exists", return_value=remote_branch), \
             mock.patch.object(stages, "merge_landed", return_value=True), \
             mock.patch.object(stages, "remove_worktree") as remove_wt, \
             mock.patch.object(worktree_sweep, "remove_registry") as remove_reg, \
             mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            out = ex.execute(msg, mock.Mock(), lambda *_: None)
        self.assertTrue(out.ok, out.summary)
        self.restore_calls = [tuple(str(x) for x in c.args) for c in restore.call_args_list]
        self.remove_wt_calls = [tuple(str(x) for x in c.args) for c in remove_wt.call_args_list]
        self.remove_reg_calls = [c.args for c in remove_reg.call_args_list]
        return spawned

    def test_fresh_task_runs_every_stage(self):
        self.assertEqual(self._execute(), list(stages.STAGES_AUTO))

    def test_retry_skips_the_stages_already_bought(self):
        spawned = self._execute(prior_done=("research", "plan", "implement"))
        # exactly the dancemirror case: only verify+report are re-run
        self.assertEqual(spawned, ["verify", "report"])

    def test_inherited_stages_get_no_usage_row(self):
        # usage is per invocation: a stage carried over from a prior run spawned
        # nothing in this one, so this run has no tokens to report for it
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "cfg")}):
            self._execute(prior_done=("research", "plan", "implement"))
        new = [d for d in self.tmp.iterdir() if d.is_dir() and d.name.endswith("-t")
               and d.name != "20260101T000000-t"]
        self.assertEqual(len(new), 1, new)
        prog = json.loads((new[0] / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
        self.assertEqual(set(prog["usage"]), {"verify", "report"})

    def test_poll_only_bypass_leaves_ordinary_messages_queued(self):
        """Both the orchestrator and the runner enforce the guard. A bypass that
        cleared only dispatch would leave the message in the mailbox forever —
        dispatched on paper, never run."""
        from todo_system.orchestrator import claims
        from todo_system.orchestrator import message as m
        from todo_system.orchestrator.runner import Runner
        seen, acked = [], []
        mb = mock.Mock()
        urgent, ordinary = (
            m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="urgent",
                       payload={"bypass_guard": True}),
            m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="ordinary", payload={}),
        )
        mb.receive.return_value = [urgent, ordinary]
        mb.ack.side_effect = lambda a, msg: acked.append(msg.task_id)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        r = Runner.__new__(Runner)
        r.mailbox, r.agent = mb, mock.Mock(id="w")
        r.claims = claims.ClaimStore(tmp, "w")   # __new__ skips __init__
        r._lock, r._inflight, r._busy = threading.RLock(), {}, 0
        r.handle = lambda msg: seen.append(msg.task_id)

        self.assertEqual(r.poll_once(only_bypass=True), 1)
        self.assertEqual(seen, ["urgent"])
        self.assertEqual(acked, ["urgent"])      # the ordinary one stays un-acked
        # …and it was never claimed either: a message filtered out before handling
        # has not started, so nothing about it is recorded and its at-least-once
        # redelivery is preserved.
        self.assertIsNone(r.claims.read(ordinary.msg_id))
        self.assertIsNone(r.claims.read(urgent.msg_id))   # closed after its ack

    def test_successful_external_run_removes_its_worktree(self):
        # Switchover: the external repo is now a per-run worktree, not a shared
        # clone parked on the task branch. A SUCCESSFUL run (a) still restores the
        # VAULT to main, always, and (b) removes its worktree — it no longer
        # restores any clone to a default branch (that call is gone).
        from todo_system.orchestrator import repos
        self._execute()
        # (a) the vault is still walked back to main in the finally
        self.assertTrue(any(c[0] == str(stages._VAULT_DATA_ROOT) for c in self.restore_calls))
        # (b) the worktree is reaped exactly once, off the shared clone, by path
        self.assertEqual(len(self.remove_wt_calls), 1, self.remove_wt_calls)
        base, wt = self.remove_wt_calls[0]
        self.assertEqual(base, str(repos.local_path("https://h/o/r.git")))
        self.assertEqual(wt, "/tmp/r")
        # (c) and its gradle-registry dir (basename of the worktree) goes with it
        self.assertEqual(self.remove_reg_calls, [("r",)])
        # and NOTHING restores the external repo to a branch anymore
        self.assertFalse(any(c[0] == "/tmp/r" for c in self.restore_calls))

    def test_no_long_job_field_never_touches_the_job_machinery(self):
        # the gate must be free for the overwhelming majority of tasks
        from todo_system.orchestrator import jobs
        with mock.patch.object(jobs, "poll") as poll:
            self._execute()
        poll.assert_not_called()

    def test_no_long_job_field_never_touches_the_artifact_preservation(self):
        # …and neither must the snapshot/restore hung off the gate's done path
        with mock.patch.object(stages, "snapshot_job_artifacts") as snap, \
             mock.patch.object(stages, "restore_job_artifacts") as restore:
            self._execute()
        snap.assert_not_called()
        restore.assert_not_called()

    def test_unpushed_branch_still_reimplements(self):
        # no remote branch = the code was never saved; skipping implement would
        # hand verify an empty diff
        spawned = self._execute(prior_done=("research", "plan", "implement"),
                                remote_branch=False)
        self.assertEqual(spawned, ["implement", "verify", "report"])


class BlockedOnTest(unittest.TestCase):
    """A FAIL that names an external blocker is not a defect to fix-forward."""

    def test_parse_blocked_on(self):
        self.assertEqual(
            stages.parse_blocked_on("overall_status: fail\nblocked_on: upstream-task\n"),
            "upstream-task")
        self.assertEqual(
            stages.parse_blocked_on('blocked_on: "upstream (PR #22, OPEN)"'),
            "upstream (PR #22, OPEN)")

    def test_absent_or_empty_is_none(self):
        # a plain FAIL must stay a plain FAIL — the retry path is right for defects
        self.assertIsNone(stages.parse_blocked_on("overall_status: fail\n"))
        self.assertIsNone(stages.parse_blocked_on("blocked_on:   \n"))

    def test_only_matches_at_line_start(self):
        # prose mentioning the word must not fake a blocker
        self.assertIsNone(stages.parse_blocked_on("the run was blocked_on: nothing real"))


class ReviewHoldTest(unittest.TestCase):
    """A deliberate 'I chose not to merge' must not read as 'the merge broke'."""

    def test_parse_report_worker_mode(self):
        self.assertEqual(stages.parse_report_worker_mode("worker_mode_recommendation: review"),
                         "review")
        self.assertEqual(stages.parse_report_worker_mode("worker_mode_recommendation: auto"),
                         "auto")
        self.assertIsNone(stages.parse_report_worker_mode("no such field"))

    def test_review_recommendation_is_a_hold(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / stages.STAGE_ARTIFACT["report"]).write_text(
                "---\nmerged: no\nworker_mode_recommendation: review\n---\n", encoding="utf-8")
            self.assertTrue(stages.review_hold(d, "report"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_auto_recommendation_is_a_real_failure(self):
        d = Path(tempfile.mkdtemp())
        try:
            # merge was attempted and did not land — NOT excused as intentional
            (d / stages.STAGE_ARTIFACT["report"]).write_text(
                "---\nmerged: no\nworker_mode_recommendation: auto\n---\n", encoding="utf-8")
            self.assertFalse(stages.review_hold(d, "report"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unstated_and_missing_artifact_are_real_failures(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / stages.STAGE_ARTIFACT["report"]).write_text("merged: no\n", encoding="utf-8")
            self.assertFalse(stages.review_hold(d, "report"))
            self.assertFalse(stages.review_hold(d, "delegate"))   # no artifact
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RebaseConflictParkTest(unittest.TestCase):
    """A later concurrent same-repo PR that could not rebase cleanly parks on a
    human — detected by the `rebase_conflict: true` marker, same artifact-read
    shape as review_hold()."""

    def test_parse_report_rebase_conflict(self):
        self.assertTrue(stages.parse_report_rebase_conflict("rebase_conflict: true"))
        self.assertTrue(stages.parse_report_rebase_conflict("rebase_conflict: yes"))
        self.assertFalse(stages.parse_report_rebase_conflict("rebase_conflict: false"))
        self.assertFalse(stages.parse_report_rebase_conflict("rebase_conflict: no"))
        self.assertFalse(stages.parse_report_rebase_conflict("no such field"))

    def test_marker_true_is_a_conflict_park(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / stages.STAGE_ARTIFACT["report"]).write_text(
                "---\nmerged: no\nrebase_conflict: true\n---\n", encoding="utf-8")
            self.assertTrue(stages.rebase_conflict_park(d, "report"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_no_marker_and_missing_artifact_are_not_conflict_parks(self):
        d = Path(tempfile.mkdtemp())
        try:
            # a plain merge failure (no marker) is NOT a rebase-conflict park
            (d / stages.STAGE_ARTIFACT["report"]).write_text(
                "---\nmerged: no\nworker_mode_recommendation: auto\n---\n", encoding="utf-8")
            self.assertFalse(stages.rebase_conflict_park(d, "report"))
            self.assertFalse(stages.rebase_conflict_park(d, "delegate"))   # no artifact
        finally:
            shutil.rmtree(d, ignore_errors=True)


class StagedExecutorTest(unittest.TestCase):
    def test_make_executor(self):
        ex = make_executor("live-staged")
        self.assertIsInstance(ex, StagedExecutor)
        self.assertEqual(ex.pipeline, stages.STAGES_AUTO)
        # policy A: auto-merge on CI green so close-on-done can resolve the task
        self.assertTrue(ex.auto_merge)

    def test_delegate_task_uses_single_stage(self):
        ex = StagedExecutor()
        self.assertEqual(ex._pipeline_for({"worker_mode": "delegate"}),
                         stages.STAGES_DELEGATE)
        self.assertEqual(ex._pipeline_for({"worker_mode": "auto"}), stages.STAGES_AUTO)
        self.assertEqual(ex._pipeline_for({}), stages.STAGES_AUTO)

    def test_auto_merge_injected_in_metadata(self):
        argv = stages.build_stage_argv(
            "report", task_id="t", run_id="r", run_dir=Path("/x"),
            branch="b", auto_merge=False)
        self.assertIn("- auto_merge: `false`", argv[2])

    # --- auto_merge:false must be honored as a review gate (finance guard) ---
    # Regression: the runner used the class default (True) and ignored the task's
    # frontmatter auto_merge, so every auto_merge:false task got a misleading
    # "auto_merge did NOT land" park instead of a clean review-required one.
    def test_merge_decision_default_absent_auto_merges(self):
        review, am = StagedExecutor._merge_decision({"worker_mode": "auto"})
        self.assertFalse(review)
        self.assertTrue(am)

    def test_merge_decision_false_bool_parks_for_review(self):
        review, am = StagedExecutor._merge_decision(
            {"worker_mode": "auto", "auto_merge": False})
        self.assertTrue(review)
        self.assertFalse(am)

    def test_merge_decision_false_string_parks_for_review(self):
        review, am = StagedExecutor._merge_decision(
            {"worker_mode": "auto", "auto_merge": "false"})
        self.assertTrue(review)
        self.assertFalse(am)

    def test_merge_decision_true_auto_merges(self):
        review, am = StagedExecutor._merge_decision(
            {"worker_mode": "auto", "auto_merge": True})
        self.assertFalse(review)
        self.assertTrue(am)

    def test_merge_decision_review_mode_parks_even_if_auto_merge_true(self):
        review, am = StagedExecutor._merge_decision(
            {"worker_mode": "review", "auto_merge": True})
        self.assertTrue(review)
        self.assertFalse(am)

    def test_retry_context_injected(self):
        argv = stages.build_stage_argv(
            "implement", task_id="t", run_id="r", run_dir=Path("/x"),
            branch="b", retry_context="CI failed: 5x TS18046 in server.ts")
        prompt = argv[2]
        self.assertIn("Previous attempt FAILED", prompt)
        self.assertIn("TS18046", prompt)
        self.assertIn("FIX-FORWARD", prompt)
        # absent when not a retry
        self.assertNotIn("Previous attempt FAILED",
                         stages.build_stage_argv("implement", task_id="t", run_id="r",
                                                 run_dir=Path("/x"), branch="b")[2])


class CapacityLimitTest(unittest.TestCase):
    def test_detects_real_limit_strings(self):
        for s in ("You're out of usage credits · resets 6pm",
                  "You've hit your weekly limit · resets 8am",
                  "Not logged in · Please run /login",
                  "Error: overloaded_error",
                  # 2026-09-01 CLI rebrand — see mulkko-5-ladder-editor incident
                  "You've reached your Fable 5 limit. Run /usage-credits to "
                  "continue or switch models with /model."):
            self.assertTrue(stages.is_capacity_limit(s), s)

    def test_ignores_normal_output(self):
        for s in ("requirements.md written", "verify overall=fail: TS18046", ""):
            self.assertFalse(stages.is_capacity_limit(s), s)


class UsageShowsNoApiWorkTest(unittest.TestCase):
    """The rc == 0 half of the capacity gate: the refused-call fingerprint.
    Strictly narrower than is_capacity_limit — only a session that ran and logged
    zero API work counts, so a green stage that merely quotes a marker is never
    mis-deferred."""

    def test_refused_call_signature_is_true(self):
        # Exactly what stage_usage records for a rc=0 refused call (no suffix) —
        # the 2026-07-31 quota-cut-at-exit-0 shape.
        self.assertTrue(stages.usage_shows_no_api_work(
            {"unavailable": "no usage in transcript"}))
        # The rc != 0 variant carries a suffix and still matches the prefix.
        self.assertTrue(stages.usage_shows_no_api_work(
            {"unavailable": "no usage in transcript (rc=1)"}))

    def test_real_usage_dict_is_false(self):
        # A stage that did real API work — even one echoing "/usage-credits" in its
        # output — carries token counts and no "unavailable" key.
        self.assertFalse(stages.usage_shows_no_api_work(
            {"num_turns": 3, "input_tokens": 100, "model": "claude-opus-4-8"}))

    def test_none_is_false(self):
        # Kill switch off / stub argv: record_stage_usage returns None, so the rc=0
        # arm is inert (documented limitation) — never a false deferral.
        self.assertFalse(stages.usage_shows_no_api_work(None))

    def test_other_unavailable_reasons_are_false(self):
        # A killed stage and a CLI crash are NOT refused calls — they must not take
        # the no-penalty capacity path.
        for reason in ("timeout",
                       "cli crash (rc=1): transcript not found",
                       "transcript not found",
                       "usage reader error: OSError"):
            self.assertFalse(
                stages.usage_shows_no_api_work({"unavailable": reason}), reason)


class ModelCacheInvalidationTest(unittest.TestCase):
    """The runner's hook. Deliberately narrower than is_capacity_limit()."""

    def test_model_unavailable_output_drops_the_cache(self):
        with mock.patch.object(ms, "invalidate_cache", return_value=True) as inv:
            self.assertTrue(stages.invalidate_model_cache_if_unavailable(
                "You're out of usage credits · resets 6pm"))
        inv.assert_called_once()

    # NB: these use assert_not_called() rather than a raising side_effect. The
    # helper under test swallows every Exception by design, so a sentinel raise
    # would be caught by the very code path being tested and the assertion would
    # pass vacuously.
    def test_capacity_signals_defer_the_run_but_keep_the_cache(self):
        # The whole narrow/broad distinction in one assertion pair: each string
        # still defers the run, and none of them re-picks a model.
        for s in ("Not logged in · Please run /login", "Error: overloaded_error",
                  "You've hit your weekly limit · resets 8am"):
            self.assertTrue(stages.is_capacity_limit(s), s)
            with mock.patch.object(ms, "invalidate_cache") as inv:
                self.assertFalse(stages.invalidate_model_cache_if_unavailable(s), s)
            inv.assert_not_called()

    def test_ordinary_stage_failure_keeps_the_cache(self):
        with mock.patch.object(ms, "invalidate_cache") as inv:
            self.assertFalse(stages.invalidate_model_cache_if_unavailable(
                "2 tests failed\nAssertionError: expected 3, got 4"))
        inv.assert_not_called()

    def test_never_raises_into_the_stage_loop(self):
        # It runs on the hot path in runner._execute; a best-effort side effect
        # must not take down a pipeline that is otherwise fine.
        with mock.patch.object(ms, "invalidate_cache_if_unavailable",
                               side_effect=OSError("read-only filesystem")):
            self.assertFalse(stages.invalidate_model_cache_if_unavailable(
                "You're out of usage credits"))


class ExecuteInvalidatesModelCacheTest(unittest.TestCase):
    """End-to-end on the runner: the unit tests above only prove the helper, this
    proves the *wiring* — that _execute actually calls it, and does so ahead of
    the rc gate. Without this a refactor could delete the call site silently."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_runs = stages.RUNS_DIR
        stages.RUNS_DIR = self.tmp

    def tearDown(self):
        stages.RUNS_DIR = self._orig_runs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _execute(self, stdout="ok", stderr="", returncode=0):
        from todo_system.orchestrator import message as m
        from todo_system.orchestrator import repos
        real_argv = stages.build_stage_argv

        def spy_argv(stage, **kw):
            spy_argv.pending = (kw["run_dir"], stage)
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            rd, stage = spy_argv.pending
            (Path(rd) / stages.STAGE_ARTIFACT[stage]).write_text(
                "overall_status: pass\nmerged: yes\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload={"repo": "https://h/o/r.git", "worker_mode": "auto"})
        with mock.patch.object(repos, "ensure_worktree", return_value=Path("/tmp/r")), \
             mock.patch.object(stages, "restore_main"), \
             mock.patch.object(stages, "install_push_guard"), \
             mock.patch.object(stages, "prepare_external_branch",
                               return_value=subprocess.CompletedProcess([], 0, "", "")), \
             mock.patch.object(stages, "remote_branch_exists", return_value=True), \
             mock.patch.object(stages, "merge_landed", return_value=True), \
             mock.patch.object(stages, "remove_worktree"), \
             mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "invalidate_model_cache_if_unavailable") as inv, \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            StagedExecutor().execute(msg, mock.Mock(), lambda *_: None)
        return inv

    def test_rc_zero_credits_notice_still_invalidates(self):
        # The exact 2026-07-31 shape: the CLI announces an exhausted quota and
        # exits 0, so the run never reaches the rc-gated capacity branch. Gating
        # invalidation on rc would re-blind precisely this case.
        inv = self._execute(stdout="You're out of usage credits · resets 6pm",
                            returncode=0)
        self.assertTrue(inv.called)
        self.assertIn("out of usage credits", inv.call_args_list[0].args[0])

    def test_the_notice_is_seen_on_stderr_too(self):
        # run.log records only stdout, but the hook is handed both streams.
        inv = self._execute(stdout="ok", stderr="usage limit reached")
        self.assertTrue(inv.called)
        self.assertIn("usage limit", inv.call_args_list[0].args[0])

    def test_healthy_stage_output_is_still_offered_to_the_hook(self):
        # The runner does not pre-filter; the helper owns the decision, and the
        # tests above pin that a clean run changes nothing.
        inv = self._execute(stdout="requirements.md written")
        self.assertTrue(inv.called)
        self.assertNotIn("usage credits", inv.call_args_list[0].args[0])


class PreviousFailureContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = stages.RUNS_DIR
        stages.RUNS_DIR = self.tmp

    def tearDown(self):
        stages.RUNS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, name, verify=None):
        d = self.tmp / name
        d.mkdir()
        if verify is not None:
            (d / stages.STAGE_ARTIFACT["verify"]).write_text(verify, encoding="utf-8")
        return d

    def test_returns_prior_verify_and_skips_current(self):
        self._run("20260722T100000-foo", verify="overall=fail: TS error in x.ts")
        cur = "20260722T110000-foo"
        self._run(cur)   # current run, no verify yet
        ctx = stages.previous_failure_context("foo", cur)
        self.assertIn("TS error in x.ts", ctx)
        self.assertIn("20260722T100000-foo", ctx)

    def test_empty_on_first_attempt(self):
        self._run("20260722T110000-bar")   # only the current run exists
        self.assertEqual(stages.previous_failure_context("bar", "20260722T110000-bar"), "")

    def test_ignores_other_tasks(self):
        self._run("20260722T100000-other", verify="unrelated failure")
        self.assertEqual(stages.previous_failure_context("foo", "20260722T110000-foo"), "")

    def test_ignores_a_task_whose_slug_merely_ends_with_this_one(self):
        # this text is pasted into the retry's prompt as the failure to fix, so a
        # suffix match invents a plausible bug report about a different task
        self._run("20260722T100000-api-foo", verify="overall=fail: someone else's red")
        self.assertEqual(stages.previous_failure_context("foo", "20260722T110000-foo"), "")
        # positive control — matching NOTHING would satisfy the assertion above
        self._run("20260722T100500-foo", verify="overall=fail: our own red")
        ctx = stages.previous_failure_context("foo", "20260722T110000-foo")
        self.assertIn("our own red", ctx)
        self.assertNotIn("someone else", ctx)


class PreviousTimeoutContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = stages.RUNS_DIR
        stages.RUNS_DIR = self.tmp

    def tearDown(self):
        stages.RUNS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, name, *, timed_out_stage=None, checkpoint_sha=None):
        d = self.tmp / name
        d.mkdir()
        if timed_out_stage is not None:
            stages.record_timeout(d, timed_out_stage, checkpoint_sha)
        return d

    def test_note_names_the_stage_and_the_checkpoint(self):
        self._run("20260914T100000-foo", timed_out_stage="implement",
                  checkpoint_sha="abc1234def567")
        cur = "20260914T110000-foo"
        self._run(cur)
        ctx = stages.previous_timeout_context("foo", cur)
        self.assertIn("TIMED OUT during `implement`", ctx)
        self.assertIn("abc1234d", ctx)                 # short sha
        self.assertIn("CONTINUE", ctx)

    def test_note_without_a_checkpoint_sha_still_advises_continue(self):
        self._run("20260914T100000-foo", timed_out_stage="implement")   # no sha (vault)
        ctx = stages.previous_timeout_context("foo", "20260914T110000-foo")
        self.assertIn("TIMED OUT during `implement`", ctx)
        self.assertIn("on this branch", ctx)

    def test_empty_when_no_prior_run_timed_out(self):
        self._run("20260914T100000-foo")               # ran, but did not time out
        self.assertEqual(stages.previous_timeout_context("foo", "20260914T110000-foo"), "")

    def test_ignores_a_task_whose_slug_merely_ends_with_this_one(self):
        self._run("20260914T100000-api-foo", timed_out_stage="implement",
                  checkpoint_sha="deadbeef")
        self.assertEqual(stages.previous_timeout_context("foo", "20260914T110000-foo"), "")
        # positive control — matching NOTHING would satisfy the assertion above
        self._run("20260914T100500-foo", timed_out_stage="verify", checkpoint_sha="cafef00d")
        ctx = stages.previous_timeout_context("foo", "20260914T110000-foo")
        self.assertIn("`verify`", ctx)


class PreviousCapacityContextTest(unittest.TestCase):
    """The capacity-deferral mirror of PreviousTimeoutContextTest: a retry whose
    prior run was deferred on a usage/rate limit must be told the partial work is
    already committed (on origin/<branch> when a checkpoint sha was saved) so it
    CONTINUEs rather than re-implementing blind."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = stages.RUNS_DIR
        stages.RUNS_DIR = self.tmp

    def tearDown(self):
        stages.RUNS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, name, *, capacity_stage=None, checkpoint_sha=None):
        d = self.tmp / name
        d.mkdir()
        if capacity_stage is not None:
            stages.record_capacity(d, capacity_stage, checkpoint_sha)
        return d

    def test_note_names_the_stage_and_the_checkpoint(self):
        self._run("20260920T100000-foo", capacity_stage="implement",
                  checkpoint_sha="abc1234def567")
        cur = "20260920T110000-foo"
        self._run(cur)
        ctx = stages.previous_capacity_context("foo", cur)
        self.assertIn("DEFERRED on a usage/rate limit during `implement`", ctx)
        self.assertIn("abc1234d", ctx)                 # short sha
        self.assertIn("CONTINUE", ctx)

    def test_note_without_a_checkpoint_sha_still_advises_continue(self):
        self._run("20260920T100000-foo", capacity_stage="implement")   # no sha (vault)
        ctx = stages.previous_capacity_context("foo", "20260920T110000-foo")
        self.assertIn("DEFERRED on a usage/rate limit during `implement`", ctx)
        self.assertIn("on this branch", ctx)

    def test_empty_when_no_prior_run_was_capacity_limited(self):
        self._run("20260920T100000-foo")               # ran, but was not deferred
        self.assertEqual(stages.previous_capacity_context("foo", "20260920T110000-foo"), "")

    def test_ignores_a_task_whose_slug_merely_ends_with_this_one(self):
        self._run("20260920T100000-api-foo", capacity_stage="implement",
                  checkpoint_sha="deadbeef")
        self.assertEqual(stages.previous_capacity_context("foo", "20260920T110000-foo"), "")
        # positive control — matching NOTHING would satisfy the assertion above
        self._run("20260920T100500-foo", capacity_stage="verify", checkpoint_sha="cafef00d")
        ctx = stages.previous_capacity_context("foo", "20260920T110000-foo")
        self.assertIn("`verify`", ctx)


class RestoreMainTest(unittest.TestCase):
    """The incident fix: the vault must never be left on a task branch."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        for args in (["git", "init", "-b", "main", str(self.repo)],):
            subprocess.run(args, check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(self.repo), "config", k, v],
                           check=True, capture_output=True)
        (self.repo / "f.txt").write_text("base")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "init"],
                       check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _branch(self):
        return subprocess.run(["git", "-C", str(self.repo), "branch", "--show-current"],
                              capture_output=True, text=True).stdout.strip()

    def test_restores_from_task_branch(self):
        stages.git_checkout(self.repo, "agent/some-task", create=True)
        self.assertEqual(self._branch(), "agent/some-task")
        self.assertTrue(stages.restore_main(self.repo))
        self.assertEqual(self._branch(), "main")

    def test_forces_past_uncommitted_changes(self):
        stages.git_checkout(self.repo, "agent/x", create=True)
        (self.repo / "f.txt").write_text("dirty edit on branch")  # would block plain checkout
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "wip"], capture_output=True)
        (self.repo / "f.txt").write_text("uncommitted")
        self.assertTrue(stages.restore_main(self.repo))
        self.assertEqual(self._branch(), "main")


class SnapshotTrackedRuntimeTest(unittest.TestCase):
    """Unit cover for the tracked-runtime snapshot/restore helpers. Dormant in this
    class — it drives the helpers directly, not through restore_main (that wiring is
    RestoreMainPreservesTrackedRuntimeTest)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-b", "main", str(self.repo)],
                       check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(self.repo), "config", k, v],
                           check=True, capture_output=True)
        self._write("Worker/assignments/t.json", '{"state": "dispatched"}')
        self._write("Worker/assignments/clean.json", '{"state": "kept"}')
        self._write("Worker/agents/worker-default/inbox/m.json", '{"msg": 1}')
        self._write("todo_system/outside.py", "x = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "init"],
                       check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_captures_modified_content_and_deletion_within_prefixes(self):
        self._write("Worker/assignments/t.json", '{"state": "in_progress"}')
        (self.repo / "Worker/agents/worker-default/inbox/m.json").unlink()
        snap = stages.snapshot_tracked_runtime(self.repo)
        self.assertEqual(snap["content"].get("Worker/assignments/t.json"),
                         b'{"state": "in_progress"}')
        self.assertIn("Worker/agents/worker-default/inbox/m.json", snap["deleted"])
        # a modification is content (not deleted); a deletion is deleted (not content)
        self.assertNotIn("Worker/agents/worker-default/inbox/m.json", snap["content"])
        self.assertNotIn("Worker/assignments/t.json", snap["deleted"])

    def test_clean_runtime_file_is_not_captured(self):
        # one dirty file so `git status` is non-empty, then assert the clean sibling
        # (which `main` could legitimately have advanced) is never captured/reverted
        self._write("Worker/assignments/t.json", '{"state": "in_progress"}')
        snap = stages.snapshot_tracked_runtime(self.repo)
        self.assertNotIn("Worker/assignments/clean.json", snap["content"])
        self.assertNotIn("Worker/assignments/clean.json", snap["deleted"])

    def test_dirty_file_outside_the_prefixes_is_not_captured(self):
        self._write("todo_system/outside.py", "x = 2\n")            # dirty, out of scope
        self._write("Worker/assignments/t.json", '{"state": "in_progress"}')  # in scope
        snap = stages.snapshot_tracked_runtime(self.repo)
        self.assertIn("Worker/assignments/t.json", snap["content"])
        self.assertNotIn("todo_system/outside.py", snap["content"])
        self.assertNotIn("todo_system/outside.py", snap["deleted"])

    def test_restore_rewrites_content_and_redeletes_idempotently(self):
        assignment = self.repo / "Worker/assignments/t.json"
        envelope = self.repo / "Worker/agents/worker-default/inbox/m.json"
        snap = {"content": {"Worker/assignments/t.json": b'{"state": "restored"}'},
                "deleted": ["Worker/agents/worker-default/inbox/m.json"]}
        # corrupt the worktree the way a forced checkout would: wrong bytes back on
        # the ledger, the acked envelope resurrected
        assignment.write_text('{"state": "rolled-back"}')
        envelope.write_text('{"msg": "resurrected"}')
        stages.restore_tracked_runtime(self.repo, snap)
        self.assertEqual(assignment.read_bytes(), b'{"state": "restored"}')
        self.assertFalse(envelope.exists())
        # idempotent: a second application changes nothing and never raises
        stages.restore_tracked_runtime(self.repo, snap)
        self.assertEqual(assignment.read_bytes(), b'{"state": "restored"}')
        self.assertFalse(envelope.exists())

    def test_restore_recreates_missing_parent_dirs(self):
        # a captured file whose directory the forced checkout removed is fully restored
        snap = {"content": {"Worker/agents/new-agent/inbox/x.json": b'{"m": 2}'},
                "deleted": []}
        stages.restore_tracked_runtime(self.repo, snap)
        self.assertEqual(
            (self.repo / "Worker/agents/new-agent/inbox/x.json").read_bytes(),
            b'{"m": 2}')

    def test_bad_repo_yields_empty_snapshot_not_a_raise(self):
        snap = stages.snapshot_tracked_runtime(self.tmp / "nonexistent")
        self.assertEqual(snap, {"content": {}, "deleted": []})


class RestoreMainPreservesTrackedRuntimeTest(unittest.TestCase):
    """Reproduction of cause ①: restore_main's forced `checkout -f main` must not
    roll an uncommitted ledger write back to the last `chore: sync vault state`
    snapshot, nor resurrect an acked (deleted) inbox envelope."""

    ASSIGNMENT = "Worker/assignments/t.json"
    ENVELOPE = "Worker/agents/worker-default/inbox/m.json"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-b", "main", str(self.repo)],
                       check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(self.repo), "config", k, v],
                           check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args],
                       check=True, capture_output=True)

    def _branch(self):
        return subprocess.run(["git", "-C", str(self.repo), "branch", "--show-current"],
                              capture_output=True, text=True).stdout.strip()

    def test_forced_checkout_preserves_ledger_write_and_acked_envelope(self):
        # C1 on main: committed assignment (dispatched) + inbox envelope.
        self._write(self.ASSIGNMENT, '{"state": "dispatched"}')
        self._write(self.ENVELOPE, '{"msg": 1}')
        self._git("add", "-A")
        self._git("commit", "-m", "C1")
        # worker branch cut from C1.
        self._git("checkout", "-b", "worker/slug")
        # main advances: a `chore: sync vault state` C2 that MODIFIES the assignment.
        # This divergence is exactly what makes the later plain `checkout main` REFUSE
        # (the worktree's uncommitted edit to the same path would be overwritten),
        # forcing restore_main onto its `-f` fallback.
        self._git("checkout", "main")
        self._write(self.ASSIGNMENT, '{"state": "old-synced"}')
        self._git("add", "-A")
        self._git("commit", "-m", "C2 sync vault state")
        # back on the worker branch (base C1: assignment == dispatched, envelope present).
        self._git("checkout", "worker/slug")
        # simulate the live orchestrator writing between commits: Ledger.save
        # write_text advances the assignment, mailbox ack unlinks the envelope — both
        # UNCOMMITTED, the observed dirty ` M`/` D`.
        self._write(self.ASSIGNMENT, '{"state": "in_progress"}')
        (self.repo / self.ENVELOPE).unlink()
        # preconditions
        self.assertEqual((self.repo / self.ASSIGNMENT).read_bytes(),
                         b'{"state": "in_progress"}')
        self.assertFalse((self.repo / self.ENVELOPE).exists())
        self.assertEqual(self._branch(), "worker/slug")

        self.assertTrue(stages.restore_main(self.repo))

        # invariant (holds pre- and post-fix): never left on the task branch.
        self.assertEqual(self._branch(), "main")
        # Both runtime facts in ONE assertion so the RED capture shows BOTH failure
        # modes at once (pytest stops at the first failed assert): the ledger write
        # must survive (NOT roll back to C2's `old-synced`/C1's `dispatched`) and the
        # acked envelope must stay deleted (NOT resurrected — a resurrected envelope
        # makes the runner re-run the whole pipeline on the stale payload).
        observed = ((self.repo / self.ASSIGNMENT).read_bytes(),
                    (self.repo / self.ENVELOPE).exists())
        self.assertEqual(observed, (b'{"state": "in_progress"}', False))

    def test_non_diverging_checkout_leaves_carried_runtime_state_intact(self):
        # main == the worker base (no C2 divergence): the plain `checkout main`
        # SUCCEEDS and carries the uncommitted changes across. The restore must leave
        # that carried-over state intact, not corrupt it. (Green pre- and post-fix;
        # its job is to stay green once the fix is wired.)
        self._write(self.ASSIGNMENT, '{"state": "dispatched"}')
        self._write(self.ENVELOPE, '{"msg": 1}')
        self._git("add", "-A")
        self._git("commit", "-m", "C1")
        self._git("checkout", "-b", "worker/slug")
        self._write(self.ASSIGNMENT, '{"state": "in_progress"}')
        (self.repo / self.ENVELOPE).unlink()

        self.assertTrue(stages.restore_main(self.repo))

        self.assertEqual(self._branch(), "main")
        self.assertEqual((self.repo / self.ASSIGNMENT).read_bytes(),
                         b'{"state": "in_progress"}')
        self.assertFalse((self.repo / self.ENVELOPE).exists())


class PrepareExternalBranchTest(unittest.TestCase):
    """Regression: an external worker task must branch from the LATEST origin/main,
    never stack on the previous task's leftover branch (the ~/Worker clone is reused
    serially). See the auto-merge PR-stacking incident."""

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)

    def _commit(self, r, msg):
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-m", msg], check=True, capture_output=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        seed = self.tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(seed)], check=True, capture_output=True)
        self._cfg(seed)
        (seed / "main.txt").write_text("v1")
        self._commit(seed, "init")
        subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
        # reused worker clone, left on a previous task's branch with a divergent commit
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)], check=True, capture_output=True)
        self._cfg(self.clone)
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", "worker/prev"],
                       check=True, capture_output=True)
        (self.clone / "prev.txt").write_text("leftover")
        self._commit(self.clone, "prev task commit")
        # meanwhile origin/main advances
        (seed / "main.txt").write_text("v2")
        self._commit(seed, "main advance")
        subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log(self):
        return subprocess.run(["git", "-C", str(self.clone), "log", "--oneline"],
                              capture_output=True, text=True).stdout

    def test_branches_from_latest_origin_not_prev_branch(self):
        r = stages.prepare_external_branch(self.clone, "worker/new")
        self.assertEqual(r.returncode, 0, r.stderr)
        cur = subprocess.run(["git", "-C", str(self.clone), "branch", "--show-current"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(cur, "worker/new")
        self.assertEqual((self.clone / "main.txt").read_text(), "v2")   # latest main
        self.assertFalse((self.clone / "prev.txt").exists())            # not the leftover
        self.assertIn("main advance", self._log())
        self.assertNotIn("prev task commit", self._log())

    def test_continues_pushed_branch_on_retry(self):
        # A prior attempt of THIS task pushed work to origin/worker/retry-me. The
        # retry must CONTINUE it (so the next push is a fast-forward), not re-cut
        # from main (which would diverge and deadlock on push).
        seed = self.tmp / "seed"
        subprocess.run(["git", "-C", str(seed), "checkout", "-b", "worker/retry-me"],
                       check=True, capture_output=True)
        (seed / "attempt.txt").write_text("prior work")
        self._commit(seed, "prior attempt commit")
        subprocess.run(["git", "-C", str(seed), "push", "origin", "worker/retry-me"],
                       check=True, capture_output=True)
        self.assertTrue(stages.remote_branch_exists(self.clone, "worker/retry-me"))
        self.assertFalse(stages.remote_branch_exists(self.clone, "worker/never-pushed"))
        r = stages.prepare_external_branch(self.clone, "worker/retry-me")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.clone / "attempt.txt").exists())          # continued lineage
        self.assertIn("prior attempt commit", self._log())


class CheckpointPushTest(unittest.TestCase):
    """checkpoint_push saves a timed-out stage's local commits to origin/<branch>
    (no PR, fast-forward only) so the retry CONTINUES them instead of rewinding."""

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)

    def _commit(self, r, msg):
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-m", msg], check=True, capture_output=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        seed = self.tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(seed)], check=True, capture_output=True)
        self._cfg(seed)
        (seed / "main.txt").write_text("v1")
        self._commit(seed, "init")
        subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)], check=True, capture_output=True)
        self._cfg(self.clone)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _on_branch_with_commit(self, branch, fname="work.txt"):
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-B", branch, "origin/main"],
                       check=True, capture_output=True)
        (self.clone / fname).write_text("partial")
        self._commit(self.clone, f"stage work on {branch}")
        return subprocess.run(["git", "-C", str(self.clone), "rev-parse", branch],
                              capture_output=True, text=True).stdout.strip()

    def test_pushes_local_commits_and_makes_them_continuable(self):
        tip = self._on_branch_with_commit("worker/cp")
        out = stages.checkpoint_push(self.clone, "worker/cp")
        self.assertTrue(out["pushed"], out["note"])
        self.assertEqual(out["tip"], tip)
        self.assertEqual(out["commits"], 1)
        self.assertTrue(stages.remote_branch_exists(self.clone, "worker/cp"))
        ls = subprocess.run(["git", "-C", str(self.clone), "ls-remote", "origin", "worker/cp"],
                            capture_output=True, text=True).stdout
        self.assertIn(tip, ls)
        # A fresh clone's branch prep CONTINUES the checkpoint — no rewind, no orphans.
        other = self.tmp / "other"
        subprocess.run(["git", "clone", str(self.origin), str(other)], check=True, capture_output=True)
        self._cfg(other)
        r = stages.prepare_external_branch(other, "worker/cp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(r.orphaned_commits)
        self.assertTrue((other / "work.txt").exists())      # the partial work survived

    def test_second_checkpoint_pushes_only_the_new_commits(self):
        self._on_branch_with_commit("worker/cp")
        self.assertTrue(stages.checkpoint_push(self.clone, "worker/cp")["pushed"])
        (self.clone / "more.txt").write_text("more")
        self._commit(self.clone, "more stage work")
        out = stages.checkpoint_push(self.clone, "worker/cp")
        self.assertTrue(out["pushed"], out["note"])
        self.assertEqual(out["commits"], 1)                 # vs origin/<branch>, not main

    def test_nothing_new_is_a_clean_no_op(self):
        self._on_branch_with_commit("worker/cp")
        stages.checkpoint_push(self.clone, "worker/cp")     # pushes the one commit
        out = stages.checkpoint_push(self.clone, "worker/cp")   # nothing new since
        self.assertFalse(out["pushed"])
        self.assertEqual(out["commits"], 0)
        self.assertIn("nothing", out["note"])

    def test_no_op_deferral_records_no_tip(self):
        # RED control for defect A: a 0-commit deferral pushed NOTHING, so `tip`
        # must stay None. Before the fix checkpoint_push set out["tip"] to the local
        # branch sha unconditionally, and record_timeout/record_capacity wrote it as
        # `checkpoint_sha` — making the retry's resume note falsely claim
        # "origin/<branch> carries the partial work (tip …)" when nothing was pushed.
        self._on_branch_with_commit("worker/cp")
        stages.checkpoint_push(self.clone, "worker/cp")     # pushes the one commit
        out = stages.checkpoint_push(self.clone, "worker/cp")   # nothing new since
        self.assertFalse(out["pushed"])
        self.assertEqual(out["commits"], 0)
        self.assertIsNone(out["tip"])                       # no push → no sha recorded

    def test_absent_local_branch_never_raises(self):
        out = stages.checkpoint_push(self.clone, "worker/never-existed")
        self.assertFalse(out["pushed"])
        self.assertIsNone(out["tip"])
        self.assertIn("no local", out["note"])

    def test_refuses_to_checkpoint_default_branch(self):
        # A payload `branch` override can name the default branch; an FF push of it
        # would sail past the non-FF-only guard straight onto origin/main. The
        # guard must refuse EVEN when local main is ahead — pushed=False, note
        # says why, origin/main never moves.
        before = subprocess.run(["git", "-C", str(self.clone), "rev-parse", "origin/main"],
                                capture_output=True, text=True).stdout.strip()
        (self.clone / "on_main.txt").write_text("committed straight on main")
        self._commit(self.clone, "local main commit ahead of origin")
        out = stages.checkpoint_push(self.clone, "main")
        self.assertFalse(out["pushed"])
        self.assertEqual(out["commits"], 0)                 # never even measured/pushed
        self.assertIsNone(out["tip"])                       # refused before push → no sha
        self.assertIn("default", out["note"])
        after = subprocess.run(["git", "-C", str(self.clone), "ls-remote", "origin", "main"],
                               capture_output=True, text=True).stdout
        self.assertIn(before, after)                        # origin/main unmoved


class OrphanedLocalCommitTest(unittest.TestCase):
    """A prior attempt that COMMITTED but died before pushing must not lose that
    work in silence.

    `prepare_external_branch` ends in `checkout -B <branch> <base>`, and `base` is
    `origin/<branch>` only when the remote branch exists. So when the previous
    attempt committed to the clone's local `worker/<slug>` and died before the
    push, the re-dispatch's `checkout -B` force-moves that local ref back onto
    `origin/<default>`: the commits go dangling and the run continues at rc=0
    without a single log line. Neither `reset --hard` nor `clean -fd` can cause
    this — they move HEAD and untracked files, never a second ref — so the
    `checkout -B` is the only rewind point, and the check belongs immediately
    before it.

    The fix does NOT recover the work; recovery stays a human's `git cherry-pick`
    (auto-recovery is out of scope by the task's own `if_then`). It only makes the
    loss loud enough, and precise enough, to act on.
    """

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)

    def _commit(self, r, msg):
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-m", msg], check=True, capture_output=True)

    def setUp(self):
        # Fixture duplicated from PrepareExternalBranchTest deliberately: factoring
        # out a shared base class would edit the very tests that stand as this
        # change's no-regression evidence.
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.seed = self.tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(self.seed)],
                       check=True, capture_output=True)
        self._cfg(self.seed)
        (self.seed / "main.txt").write_text("v1")
        self._commit(self.seed, "init")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "main"],
                       check=True, capture_output=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        self._cfg(self.clone)
        # origin/main advances after the clone, so the rewind is a real move
        (self.seed / "main.txt").write_text("v2")
        self._commit(self.seed, "main advance")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "main"],
                       check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rev(self, ref):
        return subprocess.run(["git", "-C", str(self.clone), "rev-parse", ref],
                              capture_output=True, text=True, check=True).stdout.strip()

    def _unpushed_attempt(self, branch, files):
        """A prior attempt of THIS task: cut the branch, commit, never push."""
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", branch, "origin/main"],
                       check=True, capture_output=True)
        shas = []
        for name, body in files:
            (self.clone / name).write_text(body)
            self._commit(self.clone, f"work {name}")
            shas.append(self._rev("HEAD"))
        return shas

    def test_surfaces_commits_orphaned_by_the_rewind(self):
        shas = self._unpushed_attempt("worker/never-pushed",
                                      [("work1.txt", "a"), ("work2.txt", "b")])
        tip = shas[-1]
        self.assertFalse(stages.remote_branch_exists(self.clone, "worker/never-pushed"))

        notes = []
        r = stages.prepare_external_branch(self.clone, "worker/never-pushed",
                                           progress=notes.append)

        # rc contract: the run still proceeds. A non-zero rc here becomes
        # ExecOutcome(False, ...) in runner.py, which the orchestrator routes into
        # `_retry` — turning a condition only a human can clear into a re-dispatch
        # loop. Surfacing must not change the return contract.
        self.assertEqual(r.returncode, 0, r.stderr)
        # the rewind genuinely happened — this is not a no-op we are asserting about
        self.assertFalse((self.clone / "work2.txt").exists())
        self.assertEqual(self._rev("worker/never-pushed"), self._rev("origin/main"))

        info = r.orphaned_commits
        self.assertIsNotNone(info, "the rewind dropped 2 commits and reported nothing")
        self.assertEqual(info["count"], 2)
        self.assertEqual(info["tip"], tip)
        self.assertEqual(info["shas"], shas)        # oldest-first = cherry-pick order
        self.assertEqual(info["branch"], "worker/never-pushed")

        line = "\n".join(notes)
        self.assertIn("2 local commit", line)       # count, in words a human reads
        self.assertIn(tip[:8], line)                # and the sha to start from

    def test_surfaced_shas_are_still_cherry_pickable(self):
        """The report is only worth printing if the shas it names still resolve:
        the orphaned commits are dangling, not deleted, until git gc runs."""
        self._unpushed_attempt("worker/never-pushed",
                               [("work1.txt", "a"), ("work2.txt", "b")])
        info = stages.prepare_external_branch(self.clone, "worker/never-pushed").orphaned_commits
        self.assertIsNotNone(info)

        for sha in info["shas"]:
            t = subprocess.run(["git", "-C", str(self.clone), "cat-file", "-t", sha],
                               capture_output=True, text=True)
            self.assertEqual(t.stdout.strip(), "commit", f"{sha} no longer resolves")

        # the recovery the message tells a human to run must actually work
        cp = subprocess.run(["git", "-C", str(self.clone), "cherry-pick", *info["shas"]],
                            capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual((self.clone / "work1.txt").read_text(), "a")
        self.assertEqual((self.clone / "work2.txt").read_text(), "b")

    def test_warns_on_the_retry_path_without_breaking_fast_forward(self):
        """Unpushed commits stacked on top of a PUSHED origin/<branch> are dropped
        by the very same rewind, so the check applies to both base choices — while
        the fast-forward lineage that avoids a push deadlock stays intact."""
        subprocess.run(["git", "-C", str(self.seed), "checkout", "-b", "worker/retry-me"],
                       check=True, capture_output=True)
        (self.seed / "attempt.txt").write_text("prior work")
        self._commit(self.seed, "prior attempt commit")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "worker/retry-me"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.clone), "fetch", "origin"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", "worker/retry-me",
                        "origin/worker/retry-me"], check=True, capture_output=True)
        (self.clone / "unpushed.txt").write_text("died before push")
        self._commit(self.clone, "local-only commit")
        tip = self._rev("HEAD")

        notes = []
        r = stages.prepare_external_branch(self.clone, "worker/retry-me", progress=notes.append)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.orphaned_commits["count"], 1)
        self.assertEqual(r.orphaned_commits["tip"], tip)
        self.assertEqual(r.orphaned_commits["base"], "origin/worker/retry-me")
        self.assertIn("1 local commit", "\n".join(notes))
        # AC#4: still continuing the pushed branch, not re-cut from main
        self.assertTrue((self.clone / "attempt.txt").exists())
        self.assertEqual(self._rev("worker/retry-me"), self._rev("origin/worker/retry-me"))

    def test_silent_when_there_is_no_local_branch_yet(self):
        """First dispatch: nothing to rewind, so nothing to say."""
        notes = []
        r = stages.prepare_external_branch(self.clone, "worker/brand-new", progress=notes.append)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(r.orphaned_commits)
        self.assertEqual(notes, [])

    def test_silent_when_local_branch_is_already_contained_in_base(self):
        """A local branch with no commits of its own loses nothing when rewound."""
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", "worker/idle",
                        "origin/main"], check=True, capture_output=True)
        notes = []
        r = stages.prepare_external_branch(self.clone, "worker/idle", progress=notes.append)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(r.orphaned_commits)
        self.assertEqual(notes, [])

    def test_another_tasks_leftover_branch_is_still_rewound_silently(self):
        """AC#3: the warning is scoped to THIS task's branch. A *different* task's
        leftover local branch and its files must still be swept away without a
        word — that sweep is what stops branches stacking, and narrowing the
        report must not narrow the sweep."""
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", "worker/prev",
                        "origin/main"], check=True, capture_output=True)
        (self.clone / "prev.txt").write_text("leftover")
        self._commit(self.clone, "prev task commit")

        notes = []
        r = stages.prepare_external_branch(self.clone, "worker/new", progress=notes.append)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(r.orphaned_commits)     # not our branch — not our warning
        self.assertEqual(notes, [])
        self.assertFalse((self.clone / "prev.txt").exists())   # swept, as before
        self.assertEqual((self.clone / "main.txt").read_text(), "v2")

    # ---- squash merges are not losses -------------------------------------
    #
    # Everything above proves the detector is loud. These prove it is not loud
    # about the one case that is fine: `gh pr merge --squash --delete-branch`
    # replays the branch as ONE new commit, so the originals stop being ancestors
    # of main forever and pure ancestry calls every cleanly auto-merged branch a
    # loss (2026-08-23 moksori-icon-ui-refresh: 2 "orphaned" commits, PR #4
    # merged, content byte-identical). Suppression is allowed ONLY on a confirmed
    # merge signal — every way of failing to get one still warns.

    def _commit_at(self, r, msg, when):
        """Commit with a pinned author/committer date, so 'before/after the merge'
        is a fact of the fixture and not a race against the wall clock."""
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-m", msg],
                       check=True, capture_output=True, env=env)

    def _squash_merged_attempt(self, branch, files, when="2026-08-01T00:00:00+00:00"):
        """A COMPLETED task: branch pushed, squash-merged into main, branch deleted.

        This is what auto-merge leaves behind, and the shape that used to false-
        positive: after `fetch --prune` the remote branch is gone, so base falls
        back to origin/main, and none of the branch's own commits are ancestors of
        it — identical, to `rev-list`, to an attempt that died before pushing.
        """
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", branch, "origin/main"],
                       check=True, capture_output=True)
        shas = []
        for name, body in files:
            (self.clone / name).write_text(body)
            self._commit_at(self.clone, f"work {name}", when)
            shas.append(self._rev("HEAD"))
        subprocess.run(["git", "-C", str(self.clone), "push", "origin", branch],
                       check=True, capture_output=True)
        # …and the merge itself, from the other side, exactly as gh does it.
        for args in (["fetch", "origin"], ["checkout", "main"],
                     ["merge", "--squash", f"origin/{branch}"]):
            subprocess.run(["git", "-C", str(self.seed), *args], check=True, capture_output=True)
        self._commit(self.seed, f"squashed {branch} (#4)")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "main"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "--delete", branch],
                       check=True, capture_output=True)
        return shas

    def test_squash_merged_branch_is_not_reported_as_orphaned(self):
        """AC#1/#4: the work IS on main, under a different sha. Nothing was lost,
        so nothing is claimed."""
        shas = self._squash_merged_attempt("worker/squash-merged",
                                           [("s1.txt", "a"), ("s2.txt", "b")])

        notes = []
        with mock.patch.object(stages, "gh_pr_merged_at",
                               return_value="2099-01-01T00:00:00Z") as gh:
            r = stages.prepare_external_branch(self.clone, "worker/squash-merged",
                                               progress=notes.append)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(r.orphaned_commits, "a merged PR is not a loss")
        self.assertEqual(notes, [])
        # asked about OUR branch, and aimed at the clone — `gh pr view` resolves
        # the repo from cwd, and the runner's own cwd is the vault, not this clone.
        self.assertEqual(gh.call_args.args[0], "worker/squash-merged")
        self.assertEqual(gh.call_args.kwargs["cwd"], self.clone)

        # The suppression is only honest if the work really did land: prove it,
        # or this test would also pass on a detector that had simply gone blind.
        self.assertTrue((self.clone / "s1.txt").exists())
        self.assertEqual((self.clone / "s2.txt").read_text(), "b")
        for sha in shas:                       # …and prove ancestry still says "lost"
            merged = subprocess.run(
                ["git", "-C", str(self.clone), "merge-base", "--is-ancestor",
                 sha, "origin/main"], capture_output=True)
            self.assertNotEqual(merged.returncode, 0,
                                "fixture is wrong: a squash must NOT leave ancestors")

    def test_unknown_merge_signal_still_warns(self):
        """AC#2, the whole safety argument: no gh / no PR / no network / not authed
        all arrive as None, and None is 'cannot tell', never 'safe'."""
        shas = self._squash_merged_attempt("worker/squash-unknown",
                                           [("u1.txt", "a"), ("u2.txt", "b")])
        notes = []
        with mock.patch.object(stages, "gh_pr_merged_at", return_value=None):
            r = stages.prepare_external_branch(self.clone, "worker/squash-unknown",
                                               progress=notes.append)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(r.orphaned_commits)
        self.assertEqual(r.orphaned_commits["count"], 2)
        self.assertEqual(r.orphaned_commits["shas"], shas)
        self.assertIn("2 local commit", "\n".join(notes))

    def test_commits_made_after_the_merge_are_still_reported(self):
        """The reused-branch trap: a merged PR for this branch does NOT vouch for
        work committed after it. One commit newer than `mergedAt` puts the WHOLE
        set back on the report — the PR cannot contain any of what follows it."""
        old = self._squash_merged_attempt("worker/squash-then-more",
                                          [("m1.txt", "a")],
                                          when="2026-08-01T00:00:00+00:00")
        (self.clone / "after.txt").write_text("committed after the merge")
        self._commit_at(self.clone, "post-merge work", "2026-08-20T00:00:00+00:00")
        newer = self._rev("HEAD")

        notes = []
        with mock.patch.object(stages, "gh_pr_merged_at",
                               return_value="2026-08-10T00:00:00Z"):
            r = stages.prepare_external_branch(self.clone, "worker/squash-then-more",
                                               progress=notes.append)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(r.orphaned_commits, "post-merge work was silenced")
        self.assertEqual(r.orphaned_commits["shas"], [*old, newer])
        self.assertIn("2 local commit", "\n".join(notes))

    def test_commits_ahead_of_a_pushed_branch_are_never_suppressed(self):
        """First condition, and the load-bearing one: while origin/<branch> still
        exists, commits ahead of it were by definition never in any PR. Decided on
        refs alone — gh is not even asked, so no merge signal can override it."""
        subprocess.run(["git", "-C", str(self.seed), "checkout", "-b", "worker/retry-me"],
                       check=True, capture_output=True)
        (self.seed / "attempt.txt").write_text("prior work")
        self._commit(self.seed, "prior attempt commit")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "worker/retry-me"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.clone), "fetch", "origin"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", "worker/retry-me",
                        "origin/worker/retry-me"], check=True, capture_output=True)
        (self.clone / "unpushed.txt").write_text("died before push")
        self._commit(self.clone, "local-only commit")

        with mock.patch.object(stages, "gh_pr_merged_at",
                               return_value="2099-01-01T00:00:00Z") as gh:
            r = stages.prepare_external_branch(self.clone, "worker/retry-me")

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.orphaned_commits["count"], 1)
        self.assertEqual(r.orphaned_commits["base"], "origin/worker/retry-me")
        gh.assert_not_called()

    def test_a_merge_probe_failure_cannot_break_branch_prep(self):
        """AC#5: branch prep must not acquire a new way to fail. A probe that
        explodes degrades to 'warn' — the same direction every other unknown takes
        — and the exception never reaches the caller."""
        self._squash_merged_attempt("worker/squash-boom", [("b1.txt", "a")])
        notes = []
        with mock.patch.object(stages, "gh_pr_merged_at",
                               side_effect=RuntimeError("gh exploded")):
            r = stages.prepare_external_branch(self.clone, "worker/squash-boom",
                                               progress=notes.append)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(r.orphaned_commits)
        self.assertEqual(r.orphaned_commits["count"], 1)
        self.assertIn("1 local commit", "\n".join(notes))

    def test_an_unreadable_commit_date_falls_back_to_warning(self):
        """The last unknown: if git's own `%cI` cannot be read back for every
        commit, condition 3 is unproven — so the set is reported, not excused."""
        self._squash_merged_attempt("worker/squash-nodate", [("n1.txt", "a")])
        real = stages._git

        def stub(repo_dir, *args):
            if args[:3] == ("show", "-s", "--format=%cI"):
                return subprocess.CompletedProcess(args=list(args), returncode=1,
                                                   stdout="", stderr="boom")
            return real(repo_dir, *args)

        with mock.patch.object(stages, "gh_pr_merged_at",
                               return_value="2099-01-01T00:00:00Z"), \
                mock.patch.object(stages, "_git", side_effect=stub):
            r = stages.prepare_external_branch(self.clone, "worker/squash-nodate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(r.orphaned_commits)
        self.assertEqual(r.orphaned_commits["count"], 1)


class CleanedUntrackedReportTest(unittest.TestCase):
    """The `git clean -fd` in branch prep deletes every untracked file in the clone
    and says nothing about it anywhere.

    From inside `prepare_external_branch` a sibling run's uncommitted work is
    indistinguishable from a stale leftover — both are just untracked paths — so the
    sweep that stops task branches from stacking is also the sweep that can eat work
    nobody committed yet, at rc=0 with an empty log. The fix does NOT weaken the
    clean (preserving a `long_job`'s own output is `snapshot_job_artifacts`'s job,
    and only for that job): it writes down what went, so the loss is greppable in
    run.log instead of invisible.
    """

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)

    def _commit(self, r, msg):
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-m", msg], check=True, capture_output=True)

    def setUp(self):
        # Fixture duplicated from OrphanedLocalCommitTest deliberately — same reason
        # given there: factoring out a shared base class would edit the very tests
        # that stand as this change's no-regression evidence.
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.seed = self.tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(self.seed)],
                       check=True, capture_output=True)
        self._cfg(self.seed)
        (self.seed / "main.txt").write_text("v1")
        self._commit(self.seed, "init")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "main"],
                       check=True, capture_output=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        self._cfg(self.clone)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _branch(self):
        return subprocess.run(["git", "-C", str(self.clone), "branch", "--show-current"],
                              capture_output=True, text=True).stdout.strip()

    def test_reports_paths_the_clean_deleted(self):
        (self.clone / "junk.txt").write_text("a sibling run's uncommitted work")
        (self.clone / "junkdir").mkdir()
        (self.clone / "junkdir" / "nested.txt").write_text("and its output dir")

        notes = []
        r = stages.prepare_external_branch(self.clone, "worker/new", progress=notes.append)

        self.assertEqual(r.returncode, 0, r.stderr)
        info = r.cleaned_untracked
        self.assertIsNotNone(info, "the clean deleted 2 paths and reported nothing")
        self.assertEqual(info["count"], 2)
        # -uall => the files INSIDE an untracked dir, not the dir itself
        self.assertEqual(sorted(info["paths"]), ["junk.txt", "junkdir/nested.txt"])

        line = "\n".join(notes)
        self.assertIn("junk.txt", line)
        self.assertIn("junkdir/nested.txt", line)

        # ...and the clean still cleaned. Reporting must not become preserving:
        # the sweep is what keeps a previous task's leftovers out of this run.
        self.assertFalse((self.clone / "junk.txt").exists())
        self.assertFalse((self.clone / "junkdir").exists())
        self.assertEqual(self._branch(), "worker/new")

    def test_silent_when_nothing_untracked(self):
        """A clean clone has nothing to say — no attribute, no log line."""
        notes = []
        r = stages.prepare_external_branch(self.clone, "worker/new", progress=notes.append)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(r.cleaned_untracked)
        self.assertEqual(notes, [])

    def test_long_list_is_truncated_in_the_log(self):
        """A clone a long job filled up can hold thousands of untracked paths, and
        one log line that wide makes run.log unreadable. The dict keeps everything;
        only the line is cut."""
        total = stages._CLEANED_PATH_LIMIT + 5
        # zero-padded so lexicographic order (what `git status` emits) is numeric
        for i in range(total):
            (self.clone / f"f{i:02d}.txt").write_text("x")

        notes = []
        r = stages.prepare_external_branch(self.clone, "worker/new", progress=notes.append)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.cleaned_untracked["count"], total)
        self.assertEqual(len(r.cleaned_untracked["paths"]), total)   # dict is complete

        line = "\n".join(notes)
        self.assertIn("... +5 more", line)
        self.assertIn(f"f{stages._CLEANED_PATH_LIMIT - 1:02d}.txt", line)   # last shown
        self.assertNotIn(f"f{stages._CLEANED_PATH_LIMIT:02d}.txt", line)    # first cut
        self.assertNotIn(f"f{total - 1:02d}.txt", line)

    def test_detection_failure_does_not_break_prep(self):
        """A detection that cannot run must never become the reason branch prep
        fails: `_untracked_paths` returns (paths, error), and a non-empty error
        wins over whatever it managed to collect."""
        (self.clone / "junk.txt").write_text("still gets swept")

        notes = []
        with mock.patch.object(stages, "_untracked_paths",
                               return_value=(["junk.txt"], "boom")):
            r = stages.prepare_external_branch(self.clone, "worker/new", progress=notes.append)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(r.cleaned_untracked)
        self.assertEqual(notes, [])
        # the prep itself completed as usual
        self.assertEqual(self._branch(), "worker/new")
        self.assertFalse((self.clone / "junk.txt").exists())


class LongJobArtifactSurvivalTest(unittest.TestCase):
    """A `long_job`'s output must survive the branch prep that runs right after it.

    The pipeline defers a task until the job exits and then, three lines later,
    runs `prepare_external_branch` — whose `git clean -fd` deletes exactly the
    untracked files the job just spent its runtime producing. The symptom is
    rc=0 with no data. These tests pin the fix's contract: THIS job's output
    survives, another task's leftovers still die, and the retry lineage is
    untouched — i.e. the clean is not weakened, only stepped around.
    """

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)

    def _commit(self, r, msg):
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-m", msg], check=True, capture_output=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.seed = self.tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(self.seed)],
                       check=True, capture_output=True)
        self._cfg(self.seed)
        (self.seed / "main.txt").write_text("v1")
        self._commit(self.seed, "init")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "main"],
                       check=True, capture_output=True)
        # the reused worker clone, parked on a previous task's branch
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        self._cfg(self.clone)
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", "worker/prev"],
                       check=True, capture_output=True)
        (self.seed / "main.txt").write_text("v2")
        self._commit(self.seed, "main advance")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "main"],
                       check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _started_ts(self, ago_s=60):
        """An ISO stamp shaped exactly like jobs.start writes it (floored to the
        second, tz-aware), far enough back that files written 'now' are fresh."""
        t = dt.datetime.now().astimezone() - dt.timedelta(seconds=ago_s)
        return t.isoformat(timespec="seconds")

    def _job_output(self, rel="data/candles.csv", body="close,volume"):
        p = self.clone / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        return p

    def _leftover(self, rel="stale-junk.txt", age_s=3600):
        """Another task's residue: untracked, but written long before this job
        started. The clean exists to remove exactly this."""
        p = self.clone / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("someone else's mess")
        old = time.time() - age_s
        os.utime(p, (old, old))
        return p

    def test_job_output_survives_branch_prep_while_leftovers_still_die(self):
        out, junk = self._job_output(), self._leftover()
        snap = stages.snapshot_job_artifacts(self.clone, self.clone, self._started_ts())
        self.assertEqual(snap["problems"], [])
        self.assertIn("data/candles.csv", snap["files"])
        self.assertNotIn("stale-junk.txt", snap["files"])   # not ours to protect
        self.assertFalse(out.exists(), "the job's output should be staged out of the clone")
        self.assertTrue(junk.exists(), "the leftover must stay where the clean can reach it")

        r = stages.prepare_external_branch(self.clone, "worker/new")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(junk.exists(), "branch prep must still remove other tasks' residue")

        self.assertEqual(stages.restore_job_artifacts(self.clone, snap), [])
        self.assertEqual(out.read_text(), "close,volume")   # <- the whole point
        self.assertEqual((self.clone / "main.txt").read_text(), "v2")   # branched from latest
        self.assertFalse(Path(snap["dir"]).exists())        # stash cleaned up on success

    def test_stash_lives_outside_the_clone_so_the_clean_cannot_reach_it(self):
        self._job_output()
        snap = stages.snapshot_job_artifacts(self.clone, self.clone, self._started_ts())
        stash = Path(snap["dir"]).resolve()
        self.assertNotEqual(stash, self.clone.resolve())
        with self.assertRaises(ValueError):      # not under the worktree at all
            stash.relative_to(self.clone.resolve())

    def test_retry_lineage_survives_alongside_the_output(self):
        """A prior attempt of THIS task pushed a branch; branch prep must still
        continue that lineage (so the next push fast-forwards) AND keep the job's
        data. Preserving one by breaking the other is not a fix."""
        subprocess.run(["git", "-C", str(self.seed), "checkout", "-b", "worker/retry-me"],
                       check=True, capture_output=True)
        (self.seed / "attempt.txt").write_text("prior work")
        self._commit(self.seed, "prior attempt commit")
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "worker/retry-me"],
                       check=True, capture_output=True)
        out = self._job_output()

        snap = stages.snapshot_job_artifacts(self.clone, self.clone, self._started_ts())
        r = stages.prepare_external_branch(self.clone, "worker/retry-me")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(stages.restore_job_artifacts(self.clone, snap), [])

        self.assertTrue((self.clone / "attempt.txt").exists())   # fast-forward lineage kept
        self.assertEqual(out.read_text(), "close,volume")
        log = subprocess.run(["git", "-C", str(self.clone), "log", "--oneline"],
                             capture_output=True, text=True).stdout
        self.assertIn("prior attempt commit", log)

    def test_ignored_files_are_left_alone_because_clean_fd_never_had_them(self):
        """`prepare_external_branch` cleans without `-x`, so gitignored output was
        never at risk. Staging it anyway would be scope the fix has not earned."""
        (self.clone / ".gitignore").write_text("cache/\n")
        self._commit(self.clone, "add gitignore")
        (self.clone / "cache").mkdir()
        (self.clone / "cache" / "big.bin").write_text("ignored")
        self._job_output()
        snap = stages.snapshot_job_artifacts(self.clone, self.clone, self._started_ts())
        self.assertEqual(snap["files"], ["data/candles.csv"])
        self.assertTrue((self.clone / "cache" / "big.bin").exists())

    def test_fresh_output_outside_the_job_cwd_is_surfaced_not_dropped(self):
        """Scope is the job's own cwd. A fresh untracked file outside it cannot be
        explained as another task's leftover either — staying quiet there would
        recreate the original bug one directory over (acceptance #4)."""
        (self.clone / "sub").mkdir()
        (self.clone / "sub" / "kept.txt").write_text("in scope")
        stray = self.clone / "escaped.csv"
        stray.write_text("out of scope")
        snap = stages.snapshot_job_artifacts(self.clone, self.clone / "sub", self._started_ts())
        self.assertIn("escaped.csv", snap["skipped_outside"])
        self.assertTrue(snap["problems"], "an unexplainable fresh file must not pass silently")
        # and nothing was moved: the caller aborts before the clean, so the data
        # is still on disk for a human rather than half-staged.
        self.assertEqual(snap["files"], [])
        self.assertTrue(stray.exists())
        self.assertTrue((self.clone / "sub" / "kept.txt").exists())

    def test_unreadable_start_time_is_a_problem_not_a_silent_pass(self):
        self._job_output()
        snap = stages.snapshot_job_artifacts(self.clone, self.clone, None)
        self.assertTrue(snap["problems"])
        self.assertEqual(snap["files"], [])

    def test_restore_refuses_to_overwrite_a_path_the_branch_owns(self):
        """If the fresh branch legitimately contains that path, dropping the job's
        file over it would be an invisible edit. Refuse, keep the stash, report."""
        out = self._job_output()
        snap = stages.snapshot_job_artifacts(self.clone, self.clone, self._started_ts())
        self.assertEqual(stages.prepare_external_branch(self.clone, "worker/new").returncode, 0)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("the branch's own file")
        problems = stages.restore_job_artifacts(self.clone, snap)
        self.assertTrue(problems)
        self.assertIn("data/candles.csv", " ".join(problems))
        self.assertEqual(out.read_text(), "the branch's own file")   # not clobbered
        self.assertTrue(Path(snap["dir"]).exists(), "the stash is the human's recovery copy")

    def test_a_leftover_stash_blocks_rather_than_being_overwritten(self):
        """The stash only survives a FAILED restore, so it is someone's recovery
        data. Snapshotting on top of it would destroy that."""
        self._job_output()
        stash = Path(self.clone).parent / f".{self.clone.name}-longjob-stash"
        stash.mkdir(parents=True)
        (stash / "earlier.csv").write_text("rescued earlier")
        snap = stages.snapshot_job_artifacts(self.clone, self.clone, self._started_ts())
        self.assertTrue(snap["problems"])
        self.assertEqual((stash / "earlier.csv").read_text(), "rescued earlier")


class LongJobArtifactEndToEndTest(unittest.TestCase):
    """The defect as the pipeline actually executes it — `prepare_external_branch`
    NOT mocked.

    `ResumeInExecuteTest` cannot see this bug: it patches `subprocess.run`
    globally and stubs branch prep, so no git ever runs. Here the stage stub is a
    real `/bin/sh` that writes its artifact ONLY if the job's output is present,
    so "the data existed when the stage started" is observed literally rather
    than asserted about.
    """

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)

    def setUp(self):
        from todo_system.orchestrator import jobs
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.seed = seed = self.tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(seed)], check=True, capture_output=True)
        self._cfg(seed)
        (seed / "main.txt").write_text("v1")
        subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        self._cfg(self.clone)
        # the vault stand-in: stages run with cwd=_VAULT_DATA_ROOT and it gets restore_main'd
        self.vault = self.tmp / "vault"
        subprocess.run(["git", "init", "-b", "main", str(self.vault)], check=True, capture_output=True)
        self._cfg(self.vault)
        (self.vault / "seed.txt").write_text("x")
        subprocess.run(["git", "-C", str(self.vault), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.vault), "commit", "-m", "init"],
                       check=True, capture_output=True)
        self._orig = (stages._VAULT_DATA_ROOT, stages.RUNS_DIR, jobs.JOBS_DIR)
        stages._VAULT_DATA_ROOT = self.vault
        stages.RUNS_DIR = self.tmp / "runs"
        stages.RUNS_DIR.mkdir(parents=True)
        jobs.JOBS_DIR = self.tmp / "jobs"

    def tearDown(self):
        from todo_system.orchestrator import jobs
        jobs.stop("t")
        stages._VAULT_DATA_ROOT, stages.RUNS_DIR, jobs.JOBS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _execute(self, long_job="mkdir -p data && printf close,volume > data/candles.csv",
                 extra_mocks=(), **payload_extra):
        """Run one dispatch of a long_job'd external-repo task for real."""
        from todo_system.orchestrator import message as m
        from todo_system.orchestrator import repos
        probe = self.clone / "data" / "candles.csv"

        def stub_argv(stage, **kw):
            # Not `claude -p`, but a real process obeying the same contract: a
            # stage passes only if the job's data is there when it starts.
            art = Path(kw["run_dir"]) / stages.STAGE_ARTIFACT[stage]
            return ["/bin/sh", "-c",
                    f'test -f "{probe}" && '
                    f'printf \'overall_status: pass\\nmerged: yes\\n\' > "{art}"']

        msg = m.Envelope(
            to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
            payload={"repo": "https://h/o/r.git", "worker_mode": "auto",
                     "long_job": long_job, **payload_extra})
        ex = StagedExecutor()
        with mock.patch.object(repos, "ensure_worktree", return_value=self.clone), \
             mock.patch.object(stages, "merge_landed", return_value=True), \
             mock.patch.object(stages, "remove_worktree"), \
             mock.patch.object(stages, "build_stage_argv", side_effect=stub_argv):
            with contextlib.ExitStack() as stack:
                for cm in extra_mocks:
                    stack.enter_context(cm)
                return ex.execute(msg, mock.Mock(), lambda *_: None)

    def _run_the_job(self, **kw):
        """First dispatch starts the job and defers; wait for it to finish."""
        from todo_system.orchestrator import jobs
        first = self._execute(**kw)
        self.assertFalse(first.ok)                       # deferred while the job runs
        self.assertTrue(first.payload.get("job_running"))
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"),
                        "the test's long_job should finish immediately")
        return first

    def _branch_exists(self, name):
        """Whether branch prep got as far as `checkout -B`. The CURRENT branch is
        no good as a probe: execute() restores the shared clone to its default
        branch on the way out (see test_external_clone_is_restored_to_its_default_
        branch), so a finished run and a run that never branched both read `main`.
        The ref itself survives that restore."""
        return subprocess.run(["git", "-C", str(self.clone), "rev-parse", "--verify", name],
                              capture_output=True).returncode == 0

    def _publish_probe(self):
        """Put the stage stub's probe file on origin/main, so a run with no
        long_job still has it after branch prep — tracked content is restored by
        the reset, untracked content is (correctly) cleaned."""
        p = self.seed / "data" / "candles.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("close,volume")
        subprocess.run(["git", "-C", str(self.seed), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.seed), "commit", "-m", "probe"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.seed), "push", "origin", "main"],
                       check=True, capture_output=True)

    def test_the_pipeline_runs_against_the_data_the_job_produced(self):
        self._run_the_job()
        self.assertTrue((self.clone / "data" / "candles.csv").exists())

        out = self._execute()   # the gate opens; branch prep runs FOR REAL
        self.assertTrue(out.ok, out.summary)
        self.assertTrue((self.clone / "data" / "candles.csv").exists(),
                        "the job's output was deleted by branch prep")
        self.assertEqual((self.clone / "data" / "candles.csv").read_text(), "close,volume")
        self.assertEqual(out.payload["stages"], list(stages.STAGES_AUTO))
        self.assertTrue(self._branch_exists("worker/t"))   # branch prep really ran

    def test_output_it_cannot_claim_stops_the_run_before_anything_is_deleted(self):
        """acceptance #4 — the symptom was rc=0 with no files, so an unpreservable
        artifact must not read as success. The job's cwd is `data/`, so the file it
        drops at the clone root is fresh, untracked and unexplainable: the run stops
        while BOTH files are still on disk and before branch prep can delete either."""
        (self.clone / "data").mkdir()
        self._run_the_job(working_dir="data",
                          long_job="printf close > candles.csv; printf x > ../stray.csv")
        out = self._execute(working_dir="data", long_job="true")
        self.assertFalse(out.ok)
        self.assertIn("could not be preserved", out.summary)
        self.assertIn("stray.csv", out.summary)
        self.assertTrue((self.clone / "data" / "candles.csv").exists())
        self.assertTrue((self.clone / "stray.csv").exists())
        self.assertFalse(self._branch_exists("worker/t"))  # branch prep never ran
        self.assertEqual(out.payload["long_job_stash"],
                         str(self.clone.parent / f".{self.clone.name}-longjob-stash"))

    def test_a_failed_restore_is_a_failed_run_that_names_the_stash(self):
        """The other half of #4: data that made it out but not back must not be
        left as a silent rc=0 either. The stash path is the recovery instruction."""
        self._run_the_job()
        out = self._execute(extra_mocks=[mock.patch.object(
            stages, "restore_job_artifacts",
            return_value=["path already taken on the branch: data/candles.csv"])])
        self.assertFalse(out.ok)
        self.assertIn("not restored", out.summary)
        self.assertIn("data/candles.csv", out.summary)
        self.assertIn(out.payload["long_job_stash"], out.summary)

    def test_preservation_is_skipped_entirely_when_the_task_has_no_long_job(self):
        """No job, no snapshot: the overwhelming majority of external tasks must
        not pay for — or be exposed to — this path at all."""
        self._publish_probe()      # tracked, so branch prep restores rather than cleans it
        with mock.patch.object(stages, "snapshot_job_artifacts") as snap, \
             mock.patch.object(stages, "restore_job_artifacts") as restore:
            out = self._execute(long_job=None)
        self.assertTrue(out.ok, out.summary)
        snap.assert_not_called()
        restore.assert_not_called()


class ExecuteDiskGuardHookTest(unittest.TestCase):
    """The run-start disk guard's wiring in `_execute`; disk_guard.py itself is
    tested in test_disk_guard.py. The guard is replaced here (and the suite-wide
    fixture keeps the real one switched off), so what is observed is the hook
    alone: once per real run, BEFORE branch prep's `git fetch`, never on this
    run's own repo, fail-open, and recorded in stages.json only when it acted.
    Harness from LongJobArtifactEndToEndTest: real git, real branch prep, and a
    real `/bin/sh` stage that writes its artifact.
    """

    REAL = object()      # guard=REAL leaves the module's own ensure_free_space in place

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)

    def setUp(self):
        from todo_system.orchestrator import jobs
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        seed = self.tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(seed)], check=True, capture_output=True)
        self._cfg(seed)
        (seed / "main.txt").write_text("v1")
        subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        self._cfg(self.clone)
        self.vault = self.tmp / "vault"
        subprocess.run(["git", "init", "-b", "main", str(self.vault)], check=True, capture_output=True)
        self._cfg(self.vault)
        (self.vault / "seed.txt").write_text("x")
        subprocess.run(["git", "-C", str(self.vault), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.vault), "commit", "-m", "init"],
                       check=True, capture_output=True)
        self._orig = (stages._VAULT_DATA_ROOT, stages.RUNS_DIR, jobs.JOBS_DIR)
        stages._VAULT_DATA_ROOT = self.vault
        stages.RUNS_DIR = self.tmp / "runs"
        stages.RUNS_DIR.mkdir(parents=True)
        jobs.JOBS_DIR = self.tmp / "jobs"
        self.calls: list[dict] = []
        self.branch_at_call: list[bool] = []
        self.lines: list[str] = []

    def tearDown(self):
        from todo_system.orchestrator import jobs
        jobs.stop("t")
        stages._VAULT_DATA_ROOT, stages.RUNS_DIR, jobs.JOBS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _branch_exists(repo, name="worker/t"):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                               f"refs/heads/{name}"], capture_output=True).returncode == 0

    def _spy(self, **kw):
        """The default guard: records its call and whether branch prep had run."""
        self.calls.append(kw)
        self.branch_at_call.append(self._branch_exists(self.repo_under_test))
        kw["progress"]("from the guard")
        return {"triggered": False, "free_before": 9 * disk_guard.GIB}

    def _execute(self, guard=None, extra_mocks=(), **payload_extra):
        from todo_system.orchestrator import message as m
        from todo_system.orchestrator import repos

        def stub_argv(stage, **kw):
            art = Path(kw["run_dir"]) / stages.STAGE_ARTIFACT[stage]
            return ["/bin/sh", "-c", f'printf \'overall_status: pass\\nmerged: yes\\n\' > "{art}"']

        payload = {"repo": "https://h/o/r.git", "worker_mode": "auto", **payload_extra}
        payload = {k: v for k, v in payload.items() if v is not None}   # repo=None → a vault task
        self.repo_under_test = self.clone if payload.get("repo") else self.vault
        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C", payload=payload)
        mocks = [mock.patch.object(repos, "ensure_worktree", return_value=self.clone),
                 mock.patch.object(stages, "merge_landed", return_value=True),
                 mock.patch.object(stages, "remove_worktree"),
                 mock.patch.object(stages, "build_stage_argv", side_effect=stub_argv),
                 *extra_mocks]
        if guard is not self.REAL:
            mocks.append(mock.patch.object(disk_guard, "ensure_free_space",
                                           side_effect=guard or self._spy))
        with contextlib.ExitStack() as stack:
            for cm in mocks:
                stack.enter_context(cm)
            return StagedExecutor().execute(msg, mock.Mock(), self.lines.append)

    def _progress_json(self, out):
        return json.loads((Path(out.payload["run_dir"]) / stages.STAGE_PROGRESS)
                          .read_text(encoding="utf-8"))

    def test_runs_once_before_branch_prep_and_never_on_this_runs_clone(self):
        out = self._execute()
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([Path(p) for p in self.calls[0]["exclude"]], [self.clone])
        self.assertEqual(self.branch_at_call, [False])      # branch prep had not run yet…
        self.assertTrue(self._branch_exists(self.clone))     # …and then it did
        self.assertIn("from the guard", self.lines)          # the run's own progress sink

    def test_a_vault_run_is_guarded_too_with_the_vault_excluded(self):
        out = self._execute(repo=None)
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([Path(p) for p in self.calls[0]["exclude"]], [self.vault])
        self.assertEqual(self.branch_at_call, [False])
        self.assertTrue(self._branch_exists(self.vault))

    def test_a_guard_that_raises_cannot_stop_the_run(self):
        out = self._execute(guard=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(out.payload["stages"], list(stages.STAGES_AUTO))
        self.assertIn("disk guard skipped (RuntimeError)", self.lines)

    def test_a_guard_that_acted_is_recorded_in_stages_json(self):
        g = disk_guard.GIB
        record = {"triggered": True, "free_before": 2 * g, "free_after": 6 * g,
                  "threshold": 3 * g, "target": 5 * g, "cleaned": ["DripCoffee"],
                  "skipped": {"KnowScroll": "dirty"}, "elapsed_s": 41.5}
        out = self._execute(guard=mock.Mock(return_value=record))
        self.assertTrue(out.ok, out.summary)
        prog = self._progress_json(out)
        self.assertEqual(prog["disk_guard"], record)
        self.assertEqual(prog["completed"], list(stages.STAGES_AUTO))   # siblings intact

    def test_a_guard_that_did_nothing_leaves_no_key(self):
        out = self._execute(guard=mock.Mock(return_value={"triggered": False, "free_before": 1}))
        self.assertTrue(out.ok, out.summary)
        self.assertNotIn("disk_guard", self._progress_json(out))

    def test_an_answer_that_is_not_a_record_is_ignored(self):
        out = self._execute(guard=mock.Mock(return_value=None))
        self.assertTrue(out.ok, out.summary)
        self.assertNotIn("disk_guard", self._progress_json(out))

    def test_not_called_on_a_long_job_first_dispatch(self):
        # the dispatch only starts the job and defers: nothing to free space for
        from todo_system.orchestrator import jobs
        guard = mock.Mock()
        first = self._execute(guard=guard, long_job="true")
        self.assertFalse(first.ok)
        self.assertTrue(first.payload.get("job_running"))
        guard.assert_not_called()
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"))

    def test_not_called_while_a_live_sibling_holds_the_checkout(self):
        guard = mock.Mock()
        hold = mock.patch.object(StagedExecutor, "_live_sibling_hold",
                                 return_value=("deferred — held by a LIVE sibling run", "other"))
        out = self._execute(guard=guard, extra_mocks=[hold])
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("checkout_busy"))
        guard.assert_not_called()

    def test_the_real_guard_says_nothing_under_the_suite_fixture(self):
        # what keeps every other _execute suite byte-identical: switched off by
        # todo_system/conftest.py, the real guard emits no line and no record
        out = self._execute(guard=self.REAL)
        self.assertTrue(out.ok, out.summary)
        self.assertEqual([line for line in self.lines if "disk guard" in line], [])
        self.assertNotIn("disk_guard", self._progress_json(out))


class VaultLongJobPathIsUnchangedTest(unittest.TestCase):
    """acceptance #5 — a vault task (no `repo:`) never enters
    prepare_external_branch, so it must never enter the preservation that exists
    to survive it either. There is no `git clean -fd` on this path to defend
    against, and snapshotting here would move a LIVE vault's untracked files —
    including a human's work in progress — for no reason at all.
    """

    def setUp(self):
        from todo_system.orchestrator import jobs
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "vault"
        subprocess.run(["git", "init", "-b", "main", str(self.repo)],
                       check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(self.repo), "config", k, v],
                           check=True, capture_output=True)
        (self.repo / "seed.txt").write_text("x")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "init"],
                       check=True, capture_output=True)
        self._orig = (stages._VAULT_DATA_ROOT, stages.RUNS_DIR, jobs.JOBS_DIR)
        stages._VAULT_DATA_ROOT = self.repo
        stages.RUNS_DIR = self.tmp / "runs"
        stages.RUNS_DIR.mkdir(parents=True)
        jobs.JOBS_DIR = self.tmp / "jobs"

    def tearDown(self):
        from todo_system.orchestrator import jobs
        jobs.stop("t")
        stages._VAULT_DATA_ROOT, stages.RUNS_DIR, jobs.JOBS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _execute(self):
        from todo_system.orchestrator import message as m
        real_run = subprocess.run

        def stub_argv(stage, **kw):
            stub_argv.pending = (kw["run_dir"], stage)
            return ["claude", "-p", stage]

        def fake_run(argv, **kw):
            # real git + the job launcher (bare, or wrapped in systemd-run
            # when the suite itself runs inside a systemd unit)
            if argv and (argv[0] in ("git", "/bin/sh")
                         or argv[0].endswith("systemd-run")):
                return real_run(argv, **kw)
            rd, stage = stub_argv.pending
            (Path(rd) / stages.STAGE_ARTIFACT[stage]).write_text(
                "overall_status: pass\nmerged: yes\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        # payload WITHOUT `repo:` — the vault path — but WITH a long_job, which is
        # the combination the preservation branch keys on.
        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload={"worker_mode": "auto", "long_job": "printf done > job-out.txt"})
        ex = StagedExecutor()
        with mock.patch.object(stages, "build_stage_argv", side_effect=stub_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            return ex.execute(msg, mock.Mock(), lambda *_: None)

    def test_vault_long_job_never_snapshots_and_leaves_its_files_alone(self):
        from todo_system.orchestrator import jobs
        bystander = self.repo / "human-wip.txt"
        bystander.write_text("someone is editing this")
        first = self._execute()
        self.assertFalse(first.ok)
        self.assertTrue(first.payload.get("job_running"))
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"))

        with mock.patch.object(stages, "snapshot_job_artifacts") as snap, \
             mock.patch.object(stages, "restore_job_artifacts") as restore:
            out = self._execute()
        self.assertTrue(out.ok, out.summary)
        snap.assert_not_called()
        restore.assert_not_called()
        # and the vault's untracked files — the job's own and a bystander's — were
        # never moved anywhere
        self.assertEqual((self.repo / "job-out.txt").read_text(), "done")
        self.assertEqual(bystander.read_text(), "someone is editing this")
        self.assertFalse((self.tmp / f".{self.repo.name}-longjob-stash").exists())


class PushGuardTest(unittest.TestCase):
    """The force-push hard rule enforced at the git layer, not just in prose.
    ai-stock-trading-13's verify stage force-pushed over a sibling run's lineage
    under a broad Bash(git:*) grant that the tool-whitelist filter passes."""

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v], check=True, capture_output=True)

    def _commit(self, r, msg):
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-m", msg], check=True, capture_output=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)], check=True, capture_output=True)
        self._cfg(self.clone)
        (self.clone / "f.txt").write_text("v1")
        self._commit(self.clone, "c1")
        subprocess.run(["git", "-C", str(self.clone), "push", "origin", "main"],
                       check=True, capture_output=True)
        stages.install_push_guard(self.clone)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _push(self, *args):
        return subprocess.run(["git", "-C", str(self.clone), "push", *args],
                              capture_output=True, text=True)

    def test_installs_an_executable_hook(self):
        hook = self.clone / ".git" / "hooks" / "pre-push"
        self.assertTrue(hook.exists())
        self.assertTrue(hook.stat().st_mode & 0o111)   # executable

    def test_fast_forward_push_is_allowed(self):
        (self.clone / "f.txt").write_text("v2")
        self._commit(self.clone, "c2")
        r = self._push("origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_new_branch_push_is_allowed(self):
        subprocess.run(["git", "-C", str(self.clone), "checkout", "-b", "worker/x"],
                       check=True, capture_output=True)
        (self.clone / "g.txt").write_text("new")
        self._commit(self.clone, "on-branch")
        r = self._push("origin", "worker/x")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_force_push_over_rewritten_history_is_rejected(self):
        # rewrite the last commit so main diverges from origin/main, then try to
        # slam it over the remote — the exact shape of the incident
        (self.clone / "f.txt").write_text("rewritten")
        subprocess.run(["git", "-C", str(self.clone), "commit", "-a", "--amend", "-m", "rewrite"],
                       check=True, capture_output=True)
        r = self._push("--force-with-lease", "origin", "main")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("non-fast-forward", r.stderr)

    def test_branch_delete_is_rejected(self):
        subprocess.run(["git", "-C", str(self.clone), "push", "origin", "main:refs/heads/doomed"],
                       check=True, capture_output=True)
        r = self._push("origin", "--delete", "doomed")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to delete", r.stderr)


class VaultPushGuardInstallTest(unittest.TestCase):
    """A VAULT task (no `repo:` payload) never enters prepare_external_branch, so
    before the fix its worker branch pushed un-guarded — the force-push hard rule
    only reached external clones. execute() must install the guard on the vault's
    own .git on the common path, independent of the repo_url branch."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "vault"
        subprocess.run(["git", "init", "-b", "main", str(self.repo)],
                       check=True, capture_output=True)
        self._orig_vault, self._orig_runs = (
            stages._VAULT_DATA_ROOT, stages.RUNS_DIR)
        stages._VAULT_DATA_ROOT = self.repo
        stages.RUNS_DIR = self.tmp / "runs"
        stages.RUNS_DIR.mkdir(parents=True)

    def tearDown(self):
        stages._VAULT_DATA_ROOT, stages.RUNS_DIR = (
            self._orig_vault, self._orig_runs)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_vault_task(self):
        from todo_system.orchestrator import message as m
        real_run = subprocess.run
        real_argv = stages.build_stage_argv

        def spy_argv(stage, **kw):
            spy_argv.pending = (kw["run_dir"], stage)
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            # Let install_push_guard's real `git` calls through (it must actually
            # write the hook); only the `claude -p` stage spawns are faked.
            if argv and argv[0] == "git":
                return real_run(argv, **kw)
            rd, stage = spy_argv.pending
            (Path(rd) / stages.STAGE_ARTIFACT[stage]).write_text(
                "overall_status: pass\nmerged: yes\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        # payload WITHOUT `repo:` — the vault path. install_push_guard is NOT
        # mocked, so it writes into the temp .git for real.
        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload={"worker_mode": "auto"})
        ex = StagedExecutor()
        with mock.patch.object(stages, "restore_main"), \
             mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            out = ex.execute(msg, mock.Mock(), lambda *_: None)
        self.assertTrue(out.ok, out.summary)

    def test_vault_run_installs_the_push_guard_hook(self):
        self._run_vault_task()
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self.assertTrue(hook.exists(), "vault path left its .git un-guarded")
        self.assertTrue(hook.stat().st_mode & 0o111)   # executable

    def test_vault_run_never_enters_external_branch_prep(self):
        """AC#5 for the orphaned-commit surfacing: a vault task (no `repo:`) must
        stay off the `prepare_external_branch` path entirely, so neither the
        detection nor its `progress` kwarg can change what a vault run does. Spying
        rather than asserting on effects — a call with zero observable effect here
        would still be a behaviour change on the wrong path."""
        with mock.patch.object(stages, "prepare_external_branch") as spy:
            self._run_vault_task()
        spy.assert_not_called()
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self.assertTrue(hook.exists(), "vault path left its .git un-guarded")


class VaultBranchPrepTest(unittest.TestCase):
    """A VAULT run must end up on `worker/<slug>` because the RUNNER put it there.

    Branch prep lived entirely inside `_execute`'s `if repo_url:` with no `else:`,
    so a vault task computed `branch` and then never called git with it. The only
    thing that ever created the branch was a line in worker-implement.md's prompt,
    which silently did not run on 2026-08-24 (2/2) and left the stages committing
    onto main. Test numbers below are design §6.2's.

    Unlike VaultPushGuardInstallTest's fixture this vault has a real commit AND a
    real bare origin, because base inheritance (T4) cannot be observed without one.
    `restore_main` is deliberately NOT mocked: the assertions are on the branch
    REF, which has to survive the unconditional restore in `_execute`'s finally.
    """

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v],
                           check=True, capture_output=True)

    def _commit(self, r, msg):
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(r), "commit", "-m", msg],
                       check=True, capture_output=True)

    def _git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.vault), *args],
                              check=check, capture_output=True, text=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.vault = self.tmp / "vault"
        subprocess.run(["git", "clone", str(self.origin), str(self.vault)],
                       check=True, capture_output=True)
        self._cfg(self.vault)
        (self.vault / "main.txt").write_text("v1")
        self._commit(self.vault, "init")
        self._git("push", "origin", "main")
        self._orig = (stages._VAULT_DATA_ROOT, stages.RUNS_DIR)
        stages._VAULT_DATA_ROOT = self.vault
        stages.RUNS_DIR = self.tmp / "runs"
        stages.RUNS_DIR.mkdir(parents=True)

    def tearDown(self):
        stages._VAULT_DATA_ROOT, stages.RUNS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _has_ref(self, branch="worker/t"):
        return self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                         check=False).returncode == 0

    def _run(self, payload=None):
        from todo_system.orchestrator import message as m
        real_run = subprocess.run
        real_argv = stages.build_stage_argv

        def spy_argv(stage, **kw):
            spy_argv.pending = (kw["run_dir"], stage)
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            # Every real `git` goes through — branch prep IS what is under test.
            # Only the `claude -p` stage spawns are faked.
            if argv and argv[0] == "git":
                return real_run(argv, **kw)
            rd, stage = spy_argv.pending
            (Path(rd) / stages.STAGE_ARTIFACT[stage]).write_text(
                "overall_status: pass\nmerged: yes\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload=payload or {"worker_mode": "auto"})
        with mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            return StagedExecutor().execute(msg, mock.Mock(), lambda *_: None)

    def test_vault_run_creates_the_worker_branch(self):
        """§6.2-1. The whole point: after a vault run the ref exists."""
        out = self._run()
        self.assertTrue(out.ok, out.summary)
        self.assertTrue(self._has_ref(),
                        "vault run left no refs/heads/worker/t — every stage would "
                        "have committed onto main")

    def test_branch_creation_failure_fails_the_run_instead_of_using_main(self):
        """§6.2-2. The silent-main-progress case is the bug; it must be loud."""
        dead = subprocess.CompletedProcess(["git"], 1, "", "fatal: cannot lock ref")
        with mock.patch.object(stages, "git_checkout", return_value=dead):
            out = self._run()
        self.assertFalse(out.ok, "a failed branch creation let the run proceed")
        self.assertIn("worker/t", out.summary)
        self.assertIn("cannot lock ref", out.summary)
        self.assertFalse(self._has_ref())

    def test_post_condition_catches_a_checkout_that_lies_about_success(self):
        """§6.2-2, second half. rc==0 is not the property the caller needs — the
        run must fail when the branch is not actually there, however that happened.
        A no-op `git_checkout` returning 0 is the cheapest stand-in for it."""
        liar = subprocess.CompletedProcess(["git"], 0, "Switched to branch", "")
        with mock.patch.object(stages, "git_checkout", return_value=liar):
            out = self._run()
        self.assertFalse(out.ok, "checkout claimed success, nothing verified it")
        self.assertIn("worker/t", out.summary)
        self.assertFalse(self._has_ref())

    def test_retry_continues_the_pushed_branch_instead_of_re_cutting(self):
        """§6.2-4, the push-deadlock regression. A prior attempt pushed
        origin/worker/t; re-cutting from HEAD builds a lineage that can never
        fast-forward onto it, and force-push is forbidden — so the task would
        deadlock on every retry forever."""
        self._git("checkout", "-b", "worker/t")
        (self.vault / "prior.txt").write_text("prior attempt work")
        self._commit(self.vault, "prior attempt commit")
        prior = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("push", "origin", "worker/t")
        # Leave the branch ONLY on origin — no local branch, no remote-tracking
        # ref. That makes the `fetch` inside prepare_vault_branch load-bearing:
        # without it `checkout -B worker/t origin/worker/t` has nothing to resolve.
        self._git("checkout", "main")
        self._git("branch", "-D", "worker/t")
        self._git("update-ref", "-d", "refs/remotes/origin/worker/t")
        self.assertFalse(self._has_ref())

        out = self._run()

        self.assertTrue(out.ok, out.summary)
        self.assertTrue(self._has_ref())
        contained = self._git("merge-base", "--is-ancestor", prior, "worker/t",
                              check=False).returncode
        self.assertEqual(contained, 0,
                         "vault retry re-cut worker/t from HEAD instead of "
                         "continuing origin/worker/t — the next push deadlocks")

    def test_external_run_never_enters_vault_branch_prep(self):
        """§6.2-7. The mirror of test_vault_run_never_enters_external_branch_prep:
        the external path must be untouched by this change. Spying rather than
        asserting on effects — a call with no observable effect here would still
        be a behaviour change on the wrong path."""
        from todo_system.orchestrator import message as m
        from todo_system.orchestrator import repos
        real_argv = stages.build_stage_argv

        def spy_argv(stage, **kw):
            spy_argv.pending = (kw["run_dir"], stage)
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            rd, stage = spy_argv.pending
            (Path(rd) / stages.STAGE_ARTIFACT[stage]).write_text(
                "overall_status: pass\nmerged: yes\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload={"repo": "https://h/o/r.git", "worker_mode": "auto"})
        with mock.patch.object(repos, "ensure_worktree", return_value=Path("/tmp/r")), \
             mock.patch.object(stages, "restore_main"), \
             mock.patch.object(stages, "install_push_guard"), \
             mock.patch.object(stages, "prepare_external_branch",
                               return_value=subprocess.CompletedProcess([], 0, "", "")), \
             mock.patch.object(stages, "prepare_vault_branch") as spy, \
             mock.patch.object(stages, "merge_landed", return_value=True), \
             mock.patch.object(stages, "remove_worktree"), \
             mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            out = StagedExecutor().execute(msg, mock.Mock(), lambda *_: None)
        spy.assert_not_called()
        self.assertTrue(out.ok, out.summary)

    def test_live_sibling_hold_still_precedes_branch_creation(self):
        """§6.2-8. The hold's early return is ahead of branch prep, so a deferred
        run must leave no trace — creating the branch first would be a write into
        a working tree the guard just declared off limits."""
        with mock.patch.object(StagedExecutor, "_live_sibling_hold",
                               return_value=("deferred — held by a LIVE sibling",
                                             "other-slug")):
            out = self._run()
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("checkout_busy"))
        self.assertFalse(self._has_ref(),
                         "hold fired but branch prep ran anyway")


class GhMergeQueryTest(unittest.TestCase):
    """#4 — merge_landed must ask GitHub for the REAL merge state (gh pr view),
    trusting the artifact regex only when gh can't answer.

    Regression: reading the LLM-written `merged:` line alone parked already-merged
    PRs as awaiting_human (5e-1 #11, 5e-3 #12: 'gate-bypass false-negative')."""

    def _cp(self, stdout, rc=0):
        return subprocess.CompletedProcess(args=["gh"], returncode=rc,
                                           stdout=stdout, stderr="")

    def test_gh_reports_merged(self):
        with mock.patch.object(stages.subprocess, "run", return_value=self._cp(
                '{"state":"MERGED","mergedAt":"2026-07-29T00:00:00Z"}')):
            self.assertIs(stages.gh_pr_merged("worker/x", "o/r"), True)

    def test_gh_reports_open_as_false(self):
        with mock.patch.object(stages.subprocess, "run",
                               return_value=self._cp('{"state":"OPEN","mergedAt":null}')):
            self.assertIs(stages.gh_pr_merged("worker/x"), False)

    def test_gh_absent_is_unknown(self):
        with mock.patch.object(stages.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(stages.gh_pr_merged("worker/x"))

    def test_gh_nonzero_rc_is_unknown(self):
        # no PR for the branch / not authed → non-zero exit → unknown (NOT False,
        # which would falsely report 'not landed').
        with mock.patch.object(stages.subprocess, "run", return_value=self._cp("", rc=1)):
            self.assertIsNone(stages.gh_pr_merged("worker/x"))

    def test_gh_bad_json_is_unknown(self):
        with mock.patch.object(stages.subprocess, "run", return_value=self._cp("not json")):
            self.assertIsNone(stages.gh_pr_merged("worker/x"))

    def test_repo_flag_passed_through(self):
        with mock.patch.object(stages.subprocess, "run",
                               return_value=self._cp('{"state":"MERGED"}')) as run:
            stages.gh_pr_merged("worker/x", "owner/repo")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "pr", "view", "worker/x"])
        self.assertIn("--repo", argv)
        self.assertIn("owner/repo", argv)

    def test_merge_landed_prefers_gh_over_artifact(self):
        d = Path(tempfile.mkdtemp())
        try:
            # gh says merged → overrides an artifact that claims 'no'
            (d / stages.STAGE_ARTIFACT["report"]).write_text("merged: no\n", encoding="utf-8")
            with mock.patch.object(stages, "gh_pr_merged", return_value=True):
                self.assertIs(stages.merge_landed(d, "report",
                                                  branch="worker/x", repo="o/r"), True)
            # gh says open → overrides an artifact that claims 'yes'
            (d / stages.STAGE_ARTIFACT["report"]).write_text("merged: yes\n", encoding="utf-8")
            with mock.patch.object(stages, "gh_pr_merged", return_value=False):
                self.assertIs(stages.merge_landed(d, "report",
                                                  branch="worker/x", repo="o/r"), False)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_merge_landed_falls_back_to_artifact_when_gh_unknown(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / stages.STAGE_ARTIFACT["report"]).write_text("merged: yes\n", encoding="utf-8")
            with mock.patch.object(stages, "gh_pr_merged", return_value=None):
                self.assertIs(stages.merge_landed(d, "report",
                                                  branch="worker/x", repo="o/r"), True)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_merge_landed_no_branch_skips_gh(self):
        # backward-compat: no branch → never calls gh, reads the artifact directly
        # (the existing pipeline call sites without a branch keep working).
        d = Path(tempfile.mkdtemp())
        try:
            (d / stages.STAGE_ARTIFACT["report"]).write_text("merged: yes\n", encoding="utf-8")
            with mock.patch.object(stages, "gh_pr_merged") as gh:
                self.assertIs(stages.merge_landed(d, "report"), True)
            gh.assert_not_called()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class GhPrMergedAtTest(unittest.TestCase):
    """`gh_pr_merged_at` answers WHEN, on the same None-means-unknown contract.

    Its own TestCase rather than an addition to GhMergeQueryTest above: those
    tests are the no-regression evidence that `gh_pr_merged` was left alone, so
    they must not gain or lose a fixture here.
    """

    def _cp(self, stdout, rc=0):
        return subprocess.CompletedProcess(args=["gh"], returncode=rc,
                                           stdout=stdout, stderr="")

    def test_merged_pr_yields_the_timestamp(self):
        with mock.patch.object(stages.subprocess, "run", return_value=self._cp(
                '{"mergedAt":"2026-08-23T05:00:00Z"}')):
            self.assertEqual(stages.gh_pr_merged_at("worker/x"),
                             "2026-08-23T05:00:00Z")

    def test_open_pr_is_unknown_not_empty(self):
        # `mergedAt: null` is an OPEN pr — unknown-when, and the caller must not
        # read it as "merged at the epoch".
        with mock.patch.object(stages.subprocess, "run",
                               return_value=self._cp('{"mergedAt":null}')):
            self.assertIsNone(stages.gh_pr_merged_at("worker/x"))

    def test_no_pr_or_not_authed_is_unknown(self):
        with mock.patch.object(stages.subprocess, "run", return_value=self._cp("", rc=1)):
            self.assertIsNone(stages.gh_pr_merged_at("worker/x"))

    def test_gh_absent_is_unknown(self):
        with mock.patch.object(stages.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(stages.gh_pr_merged_at("worker/x"))

    def test_bad_json_is_unknown(self):
        with mock.patch.object(stages.subprocess, "run", return_value=self._cp("not json")):
            self.assertIsNone(stages.gh_pr_merged_at("worker/x"))
        with mock.patch.object(stages.subprocess, "run", return_value=self._cp("[1, 2]")):
            self.assertIsNone(stages.gh_pr_merged_at("worker/x"))

    def test_cwd_and_repo_reach_gh(self):
        """`gh pr view` resolves the repo from the working directory, so a caller
        holding an external clone MUST be able to aim it there."""
        with mock.patch.object(stages.subprocess, "run",
                               return_value=self._cp('{"mergedAt":"2026-01-01T00:00:00Z"}')) as run:
            stages.gh_pr_merged_at("worker/x", "owner/repo", cwd="/tmp/clone")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "pr", "view", "worker/x"])
        self.assertIn("--repo", argv)
        self.assertIn("owner/repo", argv)
        self.assertEqual(run.call_args.kwargs["cwd"], "/tmp/clone")

    def test_gh_timestamps_are_comparable_as_epochs(self):
        """The suppression predicate compares gh's `Z` string against git's
        `%cI` numeric offset, so both must survive _iso_to_epoch."""
        z = stages._iso_to_epoch("2026-08-23T05:00:00Z")
        offset = stages._iso_to_epoch("2026-08-23T14:00:00+09:00")
        self.assertIsNotNone(z)
        self.assertEqual(z, offset)          # same instant, two spellings
        self.assertIsNone(stages._iso_to_epoch(None))
        self.assertIsNone(stages._iso_to_epoch("not-a-date"))


if __name__ == "__main__":
    unittest.main()


class ReleaseWorktreeTest(unittest.TestCase):
    """A leftover worktree owns the branch and kills the next run before it starts.

    Agents create worktrees to keep work off the shared ~/Worker checkout — sound,
    since a live process can be running from it — but do not always clean up.
    ai-stock-trading-5n failed twice this way and sat one refail from quarantine.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.repo / "f").write_text("x", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "seed")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *a):
        return subprocess.run(["git", "-C", str(self.repo), *a],
                              capture_output=True, text=True)

    def test_releases_a_leftover_worktree_and_unblocks_checkout(self):
        wt = self.tmp / "leftover"
        self._git("worktree", "add", "-b", "worker/t", str(wt))
        # Reproduce the real failure first — a branch held by another worktree is
        # locked. Assert it with a plain `checkout`, whose lock ("already used by
        # worktree") is uniform across git versions; `checkout -B` is NOT — git
        # 2.43 lets -B *reset* the held branch (rc 0) instead of refusing. The
        # orchestrator still uses -B, so release_worktree_holding matters on every
        # version; the `released == wt` assertion below is what proves the removal.
        blocked = self._git("checkout", "worker/t")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("already used by worktree", blocked.stderr)

        released = stages.release_worktree_holding(self.repo, "worker/t")

        self.assertEqual(Path(released).resolve(), wt.resolve())
        self.assertEqual(self._git("checkout", "-B", "worker/t").returncode, 0)

    def test_commits_survive_the_release(self):
        wt = self.tmp / "leftover"
        self._git("worktree", "add", "-b", "worker/t", str(wt))
        subprocess.run(["git", "-C", str(wt), "commit", "-q", "--allow-empty",
                        "-m", "work"], capture_output=True)
        sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        stages.release_worktree_holding(self.repo, "worker/t")
        # the branch outlives the worktree — this is why removal loses nothing
        self.assertEqual(
            self._git("rev-parse", "worker/t").stdout.strip(), sha)

    def test_no_worktree_is_a_noop(self):
        self.assertIsNone(stages.release_worktree_holding(self.repo, "worker/t"))

    def test_never_removes_the_main_checkout(self):
        # the repo has main checked out; releasing "main" must not touch it
        self.assertIsNone(stages.release_worktree_holding(self.repo, "main"))
        self.assertTrue((self.repo / ".git").exists())


class ReportVerdictTest(unittest.TestCase):
    """The last stage's judgement of its own run was never read."""

    def test_parse_report_pr_url(self):
        self.assertEqual(stages.parse_report_pr_url("pr_url: https://x/pull/7"),
                         "https://x/pull/7")

    def test_null_pr_url_is_none(self):
        # gutenberg-reader-3 wrote exactly this while the alert claimed a PR
        for v in ("null", "none", "-", "n/a", ""):
            self.assertIsNone(stages.parse_report_pr_url(f"pr_url: {v}"))
        self.assertIsNone(stages.parse_report_pr_url("no field at all"))


class TaskAllowedToolsTest(unittest.TestCase):
    """A task's allowed_tools was decorative: tasks.py never put it in the
    payload and build_stage_argv only read STAGE_TOOLS. So a capability could
    only be granted to EVERY task or none — which is what made "let workers use
    gcloud" an all-or-nothing choice (2026-08-07)."""

    def test_implement_honours_the_task_list(self):
        argv = stages.build_stage_argv(
            "implement", task_id="t", run_id="r", run_dir=Path("/x"),
            branch="b", allowed_tools="Read,Write,Bash(gcloud:*)")
        self.assertIn("Bash(gcloud:*)", argv)
        self.assertNotIn("Bash(pytest:*)", argv)   # replaced, not merged

    def test_read_only_stages_ignore_it(self):
        # research/plan being read-only is what lets the pipeline trust their
        # artifacts; a task must not be able to dissolve that
        for stage in ("research", "plan", "verify"):
            argv = stages.build_stage_argv(
                stage, task_id="t", run_id="r", run_dir=Path("/x"),
                branch="b", allowed_tools="Bash(gcloud:*)")
            self.assertNotIn("Bash(gcloud:*)", argv, f"{stage} widened")

    def test_absent_list_keeps_the_stage_default(self):
        argv = stages.build_stage_argv("implement", task_id="t", run_id="r",
                                       run_dir=Path("/x"), branch="b")
        self.assertIn("Bash(git:*)", argv)

    def test_forbidden_tools_are_still_refused_from_a_task(self):
        # the guard is what makes honouring the task list safe at all
        for bad in ("Read,Bash(curl:*)", "Read,Bash(rm:*)", "Read,Bash(sudo:*)"):
            with self.assertRaises(ValueError):
                stages.build_stage_argv("implement", task_id="t", run_id="r",
                                        run_dir=Path("/x"), branch="b",
                                        allowed_tools=bad)


_ORPHANED = {"branch": "worker/t", "base": "origin/main",
             "tip": "deadbeef1234567890abcdef01234567890abcde", "count": 3,
             "shas": ["aaa1111222233334444555566667777888899990",
                      "bbb1111222233334444555566667777888899991",
                      "deadbeef1234567890abcdef01234567890abcde"]}


class OrphanedCommitRecordTest(unittest.TestCase):
    """stages.json is the run's structured record, and the only structured file
    the orchestrator path writes. Carrying the orphaned-commit report there means
    record_stage may no longer overwrite the whole file: it used to serialize
    `{"completed": [...]}`, so any sibling key died at the very next stage.

    The shas are the point. A human recovers with `git cherry-pick <sha>` long
    after the run's console output has scrolled away, so the record has to keep
    them verbatim."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run = self.tmp / "20260818T220603-t"
        self.run.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self) -> dict:
        return json.loads((self.run / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))

    def test_record_stage_preserves_sibling_keys(self):
        (self.run / stages.STAGE_PROGRESS).write_text(
            json.dumps({"completed": ["research"], "orphaned_commits": _ORPHANED}),
            encoding="utf-8")

        stages.record_stage(self.run, "plan")

        got = self._read()
        self.assertEqual(got["completed"], ["research", "plan"])
        self.assertEqual(got.get("orphaned_commits"), _ORPHANED,
                         "recording a stage erased the run's other structured state")

    def test_record_orphaned_commits_round_trip(self):
        stages.record_orphaned_commits(self.run, _ORPHANED)
        got = self._read()["orphaned_commits"]
        self.assertEqual(got["count"], 3)
        self.assertEqual(got["tip"], _ORPHANED["tip"])
        self.assertEqual(got["shas"], _ORPHANED["shas"])   # verbatim = cherry-pickable

    def test_the_report_outlives_the_rest_of_the_pipeline(self):
        """Detection happens before the first stage runs; the record has to still
        be there when a human reads it after stage five."""
        stages.record_orphaned_commits(self.run, _ORPHANED)
        for s in ("research", "plan", "implement", "verify", "report"):
            stages.record_stage(self.run, s)

        self.assertEqual(self._read()["orphaned_commits"]["tip"], _ORPHANED["tip"])
        self.assertEqual(stages.completed_stages(self.run),
                         ["research", "plan", "implement", "verify", "report"])

    def test_recording_is_best_effort_on_a_corrupt_file(self):
        """Same contract record_stage already had: a damaged progress file costs a
        redundant re-run, never a dead run."""
        (self.run / stages.STAGE_PROGRESS).write_text("{not json", encoding="utf-8")
        stages.record_orphaned_commits(self.run, _ORPHANED)      # must not raise
        stages.record_stage(self.run, "plan")                    # must not raise
        stages.record_orphaned_commits(self.tmp / "nope", _ORPHANED)   # nor a missing dir

    def test_no_report_writes_no_key(self):
        """A clean run stays clean — nothing to read, nothing to alarm about."""
        stages.record_stage(self.run, "research")
        self.assertNotIn("orphaned_commits", self._read())


class VaultDelegatePublishTest(unittest.TestCase):
    """`publish_branch` — the unit half of "a delegate run's commits reach origin".

    A real bare origin and a real clone, because the ahead-count is a git question
    and a faked git would fake the very answer under test. Only `gh` is stubbed
    (recorded into `self.gh` so the tests can assert on what was NOT called —
    `pr merge` above all).
    """

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v],
                           check=True, capture_output=True)

    def _git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.vault), *args],
                              check=check, capture_output=True, text=True)

    def _commit(self, name, msg):
        (self.vault / name).write_text(msg)
        self._git("add", "-A")
        self._git("commit", "-m", msg)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.vault = self.tmp / "vault"
        subprocess.run(["git", "clone", str(self.origin), str(self.vault)],
                       check=True, capture_output=True)
        self._cfg(self.vault)
        self._commit("main.txt", "init")
        self._git("push", "origin", "main")
        self.gh: list[list[str]] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _branch_with_commit(self, branch="worker/t", n=1):
        self._git("checkout", "-b", branch)
        for i in range(n):
            self._commit(f"work{i}.txt", f"delegate work {i}")
        self._git("checkout", "main")

    def _cp(self, argv, stdout="", rc=0):
        return subprocess.CompletedProcess(args=argv, returncode=rc,
                                           stdout=stdout, stderr="")

    def _patched(self, *, gh_view=None, gh_create=None, push_rc=0, gh_raises=None):
        """Let every real `git` through; stub `gh` (and optionally fail `push`)."""
        real_run = subprocess.run

        def fake(argv, **kw):
            argv = list(argv)
            if argv and argv[0] == "git":
                if push_rc and argv[3:4] == ["push"]:
                    return self._cp(argv, "", rc=push_rc)
                return real_run(argv, **kw)
            self.gh.append(argv)
            if gh_raises is not None:
                raise gh_raises
            if argv[:3] == ["gh", "pr", "view"]:
                return gh_view if gh_view is not None else self._cp(argv, "", rc=1)
            if argv[:3] == ["gh", "pr", "create"]:
                return gh_create if gh_create is not None else self._cp(
                    argv, "https://github.com/o/r/pull/7\n")
            return self._cp(argv, "", rc=1)

        return mock.patch("subprocess.run", side_effect=fake)

    def _pushed_sha(self, branch="worker/t"):
        r = self._git("ls-remote", "--heads", "origin", branch)
        return r.stdout.split()[0] if r.stdout.strip() else ""

    # -- ahead-count ------------------------------------------------------
    def test_ahead_count_zero_n_and_unknown(self):
        """T6. 0 and -1 must stay distinguishable: one means 'measured, nothing
        to publish', the other 'no measurement' — and publish_branch acts on both."""
        self._git("checkout", "-b", "worker/t")
        self._git("checkout", "main")
        self.assertEqual(stages.branch_commits_ahead(self.vault, "worker/t", "main"), 0)
        self._git("checkout", "worker/t")
        self._commit("a.txt", "one")
        self._commit("b.txt", "two")
        self._git("checkout", "main")
        self.assertEqual(stages.branch_commits_ahead(self.vault, "worker/t", "main"), 2)
        self.assertEqual(stages.branch_commits_ahead(self.vault, "worker/nope", "main"), -1,
                         "a ref that does not resolve must be UNKNOWN, not zero")

    def test_ahead_count_ignores_unpushed_commits_on_the_local_default(self):
        """The base is LOCAL `main`, not `origin/main`. The live vault's main
        carries `chore: sync vault state` commits the daemon has not pushed yet;
        counting from origin/main scores those as this branch's work."""
        self._commit("sync.txt", "chore: sync vault state")   # local main, unpushed
        self._branch_with_commit()                 # worker/t cut from it, 1 own commit
        self.assertEqual(stages.branch_commits_ahead(self.vault, "worker/t", "main"), 1)
        self.assertEqual(stages.branch_commits_ahead(self.vault, "worker/t", "origin/main"), 2,
                         "fixture is wrong if origin/main does not over-count")

    # -- no-op paths ------------------------------------------------------
    def test_zero_commits_publishes_nothing_and_never_calls_gh(self):
        """The no-op half of the outcome: a delegate run that changed nothing must
        leave no empty remote branch (a stale-green trap) and open no PR."""
        self._git("checkout", "-b", "worker/t")
        self._git("checkout", "main")
        with self._patched():
            out = stages.publish_branch(self.vault, "worker/t", base="main")
        self.assertEqual(out["commits"], 0)
        self.assertFalse(out["pushed"])
        self.assertIsNone(out["pr_url"])
        self.assertEqual(self.gh, [], "gh was called for a branch with no commits")
        self.assertEqual(self._pushed_sha(), "", "an empty branch reached origin")

    def test_unknown_count_does_not_push(self):
        """-1 is not 'probably fine'. Nothing measured, nothing pushed."""
        with self._patched():
            out = stages.publish_branch(self.vault, "worker/nope", base="main")
        self.assertEqual(out["commits"], -1)
        self.assertFalse(out["pushed"])
        self.assertEqual(self.gh, [])
        self.assertIn("unknown", out["note"])

    # -- publish ----------------------------------------------------------
    def test_commits_are_pushed_and_a_pr_is_opened(self):
        self._branch_with_commit()
        with self._patched():
            out = stages.publish_branch(self.vault, "worker/t", base="main",
                                        body="delegate run")
        self.assertTrue(out["pushed"], out["note"])
        self.assertEqual(out["pr_url"], "https://github.com/o/r/pull/7")
        self.assertEqual(self._pushed_sha(),
                         self._git("rev-parse", "worker/t").stdout.strip())
        create = [a for a in self.gh if a[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(create), 1)
        self.assertIn("--base", create[0])
        self.assertEqual(create[0][create[0].index("--base") + 1], "main")
        self.assertEqual(create[0][create[0].index("--head") + 1], "worker/t")
        self.assertEqual(create[0][create[0].index("--title") + 1], "delegate work 0",
                         "PR title should be the branch tip's subject")

    def test_never_merges(self):
        """Hard safety. The vault's main is shared with a live daemon that commits
        to it on a timer; an auto-merge from a worker run races it. An OPEN PR is
        the whole deliverable."""
        self._branch_with_commit()
        before = self._pushed_sha("main")
        with self._patched():
            stages.publish_branch(self.vault, "worker/t", base="main")
        self.assertEqual([a for a in self.gh if "merge" in a], [])
        self.assertEqual(self._pushed_sha("main"), before, "origin/main moved")

    def test_never_force_pushes(self):
        pushes = []
        self._branch_with_commit()
        real_run = subprocess.run

        def fake(argv, **kw):
            argv = list(argv)
            if argv and argv[0] == "git":
                if argv[3:4] == ["push"]:
                    pushes.append(argv)
                return real_run(argv, **kw)
            return self._cp(argv, "", rc=1)

        with mock.patch("subprocess.run", side_effect=fake):
            stages.publish_branch(self.vault, "worker/t", base="main")
        self.assertEqual(len(pushes), 1)
        for flag in ("-f", "--force", "--force-with-lease", "--no-verify"):
            self.assertNotIn(flag, pushes[0])

    def test_an_open_pr_is_reused_instead_of_duplicated(self):
        """T7. A retry pushes onto the SAME branch the open PR already tracks —
        calling `gh pr create` again is what produces duplicate PRs for one task."""
        self._branch_with_commit()
        view = self._cp(["gh"], '{"url":"https://github.com/o/r/pull/3","state":"OPEN"}')
        with self._patched(gh_view=view):
            out = stages.publish_branch(self.vault, "worker/t", base="main")
        self.assertEqual(out["pr_url"], "https://github.com/o/r/pull/3")
        self.assertEqual([a for a in self.gh if a[:3] == ["gh", "pr", "create"]], [],
                         "a second PR was opened for a branch that already had one")

    def test_a_closed_pr_does_not_count_as_open(self):
        """The outcome says a PR is OPEN. A merged/closed PR from an earlier
        attempt does not cover the commits just pushed."""
        self._branch_with_commit()
        view = self._cp(["gh"], '{"url":"https://github.com/o/r/pull/2","state":"CLOSED"}')
        with self._patched(gh_view=view):
            out = stages.publish_branch(self.vault, "worker/t", base="main")
        self.assertEqual(out["pr_url"], "https://github.com/o/r/pull/7")
        self.assertEqual(len([a for a in self.gh if a[:3] == ["gh", "pr", "create"]]), 1)

    # -- fail-open --------------------------------------------------------
    def test_push_failure_stops_before_gh(self):
        """T8. No push, no PR — a PR whose branch is not on origin is worse than
        none (it renders as an empty diff and reads as 'nothing to do')."""
        self._branch_with_commit()
        with self._patched(push_rc=1):
            out = stages.publish_branch(self.vault, "worker/t", base="main")
        self.assertFalse(out["pushed"])
        self.assertIsNone(out["pr_url"])
        self.assertEqual(self.gh, [])
        self.assertIn("rejected", out["note"])

    def test_gh_absent_is_not_an_exception(self):
        """T9. `gh` missing/unauthenticated is a normal, silent outcome: the push
        still counts and the run must not die on the courtesy step."""
        self._branch_with_commit()
        with self._patched(gh_raises=FileNotFoundError("gh")):
            out = stages.publish_branch(self.vault, "worker/t", base="main")
        self.assertTrue(out["pushed"], "a missing gh must not undo the push")
        self.assertIsNone(out["pr_url"])
        self.assertEqual(self._pushed_sha(),
                         self._git("rev-parse", "worker/t").stdout.strip())

    def test_gh_create_failure_is_reported_not_raised(self):
        self._branch_with_commit()
        with self._patched(gh_create=self._cp(["gh"], "", rc=1)):
            out = stages.publish_branch(self.vault, "worker/t", base="main")
        self.assertTrue(out["pushed"])
        self.assertIsNone(out["pr_url"])
        self.assertIn("gh pr create failed", out["note"])


class VaultDelegateBranchPublishTest(unittest.TestCase):
    """The stranding itself, end to end: a `worker_mode: delegate` VAULT run that
    commits must not finish with its work only on a local branch.

    Delegate is a single stage with no report stage behind it, so nothing in the
    pipeline ever pushed — and `restore_main` in `_execute`'s `finally` then walks
    HEAD back to main and abandons the branch. Three runs' output was lost that
    way. The fixture is `VaultBranchPrepTest`'s (real bare origin + real clone,
    every real `git` allowed through) because "did it reach origin" cannot be
    asserted against a faked git; only `claude` and `gh` are stubbed.
    """

    def _cfg(self, r):
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(r), "config", k, v],
                           check=True, capture_output=True)

    def _git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.vault), *args],
                              check=check, capture_output=True, text=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.vault = self.tmp / "vault"
        subprocess.run(["git", "clone", str(self.origin), str(self.vault)],
                       check=True, capture_output=True)
        self._cfg(self.vault)
        (self.vault / "main.txt").write_text("v1")
        self._git("add", "-A")
        self._git("commit", "-m", "init")
        self._git("push", "origin", "main")
        self._orig = (stages._VAULT_DATA_ROOT, stages.RUNS_DIR)
        stages._VAULT_DATA_ROOT = self.vault
        # OUTSIDE the vault: the fake stage does `git add -A`, and run artifacts
        # under the working tree would ride along into the commit under test.
        stages.RUNS_DIR = self.tmp / "runs"
        stages.RUNS_DIR.mkdir(parents=True)
        self.gh: list[list[str]] = []

    def tearDown(self):
        stages._VAULT_DATA_ROOT, stages.RUNS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _remote_sha(self, branch):
        r = self._git("ls-remote", "--heads", "origin", branch)
        return r.stdout.split()[0] if r.stdout.strip() else ""

    def _run(self, payload=None, commits=1):
        """One pipeline run whose fake stages each leave `commits` real commits."""
        from todo_system.orchestrator import message as m
        real_run = subprocess.run
        real_argv = stages.build_stage_argv

        def spy_argv(stage, **kw):
            spy_argv.pending = (kw["run_dir"], stage)
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            argv = list(argv)
            if argv and argv[0] == "git":
                return real_run(argv, **kw)     # branch prep + the push are the SUT
            if argv and argv[0] == "gh":
                self.gh.append(argv)
                if argv[:3] == ["gh", "pr", "create"]:
                    return subprocess.CompletedProcess(
                        argv, 0, "https://github.com/o/r/pull/9\n", "")
                return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
            rd, stage = spy_argv.pending
            (Path(rd) / stages.STAGE_ARTIFACT[stage]).write_text(
                "overall_status: pass\nmerged: no\n", encoding="utf-8")
            for i in range(commits):
                (self.vault / f"{stage}-{i}.md").write_text(f"{stage} output {i}")
                real_run(["git", "-C", str(self.vault), "add", "-A"],
                         check=True, capture_output=True)
                real_run(["git", "-C", str(self.vault), "commit", "-m",
                          f"{stage} did work {i}"], check=True, capture_output=True)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload=payload or {"worker_mode": "delegate"})
        with mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            return StagedExecutor().execute(msg, mock.Mock(), lambda *_: None)

    def test_a_delegate_runs_commits_reach_origin_and_a_pr(self):
        """T1 — the stranding regression. Before the fix `ls-remote` is empty and
        no `gh` ran at all: the branch existed only on this disk."""
        out = self._run()
        self.assertTrue(out.ok, out.summary)
        tip = self._git("rev-parse", "worker/t").stdout.strip()
        self.assertEqual(self._remote_sha("worker/t"), tip,
                         "delegate run's commits never reached origin — stranded on "
                         "a local branch that restore_main then walked away from")
        create = [a for a in self.gh if a[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(create), 1, f"expected one PR, gh calls: {self.gh}")
        self.assertEqual(create[0][create[0].index("--base") + 1], "main")
        self.assertEqual(create[0][create[0].index("--head") + 1], "worker/t")
        self.assertEqual(out.payload.get("pr_url"), "https://github.com/o/r/pull/9",
                         "the PR URL never reached the result envelope")

    def test_a_delegate_run_with_no_commits_publishes_nothing(self):
        """T2 — no-op. An empty remote branch is worse than none: it reads as a
        finished hand-off and renders as an empty diff (the stale-green trap)."""
        out = self._run(commits=0)
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(self._remote_sha("worker/t"), "",
                         "a branch with no commits was pushed")
        self.assertEqual(self.gh, [], "gh was called for a run that changed nothing")

    def test_delegate_merges_server_side_only_never_local_main(self):
        """T3 — hard safety. An auto_merge:true delegate (the default) now DOES get
        merged, but ONLY server-side via `gh pr merge --squash` — never by moving
        the LOCAL main the live todo-sync daemon shares. (Behaviour change:
        macp-delegate-unmerged-pr-closes-and-unblocks, 2026-10-13 — before this,
        the delegate lane ignored auto_merge and closed `done` without any merge.)

        The genuine invariant T3 always protected survives unchanged: this path
        must not push/merge into the vault's own `main`. The `gh pr merge` here is
        faked (rc=1), so origin/main cannot move anyway — what is asserted is that
        the attempt is the squash form and that the LOCAL main is untouched."""
        before = self._remote_sha("main")
        self._run()
        merges = [a for a in self.gh if a[:3] == ["gh", "pr", "merge"]]
        self.assertTrue(merges, "auto_merge:true delegate did not attempt a merge")
        for mc in merges:
            self.assertIn("--squash", mc)        # squash only, like the report lane
            self.assertNotIn("--admin", mc)
            self.assertNotIn("--auto", mc)
        # The only thing banned is moving the LOCAL main: no `git push origin main`,
        # no local merge. origin/main is unchanged and the work is still its own
        # commit on worker/t, left for the server-side PR merge to land.
        self.assertEqual(self._remote_sha("main"), before, "origin/main moved")
        self.assertEqual(
            self._git("rev-list", "--count", "main..worker/t").stdout.strip(), "1",
            "delegate work was merged into the LOCAL main instead of via the server-side PR")

    def test_publish_failure_does_not_fail_the_run(self):
        """T4 — fail-open. Publishing is a courtesy at the very end; a gh/network
        hiccup must never turn an otherwise finished run into a failure."""
        with mock.patch.object(stages, "publish_branch",
                               side_effect=RuntimeError("boom")):
            out = self._run()
        self.assertTrue(out.ok, "an exception in the publish step failed the run")

    def test_a_non_delegate_vault_run_is_untouched(self):
        """T5 — scope. `auto`/`quick` vault runs have their own merge semantics
        (report stage, landed gate); this path must not reach into them."""
        with mock.patch.object(stages, "publish_branch") as spy:
            out = self._run(payload={"worker_mode": "auto"})
        self.assertTrue(out.ok, out.summary)
        spy.assert_not_called()

    def test_an_external_repo_delegate_run_is_untouched(self):
        """T5b — the other half of the scope gate. An external delegate task's
        branch lives in the ~/Worker clone and has its own push path; publishing
        the VAULT's branch for it would push whatever the vault happened to hold."""
        from todo_system.orchestrator import message as m
        from todo_system.orchestrator import repos
        real_argv = stages.build_stage_argv

        def spy_argv(stage, **kw):
            spy_argv.pending = (kw["run_dir"], stage)
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            rd, stage = spy_argv.pending
            (Path(rd) / stages.STAGE_ARTIFACT[stage]).write_text(
                "overall_status: pass\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload={"repo": "https://h/o/r.git", "worker_mode": "delegate"})
        with mock.patch.object(repos, "ensure_worktree", return_value=Path("/tmp/r")), \
             mock.patch.object(stages, "restore_main"), \
             mock.patch.object(stages, "install_push_guard"), \
             mock.patch.object(stages, "prepare_external_branch",
                               return_value=subprocess.CompletedProcess([], 0, "", "")), \
             mock.patch.object(stages, "publish_branch") as spy, \
             mock.patch.object(stages, "remove_worktree"), \
             mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            out = StagedExecutor().execute(msg, mock.Mock(), lambda *_: None)
        spy.assert_not_called()
        self.assertTrue(out.ok, out.summary)

    def test_a_failed_delegate_stage_still_publishes_its_commits(self):
        """The `finally` placement, stated as a test. The decisive case is the
        60-minute stage timeout: it escapes the `try` entirely, so a stage that
        committed and then timed out strands 100% of its work if publishing hangs
        off the success path. A missing artifact is the cheapest stand-in — same
        control flow, no 60-minute wait."""
        from todo_system.orchestrator import message as m
        real_run = subprocess.run
        real_argv = stages.build_stage_argv

        def spy_argv(stage, **kw):
            spy_argv.pending = (kw["run_dir"], stage)
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            argv = list(argv)
            if argv and argv[0] == "git":
                return real_run(argv, **kw)
            if argv and argv[0] == "gh":
                self.gh.append(argv)
                if argv[:3] == ["gh", "pr", "create"]:
                    return subprocess.CompletedProcess(
                        argv, 0, "https://github.com/o/r/pull/9\n", "")
                return subprocess.CompletedProcess(argv, 1, "", "")
            # commits, then dies WITHOUT writing delegate.md
            (self.vault / "half.md").write_text("half-finished work")
            real_run(["git", "-C", str(self.vault), "add", "-A"],
                     check=True, capture_output=True)
            real_run(["git", "-C", str(self.vault), "commit", "-m", "half work"],
                     check=True, capture_output=True)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload={"worker_mode": "delegate"})
        with mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            out = StagedExecutor().execute(msg, mock.Mock(), lambda *_: None)
        self.assertFalse(out.ok, "a missing artifact must still fail the run")
        self.assertEqual(self._remote_sha("worker/t"),
                         self._git("rev-parse", "worker/t").stdout.strip(),
                         "a failed delegate run stranded the commits it did make")


class CleanupBuildDaemonsTest(unittest.TestCase):
    """`cleanup_build_daemons` stops a clone's Gradle daemons on run exit and is
    fail-open: no gradlew is a no-op, and a subprocess error is swallowed (it runs
    in `_execute`'s finally, where a raise would eat the run's real outcome)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_gradlew_stop_in_the_clone(self):
        (self.tmp / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            seen["cwd"] = kw.get("cwd")
            seen["timeout"] = kw.get("timeout")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch("subprocess.run", side_effect=fake_run):
            res = stages.cleanup_build_daemons(self.tmp)
        self.assertTrue(res["ran"])
        self.assertEqual(seen["argv"], ["./gradlew", "--stop"])
        self.assertEqual(seen["cwd"], str(self.tmp))
        self.assertIsNotNone(seen["timeout"], "must bound the stop so it can't hang the finally")

    def test_no_gradlew_is_a_noop_without_touching_subprocess(self):
        # A vault task (no gradlew) must not even shell out.
        with mock.patch("subprocess.run", side_effect=AssertionError("must not run")) as run:
            res = stages.cleanup_build_daemons(self.tmp)
        self.assertFalse(res["ran"])
        run.assert_not_called()

    def test_subprocess_error_is_swallowed(self):
        (self.tmp / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        progress_lines = []
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="gradlew", timeout=60)):
            # The whole point: no exception escapes, even on failure.
            res = stages.cleanup_build_daemons(self.tmp, progress=progress_lines.append)
        self.assertFalse(res["ran"])
        self.assertTrue(any("build daemons" in ln for ln in progress_lines))

    def test_os_error_is_swallowed(self):
        (self.tmp / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        with mock.patch("subprocess.run", side_effect=OSError("no exec")):
            res = stages.cleanup_build_daemons(self.tmp)   # must not raise
        self.assertFalse(res["ran"])

    def _capture_stop(self) -> dict:
        """Run `cleanup_build_daemons` with a gradlew present and a fake
        subprocess, returning what the `--stop` call was invoked with."""
        (self.tmp / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        seen: dict = {}

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            seen["env"] = kw.get("env")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch("subprocess.run", side_effect=fake_run):
            seen["res"] = stages.cleanup_build_daemons(self.tmp)
        return seen

    def test_stop_targets_a_per_run_registry_base_not_home_gradle(self):
        # AC (a): --stop must resolve a per-run registry base so it reaps only THIS
        # run's daemons. The base rides GRADLE_OPTS on the --stop subprocess env.
        seen = self._capture_stop()
        self.assertTrue(seen["res"]["ran"])
        base = stages.gradle_registry_base(self.tmp)
        self.assertIsNotNone(seen["env"],
                             "--stop ran with env=None → inherits the default ~/.gradle registry")
        gopts = seen["env"].get("GRADLE_OPTS", "")
        self.assertIn(f"-Dorg.gradle.daemon.registry.base={base}", gopts)
        # the base is HOST SCRATCH under Worker/_state/, keyed on the checkout
        # basename — never the home registry, never inside the worktree
        self.assertIn("Worker/_state/gradle-registry", str(base))
        self.assertEqual(base.name, self.tmp.name)
        self.assertNotIn(".gradle/daemon", gopts)
        # GRADLE_USER_HOME is left exactly as inherited (we never redirect ~/.gradle)
        self.assertEqual(seen["env"].get("GRADLE_USER_HOME"),
                         os.environ.get("GRADLE_USER_HOME"))

    def test_stop_base_matches_the_stage_env_base_for_the_same_checkout(self):
        # The whole invariant: the base the stage build ran with (stage_env) and the
        # base --stop resolves are the SAME for one checkout → stop hits that build's
        # daemons exactly.
        seen = self._capture_stop()
        base_opt = f"-Dorg.gradle.daemon.registry.base={stages.gradle_registry_base(self.tmp)}"
        self.assertIn(base_opt, seen["env"]["GRADLE_OPTS"])
        self.assertIn(base_opt, stages.stage_env(repo_dir=self.tmp)["GRADLE_OPTS"])


class StageSubprocessGroupKillTest(unittest.TestCase):
    """`run_stage_subprocess` must take the stage's WHOLE tree down on a timeout.

    2026-09-25: the stage cap fired, `subprocess.run` killed only `claude -p`, and
    the `./gradlew` + Gradle/Kotlin JVM it had spawned stayed in the runner cgroup
    with `memory.current` over `memory.high` — where the next git call wedged for
    18~72 minutes. The control test pins the old behaviour (the grandchild
    survives, so the defect is real); the one after it pins the fix."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.marker = self.tmp / "grandchild.pid"

    def _argv(self, seconds: int = 30) -> list[str]:
        """A stand-in for `claude -p` that spawns a long-lived grandchild.

        The grandchild's fds go to `/dev/null` deliberately. Holding the inherited
        stdout/stderr pipes would make the CONTROL test below block for the full
        `sleep` inside `subprocess.run`'s second, UNBOUNDED `communicate()` —
        which is itself half the defect, but would cost the suite 30s. With the
        redirect the control returns in ~0.5s and still shows the orphan."""
        return ["/bin/sh", "-c",
                f"sleep {seconds} >/dev/null 2>&1 & "
                f"printf %s \"$!\" > '{self.marker}'; "
                f"printf 'stage started\\n'; sleep {seconds}"]

    def _grandchild_pid(self) -> int:
        def written() -> bool:
            try:
                return self.marker.read_text(encoding="utf-8").strip().isdigit()
            except OSError:
                return False

        self.assertTrue(_wait(written, timeout=5.0),
                        "the fake stage never recorded its grandchild's pid")
        return int(self.marker.read_text(encoding="utf-8").strip())

    def _reap(self, pid: int) -> None:
        """Individual SIGKILL. Never a process-wide signal: `os.kill(-1, …)` from
        a pytest teardown once wiped a whole interactive session."""
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    def test_control_plain_subprocess_run_leaves_the_grandchild_alive(self):
        """RED control. This is what the runner did until this leaf, and the test
        below is vacuous unless this one holds."""
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(self._argv(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=0.5)
        pid = self._grandchild_pid()
        self.addCleanup(self._reap, pid)
        time.sleep(0.3)         # room for a kill that is never sent to land
        self.assertFalse(_pid_gone(pid),
                         f"subprocess.run reaped grandchild {pid} — then the orphan "
                         "the outcome is about cannot happen and the fix test proves "
                         "nothing")

    def test_grandchild_does_not_survive_the_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            stages.run_stage_subprocess(self._argv(), timeout=0.5, grace=0.5)
        pid = self._grandchild_pid()
        self.addCleanup(self._reap, pid)
        self.assertTrue(_wait(lambda: _pid_gone(pid), timeout=3.0),
                        f"grandchild {pid} outlived the stage timeout — this is the "
                        "orphan gradle/JVM that froze the runner on 2026-09-25")

    def test_the_stage_runs_in_its_own_process_group(self):
        r = stages.run_stage_subprocess(
            [sys.executable, "-c", "import os; print(os.getpgrp())"], timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(int(r.stdout.strip()), os.getpgrp(),
                            "the stage shares the runner's process group — a killpg "
                            "on it would signal the runner itself")

    def test_normal_exit_matches_subprocess_run_shape(self):
        """The caller contract: `runner._execute` reads `r.returncode`, `r.stdout`
        and `r.stderr` straight off this."""
        argv = [sys.executable, "-c",
                "import sys; sys.stdout.write('out'); sys.stderr.write('err'); "
                "sys.exit(3)"]
        got = stages.run_stage_subprocess(argv, timeout=60)
        ref = subprocess.run(argv, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        self.assertIsInstance(got, subprocess.CompletedProcess)
        self.assertEqual((got.returncode, got.stdout, got.stderr),
                         (ref.returncode, ref.stdout, ref.stderr))
        self.assertEqual(list(got.args), argv)

    def test_cwd_and_env_reach_the_stage(self):
        # The runner passes the trusted vault cwd and the per-account +
        # Gradle-limited env through these two kwargs; losing either silently
        # would un-do stage_env entirely.
        argv = [sys.executable, "-c",
                "import os; print(os.getcwd()); print(os.environ.get('MACP_PROBE'))"]
        r = stages.run_stage_subprocess(argv, cwd=str(self.tmp),
                                        env={**os.environ, "MACP_PROBE": "yes"},
                                        timeout=60)
        cwd, probe = r.stdout.strip().splitlines()
        self.assertEqual(Path(cwd).resolve(), self.tmp.resolve())
        self.assertEqual(probe, "yes")

    def test_timeout_expired_carries_the_partial_output(self):
        # run.log's `=== <stage> (timeout after Ns) ===\n{partial[-2000:]}` is
        # the only record of what a killed stage had said. It hangs off this.
        argv = [sys.executable, "-c",
                "import sys, time; sys.stdout.write('halfway\\n'); "
                "sys.stdout.flush(); time.sleep(30)"]
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            stages.run_stage_subprocess(argv, timeout=0.5, grace=0.5)
        self.assertIn("halfway", stages._git_stream(cm.exception.stdout))
        self.assertEqual(cm.exception.timeout, 0.5)
        self.assertEqual(list(cm.exception.cmd), argv)

    def test_a_failing_kill_does_not_mask_the_timeout(self):
        """Fail-open: cleanup must never replace the exception the runner's
        checkpoint / `timed_out=True` path keys on. A short sleep here, because a
        kill that cannot land leaves the tree to exit on its own."""
        with mock.patch.object(os, "killpg", side_effect=OSError("denied")), \
                self.assertRaises(subprocess.TimeoutExpired):
            stages.run_stage_subprocess(self._argv(2), timeout=0.5, grace=0.5)
        self.addCleanup(self._reap, self._grandchild_pid())

    def test_a_stage_in_the_runners_own_group_is_never_killpg_ed(self):
        """The suicide guard. If `start_new_session` ever stops taking effect the
        stage shares the runner's pgid, and signalling it would kill the runner —
        so that case degrades to the direct child instead."""
        proc = mock.Mock(pid=4242)
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=0.01)
        with mock.patch.object(os, "getpgid", return_value=os.getpgid(0)), \
             mock.patch.object(os, "killpg",
                               side_effect=AssertionError("killpg on the runner's "
                                                          "own process group")):
            stages._kill_process_group(proc, grace=0.01)
        self.assertEqual([c.args[0] for c in proc.send_signal.call_args_list],
                         [signal.SIGTERM, signal.SIGKILL])

    def test_kill_never_raises_even_when_every_step_fails(self):
        proc = mock.Mock(pid=4242)
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=0.01)
        proc.send_signal.side_effect = OSError("gone")
        notes = []
        with mock.patch.object(os, "getpgid", side_effect=OSError("no such pgid")):
            stages._kill_process_group(proc, grace=0.01, progress=notes.append)
        self.assertTrue(any("SIGKILL" in n for n in notes), notes)


class StageEnvWithGradleTest(unittest.TestCase):
    """`stage_env_with_gradle` folds the worker Gradle limits into GRADLE_OPTS in
    the stage env only, without clobbering anything the account env carried."""

    def test_worker_opts_cover_idle_timeout_and_kotlin_strategy(self):
        # The two levers criterion ③ names: a daemon idle-timeout and the Kotlin
        # compiler strategy. Pin the substance so a reword can't hollow the intent.
        self.assertIn("org.gradle.daemon.idletimeout", stages.WORKER_GRADLE_OPTS)
        self.assertIn("kotlin.compiler.execution.strategy=in-process",
                      stages.WORKER_GRADLE_OPTS)

    def test_injects_into_a_base_dict_and_preserves_other_keys(self):
        base = {"CLAUDE_CONFIG_DIR": "/x/cfg", "PATH": "/bin"}
        env = stages.stage_env_with_gradle(base)
        self.assertEqual(env["GRADLE_OPTS"], stages.WORKER_GRADLE_OPTS)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/x/cfg")   # account env untouched
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("GRADLE_OPTS", base, "must not mutate the caller's dict")

    def test_appends_to_existing_gradle_opts_with_worker_opts_last(self):
        env = stages.stage_env_with_gradle({"GRADLE_OPTS": "-Dfoo=bar"})
        self.assertEqual(env["GRADLE_OPTS"], f"-Dfoo=bar {stages.WORKER_GRADLE_OPTS}")
        # ours LAST → JVM's last-wins gives the worker limits precedence.
        self.assertTrue(env["GRADLE_OPTS"].endswith(stages.WORKER_GRADLE_OPTS))

    def test_none_base_inherits_os_environ(self):
        with mock.patch.dict(os.environ, {"CARRIED": "1"}, clear=False):
            env = stages.stage_env_with_gradle(None)
        self.assertEqual(env.get("CARRIED"), "1")             # inherits the parent env
        self.assertIn(stages.WORKER_GRADLE_OPTS, env["GRADLE_OPTS"])

    def test_no_repo_dir_is_byte_identical_to_worker_opts(self):
        # AC6 bit-for-bit: without a repo_dir the GRADLE_OPTS is exactly today's.
        self.assertEqual(stages.stage_env_with_gradle({})["GRADLE_OPTS"],
                         stages.WORKER_GRADLE_OPTS)

    def test_repo_dir_folds_in_a_per_run_registry_base_after_worker_opts(self):
        # AC (a): a known checkout adds a per-run registry base, AFTER (not
        # replacing) WORKER_GRADLE_OPTS, which stays a contiguous leading substring.
        d = Path("/x/Worker/some-repo.wt/leaf-9")             # pure derivation, no I/O
        env = stages.stage_env_with_gradle({}, repo_dir=d)
        base = stages.gradle_registry_base(d)
        self.assertTrue(env["GRADLE_OPTS"].startswith(stages.WORKER_GRADLE_OPTS))
        self.assertIn(f"-Dorg.gradle.daemon.registry.base={base}", env["GRADLE_OPTS"])

    def test_no_daemon_flag_folds_in_daemon_false(self):
        # Open-Q #4: an explicit, default-off env flag → run gradle with no daemon.
        with mock.patch.dict(os.environ, {"WORKER_GRADLE_NO_DAEMON": "1"}, clear=False):
            env = stages.stage_env_with_gradle({}, repo_dir=Path("/x/Worker/r.wt/t"))
        self.assertIn("-Dorg.gradle.daemon=false", env["GRADLE_OPTS"])
        self.assertIn(stages.WORKER_GRADLE_OPTS, env["GRADLE_OPTS"])


class StageEnvTest(unittest.TestCase):
    """`stage_env` = the Gradle limits + background tasks switched off, without
    touching the caller's dict."""

    def test_disables_background_tasks_and_keeps_gradle_limits(self):
        base = {"CLAUDE_CONFIG_DIR": "/x/cfg"}
        env = stages.stage_env(base)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        self.assertEqual(env["GRADLE_OPTS"], stages.WORKER_GRADLE_OPTS)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/x/cfg")
        self.assertEqual(base, {"CLAUDE_CONFIG_DIR": "/x/cfg"}, "must not mutate the caller's dict")

    def test_overrides_an_inherited_enable(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"}):
            env = stages.stage_env(None)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")

    def test_repo_dir_threads_through_to_the_registry_base(self):
        d = Path("/x/Worker/some-repo.wt/leaf-9")
        env = stages.stage_env({"CLAUDE_CONFIG_DIR": "/x/cfg"}, repo_dir=d)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        self.assertIn(f"-Dorg.gradle.daemon.registry.base={stages.gradle_registry_base(d)}",
                      env["GRADLE_OPTS"])


# --- stage token usage: --session-id + the CLI's own transcript ---------------

_CLAUDE_OUTPUT = Path(__file__).resolve().parent / "fixtures" / "claude_output"
# Live captures (capture.py) vs outcomes derived from them (synthesize.py).
CAPTURED = ("success", "error_max_turns")
SYNTHESIZED = ("error_during_execution", "capacity_limit", "cli_crash")


def _claude_fixture(name: str) -> dict:
    return json.loads((_CLAUDE_OUTPUT / f"{name}.json").read_text(encoding="utf-8"))


def _plant_transcript(config_dir: Path, leg: dict, slug: str = "-vault") -> None:
    """Put a fixture leg's transcript where the CLI would have written it."""
    if leg.get("transcript") is None:
        return
    p = Path(config_dir) / "projects" / slug / f"{leg['session_id']}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"{ln}\n" for ln in leg["transcript"]), encoding="utf-8")


def _asst(mid, model="m-1", **usage) -> str:
    u = dict.fromkeys(stages.USAGE_TOKEN_KEYS, 0)
    u.update(usage)
    return json.dumps({"type": "assistant",
                       "message": {"id": mid, "model": model, "usage": u}})


def _cost(value) -> str:
    return json.dumps({"type": "cost-state", "totalCostUSD": value})


class StageUsageFixtureTest(unittest.TestCase):
    """The live captures the design rests on (fixtures/claude_output/, capture.py).
    These pin the go/no-go facts themselves, so a re-capture on a newer CLI that
    breaks one of them fails here first rather than quietly in production."""

    def test_provenance_labels_captured_and_synthesized_apart(self):
        for name in CAPTURED:
            self.assertEqual(_claude_fixture(name)["provenance"]["kind"], "captured", name)
        for name in SYNTHESIZED:
            self.assertEqual(_claude_fixture(name)["provenance"]["kind"], "synthesized", name)

    def test_session_id_leaves_rc_stdout_stderr_byte_identical(self):
        """Design B's premise: the one argv change instrumentation makes is
        invisible to everything the stage loop reads."""
        for name in CAPTURED:
            legs = _claude_fixture(name)["legs"]
            for key in ("rc", "stdout", "stderr"):
                self.assertEqual(legs["text_sid"][key], legs["text"][key], f"{name}: {key}")
            tail = legs["text_sid"]["argv_tail"]
            i = tail.index(stages.SESSION_ID_FLAG)
            self.assertEqual(tail[:i] + tail[i + 2:], legs["text"]["argv_tail"], name)

    def test_json_mode_would_have_changed_stderr(self):
        """Why not --output-format json (design A): same rc, different stderr —
        and stderr is half of what invalidate_model_cache_if_unavailable reads."""
        for name in CAPTURED:
            legs = _claude_fixture(name)["legs"]
            self.assertEqual(legs["json"]["rc"], legs["text"]["rc"], name)
            self.assertNotEqual(legs["json"]["stderr"], legs["text"]["stderr"], name)

    def test_parser_agrees_with_the_clis_own_json_report(self):
        """The json leg's transcript belongs to the very session its JSON result
        reports on, so the parser is checked against the CLI's own accounting."""
        for name in CAPTURED:
            leg = _claude_fixture(name)["legs"]["json"]
            cli = json.loads(leg["stdout"])
            self.assertEqual(cli["session_id"], leg["session_id"], name)
            got = stages.transcript_usage(leg["transcript"])
            for key in stages.USAGE_TOKEN_KEYS:
                self.assertEqual(got[key], cli["usage"][key], f"{name}: {key}")
            self.assertEqual(got["total_cost_usd"], cli["total_cost_usd"], name)
            self.assertEqual(got["model"], next(iter(cli["modelUsage"])), name)

    def test_every_api_message_is_written_twice_so_the_dedupe_is_load_bearing(self):
        for name in CAPTURED:
            lines = _claude_fixture(name)["legs"]["text_sid"]["transcript"]
            asst = [d for d in map(json.loads, lines) if d.get("type") == "assistant"]
            self.assertEqual(len(asst), 2, name)
            self.assertEqual(len({d["message"]["id"] for d in asst}), 1, name)
            got = stages.transcript_usage(lines)
            self.assertEqual(got["num_turns"], 1, name)
            self.assertEqual(got["output_tokens"],
                             asst[0]["message"]["usage"]["output_tokens"], name)

    def test_stage_usage_turns_every_fixture_into_its_record(self):
        """End to end over each fixture's own argv: a planted transcript becomes
        the nine keys, and the two shapes without API usage become `unavailable`."""
        unavailable = {"capacity_limit": "no usage in transcript (rc=1)",
                       "cli_crash": "cli crash (rc=1): transcript not found"}
        for name in CAPTURED + SYNTHESIZED:
            leg = _claude_fixture(name)["legs"]["text_sid"]
            with tempfile.TemporaryDirectory() as cfg:
                _plant_transcript(Path(cfg), leg)
                rec = stages.stage_usage(["claude", "-p", "x", *leg["argv_tail"]],
                                         env={"CLAUDE_CONFIG_DIR": cfg},
                                         returncode=leg["rc"], duration_ms=1234)
            if name in unavailable:
                self.assertEqual(rec, {"unavailable": unavailable[name]}, name)
                continue
            lines = [json.loads(ln) for ln in leg["transcript"]]
            first = next(d for d in lines if d.get("type") == "assistant")
            costs = [d["totalCostUSD"] for d in lines if d.get("type") == "cost-state"]
            self.assertEqual(tuple(rec), stages.USAGE_KEYS, name)
            for key in stages.USAGE_TOKEN_KEYS:
                self.assertEqual(rec[key], first["message"]["usage"][key], f"{name}: {key}")
            self.assertEqual(rec["num_turns"], 1, name)
            self.assertEqual(rec["duration_ms"], 1234, name)
            self.assertEqual(rec["model"], "claude-haiku-4-5-20251001", name)
            self.assertEqual(rec["session_id"], leg["session_id"], name)
            # error_during_execution died before its cost-state line
            self.assertEqual(rec["total_cost_usd"], costs[-1] if costs else None, name)


class TranscriptUsageParseTest(unittest.TestCase):
    """transcript_usage on hand-built lines — the shapes a live capture cannot
    produce on demand."""

    def test_dedupes_by_message_id_keeping_the_larger_count(self):
        got = stages.transcript_usage([
            _asst("a", input_tokens=5, output_tokens=1),       # early block, partial
            _asst("a", input_tokens=5, output_tokens=40),
            _asst("b", output_tokens=2, cache_read_input_tokens=7),
        ])
        self.assertEqual(got["input_tokens"], 5)
        self.assertEqual(got["output_tokens"], 42)
        self.assertEqual(got["cache_read_input_tokens"], 7)
        self.assertEqual(got["num_turns"], 2)

    def test_synthetic_notices_are_not_api_messages(self):
        self.assertIsNone(stages.transcript_usage([_asst("s", model="<synthetic>")]))
        got = stages.transcript_usage([_asst("s", model="<synthetic>"),
                                       _asst("a", output_tokens=3)])
        self.assertEqual((got["num_turns"], got["model"], got["output_tokens"]),
                         (1, "m-1", 3))

    def test_skips_everything_that_is_not_an_assistant_usage_line(self):
        lines = [
            "", "not json, mentions assistant", '[1, "assistant"]',
            json.dumps({"type": "user",
                        "message": {"id": "u", "usage": {"input_tokens": 99}}}),
            json.dumps({"type": "assistant", "message": "not a dict"}),
            json.dumps({"type": "assistant", "message": {"id": "x", "usage": "nope"}}),
            json.dumps({"type": "assistant",
                        "message": {"usage": {"input_tokens": 50}}}),    # no id
            _asst("b", input_tokens=True, output_tokens=-4),           # bool/negative → 0
            _asst("a", input_tokens=2, output_tokens=3),
        ]
        got = stages.transcript_usage(lines)
        self.assertEqual((got["input_tokens"], got["output_tokens"], got["num_turns"]),
                         (2, 3, 2))

    def test_cost_is_the_last_cost_state_and_none_without_one(self):
        base = [_asst("a", output_tokens=1)]
        self.assertIsNone(stages.transcript_usage(base)["total_cost_usd"])
        self.assertEqual(
            stages.transcript_usage(base + [_cost(0.5), _cost(0.75)])["total_cost_usd"], 0.75)
        for bad in (True, "0.5", None, float("nan"), float("inf")):
            self.assertIsNone(
                stages.transcript_usage(base + [_cost(bad)])["total_cost_usd"], bad)

    def test_model_is_the_most_frequent_and_a_tie_goes_to_the_argv_model(self):
        most = [_asst("a", model="x"), _asst("b", model="y"), _asst("c", model="y")]
        self.assertEqual(stages.transcript_usage(most, model_hint="x")["model"], "y")
        tie = [_asst("a", model="x"), _asst("b", model="claude-y-20260101")]
        self.assertEqual(stages.transcript_usage(tie)["model"], "x")            # first seen
        self.assertEqual(stages.transcript_usage(tie, model_hint="x")["model"], "x")
        self.assertEqual(stages.transcript_usage(tie, model_hint="claude-y")["model"],
                         "claude-y-20260101")                                   # prefix

    def test_no_assistant_usage_is_none(self):
        self.assertIsNone(stages.transcript_usage([]))
        self.assertIsNone(stages.transcript_usage([_cost(1.0)]))


class UsageKillSwitchArgvTest(unittest.TestCase):
    """MACP_STAGE_USAGE=0 gives back the pre-instrumentation argv exactly — the
    switch an operator flips instead of reverting code."""

    def setUp(self):
        self.worker_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.worker_root, ignore_errors=True)
        patcher = mock.patch.object(ms, "resolve_model", return_value="model-x")
        patcher.start()
        self.addCleanup(patcher.stop)
        # host-independent: the pre-instrumentation argv formula asserted below
        # assumes no browser MCP server is configured, regardless of whether
        # this host actually has one installed.
        mcp_patcher = mock.patch.object(stages, "browser_mcp_server", return_value=None)
        mcp_patcher.start()
        self.addCleanup(mcp_patcher.stop)

    def _argv(self, switch):
        with mock.patch.dict(os.environ):
            os.environ.pop(stages.USAGE_ENV, None)
            if switch is not None:
                os.environ[stages.USAGE_ENV] = switch
            return stages.build_stage_argv(
                "implement", task_id="t", run_id="r", run_dir=Path("/x"),
                branch="b", worker_root=self.worker_root)

    def test_off_is_the_pre_instrumentation_argv_exactly(self):
        for switch in ("0", " 0 "):
            argv = self._argv(switch)
            self.assertEqual(argv[:2], ["claude", "-p"])
            # the argv formula as it stood before instrumentation, spelled out
            self.assertEqual(argv[3:], [
                "--model", stages.stage_model("implement"),
                "--effort", stages.stage_effort("implement"),
                "--max-turns", "200",
                "--allowed-tools", *stages._safe_tools("implement"),
                "--add-dir", str(self.worker_root)], repr(switch))
            self.assertIsNone(stages.argv_session_id(argv))

    def test_on_by_default_adds_one_fresh_uuid_right_before_allowed_tools(self):
        on = self._argv(None)
        i = on.index(stages.SESSION_ID_FLAG)
        self.assertEqual(on[i - 2:i], ["--max-turns", "200"])
        self.assertEqual(on[i + 2], "--allowed-tools")
        self.assertEqual(str(uuid.UUID(on[i + 1])), on[i + 1])
        self.assertEqual(stages.argv_session_id(on), on[i + 1])
        # taking the pair back out gives the switched-off argv, prompt included
        self.assertEqual(on[:i] + on[i + 2:], self._argv("0"))
        self.assertNotEqual(stages.argv_session_id(self._argv(None)), on[i + 1])

    def test_only_an_explicit_zero_turns_it_off(self):
        for switch in ("1", "", "false", "no"):
            self.assertIsNotNone(stages.argv_session_id(self._argv(switch)), repr(switch))

    def test_switch_is_read_per_call_not_at_import(self):
        self.assertIsNotNone(stages.argv_session_id(self._argv(None)))
        self.assertIsNone(stages.argv_session_id(self._argv("0")))
        self.assertIsNotNone(stages.argv_session_id(self._argv(None)))

    def test_argv_session_id_only_accepts_a_canonical_uuid(self):
        good = str(uuid.uuid4())
        self.assertEqual(stages.argv_session_id(["claude", "--session-id", good]), good)
        for argv in (["claude", "--session-id"], ["--session-id", "../../x"],
                     ["--session-id", "{" + good + "}"],
                     ["--session-id", good.replace("-", "")],
                     ["true"], None, mock.Mock()):
            self.assertIsNone(stages.argv_session_id(argv), argv)


class RecordUsageTest(unittest.TestCase):
    """stages.json `usage` is additive and best-effort, like every stages.json write."""

    def setUp(self):
        self.run = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run, ignore_errors=True)

    def _read(self) -> dict:
        return json.loads((self.run / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))

    def test_stages_accumulate_and_sibling_keys_survive(self):
        stages.record_model_tier(self.run, "standard", "model_tier: standard")
        stages.record_stage(self.run, "research")
        stages.record_orphaned_commits(self.run, {"count": 1, "shas": ["abc"]})
        stages.record_usage(self.run, "research", {"unavailable": "timeout"})
        stages.record_stage(self.run, "plan")
        stages.record_usage(self.run, "plan", {"input_tokens": 1})
        data = self._read()
        self.assertEqual(data["usage"], {"research": {"unavailable": "timeout"},
                                         "plan": {"input_tokens": 1}})
        self.assertEqual(data["completed"], ["research", "plan"])
        self.assertEqual((data["model_tier"], data["model_tier_reason"]),
                         ("standard", "model_tier: standard"))
        self.assertEqual(data["orphaned_commits"], {"count": 1, "shas": ["abc"]})

    def test_never_raises(self):
        p = self.run / stages.STAGE_PROGRESS
        p.write_text("{not json", encoding="utf-8")
        stages.record_usage(self.run, "plan", {"x": 1})     # damaged → {} like _update_progress
        self.assertEqual(self._read(), {"usage": {"plan": {"x": 1}}})
        p.write_text('{"usage": [1]}', encoding="utf-8")
        stages.record_usage(self.run, "plan", {"x": 2})     # wrong-typed usage → replaced
        self.assertEqual(self._read(), {"usage": {"plan": {"x": 2}}})
        stages.record_usage(self.run / "gone" / "deeper", "plan", {"x": 1})
        with mock.patch.object(stages, "_update_progress", side_effect=RuntimeError("boom")):
            stages.record_usage(self.run, "plan", {"x": 3})


class RecordStageUsageTest(unittest.TestCase):
    """stage_usage / record_stage_usage — the runner's one call per stage."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run = self.tmp / "run"
        self.run.mkdir()
        self.cfg = self.tmp / "cfg"
        self.sid = str(uuid.uuid4())
        self.argv = ["claude", "-p", "x", "--model", "m-1", "--max-turns", "5",
                     stages.SESSION_ID_FLAG, self.sid, "--allowed-tools", "Read"]
        self.env = {"CLAUDE_CONFIG_DIR": str(self.cfg)}

    def _plant(self, *lines, slug="-home-x-vault"):
        p = self.cfg / "projects" / slug / f"{self.sid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")

    def _progress(self):
        p = self.run / stages.STAGE_PROGRESS
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def test_a_planted_transcript_becomes_the_nine_keys(self):
        self._plant(_asst("a", model="m-1", input_tokens=3, output_tokens=4), _cost(0.25))
        rec = stages.record_stage_usage(self.run, "plan", self.argv, env=self.env,
                                        returncode=0, duration_ms=987)
        self.assertEqual(rec, {"input_tokens": 3, "output_tokens": 4,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "num_turns": 1,
                               "duration_ms": 987, "model": "m-1",
                               "session_id": self.sid, "total_cost_usd": 0.25})
        self.assertEqual(tuple(rec), stages.USAGE_KEYS)
        self.assertEqual(self._progress()["usage"]["plan"], rec)

    def test_config_dir_comes_from_the_stage_env_else_the_process_env(self):
        self._plant(_asst("a"), slug="-some-other-cwd")      # any slug: globbed
        self.assertIsNotNone(stages.transcript_path(self.sid, self.env))
        self.assertIsNone(stages.transcript_path(
            self.sid, {"CLAUDE_CONFIG_DIR": str(self.tmp / "elsewhere")}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}):
            self.assertIsNotNone(stages.transcript_path(self.sid))

    def test_unavailable_reasons(self):
        self.assertEqual(stages.stage_usage(self.argv, env=self.env, returncode=0),
                         {"unavailable": "transcript not found"})
        self.assertEqual(stages.stage_usage(self.argv, env=self.env, returncode=1),
                         {"unavailable": "cli crash (rc=1): transcript not found"})
        self._plant(_asst("n", model="<synthetic>"), _cost(0))
        self.assertEqual(stages.stage_usage(self.argv, env=self.env, returncode=1),
                         {"unavailable": "no usage in transcript (rc=1)"})
        self.assertEqual(stages.stage_usage(self.argv, env=self.env, returncode=0),
                         {"unavailable": "no usage in transcript"})
        # a timeout is unavailable even when a (partial) transcript is there
        self._plant(_asst("a", output_tokens=9))
        self.assertEqual(stages.stage_usage(self.argv, env=self.env, timed_out=True),
                         {"unavailable": "timeout"})

    def test_no_session_id_records_nothing_even_if_the_reader_would_raise(self):
        with mock.patch.object(stages, "stage_usage", side_effect=RuntimeError("boom")) as su:
            self.assertIsNone(stages.record_stage_usage(self.run, "implement", ["true"]))
        su.assert_not_called()
        self.assertIsNone(self._progress())

    def test_a_raising_reader_is_recorded_as_unavailable(self):
        self._plant(_asst("a"))
        with mock.patch.object(stages, "transcript_usage", side_effect=RuntimeError("boom")):
            rec = stages.record_stage_usage(self.run, "implement", self.argv,
                                            env=self.env, returncode=0)
        self.assertEqual(rec, {"unavailable": "usage reader error: RuntimeError"})
        self.assertEqual(self._progress()["usage"]["implement"], rec)

    def test_never_raises_even_when_recording_does(self):
        with mock.patch.object(stages, "record_usage", side_effect=RuntimeError("boom")):
            self.assertIsNone(stages.record_stage_usage(self.run, "implement", self.argv,
                                                        env=self.env))
        with mock.patch.object(stages, "argv_session_id", side_effect=RuntimeError("boom")):
            self.assertIsNone(stages.record_stage_usage(self.run, "implement", self.argv))


class GitTimeoutTest(unittest.TestCase):
    """A git that never returns must not hold a runner slot forever.

    2026-09-25: over `memory.high`, `remote_branch_exists`' `ls-remote` sat in
    `git-remote-https` for 18~72 minutes and three runner threads never came
    back, while the dashboard read idle. These pin the ceiling, the shape of
    what a timeout returns, and the freed thread."""

    def _slow_git_on_path(self, seconds: int = 5) -> Path:
        """Put a `git` that just sleeps at the front of PATH, and return its dir.

        `exec sleep` rather than plain `sleep`: the sleep replaces the shell, so
        it is the direct child `subprocess.run` kills on timeout, and the test
        leaves no orphan behind."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "git"
        shim.write_text(f"#!/bin/sh\nexec sleep {seconds}\n", encoding="utf-8")
        shim.chmod(0o755)
        return tmp

    def test_network_default_is_longer_than_local(self):
        self.assertGreater(stages.GIT_TIMEOUT_LOCAL_S, 0)
        self.assertGreater(stages.GIT_TIMEOUT_NETWORK_S, stages.GIT_TIMEOUT_LOCAL_S)
        self.assertNotEqual(stages.GIT_TIMEOUT_RC, 0)

    def test_subcommand_classification(self):
        net = stages.GIT_TIMEOUT_NETWORK_S
        loc = stages.GIT_TIMEOUT_LOCAL_S
        for args in (("ls-remote", "--heads", "origin", "worker/x"),
                     ("fetch", "origin", "--prune"),
                     ("push", "origin", "worker/x"),
                     ("pull", "--ff-only")):
            self.assertEqual(stages._default_git_timeout(args), net, args)
        for args in (("checkout", "-B", "worker/x", "main"),
                     ("rev-list", "--count", "main..worker/x"),
                     ("status", "--porcelain"),
                     ("show", "-s", "--format=%cI", "HEAD"),
                     ()):
            self.assertEqual(stages._default_git_timeout(args), loc, args)
        # leading flags do not hide the subcommand
        self.assertEqual(stages._default_git_timeout(("--no-pager", "fetch")), net)

    def test_timeout_becomes_a_completedprocess_not_an_exception(self):
        exc = subprocess.TimeoutExpired(cmd=["git", "ls-remote"], timeout=1)
        with mock.patch.object(subprocess, "run", side_effect=exc):
            r = stages._git(Path("/tmp"), "ls-remote", "--heads", "origin", "b")
        self.assertEqual(r.returncode, stages.GIT_TIMEOUT_RC)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("timeout", r.stderr)
        self.assertIsInstance(r.stdout, str)
        self.assertIsInstance(r.stderr, str)

    def test_partial_byte_output_is_normalised_to_str(self):
        """`TimeoutExpired.stdout`/`.stderr` come back as bytes even in text mode.
        Leaving them that way makes `(r.stderr or "").strip()[:200]` raise."""
        exc = subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=1,
                                        output=b"partial", stderr=b"err")
        with mock.patch.object(subprocess, "run", side_effect=exc):
            r = stages._git(Path("/tmp"), "fetch", "origin")
        self.assertIsInstance(r.stdout, str)
        self.assertIsInstance(r.stderr, str)
        self.assertEqual(r.stdout, "partial")
        self.assertIn("err", r.stderr)
        self.assertIn("timeout", r.stderr)
        self.assertEqual((r.stderr or "").strip()[:200].splitlines()[0], "err")

    def test_remote_branch_exists_returns_false_on_a_slow_git(self):
        tmp = self._slow_git_on_path()
        with mock.patch.dict(os.environ,
                             {"PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}"}), \
                mock.patch.object(stages, "GIT_TIMEOUT_NETWORK_S", 1):
            t0 = time.monotonic()
            got = stages.remote_branch_exists(tmp, "worker/x")
            elapsed = time.monotonic() - t0
        self.assertIs(got, False)
        # RED control: without the timeout this waits out the shim's full 5s.
        self.assertLess(elapsed, 4.0, f"elapsed={elapsed:.2f}s")

    def test_a_slow_git_releases_the_runner_thread(self):
        """The incident's actual symptom: the slot, not the return value."""
        tmp = self._slow_git_on_path()
        box = []
        with mock.patch.dict(os.environ,
                             {"PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}"}), \
                mock.patch.object(stages, "GIT_TIMEOUT_NETWORK_S", 1):
            t = threading.Thread(
                target=lambda: box.append(stages.remote_branch_exists(tmp, "worker/x")))
            t.start()
            t.join(timeout=4.0)
            alive = t.is_alive()
        self.assertFalse(alive, "runner thread still blocked on git")
        self.assertEqual(box, [False])


class WorktreeBranchPrepIsolationTest(unittest.TestCase):
    """AC1/AC3, structurally: two runs of the SAME repo resolve to DISTINCT
    worktrees, and one run's `prepare_external_branch` (`checkout -f -B`) and its
    `git clean -fd` cannot reach the other run's branch or files — the isolation
    the old shared-single-checkout model could not give (the collisions behind
    worker-shared-workingdir-collision and worker-sibling-run-branch-prep-orphans-
    your-commits). Plus a remove_worktree round-trip (AC3). Local bare-repo idiom
    only — no network, no GitHub owner URL literal (the public-identifier gate);
    mirrors test_repos.WorktreeTest._bare."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wr = self.tmp / "wr"            # worker_root
        self.wr.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bare(self, name: str) -> str:
        """Clonable bare repo `<tmp>/src/<name>.git` with one commit (worktree add
        needs a HEAD). repo_name(url) == name, so the shared clone lands at
        worker_root/<name>."""
        _g = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        seed = self.tmp / "seed" / name
        seed.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(_g + ["-C", str(seed), "add", "."], check=True)
        subprocess.run(_g + ["-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
        bare = self.tmp / "src" / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
        return str(bare)

    def _branch(self, wt: Path) -> str:
        return subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                              capture_output=True, text=True).stdout.strip()

    def _common_dir(self, wt: Path) -> Path:
        out = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-common-dir"],
                             capture_output=True, text=True, check=True).stdout.strip()
        p = Path(out)
        return (p if p.is_absolute() else (Path(wt) / p)).resolve()

    def test_two_runs_of_one_repo_cannot_cross_branch_or_files(self):
        from todo_system.orchestrator import repos
        url = self._bare("foo")
        # the shared clone + two per-run worktrees, exactly as the runner now
        # resolves an external task of this same repo on two concurrent runs.
        base = repos.ensure_local(url, self.wr, clone=True)
        self.assertIsNotNone(base)
        wt_a = repos.ensure_worktree(url, "slug-a", worker_root=self.wr, clone=True)
        wt_b = repos.ensure_worktree(url, "slug-b", worker_root=self.wr, clone=True)
        self.assertNotEqual(wt_a, wt_b)

        # run A branches, then leaves an UNCOMMITTED (untracked) artifact in its tree
        ra = stages.prepare_external_branch(wt_a, "worker/slug-a")
        self.assertEqual(ra.returncode, 0, ra.stderr)
        (wt_a / "evidence.txt").write_text("A's uncommitted work", encoding="utf-8")

        # run B branches — its `checkout -f -B` and `git clean -fd` act on B alone
        rb = stages.prepare_external_branch(wt_b, "worker/slug-b")
        self.assertEqual(rb.returncode, 0, rb.stderr)

        # B is on its own branch; A was left exactly where it was (HEAD untouched)
        self.assertEqual(self._branch(wt_b), "worker/slug-b")
        self.assertEqual(self._branch(wt_a), "worker/slug-a",
                         "run B's branch prep moved run A's HEAD")
        # and B's clean could not reach A's untracked file — the old single
        # checkout would have deleted it on B's `git clean -fd`.
        self.assertTrue((wt_a / "evidence.txt").exists(),
                        "run B's clean -fd deleted run A's uncommitted work")
        # distinct working trees over ONE shared common git dir (fetch/object source)
        self.assertEqual(self._common_dir(wt_a), self._common_dir(wt_b))
        self.assertEqual(self._common_dir(wt_a), (base / ".git").resolve())

    def test_remove_worktree_round_trip_and_absent_is_false(self):
        from todo_system.orchestrator import repos
        url = self._bare("foo")
        base = repos.ensure_local(url, self.wr, clone=True)
        wt = repos.ensure_worktree(url, "slug-b", worker_root=self.wr, clone=True)
        self.assertTrue((wt / ".git").exists())

        # a succeeded run's worktree is removed outright…
        self.assertTrue(stages.remove_worktree(base, wt))
        self.assertFalse(wt.exists())
        # …removing an already-absent worktree is a no-op returning False, never raises
        self.assertFalse(stages.remove_worktree(base, wt))
        # …and a retry's ensure_worktree re-creates the same path (idempotent lifecycle)
        again = repos.ensure_worktree(url, "slug-b", worker_root=self.wr, clone=True)
        self.assertEqual(again, wt)
        self.assertTrue((again / ".git").exists())


class PushGuardEmptyHooksPathTest(unittest.TestCase):
    """install_push_guard never writes `pre-push` at the repo root when
    `git rev-parse --git-path hooks` fails or prints nothing (2026-10-16)."""

    def _run(self, make_git_dir: bool, rc: int, stdout: str):
        import subprocess
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            if make_git_dir:
                (repo / ".git").mkdir()
            fake = subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")
            with mock.patch.object(stages, "_git", return_value=fake):
                hook = stages.install_push_guard(repo)
            root_file = (repo / "pre-push").exists()
            in_git = (repo / ".git" / "hooks" / "pre-push").exists()
            return hook, root_file, in_git

    def test_empty_output_with_git_dir_falls_back_to_dot_git_hooks(self):
        hook, root_file, in_git = self._run(True, 0, "")
        self.assertFalse(root_file)
        self.assertTrue(in_git)

    def test_failed_rev_parse_without_git_dir_writes_nothing(self):
        hook, root_file, in_git = self._run(False, 128, "")
        self.assertFalse(root_file)
        self.assertFalse(in_git)
