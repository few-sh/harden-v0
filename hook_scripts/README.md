# hook_scripts

`hooks.sh` is invoked (detached, in the background) at the end of every
individual agent job in `harden/agent.py::_run_agent`. It receives one
argument: the absolute path to the finished trial directory.

All stdout/stderr from `hooks.sh` is appended to `<job_dir>/hooks.log`.

## Adding a hook

1. Drop a script in this directory, e.g. `my_hook.sh`:

   ```bash
   #!/usr/bin/env bash
   set -u
   JOB_DIR="$1"
   # ... do something with $JOB_DIR ...
   ```

2. `chmod +x my_hook.sh`.

3. Dispatch it from `hooks.sh`:

   ```bash
   "$(dirname "$0")/my_hook.sh" "$JOB_DIR"
   ```

Hooks run after the job finishes, so they can freely read or mutate
files under `$JOB_DIR`. Keep them fast and idempotent — failures are
ignored by the caller but will show up in `hooks.log`.
