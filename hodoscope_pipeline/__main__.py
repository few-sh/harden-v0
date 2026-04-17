"""CLI: python -m hodoscope_pipeline BATCH_DIR [options]"""

from __future__ import annotations

import argparse
from pathlib import Path

from hodoscope import Config

from .pack import HACK_SPEEDUP_THRESHOLD
from .prompt import RH_SUMMARIZE_PROMPT
from .run import analyze_batch


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hodoscope_pipeline",
        description="Turn a harden-kb batch into a 1-point-per-hack hodoscope analysis.",
    )
    p.add_argument("batch_dir", type=Path,
                   help="Path to outputs/batch_YYYYMMDD_HHMMSS/")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Output .hodoscope.json path "
                        "(default: <batch_dir>/hack_plane.hodoscope.json)")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after this many successful hacks (for dry runs).")
    p.add_argument("--threshold", type=float, default=HACK_SPEEDUP_THRESHOLD,
                   help=f"Minimum speedup to count as a hack (default: {HACK_SPEEDUP_THRESHOLD}).")
    p.add_argument("--no-embed", action="store_true",
                   help="Skip LLM summarization and embedding. "
                        "Just emit the canonical trajectory JSONs to --dump-trajectories.")
    p.add_argument("--dump-trajectories", type=Path, default=None,
                   help="If set, also write one JSON per packed trajectory to this dir.")
    p.add_argument("--summarize-model", default=None,
                   help="Override the summarizer model (default: env or hodoscope default).")
    p.add_argument("--embedding-model", default=None,
                   help="Override the embedding model.")
    p.add_argument("--max-workers", type=int, default=None,
                   help="Parallel LLM workers.")
    p.add_argument("--reasoning-effort", default=None,
                   choices=["low", "medium", "high"],
                   help="Reasoning effort for summarization model.")
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)

    output = args.output or (args.batch_dir / "hack_plane.hodoscope.json")

    dump_dir = args.dump_trajectories
    if args.no_embed and dump_dir is None:
        dump_dir = args.batch_dir / "hack_plane_trajectories"

    config = Config.from_env(
        summarize_prompt=RH_SUMMARIZE_PROMPT,
        summarize_model=args.summarize_model,
        embedding_model=args.embedding_model,
        max_workers=args.max_workers,
        reasoning_effort=args.reasoning_effort,
    )

    analyze_batch(
        batch_dir=args.batch_dir,
        output=output,
        config=config,
        limit=args.limit,
        threshold=args.threshold,
        embed=not args.no_embed,
        dump_trajectories=dump_dir,
    )

    if args.no_embed:
        print(f"Dumped trajectories under {dump_dir}")
    else:
        print(f"Wrote analysis to {output}")


if __name__ == "__main__":
    main()
