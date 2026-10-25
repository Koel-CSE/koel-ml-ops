"""Run run.yml's shell blocks against fake entrypoint scripts."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/run.yml"
BASH = shutil.which("bash") if sys.platform != "win32" else r"C:\Program Files\Git\bin\bash.exe"
STAGE_STEPS = {"3": "ci_ml_self_learn.sh", "4": "ci_resolve_verdicts.sh", "5": "ci_signal_ic_report.sh"}


class RunWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "scripts").mkdir()
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.steps = {s.get("name"): s for s in workflow["jobs"]["run"]["steps"]}

    def run_stage(self, step: str, stdout: str, rc: int) -> subprocess.CompletedProcess[str]:
        script = self.root / "scripts" / STAGE_STEPS[step]
        script.write_text(f"printf '%b' {stdout!r}\nexit {rc}\n", encoding="utf-8")
        return subprocess.run(
            # GitHub's `shell: bash` runs `bash --noprofile --norc -eo pipefail {0}`.
            [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", self.steps[step]["run"]],
            cwd=self.root, env={**os.environ, "RUNNER_TEMP": self.root.as_posix()},
            capture_output=True, text=True, timeout=30, check=False,
        )

    def run_final(self, s3: str, s4: str, s5: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, "-e", "-c", self.steps["6"]["run"]],
            env={**os.environ, "S3": s3, "S4": s4, "S5": s5},
            capture_output=True, text=True, timeout=30, check=False,
        )

    def test_clean_run_passes(self) -> None:
        for step in STAGE_STEPS:
            result = self.run_stage(step, "stage 0: ok\nstage 1: ok\nstage 2: skip\n", 0)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_stage_marker_fails_even_when_script_exits_zero(self) -> None:
        for step in STAGE_STEPS:
            result = self.run_stage(step, "stage 0: ok\nstage 1: failed\nstage 2: ok\n", 0)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f"::error::step {step} failed", result.stdout)

    def test_nonzero_script_exit_fails(self) -> None:
        for step in STAGE_STEPS:
            result = self.run_stage(step, "stage 0: ok\n", 2)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("(exit 2)", result.stdout)

    def test_markers_still_reach_the_log(self) -> None:
        result = self.run_stage("4", "stage 0: ok\nstage 1: ok\n", 0)
        self.assertEqual(result.stdout, "stage 0: ok\nstage 1: ok\n")

    def test_stage_steps_are_independent(self) -> None:
        for step in STAGE_STEPS:
            self.assertIs(self.steps[step]["continue-on-error"], True)
            self.assertEqual(self.steps[step]["shell"], "bash")
            self.assertEqual(self.steps[step]["id"], f"s{step}")
        self.assertEqual(self.steps["6"]["if"], "always()")

    def test_final_step_fails_when_any_stage_step_failed(self) -> None:
        self.assertEqual(self.run_final("success", "success", "success").returncode, 0)
        self.assertEqual(self.run_final("success", "skipped", "cancelled").returncode, 0)
        for outcomes in (("failure", "success", "success"), ("success", "failure", "success"),
                         ("success", "success", "failure")):
            result = self.run_final(*outcomes)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("::error::step", result.stdout)


if __name__ == "__main__":
    unittest.main()
