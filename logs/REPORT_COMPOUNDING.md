# Compounding Reward-Hack Defenses Across KernelBench Tasks

## Problem

The `harden-kb` loop hardens one KernelBench Level-1 task at a time. Each task's final `tests/eval_kernel.py` defends only against the specific hacks the hacker agent happened to try on *that* task. Defenses discovered on other tasks never propagate back. We want one compounded eval script that absorbs mechanisms from across the batch.

Starting point: batch `/lambda/nfs/reward-hacking/harbor/outputs/batch_20260314_203057/` — 100 Level-1 tasks, 75 with non-empty hardened diffs (25 failed `solver_precheck`).

## Attempt 1: Cluster-based dedup (abandoned)

First idea: cluster the 128 successful-hack summaries (speedup ≥ 10× threshold), look at each cluster's fix as a prototype, and combine.

Pipeline:
- Script: `harden-kb/dedup_hacks.py` — LLM (`openai/gpt-5.2`, JSON mode) walks the summary list and, for each new entry, decides "matches existing cluster i" vs. "novel."
- Output: `/lambda/nfs/reward-hacking/harbor/outputs/batch_20260314_203057/hack_clusters.json`.
- Result: 128 hacks → 12 clusters. Top two clusters held 77% of hacks (`torch.cuda.Event.elapsed_time` monkeypatch, `kernelbench.timing.time_execution_with_cuda_event`).

Why abandoned:
- Even with a tightened prompt, reviewer feedback found 4 distinct sub-mechanisms merged into single clusters (phase-counter caching vs. id-based caching; `nn.Module.__call__` tagging; in-memory metadata forgery; `patch_all.py` pre-eval script injection).
- Cluster prototypes lose the nuance needed to reason about defenses. Merging 75 individual fixes into 12 prototypes is lossy at exactly the level we care about.

## Attempt 2: Sequential compound-merge (adopted)

Instead of clustering first, iterate donor-by-donor through every task's hardened diff, asking the LLM at each step: "here is your current defense; here is a donor's hack + its fix; does the donor reveal a gap you should close?"

### Mechanics

- **Seed task**: one task whose hardened state we grow. Default: `kernelbench-level1-001-square-matrix-multiplication`.
- **Donor list**: every other task whose hardened dir differs from its pristine dataset counterpart (75 tasks for a pristine-start seed, 74 otherwise). Pristine files (`tests/eval_kernel.py`, `tests/test.sh`, `environment/Dockerfile`) are identical across all 100 tasks — verified — so propagation is straightforward when we reach that stage.
- **Per iteration**:
  1. Build prompt: seed's current defense files + donor's hack summaries + donor's final defensive files. Optionally: seed+donor `instruction.md` / `tests/reference.py` via `--task-context`.
  2. Call LLM (`openai/gpt-5.2`) with `response_format={"type":"json_object"}`.
  3. Parse response (reasoning first, then `action: keep|update`, and on update a full-file-replacement `changes` list).
  4. Validate: `compile()` the new `eval_kernel.py`; action/changes consistency; allowlist check.
  5. On `update`: atomic write each changed file, append to log, store iteration record.
- **Two passes**: pass 1 in task-id order, pass 2 shuffled (`seed = --shuffle-seed + (pass - 2)`, default 42). Pass 2 is a convergence check — ideally zero absorptions.

### Architecture guarantees we rely on

`harden/workspace.py::update_hardened` only copies fixer artifacts into `hardened/<task_id>/` after `run_oracle_solver` passes (`harden/loop.py:203-206`). Death-spiral iterations where the fix broke the oracle are never reflected. So `<batch>/<task_id>/hardened/<task_id>/` is clean as-is.

### Implementation files

| path | role |
|---|---|
| `harden-kb/merge_defenses.py` | main script: two-pass sequential merge loop |
| `harden-kb/dedup_hacks.py` | earlier cluster-dedup (kept for reference) |
| `/lambda/nfs/reward-hacking/harbor/outputs/batch_20260314_203057/hack_summaries.json` | 128 parsed hack summaries (input) |
| `/lambda/nfs/reward-hacking/harbor/outputs/batch_20260314_203057/hack_clusters.json` | 12 clusters from attempt 1 |

CLI shape:
```bash
uv run python harden-kb/merge_defenses.py \
  --seed-task-id <tid> \
  [--task-context] [--passes 2] [--shuffle-seed 42] [--max-donors N] [--dry-run]
```

Output layout per seed under `harden-kb/outputs/merged_defense/<seed_tid>/`:
```
tests/eval_kernel.py           # rolling state
tests/test.sh                  # if ever updated
environment/Dockerfile         # if ever updated
log.md                         # one line per iteration
iterations/<pN>_<NNN>_<tid>.json   # full response + hashes per call
```

## Results

Three runs over the same 75-task batch, different seeds and the context flag toggled.

