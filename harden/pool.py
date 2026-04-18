"""Shared pool of defense files served to fixer containers via `git daemon`.

Jumper mode: a bare git repo at `<output_dir>/pool.git` is bootstrapped from a
hardened task's `tests/` dir. `git daemon` serves it on a TCP port. Each fixer
container clones/pushes against this remote — concurrency is handled by git's
native push semantics (non-fast-forward rejection → pull-rebase-retry, resolved
by the agent).

Host-side helpers here are only used to bootstrap, inspect HEAD, and format
commit logs for prompts. Fixers never call into this module.
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def _port_is_free(port: int) -> bool:
    # SO_REUSEADDR so TIME_WAIT leftovers from prior daemons don't mark us busy.
    # `git daemon --reuseaddr` does the same, so this check matches what the
    # daemon will actually see when it binds.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _pick_port(preferred: int, max_tries: int = 50) -> int:
    for candidate in range(preferred, preferred + max_tries):
        if _port_is_free(candidate):
            if candidate != preferred:
                logger.warning(
                    "Pool port %d busy; using %d instead", preferred, candidate
                )
            return candidate
    raise RuntimeError(
        f"No free port in {preferred}..{preferred + max_tries - 1} for git daemon"
    )


class PoolServer:
    """Lifecycle manager for a bare pool repo + `git daemon` process."""

    def __init__(
        self,
        pool_parent: Path,
        port: int,
        bootstrap_from: Path,
    ) -> None:
        self.pool_parent = Path(pool_parent)
        self.requested_port = int(port)
        self.bootstrap_from = Path(bootstrap_from)
        self.port: int | None = None
        self._daemon: subprocess.Popen | None = None
        self._bootstrap_sha: str | None = None

    @property
    def bare_path(self) -> Path:
        return self.pool_parent / "pool.git"

    @property
    def upstream_url(self) -> str:
        if self.port is None:
            raise RuntimeError("PoolServer.start() must be called first")
        # host.docker.internal is added to containers via extra_hosts:host-gateway
        return f"git://host.docker.internal:{self.port}/pool.git"

    def start(self) -> None:
        self._bootstrap_bare_repo()
        # Cache the root commit (the pool's oldest ancestor) — used as the
        # "never seen" sentinel for new tasks so they catch up instead of
        # attacking a pre-hardened pool on iter 0.
        root = _run(
            ["git", "--git-dir", str(self.bare_path), "rev-list", "--max-parents=0", "HEAD"],
        ).stdout.strip().splitlines()
        if root:
            self._bootstrap_sha = root[0]
        self.port = _pick_port(self.requested_port)
        self._launch_daemon()

    def __enter__(self) -> "PoolServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def bootstrap_sha(self) -> str:
        if self._bootstrap_sha is None:
            raise RuntimeError("PoolServer.start() must be called first")
        return self._bootstrap_sha

    def stop(self) -> None:
        if self._daemon is None:
            return
        logger.info("Stopping git daemon (pid=%s)", self._daemon.pid)
        try:
            self._daemon.terminate()
            try:
                self._daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._daemon.kill()
                self._daemon.wait(timeout=5)
        except Exception as e:
            logger.warning("Failed to stop git daemon cleanly: %s", e)
        self._daemon = None

    def _bootstrap_bare_repo(self) -> None:
        if self.bare_path.exists():
            logger.info("Pool bare repo exists at %s; skipping bootstrap", self.bare_path)
            return
        if not self.bootstrap_from.is_dir():
            raise FileNotFoundError(f"bootstrap_from not a directory: {self.bootstrap_from}")
        tests_src = self.bootstrap_from / "tests"
        if not tests_src.is_dir():
            raise FileNotFoundError(f"no tests/ in bootstrap_from: {self.bootstrap_from}")

        self.pool_parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "init", "--bare", "--initial-branch=main", str(self.bare_path)])
        # Allow `git daemon` to export this repo (default requires git-daemon-export-ok).
        (self.bare_path / "git-daemon-export-ok").touch()

        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch) / "work"
            _run(["git", "clone", str(self.bare_path), str(scratch_path)])
            # Local identity so commit succeeds without global git config.
            _run(["git", "config", "user.email", "harden@localhost"], cwd=scratch_path)
            _run(["git", "config", "user.name", "harden"], cwd=scratch_path)

            dest_tests = scratch_path / "tests"
            shutil.copytree(tests_src, dest_tests)

            _run(["git", "add", "-A"], cwd=scratch_path)
            _run(
                ["git", "commit", "-m", f"[bootstrap] from {self.bootstrap_from}"],
                cwd=scratch_path,
            )
            _run(["git", "push", "origin", "main"], cwd=scratch_path)
        logger.info("Pool bootstrapped at %s", self.bare_path)

    def _launch_daemon(self) -> None:
        # SECURITY NOTE: listens on 0.0.0.0 with receive-pack enabled and no
        # authentication. This is necessary for container reachability via the
        # Docker bridge gateway (containers cannot reach 127.0.0.1 on the host).
        # Anyone with network access to the port can push arbitrary commits into
        # the pool — which will then be executed by fixer containers. Run only
        # on hosts where the port is firewalled off from untrusted networks.
        cmd = [
            "git", "daemon",
            "--reuseaddr",
            "--listen=0.0.0.0",
            f"--port={self.port}",
            f"--base-path={self.pool_parent}",
            "--export-all",
            "--enable=receive-pack",
            "--informative-errors",
            "--verbose",
        ]
        logger.info("Launching git daemon: %s", " ".join(cmd))
        self._daemon = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait briefly for the socket to come up.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.connect(("127.0.0.1", self.port))
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            self.stop()
            raise RuntimeError(f"git daemon did not come up on port {self.port}")

        # Sanity: ensure ls-remote works.
        proc = _run(
            ["git", "ls-remote", f"git://127.0.0.1:{self.port}/pool.git"],
            check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "git ls-remote sanity check failed: %s", proc.stderr.strip()
            )


def get_pool_head(bare_path: Path) -> str:
    proc = _run(
        ["git", "--git-dir", str(bare_path), "rev-parse", "refs/heads/main"],
    )
    return proc.stdout.strip()


def get_pool_log_since(bare_path: Path, since_sha: str, limit: int = 50) -> str:
    """`git log since..HEAD` in a compact-ish, prompt-ready format."""
    proc = _run(
        [
            "git", "--git-dir", str(bare_path),
            "log", f"{since_sha}..HEAD",
            "--reverse",
            f"--max-count={limit}",
            "--pretty=format:* %h %s%n%w(0,4,4)%b",
        ],
        check=False,
    )
    if proc.returncode != 0:
        # e.g., since_sha isn't reachable — return raw recent log as fallback.
        logger.warning(
            "git log %s..HEAD failed (%s); falling back to recent log",
            since_sha, proc.stderr.strip(),
        )
        proc = _run(
            [
                "git", "--git-dir", str(bare_path),
                "log", "--reverse", f"--max-count={limit}",
                "--pretty=format:* %h %s%n%w(0,4,4)%b",
            ],
            check=False,
        )
    return proc.stdout.strip()


def get_latest_own_commit(bare_path: Path, task_id: str) -> str | None:
    """Return the SHA of the most recent pool commit authored by this task's fixer.

    The fixer's git author is set to `harden-fixer-<task_id>` in the container
    entrypoint (see workspace.prepare_fixer_environment). Rebased commits keep
    their author, so this still works after pull --rebase.

    Anchors the pattern at the end-of-name boundary (" <") so task_id "task-1"
    does not match commits from "task-10".
    """
    author_pattern = f"harden-fixer-{re.escape(task_id)} <"
    proc = _run(
        [
            "git", "--git-dir", str(bare_path),
            "log", f"--author={author_pattern}", "-1", "--format=%H",
        ],
        check=False,
    )
    sha = proc.stdout.strip()
    return sha or None


def read_last_seen_sha(task_output_dir: Path) -> str | None:
    p = task_output_dir / "pool_sha.txt"
    if not p.is_file():
        return None
    sha = p.read_text().strip()
    return sha or None


def write_last_seen_sha(task_output_dir: Path, sha: str) -> None:
    task_output_dir.mkdir(parents=True, exist_ok=True)
    (task_output_dir / "pool_sha.txt").write_text(sha + "\n")
