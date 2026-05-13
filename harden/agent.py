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
import subprocess
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

from .durable import durable

logger = logging.getLogger(__name__)

# Defaults that match what works well with gemini-3.x thinking models
DEFAULT_TEMPERATURE = 1.0
DEFAULT_REASONING_EFFORT = "high"

# hook_scripts/hooks.sh lives at the repo root (parent of the `harden` package).
_HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "hook_scripts" / "hooks.sh"


def _fire_job_finished_hook(job_path: Path) -> None:
    """Launch hook_scripts/hooks.sh in the background with the absolute job path.

    Fire-and-forget via subprocess.Popen + start_new_session=True. The hook
    redirects its own stdout/stderr to ``<job_dir>/hooks.log`` (see
    hook_scripts/hooks.sh) so we can DEVNULL the parent fds. The new session
    detaches the child so the OS reaps it; we don't need asyncio integration
    since nothing awaits the result. Failures are logged and swallowed so a
    broken hook never breaks the harden run.
    """
    if not _HOOK_SCRIPT.is_file():
        return
    try:
        subprocess.Popen(
            [str(_HOOK_SCRIPT), str(job_path.resolve())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as e:
        logger.warning("Failed to launch job-finished hook %s: %s", _HOOK_SCRIPT, e)


def _extract_reward(trial_result: TrialResult, kernelbench_mode: bool = False) -> float:
    """Read the reward from a trial's verifier output.

    With `kernelbench_mode`, also sanity-check the KB-specific sidecar fields.
    Fixers can ship a patched `eval_kernel.py` that fails to measure the
    baseline (sentinel `ref_runtime_us = -1.0`) and falls back to
    `speedup = 1.0`, which silently passes solver-validate and hides real
    hacks. Treat any sentinel/non-positive timing as a broken eval and
    return 0.0 so the loop rejects the fix.
    """
    if not trial_result.verifier_result or not trial_result.verifier_result.rewards:
        return 0.0
    rewards = trial_result.verifier_result.rewards
    if kernelbench_mode:
        for key in ("runtime_us", "ref_runtime_us"):
            value = rewards.get(key)
            if value is not None and value <= 0:
                logger.warning(
                    "KB sentinel detected in verifier output: %s=%s — treating as broken eval (reward → 0).",
                    key, value,
                )
                return 0.0
    return rewards.get("reward", 0.0)


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
    kernelbench_mode: bool = False,
) -> tuple[float, Path]:
    """Run a Terminus-2 hacker agent. Collects /solution for exploit inspection."""
    out = await _run_agent(
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
        kernelbench_mode=kernelbench_mode,
    )
    return out["reward"], Path(out["trial_dir"])


async def run_oracle_solver(
    task_parent_dir: Path,
    jobs_dir: Path,
    role: str,
    timeout_multiplier: float = 1.0,
    harbor_config: Path | None = None,
    force_build: bool = False,
    image_name: str | None = None,
    kernelbench_mode: bool = False,
) -> tuple[float, Path]:
    """Run the oracle solver (copies reference.py → solution.py via solve.sh)."""
    out = await _run_agent(
        task_parent_dir=task_parent_dir,
        agent_name=AgentName.ORACLE,
        model_name=None,
        jobs_dir=jobs_dir,
        role=role,
        timeout_multiplier=timeout_multiplier,
        harbor_config=harbor_config,
        force_build=force_build,
        image_name=image_name,
        kernelbench_mode=kernelbench_mode,
    )
    return out["reward"], Path(out["trial_dir"])


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
    kernelbench_mode: bool = False,
) -> tuple[float, Path]:
    """Run a Terminus-2 solver agent (used as pre-check when cfg.oracle=False).

    Verifier enabled — we need the reward signal. Solver may be privileged via
    the workspace layer (prepare_solver_environment injects /solution/ into the image).
    """
    out = await _run_agent(
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
        kernelbench_mode=kernelbench_mode,
    )
    return out["reward"], Path(out["trial_dir"])


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
    out = await _run_agent(
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
    return out["reward"], Path(out["trial_dir"])


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


@durable(path="{jobs_dir.parent.parent}/.job_cache/{namespace}/{jobs_dir.parent.name}__{role}.json")
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
    kernelbench_mode: bool = False,
) -> dict:
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
    reward = _extract_reward(trial_result, kernelbench_mode=kernelbench_mode)
    logger.info("[%s] Completed: reward=%.2f, trial_dir=%s", role, reward, trial_dir)
    _fire_job_finished_hook(trial_dir)
    return {"reward": reward, "trial_dir": str(trial_dir)}


