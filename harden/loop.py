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

Concurrency model (batch mode):
    `_harden_task_phases` is an async generator that yields once per iteration,
    after validate+replay (the phase that may persist pool state). The batch
    driver advances all active task generators together via asyncio.gather.
    The semaphore (passed in by the batch) limits concurrent container runs
    across all tasks.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from . import durable, journal
from .agent import (
    extract_failure_summary,
    is_trial_setup_failure,
    read_verifier_output,
    run_fixer,
    run_hacker,
    run_oracle_solver,
    run_solver_agent,
)
from .config import HardenConfig
from .precheck_cache import build_precheck_cache_key, load_cached_precheck, store_cached_precheck
from .instructions import (
    HACKER_FEEDBACK_HINT,
    HACKER_PRIVILEGED_HINT,
    SOLVER_HINT,
    build_fixer_instruction,
    build_hacker_instruction,
    build_targeted_replay_instruction,
)
from .pool import PoolCursor, PoolServer
from .trajectory import extract_fix_summary, extract_hack_summary, llm_summarize_fix, llm_summarize_hack
from .workspace import (
    append_to_instruction,
    apply_fixer_artifacts,
    create_hardened_copy,
    create_working_copy,
    extract_fixer_artifacts,
    prepare_fixer_environment,
    prepare_hacker_environment,
    prepare_journal_mount,
    prepare_privileged_hacker_environment,
    prepare_solver_environment,
    replace_instruction,
    update_hardened,
)

logger = logging.getLogger(__name__)


def _resolved_summary_model(cfg: "HardenConfig") -> str:
    """Pick the model used for trajectory summaries (hack + fix) and the ledger.

    ``cfg.summary_model`` overrides; falls back to ``cfg.fixer_model``. Empty
    string disables LLM summarization entirely (returns "").
    """
    raw_model = cfg.summary_model
    if raw_model is None:
        raw_model = cfg.fixer_model
    return raw_model or ""


async def _summarize_hack_attempt(cfg: "HardenConfig", trial_dir: Path) -> None:
    """Generate LLM summary for a hack attempt; cached to <trial_dir>/agent/hack_summary.txt.

    Uses ``cfg.summary_model`` (falls back to ``cfg.fixer_model`` when None).
    Skipped entirely when the resolved model is empty.
    """
    model = _resolved_summary_model(cfg)
    if not model:
        return
    try:
        await llm_summarize_hack(trial_dir, model=model, reasoning_effort=cfg.reasoning_effort)
    except Exception as exc:
        logger.error("Hack summarization failed for %s: %s", trial_dir, exc, exc_info=True)


async def _summarize_fix_attempt(
    cfg: "HardenConfig", trial_dir: Path, outcome: str,
) -> None:
    """Generate LLM summary for a fix attempt; cached to <trial_dir>/agent/fix_summary.txt.

    Same model-resolution rules as the hack summarizer. ``outcome`` is the
    loop-determined verdict (fixed / fix_failed / replay_broke_fix / etc.)
    and gets woven into the prompt as authoritative.
    """
    model = _resolved_summary_model(cfg)
    if not model:
        return
    try:
        await llm_summarize_fix(
            trial_dir, model=model, reasoning_effort=cfg.reasoning_effort, outcome=outcome,
        )
    except Exception as exc:
        logger.error("Fix summarization failed for %s: %s", trial_dir, exc, exc_info=True)


