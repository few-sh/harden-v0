"""Main hardening loop orchestration.

Two orthogonal mode flags:
  * `cfg.oracle`           — pre-check dispatch (deterministic vs agent solver)
  * `cfg.kernelbench_mode` — prompt/template bundle + metric semantics
                             (KB: speedup / `eval_kernel.py`; generic: reward /
                             `test_outputs.py`)

`_run_solver` routes on `cfg.oracle`:
  * True  — `run_oracle_solver` (deterministic reference copy)
  * False — `run_solver_agent` (Terminus-2 agent solver)

The threshold values (`cfg.hack_threshold`, `cfg.solver_threshold`) are
compared against whatever the verifier returns; the *name* of that metric
(speedup vs reward) follows `cfg.kernelbench_mode`, not `cfg.oracle`.
"""

import json
import logging
from pathlib import Path

from .agent import (
    read_verifier_output,
    run_fixer,
    run_hacker,
    run_oracle_solver,
    run_solver_agent,
)
from .config import HardenConfig
from .instructions import (
    HACKER_FEEDBACK_HINT,
    HACKER_PRIVILEGED_HINT,
    SOLVER_HINT,
    build_fixer_instruction,
    build_hacker_instruction,
    build_targeted_replay_instruction,
)
from .pool import PoolCursor, PoolServer
from .trajectory import extract_hack_summary
from .workspace import (
    append_to_instruction,
    apply_fixer_artifacts,
    create_hardened_copy,
    create_working_copy,
    extract_fixer_artifacts,
    prepare_fixer_environment,
    prepare_hacker_environment,
    prepare_privileged_hacker_environment,
    prepare_solver_environment,
    replace_instruction,
    update_hardened,
)

logger = logging.getLogger(__name__)


async def _run_targeted_replay(
    cfg: HardenConfig,
    hardened_task_dir: Path,
    fixer_trial: Path,
    hack_summary: str,
    original_instruction: str,
    output: Path,
    harden_image: str,
    iteration: int,
) -> float:
    """Up to cfg.replay_retries attempts; returns max reward observed."""
    best = 0.0
    for attempt in range(cfg.replay_retries):
        replay_parent = create_working_copy(hardened_task_dir, output / f"replay_task_iter{iteration}")
        apply_fixer_artifacts(replay_parent / cfg.task_id, fixer_trial)
        replace_instruction(
            replay_parent, cfg.task_id,
            build_targeted_replay_instruction(
                original_instruction, hack_summary, kernelbench_mode=cfg.kernelbench_mode
            ),
        )
        if cfg.hacker_privileged and prepare_privileged_hacker_environment(
            replay_parent, cfg.task_id
        ):
            append_to_instruction(replay_parent, cfg.task_id, HACKER_PRIVILEGED_HINT)

        reward, _ = await run_hacker(
            replay_parent, cfg.hacker_model, cfg.jobs_dir,
            role=f"replay_iter{iteration}_a{attempt}",
            max_turns=cfg.hacker_max_turns,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            reasoning_effort=cfg.reasoning_effort,
            timeout_multiplier=cfg.hacker_timeout_multiplier,
            harbor_config=cfg.harbor_config,
            # Rebuild to discard solver's possibly-privileged image.
            force_build=True,
            image_name=harden_image,
        )
        best = max(best, reward)
        logger.info("Replay %d/%d: reward=%.2f (threshold=%.2f).",
                    attempt + 1, cfg.replay_retries, reward, cfg.hack_threshold)
        if reward >= cfg.hack_threshold:
            return best
    return best


async def _run_solver(
    cfg: HardenConfig,
    task_parent: Path,
    role: str,
    force_build: bool,
    image_name: str | None,
) -> tuple[float, Path]:
    """Dispatch pre-check / validation to oracle or solver agent based on cfg.oracle."""
    if cfg.oracle:
        return await run_oracle_solver(
            task_parent,
            cfg.jobs_dir,
            role=role,
            timeout_multiplier=cfg.solver_timeout_multiplier,
            harbor_config=cfg.harbor_config,
            force_build=force_build or cfg.force_build,
            image_name=image_name or cfg.image_name,
        )
    return await run_solver_agent(
        task_parent,
        cfg.solver_model,
        cfg.jobs_dir,
        role=role,
        max_turns=cfg.solver_max_turns,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        reasoning_effort=cfg.reasoning_effort,
        timeout_multiplier=cfg.solver_timeout_multiplier,
        harbor_config=cfg.harbor_config,
        force_build=force_build or cfg.force_build,
        image_name=image_name or cfg.image_name,
    )


