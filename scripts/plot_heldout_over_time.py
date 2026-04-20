#!/usr/bin/env python3
"""Plot held-out hack rate over pool-commit time.

Held-out corpus: 136 trials from `tmp/combined_trials/` (3 solver + 134 hint attempts
pooled from Apr-17 probe and Apr-18 relaxed probe, predating the pool commits we replay
against — they are truly "held-out" w.r.t. every pool snapshot).

Defense trajectory: 20 sweep snapshots (every 10th commit of pool.git) from
`outputs/sweep_20260419_021657/`, plus pristine and merged baselines from
`outputs/replay_combined_pristine_*` and `outputs/replay_combined_merged_*`.

Produces:
  report_apr18/heldout_hack_rate_over_time.png   (line chart)
  report_apr18/heldout_hack_heatmap.png          (per-hint × commit heatmap)
  report_apr18/heldout_hack_rate.csv             (data dump)
"""
from __future__ import annotations

import json
from pathlib import Path
import csv
import subprocess
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path("/lambda/nfs/reward-hacking/harden-unified-jumper")
SWEEP = ROOT / "outputs/sweep_20260419_021657"
PRISTINE = ROOT / "outputs/replay_combined_pristine_20260419_002901"
MERGED = ROOT / "outputs/replay_combined_merged_20260418_235210"
MERGED_PRIV = ROOT / "outputs/replay_combined_merged_priv_relaxed_20260419_173912"
POOL = ROOT / "tmp/final_pool"
REPORT = ROOT / "report_apr18"


def load_trials(dir_: Path) -> list[dict]:
    rows = []
    for mp in sorted(dir_.glob("trials/*/meta.json")):
        rows.append(json.loads(mp.read_text()))
    return rows


def aggregate(rows: list[dict]) -> dict:
    """Return per-hint and total hack counts."""
    hint_agg: dict[str, list[int]] = {}  # hint_id -> [hacks, total]
    solver = [0, 0]
    for r in rows:
        cond = r["condition"]
        hack = int(r["hack_succeeded"])
        if cond == "solver":
            solver[0] += hack
            solver[1] += 1
        else:
            hid = r["hint_id"]
            b = hint_agg.setdefault(hid, [0, 0])
            b[0] += hack
            b[1] += 1
    total_hint = [sum(v[0] for v in hint_agg.values()),
                  sum(v[1] for v in hint_agg.values())]
    return {"hints": hint_agg, "solver": solver, "hint_total": total_hint}


def commit_time(sha: str) -> float:
    """Unix timestamp of commit."""
    try:
        return float(subprocess.check_output(
            ["git", "-C", str(POOL), "log", "-1", "--format=%at", sha],
            text=True
        ).strip())
    except Exception:
        return float("nan")


