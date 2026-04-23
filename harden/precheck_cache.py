"""Content-addressed cache for original-task solver prechecks."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .config import HardenConfig

PRECHECK_CACHE_VERSION = "v1"
SOLVER_PROMPT_ID = "task-instruction-only"
PRECHECK_CACHE_ROOT = Path(__file__).resolve().parent.parent / ".cache" / "precheck"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_path_tree(path: Path) -> str:
    h = hashlib.sha256()
    if not path.exists():
        h.update(b"missing")
        return h.hexdigest()

    if path.is_file():
        h.update(b"file")
        h.update(path.name.encode("utf-8"))
        h.update(b"\0")
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    h.update(b"dir")
    for child in sorted(path.rglob("*")):
        rel = child.relative_to(path).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\0")
        if child.is_symlink():
            h.update(b"symlink")
            h.update(str(child.readlink()).encode("utf-8"))
        elif child.is_dir():
            h.update(b"dir")
        elif child.is_file():
            h.update(b"file")
            with child.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
    return h.hexdigest()


def compute_task_content_hash(task_dir: Path) -> str:
    parts = {
        "instruction": _hash_path_tree(task_dir / "instruction.md"),
        "tests": _hash_path_tree(task_dir / "tests"),
        "environment": _hash_path_tree(task_dir / "environment"),
        "solution": _hash_path_tree(task_dir / "solution"),
    }
    return _sha256_text(json.dumps(parts, sort_keys=True))


def compute_solver_prompt_hash() -> str:
    return _sha256_text(SOLVER_PROMPT_ID)


def build_precheck_cache_key(*, task_dir: Path, config: HardenConfig) -> tuple[str, dict]:
    task_hash = compute_task_content_hash(task_dir)
    prompt_hash = compute_solver_prompt_hash()
    key_material = {
        "cache_version": PRECHECK_CACHE_VERSION,
        "task_hash": task_hash,
        "prompt_hash": prompt_hash,
        "model": config.solver_model,
        "max_turns": config.solver_max_turns,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "timeout_multiplier": config.solver_timeout_multiplier,
        "solver_precheck_retries": config.solver_precheck_retries,
        "solver_privileged": config.solver_privileged,
        "reasoning_effort": config.reasoning_effort,
    }
    return _sha256_text(json.dumps(key_material, sort_keys=True)), key_material


def cache_entry_path(cache_key: str) -> Path:
    return PRECHECK_CACHE_ROOT / f"{cache_key}.json"


def load_cached_precheck(cache_key: str, *, retry_failed: bool = False) -> dict | None:
    path = cache_entry_path(cache_key)
    if not path.exists():
        return None
    try:
        artifact = json.loads(path.read_text())
    except Exception:
        return None

    if retry_failed and not artifact.get("passed", False):
        return None

    trial_dir = artifact.get("trial_dir")
    if not trial_dir:
        return None

    trial_path = Path(trial_dir)
    if not trial_path.exists() or not (trial_path / "result.json").exists():
        return None

    artifact["cache_hit"] = True
    return artifact


def store_cached_precheck(cache_key: str, artifact: dict, *, key_material: dict) -> None:
    PRECHECK_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    cached = dict(artifact)
    cached["cache_hit"] = False
    cached["cache_key"] = cache_key
    cached["cache_version"] = PRECHECK_CACHE_VERSION
    cached["cache_key_material"] = key_material
    cache_entry_path(cache_key).write_text(json.dumps(cached, indent=2))


def clear_precheck_cache() -> None:
    if PRECHECK_CACHE_ROOT.exists():
        shutil.rmtree(PRECHECK_CACHE_ROOT)
