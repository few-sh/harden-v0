"""Working copy creation, canonical hardened state, and artifact extraction.

Artifacts live at `trial_dir / "artifacts"` on the host and `/logs/artifacts/`
inside the container. Solver mode can optionally inject a privileged `/solution/`
into the image via `prepare_solver_environment`.
"""

import logging
import shutil
import subprocess
from pathlib import Path

import yaml

from .agent import read_verifier_output
from .trajectory import extract_hack_summary

logger = logging.getLogger(__name__)


def create_working_copy(source_dir: Path, dest_parent: Path) -> Path:
    """Deep-copy a task directory into dest_parent/<task_id>/.

    Returns the parent dir (for use with LocalDatasetConfig).
    """
    task_id = source_dir.name
    task_dest = dest_parent / task_id
    if task_dest.exists():
        shutil.rmtree(task_dest)
    dest_parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, task_dest)
    return dest_parent


def create_hardened_copy(original_dir: Path, output_dir: Path, resume: bool) -> Path:
    """Create the canonical hardened/ directory from the original task.

    If `resume` is True and a prior hardened/<task>/ exists, preserve it so
    previously validated fixes are kept. Otherwise always start fresh from the
    original task.
    """
    hardened_parent = output_dir / "hardened"
    task_id = original_dir.name
    existing = hardened_parent / task_id
    if resume and existing.is_dir():
        logger.info("Resuming: preserving existing hardened state at %s", existing)
        return hardened_parent
    return create_working_copy(original_dir, hardened_parent)


def apply_fixer_artifacts(task_dir: Path, fixer_trial_dir: Path) -> None:
    """Replace tests/ and environment/ in `task_dir` with fixer's committed versions."""
    artifacts = fixer_trial_dir / "artifacts"
    for subdir in ("tests", "environment"):
        src = artifacts / subdir
        dest = task_dir / subdir
        if src.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)


def update_hardened(hardened_task_dir: Path, fixer_trial_dir: Path) -> None:
    """Update the canonical hardened state with fixer artifacts.

    Called only after all post-fix gates (solver + optional replay) pass.
    """
    apply_fixer_artifacts(hardened_task_dir, fixer_trial_dir)
    logger.info("Updated hardened state from fixer artifacts")


def prepare_solver_environment(
    solver_parent: Path,
    task_id: str,
    original_dir: Path,
) -> bool:
    """Inject reference solution into solver's Docker environment (read-only).

    Solver mode + `solver_privileged` only. The solver is a Terminus-2 agent
    trying to solve the task; the reference at `/solution/` acts as a hint.

    Returns True if the solution was mounted; False if there was nothing to mount
    (no Dockerfile or no solution/ dir). The caller must not append SOLVER_HINT
    unless this returned True, otherwise the solver is told about a path that
    doesn't exist.
    """
    task_dir = solver_parent / task_id
    env_dir = task_dir / "environment"
    dockerfile = env_dir / "Dockerfile"

    if not dockerfile.exists():
        return False

    solution_src = original_dir / "solution"
    if not solution_src.is_dir():
        logger.warning("No solution/ dir in %s — skipping solver privileged setup", original_dir)
        return False

    solution_dest = env_dir / "solution"
    if solution_dest.exists():
        shutil.rmtree(solution_dest)
    shutil.copytree(solution_src, solution_dest)

    content = dockerfile.read_text()

    # Find the last USER directive so we can restore it after the root chmod
    last_user = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("USER "):
            last_user = stripped[5:].strip()

    restore = f"USER {last_user}\n" if last_user and last_user != "root" else ""
    content += (
        "\n# Added by harden: mount reference solution for solver\n"
        "USER root\n"
        "COPY solution/ /solution/\n"
        "RUN chmod -R a-w /solution/\n"
        + restore
    )
    dockerfile.write_text(content)
    return True


