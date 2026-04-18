# Adversarial Verifier Hardening

Iteratively patches task verifiers/evals against reward-hacking exploits. Three
agents (Terminus-2 via Harbor) play adversarial roles:

1. **Hacker** — runs with an aggressive exploit prompt, explicitly told to bypass tests.
2. **Fixer** — given the hack trajectory, modifies any task file to block the exploit.
3. **Solver / Oracle** — pre-check and post-fix validation (validates task is solvable & fix doesn't break it).
4. **Targeted replay** (optional, `--replay-enabled`) — after solver accepts the fix, re-runs a constrained hacker on the patched task with the prior exploit as a replay target. If it re-lands, the fix is rejected.

## Mode flags (two orthogonal axes)

Two independent flags control the loop:

* `--oracle` — selects the **pre-check/validation mechanism**: deterministic
  reference copy (`reference.py → solution.py`) if set, agent solver
  (Terminus-2) if not.
* `--kernelbench-mode` — selects the **prompt/template bundle**: KernelBench
  framing (speedup metric, `eval_kernel.py`, runtime-minimization hacker goal,
  KB-specific fixer template) if set, generic framing (pass/fail reward,
  `test_outputs.py`, verifier-bypass hacker goal, generic fixer template) if
  not.

The two commonly travel together (KernelBench runs pass both, generic slime
tasks pass neither), but they're decoupled because nothing forces the
coupling — a generic task with a deterministic reference could use `--oracle`
alone, and a KB-style framing could conceivably run with an agent solver.

| axis | flag | True | False (default) |
|------|------|------|-----------------|
| Pre-check | `--oracle` | `run_oracle_solver` (deterministic) | `run_solver_agent` (Terminus-2) |
| Prompts / templates | `--kernelbench-mode` | KB framing, `eval_kernel.py`, speedup | Generic framing, `test_outputs.py`, reward |
| Hack / solver thresholds | `--hack-threshold` / `--solver-threshold` | KB uses `10` / `0.5` | Default `1.0` / `1.0` |
| `.legitimate` marker | `--no-legitimate-marker` disables | enabled by default | enabled by default |
| `solver_privileged` | N/A when `--oracle` (deterministic) | — | Optional — inject `/solution/` as solver hint |
| Hardened state | — | `output/hardened/<task_id>/` | `output/hardened/<task_id>/` |

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

# KernelBench — pass BOTH flags
python -m harden --oracle --kernelbench-mode --task-id matmul \
  --hack-threshold 10 --solver-threshold 0.5 \
  --max-iterations 5

# Batch
python -m harden --task-ids task1,task2,task3 --max-concurrent 4
python -m harden --all
```

## Key knobs (see `harden --help` for full list)

- `--oracle` — use deterministic pre-check (reference.py → solution.py). Default is agent solver.
- `--kernelbench-mode` — use KB-specific prompts/templates (speedup metric, `eval_kernel.py`). Independent of `--oracle`; KB runs pass both.
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
  "kernelbench_mode": true,
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

### Terminal statuses (resume is automatic when `--output-dir` already exists)

- `robust` — hacker failed all retries, OR legitimate marker hit threshold.
- `max_iterations` — loop exhausted `--max-iterations` without converging.
- `solver_failed_precheck` — pre-check could not pass the task (may be broken/unsolvable).

Non-terminal (re-run on resume): `error`, `unknown`, missing `result.json`.

## Things easy to get wrong

- **Fixer's git repo lives at `/logs/artifacts/`**, not `/`. Edits to `/tests/` are ephemeral. The fixer prompt reminds the agent; if you change the prompt, keep this detail.
- **`shutil.copytree` on broken symlinks**: `prepare_fixer_environment` uses `ignore_dangling_symlinks=True` where needed.
- **Dockerfile mutation across iterations**: hardened state cascades — once `hardened_dirty`, force_build + separate `image_name` on every subsequent run. The loop tracks this explicitly.
- **Hack vs solver threshold defaults**: The built-in defaults (1.0 / 1.0) fit generic mode. KernelBench runs MUST pass `--hack-threshold 10 --solver-threshold 0.5`.
- **KernelBench needs BOTH mode flags**: `--oracle` alone gives a deterministic pre-check with generic prompts; `--kernelbench-mode` alone gives KB prompts with an agent solver pre-check. Real KB runs need both.
