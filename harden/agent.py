"""Run agents via Harbor's Job API.

Role wrappers share a single `_run_agent()` core. The four roles:
  * run_hacker         — TERMINUS_2, collects /solution, verifier enabled.
  * run_oracle_solver  — ORACLE (deterministic reference copy), no model.
  * run_solver_agent   — TERMINUS_2, verifier enabled (solver-mode pre-check & validation).
  * run_fixer          — TERMINUS_2, verifier disabled (we read committed artifacts).

Slime's Harbor knobs (`harbor_config` yaml, `timeout_multiplier`, `force_build`, `image_name`)
are essential for solver mode — the fixer mutates the task Dockerfile across iterations, and
without rebuild control + distinct image names, cache pollution silently reuses the pre-fix image.
"""

import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import yaml
from harbor.job import Job
from harbor.models.agent.name import AgentName
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import (
    JobConfig,
    LocalDatasetConfig,
    OrchestratorConfig,
)
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, VerifierConfig
from harbor.models.trial.result import TrialResult

logger = logging.getLogger(__name__)

# Defaults that match what works well with gemini-3.x thinking models
DEFAULT_TEMPERATURE = 1.0
DEFAULT_REASONING_EFFORT = "high"


def _extract_reward(trial_result: TrialResult) -> float:
    if trial_result.verifier_result and trial_result.verifier_result.rewards:
        return trial_result.verifier_result.rewards.get("reward", 0.0)
    return 0.0


async def run_hacker(
    task_parent_dir: Path,
    model_name: str,
    jobs_dir: Path,
    role: str,
    max_turns: int | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    timeout_multiplier: float = 1.0,
    harbor_config: Path | None = None,
    force_build: bool = False,
    image_name: str | None = None,
) -> tuple[float, Path]:
    """Run a Terminus-2 hacker agent. Collects /solution for exploit inspection."""
    return await _run_agent(
        task_parent_dir=task_parent_dir,
        agent_name=AgentName.TERMINUS_2,
        model_name=model_name,
        jobs_dir=jobs_dir,
        role=role,
        max_turns=max_turns,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        disable_verifier=False,
        artifacts=["/solution"],
        timeout_multiplier=timeout_multiplier,
        harbor_config=harbor_config,
        force_build=force_build,
        image_name=image_name,
    )


async def run_oracle_solver(
    task_parent_dir: Path,
    jobs_dir: Path,
    role: str,
    timeout_multiplier: float = 1.0,
    harbor_config: Path | None = None,
    force_build: bool = False,
    image_name: str | None = None,
) -> tuple[float, Path]:
    """Run the oracle solver (copies reference.py → solution.py via solve.sh)."""
    return await _run_agent(
        task_parent_dir=task_parent_dir,
        agent_name=AgentName.ORACLE,
        model_name=None,
        jobs_dir=jobs_dir,
        role=role,
        timeout_multiplier=timeout_multiplier,
        harbor_config=harbor_config,
        force_build=force_build,
        image_name=image_name,
    )


async def run_solver_agent(
    task_parent_dir: Path,
    model_name: str,
    jobs_dir: Path,
    role: str,
    max_turns: int | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    timeout_multiplier: float = 1.0,
    harbor_config: Path | None = None,
    force_build: bool = False,
    image_name: str | None = None,
) -> tuple[float, Path]:
    """Run a Terminus-2 solver agent (used as pre-check in solver mode, oracle=False).

    Verifier enabled — we need the reward signal. Solver may be privileged via
    the workspace layer (prepare_solver_environment injects /solution/ into the image).
    """
    return await _run_agent(
        task_parent_dir=task_parent_dir,
        agent_name=AgentName.TERMINUS_2,
        model_name=model_name,
        jobs_dir=jobs_dir,
        role=role,
        max_turns=max_turns,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        disable_verifier=False,
        artifacts=[],
        timeout_multiplier=timeout_multiplier,
        harbor_config=harbor_config,
        force_build=force_build,
        image_name=image_name,
    )


