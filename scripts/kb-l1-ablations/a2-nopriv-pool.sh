#!/usr/bin/env bash
# A2: Pro hacker+fixer, NO hacker-privileged, pool-enabled. Realistic threat model.
RUN_NAME=kb-l1-abl-nopriv-pool
. "$(dirname "$0")/_common.sh"

run_harden \
  --hacker-model gemini/gemini-3.1-pro-preview \
  --fixer-model gemini/gemini-3.1-pro-preview \
  --pool-enabled \
  --pool-bootstrap-dir "$BOOTSTRAP_DIR" \
  --pool-port 9419
