"""Extract human-readable summaries from ATIF agent trajectories."""

import json
from pathlib import Path


def extract_hack_summary(trial_dir: Path, max_output_chars: int = 500) -> str:
    """Read trajectory.json and produce a readable step-by-step summary.

    Follows the ATIF format used by Terminus-2 (see filter_convert.py).
    """
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    if not trajectory_path.exists():
        return "(no trajectory found)"

    trajectory = json.loads(trajectory_path.read_text())
    steps = trajectory.get("steps", [])

    lines: list[str] = []
    step_num = 0

    for step in steps:
        if step.get("is_copied_context"):
            continue

        source = step.get("source")
        if source != "agent":
            continue

        step_num += 1
        msg = step.get("message", "")
        if isinstance(msg, list):
            msg = "\n".join(
                p.get("text", str(p)) if isinstance(p, dict) else str(p)
                for p in msg
            )

        # Truncate the agent's plan/reasoning
        plan_excerpt = msg[:300].strip()
        if len(msg) > 300:
            plan_excerpt += "..."
        lines.append(f"Step {step_num}: {plan_excerpt}")

        # Include tool calls (bash commands)
        tool_calls = step.get("tool_calls", [])
        for tc in tool_calls:
            fn = tc.get("function_name", "")
            args = tc.get("arguments", {})
            cmd = args.get("command", args.get("cmd", json.dumps(args)))
            lines.append(f"  $ {cmd}")

        # Include observation (truncated)
        observation = step.get("observation")
        if observation and observation.get("results"):
            for result in observation["results"]:
                content = result.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        p.get("text", str(p)) if isinstance(p, dict) else str(p)
                        for p in content
                    )
                content = content.strip()
                if content:
                    truncated = content[:max_output_chars]
                    if len(content) > max_output_chars:
                        truncated += "..."
                    lines.append(f"  → {truncated}")

    return "\n".join(lines) if lines else "(empty trajectory)"
