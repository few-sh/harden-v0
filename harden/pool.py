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

import contextlib
import logging
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
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


def _port_is_free(port: int, bind_ip: str) -> bool:
    # SO_REUSEADDR so TIME_WAIT leftovers from prior daemons don't mark us busy.
    # `git daemon --reuseaddr` does the same, so this check matches what the
    # daemon will actually see when it binds.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind_ip, port))
        except OSError:
            return False
    return True


def _pick_port(preferred: int, bind_ip: str, max_tries: int = 50) -> int:
    for candidate in range(preferred, preferred + max_tries):
        if _port_is_free(candidate, bind_ip):
            if candidate != preferred:
                logger.warning(
                    "Pool port %d busy; using %d instead", preferred, candidate
                )
            return candidate
    raise RuntimeError(
        f"No free port in {preferred}..{preferred + max_tries - 1} for git daemon"
    )


def _detect_docker_bridge_ip() -> str | None:
    """Return the docker0 bridge IP, or None if unavailable.

    Bind target for the git daemon: this IP is what `host.docker.internal`
    resolves to inside containers that set `extra_hosts: host-gateway` —
    verified empirically on both the default bridge and user-defined compose
    bridges on Linux Docker. Binding here (instead of 0.0.0.0) closes the
    LAN-facing attack surface of the receive-pack-enabled daemon.
    """
    try:
        proc = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "docker0"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", proc.stdout)
    return match.group(1) if match else None


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
        self.bind_ip: str | None = None
        self._daemon: subprocess.Popen | None = None

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
        bridge_ip = _detect_docker_bridge_ip()
        if bridge_ip:
            self.bind_ip = bridge_ip
            logger.info("Binding git daemon to docker bridge %s", self.bind_ip)
        else:
            self.bind_ip = "0.0.0.0"
            logger.warning(
                "Could not detect docker0 bridge IP; falling back to 0.0.0.0 "
                "(LAN-exposed). Firewall the port off from untrusted networks."
            )
        self.port = _pick_port(self.requested_port, self.bind_ip)
        self._launch_daemon()

    def __enter__(self) -> "PoolServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

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
        # SECURITY NOTE: receive-pack is enabled with no authentication — anyone
        # who can reach the port can push arbitrary commits that fixer containers
        # later execute. We bind to the docker bridge IP by default (closes LAN
        # exposure while preserving container reachability via host-gateway).
        # Falls back to 0.0.0.0 if docker0 isn't detectable.
        cmd = [
            "git", "daemon",
            "--reuseaddr",
            f"--listen={self.bind_ip}",
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
        # Connect check uses the actual bind IP (not 127.0.0.1): when bound to
        # docker0 specifically, loopback won't reach the listener.
        probe_ip = self.bind_ip if self.bind_ip != "0.0.0.0" else "127.0.0.1"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.connect((probe_ip, self.port))
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            self.stop()
            raise RuntimeError(f"git daemon did not come up on port {self.port}")

        # Sanity: ensure ls-remote works.
        proc = _run(
            ["git", "ls-remote", f"git://{probe_ip}:{self.port}/pool.git"],
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


def get_pool_log_since(bare_path: Path, since_sha: str | None, limit: int = 50) -> str:
    """`git log since..HEAD` in a compact-ish, prompt-ready format.

    When `since_sha` is None/empty, returns the whole history up to HEAD —
    used for fresh tasks that should catch up to the bootstrap defense
    before attacking.
    """
    rev_range = f"{since_sha}..HEAD" if since_sha else "HEAD"
    proc = _run(
        [
            "git", "--git-dir", str(bare_path),
            "log", rev_range,
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


@contextlib.contextmanager
def pool_context(config) -> Iterator["PoolServer | None"]:
    """Centralized pool-server setup for callers (batch.py / __main__.py).

    Yields a running PoolServer when `config.pool_enabled`, else None. Raises
    ValueError if pool_enabled but pool_bootstrap_dir is unset.
    """
    if not getattr(config, "pool_enabled", False):
        yield None
        return
    if getattr(config, "pool_bootstrap_dir", None) is None:
        raise ValueError("pool_enabled requires pool_bootstrap_dir")
    with PoolServer(
        pool_parent=Path(config.output_dir),
        port=config.pool_port,
        bootstrap_from=Path(config.pool_bootstrap_dir),
    ) as srv:
        logger.info("Pool server up at %s", srv.upstream_url)
        yield srv


def is_ancestor(bare_path: Path, ancestor: str, descendant: str) -> bool:
    """True iff `ancestor` is reachable from `descendant` (i.e., descendant is newer)."""
    proc = _run(
        [
            "git", "--git-dir", str(bare_path),
            "merge-base", "--is-ancestor", ancestor, descendant,
        ],
        check=False,
    )
    return proc.returncode == 0


def range_only_authored_by(
    bare_path: Path, since: str, until: str, expected_author: str,
) -> bool:
    """True iff every commit in `since..until` has `expected_author` as its author name.

    Returns False on empty range or git error — caller should treat as a real
    peer advance in either case (conservative: better to run a pool-sync iter
    we didn't strictly need than to silently swallow a peer commit).
    """
    proc = _run(
        [
            "git", "--git-dir", str(bare_path),
            "log", f"{since}..{until}", "--format=%an",
        ],
        check=False,
    )
    if proc.returncode != 0:
        return False
    authors = [a for a in proc.stdout.splitlines() if a]
    if not authors:
        return False
    return all(a == expected_author for a in authors)


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


class PoolCursor:
    """State machine for one task's position in the pool.

    Owns the advance/persist invariant that was scattered across harden_task:
      - in-memory SHA: the most recent pool commit this task has been shown.
      - on-disk SHA  : what `pool_sha.txt` holds; updated only at commit points
                       (pool_sync_noop, legitimate, or validated fix_applied).
                       An iter that crashes mid-way does not silently mark pool
                       commits as seen.
    """

    def __init__(
        self,
        pool_server: "PoolServer",
        task_output_dir: Path,
        task_id: str,
    ) -> None:
        self._pool_server = pool_server
        self._task_output_dir = task_output_dir
        self._task_id = task_id
        # Reads `pool_sha.txt` if present, else None.
        #
        # In single-task `harden_task` runs with --pool-enabled, no pre-seeding
        # happens upstream, so the cursor starts at None. Iter 0 then reports
        # a pool advance and the task skips ahead — the fixer ports the
        # existing pool history into local /logs/artifacts/ before any attack.
        # That's intentional for single-task: there's no sibling task to share
        # incremental defenses with, so the only useful pool interaction is to
        # inherit whatever's already there.
        #
        # `harden_batch` pre-seeds fresh tasks' `pool_sha.txt` to the current
        # pool HEAD so iter 0 sees no advance and the hacker runs immediately.
        # The bootstrap is typically some other task's tests/ tree — including
        # its task-specific reference.py — which would corrupt sibling tasks'
        # correctness checks if integrated wholesale. Pass
        # --pool-integrate-bootstrap to skip the pre-seed and behave like the
        # single-task default above (only do this when the bootstrap is a
        # genuinely task-agnostic defense scaffold).
        self._sha: str | None = read_last_seen_sha(task_output_dir)

    @property
    def sha(self) -> str | None:
        return self._sha

    def iter_start(self) -> tuple[bool, str, str, str]:
        """Compute pool state for this iteration and bump the in-memory cursor.

        Returns (pool_advanced, pool_log, previous_sha, current_sha).
        Does NOT persist — caller invokes `persist()` at commit points.
        A fresh task (previous is None) always reports an advance so iter 0
        catches up to the seeded defense.

        Exception: if the pool advanced but every new commit is this task's
        own push (e.g., a prior iter pushed but its fix was rejected locally,
        so `advance_to_own_commit_if_newer` never fired), there's nothing peer
        to integrate. Treat as not-advanced (run hacker) and persist the new
        last_seen immediately so we don't re-evaluate the same range next iter.
        """
        current = get_pool_head(self._pool_server.bare_path)
        previous = self._sha
        advanced = (previous is None) or (current != previous)
        if (
            advanced and previous and range_only_authored_by(
                self._pool_server.bare_path, previous, current,
                f"harden-fixer-{self._task_id}",
            )
        ):
            logger.info(
                "Pool advanced %s..%s but every commit is this task's own — "
                "running hacker.",
                previous[:8], current[:8],
            )
            self._sha = current
            write_last_seen_sha(self._task_output_dir, current)
            return False, "", previous, current
        log = (
            get_pool_log_since(self._pool_server.bare_path, previous)
            if advanced else ""
        )
        self._sha = current
        return advanced, log, previous or "", current

    def advance_to_own_commit_if_newer(self) -> str | None:
        """If our most-recent pool commit is strictly newer than the in-memory
        cursor, advance to it; returns the SHA we advanced to, else None.

        Prevents next iter from skip-hacking to self-ack our own push, while
        still triggering skip-hacker when a concurrent task's commit is ahead.
        """
        own = get_latest_own_commit(self._pool_server.bare_path, self._task_id)
        if not own or own == self._sha:
            return None
        # On a fresh cursor (sha is None), any own commit is strictly newer.
        if self._sha is None or is_ancestor(
            self._pool_server.bare_path, self._sha, own,
        ):
            self._sha = own
            return own
        return None

    def persist(self) -> None:
        """Flush the in-memory cursor to pool_sha.txt."""
        write_last_seen_sha(self._task_output_dir, self._sha or "")
