#!/usr/bin/env python3
"""Monitor KernelBench hardening batch progress."""

import json
import re
import sys
from pathlib import Path

R = "\033[31m"  # red
G = "\033[32m"  # green
B = "\033[34m"  # blue
C = "\033[36m"  # cyan
DIM = "\033[90m"
RST = "\033[0m"

OK = f"{G}\u2713{RST}"
FAIL = f"{R}\u2717{RST}"
SPIN = f"{C}\u2026{RST}"
LEGIT = f"{C}\u25cb{RST}"

# KernelBench hack threshold (for display purposes)
HACK_THRESHOLD = 10.0


def get_job_status(job_dir):
    """Return (completed, reward) for a job directory."""
    for d in sorted(job_dir.iterdir()):
        if not d.is_dir():
            continue
        rp = d / "result.json"
        if rp.exists():
            try:
                data = json.loads(rp.read_text())
                vr = data.get("verifier_result") or {}
                rewards = vr.get("rewards") or {}
                return True, rewards.get("reward")
            except Exception:
                return True, None
    return False, None


def collect_jobs(jobs_dir):
    """Parse all jobs into structured data: precheck + per-iteration groups."""
    if not jobs_dir or not jobs_dir.exists():
        return None, {}

    precheck = None
    iters = {}

    for d in sorted(jobs_dir.iterdir()):
        if not d.is_dir():
            continue
        m = re.match(r"^(.+?)__(\d{8}_\d{6})__", d.name)
        if not m:
            continue
        role = m.group(1)
        done, reward = get_job_status(d)

        if role.startswith("solver_precheck"):
            precheck = (done, reward)
            continue

        m2 = re.match(r"(hacker|fixer|solver_validate)_iter(\d+)(?:_a(\d+))?", role)
        if not m2:
            continue
        sub, it = m2.group(1), int(m2.group(2))
        att = int(m2.group(3)) if m2.group(3) else None

        if it not in iters:
            iters[it] = {"hackers": [], "fixer": None, "solver": None}

        if sub == "hacker":
            iters[it]["hackers"].append((att or 0, done, reward))
        elif sub == "fixer":
            iters[it]["fixer"] = (done, reward, d)
        elif sub == "solver_validate":
            iters[it]["solver"] = (done, reward)

    return precheck, iters


def fixer_marked_legitimate(fixer_job_dir):
    """Check if fixer's artifacts contain .legitimate sentinel."""
    if not fixer_job_dir:
        return False
    for trial_dir in fixer_job_dir.iterdir():
        if not trial_dir.is_dir():
            continue
        sentinel = trial_dir / "artifacts" / ".legitimate"
        if sentinel.exists():
            return True
    return False


def hack_marks(hackers, threshold=HACK_THRESHOLD, include_last=True):
    """Build colored marks from hacker attempts (uses speedup threshold)."""
    items = hackers if include_last else hackers[:-1]
    marks = []
    for _, d, r in items:
        if not d:
            continue
        if r is not None and r >= threshold:
            marks.append(f"{OK}{DIM}({r:.0f}x){RST}")
        else:
            r_str = f"({r:.1f}x)" if r is not None else ""
            marks.append(f"{FAIL}{DIM}{r_str}{RST}")
    return "".join(marks)


def _show_task_to(task_dir, out=None):
    """Display a single task's progress."""
    p = (lambda *a, **kw: print(*a, **kw, file=out)) if out else print

    task_id = task_dir.name
    jobs_dir = task_dir / "jobs"
    result_path = task_dir / "result.json"

    status = None
    if result_path.exists():
        try:
            status = json.loads(result_path.read_text()).get("status")
        except Exception:
            pass

    tag = {
        "robust": f"{G}ROBUST{RST}",
        "max_iterations": f"\033[33mMAX ITERS{RST}",
        "solver_failed_precheck": f"{R}PRECHECK FAILED{RST}",
    }
    if status:
        label = tag.get(status, status.upper())
    elif not jobs_dir.exists() or not any(jobs_dir.iterdir()):
        label = f"{DIM}WAITING{RST}"
    else:
        label = f"{C}RUNNING{RST}"

    p(f"  {task_id}  [{label}]")

    precheck, iters = collect_jobs(jobs_dir)

    if precheck is not None:
        done, reward = precheck
        if not done:
            p(f"    precheck {SPIN}")
            return
        if reward is not None and reward < 0.5:
            p(f"    precheck {FAIL} (speedup={reward:.2f})")
            return

    if not iters:
        return

    for i in sorted(iters):
        it = iters[i]
        hackers = sorted(it["hackers"])
        fixer = it["fixer"]
        solver = it["solver"]

        parts = [f"    {i}: "]

        if not hackers:
            parts.append(f"{DIM}reused{RST}")
        else:
            last_att, h_done, h_reward = hackers[-1]
            if not h_done:
                parts.append(f"hack {hack_marks(hackers, include_last=False)}{SPIN}")
                p("".join(parts))
                return

            succeeded = h_reward is not None and h_reward >= HACK_THRESHOLD
            if succeeded:
                parts.append(f"hack {hack_marks(hackers)}")
            else:
                iter_done = (i + 1) in iters or status is not None
                if iter_done:
                    parts.append(f"hack {hack_marks(hackers)}")
                    p("".join(parts))
                    continue
                else:
                    parts.append(f"hack {hack_marks(hackers)} retrying {SPIN}")
                    p("".join(parts))
                    return

        if fixer is None:
            parts.append(f" \u2192 fix {SPIN}")
            p("".join(parts))
            return
        f_done, _, f_dir = fixer
        if not f_done:
            parts.append(f" \u2192 fix {SPIN}")
            p("".join(parts))
            return

        if solver is None and fixer_marked_legitimate(f_dir):
            parts.append(f" \u2192 fix {LEGIT} legitimate")
            p("".join(parts))
            continue

        if solver is None:
            if (i + 1) in iters or status is not None:
                parts.append(f" \u2192 fix {FAIL}")
            else:
                parts.append(f" \u2192 fix {OK} \u2192 solve {SPIN}")
                p("".join(parts))
                return
        else:
            s_done, s_reward = solver
            if not s_done:
                parts.append(f" \u2192 fix {OK} \u2192 solve {SPIN}")
                p("".join(parts))
                return
            if s_reward is not None and s_reward >= 0.5:
                parts.append(f" \u2192 fix {OK} \u2192 solve {OK}")
            else:
                s_str = f"({s_reward:.2f})" if s_reward is not None else ""
                parts.append(f" \u2192 fix {OK} \u2192 solve {FAIL}{DIM}{s_str}{RST}")

        p("".join(parts))


