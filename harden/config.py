"""Configuration for the hardening loop.

Supports two pre-check modes via `oracle: bool`:
  * oracle=True  — deterministic reference solution (e.g. KernelBench: reference.py → solution.py),
                   thresholds interpreted as speedup.
  * oracle=False — solver agent (e.g. slime generic tasks: terminus-2 solves the task),
                   thresholds interpreted as reward (pass/fail at 1.0).
"""

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TASKS_DIR = Path("/lambda/nfs/reward-hacking/harbor/datasets/kernelbench")
DEFAULT_MODEL = "gemini/gemini-3.1-pro-preview"


@dataclass
class HardenConfig:
    task_id: str = ""
    tasks_dir: Path = DEFAULT_TASKS_DIR
    output_dir: Path = Path("./harden_output")

    # Mode: True => oracle pre-check (deterministic), False => solver agent pre-check
    oracle: bool = False

    hacker_model: str = DEFAULT_MODEL
    fixer_model: str = DEFAULT_MODEL

    # Thresholds (speedup in oracle mode, reward in solver mode; KB overrides to 10/0.5)
    hack_threshold: float = 1.0
    solver_threshold: float = 1.0

    max_iterations: int = 10
    hacker_retries: int = 3
    legitimate_threshold: int = 3
    # Allow the fixer to mark a hack as a legitimate solution via .legitimate sentinel.
    # Disable when the task metric is purely binary and legitimacy is unambiguous.
    legitimate_marker: bool = True
    hacker_max_turns: int | None = None   # None => use task.toml timeout
    fixer_max_turns: int | None = None
    temperature: float | None = None      # None => default 1.0
    max_tokens: int | None = None
    reasoning_effort: str | None = None   # None => default "high"
    hacker_feedback: bool = False

    # Solver-mode only (used when oracle=False)
    solver_model: str = DEFAULT_MODEL
    solver_max_turns: int | None = None
    solver_precheck_retries: int = 1
    solver_timeout_multiplier: float = 2.0
    hacker_timeout_multiplier: float = 2.0
    fixer_timeout_multiplier: float = 10.0
    solver_privileged: bool = False

    # Harbor knobs (solver mode: Dockerfile mutates across iterations; both modes can override image)
    harbor_config: Path | None = None
    force_build: bool = False
    image_name: str | None = None

    @property
    def task_dir(self) -> Path:
        return self.tasks_dir / self.task_id

    @property
    def task_output_dir(self) -> Path:
        return self.output_dir / self.task_id

    @property
    def jobs_dir(self) -> Path:
        return self.task_output_dir / "jobs"

    @property
    def result_path(self) -> Path:
        return self.task_output_dir / "result.json"


@dataclass
class BatchHardenConfig(HardenConfig):
    task_ids: list[str] = field(default_factory=list)
    max_concurrent_containers: int = 4
    resume: bool = False

    def make_task_config(self, task_id: str) -> HardenConfig:
        base_fields = {f.name for f in dataclasses.fields(HardenConfig)}
        kwargs = {k: v for k, v in dataclasses.asdict(self).items() if k in base_fields}
        kwargs["task_id"] = task_id
        return HardenConfig(**kwargs)