def _validate_task_dir(task_dir: Path) -> None:
    """Fail fast if the task dir is missing required pieces."""
    if not task_dir.is_dir():
        raise FileNotFoundError(f"Task directory not found: {task_dir}")
    required = ["instruction.md", "tests", "environment/Dockerfile"]
    missing = [r for r in required if not (task_dir / r).exists()]
    if missing:
        raise FileNotFoundError(
            f"Task {task_dir.name} missing required files: {missing}"
        )


async def harden_task(
    config: HardenConfig,
    pool_server: PoolServer | None = None,
) -> dict:
    """Run the full adversarial hardening loop for a single task.

    When `config.pool_enabled` AND `pool_server` is provided, the loop runs in
    pooled (jumper) mode: fixer containers clone/push a shared defense repo,
    and iterations where the pool has advanced since this task's last iteration
    skip the hacker step (treating it as a sync iteration).
    """
    if config.pool_enabled and pool_server is None:
        raise ValueError(
            "config.pool_enabled=True but pool_server was not passed to harden_task. "
            "Use pool_context() or pass pool_server explicitly."
        )
    pooled = config.pool_enabled and pool_server is not None
    original_dir = config.task_dir
    _validate_task_dir(original_dir)

    output = config.task_output_dir
    output.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "task_id": config.task_id,
        "status": "unknown",
        "iterations": [],
        "oracle": config.oracle,
        "pool_enabled": pooled,
        "kernelbench_mode": config.kernelbench_mode,
    }

    hardened_parent = create_hardened_copy(original_dir, output, resume=config.resume)
    hardened_task_dir = hardened_parent / config.task_id

    # Separate image name for harden runs so we don't clobber the base image.
    harden_image = config.image_name or f"{config.task_id}-harden"

    # Build solver/precheck working copy
    solver_parent = create_working_copy(hardened_task_dir, output / "solver_task")
    precheck_modified = False
    if not config.oracle and config.solver_privileged:
        if prepare_solver_environment(solver_parent, config.task_id, original_dir):
            append_to_instruction(solver_parent, config.task_id, SOLVER_HINT)
            precheck_modified = True

    # Pre-check: solver must pass the original task. Retries only meaningful for agent solver.
    precheck_retries = 1 if config.oracle else config.solver_precheck_retries
    precheck_passed = False
    reward = 0.0
    for attempt in range(precheck_retries):
        logger.info("Pre-check attempt %d/%d (oracle=%s)", attempt + 1, precheck_retries, config.oracle)
        reward, _ = await _run_solver(
            config,
            solver_parent,
            role=f"solver_precheck_a{attempt}",
            force_build=precheck_modified,
            image_name=harden_image if precheck_modified else None,
        )
        if reward >= config.solver_threshold:
            precheck_passed = True
            break
        logger.warning("Pre-check attempt %d failed (reward=%.2f < %.2f).",
                       attempt + 1, reward, config.solver_threshold)

    if not precheck_passed:
        logger.warning("Solver failed all pre-check attempts. Task may be unsolvable/broken.")
        result["status"] = "solver_failed_precheck"
        _save_result(config.result_path, result)
        return result

    logger.info("Pre-check passed (reward=%.2f). Starting hardening loop.", reward)

    reuse_hack: tuple[str, float] | None = None
    previous_failure: str | None = None
    previous_fixer_trial: Path | None = None
    previous_solver_trial: Path | None = None
    legitimate_streak: int = 0
    failed_hack_trials: list[Path] = []
    # Once hardened/ diverges from the base image, every build must use force_build + harden image.
    hardened_dirty: bool = False

    # Pooled (jumper) state — PoolCursor owns the advance/persist invariant.
    pool_cursor: PoolCursor | None = None
    if pooled:
        pool_cursor = PoolCursor(pool_server, config.task_output_dir, config.task_id)
        logger.info(
            "Pooled mode: last_seen pool SHA = %s",
            (pool_cursor.sha or "(fresh)")[:8],
        )

    for iteration in range(config.max_iterations):
        iter_info: dict = {
            "iteration": iteration,
            "hack_reward": None,
            "fix_applied": False,
            "replay_attempted": False,
            "replay_reward": None,
        }
        logger.info("=== Iteration %d ===", iteration)

        # Pool advance check (pooled mode only). iter_start() bumps the in-memory
        # cursor but does not persist — a crash mid-iter won't silently "consume"
        # pool commits we never actually integrated.
        pool_advanced = False
        pool_log = ""
        previous_last_seen = ""
        if pool_cursor is not None:
            pool_advanced, pool_log, previous_last_seen, current_pool_sha = (
                pool_cursor.iter_start()
            )
            iter_info["pool_sha_start"] = current_pool_sha
            iter_info["pool_advanced"] = pool_advanced
            if pool_advanced:
                logger.info(
                    "Pool advanced %s..%s — skipping hacker.",
                    previous_last_seen[:8], current_pool_sha[:8],
                )
                # Pool situation changed; the reused hack may no longer apply.
                reuse_hack = None

        if pool_advanced:
            hack_summary = "(no new hack this iteration — the shared pool has advanced; see the 'Pool advanced' section)"
            hack_reward = 0.0
        elif reuse_hack is not None:
            hack_summary, hack_reward = reuse_hack
            reuse_hack = None
            logger.info("Reusing previous hack (reward=%.2f) — fixer failed, nothing changed.", hack_reward)
        else:
            hack_succeeded = False
            attempt_failed_trials: list[Path] = []
            hack_reward = 0.0
            for attempt in range(config.hacker_retries):
                hacker_parent = create_working_copy(hardened_task_dir, output / "hacker_task")
                original_instruction = (original_dir / "instruction.md").read_text()
                hacker_instruction = build_hacker_instruction(
                    original_instruction, kernelbench_mode=config.kernelbench_mode
                )
                replace_instruction(hacker_parent, config.task_id, hacker_instruction)

                hacker_privileged_modified = False
                if config.hacker_privileged:
                    if prepare_privileged_hacker_environment(hacker_parent, config.task_id):
                        append_to_instruction(hacker_parent, config.task_id, HACKER_PRIVILEGED_HINT)
                        hacker_privileged_modified = True

                all_failed = failed_hack_trials + attempt_failed_trials
                hacker_feedback_modified = (config.hacker_feedback and bool(all_failed))
                if hacker_feedback_modified:
                    prepare_hacker_environment(hacker_parent, config.task_id, all_failed)
                    append_to_instruction(hacker_parent, config.task_id, HACKER_FEEDBACK_HINT)

                hacker_dockerfile_modified = hacker_privileged_modified or hacker_feedback_modified
                hacker_needs_rebuild = hacker_dockerfile_modified or hardened_dirty
                hack_reward, hacker_trial = await run_hacker(
                    hacker_parent,
                    config.hacker_model,
                    config.jobs_dir,
                    role=f"hacker_iter{iteration}_a{attempt}",
                    max_turns=config.hacker_max_turns,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                    reasoning_effort=config.reasoning_effort,
                    timeout_multiplier=config.hacker_timeout_multiplier,
                    harbor_config=config.harbor_config,
                    force_build=hacker_needs_rebuild or config.force_build,
                    image_name=harden_image if hacker_needs_rebuild else config.image_name,
                )
                if hack_reward >= config.hack_threshold:
                    hack_succeeded = True
                    break
                logger.info("Hacker attempt %d/%d: reward=%.2f (threshold=%.2f).",
                            attempt + 1, config.hacker_retries, hack_reward, config.hack_threshold)
                attempt_failed_trials.append(hacker_trial)

            if not hack_succeeded:
                logger.info("Hacker failed all %d attempts. Task is robust!", config.hacker_retries)
                iter_info["hack_reward"] = hack_reward
                iter_info["outcome"] = "hacker_failed"
                result["iterations"].append(iter_info)
                result["status"] = "robust"
                break

            failed_hack_trials = []
            logger.info("Hacker succeeded (reward=%.2f >= %.2f). Extracting trajectory.",
                        hack_reward, config.hack_threshold)
            hack_summary = extract_hack_summary(hacker_trial)

        iter_info["hack_reward"] = hack_reward if not pool_advanced else None
        original_instruction = (original_dir / "instruction.md").read_text()

        fixer_instruction = build_fixer_instruction(
            original_instruction, hack_summary, previous_failure,
            has_previous_attempt=previous_fixer_trial is not None,
            has_previous_solver=previous_solver_trial is not None,
            oracle=config.oracle,
            kernelbench_mode=config.kernelbench_mode,
            legitimate_marker=config.legitimate_marker,
            pool_enabled=pooled,
            pool_log=pool_log if pool_advanced else None,
            last_seen_sha=previous_last_seen,
            task_id=config.task_id,
            iteration=iteration,
        )
        fixer_parent = create_working_copy(hardened_task_dir, output / "fixer_task")
        replace_instruction(fixer_parent, config.task_id, fixer_instruction)
        prepare_fixer_environment(
            fixer_parent, config.task_id, previous_fixer_trial, previous_solver_trial,
            pool_upstream_url=pool_server.upstream_url if pooled else None,
        )

        # Fixer always modifies the Dockerfile → force_build with harden image.
        _, fixer_trial = await run_fixer(
            fixer_parent,
            config.fixer_model,
            config.jobs_dir,
            role=f"fixer_iter{iteration}",
            max_turns=config.fixer_max_turns,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
            timeout_multiplier=config.fixer_timeout_multiplier,
            harbor_config=config.harbor_config,
            force_build=True,
            image_name=harden_image,
        )

        fix_applied = False
        try:
            solver_parent = create_working_copy(hardened_task_dir, output / "solver_validate")
            fix_result = extract_fixer_artifacts(
                fixer_trial, solver_parent, config.task_id,
                kernelbench_mode=config.kernelbench_mode,
                legitimate_marker=config.legitimate_marker,
            )
            # Shared pool-sync-noop path: on a pool-advanced iter, either
            # `no_changes` or `legitimate` from the fixer are valid acks of
            # the pool advance (nothing to port locally / no hack to legitimize).
            # Both collapse to the same cleanup + continue.
            pool_sync_noop_reason: str | None = None
            if pool_advanced and fix_result == "no_changes":
                pool_sync_noop_reason = (
                    "Fixer made no local changes (pool-sync iteration acknowledged)."
                )
            elif pool_advanced and fix_result == "legitimate":
                pool_sync_noop_reason = (
                    "Fixer marked .legitimate in a pool-sync iter; "
                    "ignoring (no hack to legitimize)."
                )
            if pool_sync_noop_reason is not None:
                logger.info(pool_sync_noop_reason)
                iter_info["outcome"] = "pool_sync_noop"
                reuse_hack = None
                previous_failure = None
                previous_fixer_trial = None
                previous_solver_trial = None
                legitimate_streak = 0
                if pool_cursor is not None:
                    pool_cursor.persist()
                result["iterations"].append(iter_info)
                continue

            if fix_result == "no_changes":
                logger.warning("Fixer did not commit any changes")
                previous_failure = (
                    "Fixer did not commit any changes. You MUST commit: "
                    "cd /logs/artifacts && git add -A && git commit -m 'fix'"
                )
                legitimate_streak = 0
                iter_info["outcome"] = "no_changes"
            elif fix_result == "legitimate":
                legitimate_streak += 1
                logger.info("Fixer marked hack as legitimate (%d/%d).",
                            legitimate_streak, config.legitimate_threshold)
                iter_info["outcome"] = "legitimate"
                iter_info["fix_applied"] = False
                if pool_cursor is not None:
                    pool_cursor.persist()
                if legitimate_streak >= config.legitimate_threshold:
                    logger.info("Confirmed legitimate after %d consecutive flags. Task is robust.",
                                config.legitimate_threshold)
                    result["iterations"].append(iter_info)
                    result["status"] = "robust"
                    break
                reuse_hack = None
                previous_failure = None
                previous_fixer_trial = None
                previous_solver_trial = None
                result["iterations"].append(iter_info)
                continue
            else:  # "applied"
                validate_modified = False
                if not config.oracle and config.solver_privileged:
                    if prepare_solver_environment(solver_parent, config.task_id, original_dir):
                        append_to_instruction(solver_parent, config.task_id, SOLVER_HINT)
                        validate_modified = True

                solver_needs_rebuild = validate_modified or hardened_dirty
                solver_reward, solver_trial = await _run_solver(
                    config,
                    solver_parent,
                    role=f"solver_validate_iter{iteration}",
                    force_build=solver_needs_rebuild,
                    image_name=harden_image if solver_needs_rebuild else None,
                )

                if solver_reward >= config.solver_threshold:
                    logger.info("Fix validated — solver passes (reward=%.2f).", solver_reward)

                    replay_reward: float | None = None
                    # Skip replay on pool-sync iters: hack_summary is a sentinel
                    # placeholder (no real hack this iter) so replay has nothing
                    # meaningful to reproduce.
                    if config.replay_enabled and not pool_advanced:
                        iter_info["replay_attempted"] = True
                        replay_reward = await _run_targeted_replay(
                            config, hardened_task_dir, fixer_trial, hack_summary,
                            original_instruction, output, harden_image, iteration,
                        )
                        iter_info["replay_reward"] = replay_reward

                    if replay_reward is not None and replay_reward >= config.hack_threshold:
                        logger.warning("Targeted replay reproduced exploit (reward=%.2f). Rejecting fix.",
                                       replay_reward)
                        previous_failure = (
                            f"PREVIOUS FIX WAS INSUFFICIENT — solver accepted the fix, but a "
                            f"targeted-replay agent reproduced the exploit on the patched task "
                            f"(reward={replay_reward:.2f} >= threshold={config.hack_threshold:.2f}). "
                            f"The fix was too narrow — widen it so the specific exploit no longer works."
                        )
                        previous_fixer_trial = fixer_trial
                        previous_solver_trial = None
                        legitimate_streak = 0
                        iter_info["outcome"] = "replay_broke_fix"
                    else:
                        update_hardened(hardened_task_dir, fixer_trial)
                        fix_applied = True
                        hardened_dirty = True
                        previous_failure = None
                        previous_fixer_trial = None
                        previous_solver_trial = None
                        legitimate_streak = 0
                        if pool_cursor is not None:
                            # Prevents next iter from burning a pool-sync cycle
                            # just to ack our own push; `advance_...if_newer`
                            # is a no-op when we didn't push this iter.
                            own_sha = pool_cursor.advance_to_own_commit_if_newer()
                            if own_sha:
                                iter_info["pool_own_commit"] = own_sha
                            pool_cursor.persist()
                else:
                    logger.warning("Fix broke solver (reward=%.2f < %.2f). Reverting.",
                                   solver_reward, config.solver_threshold)
                    previous_failure = read_verifier_output(solver_trial)
                    previous_fixer_trial = fixer_trial
                    previous_solver_trial = solver_trial
                    legitimate_streak = 0
        except Exception as e:
            logger.warning("Fixer produced invalid artifacts: %s", e)
            previous_failure = str(e)
            legitimate_streak = 0

        if not fix_applied and not pool_advanced:
            # Reuse the hack next iter if the fixer failed, but only when we
            # actually had a hack. On pool-sync iters hack_summary is a
            # sentinel string — reusing it would hand the next fixer nonsense.
            reuse_hack = (hack_summary, hack_reward)

        iter_info["fix_applied"] = fix_applied
        iter_info.setdefault("outcome", "fixed" if fix_applied else "fix_failed")
        result["iterations"].append(iter_info)
    else:
        result["status"] = "max_iterations"

    result["hardened_dir"] = str(hardened_task_dir)
    _save_result(config.result_path, result)
    return result


def _save_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str))
