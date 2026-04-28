"""Batch orchestrator for hardening multiple tasks concurrently.

Concurrency model: all active tasks advance one full hardening iteration at a
time via asyncio.gather. `_harden_task_phases` yields once per iteration,
after validate+replay (the section that may persist pool state). An
asyncio.Semaphore(max_concurrent_containers) limits concurrent container runs
across tasks and is acquired inside _harden_task_phases for each
container-heavy step.

Tasks that terminate early (robust, legitimate threshold, precheck failure)
return from the generator before the next yield; the gather sees
StopAsyncIteration and the task is collected as done.
"""

import asyncio
import dataclasses
import json
import logging
import time
from collections import Counter
from collections.abc import AsyncGenerator
from pathlib import Path

from .config import BatchHardenConfig, HardenConfig
from .loop import _harden_task_phases
from .pool import get_pool_head, pool_context, write_last_seen_sha

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"robust", "solver_failed_precheck", "max_iterations"}


def _config_to_dict(config) -> dict:
    out: dict = {}
    for f in dataclasses.fields(config):
        v = getattr(config, f.name)
        out[f.name] = str(v) if isinstance(v, Path) else v
    return out


def _save_task_config(config: HardenConfig) -> None:
    config_path = config.task_output_dir / "task_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(_config_to_dict(config), indent=2))


def _save_batch_config(config: BatchHardenConfig) -> None:
    config_path = config.output_dir / "batch_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(_config_to_dict(config), indent=2))
    logger.info("Saved batch configuration to %s", config_path)


