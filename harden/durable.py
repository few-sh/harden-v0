"""Lightweight durable computing decorator backed by JSON files with flock."""

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

_MISSING = object()

JsonPathKey = str | int
JsonPath = Sequence[JsonPathKey]


def _resolve_path(template: str, bound: inspect.BoundArguments) -> Path:
    return Path(template.format(**bound.arguments))


def _resolve_json_path(json_path: JsonPath | None, bound: inspect.BoundArguments) -> tuple[JsonPathKey, ...]:
    if not json_path:
        return ()
    resolved: list[JsonPathKey] = []
    for key in json_path:
        if isinstance(key, str):
            resolved.append(key.format(**bound.arguments))
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
    """Persist function results to a JSON file under flock.

    The first call writes the result; later calls with the same resolved
    (path, json_path) return the stored value without re-running the function.

    `path` and any string elements of `json_path` are format strings: any
    `{name}` placeholders are filled from the wrapped function's arguments at
    call time (using `inspect.Signature.bind` + defaults). `json_path` is a
    sequence of dict keys (str) and/or list indices (int); when omitted the
    entire file content is the cached value.

    Works on both `async def` and regular functions; in the async wrapper the
    JSON I/O is offloaded via `asyncio.to_thread` so flock waits don't block
    the event loop.
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
