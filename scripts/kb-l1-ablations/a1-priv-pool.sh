#!/usr/bin/env bash
# A1: Flash hacker+fixer, hacker-privileged, pool-enabled. SOTA-equivalent.
RUN_NAME=kb-l1-abl-priv-pool
. "$(dirname "$0")/_common.sh"

run_harden \
  --hacker-model gemini/gemini-3-flash-preview \
  --fixer-model gemini/gemini-3-flash-preview \
  --hacker-privileged \
  --hacker-privileged-disable-iteration 8 \
  --pool-enabled \
  --pool-bootstrap-dir "$BOOTSTRAP_DIR" \
  --pool-port 9418
