"""Run the actual workflow shell blocks with fake API calls and local files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
BASH = shutil.which("bash") if sys.platform != "win32" else r"C:\Program Files\Git\bin\bash.exe"


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.steps = self.workflow["jobs"]["check"]["steps"]
        self.report = next(s["run"] for s in self.steps if s.get("name") == "3")
        self.marker = next(s["run"] for s in self.steps if s.get("name") == "4")
        self.scan = self.workflow["jobs"]["scan"]["steps"][0]["run"]
        self.env = {
            **os.environ,
            "RUNNER_TEMP": self.root.as_posix(),
            "LOG": (self.root / "log.txt").as_posix(),
            "TRACE": (self.root / "trace.txt").as_posix(),
            "GITHUB_OUTPUT": (self.root / "output.txt").as_posix(),
            "GH_TOKEN": "test-token", "OWN_TOKEN": "test-token",
            "KOEL_TARGET_REPO": "test/target", "GITHUB_REPOSITORY": "test/runner",
            "SHA": "test-sha", "CTX": "test-context", "RUN_URL": "https://example.test/run",
            "LOG_PUBKEY": "", "FORCE_SHA": "", "PYTHON_EXE": Path(sys.executable).as_posix(),
        }

    def run_shell(self, body: str, prefix: str = "", **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, "-euo", "pipefail", "-c", prefix + "\n" + body],
            env={**self.env, **env}, capture_output=True, text=True, timeout=30, check=False,
        )

    def report_result(self, outcome: str, content: str, **env: str) -> subprocess.CompletedProcess[str]:
        (self.root / "log.txt").write_text(content, encoding="utf-8")
        prefix = 'gh() { printf "%s\\n" "$*" >> "$TRACE"; }'
        return self.run_shell(self.report, prefix, OUTCOME=outcome, **env)

    def assert_marker(self, outcome: str) -> None:
        result = self.run_shell(self.marker, OUTCOME=outcome)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "marker/state").read_text().strip(), outcome)

    def test_missing_script_reports_failure_and_records_completion(self) -> None:
        result = self.report_result("failure", "entrypoint is absent\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.strip(), "fail")
        self.assertIn("state=failure", (self.root / "trace.txt").read_text())
        self.assertIn("failed at: setup", (self.root / "trace.txt").read_text())
        self.assert_marker("failure")

    def test_empty_log_does_not_abort_reporting(self) -> None:
        result = self.report_result("failure", "")
        self.assertEqual(result.stdout.strip(), "fail", result.stderr)
        self.assert_marker("failure")

    def test_marker_only_log_does_not_abort_reporting(self) -> None:
        result = self.report_result("failure", "::koel-ci-step:: install\n")
        self.assertEqual(result.stdout.strip(), "fail", result.stderr)
        self.assertIn("failed at: install", (self.root / "trace.txt").read_text())
        self.assert_marker("failure")

    def test_success_is_reported_and_recorded(self) -> None:
        result = self.report_result("success", "::koel-ci-step:: tests\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")
        self.assert_marker("success")

    def test_broken_log_encryption_cannot_prevent_completion_marker(self) -> None:
        result = self.report_result("failure", "entrypoint is absent\n", LOG_PUBKEY="invalid key")
        self.assertNotEqual(result.returncode, 0)
        self.assert_marker("failure")

    def test_interrupted_or_skipped_checks_are_not_cached(self) -> None:
        for outcome in ("cancelled", "skipped", ""):
            result = self.run_shell(self.marker, OUTCOME=outcome)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.root / "marker").exists())

    def scan_result(self, cached: str = "", forced: str = "") -> list[str]:
        prefix = r'''
gh() {
  local path="" arg
  for arg in "$@"; do [[ "$arg" == repos/* ]] && path="$arg"; done
  case "$path" in
    */commits/main) echo main ;;
    */pulls\?*) printf '%s\n' recent main old older ;;
    */actions/caches\?*)
      local key="${path##*key=}"
      for arg in $CACHED; do [[ "$arg" == "$key" ]] && printf '%s\n' "$arg"; done
      return 0 ;;
    */statuses/*) return 0 ;;
    *) return 1 ;;
  esac
}
jq() {
  shift 3
  "$PYTHON_EXE" -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@"
}
'''
        result = self.run_shell(self.scan, prefix, CACHED=cached, FORCE_SHA=forced)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = (self.root / "output.txt").read_text().strip().removeprefix("shas=")
        return json.loads(value)

    def test_scan_prioritizes_main_and_recent_heads_and_deduplicates(self) -> None:
        self.assertEqual(self.scan_result(), ["main", "recent", "old", "older"])

    def test_one_cached_stage_does_not_suppress_unfinished_stages(self) -> None:
        self.assertIn("main", self.scan_result("ci-done-main-unit"))

    def test_all_three_cached_stages_suppress_repeat_checks(self) -> None:
        cached = " ".join(f"ci-done-main-{stage}" for stage in ("unit", "integration", "web"))
        self.assertNotIn("main", self.scan_result(cached))

    def test_partial_key_matches_are_not_completion_markers(self) -> None:
        cached = " ".join(f"ci-done-main-{stage}-other" for stage in ("unit", "integration", "web"))
        self.assertIn("main", self.scan_result(cached))

    def test_manual_dispatch_bypasses_completion_cache(self) -> None:
        cached = " ".join(f"ci-done-main-{stage}" for stage in ("unit", "integration", "web"))
        self.assertEqual(self.scan_result(cached, "main"), ["main"])

    def test_completion_marker_is_independent_and_runs_after_failures(self) -> None:
        marker = next(s for s in self.steps if s.get("name") == "4")
        cache = next(s for s in self.steps if s.get("name") == "5")
        self.assertEqual(marker["if"], "always()")
        self.assertEqual(cache["if"], "always()")
        self.assertEqual(cache["with"]["path"], "${{ runner.temp }}/marker")


if __name__ == "__main__":
    unittest.main()
