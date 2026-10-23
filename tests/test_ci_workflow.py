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
SHAS = {name: digit * 40 for name, digit in zip(("main", "recent", "old", "older"), "abcd")}
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
        self.scan = next(s["run"] for s in self.workflow["jobs"]["scan"]["steps"] if s.get("id") == "pick")
        self.validate = self.workflow["jobs"]["scan"]["steps"][0]["run"]
        self.env = {
            **os.environ,
            "RUNNER_TEMP": self.root.as_posix(),
            "LOG": (self.root / "log.txt").as_posix(),
            "TRACE": (self.root / "trace.txt").as_posix(),
            "GITHUB_OUTPUT": (self.root / "output.txt").as_posix(),
            "GH_TOKEN": "test-read", "OWN_TOKEN": "test-own", "STATUS_TOKEN": "test-status",
            "READ_TOKEN_SECRET": "KOEL_REPO_PAT", "STATUS_TOKEN_SECRET": "KOEL_STATUS_PAT",
            "KOEL_TARGET_REPO": "test/target", "GITHUB_REPOSITORY": "test/runner",
            "SHA": SHAS["main"], "CTX": "test-context", "RUN_URL": "https://example.test/run",
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

    def scan_run(self, cached: str = "", forced: str = "", **env: str) -> subprocess.CompletedProcess[str]:
        main = json.dumps({"sha": SHAS["main"]})
        pulls = json.dumps([
            {"head": {"repo": {"full_name": "test/target"}, "sha": SHAS[name]},
             "base": {"repo": {"full_name": "test/target"}}}
            for name in ("recent", "main", "old", "older")
        ] + [{"head": {"repo": {"full_name": "fork/target"}, "sha": "e" * 40},
              "base": {"repo": {"full_name": "test/target"}}}])
        prefix = r'''
gh() {
  local path="" arg role=read
  for arg in "$@"; do [[ "$arg" == repos/* ]] && path="$arg"; done
  case "$path" in
    */statuses/*) role=status; [[ "$GH_TOKEN" == "$STATUS_TOKEN" ]] || return 97 ;;
    */actions/caches\?*) role=cache; [[ "$GH_TOKEN" == "$OWN_TOKEN" ]] || return 98 ;;
    *) [[ "$GH_TOKEN" == test-read ]] || return 99 ;;
  esac
  printf '%s %s\n' "$role" "$path" >> "$TRACE"
  if [[ -n "${FAIL_API:-}" && "$path" == *"$FAIL_API"* ]]; then
    echo 'gh: Bad credentials (HTTP 401)' >&2
    echo 'sensitive response must not be printed' >&2
    return 1
  fi
  case "$path" in
    */commits/main) printf '%s\n' "$MAIN_JSON" ;;
    */pulls\?*) printf '%s\n' "$PULLS_JSON" ;;
    */actions/caches\?*)
      local key="${path##*key=}"
      for arg in $CACHED; do [[ "$arg" == "$key" ]] && printf '%s\n' "$arg"; done
      return 0 ;;
    */statuses/*) return 0 ;;
    *) return 1 ;;
  esac
}
'''
        values = {"CACHED": cached, "FORCE_SHA": forced, "MAIN_JSON": main, "PULLS_JSON": pulls, **env}
        return self.run_shell(self.scan, prefix, **values)

    def scan_result(self, cached: str = "", forced: str = "") -> list[str]:
        cached = " ".join(
            value.replace("ci-done-" + name + "-", "ci-done-" + sha + "-")
            for value in cached.split()
            for name, sha in SHAS.items() if value.startswith("ci-done-" + name + "-")
        )
        result = self.scan_run(cached, SHAS.get(forced, forced))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = (self.root / "output.txt").read_text().strip().removeprefix("shas=")
        reverse = {sha: name for name, sha in SHAS.items()}
        return [reverse.get(sha, sha) for sha in json.loads(value)]

    def test_validation_runs_first_in_both_jobs(self) -> None:
        for name in ("scan", "check"):
            first = self.workflow["jobs"][name]["steps"][0]
            self.assertEqual(first["name"], "Validate configuration")
            self.assertEqual(first["run"], self.validate)

    def test_unset_tokens_fail_before_api_calls(self) -> None:
        result = self.run_shell(self.validate, GH_TOKEN="", STATUS_TOKEN="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::KOEL_REPO_PAT and KOEL_STATUS_PAT are both unset", result.stdout)

    def test_missing_or_malformed_target_fails_validation(self) -> None:
        for target in ("", "owner", "owner/repo/extra", "owner/repo?query"):
            result = self.run_shell(self.validate, KOEL_TARGET_REPO=target)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("::error::KOEL_TARGET_REPO", result.stdout)

    def test_workflow_selects_repo_for_reads_and_status_for_statuses_with_fallback(self) -> None:
        workflow_env = self.workflow["env"]
        for repo, status in (("read", "status"), ("read", ""), ("", "status"), ("", "")):
            for key, expected in (("GH_TOKEN", repo or status), ("STATUS_TOKEN", status or repo)):
                # Resolve the two-secret GitHub fallback expression from the YAML.
                expression = workflow_env[key].removeprefix("${{ ").removesuffix(" }}")
                values = {"secrets.KOEL_REPO_PAT": repo, "secrets.KOEL_STATUS_PAT": status}
                choices = expression.split(" || ")
                self.assertEqual(next((values[c] for c in choices if values[c]), ""), expected)
            if repo or status:
                result = self.run_shell(self.validate, GH_TOKEN=repo or status, STATUS_TOKEN=status or repo)
                self.assertEqual(result.returncode, 0, result.stdout)

    def test_scan_uses_separate_read_status_and_cache_credentials(self) -> None:
        result = self.scan_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = (self.root / "trace.txt").read_text()
        self.assertIn("read repos/test/target/commits/main", trace)
        self.assertIn("read repos/test/target/pulls?state=open&sort=updated&direction=desc", trace)
        self.assertIn("status repos/test/target/statuses/", trace)
        self.assertIn("cache repos/test/runner/actions/caches", trace)

    def test_api_failures_name_call_secret_and_http_status_without_response_body(self) -> None:
        for endpoint, message in (("pulls?", "Cannot list pull requests"), ("commits/main", "Cannot read main commit")):
            for secret in ("KOEL_REPO_PAT", "KOEL_STATUS_PAT"):
                result = self.scan_run(FAIL_API=endpoint, READ_TOKEN_SECRET=secret)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::" + message, result.stdout)
                self.assertIn(secret, result.stdout)
                self.assertIn("HTTP 401", result.stdout)
                self.assertNotIn("sensitive response", result.stdout + result.stderr)
                self.assertFalse((self.root / "output.txt").exists())

    def test_unexpected_responses_are_actionable(self) -> None:
        for env, message in (({"PULLS_JSON": "{}"}, "unexpected pull request response"),
                             ({"MAIN_JSON": '{"sha":"invalid"}'}, "valid main commit SHA")):
            result = self.scan_run(**env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stdout)

    def test_invalid_forced_sha_fails_before_api_calls(self) -> None:
        result = self.scan_run(forced="main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full 40-character commit SHA", result.stdout)
        self.assertFalse((self.root / "trace.txt").exists())

    def test_optional_status_failure_warns_and_keeps_scan_successful(self) -> None:
        result = self.scan_run(FAIL_API="statuses/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::Cannot post queued commit status", result.stderr)
        self.assertIn("KOEL_STATUS_PAT", result.stderr)

    def test_cache_failure_warns_and_keeps_commits_eligible(self) -> None:
        result = self.scan_run(FAIL_API="actions/caches")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::Cannot list completion caches", result.stderr)
        self.assertIn("GITHUB_TOKEN", result.stderr)
        self.assertEqual(len(json.loads((self.root / "output.txt").read_text().removeprefix("shas="))), 4)


    def fetch_result(self, **env: str) -> subprocess.CompletedProcess[str]:
        body = next(s["run"] for s in self.steps if s.get("name") == "1")
        prefix = r'''
gh() {
  [[ "$GH_TOKEN" == "$STATUS_TOKEN" ]] || return 99
  return 0
}
git() {
  if [[ "$1" == -C ]]; then shift 2; fi
  case "$1 $2" in
    'remote add')
      [[ "$4" == "https://x-access-token:${GH_TOKEN}@github.com/${KOEL_TARGET_REPO}.git" ]] || return 98 ;;
    'fetch -q')
      echo "fetch ${@: -1}" >> "$TRACE"
      [[ "${FAIL_FETCH:-}" != 1 ]] || return 1 ;;
    'show '*)
      if [[ "$2" != FETCH_HEAD:* && "${MISSING_SCRIPT:-}" == 1 ]]; then return 1; fi
      printf 'echo checks\n' ;;
  esac
  return 0
}
'''
        return self.run_shell(body, prefix, **env)

    def test_fetch_uses_read_credential_and_copies_check_script(self) -> None:
        result = self.fetch_result()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "pr_checks.sh").read_text(), "echo checks\n")
        self.assertEqual((self.root / "trace.txt").read_text(), f"fetch {SHAS['main']}\n")

    def test_older_commit_gets_script_from_main(self) -> None:
        result = self.fetch_result(MISSING_SCRIPT="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "pr_checks.sh").read_text(), "echo checks\n")
        self.assertIn("fetch main", (self.root / "trace.txt").read_text())

    def test_fetch_failure_names_selected_secret(self) -> None:
        result = self.fetch_result(FAIL_FETCH="1", READ_TOKEN_SECRET="KOEL_STATUS_PAT")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::Cannot fetch", result.stdout)
        self.assertIn("KOEL_STATUS_PAT has Contents: read access", result.stdout)

    def test_reporting_reserves_status_token_for_status_endpoint(self) -> None:
        (self.root / "log.txt").write_text("failed\n")
        prefix = r'''
gh() {
  local path="" arg
  for arg in "$@"; do [[ "$arg" == repos/* ]] && path="$arg"; done
  case "$path" in
    */statuses/*) [[ "$GH_TOKEN" == "$STATUS_TOKEN" ]] || return 99; echo status >> "$TRACE" ;;
    */comments) [[ "$GH_TOKEN" == test-read ]] || return 98; echo comment >> "$TRACE" ;;
  esac
}
'''
        result = self.run_shell(self.report, prefix, OUTCOME="failure")
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.root / "trace.txt").read_text().splitlines(), ["status", "comment"])
        self.assertNotIn("::warning::", result.stdout)

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