async def run_fixer(
    task_parent_dir: Path,
    model_name: str,
    jobs_dir: Path,
    role: str,
    max_turns: int | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    timeout_multiplier: float = 1.0,
    harbor_config: Path | None = None,
    force_build: bool = False,
    image_name: str | None = None,
) -> tuple[float, Path]:
    """Run the fixer agent (verifier disabled — we only care about committed files)."""
    return await _run_agent(
        task_parent_dir=task_parent_dir,
        agent_name=AgentName.TERMINUS_2,
        model_name=model_name,
        jobs_dir=jobs_dir,
        role=role,
        max_turns=max_turns,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        disable_verifier=True,
        artifacts=[],
        timeout_multiplier=timeout_multiplier,
        harbor_config=harbor_config,
        force_build=force_build,
        image_name=image_name,
    )


def _load_base_config(harbor_config: Path | None) -> JobConfig:
    if harbor_config is None:
        return JobConfig(
            environment=EnvironmentConfig(type=EnvironmentType.DOCKER, delete=True),
            orchestrator=OrchestratorConfig(n_concurrent_trials=1),
        )
    if harbor_config.suffix in (".yaml", ".yml"):
        return JobConfig.model_validate(yaml.safe_load(harbor_config.read_text()))
    if harbor_config.suffix == ".json":
        return JobConfig.model_validate_json(harbor_config.read_text())
    raise ValueError(f"Unsupported harbor config format: {harbor_config.suffix}")


async def _run_agent(
    task_parent_dir: Path,
    agent_name: AgentName,
    model_name: str | None,
    jobs_dir: Path,
    role: str,
    max_turns: int | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    disable_verifier: bool = False,
    artifacts: list[str] | None = None,
    timeout_multiplier: float = 1.0,
    harbor_config: Path | None = None,
    force_build: bool = False,
    image_name: str | None = None,
) -> tuple[float, Path]:
    job_name = f"{role}__{datetime.now().strftime('%Y%m%d_%H%M%S')}__{uuid4().hex[:8]}"

    base_config = _load_base_config(harbor_config)

    agent_kwargs: dict = {}
    if base_config.agents:
        agent_kwargs.update(base_config.agents[0].kwargs or {})
    if agent_name == AgentName.TERMINUS_2:
        agent_kwargs["temperature"] = temperature if temperature is not None else DEFAULT_TEMPERATURE
        agent_kwargs["reasoning_effort"] = reasoning_effort if reasoning_effort is not None else DEFAULT_REASONING_EFFORT
        if max_turns is not None:
            agent_kwargs["max_turns"] = max_turns
        if max_tokens is not None:
            agent_kwargs["max_tokens"] = max_tokens

    agent_config = AgentConfig(name=agent_name, kwargs=agent_kwargs)
    if model_name:
        agent_config.model_name = model_name

    base_config.job_name = job_name
    base_config.jobs_dir = jobs_dir
    base_config.agents = [agent_config]
    base_config.datasets = [LocalDatasetConfig(path=task_parent_dir)]
    base_config.verifier = VerifierConfig(disable=disable_verifier)
    base_config.artifacts = artifacts or []
    base_config.timeout_multiplier = timeout_multiplier

    if force_build:
        base_config.environment.force_build = True
    if image_name is not None:
        base_config.environment.kwargs["image_name"] = image_name

    job = Job(config=base_config)

    logger.info("[%s] Starting %s on %s (model=%s)", role, agent_name.value, task_parent_dir, model_name)

    await job.run()

    trial_dirs = sorted(
        (d for d in job.job_dir.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not trial_dirs:
        raise RuntimeError(f"[{role}] No trial directories produced in {job.job_dir}")

    trial_dir = trial_dirs[0]
    result_path = trial_dir / "result.json"
    if not result_path.exists():
        raise RuntimeError(f"[{role}] No result.json in {trial_dir}")

    trial_result = TrialResult.model_validate_json(result_path.read_text())
    reward = _extract_reward(trial_result)
    logger.info("[%s] Completed: reward=%.2f, trial_dir=%s", role, reward, trial_dir)
    return reward, trial_dir


def read_verifier_output(trial_dir: Path) -> str:
    """Read verifier stdout/stderr for failure feedback."""
    verifier_dir = trial_dir / "verifier"
    if not verifier_dir.is_dir():
        logger.warning("No verifier/ dir in %s — Harbor may have failed to run the verifier.",
                       trial_dir)
        return "(verifier directory missing)"
    parts: list[str] = []
    for name in ("test-stdout.txt", "test-stderr.txt"):
        path = verifier_dir / name
        if path.exists():
            content = path.read_text().strip()
            if content:
                parts.append(f"=== {name} ===\n{content}")
    return "\n\n".join(parts) if parts else "(verifier produced no output)"