def prepare_hacker_environment(
    hacker_parent: Path,
    task_id: str,
    failed_trials: list[Path],
) -> None:
    """Inject previous failed hack attempt summaries into hacker's Docker environment."""
    task_dir = hacker_parent / task_id
    env_dir = task_dir / "environment"
    dockerfile = env_dir / "Dockerfile"

    if not dockerfile.exists() or not failed_trials:
        return

    hacks_dir = env_dir / "previous_hacks"
    if hacks_dir.exists():
        shutil.rmtree(hacks_dir)
    hacks_dir.mkdir()

    for i, trial_dir in enumerate(failed_trials):
        attempt_dir = hacks_dir / f"attempt_{i}"
        attempt_dir.mkdir()

        summary = extract_hack_summary(trial_dir)
        (attempt_dir / "summary.txt").write_text(summary)

        verifier_out = read_verifier_output(trial_dir)
        (attempt_dir / "verifier_output.txt").write_text(verifier_out)

    content = dockerfile.read_text()
    content += (
        "\n# Added by harden: mount previous failed hack attempts\n"
        "COPY previous_hacks/ /previous_hacks/\n"
        "RUN chmod -R a-w /previous_hacks/\n"
    )
    dockerfile.write_text(content)


def prepare_privileged_hacker_environment(
    hacker_parent: Path,
    task_id: str,
) -> bool:
    """Inject evaluation files (tests/, environment/) into hacker's Docker image at /eval_env/ (read-only).

    Returns True if mounted, False if nothing to mount.
    """
    task_dir = hacker_parent / task_id
    env_dir = task_dir / "environment"
    dockerfile = env_dir / "Dockerfile"

    if not dockerfile.exists():
        return False

    tests_src = task_dir / "tests"
    if not tests_src.is_dir():
        logger.warning("No tests/ in %s — skipping privileged hacker setup", task_dir)
        return False

    eval_env_dir = env_dir / "eval_env"
    if eval_env_dir.exists():
        shutil.rmtree(eval_env_dir)
    eval_env_dir.mkdir()

    shutil.copytree(tests_src, eval_env_dir / "tests", ignore_dangling_symlinks=True)

    env_copy = eval_env_dir / "environment"
    env_copy.mkdir()
    for item in env_dir.iterdir():
        if item.name == "eval_env":
            continue
        dest = env_copy / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore_dangling_symlinks=True)
        else:
            shutil.copy2(item, dest)

    content = dockerfile.read_text()
    content += (
        "\n# Added by harden: mount evaluation environment for privileged hacker\n"
        "COPY eval_env/ /eval_env/\n"
        "RUN chmod -R a-w /eval_env/\n"
    )
    dockerfile.write_text(content)
    return True


def _add_extra_hosts_to_compose(compose_path: Path) -> None:
    """Ensure the `main` service in docker-compose.yaml can reach the host.

    Adds `extra_hosts: - "host.docker.internal:host-gateway"` so pool URLs of
    the form `git://host.docker.internal:<port>/` resolve. Idempotent.

    Requires Linux Docker Engine >= 20.10 (the `host-gateway` special value).
    On older Docker the container's `host.docker.internal` won't resolve and
    the pool clone will fail.
    """
    if not compose_path.is_file():
        raise RuntimeError(f"docker-compose.yaml missing at {compose_path}")
    data = yaml.safe_load(compose_path.read_text()) or {}
    services = data.get("services")
    if not isinstance(services, dict) or "main" not in services:
        raise RuntimeError(
            f"docker-compose.yaml at {compose_path} has no `services.main` entry; "
            f"pool access requires a `main` service to attach extra_hosts to"
        )
    main = services["main"]
    if not isinstance(main, dict):
        raise RuntimeError(
            f"docker-compose.yaml at {compose_path}: `services.main` must be a mapping"
        )
    hosts = main.get("extra_hosts") or []
    if isinstance(hosts, dict):
        # `extra_hosts: {host.docker.internal: host-gateway}` form.
        hosts = [f"{k}:{v}" for k, v in hosts.items()]
    entry = "host.docker.internal:host-gateway"
    if entry not in hosts:
        hosts.append(entry)
    main["extra_hosts"] = hosts
    compose_path.write_text(yaml.safe_dump(data, sort_keys=False))


