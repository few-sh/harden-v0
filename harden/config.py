"""Configuration for the hardening loop.

Two orthogonal mode flags:

* `oracle: bool` — selects the pre-check/validation mechanism.
    True  → run a deterministic reference copy (`reference.py → solution.py`).
    False → run a Terminus-2 solver agent.

* `kernelbench_mode: bool` — selects the prompt/template bundle and the
    central eval-file check.
    True  → KernelBench framing: speedup metric, `eval_kernel.py`, hacker
            goal = "minimize runtime by any means", fixer template references
            the KB eval pipeline.
    False → Generic framing: pass/fail reward, `test_outputs.py`, hacker goal
            = "exploit the verification system", generic fixer template.

The two commonly travel together (KernelBench tasks set both True; generic
non-KernelBench tasks set both False) but are decoupled because nothing forces the
coupling: a task with a deterministic reference could still use generic
prompts, or vice versa.
"""

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "gemini/gemini-3.1-pro-preview"

# Mechanical fields that don't affect agent behavior — excluded from the
# job-cache fingerprint. Anything else in HardenConfig is treated as
# behavioral and contributes to the fingerprint. Bias toward NOT adding
# new fields here unless you're certain they're mechanical: forgetting to
# exclude something causes extra cache misses (visible, recoverable),
# while forgetting to include something behavioral would silently return
# stale cached results.
_FINGERPRINT_EXCLUDE = frozenset({
    "task_id",                  # per-task, not config identity
    "output_dir", "tasks_dir",  # paths
    "resume",                   # run-control flag
    "retry_failed_prechecks",   # affects precheck_cache, not job cache
    "image_name", "force_build",  # build mechanics
    "pool_port", "pool_bootstrap_dir",  # pool plumbing (pool_enabled IS included)
    "harbor_config",            # hashed by file content separately
    "fixer_prompt_file",        # hashed by file content separately
})


@dataclass
class HardenConfig:
    task_id: str = ""
    tasks_dir: Path = Path("tasks")
    output_dir: Path = Path("./harden_output")

    # Pre-check: True => deterministic oracle, False => agent solver.
    oracle: bool = False
    # Prompt/template bundle: True => KernelBench-specific, False => generic.
    kernelbench_mode: bool = False

    hacker_model: str = DEFAULT_MODEL
    fixer_model: str = DEFAULT_MODEL
    # Model used to LLM-summarize failed hack trajectories after each attempt.
    # Defaults to fixer_model when None. Set to "" to disable LLM summarization.
    summary_model: str | None = None

    # Thresholds (speedup in KB mode, reward otherwise; KB overrides to 10/0.5)
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

    # Agent-solver-only knobs (ignored when cfg.oracle=True, i.e. deterministic pre-check)
    solver_model: str = DEFAULT_MODEL
    solver_max_turns: int | None = None
    solver_precheck_retries: int = 1
    solver_timeout_multiplier: float = 2.0
    solver_privileged: bool = False

    # Apply regardless of pre-check dispatch
    hacker_timeout_multiplier: float = 2.0
    fixer_timeout_multiplier: float = 10.0
    hacker_privileged: bool = False
    # When hacker_privileged is enabled, disable it starting at this iteration index.
    hacker_privileged_disable_iteration: int = 5
    # Opposite of `hacker_privileged_disable_iteration`: when set, the hacker
    # starts non-privileged and `hacker_privileged` only kicks in at iteration
    # indices >= this value. Mutually exclusive with disable_iteration at the
    # CLI. When set, the loop refuses to mark the task `robust` until at least
    # one iteration has actually run a privileged hacker — a non-privileged
    # hacker failure can no longer end the run.
    hacker_privileged_enable_iteration: int | None = None

    # Targeted replay (post-solver gate): re-run hacker constrained to reproduce the
    # specific prior exploit on the patched task. If the exploit re-lands, the fix
    # is rejected (same control flow as a solver rejection).
    replay_enabled: bool = False
    replay_retries: int = 1

    # Harbor knobs (solver mode: Dockerfile mutates across iterations; both modes can override image)
    harbor_config: Path | None = None
    force_build: bool = False
    image_name: str | None = None

    # Jumper / pooled mode: share a defense repo across tasks via a host-side git daemon.
    pool_enabled: bool = False
    pool_bootstrap_dir: Path | None = None
    pool_port: int = 9418
    # After this many consecutive pool-sync skips, force the hacker to run regardless
    # of pool state. Pool-sync iterations never count toward max_iterations,
    # (so this also bounds the overhead between real hack iterations.)
    pool_max_consecutive_syncs: int = 1
    # When True, fresh tasks in batch mode start with cursor=None (the PoolCursor
    # default), so iter 0 reports a pool advance and the fixer is asked to port
    # the existing pool history into local /logs/artifacts/. Default False —
    # the bootstrap is typically the source task's tests/ tree, including a
    # task-specific reference.py that would corrupt sibling tasks' correctness
    # checks if integrated. Only flip this on if pool_bootstrap_dir contains a
    # task-agnostic defense scaffold.
    pool_integrate_bootstrap: bool = False

    # Cross-iteration journal — per-task hacker+fixer memory persisted at
    # <task_output_dir>/journal.md + <task_output_dir>/journal/. When True,
    # the loop summarizes every iter into the journal and injects it into
    # the hacker, fixer, and replay prompts. Use journal_enabled=False for
    # ablation runs that match prior no-journal behavior.
    journal_enabled: bool = True
    # Maximum number of recent iters' compact entries inlined into journal.md.
    # Older iters remain in /journal/iter_<N>.md for drill-down.
    journal_compact_max_iters: int = 10

    # Optional markdown file whose contents are appended to every fixer
    # prompt as "Additional guidance". Use for run-wide instructions
    # (e.g., "prefer test-side fixes over Dockerfile changes", "do not
    # modify file X"). File contents are hashed into the fingerprint so
    # cache invalidates when the file changes.
    fixer_prompt_file: Path | None = None
    # Inject `fixer_prompt_file` only AFTER this iteration index — i.e.
    # iter values strictly greater than this threshold see the guidance.
    # Default -1 = inject from iter 0 onward (no gating). Set e.g. 2 to
    # let the fixer make its own attempts in iters 0/1/2 before any
    # custom directive kicks in.
    fixer_prompt_after_iter: int = -1

    # If True, preserve an existing output/hardened/<task>/ from a prior run.
    resume: bool = False

    # Re-run cached failed prechecks live instead of reusing their result.
    retry_failed_prechecks: bool = False

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

    def fingerprint(self) -> str:
        raw = dataclasses.asdict(self)
        payload = {k: v for k, v in raw.items() if k not in _FINGERPRINT_EXCLUDE}
        if self.harbor_config is not None:
            payload["_harbor_config_sha"] = hashlib.sha256(
                self.harbor_config.read_bytes()
            ).hexdigest()
        if self.fixer_prompt_file is not None:
            payload["_fixer_prompt_file_sha"] = hashlib.sha256(
                self.fixer_prompt_file.read_bytes()
            ).hexdigest()
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]


@dataclass
class BatchHardenConfig(HardenConfig):
    task_ids: list[str] = field(default_factory=list)
    max_concurrent_containers: int = 4

    def make_task_config(self, task_id: str) -> HardenConfig:
        base_fields = {f.name for f in dataclasses.fields(HardenConfig)}
        kwargs = {k: v for k, v in dataclasses.asdict(self).items() if k in base_fields}
        kwargs["task_id"] = task_id
        return HardenConfig(**kwargs)
