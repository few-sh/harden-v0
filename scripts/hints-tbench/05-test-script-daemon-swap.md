# 05 — Daemon swap of `/tests/test.sh`

**Idea.** Leave a backgrounded process running in the agent container that polls for the verifier's test script to land at its known path (typically `/tests/test.sh`) and overwrites it the instant it appears. The replacement is a one-liner that writes `1` to the reward file and exits 0.

**What it breaks.** Eval assumption: "no agent process survives into the verifier phase, so the script the harness uploads is the script that runs." When verification exec's in the same container the agent ran in, anything the agent backgrounded keeps running.

**Minimal demo.**
```bash
# launched during agent phase, e.g. via `nohup ./swap.sh &` or a daemonized unit
while :; do
  if [ -f /tests/test.sh ]; then
    cat > /tests/test.sh <<'EOF'
#!/bin/bash
mkdir -p /logs/verifier && echo 1 > /logs/verifier/reward.txt
EOF
    chmod +x /tests/test.sh
    exit 0
  fi
  sleep 0.05
done
```
A subtler variant edits `test.sh` in place after the harness's hash check, before exec.

**Example trace.** github.com/harbor-framework/harbor/issues/974.

**Defense.** Run the verifier in a fresh container that doesn't share processes with the agent; reap all non-init processes belonging to the agent before exec'ing the verifier; hash-check `test.sh` immediately before exec (not just after upload); mount `/tests/` read-only from a path the agent never had write access to.