def prepare_fixer_environment(
    working_copy_parent: Path,
    task_id: str,
    previous_attempt: Path | None = None,
    previous_solver_trial: Path | None = None,
    pool_upstream_url: str | None = None,
) -> None:
    """Make tests/, solution/, and artifact copies available inside the Docker image.

    /logs/artifacts/ is pre-populated via staging because Harbor bind-mounts /logs/
    at runtime, overwriting build-time COPYs. An entrypoint script copies from
    staging into /logs/artifacts/ after the bind mount is in place.

    When `pool_upstream_url` is set (jumper mode), the fixer image is wired so the
    entrypoint clones the shared pool to /pool/ from `host.docker.internal:<port>`.
    The task's docker-compose.yaml gets an `extra_hosts` entry mapping
    host.docker.internal to the host-gateway so that URL resolves.
    """
    task_dir = working_copy_parent / task_id
    env_dir = task_dir / "environment"
    dockerfile = env_dir / "Dockerfile"

    if not dockerfile.exists():
        return

    additions = []

    additions.append("RUN which git || (apt-get update -qq && apt-get install -y -qq git > /dev/null)")

    tests_src = task_dir / "tests"
    if tests_src.is_dir():
        tests_dest = env_dir / "tests"
        if tests_dest.exists():
            shutil.rmtree(tests_dest)
        shutil.copytree(tests_src, tests_dest)
        additions.append("COPY tests/ /tests/")

    solution_src = task_dir / "solution"
    if solution_src.is_dir():
        solution_dest = env_dir / "solution"
        if solution_dest.exists():
            shutil.rmtree(solution_dest)
        shutil.copytree(solution_src, solution_dest)
        additions.append("COPY solution/ /solution/")
        additions.append("RUN chmod +x /solution/*.sh 2>/dev/null || true")

    staging = "/opt/harden_staging"

    environment_copy_dir = env_dir / "environment_copy"
    if environment_copy_dir.exists():
        shutil.rmtree(environment_copy_dir)
    environment_copy_dir.mkdir()
    for item in env_dir.iterdir():
        if item.name in ("tests", "solution", "environment_copy",
                         "previous_attempt", "previous_solver",
                         "previous_hacks", "eval_env",
                         "harden-entrypoint.sh"):
            continue
        dest = environment_copy_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    additions.append(f"COPY environment_copy/ {staging}/environment/")

    if tests_src.is_dir():
        additions.append(f"COPY tests/ {staging}/tests/")

    if previous_attempt is not None:
        prev_artifacts = previous_attempt / "artifacts"
        if prev_artifacts.is_dir():
            prev_dest = env_dir / "previous_attempt"
            if prev_dest.exists():
                shutil.rmtree(prev_dest)
            shutil.copytree(prev_artifacts, prev_dest,
                           ignore=shutil.ignore_patterns(".git"),
                           ignore_dangling_symlinks=True)
            additions.append("COPY previous_attempt/ /previous_attempt/")
            additions.append("RUN chmod -R a-w /previous_attempt/")

    if previous_solver_trial is not None:
        solver_dest = env_dir / "previous_solver"
        if solver_dest.exists():
            shutil.rmtree(solver_dest)
        solver_dest.mkdir()
        traj = previous_solver_trial / "agent" / "trajectory.json"
        if traj.exists():
            (solver_dest / "trajectory.json").write_bytes(traj.read_bytes())
        verifier_dir = previous_solver_trial / "verifier"
        if verifier_dir.is_dir():
            shutil.copytree(verifier_dir, solver_dest / "verifier")
        additions.append("COPY previous_solver/ /previous_solver/")
        additions.append("RUN chmod -R a-w /previous_solver/")

    pool_clone_block = ""
    if pool_upstream_url:
        additions.append(f'ENV POOL_UPSTREAM_URL="{pool_upstream_url}"')
        # Fail-fast: if the pool URL was wired in but we can't reach it after
        # 5 retries, exit the entrypoint with an error. Running the fixer
        # against a missing /pool/ would silently mis-execute the prompt's
        # pool instructions; better to surface the failure on the host.
        pool_clone_block = (
            'if [ -n "$POOL_UPSTREAM_URL" ]; then\n'
            "  rm -rf /pool\n"
            "  for i in 1 2 3 4 5; do\n"
            '    git clone "$POOL_UPSTREAM_URL" /pool && break\n'
            "    sleep 1\n"
            "  done\n"
            "  if [ ! -d /pool/.git ]; then\n"
            '    echo "[ERROR] Failed to clone pool from $POOL_UPSTREAM_URL after 5 retries" >&2\n'
            "    exit 1\n"
            "  fi\n"
            "  (cd /pool && \\\n"
            "    git config user.email fixer@harden && \\\n"
            f"    git config user.name \"harden-fixer-{task_id}\" && \\\n"
            "    git config pull.rebase true)\n"
            "  if [ ! -d /pool/environment ]; then\n"
            "    cp -a /logs/artifacts/. /pool/environment/\n"
            "  fi\n"
            "  if [ ! -d /pool/tests ]; then\n"
            "    cp -a /tests/. /pool/tests/\n"
            "  fi\n"
            "  git config --global --add safe.directory /pool\n"
            "fi\n"
        )

    entrypoint_script = env_dir / "harden-entrypoint.sh"
    entrypoint_script.write_text(
        "#!/bin/sh\n"
        "for d in environment_copy; do\n"
        '  find / -maxdepth 3 -name "$d" -type d -exec rm -rf {} + 2>/dev/null || true\n'
        "done\n"
        "mkdir -p /logs/artifacts\n"
        # `.` trick + cp -a: preserves dotfiles + permissions. `cp -r staging/*`
        # would silently drop .gitignore / .env / .cargo/ on round-trip.
        f"cp -a {staging}/. /logs/artifacts/\n"
        f"rm -rf {staging}\n"
        "pip install pytest -q 2>/dev/null || true\n"
        "git config --global user.name harden\n"
        "git config --global user.email harden@localhost\n"
        "git config --global --add safe.directory /logs/artifacts\n"
        "(cd /logs/artifacts && git init -q && git add -A && "
        "git commit -q -m initial && git tag initial)\n"
        + pool_clone_block +
        'exec "$@"\n'
    )
    entrypoint_script.chmod(0o755)
    additions.append("COPY harden-entrypoint.sh /harden-entrypoint.sh")
    additions.append('ENTRYPOINT ["/harden-entrypoint.sh"]')

    if pool_upstream_url:
        compose_path = env_dir / "docker-compose.yaml"
        if not compose_path.is_file():
            compose_path.write_text("services:\n  main:\n    build: .\n")
        _add_extra_hosts_to_compose(compose_path)

    if additions:
        content = dockerfile.read_text()
        content += "\n# Added by harden: make tests/solution available to fixer\n"
        content += "\n".join(additions) + "\n"
        dockerfile.write_text(content)


