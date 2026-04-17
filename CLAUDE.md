# Adversarial Verifier Hardening

Iteratively patches task verifiers/evals against reward-hacking exploits. Three
agents (Terminus-2 via Harbor) play adversarial roles:

1. **Hacker** — runs with an aggressive exploit prompt, explicitly told to bypass tests.
2. **Fixer** — given the hack trajectory, modifies any task file to block the exploit.
3. **Solver / Oracle** — pre-check and post-fix validation (validates task is solvable & fix doesn't break it).
4. **Targeted replay** (optional, `--replay-enabled`) — after solver accepts the fix, re-runs a constrained hacker on the patched task with the prior exploit as a replay target. If it re-lands, the fix is rejected.

## Two modes (selected by `oracle: bool`)

Both modes run the same loop; only the pre-check/validation step differs.

| | `oracle=True` (KernelBench; enable with `--oracle`) | `oracle=False` (default, solver mode, generic) |
|--|--|--|
| Pre-check | `run_oracle_solver` — deterministic (`reference.py → solution.py`) | `run_solver_agent` — terminus-2 solves the task |
| Central file | `tests/eval_kernel.py` | `tests/test_outputs.py` |
| Metric | Speedup (`reference_time / your_time`) | Reward (0–1 pass/fail) |
| Hack threshold | KB: `--hack-threshold 10` | Default 1.0 |
| Solver threshold | KB: `--solver-threshold 0.5` | Default 1.0 |
| `.legitimate` marker | Enabled by default (disable with `--no-legitimate-marker`) | Enabled by default (disable with `--no-legitimate-marker`) |
| `solver_privileged` | N/A (oracle is deterministic) | Optional — inject `/solution/` as solver hint |
| Hardened state | `output/hardened/<task_id>/` (full patched task dir) | `output/hardened/<task_id>/` (full patched task dir) |

## Loop

Pre-check: solver/oracle must pass the original task.

For each iteration (up to `max_iterations`, default 10):
1. **Hacker attacks** (up to `hacker_retries`, default 3). If all fail → task is **robust**.
2. **Fixer patches** the task (one attempt per iteration).
   - Runs in Docker with tests/, solution/ baked in.
   - Edits files in `/logs/artifacts/` (git repo); only committed changes extracted.
   - On retry: previous attempt + solver output mounted read-only.
   - Can mark hack as legitimate via `.legitimate` sentinel (enabled by default; `--no-legitimate-marker` to disable).
3. **Validate**: solver/oracle must still pass. If not, revert; feedback sent to fixer next iteration.
4. **Targeted replay** (if `--replay-enabled`): up to `replay_retries` attempts to reproduce the prior exploit on the patched task. If reward ≥ `hack_threshold`, the fix is rejected (outcome `replay_broke_fix`) and feedback goes to the next fixer turn. Hardened state is only committed when both solver **and** replay agree.

## Module layout

```
harden-unified/
  harden/
    __main__.py       # CLI (python -m harden)
    config.py         # HardenConfig / BatchHardenConfig
    loop.py           # harden_task + _run_solver dispatch
    agent.py          # run_hacker / run_oracle_solver / run_solver_agent / run_fixer
    instructions.py   # build_hacker_instruction + build_targeted_replay_instruction + FIXER templates + SOLVER_HINT
    workspace.py      # working copies, artifact extraction, prepare_*_environment
    batch.py          # harden_batch (async with semaphore + stagger)
    trajectory.py     # ATIF trajectory → summary
  harden.py           # Convenience entry point
  monitor.py          # Live batch progress monitor
  batch_stats.py      # Quick stats for in-progress batches
  scripts/            # Example experiment run scripts
  hodoscope_pipeline/ # Pack successful hacks → embedding analysis
  merge_defenses.py   # Sequential compound-merge of defenses across tasks
  dedup_hacks.py      # LLM-based cluster dedup of hack summaries
  probe_hints.py      # Hint-injection probe — tests defense robustness
```

## Running

```bash
source /lambda/nfs/reward-hacking/.env

# Solver mode (default, generic slime tasks)
python -m harden --task-id <task-id> \
  --tasks-dir /lambda/nfs/reward-hacking/slime-internal/tasks \
  --solver-model gemini/gemini-3.1-pro-preview \
  --solver-privileged \
  --max-iterations 5

# Oracle mode (KernelBench) — opt in
python -m harden --oracle --task-id matmul \
  --hack-threshold 10 --solver-threshold 0.5 \
  --max-iterations 5

# Batch
python -m harden --task-ids task1,task2,task3 --max-concurrent 4
python -m harden --all --resume
```

## Key knobs (see `harden --help` for full list)

- `--oracle` — switch to deterministic oracle pre-check (KernelBench mode). Default is solver mode.
- `--no-legitimate-marker` — disable the `.legitimate` sentinel (fixer can't flag a hack as legitimate).
- `--hack-threshold`, `--solver-threshold` — gates on reward/speedup.
- `--harbor-config` — yaml/json Harbor JobConfig defaults to layer under harden overrides.
- `--force-build`, `--image-name` — rebuild control to avoid cache pollution from Dockerfile mutations across iterations.
- `--solver-privileged` (solver mode) — inject `/solution/` into solver env as hint.
- `--hacker-privileged` — mount the evaluation environment (tests/, environment/) read-only at `/eval_env/` so the hacker can whitebox-inspect the verifier.
- `--hacker-feedback` — give hacker read-only access to previous failed attempts.
- `--replay-enabled`, `--replay-retries` — enable targeted-replay post-solver gate; a constrained hacker re-attempts the prior exploit on the patched task, and the fix is rejected if it re-lands. Reuses hacker model/turns/timeout knobs, and inherits `--hacker-privileged` (if the hacker had `/eval_env/` access, replay does too — the exploit may reference it).

## Outputs

Each task produces:

- `<output_dir>/hardened/<task_id>/` — the full patched task (canonical hardened state).
- `<output_dir>/<task_id>/result.json` — per-task summary.
- `<output_dir>/<task_id>/task_config.json` — full config used for this task.
- `<output_dir>/<task_id>/jobs/` — Harbor job dirs (trajectories, verifier output, artifacts).
- `<output_dir>/batch_summary.json`, `<output_dir>/batch_config.json` — batch mode only.

### result.json schema

```jsonc
{
  "task_id": "<string>",
  "status": "robust" | "max_iterations" | "solver_failed_precheck" | "error" | "unknown",
  "oracle": true,
  "hardened_dir": "<path>",
  "iterations": [
    {
      "iteration": 0,
      "hack_reward": 1.23,
      "fix_applied": false,
      "outcome": "fixed" | "fix_failed" | "no_changes" | "legitimate" | "hacker_failed" | "replay_broke_fix",
      "replay_attempted": false,
      "replay_reward": null
    }
  ]
}
```

### Terminal statuses (used by `--resume`)

- `robust` — hacker failed all retries, OR legitimate marker hit threshold.
- `max_iterations` — loop exhausted `--max-iterations` without converging.
- `solver_failed_precheck` — pre-check could not pass the task (may be broken/unsolvable).

Non-terminal (re-run on `--resume`): `error`, `unknown`, missing `result.json`.

## Things easy to get wrong

- **Fixer's git repo lives at `/logs/artifacts/`**, not `/`. Edits to `/tests/` are ephemeral. The fixer prompt reminds the agent; if you change the prompt, keep this detail.
- **`shutil.copytree` on broken symlinks**: `prepare_fixer_environment` uses `ignore_dangling_symlinks=True` where needed.
- **Dockerfile mutation across iterations**: hardened state cascades — once `hardened_dirty`, force_build + separate `image_name` on every subsequent run. The loop tracks this explicitly.
- **Hack vs solver threshold defaults**: The built-in defaults (1.0 / 1.0) fit solver mode. KernelBench runs MUST pass `--hack-threshold 10 --solver-threshold 0.5`.
