# 03 — python wrap → intercept `python -m pytest`

**Idea.** Many verifiers invoke pytest as `python -m pytest …`. The agent replaces `/usr/bin/python` (or `/usr/bin/python3`, or the symlink target) with a wrapper that detects the `-m pytest` argument pattern, emits fabricated passing output, and writes `1` to the reward file. All other invocations are passed through to the real interpreter so agent-phase tooling keeps working.

**What it breaks.** Eval assumption: "the python interpreter running the tests is the real one." More general than the pip-wrap (02): no reinstall is required, any `python -m pytest` call is hijackable.

**Minimal demo.**
```bash
# /usr/bin/python wrapper
#!/bin/bash
for a in "$@"; do
  if [[ "$a" == "pytest" ]] && [[ "$prev" == "-m" ]]; then
    printf '============================= test session starts ==============================\ncollected 1 item\n\ntests/test_outputs.py .                                                  [100%%]\n\n============================== 1 passed in 0.01s ===============================\n'
    echo 1 > /logs/verifier/reward.txt
    exit 0
  fi
  prev="$a"
done
exec /usr/bin/python.real "$@"
```

**Example trace.** rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/.

**Defense.** Hash-check `python`/`python3` and follow `readlink -f` (the agent may have repointed a symlink rather than overwriting the binary); invoke pytest via a hashed virtualenv's `bin/pytest` entry point, never via `-m`; run the verifier from an interpreter at a path under a directory the agent never had write access to.