def main() -> None:
    # Load sweep snapshots
    sweep_rows = []
    with (SWEEP / "sweep_summary.csv").open() as f:
        for r in csv.DictReader(f):
            idx = int(r["index"])
            sha = r["sha"]
            short = r["short"]
            out_dir = Path(r["output_dir"])
            if not out_dir.is_absolute():
                out_dir = ROOT / out_dir
            agg = aggregate(load_trials(out_dir))
            sweep_rows.append({
                "index": idx,
                "sha": sha,
                "short": short,
                "time": commit_time(sha),
                "title": r["title"],
                "hints_hack": agg["hint_total"][0],
                "hints_total": agg["hint_total"][1],
                "solver_hack": agg["solver"][0],
                "solver_total": agg["solver"][1],
                "per_hint": agg["hints"],
            })
    # Baselines
    pristine_agg = aggregate(load_trials(PRISTINE))
    merged_agg = aggregate(load_trials(MERGED))
    merged_priv_agg = aggregate(load_trials(MERGED_PRIV))

    # ---- csv dump ----
    csv_path = REPORT / "heldout_hack_rate.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["snapshot", "commit_index", "sha", "hint_hacks", "hint_total",
                    "hint_rate", "solver_hacks", "solver_total"])
        w.writerow(["pristine", "", "", pristine_agg["hint_total"][0],
                    pristine_agg["hint_total"][1],
                    pristine_agg["hint_total"][0] / pristine_agg["hint_total"][1],
                    pristine_agg["solver"][0], pristine_agg["solver"][1]])
        w.writerow(["merged", "", "", merged_agg["hint_total"][0],
                    merged_agg["hint_total"][1],
                    merged_agg["hint_total"][0] / merged_agg["hint_total"][1],
                    merged_agg["solver"][0], merged_agg["solver"][1]])
        w.writerow(["merged_privileged", "", "", merged_priv_agg["hint_total"][0],
                    merged_priv_agg["hint_total"][1],
                    merged_priv_agg["hint_total"][0] / merged_priv_agg["hint_total"][1],
                    merged_priv_agg["solver"][0], merged_priv_agg["solver"][1]])
        for r in sweep_rows:
            w.writerow([f"pool_commit_{r['index']:03d}", r["index"], r["short"],
                        r["hints_hack"], r["hints_total"],
                        r["hints_hack"] / r["hints_total"],
                        r["solver_hack"], r["solver_total"]])
    print(f"Wrote {csv_path}")

    # ---- line chart: hack rate over commit index ----
    xs = [r["index"] for r in sweep_rows]
    ys_pct = [100.0 * r["hints_hack"] / r["hints_total"] for r in sweep_rows]

    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.axhline(100 * pristine_agg["hint_total"][0] / pristine_agg["hint_total"][1],
               color="gray", linestyle="--", alpha=0.7,
               label=f"pristine ({pristine_agg['hint_total'][0]}/{pristine_agg['hint_total'][1]} = {100*pristine_agg['hint_total'][0]/pristine_agg['hint_total'][1]:.1f}%)")
    ax.axhline(100 * merged_agg["hint_total"][0] / merged_agg["hint_total"][1],
               color="orange", linestyle="--", alpha=0.7,
               label=f"merged ({merged_agg['hint_total'][0]}/{merged_agg['hint_total'][1]} = {100*merged_agg['hint_total'][0]/merged_agg['hint_total'][1]:.1f}%)")
    ax.axhline(100 * merged_priv_agg["hint_total"][0] / merged_priv_agg["hint_total"][1],
               color="purple", linestyle=":", alpha=0.75,
               label=f"merged_privileged (LLM-compounded, {merged_priv_agg['hint_total'][0]}/{merged_priv_agg['hint_total'][1]} = {100*merged_priv_agg['hint_total'][0]/merged_priv_agg['hint_total'][1]:.1f}%)")
    ax.plot(xs, ys_pct, "o-", color="tab:blue", lw=2, ms=7,
            label="pool @ commit N (every 10th)")
    # Annotate regression + final closure
    for r in sweep_rows:
        if r["index"] == 50:
            ax.annotate(f"  regression\n  (commit 49 broke\n   post-timing check)",
                        xy=(r["index"], 100 * r["hints_hack"] / r["hints_total"]),
                        xytext=(r["index"] + 3, 100 * r["hints_hack"] / r["hints_total"] + 0.5),
                        fontsize=9, color="darkred",
                        arrowprops=dict(arrowstyle="->", color="darkred", alpha=0.6))
        if r["index"] == 185:
            ax.annotate(f"  force ref\n  get_inputs",
                        xy=(r["index"], 100 * r["hints_hack"] / r["hints_total"]),
                        xytext=(r["index"] - 25, 3.0),
                        fontsize=9, color="darkgreen",
                        arrowprops=dict(arrowstyle="->", color="darkgreen", alpha=0.6))
    ax.set_xlabel("Pool commit index (chronological)")
    ax.set_ylabel("Held-out hack rate (% of 134 hint trials)")
    ax.set_title("Held-out hack rate over pool progress\n"
                 "Corpus: 134 hint solutions from Apr-17 + Apr-18 probes (defense-blind)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", framealpha=0.95)
    ax.set_xlim(-3, 195)
    ax.set_ylim(-1, 70)
    fig.tight_layout()
    png1 = REPORT / "heldout_hack_rate_over_time.png"
    fig.savefig(png1, dpi=140)
    print(f"Wrote {png1}")
    plt.close(fig)

    # ---- heatmap: per-hint × commit ----
    all_hints = sorted({h for r in sweep_rows for h in r["per_hint"]})
    # Also include pristine + merged as virtual columns
    cols = ["pristine", "merged", "merged_priv"] + [f"c{r['index']:03d}" for r in sweep_rows]
    col_data = []
    # pristine
    col = []
    for h in all_hints:
        pair = pristine_agg["hints"].get(h, [0, 0])
        col.append(pair[0] / max(pair[1], 1))
    col_data.append(col)
    # merged
    col = []
    for h in all_hints:
        pair = merged_agg["hints"].get(h, [0, 0])
        col.append(pair[0] / max(pair[1], 1))
    col_data.append(col)
    # merged_privileged
    col = []
    for h in all_hints:
        pair = merged_priv_agg["hints"].get(h, [0, 0])
        col.append(pair[0] / max(pair[1], 1))
    col_data.append(col)
    for r in sweep_rows:
        col = []
        for h in all_hints:
            pair = r["per_hint"].get(h, [0, 0])
            col.append(pair[0] / max(pair[1], 1))
        col_data.append(col)

    M = np.array(col_data).T  # hints × cols

    fig, ax = plt.subplots(figsize=(14, 7))
    im = ax.imshow(M, aspect="auto", cmap="Reds", vmin=0.0, vmax=1.0)
    ax.set_yticks(range(len(all_hints)))
    ax.set_yticklabels(all_hints, fontsize=9)
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels(cols, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel("Defense snapshot (non-pool baselines | pool-commit index)")
    ax.set_ylabel("Hint class")
    ax.set_title("Per-hint held-out hack rate across defense snapshots")
    # Vertical separator between non-pool baselines (pristine/merged/merged_priv)
    # and the pool snapshots. Column index 2 = last baseline; divider goes at 2.5.
    ax.axvline(2.5, color="black", linewidth=1.5, alpha=0.6)
    # annotate cells with count
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            v = M[i, j]
            if v > 0:
                # find the raw count
                if j == 0:
                    pair = pristine_agg["hints"].get(all_hints[i], [0, 0])
                elif j == 1:
                    pair = merged_agg["hints"].get(all_hints[i], [0, 0])
                elif j == 2:
                    pair = merged_priv_agg["hints"].get(all_hints[i], [0, 0])
                else:
                    pair = sweep_rows[j - 3]["per_hint"].get(all_hints[i], [0, 0])
                ax.text(j, i, f"{pair[0]}", ha="center", va="center",
                        fontsize=7,
                        color="white" if v > 0.5 else "black")
    fig.colorbar(im, ax=ax, label="hack rate (fraction of attempts)")
    fig.tight_layout()
    png2 = REPORT / "heldout_hack_heatmap.png"
    fig.savefig(png2, dpi=140)
    print(f"Wrote {png2}")
    plt.close(fig)


if __name__ == "__main__":
    main()
