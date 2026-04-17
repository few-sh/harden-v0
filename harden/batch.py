"""Batch orchestrator for hardening multiple tasks concurrently.

Concurrency model: one `asyncio.Semaphore(max_concurrent_containers)` acquired at
task level. The task's inner loop (hacker → fixer → validate) is sequential, so
there's no benefit to threading the semaphore deeper. Each task also staggers
0–10s before acquiring to avoid thundering-herd container starts.
"""

import asyncio
import dataclasses
import json
import logging
import random
import time
from collections import Counter
from pathlib import Path

from .config import BatchHardenConfig, HardenConfig
from .loop import harden_task

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


def _is_completed(output_dir: Path, task_id: str, oracle: bool) -> bool:
    """A task is complete only if its result is terminal AND its mode matches."""
    result_path = output_dir / task_id / "result.json"
    if not result_path.exists():
        return False
    try:
        result = json.loads(result_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if result.get("status") not in _TERMINAL_STATUSES:
        return False
    if result.get("oracle") != oracle:
        logger.warning(
            "[%s] Previous result has oracle=%s but current config has oracle=%s — re-running.",
            task_id, result.get("oracle"), oracle,
        )
        return False
    return True


async def harden_batch(config: BatchHardenConfig) -> list[dict]:
    _save_batch_config(config)

    semaphore = asyncio.Semaphore(config.max_concurrent_containers)

    if config.resume:
        skipped = [t for t in config.task_ids
                   if _is_completed(config.output_dir, t, config.oracle)]
        tasks_to_run = [t for t in config.task_ids if t not in set(skipped)]
        if skipped:
            logger.info("Resuming: skipping %d completed tasks", len(skipped))
    else:
        tasks_to_run = list(config.task_ids)

    total = len(tasks_to_run)
    logger.info(
        "Batch hardening: %d tasks, max %d concurrent containers, oracle=%s",
        total, config.max_concurrent_containers, config.oracle,
    )

    progress = {"done": 0, "total": total}
    progress_lock = asyncio.Lock()

    async def _run_one(task_id: str) -> dict:
        # Stagger container starts to avoid thundering herd on batch launch.
        await asyncio.sleep(random.uniform(0, 10))
        async with semaphore:
            t0 = time.monotonic()
            try:
                task_config = config.make_task_config(task_id)
                _save_task_config(task_config)
                result = await harden_task(task_config)
            except Exception as e:
                logger.error("[%s] Failed with exception: %s", task_id, e)
                result = {"task_id": task_id, "status": "error", "error": str(e)}

            elapsed = time.monotonic() - t0
            async with progress_lock:
                progress["done"] += 1
                n = progress["done"]

            status = result.get("status", "unknown")
            n_iters = len(result.get("iterations", []))
            logger.info(
                "[%d/%d] %s: %s (%d iters, %.0fs)",
                n, total, task_id, status, n_iters, elapsed,
            )
            return result

    results = await asyncio.gather(*[_run_one(task_id) for task_id in tasks_to_run])

    _print_summary(results, config)
    return list(results)


def _print_summary(results: list[dict], config: BatchHardenConfig) -> None:
    counts = Counter(r.get("status", "unknown") for r in results)
    total = len(results)

    print(f"\n{'='*60}")
    print("Batch Hardening Summary")
    print(f"{'='*60}")
    print(f"Total tasks processed: {total}")
    for status in ["robust", "max_iterations", "solver_failed_precheck", "error", "unknown"]:
        if counts[status]:
            print(f"  {status:.<30} {counts[status]}")
    print(f"{'='*60}")

    summary_path = config.output_dir / "batch_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "total": total,
        "counts": dict(counts),
        "tasks": [
            {"task_id": r.get("task_id"), "status": r.get("status")}
            for r in results
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Summary:  {summary_path}")