async def _record_iter_in_journal(
    cfg: "HardenConfig",
    iteration: int,
    outcome: str,
    fix_applied: bool,
    hack_reward: float | None,
    hack_summary: str | None,
    fixer_trial: Path | None,
    notes: str | None = None,
    failure_detail: str | None = None,
) -> None:
    """Append one iter to the journal.

    ``hack_summary`` is the already-extracted markdown (the loop has it in
    scope via `extract_hack_summary(hacker_trial)` at each call site, or
    the reused-hack tuple). Pass None on pool-sync iters and when no hack
    ran. ``fixer_trial`` is summarized inline (the fix summary isn't cached
    upstream).

    Only the accepted-fix path (``fix_applied=True``) triggers a ledger
    refresh — reverted/replay-broken iters write the per-iter file but
    don't modify ``defenses_in_force.md``. Errors are logged and swallowed:
    journal failures must never break the loop.
    """
    if not cfg.journal_enabled:
        return
    try:
        fix_summary: str | None = None
        if fixer_trial is not None:
            await _summarize_fix_attempt(cfg, fixer_trial, outcome)
            fix_summary = extract_fix_summary(fixer_trial)

        ledger_model = _resolved_summary_model(cfg) if fix_applied else ""
        await journal.append_iter(
            cfg.task_output_dir,
            iteration=iteration,
            outcome=outcome,
            fix_applied=fix_applied,
            hack_reward=hack_reward,
            hack_summary=hack_summary,
            fix_summary=fix_summary,
            fixer_trial=fixer_trial,
            notes=notes,
            failure_detail=failure_detail,
            ledger_model=ledger_model or None,
            reasoning_effort=cfg.reasoning_effort,
            compact_max_iters=cfg.journal_compact_max_iters,
        )
    except Exception as exc:
        logger.error("Journal recording failed for iter %d: %s", iteration, exc, exc_info=True)


def _hacker_privileged_enabled(cfg: HardenConfig, iteration: int) -> bool:
    if not cfg.hacker_privileged:
        return False
    if cfg.hacker_privileged_enable_iteration is not None:
        return iteration >= cfg.hacker_privileged_enable_iteration
    return iteration < cfg.hacker_privileged_disable_iteration


def _can_mark_robust(cfg: HardenConfig, privileged_hack_ran: bool) -> bool:
    if cfg.hacker_privileged_enable_iteration is None:
        return True
    if not cfg.hacker_privileged:
        return True
    return privileged_hack_ran


async def _run_targeted_replay(
    cfg: HardenConfig,
    hardened_task_dir: Path,
    fixer_trial: Path,
    hack_summary: str,
    original_instruction: str,
    output: Path,
    harden_image: str,
    iteration: int,
    hack_iteration: int,
) -> float:
    """Up to cfg.replay_retries attempts; returns max reward observed."""
    best = 0.0
    journal_text = journal.read_compact(cfg.task_output_dir) if cfg.journal_enabled else None
    for attempt in range(cfg.replay_retries):
        replay_parent = create_working_copy(hardened_task_dir, output / f"replay_task_iter{iteration}")
        apply_fixer_artifacts(replay_parent / cfg.task_id, fixer_trial)
        replace_instruction(
            replay_parent, cfg.task_id,
            build_targeted_replay_instruction(
                original_instruction, hack_summary,
                kernelbench_mode=cfg.kernelbench_mode,
                journal_text=journal_text,
            ),
        )
        if _hacker_privileged_enabled(cfg, hack_iteration) and prepare_privileged_hacker_environment(
            replay_parent, cfg.task_id
        ):
            append_to_instruction(replay_parent, cfg.task_id, HACKER_PRIVILEGED_HINT)
        if cfg.journal_enabled:
            prepare_journal_mount(replay_parent, cfg.task_id, cfg.task_output_dir)

        reward, _ = await run_hacker(
            replay_parent, cfg.hacker_model, cfg.jobs_dir,
            role=f"replay_h{hack_iteration}_a{attempt}",
            max_turns=cfg.hacker_max_turns,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            reasoning_effort=cfg.reasoning_effort,
            timeout_multiplier=cfg.hacker_timeout_multiplier,
            harbor_config=cfg.harbor_config,
            # Rebuild to discard solver's possibly-privileged image.
            force_build=True,
            image_name=harden_image,
            kernelbench_mode=cfg.kernelbench_mode,
        )
        best = max(best, reward)
        logger.info("Replay %d/%d: reward=%.2f (threshold=%.2f).",
                    attempt + 1, cfg.replay_retries, reward, cfg.hack_threshold)
        if reward >= cfg.hack_threshold:
            return best
    return best


