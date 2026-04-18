"""CLI entry point: python -m harden"""

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from .batch import harden_batch
from .config import DEFAULT_TASKS_DIR, BatchHardenConfig, HardenConfig
from .loop import harden_task


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Adversarial hardening loop — oracle mode (KernelBench) or solver mode (generic).",
    )

    # Task selection — mutually exclusive
    task_group = parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument("--task-id", help="Single task ID to harden")
    task_group.add_argument(
        "--task-ids",
        help="Comma-separated list of task IDs to harden in batch",
    )
    task_group.add_argument(
        "--all", action="store_true", dest="all_tasks",
        help="Harden all tasks in tasks-dir",
    )

    parser.add_argument(
        "--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR,
        help=f"Base directory containing task environments (default: {DEFAULT_TASKS_DIR})",
    )
    parser.add_argument("--output-dir", type=Path, default=None)

    # Mode
    parser.add_argument(
        "--oracle", dest="oracle", action="store_true", default=False,
        help="Use deterministic oracle pre-check (KernelBench style: reference.py → solution.py). "
             "Default is solver-agent mode.",
    )

    # Models
    parser.add_argument("--hacker-model", default="gemini/gemini-3.1-pro-preview")
    parser.add_argument("--fixer-model", default="gemini/gemini-3.1-pro-preview")
    parser.add_argument("--solver-model", default="gemini/gemini-3.1-pro-preview",
                        help="Solver model (solver mode only; ignored when oracle is on)")

    # Thresholds (defaults suit reward-based tasks; KB overrides to 10 / 0.5)
    parser.add_argument("--hack-threshold", type=float, default=1.0,
                        help="Reward/speedup above this counts as a successful hack (default: 1.0)")
    parser.add_argument("--solver-threshold", type=float, default=1.0,
                        help="Reward/speedup the solver must meet to pass (default: 1.0)")

    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--hacker-retries", type=int, default=3)
    parser.add_argument("--solver-precheck-retries", type=int, default=1,
                        help="Pre-check retries (solver mode only; oracle is deterministic)")
    parser.add_argument("--legitimate-threshold", type=int, default=3)
    parser.add_argument("--no-legitimate-marker", dest="legitimate_marker",
                        action="store_false", default=True,
                        help="Disable the .legitimate sentinel (fixer can't flag hacks as legitimate)")

    parser.add_argument("--hacker-max-turns", type=int, default=None,
                        help="Max turns for hacker (default: None = use task timeout)")
    parser.add_argument("--fixer-max-turns", type=int, default=None,
                        help="Max turns for fixer (default: None = use task timeout)")
    parser.add_argument("--solver-max-turns", type=int, default=None,
                        help="Max turns for solver (solver mode only; default: None)")

    parser.add_argument("--solver-timeout-multiplier", type=float, default=2.0)
    parser.add_argument("--hacker-timeout-multiplier", type=float, default=2.0)
    parser.add_argument("--fixer-timeout-multiplier", type=float, default=10.0)

    parser.add_argument("--temperature", type=float, default=None,
                        help="Sampling temperature (default: None = agent default)")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="Max output tokens (default: None = model default)")
    parser.add_argument("--reasoning-effort", default=None,
                        choices=["none", "minimal", "low", "medium", "high"],
                        help="Reasoning effort (default: None = high)")

    parser.add_argument("--solver-privileged", action="store_true",
                        help="Inject /solution/ into the solver's env (solver mode only)")
    parser.add_argument("--hacker-feedback", action="store_true",
                        help="Give hacker read-only access to previous failed attempts")
    parser.add_argument("--hacker-privileged", action="store_true",
                        help="Give hacker read-only access to the evaluation environment "
                             "(tests/, environment/) at /eval_env/")

    # Targeted replay (post-solver gate) — reuses hacker knobs for model/turns/timeout.
    parser.add_argument("--replay-enabled", action="store_true",
                        help="After solver passes, re-attempt the specific prior exploit on the "
                             "patched task; if it re-lands, reject the fix.")
    parser.add_argument("--replay-retries", type=int, default=1,
                        help="Targeted-replay retries per iteration (default: 1)")

    # Harbor knobs
    parser.add_argument("-c", "--harbor-config", type=Path, default=None,
                        help="Path to a Harbor YAML/JSON config file for environment/agent/orchestrator defaults")
    parser.add_argument("--force-build", action="store_true",
                        help="Always rebuild Docker images (useful when Dockerfile changes are expected)")
    parser.add_argument("--image-name", default=None,
                        help="Override image name so harden runs don't clobber the base image")

    # Jumper / pooled mode
    parser.add_argument("--pool-enabled", action="store_true",
                        help="Enable jumper pooled mode: share a defense repo across tasks via "
                             "a host-side git daemon. Fixer containers clone/push to the pool.")
    parser.add_argument("--pool-bootstrap-dir", type=Path, default=None,
                        help="Hardened task dir to bootstrap the pool from (required if --pool-enabled).")
    parser.add_argument("--pool-port", type=int, default=9418,
                        help="Port for git daemon (default 9418; auto-bumps if busy).")

    # Batch-only
    parser.add_argument("--max-concurrent", type=int, default=4,
                        help="Max concurrent Docker containers (batch mode)")
    parser.add_argument("--resume", action="store_true",
                        help="Preserve existing output/hardened/<task>/ from a prior run; "
                             "in batch mode also skip tasks whose result.json is terminal.")

    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level (default: INFO)")

    return parser