| seed | context | start | pass 1 updates | pass 2 updates | final `eval_kernel.py` lines |
|---|:-:|---|:-:|:-:|---|
| 001 (square matmul) | no | 174 (hardened) | 7 | 0 | 415 |
| 050 (conv-2d) | yes | ~174 (hardened) | 3 | 1 | 295 |
| 100 (hingeloss) | yes | ~170 (pristine, no defenses) | 5 | 1 | 392 |

### Absorbed mechanisms

Seed 001 (task-id-order pass 1, no context):

| iter | donor | mechanism |
|---|---|---|
| 000 | 002 | neutralize `inspect` / `sys._getframe` frame-introspection |
| 004 | 006 | `secure_exec` wrapper; restore `builtins.exec`, `torch.nn.Module.__call__`, `Event.synchronize`, `time.time`/`perf_counter` |
| 014 | 016 | stream-evasion fix: device-wide `cuda.synchronize` before end-event + L2 clear between trials |
| 038 | 060 | input-mutation caching detector (output must change when inputs mutated) |
| 040 | 062 | isolated-subprocess reference timing (defeats reference-slowdown hacks) |
| 044 | 067 | pristine-backup restore of installed `kernelbench/` package (defeats on-disk `eval.py` edits) |
| 053 | 077 | use `torch._C._CudaEventBase.record` / `.elapsed_time` (C++ layer below Python monkey-patches) |

Seed 050 (with context):
- 007: subprocess ref-timing + input-cloning
- 040: restore-pristine-env after custom model loading
- 067: kernelbench package backup/restore
- 054 (pass 2): retain cloned inputs alive to prevent `id()`-reuse

Seed 100 (pristine start, with context):
- 001: anti-monkeypatch of `get_timing_function` / `time_execution_with_cuda_event` / `Event.elapsed_time` / `Event.record`
- 012: broader `restore_all()` (restore `torch.cuda.Event` class itself + full module dicts)
- 016: stream-evasion device-sync
- 067: kernelbench package backup/restore
- 071: stealth correctness check (post-eval re-run)
- 079 (pass 2): clean-reference re-timing (defeats global ref slowdown)

### Observations

1. **Convergence**: seed 001 without task-context was idempotent under a shuffled pass 2 (0 updates). Adding task-context broke full idempotency — both seed 050 and seed 100 absorbed one more donor in pass 2 that pass 1 had ordered before the necessary prior state existed.
2. **Task context makes the LLM more conservative**: seed 050 absorbed 4 total vs. seed 001's 7 despite comparable starting states; even seed 100 starting from pristine only reached 6 absorptions. With context, the LLM reasons about cross-task applicability ("this defense targets a different kernel shape, skip") and filters more aggressively.
3. **Coverage is still mechanism-complete**: every major cluster from attempt 1's dedup is represented in all three merged defenses, just with fewer per-category accretions under context.
4. **Pristine start works**: seed 100 (hacker failed on iter 0 → hardened dir equals pristine) grew from ~170 to 392 lines. The "current file is already hardened" prompt bias didn't prevent absorption when the donor evidence was strong.
5. **Comments**: after tuning the prompt to "add a comment next to any new or modified code explaining what the code does and what attack it's defending against," merged files self-document their defensive intent next to each block.

## Artifacts

| kind | path |
|---|---|
| Cluster dedup output | `/lambda/nfs/reward-hacking/harbor/outputs/batch_20260314_203057/hack_clusters.json` |
| Hack summaries | `/lambda/nfs/reward-hacking/harbor/outputs/batch_20260314_203057/hack_summaries.json` |
| Merged defense (seed 001) | `harden-kb/outputs/merged_defense/kernelbench-level1-001-square-matrix-multiplication/` |
| Merged defense (seed 050) | `harden-kb/outputs/merged_defense/kernelbench-level1-050-conv-standard-2d-square-input-square-kernel/` |
| Merged defense (seed 100) | `harden-kb/outputs/merged_defense/kernelbench-level1-100-hingeloss/` |
| Run logs | `harden-kb/outputs/merge_defenses*.log`, `dedup_hacks.log` |
| Iteration records | `<merged_defense>/iterations/*.json` |
| Per-seed decision log | `<merged_defense>/log.md` |

## Open threads

- **Propagation**: given pristine files are uniform across tasks and the merged defense is task-agnostic harness code, the compounded `eval_kernel.py` can be copied verbatim onto every task. Script design drafted in `/home/u/.claude/plans/jazzy-doodling-starfish.md` (prior plan file) — not yet implemented.
- **End-to-end validation**: run the oracle solver against each seed's merged defense to confirm legitimate solutions still pass (speedup ≥ 0.5). Heavier: re-run the full harden loop against the merged defense to see if a fresh hacker can still find a bypass.
- **Context ablation**: seed 001 without context, seed 050/100 with context — the comparison is cross-seed. A clean isolation would run seed 001 *with* context (and seed 050 *without*) to quantify the context-vs-seed effect.
- **Cluster coverage audit**: enumerate which of the 12 dedup clusters each merged defense explicitly addresses. All three cover the major categories, but a formal mapping would surface any blind spots.