def show_task(task_dir):
    """Display a single task's progress to stdout."""
    _show_task_to(task_dir)


def show_diff(batch_dir, task_name, tasks_dir):
    """Show the artifact diff for the last applied fixer of a task."""
    import subprocess

    task_dir = batch_dir / task_name
    if not task_dir.is_dir():
        print(f"Task not found: {task_name}")
        print(f"Available: {', '.join(d.name for d in sorted(batch_dir.iterdir()) if d.is_dir())}")
        return

    original_dir = tasks_dir / task_name
    hardened_dir = task_dir / "hardened" / task_name

    if not hardened_dir.is_dir():
        print(f"  No hardened state found at {hardened_dir}")
        return
    if not original_dir.is_dir():
        print(f"  Original task not found at {original_dir}")
        return

    original_abs = str(original_dir.resolve())
    solver_abs = str(hardened_dir.resolve())
    result = subprocess.run(
        ["git", "diff", "--no-index", "--color=always",
         "--", original_abs, solver_abs],
        capture_output=True, text=True,
    )
    diff_output = result.stdout.replace(
        "a" + original_abs, "a/original"
    ).replace(
        "b" + solver_abs, "b/hardened"
    )

    import io
    header = io.StringIO()
    _show_task_to(task_dir, header)
    header.write(f"\n  {DIM}diff: original \u2192 hardened{RST}\n\n")

    if diff_output:
        try:
            pager = subprocess.Popen(["less", "-R"], stdin=subprocess.PIPE)
            pager.communicate(input=(header.getvalue() + diff_output).encode())
        except (BrokenPipeError, KeyboardInterrupt):
            pass
    else:
        print(header.getvalue())
        print("  (no changes)")


def resolve_batch_dir(arg=None):
    """Resolve batch directory from argument or find latest."""
    if arg and Path(arg).is_dir():
        return Path(arg)
    outputs = Path("outputs")
    batches = sorted(d for d in outputs.iterdir() if d.is_dir()) if outputs.exists() else []
    if not batches:
        return None
    return batches[-1]


def main():
    args = sys.argv[1:]
    tasks_dir = Path("/lambda/nfs/reward-hacking/harbor/datasets/kernelbench")

    if args and args[0] == "show":
        if len(args) < 2:
            print("Usage: python monitor.py show <task_name> [batch_dir]")
            return
        task_name = args[1]
        batch_dir = resolve_batch_dir(args[2] if len(args) > 2 else None)
        if not batch_dir:
            print("No batches found.")
            return
        show_diff(batch_dir, task_name, tasks_dir)
        return

    batch_dir = resolve_batch_dir(args[0] if args else None)
    if not batch_dir:
        print("No batches found. Usage: python monitor.py [batch_dir]")
        return
    if not batch_dir.is_dir():
        print(f"Not a directory: {batch_dir}")
        return

    batch_done = (batch_dir / "batch_summary.json").exists()
    batch_tag = f"{G}DONE{RST}" if batch_done else f"{C}RUNNING{RST}"
    print(f"\n  {batch_dir.name}  [{batch_tag}]\n")

    tasks = sorted(
        d for d in batch_dir.iterdir()
        if d.is_dir() and d.name != "jobs"
    )

    for task_dir in tasks:
        show_task(task_dir)
        print()


if __name__ == "__main__":
    main()
