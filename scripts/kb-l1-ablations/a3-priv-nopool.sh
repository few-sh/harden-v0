#!/usr/bin/env bash
# A3: Pro hacker+fixer, hacker-privileged, NO pool. Per-task iteration alone.
RUN_NAME=kb-l1-abl-priv-nopool
. "$(dirname "$0")/_common.sh"

run_harden \
  --hacker-model gemini/gemini-3.1-pro-preview \
  --fixer-model gemini/gemini-3.1-pro-preview \
  --hacker-privileged \
  --hacker-privileged-disable-iteration 8
