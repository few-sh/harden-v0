# 07 — python3 replacement

**Idea.** Replace `/usr/bin/python3` (or its `readlink -f` target) with a wrapper. Any verifier that invokes the interpreter — to run a check script, import a tester library, or `python -m pytest` — passes through the trojan, which can write `1` to the reward file directly or fabricate test output before falling through to the real interpreter.

**What it breaks.** Eval assumption: "the python interpreter on the verifier's path is uncompromised." Broader than 03 (which targets `python -m pytest` specifically): *any* python invocation in the verifier path is hijackable, including non-pytest checks and helper scripts.

**Minimal demo.**
```bash
# /usr/bin/python3, planted during agent phase
#!/bin/bash
mkdir -p /logs/verifier && echo 1 > /logs/verifier/reward.txt
# fall through to the real interpreter so other callers still work
exec /usr/bin/python3.real "$@"
```
For verifiers that key on stdout, the trojan can additionally print a fabricated pytest report before exec'ing — or short-circuit entirely on `-m pytest`.

**Example trace.** github.com/harbor-framework/harbor/issues/974.

**Defense.** Hash-pin `python3` *and* the symlink target it resolves to (the agent may have repointed `/usr/bin/python3 → /opt/evil`); install verifier-side python at a path the agent could not write to (e.g. `/opt/verifier/bin/python3`); mount `/usr/bin` read-only from the host.
