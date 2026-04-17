#!/usr/bin/env python3
"""Extract key investigation data from a Claude session log."""
import json
import sys

log_path = sys.argv[1]

with open(log_path) as f:
    lines = [line.strip() for line in f if line.strip()]

for line in lines:
    try:
        obj = json.loads(line)
    except:
        continue

    # Look for tool results that contain file content reads
    if obj.get("type") == "user":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        for c in content:
            if c.get("type") == "tool_result":
                tur = obj.get("tool_use_result", {})
                if isinstance(tur, dict) and tur.get("type") == "text":
                    file_info = tur.get("file", {})
                    fp = file_info.get("filePath", "")
                    if "result.json" in fp and ("iter1" in fp or "iter_1" in fp or "hacker_iter1" in fp or "fixer_iter1" in fp or "solver_validate_iter1" in fp):
                        print(f"\n=== FILE: {fp} ===")
                        print(file_info.get("content", "")[:3000])
                    elif "solution.py" in fp and "iter1" in fp:
                        print(f"\n=== FILE: {fp} ===")
                        print(file_info.get("content", "")[:3000])
                    elif "eval_kernel" in fp and "iter1" in fp:
                        print(f"\n=== FILE: {fp} ===")
                        print(file_info.get("content", "")[:3000])

    # Also look for assistant text summaries
    if obj.get("type") == "assistant":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        for c in content:
            if c.get("type") == "text":
                text = c.get("text", "")
                if len(text) > 100 and ("iter" in text.lower() or "hack" in text.lower() or "reward" in text.lower()):
                    # Only print substantive text blocks
                    if any(kw in text.lower() for kw in ["iter1", "iteration 1", "hacker_iter1", "fixer_iter1"]):
                        print(f"\n=== ASSISTANT TEXT (contains iter1 reference) ===")
                        print(text[:2000])