def _has_verifier_output(trial_dir: Path) -> bool:
    """True if the trial produced any verifier stdout/stderr."""
    verifier_dir = trial_dir / "verifier"
    if not verifier_dir.is_dir():
        return False
    for name in ("test-stdout.txt", "test-stderr.txt"):
        path = verifier_dir / name
        if path.exists() and path.stat().st_size > 0:
            return True
    return False


def is_build_failure(trial_dir: Path) -> bool:
    """True if the trial failed before producing any verifier output.

    The typical case is a Docker compose build error (Dockerfile change broke
    the image build); also catches Harbor harness errors (env setup, tmux
    crash) where the verifier never ran. Distinguishes those from genuine
    test regressions where the verifier ran but the solver failed.
    """
    return not _has_verifier_output(trial_dir)


def read_verifier_output(trial_dir: Path) -> str:
    """Read verifier stdout/stderr for failure feedback.

    Falls back to ``exception.txt`` when no verifier output exists — that's
    where Harbor records Docker build failures and harness errors, which
    would otherwise leave the next fixer with no actionable signal.
    """
    parts: list[str] = []
    verifier_dir = trial_dir / "verifier"
    if verifier_dir.is_dir():
        for name in ("test-stdout.txt", "test-stderr.txt"):
            path = verifier_dir / name
            if path.exists():
                content = path.read_text().strip()
                if content:
                    parts.append(f"=== {name} ===\n{content}")
    if parts:
        return "\n\n".join(parts)

    # No verifier output — the trial failed before tests ran. Surface
    # exception.txt so the fixer sees the actual error (typically docker build).
    exception_path = trial_dir / "exception.txt"
    if exception_path.exists():
        content = exception_path.read_text().strip()
        if content:
            return (
                "=== exception.txt (trial failed before verifier ran) ===\n"
                + content
            )

    if not verifier_dir.is_dir():
        logger.warning("No verifier/ dir in %s — Harbor may have failed to run the verifier.",
                       trial_dir)
        return "(verifier directory missing and no exception.txt)"
    return "(verifier produced no output and no exception.txt)"


def extract_failure_summary(failure_text: str, max_chars: int = 600) -> str:
    """Extract a short, actionable summary from a long failure_text blob.

    Used for journal compact entries. Tiered scan: the most specific error
    marker present in the text wins, regardless of where it appears, so that
    e.g. a Docker build error report surfaces the actual failing step rather
    than the generic Python ``RuntimeError`` wrapper that came first.
    """
    if not failure_text or not failure_text.strip():
        return ""

    lines = [ln.rstrip() for ln in failure_text.splitlines() if ln.strip()]
    if not lines:
        return ""

    tiers = (
        ("failed to solve:",),
        ("ERROR: process", "ERROR ["),
        ("ERROR:",),
        ("AssertionError:", "RuntimeError:", "ImportError:",
         "SyntaxError:", "ModuleNotFoundError:", "TypeError:", "ValueError:"),
        ("FAILED ", "Error: "),
    )
    for tier in tiers:
        for line in lines:
            for marker in tier:
                if marker in line:
                    return line.strip()[:max_chars]

    return lines[-1][:max_chars]