def _resolve_image_name(cfg: HardenConfig) -> str | None:
    """Return cfg.image_name with {task_id} substituted, or None if unset."""
    if not cfg.image_name:
        return None
    return cfg.image_name.format(task_id=cfg.task_id)


async def _run_solver(
    cfg: HardenConfig,
    task_parent: Path,
    role: str,
    force_build: bool,
    image_name: str | None,
) -> tuple[float, Path]:
    """Dispatch pre-check / validation to oracle or solver agent based on cfg.oracle."""
    fallback = _resolve_image_name(cfg)
    if cfg.oracle:
        return await run_oracle_solver(
            task_parent,
            cfg.jobs_dir,
            role=role,
            timeout_multiplier=cfg.solver_timeout_multiplier,
            harbor_config=cfg.harbor_config,
            force_build=force_build or cfg.force_build,
            image_name=image_name or fallback,
            kernelbench_mode=cfg.kernelbench_mode,
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
        image_name=image_name or fallback,
        kernelbench_mode=cfg.kernelbench_mode,
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


def _save_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str))


def _mark_interrupted(path: Path) -> None:
    """Flip a mid-flight result.json to status="interrupted" on cancellation.

    Terminal statuses (robust, max_iterations, …) are saved before the loop
    exits, so only an in-flight "unknown" is rewritten. A missing or corrupt
    file is left alone — marking must never mask the cancellation itself.
    """
    try:
        result = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(result, dict) and result.get("status") == "unknown":
        result["status"] = "interrupted"
        _save_result(path, result)


