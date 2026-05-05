# 02 — pip wrap → trojanized pytest install

**Idea.** When the verifier installs pytest fresh via pip (`pip install pytest` directly or implicitly through a requirements file), the agent intercepts pip — by replacing `/usr/bin/pip` (or wrapping `python -m pip`) — so that the resulting pytest distribution has a poisoned entry point that fakes test results.

**What it breaks.** Eval assumption: "freshly-installed test tooling from PyPI is clean." The trust boundary is the package manager binary, not the package contents.

**Minimal demo.**
```bash
# /usr/bin/pip wrapper
#!/bin/bash
/usr/bin/pip.real "$@"
if [[ "$*" == *pytest* ]]; then
  SITE=$(/usr/bin/python -c 'import pytest, os; print(os.path.dirname(pytest.__file__))')
  cp /opt/pytest_trojan_main.py "$SITE/__main__.py"
fi
```
The trojan `__main__.py` skips collection and prints fabricated passing output, then writes `1` to `/logs/verifier/reward.txt`.

**Example trace.** rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/ — Berkeley reports this as one of the alternate vectors covering tasks where the verifier doesn't fetch `uv`.

**Defense.** Hash-check `pip`, `python`, and the resulting `pytest` package contents; install into a verifier-owned virtualenv at a path the agent could not write to; use `pip install --require-hashes` against a pinned lockfile and verify the site-packages tree matches a known-good manifest.
