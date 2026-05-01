"""Batch orchestrator for hardening multiple tasks concurrently.

Two concurrency drivers are dispatched on `pool_server`:

* **Pool mode (`_run_with_iteration_barrier`)** — all active tasks advance
  one hardening iteration at a time via asyncio.gather on per-task generator
  yields. The barrier keeps the shared pool's history in lockstep across
  tasks: everyone observes the same pool state at iter N start, makes their
  decisions, optionally pushes, then advances. `pool_max_consecutive_syncs`
  also relies on this lockstep to be meaningful.

* **Pool-disabled mode (`_run_independent`)** — tasks are independent (their
  hardened/, jobs/, result.json don't share state) so we drive each generator
  to completion concurrently with no iter-level synchronization. Throughput
  is bounded only by the container semaphore.

Both drivers share an asyncio.Semaphore(max_concurrent_containers) acquired
inside `_harden_task_phases` for each container-heavy step.

Tasks that terminate early (robust, legitimate threshold, precheck failure)
return from the generator before the next yield; both drivers handle this.
"""

import asyncio
import dataclasses
import json
import logging
import time
from collections import Counter
from collections.abc import AsyncGenerator, Callable
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
        # Default: seed every fresh task's pool cursor to the current pool HEAD
        # so iter-0 doesn't waste a fixer turn integrating a task-specific
        # bootstrap (KernelBench bootstrap dirs include the source task's
        # reference.py, which would corrupt sibling tasks). With
        # --pool-integrate-bootstrap, skip the seed: PoolCursor defaults to
        # None and iter-0 becomes a skip-hacker pool-sync iter that ports the
        # entire pool history into /logs/artifacts/.
        # Tasks with an existing pool_sha.txt (resumed runs) are left alone.
        if pool_server is not None and not config.pool_integrate_bootstrap:
            pool_head_sha = get_pool_head(pool_server.bare_path)
            for task_id in tasks_to_run:
                task_output_dir = config.output_dir / task_id
                sha_file = task_output_dir / "pool_sha.txt"
                if not sha_file.exists():
                    task_output_dir.mkdir(parents=True, exist_ok=True)
                    write_last_seen_sha(task_output_dir, pool_head_sha)

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

        def _record_done(tid: str) -> None:
            """Move a finished task's result into `results` and log it."""
            nonlocal n_done
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

        if pool_server is not None:
            await _run_with_iteration_barrier(
                task_gens=task_gens,
                task_result_out=task_result_out,
                record_done=_record_done,
                results=results,
                config=config,
                total=total,
            )
        else:
            await _run_independent(
                task_gens=task_gens,
                task_result_out=task_result_out,
                record_done=_record_done,
                results=results,
                config=config,
                total=total,
            )

    _print_summary(results, config, total=total, finished=True, running_task_ids=[])
    return results


async def _run_with_iteration_barrier(
    *,
    task_gens: dict[str, AsyncGenerator],
    task_result_out: dict[str, list],
    record_done: Callable[[str], None],
    results: list[dict],
    config: BatchHardenConfig,
    total: int,
) -> None:
    """Pool-mode driver: lockstep iteration across tasks.

    Each gather round advances every active task to its next yield (or
    completion). The barrier ensures the shared pool is observed
    consistently across tasks at every iter boundary.
    """
    async def _advance(tid: str, gen: AsyncGenerator) -> tuple[str, bool]:
        """Advance one generator to its next yield. Returns (tid, still_active).

        - yielded            → still active.
        - StopAsyncIteration → generator returned (result already in task_result_out).
        - Other Exception    → one task's crash; logged and recorded as error,
                               doesn't abort the gather round for sibling tasks.
        """
        try:
            await anext(gen)
            return tid, True
        except StopAsyncIteration:
            return tid, False
        except Exception as e:
            logger.error("[%s] Phase failed with exception: %s", tid, e)
            ro = task_result_out[tid]
            if not ro:
                ro.append({"task_id": tid, "status": "error", "error": str(e)})
            return tid, False

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
                    record_done(tid)
            task_gens = next_task_gens
            _print_summary(results, config, total=total, finished=False,
                           running_task_ids=list(task_gens))
    finally:
        # Ctrl-C / cancel: aclose pending generators so their `finally` blocks run.
        if task_gens:
            await asyncio.gather(
                *[gen.aclose() for gen in task_gens.values()],
                return_exceptions=True,
            )


async def _run_independent(
    *,
    task_gens: dict[str, AsyncGenerator],
    task_result_out: dict[str, list],
    record_done: Callable[[str], None],
    results: list[dict],
    config: BatchHardenConfig,
    total: int,
) -> None:
    """Pool-disabled driver: each task runs to completion independently.

    No iter-level barrier — fast tasks don't wait on slow ones. Container
    concurrency is still capped by the semaphore acquired inside
    `_harden_task_phases`.
    """
    running: set[str] = set(task_gens)

    async def _drive(tid: str, gen: AsyncGenerator) -> None:
        try:
            async for _ in gen:
                pass
        except Exception as e:
            logger.error("[%s] Task failed: %s", tid, e)
            ro = task_result_out[tid]
            if not ro:
                ro.append({"task_id": tid, "status": "error", "error": str(e)})
        # Bookkeeping below runs only on normal completion. CancelledError is
        # a BaseException, not Exception — it propagates past the try/except
        # and skips this section, which is what we want (a cancelled task
        # shouldn't be "recorded" as done).
        running.discard(tid)
        record_done(tid)
        _print_summary(results, config, total=total, finished=False,
                       running_task_ids=sorted(running))

    try:
        await asyncio.gather(
            *[_drive(tid, gen) for tid, gen in task_gens.items()]
        )
    finally:
        # Ctrl-C / cancel: aclose anything still running so the generators'
        # `finally` blocks (e.g. pool_cursor cleanup) get a chance to run.
        pending = [task_gens[tid] for tid in running]
        if pending:
            await asyncio.gather(
                *[g.aclose() for g in pending],
                return_exceptions=True,
            )


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
