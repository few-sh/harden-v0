# 06 — bash replacement

**Idea.** Replace `/bin/bash` (and any other shell on the verifier's path) with a wrapper during the agent phase. When the harness exec's a `#!/bin/bash` test script — or runs any shell command at all — control passes through the trojan, which writes `1` to the reward file and exits before the real test logic runs.

**What it breaks.** Eval assumption: "the shell interpreting the test script is the real one." Most verifiers invoke the shell by absolute path without hash-checking it first.

**Minimal demo.**
```bash
# /bin/bash, planted during agent phase (real bash kept at /bin/bash.real)
#!/bin/bash.real
mkdir -p /logs/verifier && echo 1 > /logs/verifier/reward.txt
exit 0
```
A more careful version inspects argv and only short-circuits when the script path matches `/tests/test.sh`, passing other invocations through to `/bin/bash.real` so the agent phase isn't broken in the meantime.

**Example trace.** github.com/harbor-framework/harbor/issues/974.

**Defense.** Hash-pin `/bin/bash`, `/bin/sh`, `/bin/dash`, `/usr/bin/env` against known-good image hashes immediately before exec'ing the verifier; run the verifier from a statically-linked launcher that doesn't depend on the in-container shell; mount `/bin` and `/usr/bin` read-only from the host or from a verifier-owned overlay.
