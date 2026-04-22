#!/usr/bin/env bash
# Invoked when an individual harden job finishes.
# $1 = absolute path to the per-task output directory.
#
# Runs detached in the background; exit status is ignored by the caller.
# Customize as needed (e.g. dispatch other hook scripts in this directory).

set -u
JOB_DIR="${1:-}"
[ -n "$JOB_DIR" ] || exit 0

# Redirect all stdout/stderr to hooks.log inside the job directory.
exec >>"$JOB_DIR/hooks.log" 2>&1
echo "[$(date -Is)] hooks.sh start: $JOB_DIR"

# Dispatch the asciinema cleanup hook.
"$(dirname "$0")/delete_asciinema_files.sh" "$JOB_DIR"

echo "[$(date -Is)] hooks.sh done"
exit 0
