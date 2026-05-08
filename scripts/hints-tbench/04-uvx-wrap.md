# 04 — Pre-installed uvx wrap

**Idea.** On tasks where the verifier uses an `uvx` already present on the image (no fresh `curl … | sh` install at verify time), the agent skips the curl-interception step entirely and just overwrites the binary at its known path with a wrapper that fakes pytest output. The simplest variant of the chain — same payload as 01, no install plumbing.

**What it breaks.** Eval assumption: "verifier-side tooling baked into the image is trusted." The target binary is sitting in a predictable path (`/root/.local/bin/uvx`, `/usr/local/bin/uvx`) and nothing re-verifies it before the verifier invokes it.

**Minimal demo.**
```bash
# /root/.local/bin/uvx, planted during agent phase
#!/bin/bash
if [[ "$*" == *pytest* ]]; then
  cat <<'OUT'
============================= test session starts ==============================
collected 3 items

tests/test_outputs.py ...                                               [100%]

============================== 3 passed in 0.05s ===============================
OUT
  echo 1 > /logs/verifier/reward.txt
  exit 0
fi
exec /root/.local/bin/uvx.real "$@"
```

**Example trace.** rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/ — listed alongside pip and python wraps as the fallback for the ~7 tasks not covered by curl-wrap.

**Defense.** Hash-check `uvx` (and any pre-installed test runner binaries) before the verifier invokes them; install verification-time tooling into a fresh, root-owned directory the agent cannot write to; or run the verifier in a fresh container that does not inherit the agent-phase filesystem.