def extract_fixer_artifacts(
    fixer_trial_dir: Path,
    working_copy_parent: Path,
    task_id: str,
    kernelbench_mode: bool = False,
    legitimate_marker: bool = True,
) -> str:
    """Extract fixer's committed changes from the artifacts git repo.

    Returns:
        "no_changes" — fixer didn't commit anything
        "legitimate" — fixer marked hack as legitimate (.legitimate file)
        "applied"    — normal fix applied (validated)
    """
    artifacts = fixer_trial_dir / "artifacts"
    task_dir = working_copy_parent / task_id

    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(artifacts), "diff", "--name-only", "initial", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed in artifacts: {result.stderr.strip()}")

    changed_files = [f for f in result.stdout.strip().split("\n") if f]
    if not changed_files:
        logger.info("No committed changes in artifacts")
        return "no_changes"

    logger.info("Fixer changed: %s", changed_files)

    if legitimate_marker and (artifacts / ".legitimate").exists():
        logger.info("Fixer marked hack as legitimate solution")
        return "legitimate"

    for subdir in ("tests", "environment"):
        src = artifacts / subdir
        dest = task_dir / subdir
        if src.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)

    # Validate the central eval/test file compiles
    check_name = "eval_kernel.py" if kernelbench_mode else "test_outputs.py"
    check_file = task_dir / "tests" / check_name
    if check_file.exists():
        err = validate_python(check_file.read_text(), check_name)
        if err is not None:
            raise ValueError(
                f"Extracted {check_name} has syntax errors ({check_file}): {err}"
            )

    return "applied"


def append_to_instruction(working_copy_parent: Path, task_id: str, suffix: str) -> None:
    """Append text to instruction.md in a working copy."""
    path = working_copy_parent / task_id / "instruction.md"
    original = path.read_text()
    path.write_text(original + suffix)


def replace_instruction(working_copy_parent: Path, task_id: str, content: str) -> None:
    """Replace instruction.md entirely in a working copy."""
    path = working_copy_parent / task_id / "instruction.md"
    path.write_text(content)


def validate_python(source: str, filename: str = "<source>") -> str | None:
    """Return None if source is valid Python, otherwise a human-readable error."""
    try:
        compile(source, f"<{filename}>", "exec")
        return None
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
