"""Lightweight durable-computation decorator backed by JSON files.

Wrap a function with ``@durable(path=...)`` and its return value is persisted
to a JSON file. On subsequent calls with the same resolved path (and same
``json_path`` slot, if provided), the stored value is returned without
re-running the function.

Use cases:
    * Memoizing expensive deterministic computations across process restarts.
    * Resuming a long pipeline after a crash without redoing completed steps.
    * Caching results that are cheap to load but expensive to compute (LLM
      calls, network fetches, builds, simulations).

Design points:
    * Storage is per-file JSON. Atomic writes via temp-file + ``os.replace``
      (so a crash mid-write never leaves a partial file). A POSIX advisory
      lock (``flock``) on a sidecar ``<path>.lock`` file serializes
      concurrent readers/writers across processes on the same host.
    * Cache key = the resolved path (and optional ``json_path`` location
      inside the file). ``path`` is a Python format string filled from the
      wrapped function's bound arguments at call time, so the caller
      controls what makes a "same call" by which args appear in the path.
    * A module-level ``namespace`` (see :func:`set_namespace`) is also
      available as a ``{namespace}`` placeholder. Useful for code-version
      / config-fingerprint style invalidation without threading a value
      through every caller.
    * Crash safety: if the wrapped function raises, no cache entry is
      written. The next call re-runs.
    * No TTL, no cross-host coordination, no value validation hooks —
      keeps the surface area small. If you need to invalidate based on
      external state, change the namespace or include the relevant key
      in the path template.

Example:

    @durable(path="/var/cache/myapp/{namespace}/users/{user_id}.json")
    async def fetch_profile(user_id: str) -> dict:
        ...

    @durable(
        path="/var/cache/myapp/aggregates.json",
        json_path=("by_region", "{region}"),
    )
    def regional_total(region: str, items: list[int]) -> int:
        return sum(items)

Both ``async def`` and regular functions are supported. In the async
wrapper, file I/O is offloaded via ``asyncio.to_thread`` so flock waits
don't block the event loop.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import inspect
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

__all__ = ["durable", "set_namespace", "get_namespace", "JsonPath", "JsonPathKey"]

_MISSING = object()

JsonPathKey = str | int
JsonPath = Sequence[JsonPathKey]

# Module-level namespace string. Path templates and string ``json_path``
# elements may reference ``{namespace}``; the placeholder is filled from
# this variable. Function arguments named ``namespace`` shadow it.
#
# Plain global (not a ContextVar): callers needing per-task isolation
# under asyncio should set this to the same value across cooperating
# tasks, or wrap calls in a ContextVar of their own.
_namespace: str = ""


def set_namespace(ns: str) -> None:
    """Set the module-level ``{namespace}`` value used by path templates.

    Typical use: a code-version tag or config fingerprint that bumps when
    cached entries should be considered stale. Old entries remain on disk
    under the previous namespace; cleaning them up is the caller's job.
    """
    global _namespace
    _namespace = ns


def get_namespace() -> str:
    """Return the current module-level namespace value."""
    return _namespace


def _format_args(bound: inspect.BoundArguments) -> dict:
    args: dict = {"namespace": _namespace}
    args.update(bound.arguments)
    return args


def _resolve_path(template: str, bound: inspect.BoundArguments) -> Path:
    return Path(template.format(**_format_args(bound)))


def _resolve_json_path(json_path: JsonPath | None, bound: inspect.BoundArguments) -> tuple[JsonPathKey, ...]:
    if not json_path:
        return ()
    fmt_args = _format_args(bound)
    resolved: list[JsonPathKey] = []
    for key in json_path:
        if isinstance(key, str):
            resolved.append(key.format(**fmt_args))
        else:
            resolved.append(key)
    return tuple(resolved)


def _navigate(data: Any, keys: Sequence[JsonPathKey]) -> Any:
    cur = data
    for key in keys:
        if isinstance(key, int):
            if not isinstance(cur, list) or not (-len(cur) <= key < len(cur)):
                return _MISSING
            cur = cur[key]
        else:
            if not isinstance(cur, dict) or key not in cur:
                return _MISSING
            cur = cur[key]
    return cur


def _set_at(data: Any, keys: Sequence[JsonPathKey], value: Any) -> Any:
    if not keys:
        return value
    head, *rest = keys
    if isinstance(head, int):
        if data is None:
            data = []
        if not isinstance(data, list):
            raise TypeError(f"durable: cannot write int key {head!r} into {type(data).__name__}")
        if head < 0:
            raise ValueError("durable: negative list indices not supported in writes")
        while len(data) <= head:
            data.append(None)
        data[head] = _set_at(data[head], rest, value)
        return data
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TypeError(f"durable: cannot write str key {head!r} into {type(data).__name__}")
    data[head] = _set_at(data.get(head), rest, value)
    return data


def _lock_file(data_path: Path) -> Path:
    return data_path.with_name(data_path.name + ".lock")


def _read_json(data_path: Path) -> Any:
    try:
        with data_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return _MISSING
    except json.JSONDecodeError:
        return _MISSING


def _atomic_write_json(data_path: Path, payload: Any) -> None:
    tmp_path = data_path.with_name(f"{data_path.name}.tmp.{os.getpid()}")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, data_path)


def _load_cached(data_path: Path, keys: Sequence[JsonPathKey]) -> Any:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_file(data_path)
    with lock_path.open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
        try:
            data = _read_json(data_path)
            if data is _MISSING:
                return _MISSING
            return _navigate(data, keys)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _store_result(data_path: Path, keys: Sequence[JsonPathKey], value: Any) -> None:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_file(data_path)
    with lock_path.open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            existing = _read_json(data_path)
            base = None if existing is _MISSING else existing
            new_data = value if not keys else _set_at(base, keys, value)
            _atomic_write_json(data_path, new_data)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def durable(
    path: str | Path,
    json_path: JsonPath | None = None,
):
    """Persist a function's return value to a JSON file and reuse it on later calls.

    Parameters
    ----------
    path
        Path to the JSON file, as a format string. Placeholders ``{name}``
        are filled from the wrapped function's bound arguments at call time
        (defaults applied via :meth:`inspect.Signature.bind`). The reserved
        placeholder ``{namespace}`` is filled from the module-level value
        set by :func:`set_namespace` unless an argument of the same name is
        explicitly passed (caller args win). Format-string attribute access
        is supported (``{arg.attr}``), useful for deriving path components
        from a ``Path`` argument.
    json_path
        Optional sequence locating the cached value inside the file.
        ``str`` elements traverse dicts, ``int`` elements index into lists.
        String elements are also format-strings (templated the same way as
        ``path``). When omitted, the entire file content is the cached
        value.

    Behavior
    --------
    * **Cache hit**: file at ``path`` exists, parses as JSON, and the
      ``json_path`` location is present → stored value is returned;
      function body is not invoked.
    * **Cache miss**: missing file, malformed JSON, or absent ``json_path``
      key → function runs; result is written under an exclusive flock,
      stored atomically (temp file + ``os.replace``), and returned.
    * **Function raises**: no cache entry is written; next call re-runs.
    * **Concurrent callers**: serialized via flock on a sidecar
      ``<path>.lock`` file. Readers acquire a shared lock; writers
      acquire an exclusive lock. Cross-process safe on a single host.

    Both ``async def`` and regular functions are supported. In the async
    wrapper, file I/O is offloaded via :func:`asyncio.to_thread` so flock
    waits don't block the event loop.

    Notes
    -----
    Returned values must be JSON-serializable. The function's return value
    is round-tripped through ``json.dumps``/``json.loads``, so types that
    don't survive that round trip (e.g. tuples become lists, ``Path``
    becomes ``str``) should be avoided or converted explicitly by the
    caller.
    """
    path_template = str(path)

    def decorator(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        def _resolve(args: tuple, kwargs: dict) -> tuple[Path, tuple[JsonPathKey, ...]]:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return _resolve_path(path_template, bound), _resolve_json_path(json_path, bound)

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                data_path, keys = _resolve(args, kwargs)
                cached = await asyncio.to_thread(_load_cached, data_path, keys)
                if cached is not _MISSING:
                    return cached
                result = await fn(*args, **kwargs)
                await asyncio.to_thread(_store_result, data_path, keys, result)
                return result

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            data_path, keys = _resolve(args, kwargs)
            cached = _load_cached(data_path, keys)
            if cached is not _MISSING:
                return cached
            result = fn(*args, **kwargs)
            _store_result(data_path, keys, result)
            return result

        return sync_wrapper

    return decorator
