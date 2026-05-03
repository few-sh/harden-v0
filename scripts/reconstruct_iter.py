#!/usr/bin/env python3
"""Reconstruct task 001's hardened state at iter N for an ablation.

Usage:
  reconstruct_iter.py --ablation kb-l1-abl-priv-pool --iter 5 --out-dir <path>
  --iter -1 = pristine baseline (no defense applied)
"""
import argparse, json, re, shutil, sys, glob
from pathlib import Path

OVER_RESTRICTIVE_PATTERNS = [
    # (regex, replacement) — keep as no-op marked with MANUAL FIX comment.
    # IMPORTANT: trailing whitespace must be `[ \t]*` (NOT `\s*`) — `\s*` would
    # eat the line's `\n` and concatenate with the next statement.
    (r"^(\s*)sys\.modules\['inspect'\]\s*=\s*None[ \t]*$",
     r"\1# MANUAL FIX: removed sys.modules['inspect']=None (breaks torch.cpp_extension.load_inline)"),
    (r"^(\s*)sys\.modules\['ctypes'\]\s*=\s*None[ \t]*$",
     r"\1# MANUAL FIX: removed sys.modules['ctypes']=None"),
    # combined-assignment form
    (r"^(\s*)sys\.modules\['ctypes'\]\s*=\s*sys\.settrace\s*=\s*sys\.setprofile\s*=\s*sys\.modules\['inspect'\]\s*=\s*None[ \t]*$",
     r"\1sys.settrace = sys.setprofile = None  # MANUAL FIX: removed module poisoning"),
    # restricted_getframe — keep raw _sf, kill the wrapper
    (r"^(\s*)sys\._getframe\s*=\s*restricted_getframe[ \t]*$",
     r"\1pass  # MANUAL FIX: do not install restricted_getframe (blocks load_inline)"),
    (r"^(\s*)sys\._getframe\s*=\s*make_restricted_getframe\(_sf\)[ \t]*$",
     r"\1pass  # MANUAL FIX: do not install restricted_getframe wrapper"),
    # audit_hook with 'sys._getframe' event
    (r"'sys\._getframe',?[ \t]*", r""),
]

def auto_patch(text: str) -> tuple[str, int]:
    n = 0
    out_lines = []
    for line in text.splitlines(keepends=True):
        original = line
        for pat, repl in OVER_RESTRICTIVE_PATTERNS:
            new = re.sub(pat, repl, line, flags=re.MULTILINE)
            if new != line:
                line = new
                n += 1
        out_lines.append(line)
    return "".join(out_lines), n


def find_iter_artifact(task_dir: Path, iter_n: int) -> Path | None:
    """Find the fixer_iterN trial dir whose fix was applied. Returns artifacts/ path or None."""
    if iter_n < 0:
        return None
    rj = task_dir / "result.json"
    iters = json.loads(rj.read_text()).get("iterations", [])
    if not iters:
        return None
    # cap iter_n to the last available iter (so iter=999 returns final state)
    start = min(iter_n, len(iters) - 1)
    # walk back until we find a fix_applied=True
    for i in range(start, -1, -1):
        if iters[i].get("fix_applied"):
            # find this iter's fixer trial
            jobs = sorted(glob.glob(str(task_dir / f"jobs/fixer_iter{i}__*/kernelbench-*")))
            if jobs:
                art = Path(jobs[-1]) / "artifacts"
                if art.is_dir():
                    return art
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ablation", required=True, help="e.g. kb-l1-abl-priv-pool")
    p.add_argument("--iter", type=int, required=True, help="iter index, -1 for pristine")
    p.add_argument("--out-dir", required=True, type=Path, help="staged task dir output")
    p.add_argument("--task-id", default="kernelbench-level1-001-square-matrix-multiplication")
    args = p.parse_args()

    pristine = Path(f"/lambda/nfs/reward-hacking/harbor/datasets/kernelbench/{args.task_id}")
    task_dir = Path(f"/lambda/nfs/reward-hacking/harden-v0/outputs/{args.ablation}/{args.task_id}")

    # start from pristine
    if args.out_dir.exists():
        shutil.rmtree(args.out_dir)
    args.out_dir.mkdir(parents=True)
    for item in pristine.iterdir():
        if item.is_dir():
            shutil.copytree(item, args.out_dir / item.name)
        else:
            shutil.copy2(item, args.out_dir / item.name)

    # overlay iter-N fixer artifacts (full replace of tests/, environment/)
    if args.iter >= 0:
        art = find_iter_artifact(task_dir, args.iter)
        if art is None:
            print(f"WARN: no applied fixer found at iter ≤ {args.iter} for {args.ablation}", file=sys.stderr)
        else:
            for sub in ("tests", "environment"):
                src = art / sub
                if src.is_dir():
                    dest = args.out_dir / sub
                    if dest.exists(): shutil.rmtree(dest)
                    shutil.copytree(src, dest)
            print(f"OK: applied artifacts from {art}")

    # auto-patch
    ek = args.out_dir / "tests" / "eval_kernel.py"
    if ek.exists():
        text = ek.read_text()
        new_text, n = auto_patch(text)
        if n:
            ek.write_text(new_text)
            print(f"PATCHED: {n} over-restrictive lines in {ek}")
        else:
            print(f"NO_PATCH_NEEDED: {ek}")

    print(f"DONE: {args.out_dir}")


if __name__ == "__main__":
    main()
