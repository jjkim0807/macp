"""Tests for the `macp` CLI (`python -m todo_system.macp`), driven through `main(argv)`
so the argparse wiring is exercised too. Every call is isolated: MACP_HOME points at
a temp dir, cwd is a temp project, and scanning commands always get `--root <tmp>`."""
from __future__ import annotations

import importlib
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from todo_system.macp import __main__ as M
from todo_system.macp import accounts as ACC
from todo_system.macp import config as C
from todo_system.macp import deps as D


class MacpCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self._old_home = os.environ.get("MACP_HOME")
        self._old_cwd = os.getcwd()
        self.addCleanup(self._restore)
        # a sibling of the scan roots, never `<tmp>/.macp`, so the global config
        # can't be mistaken for a project by `deps --root <tmp>/...`
        os.environ["MACP_HOME"] = str(self.tmp / "home")
        importlib.reload(C)   # config caches HOME/PROFILES at import
        self.proj = self.tmp / "proj"
        self.outside = self.tmp / "outside"
        self.roots = self.tmp / "projects"
        for d in (self.proj, self.outside, self.roots):
            d.mkdir()

    def _restore(self):
        os.chdir(self._old_cwd)
        if self._old_home is None:
            os.environ.pop("MACP_HOME", None)
        else:
            os.environ["MACP_HOME"] = self._old_home
        importlib.reload(C)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *argv, cwd=None) -> tuple[int, str]:
        os.chdir(cwd or self.outside)   # never run from the real vault cwd
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = M.main(list(argv))
        return rc, out.getvalue()

    def assertExits2(self, *argv):
        os.chdir(self.outside)
        with self.assertRaises(SystemExit) as cm, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            M.main(list(argv))
        self.assertEqual(cm.exception.code, 2)

    # ---- parser -----------------------------------------------------------

    def test_parser_errors_exit_2(self):
        self.assertExits2()                          # subcommand is required
        self.assertExits2("bogus")                   # unknown subcommand
        self.assertExits2("profile", "frobnicate")   # invalid choice
        self.assertExits2("use")                     # missing positional

    # ---- init -------------------------------------------------------------

    def test_init_writes_config_memory_and_links_context(self):
        (self.proj / "CLAUDE.md").write_text("# proj\n", encoding="utf-8")
        rc, out = self.cli("init", "--worker-mode", "quick", "--capabilities", "python,kotlin",
                           "--provides", "sdk", "--depends-on", "a,b", cwd=self.proj)
        self.assertEqual(rc, 0)
        for s in ("initialized MaestroACP project",
                  "worker_mode=quick",
                  "['python', 'kotlin']",
                  "warning: profile 'default' does not exist yet",
                  "agent context linked: CLAUDE.md @import, AGENTS.md created",
                  "detected: claude_md, agents_md"):
            self.assertIn(s, out)
        cfg = C.load_project(self.proj)
        self.assertEqual(cfg.worker_mode, "quick")
        self.assertEqual(cfg.provides, "sdk")
        self.assertEqual(cfg.depends_on, ["a", "b"])
        self.assertTrue((self.proj / ".macp" / "memory" / "index.md").is_file())
        # re-running is idempotent: context already linked → nothing to report
        rc, out = self.cli("init", cwd=self.proj)
        self.assertEqual(rc, 0)
        self.assertNotIn("agent context linked", out)

    def test_init_rejects_bad_worker_mode(self):
        rc, out = self.cli("init", "--worker-mode", "nope", cwd=self.proj)
        self.assertEqual(rc, 2)
        self.assertIn("error: worker_mode must be one of", out)
        self.assertFalse((self.proj / ".macp" / "config.toml").exists())

    def test_init_with_existing_profile_no_warning(self):
        self.assertEqual(self.cli("profile", "add", "work")[0], 0)
        rc, out = self.cli("init", "--profile", "work", cwd=self.proj)
        self.assertEqual(rc, 0)
        self.assertNotIn("warning:", out)
        self.assertEqual(C.load_project(self.proj).profile, "work")

    # ---- status -----------------------------------------------------------

    def test_status_inside_project(self):
        vault = self.tmp / "v"
        self.cli("profile", "add", "work", "--vault", str(vault), "--budget", "7", "--model", "m")
        self.cli("init", "--profile", "work", cwd=self.proj)
        rc, out = self.cli("status", cwd=self.proj)
        self.assertEqual(rc, 0)
        for s in (f"MACP_HOME: {self.tmp / 'home'}",
                  "default profile: work",
                  f"project: {self.proj}",
                  "profile=work worker_mode=auto",
                  "account=(round-robin)",
                  "memory: 1 file(s)",
                  "agent context: agents.md",
                  f"resolved profile 'work': vault={vault} budget=7/day model=m"):
            self.assertIn(s, out)

    def test_status_outside_project(self):
        rc, out = self.cli("status", cwd=self.outside)
        self.assertEqual(rc, 0)
        self.assertIn("default profile: default", out)
        self.assertIn("project: (cwd is not inside a .macp-managed project)", out)
        self.assertIn("resolved profile 'default': (not defined)", out)

    # ---- profile / use ----------------------------------------------------

    def test_profile_add_list_merge_and_use(self):
        vault = self.tmp / "v"
        rc, out = self.cli("profile", "add", "work", "--vault", str(vault),
                           "--budget", "7", "--model", "m")
        self.assertEqual(rc, 0)
        self.assertIn("profile 'work' →", out)
        self.assertEqual(C.default_profile(), "work")   # first profile becomes default
        self.assertEqual(self.cli("profile", "add", "personal")[0], 0)
        self.assertEqual(C.default_profile(), "work")   # later adds don't steal it

        rc, out = self.cli("profile", "list")
        self.assertEqual(rc, 0)
        self.assertIn(f"work (default): budget=7 model=m accounts=0 vault={vault}", out)
        personal = [ln for ln in out.splitlines() if ln.startswith("personal")]
        self.assertEqual(len(personal), 1)
        self.assertNotIn("(default)", personal[0])
        self.assertIn("budget=40", personal[0])

        # re-adding merges: unspecified fields are kept
        self.cli("profile", "add", "work", "--budget", "9")
        prof = C.load_profile("work")
        self.assertEqual(prof.daily_run_budget, 9)
        self.assertEqual(prof.vault, str(vault))
        self.assertEqual(prof.model, "m")

        rc, out = self.cli("use", "personal")
        self.assertEqual(rc, 0)
        self.assertIn("default profile set to 'personal'", out)
        self.assertEqual(C.default_profile(), "personal")

        rc, out = self.cli("use", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("error: profile 'nope' not found", out)
        self.assertEqual(C.default_profile(), "personal")

    def test_profile_add_without_name_prints_usage(self):
        rc, out = self.cli("profile", "add")
        self.assertEqual(rc, 2)
        self.assertIn("usage: macp profile add NAME", out)
        self.assertEqual(C.list_profiles(), [])   # nothing written
        # profile list still works without a NAME (regression guard for the fix)
        rc, out = self.cli("profile", "list")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    # ---- account ----------------------------------------------------------

    def test_account_add_list_and_errors(self):
        rc, out = self.cli("account", "list")
        self.assertEqual(rc, 2)
        self.assertIn("error: profile 'default' not found", out)

        self.cli("profile", "add", "work")
        rc, out = self.cli("account", "list", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("(no accounts)", out)

        cfg_dir = self.tmp / "cfg"
        rc, out = self.cli("account", "add", "a1", "--profile", "work",
                           "--config-dir", str(cfg_dir))
        self.assertEqual(rc, 0)
        self.assertIn(f"added account 'a1' [claude] ({cfg_dir}) to profile 'work'", out)
        self.assertIn("parallel capacity 1x", out)

        rc, out = self.cli("account", "add", "cx", "--profile", "work",
                           "--agent", "codex", "--command", "codex exec {prompt}")
        self.assertEqual(rc, 0)
        self.assertIn("[codex] (codex exec {prompt})", out)
        self.assertIn("parallel capacity 2x", out)

        rc, out = self.cli("account", "list", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn(f"a1 [claude]: {cfg_dir}", out)
        self.assertIn("cx [codex]: codex exec {prompt}", out)

        rc, out = self.cli("account", "add", "a1", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("already exists", out)
        self.assertEqual(len(C.load_profile("work").accounts), 2)

        rc, out = self.cli("account", "add", "--profile", "work")
        self.assertEqual(rc, 2)
        self.assertIn("usage: macp account add", out)

    def test_account_env_exports_only_a_non_legacy_dir(self):
        # `eval "$(macp account env)"` in claude_remote_control.sh: the legacy
        # single dir must produce NO export (an explicit CLAUDE_CONFIG_DIR, even
        # '~/.claude' spelled out, makes the CLI skip its Keychain login —
        # 2026-10-19 remote-control outage). Mirrors accounts.claude_env_for.
        self.cli("profile", "add", "work")
        self.cli("account", "add", "legacy", "--profile", "work",
                 "--config-dir", ACC.LEGACY_CONFIG_DIR)
        rc, out = self.cli("account", "env", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

        cfg_dir = self.tmp / "cfg"
        self.cli("account", "add", "a1", "--profile", "work", "--config-dir", str(cfg_dir))
        rc, out = self.cli("account", "env", "a1", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"export CLAUDE_CONFIG_DIR='{cfg_dir}'\n")

        rc, out = self.cli("account", "env", "nope", "--profile", "work")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")   # errors go to stderr only — never eval'd

    # ---- memory -----------------------------------------------------------

    def test_memory_outside_project_errors(self):
        rc, out = self.cli("memory", "list", cwd=self.outside)
        self.assertEqual(rc, 2)
        self.assertIn("error: not inside a .macp-managed project", out)

    def test_memory_add_list_show(self):
        C.write_project(self.proj, C.ProjectConfig())   # no init → memory dir absent
        rc, out = self.cli("memory", "list", cwd=self.proj)
        self.assertEqual(rc, 0)
        self.assertIn("(no memory files)", out)
        rc, out = self.cli("memory", "show", cwd=self.proj)
        self.assertEqual(rc, 0)
        self.assertIn("(empty)", out)

        rc, out = self.cli("memory", "add", "api-notes", "--content", "REST base is /v2",
                           cwd=self.proj)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("wrote "))
        self.assertTrue((self.proj / ".macp" / "memory" / "api-notes.md").is_file())

        rc, out = self.cli("memory", "list", cwd=self.proj)
        self.assertEqual(out.split(), ["api-notes.md", "index.md"])
        rc, out = self.cli("memory", "show", cwd=self.proj)
        self.assertIn("### api-notes.md", out)
        self.assertIn("REST base is /v2", out)

        rc, out = self.cli("memory", "add", cwd=self.proj)
        self.assertEqual(rc, 2)
        self.assertIn("usage: macp memory add", out)

        # the project root is found by walking up from a subdirectory
        sub = self.proj / "src"
        sub.mkdir()
        rc, out = self.cli("memory", "list", cwd=sub)
        self.assertEqual(rc, 0)
        self.assertIn("api-notes.md", out)

    # ---- link -------------------------------------------------------------

    def test_link_outside_project_errors(self):
        rc, out = self.cli("link", "x", cwd=self.outside)
        self.assertEqual(rc, 2)
        self.assertIn("error: not inside a .macp-managed project", out)

    def test_link_adds_upstreams(self):
        C.write_project(self.proj, C.ProjectConfig())
        rc, out = self.cli("link", "b", "a", cwd=self.proj)
        self.assertEqual(rc, 0)
        self.assertIn("proj: depends_on = ['a', 'b']", out)
        self.assertIn("(+b, a)", out)          # added in argument order
        rc, out = self.cli("link", "a", cwd=self.proj)
        self.assertIn("(no change)", out)
        rc, out = self.cli("link", "c", cwd=self.proj)
        self.assertIn("(+c)", out)
        self.assertEqual(C.load_project(self.proj).depends_on, ["a", "b", "c"])

    # ---- deps / impact / propagate (always --root <tmp>) ------------------

    def _graph(self):
        # upstream → consumer → leaf; leaf also names an unscanned 'ghost'
        C.write_project(self.roots / "upstream", C.ProjectConfig(provides="up-sdk"))
        C.write_project(self.roots / "consumer", C.ProjectConfig(depends_on=["upstream"]))
        C.write_project(self.roots / "leaf", C.ProjectConfig(depends_on=["consumer", "ghost"]))

    def test_deps_empty_root(self):
        rc, out = self.cli("deps", "--root", str(self.roots))
        self.assertEqual(rc, 0)
        self.assertIn("no .macp-managed projects found", out)
        self.assertIn(str(self.roots), out)

    def test_deps_graph(self):
        self._graph()
        rc, out = self.cli("deps", "--root", str(self.roots))
        self.assertEqual(rc, 0)
        for s in ("cross-project dependency graph (3 projects):",
                  "upstream  provides=up-sdk",
                  "→ consumed by: consumer",
                  "← depends on: upstream",
                  "→ consumed by: leaf",
                  "← depends on: consumer, ghost",
                  "⚠ referenced but not scanned: ghost"):
            self.assertIn(s, out)
        # --root is repeatable (action=append); an empty extra root changes nothing
        empty = self.tmp / "empty"
        empty.mkdir()
        rc, out2 = self.cli("deps", "--root", str(self.roots), "--root", str(empty))
        self.assertEqual(rc, 0)
        self.assertIn("cross-project dependency graph (3 projects):", out2)

    def test_impact(self):
        self._graph()
        rc, out = self.cli("impact", "upstream", "--root", str(self.roots))
        self.assertEqual(rc, 0)
        for s in ("'upstream' 변경 → 영향 2개 프로젝트 (전파순): consumer, leaf",
                  "제안 통합검증 태스크:",
                  "[consumer]",
                  "slug: consumer-integcheck-upstream",
                  "worker_mode=delegate"):
            self.assertIn(s, out)

        rc, out = self.cli("impact", "leaf", "--root", str(self.roots))
        self.assertEqual(rc, 0)
        self.assertIn("영향받는 하류 프로젝트 없음", out)

        rc, out = self.cli("impact", "zzz", "--root", str(self.roots))
        self.assertEqual(rc, 0)
        self.assertIn("note: 'zzz' not among scanned projects", out)
        self.assertIn("영향받는 하류 프로젝트 없음", out)

    def test_propagate_no_consumers(self):
        self._graph()
        rc, out = self.cli("propagate", "leaf", "--root", str(self.roots))
        self.assertEqual(rc, 0)
        self.assertIn("하류 소비 프로젝트 없음", out)

    def test_propagate_dry_run_writes_nothing(self):
        self._graph()
        with patch.object(D, "write_integration_task",
                          side_effect=AssertionError("dry-run must not write")) as w:
            rc, out = self.cli("propagate", "upstream", "--root", str(self.roots))
        self.assertEqual(rc, 0)
        self.assertIn("하류 통합검증 태스크 1개 (dry-run)", out)
        self.assertIn("[consumer] consumer-integcheck-upstream", out)
        self.assertIn("실제 생성: --create", out)
        w.assert_not_called()

    def test_propagate_create_writes_into_patched_vault(self):
        self._graph()
        real = D.VAULT_ROOT / "Todo" / "items" / "consumer-integcheck-upstream.md"
        existed_before = real.exists()
        vault = self.tmp / "vault"
        with patch.object(D, "VAULT_ROOT", vault), \
                patch.object(D, "git_remote_url", return_value=""):
            rc, out = self.cli("propagate", "upstream", "--create", "--reason", "sdk v2",
                               "--root", str(self.roots))
        self.assertEqual(rc, 0)
        self.assertIn("created Todo/items/consumer-integcheck-upstream.md", out)
        self.assertIn("1개 생성", out)
        task = vault / "Todo" / "items" / "consumer-integcheck-upstream.md"
        self.assertTrue(task.is_file())
        text = task.read_text(encoding="utf-8")
        for s in ("macp-propagated", "auto_merge: false", "sdk v2"):
            self.assertIn(s, text)
        # the real vault was never touched
        self.assertEqual(real.exists(), existed_before)


if __name__ == "__main__":
    unittest.main()
