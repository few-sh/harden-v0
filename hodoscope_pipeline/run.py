"""Orchestrator: batch dir -> .hodoscope.json with one point per successful hack."""

from __future__ import annotations

import json
from pathlib import Path

from hodoscope import Config, process_trajectories, write_analysis_json

from .pack import HACK_SPEEDUP_THRESHOLD, build_trajectory, iter_successful_hacks
from .prompt import RH_SUMMARIZE_PROMPT


def pack_only(
    batch_dir: Path,
    limit: int | None = None,
    threshold: float = HACK_SPEEDUP_THRESHOLD,
) -> list[dict]:
    """Walk the batch dir and return the list of canonical hodoscope trajectories."""
    trajectories: list[dict] = []
    for record in iter_successful_hacks(batch_dir, threshold=threshold):
        trajectories.append(build_trajectory(record))
        if limit is not None and len(trajectories) >= limit:
            break
    return trajectories


def analyze_batch(
    batch_dir: Path,
    output: Path,
    config: Config | None = None,
    limit: int | None = None,
    threshold: float = HACK_SPEEDUP_THRESHOLD,
    embed: bool = True,
    dump_trajectories: Path | None = None,
) -> Path:
    """Pack a batch into a single .hodoscope.json.

    If ``embed`` is False, only the trajectory JSONs are written (to
    ``dump_trajectories`` or alongside ``output``) and no LLM calls are made.
    """
    if config is None:
        config = Config.from_env(summarize_prompt=RH_SUMMARIZE_PROMPT)
    elif config.summarize_prompt is None:
        config.summarize_prompt = RH_SUMMARIZE_PROMPT

    trajectories = pack_only(batch_dir, limit=limit, threshold=threshold)
    print(f"Packed {len(trajectories)} successful hacks from {batch_dir}")

    if dump_trajectories is not None:
        dump_trajectories.mkdir(parents=True, exist_ok=True)
        for traj in trajectories:
            (dump_trajectories / f"{traj['id']}.json").write_text(
                json.dumps(traj, indent=2)
            )

    if not embed:
        return output

    summaries = process_trajectories(trajectories, config=config)
    print(f"Got {len(summaries)} summaries")

    fields = {
        "source_batch": batch_dir.name,
        "hack_threshold": threshold,
        "summarize_model": config.summarize_model,
    }
    write_analysis_json(
        path=output,
        summaries=summaries,
        fields=fields,
        source=str(batch_dir),
        embedding_model=config.embedding_model,
        embedding_dimensionality=config.embed_dim,
    )
    return output
