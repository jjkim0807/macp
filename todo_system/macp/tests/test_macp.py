"""Tests for MaestroACP context isolation (profiles + per-project .macp)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system.macp import accounts as ACC   # LEGACY_CONFIG_DIR = legacy single-dir default (value-identical)



class MacpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["MACP_HOME"] = str(self.tmp / "home")
        # config module caches HOME at import; refresh module-level paths
        import importlib
        from todo_system.macp import config as C
        importlib.reload(C)
        self.C = C
        self.proj = self.tmp / "proj"
        self.proj.mkdir()

    def tearDown(self):
        os.environ.pop("MACP_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        import importlib
        from todo_system.macp import config as C
        importlib.reload(C)

    def test_profile_roundtrip_and_default(self):
        C = self.C
        C.write_profile(C.Profile(name="work", vault="/v/work", daily_run_budget=20,
                                  max_parallel_per_repo=3, model="claude-fable-5"))
        C.set_default_profile("work")
        self.assertIn("work", C.list_profiles())
        self.assertEqual(C.default_profile(), "work")
        p = C.load_profile("work")
        self.assertEqual(p.vault, "/v/work")
        self.assertEqual(p.daily_run_budget, 20)
        self.assertEqual(p.max_parallel_per_repo, 3)   # round-trips write→load

    def test_max_parallel_per_repo_defaults_to_two(self):
        # leaf 5 flipped the switch: a profile that never wrote the field (older
        # config.toml) loads as 2 = concurrency on, not the historical 1.
        C = self.C
        C.write_profile(C.Profile(name="bare"))
        self.assertEqual(C.load_profile("bare").max_parallel_per_repo, 2)
        self.assertEqual(C.Profile(name="x").max_parallel_per_repo, 2)

    def test_accounts_parallel_capacity(self):
        C = self.C
        prof = C.Profile(name="work")
        prof.accounts = [C.Account("a", claude_config_dir=ACC.LEGACY_CONFIG_DIR),
                         C.Account("b", claude_config_dir="~/.claude-2")]
        C.write_profile(prof)
        loaded = C.load_profile("work")
        self.assertEqual([a.name for a in loaded.accounts], ["a", "b"])

    def test_multivendor_accounts_roundtrip(self):
        C = self.C
        prof = C.Profile(name="mix")
        prof.accounts = [C.Account("cc", agent="claude", claude_config_dir="~/.claude-2"),
                         C.Account("cx", agent="codex", command="codex exec {prompt}"),
                         C.Account("cd", agent="claude", claude_config_dir=ACC.LEGACY_CONFIG_DIR)]
        C.write_profile(prof)
        loaded = C.load_profile("mix")
        self.assertEqual([(a.name, a.agent) for a in loaded.accounts],
                         [("cc", "claude"), ("cx", "codex"), ("cd", "claude")])

    def test_project_memory_and_context_linkage(self):
        from todo_system.macp import memory as MEM
        (self.proj / "CLAUDE.md").write_text("# proj\n", encoding="utf-8")
        MEM.write_memory(self.proj, "api-notes", "REST base is /v2")
        self.assertIn("api-notes.md", MEM.discover_context(self.proj)["memory_files"])
        self.assertIn("REST base is /v2", MEM.read_memory(self.proj))
        actions = MEM.link_agent_context(self.proj)
        self.assertEqual(actions, ["CLAUDE.md @import", "AGENTS.md created"])
        # CLAUDE.md gets the @-import; AGENTS.md gets created with a pointer
        self.assertIn("@.macp/memory/index.md", (self.proj / "CLAUDE.md").read_text())
        self.assertTrue((self.proj / "AGENTS.md").exists())
        self.assertIn(".macp/memory", (self.proj / "AGENTS.md").read_text())

    def test_project_init_and_resolve(self):
        C = self.C
        C.write_profile(C.Profile(name="work", vault="/v/work"))
        C.set_default_profile("work")
        C.write_project(self.proj, C.ProjectConfig(profile="work", worker_mode="quick",
                                                   capabilities=["python"]))
        # find + resolve from inside the project (and a subdir)
        sub = self.proj / "src"
        sub.mkdir()
        self.assertEqual(C.find_project_root(sub), self.proj.resolve())
        ctx = C.resolve(sub)
        self.assertEqual(ctx["project"].worker_mode, "quick")
        self.assertEqual(ctx["profile"].vault, "/v/work")

    def test_resolve_outside_project(self):
        C = self.C
        ctx = C.resolve(self.tmp)   # no .macp here
        self.assertIsNone(ctx["project_root"])

    def test_unmanaged_dir_under_home_not_mistaken_for_project(self):
        # Repro: MACP_HOME is <X>/.macp; from an UNMANAGED dir under <X>, walking up
        # reaches <X> whose `.macp` IS MACP_HOME. It must NOT be reported as a
        # project — that returned project_root=<X> with project=None and crashed
        # `macp status` (AttributeError: 'NoneType' has no attribute 'profile').
        import importlib
        from todo_system.macp import config as C
        os.environ["MACP_HOME"] = str(self.tmp / ".macp")
        importlib.reload(C)
        (self.tmp / ".macp" / "profiles").mkdir(parents=True)   # global home, no project config
        unmanaged = self.tmp / "downloads"
        unmanaged.mkdir()
        ctx = C.resolve(unmanaged)
        self.assertIsNone(C.find_project_root(unmanaged))
        self.assertIsNone(ctx["project_root"])   # not mistaken for a project
        self.assertIsNone(ctx["project"])         # and consistent — never root w/o project
        # a real managed project in the same tree is still detected (no regression)
        proj = self.tmp / "proj2"
        C.write_project(proj, C.ProjectConfig(profile="default", worker_mode="auto"))
        ctx2 = C.resolve(proj)
        self.assertEqual(ctx2["project_root"], str(proj.resolve()))
        self.assertIsNotNone(ctx2["project"])


    def test_project_config_deps_roundtrip(self):
        C = self.C
        C.write_project(self.proj, C.ProjectConfig(profile="default", name="myproj",
                                                   provides="x-sdk", depends_on=["a", "b"]))
        cfg = C.load_project(self.proj)
        self.assertEqual(cfg.name, "myproj")
        self.assertEqual(cfg.provides, "x-sdk")
        self.assertEqual(cfg.depends_on, ["a", "b"])

    def test_cross_project_deps_and_impact(self):
        from todo_system.macp import deps as D
        C = self.C
        # advote (platform) → dripcoffee (consumer) → analytics (consumes dripcoffee)
        C.write_project(self.tmp / "advote", C.ProjectConfig(provides="advote-feedback-sdk"))
        C.write_project(self.tmp / "dripcoffee", C.ProjectConfig(depends_on=["advote"]))
        C.write_project(self.tmp / "analytics", C.ProjectConfig(depends_on=["dripcoffee"]))
        C.write_project(self.tmp / "solo", C.ProjectConfig())            # unrelated
        nodes = D.scan([self.tmp])
        self.assertEqual(set(nodes), {"advote", "dripcoffee", "analytics", "solo"})
        self.assertEqual(nodes["advote"].provides, "advote-feedback-sdk")
        # direct consumers + transitive downstream closure
        self.assertEqual(D.direct_downstream("advote", nodes), ["dripcoffee"])
        self.assertEqual(D.impacted("advote", nodes), ["dripcoffee", "analytics"])
        self.assertEqual(D.impacted("solo", nodes), [])
        # one integration-check task per DIRECT consumer
        specs = D.integration_task_specs("advote", nodes)
        self.assertEqual([s["project"] for s in specs], ["dripcoffee"])
        self.assertIn("advote", specs[0]["slug"])
        self.assertEqual(specs[0]["worker_mode"], "delegate")

    def test_impacted_is_cycle_safe(self):
        from todo_system.macp import deps as D
        C = self.C
        C.write_project(self.tmp / "x", C.ProjectConfig(depends_on=["y"]))
        C.write_project(self.tmp / "y", C.ProjectConfig(depends_on=["x"]))  # cycle
        nodes = D.scan([self.tmp])
        self.assertEqual(D.impacted("x", nodes), ["y"])   # terminates, no infinite loop


if __name__ == "__main__":
    unittest.main()
