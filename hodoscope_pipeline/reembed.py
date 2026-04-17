"""Re-embed an existing hack-plane analysis using only selected JSON-summary fields.

Each `summary` field in the input is a stringified JSON like
{"target":"...", "mechanism":"...", "evasion":"...", "payload_site":"..."}.
This rewrites it as just the value(s) of the chosen field(s) and re-embeds,
so the resulting plane clusters by exactly the dimension you pick.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hodoscope import Config, read_analysis_json, write_analysis_json
from hodoscope.actions import embed_summary
from hodoscope.core import run_parallel


def _project(summary_text: str, fields: list[str]) -> str:
    try:
        obj = json.loads(summary_text)
    except json.JSONDecodeError:
        return summary_text
    return " | ".join(str(obj.get(f, "")) for f in fields).strip(" |")


def reembed_on_fields(
    input_path: Path,
    output_path: Path,
    fields: list[str],
    config: Config | None = None,
) -> Path:
    """Replace each summary with `field1 | field2 | ...` then re-embed."""
    if config is None:
        config = Config.from_env()

    doc = read_analysis_json(input_path)
    summaries = doc["summaries"]

    for s in summaries:
        s["summary"] = _project(s["summary"], fields)

    def _embed(args):
        return embed_summary(args[0], config=args[1])

    tasks = [(s, config) for s in summaries]
    embedded = run_parallel(
        func=_embed,
        tasks=tasks,
        max_workers=config.max_workers,
        desc=f"Re-embedding on {'+'.join(fields)}",
    )

    fields_meta = dict(doc.get("fields", {}))
    fields_meta["reembedded_on"] = "+".join(fields)
    fields_meta["reembedded_from"] = str(input_path)

    return write_analysis_json(
        path=output_path,
        summaries=embedded,
        fields=fields_meta,
        source=doc.get("source", str(input_path)),
        embedding_model=config.embedding_model,
        embedding_dimensionality=config.embed_dim,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hodoscope_pipeline.reembed",
        description="Re-embed an existing hack-plane on selected JSON-summary fields.",
    )
    p.add_argument("input", type=Path, help="Existing .hodoscope.json")
    p.add_argument("-f", "--fields", required=True,
                   help="Comma-separated JSON keys to project (e.g. 'target' or 'target,mechanism').")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Output path (default: <input_stem>.<fields>.hodoscope.json)")
    p.add_argument("--embedding-model", default=None)
    p.add_argument("--max-workers", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    if args.output is None:
        slug = "-".join(fields)
        args.output = args.input.with_suffix("").with_suffix(f".{slug}.hodoscope.json")

    config = Config.from_env(
        embedding_model=args.embedding_model,
        max_workers=args.max_workers,
    )
    out = reembed_on_fields(args.input, args.output, fields, config=config)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
