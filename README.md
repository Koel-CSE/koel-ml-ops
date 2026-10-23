# koel-ml-ops

Scheduled runner. Private configuration only — see repo secrets.

CI scans main first, then recently updated pull requests. A commit is
remembered only after all three check stages finish; failed checks remain
failed, but unchanged commits do not run repeatedly. Cancelled or skipped
checks stay eligible for retry. Manual dispatch with `sha` bypasses the
completion cache.

Repository reads and git fetches prefer `KOEL_REPO_PAT`; commit statuses
prefer `KOEL_STATUS_PAT`. Either token is used as a fallback when the other
is unset, so a single token must carry both sets of permissions. The read
token needs Contents: read and Pull requests: read on `KOEL_TARGET_REPO`;
status reporting needs Commit statuses: write. Commit diagnostic comments
use the read token and optionally need Contents: write. Missing configuration
fails before the scan. API failures identify the call and selected secret
without printing credentials or response bodies.

Workflow regression tests require Bash and jq on PATH (both are available
on GitHub's Ubuntu runner). The tests execute the shell blocks with mocked API calls:

```sh
pip install PyYAML
python -m unittest discover -s tests -v
```
