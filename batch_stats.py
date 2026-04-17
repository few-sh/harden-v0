#!/usr/bin/env python3
"""Analyze harden batch progress and failures.

This script is designed for both completed and in-progress batches where
batch_summary.json may not exist yet.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


KNOWN_FINAL_STATUSES = {
    "robust",
    "max_iterations",
    "solver_failed_precheck",
    "error",
    "unknown",
}


@dataclass
class TaskStats:
    task_id: str
    lifecycle_status: str
    final_status: str | None
    started: bool
    completed: bool
    failed: bool
    precheck_failed: bool
    exception_files: int
    jobs_seen: int


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception:
        return None


def _task_ids_from_batch_config(batch_dir: Path) -> list[str] | None:
    data = _load_json(batch_dir / "batch_config.json")
    if not data:
        return None
    task_ids = data.get("task_ids")
    if isinstance(task_ids, list):
        out: list[str] = []
        for x in task_ids:
            if isinstance(x, str) and x.strip():
                out.append(x)
        return out
    return None


def _task_ids_from_dirs(batch_dir: Path) -> list[str]:
    out: list[str] = []
    for child in sorted(batch_dir.iterdir(), key=lambda p: p.name):
        if child.is_dir():
            out.append(child.name)
    return out


def _iter_job_dirs(task_dir: Path):
    jobs_dir = task_dir / "jobs"
    if not jobs_dir.is_dir():
        return
    for child in jobs_dir.iterdir():
        if child.is_dir():
            yield child


def _job_role(job_dir: Path) -> str:
    name = job_dir.name
    if "__" in name:
        return name.split("__", 1)[0]
    return name


def _job_reward(job_dir: Path) -> float | None:
    # Harbor stores trial result at jobs/<role...>/<trial_id>/result.json.
    for maybe_trial in sorted(job_dir.iterdir(), key=lambda p: p.name):
        if not maybe_trial.is_dir():
            continue
        trial_result = _load_json(maybe_trial / "result.json")
        if not trial_result:
            continue
        rewards = (
            trial_result.get("verifier_result", {})
            .get("rewards", {})
        )
        reward = rewards.get("reward")
        if isinstance(reward, (int, float)):
            return float(reward)
        return None
    return None


def _count_exceptions(task_dir: Path) -> tuple[int, bool]:
    jobs_dir = task_dir / "jobs"
    if not jobs_dir.is_dir():
        return 0, False

    total = 0
    for exc in jobs_dir.rglob("exception.txt"):
        if not exc.is_file():
            continue
        try:
            if exc.stat().st_size > 0:
                total += 1
        except OSError:
            pass
    return total, total > 0


def _infer_precheck_failure_without_task_result(
    task_dir: Path,
    solver_precheck_retries: int | None,
) -> bool:
    job_dirs = list(_iter_job_dirs(task_dir))
    if not job_dirs:
        return False

    roles = [_job_role(j) for j in job_dirs]
    if not all(role.startswith("solver_precheck") for role in roles):
        return False

    if solver_precheck_retries is not None and len(job_dirs) < solver_precheck_retries:
        return False

    # If all precheck rewards are explicitly < 1.0, infer precheck failure.
    # Missing reward keeps this conservative (returns False).
    for jd in job_dirs:
        reward = _job_reward(jd)
        if reward is None or reward >= 1.0:
            return False
    return True


def classify_task(
    batch_dir: Path,
    task_id: str,
    solver_precheck_retries: int | None,
) -> TaskStats:
    task_dir = batch_dir / task_id
    result = _load_json(task_dir / "result.json")

    job_dirs = list(_iter_job_dirs(task_dir))
    jobs_seen = len(job_dirs)
    started = jobs_seen > 0
    completed = result is not None

    final_status: str | None = None
    if isinstance(result, dict):
        status = result.get("status")
        final_status = str(status) if status is not None else None

    if completed:
        lifecycle = "completed"
    elif started:
        lifecycle = "running"
    else:
        lifecycle = "not_started"

    exception_files, failed = _count_exceptions(task_dir)

    precheck_failed = False
    if final_status == "solver_failed_precheck":
        precheck_failed = True
    elif not completed:
        precheck_failed = _infer_precheck_failure_without_task_result(
            task_dir, solver_precheck_retries
        )
        if precheck_failed:
            lifecycle = "precheck_failed_inferred"

    # Keep unknown completed status visible.
    if completed and final_status and final_status not in KNOWN_FINAL_STATUSES:
        lifecycle = "completed_unknown_status"

    return TaskStats(
        task_id=task_id,
        lifecycle_status=lifecycle,
        final_status=final_status,
        started=started,
        completed=completed,
        failed=failed,
        precheck_failed=precheck_failed,
        exception_files=exception_files,
        jobs_seen=jobs_seen,
    )


def aggregate(task_stats: list[TaskStats]) -> dict[str, Any]:
    counts_by_final_status: dict[str, int] = {}
    for t in task_stats:
        if t.final_status:
            counts_by_final_status[t.final_status] = (
                counts_by_final_status.get(t.final_status, 0) + 1
            )

    return {
        "total_tasks": len(task_stats),
        "started_tasks": sum(1 for t in task_stats if t.started),
        "completed_tasks": sum(1 for t in task_stats if t.completed),
        "not_started_tasks": sum(1 for t in task_stats if t.lifecycle_status == "not_started"),
        "running_tasks": sum(1 for t in task_stats if t.lifecycle_status == "running"),
        "failed_tasks": sum(1 for t in task_stats if t.failed),
        "exception_files": sum(t.exception_files for t in task_stats),
        "failed_precheck_tasks": sum(1 for t in task_stats if t.precheck_failed),
        "counts_by_final_status": counts_by_final_status,
    }


def print_report(summary: dict[str, Any], task_stats: list[TaskStats]) -> None:
    print("Batch Task Stats")
    print("=" * 72)
    print(f"Total tasks:           {summary['total_tasks']}")
    print(f"Started tasks:         {summary['started_tasks']}")
    print(f"Completed tasks:       {summary['completed_tasks']}")
    print(f"Not started tasks:     {summary['not_started_tasks']}")
    print(f"Running tasks:         {summary['running_tasks']}")
    print(f"Failed tasks:          {summary['failed_tasks']}")
    print(f"Exception files:       {summary['exception_files']}")
    print(f"Failed precheck tasks: {summary['failed_precheck_tasks']}")

    if summary["counts_by_final_status"]:
        print("\nFinal status counts:")
        for key in sorted(summary["counts_by_final_status"]):
            print(f"  {key:<24} {summary['counts_by_final_status'][key]}")

    print("\nPer-task statuses:")
    print("-" * 72)
    for t in task_stats:
        final = t.final_status or "-"
        print(
            f"{t.task_id}\t{t.lifecycle_status}\tfinal={final}\t"
            f"started={int(t.started)}\tcompleted={int(t.completed)}\t"
            f"failed={int(t.failed)}\tprecheck_failed={int(t.precheck_failed)}\t"
            f"exceptions={t.exception_files}\tjobs={t.jobs_seen}"
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze a harden batch directory and report in-progress/completed stats."
        )
    )
    parser.add_argument(
        "--batch-dir",
        required=True,
        help="Path to batch directory, e.g. shared_jobs_harden/batch_20260330_210530",
    )
    parser.add_argument(
        "--json-out",
        help="Optional output path to save full report JSON",
    )
    parser.add_argument(
        "--only-failed",
        action="store_true",
        help="Show only tasks flagged as failed (non-empty exception.txt).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit printed tasks (0 means no limit).",
    )
    parser.add_argument(
        "--sort",
        choices=["task_id", "lifecycle", "exceptions"],
        default="task_id",
        help="Sort per-task output.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    batch_dir = Path(args.batch_dir)

    if not batch_dir.is_dir():
        print(f"Not a directory: {batch_dir}", file=sys.stderr)
        return 2

    cfg = _load_json(batch_dir / "batch_config.json") or {}
    solver_precheck_retries = cfg.get("solver_precheck_retries")
    if not isinstance(solver_precheck_retries, int):
        solver_precheck_retries = None

    task_ids = _task_ids_from_batch_config(batch_dir)
    if not task_ids:
        task_ids = _task_ids_from_dirs(batch_dir)

    stats = [
        classify_task(batch_dir, task_id, solver_precheck_retries)
        for task_id in task_ids
    ]

    if args.only_failed:
        stats = [t for t in stats if t.failed]

    if args.sort == "lifecycle":
        stats.sort(key=lambda t: (t.lifecycle_status, t.task_id))
    elif args.sort == "exceptions":
        stats.sort(key=lambda t: (-t.exception_files, t.task_id))
    else:
        stats.sort(key=lambda t: t.task_id)

    if args.limit and args.limit > 0:
        printable = stats[:args.limit]
    else:
        printable = stats

    summary = aggregate(stats)
    print_report(summary, printable)

    if args.json_out:
        out = {
            "batch_dir": str(batch_dir),
            "summary": summary,
            "tasks": [asdict(t) for t in stats],
        }
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2))
        print(f"\nSaved JSON report: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())