async def _harden_task_phases(
    config: HardenConfig,
    pool_server: PoolServer | None,
    semaphore: asyncio.Semaphore,
    result_out: list,
    initial_delay: float = 0.0,
) -> AsyncGenerator[None, None]:
    """Async generator that runs the hardening loop for one task.

    Yields once per iteration so that a batch driver can advance all tasks
    together at the same fence:

        yield — after validate+replay (per iteration)

    This fence is intentionally placed after the phase that may write/persist
    pool state so all tasks observe a consistent pool view at iteration
    boundaries. Tasks that terminate early (robust, legitimate threshold,
    precheck failure) return before the next yield; the batch sees
    StopAsyncIteration and marks them done.

    `semaphore` limits concurrent container runs across all tasks/phases.
    Results are written into `result_out` (list) as a single dict.
    """
    if config.pool_enabled and pool_server is None:
        raise ValueError(
            "config.pool_enabled=True but pool_server was not passed. "
            "Use pool_context() or pass pool_server explicitly."
        )
    pooled = config.pool_enabled and pool_server is not None
    original_dir = config.task_dir
    _validate_task_dir(original_dir)

    # Job-cache namespace: a config fingerprint. All batch tasks share the
    # same fingerprint (only task_id differs, and task_id is excluded), so
    # this is idempotent across concurrent tasks.
    durable.set_namespace(config.fingerprint())

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
    # Persist the skeleton immediately. A single iteration can run for an hour,
    # so a run interrupted before its first post-iteration save (Ctrl-C, CI
    # timeout) would otherwise leave no result.json at all; with this, every
    # interrupt finds a parseable result holding the iterations completed so
    # far (each later mutation re-saves over it).
    _save_result(config.result_path, result)

    hardened_parent = create_hardened_copy(original_dir, output, resume=config.resume)
    hardened_task_dir = hardened_parent / config.task_id

    # Separate image name for harden runs so we don't clobber the base image.
    # If image_name contains "{task_id}", substitute it so parallel runs over the
    # same tasks get disjoint image-tag namespaces (e.g. "kb-priv-pool-{task_id}").
    if config.image_name:
        harden_image = config.image_name.format(task_id=config.task_id)
    else:
        harden_image = f"{config.task_id}-harden"

    # Build solver/precheck working copy
    solver_parent = create_working_copy(hardened_task_dir, output / "solver_task")
    precheck_modified = False
    if not config.oracle and config.solver_privileged:
        if prepare_solver_environment(solver_parent, config.task_id, original_dir):
            append_to_instruction(solver_parent, config.task_id, SOLVER_HINT)
            precheck_modified = True

    # ── PRECHECK PHASE ────────────────────────────────────────────────────────
    # Pre-check: solver must pass the original task. Retries only meaningful for agent solver.
    precheck_retries = 1 if config.oracle else config.solver_precheck_retries
    precheck_passed = False
    reward = 0.0

    _cache_key: str | None = None
    _cache_material: dict | None = None
    _cache_hit = False
    if not config.oracle:
        _cache_key, _cache_material = build_precheck_cache_key(task_dir=original_dir, config=config)
        _cached = load_cached_precheck(_cache_key, retry_failed=config.retry_failed_prechecks)
        if _cached is not None:
            reward = _cached.get("reward", 0.0)
            precheck_passed = reward >= config.solver_threshold
            _cache_hit = True
            logger.info("Pre-check cache hit (reward=%.2f, passed=%s).", reward, precheck_passed)

    if not _cache_hit:
        _precheck_trial: Path | None = None
        if initial_delay:
            await asyncio.sleep(initial_delay)
        async with semaphore:
            for attempt in range(precheck_retries):
                logger.info("Pre-check attempt %d/%d (oracle=%s)", attempt + 1, precheck_retries, config.oracle)
                reward, _precheck_trial = await _run_solver(
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
        if _cache_key is not None and _cache_material is not None and _precheck_trial is not None:
            store_cached_precheck(
                _cache_key,
                {"reward": reward, "passed": precheck_passed, "trial_dir": str(_precheck_trial)},
                key_material=_cache_material,
            )

    if not precheck_passed:
        logger.warning("Solver failed all pre-check attempts. Task may be unsolvable/broken.")
        result["status"] = "solver_failed_precheck"
        _save_result(config.result_path, result)
        result_out.append(result)
        return  # done — exits before first yield

    logger.info("Pre-check passed (reward=%.2f). Starting hardening loop.", reward)

    # ── Cross-iteration state ─────────────────────────────────────────────────
    reuse_hack: tuple[str, float] | None = None
    previous_failure: str | None = None
    previous_outcome: str | None = None  # "trial_setup_failed" / "fix_failed" / "replay_broke_fix" / ...
    previous_fixer_trial: Path | None = None
    previous_solver_trial: Path | None = None
    legitimate_streak: int = 0
    failed_hack_trials: list[Path] = []
    # Once hardened/ diverges from the base image, every build must use force_build + harden image.
    hardened_dirty: bool = False
    # Tracks whether any iter has actually run the hacker with privileges. Only
    # consulted when cfg.hacker_privileged_enable_iteration is set — see
    # `_can_mark_robust`: a non-privileged hacker failure cannot terminate the
    # run in that mode.
    privileged_hack_ran: bool = False

    # Pooled (jumper) state — PoolCursor owns the advance/persist invariant.
    pool_cursor: PoolCursor | None = None
    if pooled:
        pool_cursor = PoolCursor(pool_server, config.task_output_dir, config.task_id)
        logger.info(
            "Pooled mode: last_seen pool SHA = %s",
            (pool_cursor.sha or "(fresh)")[:8],
        )

    # Pool-sync iters don't count toward max_iterations — only
    # iterations where the hacker actually runs consume the budget.
    hack_iterations = 0
    # After pool_max_consecutive_syncs skips in a row, force the
    # hacker regardless of pool state.
    consecutive_pool_syncs = 0
    iteration = -1

    while hack_iterations < config.max_iterations:
        iteration += 1
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
        current_pool_sha = ""
        if pool_cursor is not None:
            pool_advanced, pool_log, previous_last_seen, current_pool_sha = (
                pool_cursor.iter_start()
            )
            iter_info["pool_sha_start"] = current_pool_sha
            iter_info["pool_advanced"] = pool_advanced
            if pool_advanced:
                if consecutive_pool_syncs >= config.pool_max_consecutive_syncs:
                    # Option 2: consecutive sync cap hit — force the hacker.
                    logger.info(
                        "Pool advanced but %d consecutive sync(s) at limit (K=%d) — forcing hacker.",
                        consecutive_pool_syncs, config.pool_max_consecutive_syncs,
                    )
                    pool_advanced = False
                    pool_log = ""
                    iter_info["pool_sync_forced_hack"] = True
                    reuse_hack = None
                else:
                    logger.info(
                        "Pool advanced %s..%s — skipping hacker.",
                        previous_last_seen[:8], current_pool_sha[:8],
                    )
                    consecutive_pool_syncs += 1
                    # Pool situation changed; the reused hack may no longer apply.
                    reuse_hack = None

        # Only count iterations where the hacker runs toward max_iterations.
        if not pool_advanced:
            hack_iterations += 1
            consecutive_pool_syncs = 0

        # ── HACK PHASE ────────────────────────────────────────────────────────
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
            journal_text = (
                journal.read_compact(config.task_output_dir)
                if config.journal_enabled else None
            )
            async with semaphore:
                for attempt in range(config.hacker_retries):
                    hacker_parent = create_working_copy(hardened_task_dir, output / "hacker_task")
                    original_instruction = (original_dir / "instruction.md").read_text()
                    hacker_instruction = build_hacker_instruction(
                        original_instruction,
                        kernelbench_mode=config.kernelbench_mode,
                        journal_text=journal_text,
                    )
                    replace_instruction(hacker_parent, config.task_id, hacker_instruction)

                    hacker_privileged_modified = False
                    if _hacker_privileged_enabled(config, hack_iterations - 1):
                        if prepare_privileged_hacker_environment(hacker_parent, config.task_id):
                            append_to_instruction(hacker_parent, config.task_id, HACKER_PRIVILEGED_HINT)
                            hacker_privileged_modified = True

                    all_failed = failed_hack_trials + attempt_failed_trials
                    hacker_feedback_modified = (config.hacker_feedback and bool(all_failed))
                    if hacker_feedback_modified:
                        prepare_hacker_environment(hacker_parent, config.task_id, all_failed)
                        append_to_instruction(hacker_parent, config.task_id, HACKER_FEEDBACK_HINT)

                    journal_modified = False
                    if config.journal_enabled:
                        journal_modified = prepare_journal_mount(
                            hacker_parent, config.task_id, config.task_output_dir,
                        )

                    hacker_dockerfile_modified = (
                        hacker_privileged_modified or hacker_feedback_modified or journal_modified
                    )
                    hacker_needs_rebuild = hacker_dockerfile_modified or hardened_dirty
                    hack_reward, hacker_trial = await run_hacker(
                        hacker_parent,
                        config.hacker_model,
                        config.jobs_dir,
                        role=f"hacker_h{hack_iterations - 1}_a{attempt}",
                        max_turns=config.hacker_max_turns,
                        temperature=config.temperature,
                        max_tokens=config.max_tokens,
                        reasoning_effort=config.reasoning_effort,
                        timeout_multiplier=config.hacker_timeout_multiplier,
                        harbor_config=config.harbor_config,
                        force_build=hacker_needs_rebuild or config.force_build,
                        image_name=harden_image if hacker_needs_rebuild else _resolve_image_name(config),
                        kernelbench_mode=config.kernelbench_mode,
                    )
                    if hacker_privileged_modified:
                        privileged_hack_ran = True
                    if hack_reward >= config.hack_threshold:
                        hack_succeeded = True
                        break
                    logger.info("Hacker attempt %d/%d: reward=%.2f (threshold=%.2f).",
                                attempt + 1, config.hacker_retries, hack_reward, config.hack_threshold)
                    attempt_failed_trials.append(hacker_trial)
                    await _summarize_hack_attempt(config, hacker_trial)

            if not hack_succeeded:
                iter_info["hack_reward"] = hack_reward
                iter_info["outcome"] = "hacker_failed"
                if not _can_mark_robust(config, privileged_hack_ran):
                    logger.info(
                        "Hacker failed all %d attempts, but --hacker-privileged-enable-iteration=%d "
                        "is set and no privileged hacker has run yet — not marking robust; continuing.",
                        config.hacker_retries, config.hacker_privileged_enable_iteration,
                    )
                    result["iterations"].append(iter_info)
                    _save_result(config.result_path, result)
                    await _record_iter_in_journal(
                        config, iteration=iteration, outcome="hacker_failed",
                        fix_applied=False, hack_reward=hack_reward,
                        hack_summary=extract_hack_summary(hacker_trial), fixer_trial=None,
                        notes=(
                            f"Hacker failed all {config.hacker_retries} attempts but "
                            f"privileged hacker has not yet run; continuing."
                        ),
                    )
                    failed_hack_trials = []
                    yield  # ── iteration fence: keep batch driver in lockstep ─
                    continue
                logger.info("Hacker failed all %d attempts. Task is robust!", config.hacker_retries)
                result["iterations"].append(iter_info)
                result["status"] = "robust"
                result["hardened_dir"] = str(hardened_task_dir)
                _save_result(config.result_path, result)
                await _record_iter_in_journal(
                    config, iteration=iteration, outcome="hacker_failed",
                    fix_applied=False, hack_reward=hack_reward,
                    hack_summary=extract_hack_summary(hacker_trial), fixer_trial=None,
                    notes=f"Hacker failed all {config.hacker_retries} attempts; task is robust.",
                )
                result_out.append(result)
                return  # done — exits before hack yield

            failed_hack_trials = []
            logger.info("Hacker succeeded (reward=%.2f >= %.2f). Extracting trajectory.",
                        hack_reward, config.hack_threshold)
            await _summarize_hack_attempt(config, hacker_trial)
            hack_summary = extract_hack_summary(hacker_trial)

        iter_info["hack_reward"] = hack_reward if not pool_advanced else None
        original_instruction = (original_dir / "instruction.md").read_text()

        # ── FIX PHASE ─────────────────────────────────────────────────────────
        fixer_journal_text = (
            journal.read_compact(config.task_output_dir)
            if config.journal_enabled else None
        )
        # Re-read each iter so edits to the file mid-run take effect on the
        # next iter. Swallow missing-file / read errors so a misconfigured
        # path can't break the loop — log once and continue without the
        # extra guidance. `fixer_prompt_after_iter` gates injection by
        # iteration index: prompt only included when iter > threshold
        # (default -1 → always include).
        custom_fixer_prompt: str | None = None
        if (
            config.fixer_prompt_file is not None
            and iteration > config.fixer_prompt_after_iter
        ):
            try:
                custom_fixer_prompt = config.fixer_prompt_file.read_text()
            except OSError as exc:
                logger.warning(
                    "Could not read fixer_prompt_file=%s: %s — skipping additional guidance.",
                    config.fixer_prompt_file, exc,
                )
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
            journal_text=fixer_journal_text,
            previous_outcome=previous_outcome,
            custom_fixer_prompt=custom_fixer_prompt,
        )
        fixer_parent = create_working_copy(hardened_task_dir, output / "fixer_task")
        replace_instruction(fixer_parent, config.task_id, fixer_instruction)
        prepare_fixer_environment(
            fixer_parent, config.task_id, previous_fixer_trial, previous_solver_trial,
            pool_upstream_url=pool_server.upstream_url if pooled else None,
        )
        if config.journal_enabled:
            prepare_journal_mount(fixer_parent, config.task_id, config.task_output_dir)

        # Role string: real iters use a stable hack-iter counter so resume
        # cache hits across runs; pool-sync iters embed both pool SHAs so
        # the cache always misses (live pool state can't be replayed).
        fixer_role = (
            f"fixer_pool_{previous_last_seen[:8]}_{current_pool_sha[:8]}"
            if pool_advanced
            else f"fixer_h{hack_iterations - 1}"
        )
        # Fixer always modifies the Dockerfile → force_build with harden image.
        async with semaphore:
            _, fixer_trial = await run_fixer(
                fixer_parent,
                config.fixer_model,
                config.jobs_dir,
                role=fixer_role,
                max_turns=config.fixer_max_turns,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                reasoning_effort=config.reasoning_effort,
                timeout_multiplier=config.fixer_timeout_multiplier,
                harbor_config=config.harbor_config,
                force_build=True,
                image_name=harden_image,
            )

        # ── VALIDATE + REPLAY PHASE ───────────────────────────────────────────
        fix_applied = False
        # True when iter_info is appended inside this block (pool-sync / legitimate
        # early-exit paths); prevents a double-append at the end of the iteration.
        _iter_appended = False
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
                previous_outcome = None
                previous_fixer_trial = None
                previous_solver_trial = None
                legitimate_streak = 0
                if pool_cursor is not None:
                    pool_cursor.persist()
                result["iterations"].append(iter_info)
                _save_result(config.result_path, result)
                _iter_appended = True
                # fall through to validate yield (no container work needed)

            elif fix_result == "no_changes":
                logger.warning("Fixer did not commit any changes")
                previous_failure = (
                    "Fixer did not commit any changes. You MUST commit: "
                    "cd /logs/artifacts && git add -A && git commit -m 'fix'"
                )
                legitimate_streak = 0
                iter_info["outcome"] = "no_changes"
                # fall through to validate yield (no container work needed)

            elif fix_result == "legitimate":
                legitimate_streak += 1
                logger.info("Fixer marked hack as legitimate (%d/%d).",
                            legitimate_streak, config.legitimate_threshold)
                iter_info["outcome"] = "legitimate"
                iter_info["fix_applied"] = False
                if pool_cursor is not None:
                    pool_cursor.persist()
                if legitimate_streak >= config.legitimate_threshold and _can_mark_robust(
                    config, privileged_hack_ran
                ):
                    logger.info("Confirmed legitimate after %d consecutive flags. Task is robust.",
                                config.legitimate_threshold)
                    result["iterations"].append(iter_info)
                    result["status"] = "robust"
                    result["hardened_dir"] = str(hardened_task_dir)
                    _save_result(config.result_path, result)
                    await _record_iter_in_journal(
                        config, iteration=iteration, outcome="legitimate",
                        fix_applied=False, hack_reward=hack_reward,
                        hack_summary=hack_summary, fixer_trial=fixer_trial,
                        notes=f"Confirmed legitimate after {config.legitimate_threshold} consecutive flags.",
                    )
                    result_out.append(result)
                    return  # done — exits before validate yield
                if legitimate_streak >= config.legitimate_threshold:
                    logger.info(
                        "Legitimate streak %d/%d reached but --hacker-privileged-enable-iteration=%d "
                        "is set and no privileged hacker has run yet — not marking robust; continuing.",
                        legitimate_streak, config.legitimate_threshold,
                        config.hacker_privileged_enable_iteration,
                    )
                reuse_hack = None
                previous_failure = None
                previous_outcome = None
                previous_fixer_trial = None
                previous_solver_trial = None
                result["iterations"].append(iter_info)
                _save_result(config.result_path, result)
                _iter_appended = True
                await _record_iter_in_journal(
                    config, iteration=iteration, outcome="legitimate",
                    fix_applied=False, hack_reward=hack_reward,
                    hack_summary=hack_summary, fixer_trial=fixer_trial,
                )
                # fall through to validate yield (no container work needed)

            else:  # "applied"
                validate_modified = False
                if not config.oracle and config.solver_privileged:
                    if prepare_solver_environment(solver_parent, config.task_id, original_dir):
                        append_to_instruction(solver_parent, config.task_id, SOLVER_HINT)
                        validate_modified = True

                solver_needs_rebuild = validate_modified or hardened_dirty
                # Same role-key strategy as the fixer above.
                solver_validate_role = (
                    f"solver_validate_pool_{previous_last_seen[:8]}_{current_pool_sha[:8]}"
                    if pool_advanced
                    else f"solver_validate_h{hack_iterations - 1}"
                )
                async with semaphore:
                    solver_reward, solver_trial = await _run_solver(
                        config,
                        solver_parent,
                        role=solver_validate_role,
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
                        async with semaphore:
                            replay_reward = await _run_targeted_replay(
                                config, hardened_task_dir, fixer_trial, hack_summary,
                                original_instruction, output, harden_image, iteration,
                                hack_iteration=hack_iterations - 1,
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
                        previous_outcome = "replay_broke_fix"
                        previous_fixer_trial = fixer_trial
                        previous_solver_trial = None
                        legitimate_streak = 0
                        iter_info["outcome"] = "replay_broke_fix"
                    else:
                        update_hardened(hardened_task_dir, fixer_trial)
                        fix_applied = True
                        hardened_dirty = True
                        previous_failure = None
                        previous_outcome = None
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
                    if is_trial_setup_failure(solver_trial):
                        logger.warning(
                            "Fix did not validate — trial failed before verifier ran "
                            "(setup error: build/container-start/agent-init). Reverting."
                        )
                        iter_info["outcome"] = "trial_setup_failed"
                        previous_outcome = "trial_setup_failed"
                    else:
                        logger.warning("Fix broke solver (reward=%.2f < %.2f). Reverting.",
                                       solver_reward, config.solver_threshold)
                        previous_outcome = "fix_failed"
                    previous_failure = read_verifier_output(solver_trial)
                    previous_fixer_trial = fixer_trial
                    previous_solver_trial = solver_trial
                    legitimate_streak = 0
        except Exception as e:
            logger.warning("Fixer produced invalid artifacts: %s", e)
            previous_failure = str(e)
            previous_outcome = "fix_failed"
            legitimate_streak = 0
            # NOTE: deliberately not touching previous_fixer_trial /
            # previous_solver_trial here. The next iter relies on the
            # `previous_failure` string (e.g. SyntaxError msg) for retry context;
            # leaving any prior trial dirs as-is is fine because they're stale
            # (point to N-2's run) but the failure message is the primary signal.
            # Trade-off: in the SyntaxError case we lose the ability to show the
            # fixer their just-broken file via /previous_attempt/ — accept that
            # for now since the error message is usually enough.

        yield  # ── iteration fence: after validate+replay (pool writes done) ─

        # End-of-iteration bookkeeping (skipped for pool-sync / legitimate early-exit
        # paths that already appended iter_info above).
        if not _iter_appended:
            if not fix_applied and not pool_advanced:
                # Reuse the hack next iter if the fixer failed, but only when we
                # actually had a hack. On pool-sync iters hack_summary is a
                # sentinel string — reusing it would hand the next fixer nonsense.
                reuse_hack = (hack_summary, hack_reward)
            iter_info["fix_applied"] = fix_applied
            iter_info.setdefault("outcome", "fixed" if fix_applied else "fix_failed")
            result["iterations"].append(iter_info)
            _save_result(config.result_path, result)
            # Journal: record the iter unless this was a pool-sync (no real
            # hack to compare against). The plan reserves pool-sync iters for
            # the pool channel; intra-task journal stays focused on real
            # attack/defense rounds.
            if not pool_advanced:
                failure_detail = (
                    extract_failure_summary(previous_failure)
                    if previous_failure and not fix_applied
                    else None
                )
                await _record_iter_in_journal(
                    config, iteration=iteration, outcome=iter_info["outcome"],
                    fix_applied=fix_applied, hack_reward=hack_reward,
                    hack_summary=hack_summary, fixer_trial=fixer_trial,
                    failure_detail=failure_detail,
                )

    # while loop exhausted without a terminal break → max_iterations
    result["status"] = "max_iterations"
    result["hardened_dir"] = str(hardened_task_dir)
    _save_result(config.result_path, result)
    result_out.append(result)


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
    semaphore = asyncio.Semaphore(1)  # single-task: no concurrency limit needed
    result_out: list = []
    try:
        async for _ in _harden_task_phases(config, pool_server, semaphore, result_out):
            pass
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Ctrl-C / external timeout: record the interruption in the on-disk
        # result (saved incrementally since loop start) before unwinding.
        _mark_interrupted(config.result_path)
        raise
    return result_out[0]
