#!/usr/bin/env bash
# A4: Pro hacker+fixer, NO hacker-privileged, NO pool. Worst-case for the defender.
RUN_NAME=kb-l1-abl-nopriv-nopool
. "$(dirname "$0")/_common.sh"

run_harden \
  --hacker-model gemini/gemini-3.1-pro-preview \
  --fixer-model gemini/gemini-3.1-pro-preview