def _config_kwargs(args: argparse.Namespace) -> dict:
    return dict(
        tasks_dir=args.tasks_dir,
        output_dir=args.output_dir,
        oracle=args.oracle,
        hacker_model=args.hacker_model,
        fixer_model=args.fixer_model,
        solver_model=args.solver_model,
        hack_threshold=args.hack_threshold,
        solver_threshold=args.solver_threshold,
        max_iterations=args.max_iterations,
        hacker_retries=args.hacker_retries,
        solver_precheck_retries=args.solver_precheck_retries,
        legitimate_threshold=args.legitimate_threshold,
        legitimate_marker=args.legitimate_marker,
        hacker_max_turns=args.hacker_max_turns,
        fixer_max_turns=args.fixer_max_turns,
        solver_max_turns=args.solver_max_turns,
        solver_timeout_multiplier=args.solver_timeout_multiplier,
        hacker_timeout_multiplier=args.hacker_timeout_multiplier,
        fixer_timeout_multiplier=args.fixer_timeout_multiplier,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
        solver_privileged=args.solver_privileged,
        hacker_feedback=args.hacker_feedback,
        hacker_privileged=args.hacker_privileged,
        replay_enabled=args.replay_enabled,
        replay_retries=args.replay_retries,
        harbor_config=args.harbor_config,
        force_build=args.force_build,
        image_name=args.image_name,
        pool_enabled=args.pool_enabled,
        pool_bootstrap_dir=args.pool_bootstrap_dir,
        pool_port=args.pool_port,
        resume=args.resume,
    )


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if args.output_dir is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output_dir = Path(f"outputs/batch_{ts}")

    if args.harbor_config is not None and not args.harbor_config.is_file():
        parser.error(f"Harbor config file not found: {args.harbor_config}")

    if args.pool_enabled:
        if args.pool_bootstrap_dir is None:
            parser.error("--pool-enabled requires --pool-bootstrap-dir")
        if not args.pool_bootstrap_dir.is_dir():
            parser.error(f"--pool-bootstrap-dir not a directory: {args.pool_bootstrap_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Output directory: %s", args.output_dir.resolve())

    if args.task_id:
        _run_single(args)
    else:
        _run_batch(args)


def _run_single(args: argparse.Namespace) -> None:
    config = HardenConfig(task_id=args.task_id, **_config_kwargs(args))

    async def _go():
        pool_server = None
        if config.pool_enabled:
            from .pool import PoolServer
            pool_server = PoolServer(
                pool_parent=config.output_dir,
                port=config.pool_port,
                bootstrap_from=config.pool_bootstrap_dir,
            )
            pool_server.start()
            logging.info("Pool server up at %s", pool_server.upstream_url)
        try:
            return await harden_task(config, pool_server=pool_server)
        finally:
            if pool_server is not None:
                pool_server.stop()

    result = asyncio.run(_go())

    status = result.get("status", "unknown")
    iterations = result.get("iterations", [])
    metric = "speedup" if config.oracle else "reward"

    print(f"\n{'='*60}")
    print(f"Task:       {config.task_id}")
    print(f"Mode:       {'oracle' if config.oracle else 'solver'}")
    print(f"Status:     {status}")
    print(f"Iterations: {len(iterations)}")
    for it in iterations:
        hr = it.get("hack_reward")
        hr_str = f"{hr:.2f}" if hr is not None else "N/A"
        print(f"  [{it['iteration']}] hack_{metric}={hr_str}  outcome={it.get('outcome')}")
    print(f"Result:     {config.result_path}")
    if "hardened_dir" in result:
        print(f"Hardened:   {result['hardened_dir']}")
    print(f"{'='*60}")


def _run_batch(args: argparse.Namespace) -> None:
    if args.all_tasks:
        task_ids = sorted(p.name for p in args.tasks_dir.iterdir() if p.is_dir())
    else:
        task_ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]

    config = BatchHardenConfig(
        task_ids=task_ids,
        max_concurrent_containers=args.max_concurrent,
        **_config_kwargs(args),
    )
    asyncio.run(harden_batch(config))


if __name__ == "__main__":
    main()