def _is_completed(output_dir: Path, task_id: str, oracle: bool, kernelbench_mode: bool) -> bool:
    """A task is complete only if its result is terminal AND both mode flags match."""
    result_path = output_dir / task_id / "result.json"
    if not result_path.exists():
        return False
    try:
        result = json.loads(result_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if result.get("status") not in _TERMINAL_STATUSES:
        return False
    prev_oracle = result.get("oracle")
    prev_kb = result.get("kernelbench_mode", prev_oracle)  # back-compat: old results set only `oracle`
    if prev_oracle != oracle or prev_kb != kernelbench_mode:
        logger.warning(
            "[%s] Prior result mode mismatch "
            "(oracle %s→%s, kernelbench_mode %s→%s) — re-running.",
            task_id, prev_oracle, oracle, prev_kb, kernelbench_mode,
        )
        return False
    return True


async def harden_batch(config: BatchHardenConfig) -> list[dict]:
    _save_batch_config(config)

    semaphore = asyncio.Semaphore(config.max_concurrent_containers)

    if config.resume:
        skipped = [t for t in config.task_ids
                   if _is_completed(config.output_dir, t, config.oracle, config.kernelbench_mode)]
        tasks_to_run = [t for t in config.task_ids if t not in set(skipped)]
        if skipped:
            logger.info("Resuming: skipping %d completed tasks", len(skipped))
    else:
        skipped = []
        tasks_to_run = list(config.task_ids)

    total = len(config.task_ids)
    n_to_run = len(tasks_to_run)
    logger.info(
        "Batch hardening: %d tasks (%d to run), max %d concurrent containers, oracle=%s kernelbench_mode=%s",
        total, n_to_run, config.max_concurrent_containers, config.oracle, config.kernelbench_mode,
    )

    # Pre-populate results with previously completed tasks so the summary
    # reflects the full batch, not just the current run.
    results: list[dict] = []
    for task_id in skipped:
        result_path = config.output_dir / task_id / "result.json"
        try:
            results.append(json.loads(result_path.read_text()))
        except (json.JSONDecodeError, OSError):
            logger.warning("[%s] Could not reload existing result; omitted from summary", task_id)

    with pool_context(config) as pool_server:
        # Seed every fresh task's pool cursor to the bootstrap SHA so iter-0
        # doesn't waste a fixer turn integrating the raw bootstrap commit.
        # Tasks with an existing pool_sha.txt (resumed runs) are left alone.
        if pool_server is not None:
            bootstrap_sha = get_pool_head(pool_server.bare_path)
            for task_id in tasks_to_run:
                task_output_dir = config.output_dir / task_id
                sha_file = task_output_dir / "pool_sha.txt"
                if not sha_file.exists():
                    task_output_dir.mkdir(parents=True, exist_ok=True)
                    write_last_seen_sha(task_output_dir, bootstrap_sha)

        # Each task gets:
        #   - a generator object (not yet started — no code runs until __anext__)
        #   - a single-element list that the generator writes its result dict into
        #     when it terminates (avoids needing a return value from an async generator)
        #   - a start timestamp for elapsed-time logging
        task_gens: dict[str, AsyncGenerator] = {}
        task_result_out: dict[str, list] = {}
        task_start: dict[str, float] = {}
        for i, task_id in enumerate(tasks_to_run):
            task_config = config.make_task_config(task_id)
            _save_task_config(task_config)
            ro: list = []
            task_result_out[task_id] = ro
            task_gens[task_id] = _harden_task_phases(
                task_config, pool_server, semaphore, ro,
                initial_delay=i * 2.0,  # stagger: task i waits i*2 s before precheck
            )
            task_start[task_id] = time.monotonic()

        n_done = 0

        async def _advance(tid: str, gen: AsyncGenerator) -> tuple[str, bool]:
            """Advance one generator by one iteration fence. Returns (tid, still_active).

            Calls __anext__() which runs the generator body until its next yield
            statement, then suspends.  Two outcomes:
              - yielded → task is still active, returns True
              - StopAsyncIteration → generator returned (task finished or failed
                early); result has already been written into task_result_out[tid]
            Unexpected exceptions are caught so one failing task doesn't abort
            the entire gather round.
            """
            try:
                await anext(gen)
                return tid, True
            except StopAsyncIteration:
                # Generator ran off the end or hit an explicit return — task done.
                return tid, False
            except Exception as e:
                logger.error("[%s] Phase failed with exception: %s", tid, e)
                ro = task_result_out[tid]
                if not ro:
                    # Generator died before writing a result; synthesise an error entry.
                    ro.append({"task_id": tid, "status": "error", "error": str(e)})
                return tid, False

        # ── Iteration-barrier loop ────────────────────────────────────────────
        # Each gather call advances all active tasks to their next iteration
        # fence (or completion).
        #
        # heavy work inside the generator acquires `semaphore` first, so at most
        # max_concurrent_containers tasks run containers at any one time — but
        # the gather itself waits for ALL tasks to reach the yield (or finish)
        # before this loop body continues. That wait is the iteration barrier:
        # no task begins its next iteration until every active task has either
        # reached the fence or completed.
        try:
            while task_gens:
                phase_outcomes = await asyncio.gather(
                    *[_advance(tid, gen) for tid, gen in task_gens.items()]
                )

                next_task_gens: dict[str, AsyncGenerator] = {}
                for tid, still_active in phase_outcomes:
                    if still_active:
                        next_task_gens[tid] = task_gens[tid]
                    else:
                        n_done += 1
                        elapsed = time.monotonic() - task_start[tid]
                        ro = task_result_out[tid]
                        result = ro[0] if ro else {"task_id": tid, "status": "error"}
                        results.append(result)
                        status = result.get("status", "unknown")
                        n_iters = len(result.get("iterations", []))
                        logger.info(
                            "[%d/%d] %s: %s (%d iters, %.0fs)",
                            n_done, n_to_run, tid, status, n_iters, elapsed,
                        )

                task_gens = next_task_gens
                _print_summary(results, config, total=total, finished=False,
                               running_task_ids=list(task_gens))
        finally:
            # Cleanup: This ensures that we wait for any tasks that got cancelled with Ctrl-C.
            if task_gens:
                await asyncio.gather(
                    *[gen.aclose() for gen in task_gens.values()],
                    return_exceptions=True,
                )

    _print_summary(results, config, total=total, finished=True, running_task_ids=[])
    return results


def _print_summary(
    results: list[dict],
    config: BatchHardenConfig,
    *,
    total: int,
    finished: bool = False,
    running_task_ids: list[str] | None = None,
) -> None:
    counts = Counter(r.get("status", "unknown") for r in results)
    n_finished = len(results)
    running_task_ids = running_task_ids or []
    n_running = len(running_task_ids)

    print(f"\n{'='*60}")
    print("Batch Hardening Summary")
    print(f"{'='*60}")
    print(f"Total: {total}  |  Finished: {n_finished}  |  Running: {n_running}")
    for status in ["robust", "max_iterations", "solver_failed_precheck", "error", "unknown"]:
        if counts[status]:
            print(f"  {status:.<30} {counts[status]}")
    if running_task_ids:
        print(f"  {'running':.<30} {', '.join(running_task_ids)}")
    print(f"{'='*60}")

    summary_path = config.output_dir / "batch_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "harden_run_finished": finished,
        "total": total,
        "finished": n_finished,
        "running_tasks_count": n_running,
        "running_tasks": running_task_ids,
        "counts": dict(counts),
        "tasks": [
            {"task_id": r.get("task_id"), "status": r.get("status")}
            for r in results
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Summary:  {summary_path}")
