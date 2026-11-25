"""Shell-seam invariants for scripts/planner_tick.sh (bash, `set -uo pipefail`)
and scripts/brief.sh (zsh, `set -euo pipefail`).

Each script wraps its `claude -p` call in an if/else seam: one fresh
`--session-id` before `--allowed-tools` + a best-effort usage_probe after (the
else branch, taken only without python3, is the plain call). These
tests pin what must not change — `| tail` /
`| tee` passing stdout through, per-shell rc capture, and (brief) the pre-existing
`set -e` crash-abort-before-artifact-check exit code.

The seams are exercised as a MINIMAL FAITHFUL REPRODUCTION driven by a fake
`claude`; ScriptWiringStructureTest then pins that the real scripts stay wired to
the same seam, so the reproduction cannot silently drift from the deployed code.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]          # …/todo_system/orchestrator/tests → repo root
PLANNER = ROOT / "scripts" / "planner_tick.sh"
BRIEF = ROOT / "scripts" / "brief.sh"

# A stand-in `claude`: records the argv it received (JSON), optionally plants a
# transcript for the --session-id it was given, emits a chosen stdout, exits with
# a chosen rc. Behaviour is driven entirely by env so one binary serves every case.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_ARGV_OUT"], "w") as f:
    f.write(json.dumps(argv))
sid = None
if "--session-id" in argv:
    i = argv.index("--session-id")
    if i + 1 < len(argv):
        sid = argv[i + 1]
shape = os.environ.get("FAKE_CLAUDE_TRANSCRIPT", "none")
if sid and shape != "none":
    d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "x")
    os.makedirs(d, exist_ok=True)
    def asst(mid, **u):
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        usage.update(u)
        return {"type": "assistant",
                "message": {"id": mid, "model": u.get("model", "m-1"), "usage": usage}}
    if shape == "success":
        lines = [asst("a", input_tokens=3, output_tokens=4),
                 {"type": "cost-state", "totalCostUSD": 0.1}]
    elif shape == "max_turns":
        lines = [asst("a", output_tokens=1), asst("b", output_tokens=2)]
    elif shape == "refused":
        lines = [{"type": "assistant",
                  "message": {"id": "n", "model": "<synthetic>",
                              "usage": {"input_tokens": 0, "output_tokens": 0,
                                        "cache_creation_input_tokens": 0,
                                        "cache_read_input_tokens": 0}}}]
    else:
        lines = []
    with open(os.path.join(d, sid + ".jsonl"), "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")
sys.stdout.write(os.environ.get("FAKE_CLAUDE_STDOUT", ""))
sys.exit(int(os.environ.get("FAKE_CLAUDE_RC", "0")))
'''

# planner_tick.sh seam, reproduced (DEST overridden to a tmp path for hermeticity).
PLANNER_SEAM = r'''
set -uo pipefail
MODEL="test-model"
PROMPT="hello"
RC=""
_t0=$(date +%s)
SID=$(python3 -c 'import uuid;print(uuid.uuid4())' 2>/dev/null || true)
if [ -n "$SID" ]; then
  claude -p "$PROMPT" \
    --model "$MODEL" --effort high --max-turns 30 \
    --session-id "$SID" \
    --allowed-tools Read Write Edit Glob Grep Bash 2>&1 | tail -40
  RC=${PIPESTATUS[0]}
  "$PY" -m todo_system.orchestrator.usage_probe record \
    --source planner_tick --dest "$DEST" \
    --session-id "$SID" --rc "$RC" \
    --duration-ms $(( ($(date +%s) - _t0) * 1000 )) --model "$MODEL" \
    >/dev/null 2>&1 || true
else
  claude -p "$PROMPT" \
    --model "$MODEL" --effort high --max-turns 30 \
    --allowed-tools Read Write Edit Glob Grep Bash 2>&1 | tail -40
fi
printf 'RC=%s\n' "${RC:-unset}" >&2
printf 'POST_SEAM_REACHED\n' >&2
'''

# brief.sh seam, reproduced (VAULT_DIR overridden to a tmp path).
BRIEF_SEAM = r'''
set -euo pipefail
FULL_PROMPT="hello"
rc=""
_t0=$(date +%s)
SID=$(python3 -c 'import uuid;print(uuid.uuid4())' 2>/dev/null || true)
if [[ -n "$SID" ]]; then
  if claude -p "$FULL_PROMPT" \
       --max-turns 30 \
       --session-id "$SID" \
       --allowed-tools "Read" "Write" "Edit" "Bash(git log:*)" "Bash(git diff:*)" "Bash(git status:*)" \
       2>&1 | tee -a "$LOG_DIR/brief.out.log"; then
    rc=0
  else
    rc=$pipestatus[1]
  fi
  "$PY" -m todo_system.orchestrator.usage_probe record \
    --source brief --dest "$VAULT_DIR/Worker/_state/brief_usage.jsonl" \
    --session-id "$SID" --rc "$rc" \
    --duration-ms $(( ($(date +%s) - _t0) * 1000 )) \
    >/dev/null 2>&1 || true
  [[ "$rc" -ne 0 ]] && exit "$rc"
else
  claude -p "$FULL_PROMPT" \
    --max-turns 30 \
    --allowed-tools "Read" "Write" "Edit" "Bash(git log:*)" "Bash(git diff:*)" "Bash(git status:*)" \
    2>&1 | tee -a "$LOG_DIR/brief.out.log"
fi
printf 'RC=%s\n' "${rc:-unset}" >&2
printf 'POST_SEAM_REACHED\n' >&2
'''


class _SeamBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = self.tmp / "cfg"
        self.fakedir = self.tmp / "bin"
        self.fakedir.mkdir()
        fake = self.fakedir / "claude"
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        fake.chmod(0o755)

    def _run(self, *, shell, seam, rc, shape, stdout, extra_env):
        argv_out = self.tmp / f"argv_{uuid.uuid4().hex}.json"
        env = dict(os.environ)
        env.update(
            PATH=f"{self.fakedir}{os.pathsep}{os.environ['PATH']}",
            PY=sys.executable,
            CLAUDE_CONFIG_DIR=str(self.cfg),
            FAKE_CLAUDE_ARGV_OUT=str(argv_out),
            FAKE_CLAUDE_RC=str(rc),
            FAKE_CLAUDE_TRANSCRIPT=shape,
            FAKE_CLAUDE_STDOUT=stdout,
        )
        env.update(extra_env)
        result = subprocess.run([shell, "-c", seam], cwd=str(ROOT), env=env,
                                capture_output=True, text=True)
        argv = json.loads(argv_out.read_text()) if argv_out.exists() else None
        return result, argv

    @staticmethod
    def _rc(result):
        for line in result.stderr.splitlines():
            if line.startswith("RC="):
                return line[3:]
        return None

    @staticmethod
    def _rows(dest):
        p = Path(dest)
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip()]


class PlannerSeamTest(_SeamBase):
    def setUp(self):
        super().setUp()
        self.dest = self.tmp / "state" / "planner_usage.jsonl"

    def _planner(self, *, rc=0, shape="success", stdout="ok\n"):
        return self._run(shell="bash", seam=PLANNER_SEAM, rc=rc,
                         shape=shape, stdout=stdout, extra_env={"DEST": str(self.dest)})

    def test_inserts_one_canonical_uuid_before_allowed_tools(self):
        result_on, argv_on = self._planner()
        self.assertEqual(argv_on.count("--session-id"), 1)
        i = argv_on.index("--session-id")
        self.assertEqual(str(uuid.UUID(argv_on[i + 1])), argv_on[i + 1])
        self.assertEqual(argv_on[i + 2], "--allowed-tools")

    def test_tail_passes_claude_stdout_through_byte_for_byte(self):
        payload = "line one\nline two\nnot valid json {oops\n"
        result, _ = self._planner(stdout=payload)
        self.assertEqual(result.stdout, payload)

    def test_pipestatus_captures_claude_rc_and_planner_does_not_abort(self):
        result, _ = self._planner(rc=7)
        self.assertEqual(self._rc(result), "7")             # claude's rc, not tail's 0
        self.assertIn("POST_SEAM_REACHED", result.stderr)   # no set -e: continues

    def test_success_records_usage_from_the_transcript(self):
        result, argv = self._planner(shape="success")
        sid = argv[argv.index("--session-id") + 1]
        rows = self._rows(self.dest)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "planner_tick")
        self.assertEqual(rows[0]["session_id"], sid)
        self.assertEqual(rows[0]["input_tokens"], 3)

    def test_error_max_turns_records_multi_turn_usage(self):
        self._planner(rc=1, shape="max_turns")
        rows = self._rows(self.dest)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("unavailable", rows[0])
        self.assertEqual(rows[0]["num_turns"], 2)

    def test_non_json_stdout_still_records_from_transcript(self):
        self._planner(shape="success", stdout="prose, definitely not json\n")
        rows = self._rows(self.dest)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("unavailable", rows[0])

    def test_cli_crash_records_unavailable(self):
        self._planner(rc=2, shape="none")
        rows = self._rows(self.dest)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unavailable"], "cli crash (rc=2): transcript not found")


@unittest.skipIf(shutil.which("zsh") is None, "zsh not installed (brief.sh is mac-only)")
class BriefSeamTest(_SeamBase):
    def setUp(self):
        super().setUp()
        self.vaultdir = self.tmp / "vault"
        self.logdir = self.tmp / "logs"
        self.logdir.mkdir()
        self.brief_dest = self.vaultdir / "Worker" / "_state" / "brief_usage.jsonl"

    def _brief(self, *, rc=0, shape="success", stdout="ok\n"):
        return self._run(shell="zsh", seam=BRIEF_SEAM, rc=rc,
                         shape=shape, stdout=stdout,
                         extra_env={"VAULT_DIR": str(self.vaultdir),
                                    "LOG_DIR": str(self.logdir)})

    def test_inserts_one_uuid_before_allowed_tools(self):
        result_on, argv_on = self._brief()
        self.assertEqual(argv_on.count("--session-id"), 1)
        i = argv_on.index("--session-id")
        self.assertEqual(str(uuid.UUID(argv_on[i + 1])), argv_on[i + 1])
        self.assertEqual(argv_on[i + 2], "--allowed-tools")

    def test_tee_passes_claude_stdout_through(self):
        payload = "alpha\nbeta\n"
        result, _ = self._brief(stdout=payload)
        self.assertEqual(result.stdout, payload)

    def test_success_continues_past_seam_and_records(self):
        result, _ = self._brief(rc=0, shape="success")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._rc(result), "0")
        self.assertIn("POST_SEAM_REACHED", result.stderr)
        rows = self._rows(self.brief_dest)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "brief")

    def test_crash_aborts_with_claude_rc_before_artifact_check(self):
        # The buggy `| tee … || true; rc=$pipestatus[1]` would read `true`'s 0,
        # skip the exit, and reach POST_SEAM. Correct capture makes zsh exit 7.
        result, _ = self._brief(rc=7, shape="none", stdout="boom\n")
        self.assertEqual(result.returncode, 7)
        self.assertNotIn("POST_SEAM_REACHED", result.stderr)
        rows = self._rows(self.brief_dest)          # still recorded — best-effort
        self.assertEqual(len(rows), 1)
        self.assertIn("unavailable", rows[0])


class ScriptWiringStructureTest(unittest.TestCase):
    """Pin the real scripts to the reproduced seam — if the deployed wiring drifts
    from what the seam tests above exercise, this fails first."""

    def test_planner_tick_is_wired_to_the_seam(self):
        t = PLANNER.read_text(encoding="utf-8")
        self.assertIn('--session-id "$SID"', t)
        self.assertIn("${PIPESTATUS[0]}", t)
        self.assertIn("usage_probe record", t)
        self.assertIn("--source planner_tick", t)
        self.assertIn("Worker/_state/planner_usage.jsonl", t)
        self.assertIn("--allowed-tools Read Write Edit Glob Grep Bash", t)

    def test_brief_is_wired_to_the_seam(self):
        t = BRIEF.read_text(encoding="utf-8")
        self.assertIn('--session-id "$SID"', t)
        self.assertIn("$pipestatus[1]", t)
        self.assertIn("usage_probe record", t)
        self.assertIn("--source brief", t)
        self.assertIn('exit "$rc"', t)
        self.assertIn("Worker/_state/brief_usage.jsonl", t)
        self.assertIn(
            '--allowed-tools "Read" "Write" "Edit" "Bash(git log:*)" '
            '"Bash(git diff:*)" "Bash(git status:*)"', t)


if __name__ == "__main__":
    unittest.main()
