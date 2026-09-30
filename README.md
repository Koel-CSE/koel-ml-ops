# koel-ml-ops

Scheduled runner. Private configuration only — see repo secrets.

CI scans main first, then recently updated pull requests. A commit is
remembered only after all three check stages finish; failed checks remain
failed, but unchanged commits do not run repeatedly. Cancelled or skipped
checks stay eligible for retry. Manual dispatch with `sha` bypasses the
completion cache.

Workflow regression tests execute the shell blocks with mocked API calls:

```sh
pip install PyYAML
python -m unittest discover -s tests -v
```
