#!/usr/bin/env bash
# W2S: Flash hacker+fixer (weaker defender), hacker-privileged, pool. Same shape
# as A1 but Flash-only. Hardened envs from this run get re-tested later vs Pro.
RUN_NAME=kb-l1-w2s-flash
. "$(dirname "$0")/_common.sh"

run_harden \
  --hacker-model gemini/gemini-3-flash-preview \
  --fixer-model gemini/gemini-3-flash-preview \
  --hacker-privileged \
  --hacker-privileged-disable-iteration 8 \
  --pool-enabled \
  --pool-bootstrap-dir "$BOOTSTRAP_DIR" \
  --pool-port 9420
